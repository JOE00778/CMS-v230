"""模块 #27 数据获取 · 外部 API 拉回的原始数据浏览 + 调度管理.

两个数据源顶层 tab：
  - NST  · NetSuite API（商品マスタ / 在庫 / 売上 / スケジュール / 手動更新 / 履歴）
  - JDL  · 京东物流海外仓 OpenAPI（库存 / 调度+手动同步 / 拉取历史）

PG schema：
  - nst.item_master_raw / nst.inventory_snapshot / nst.sales_monthly / nst._pull_runs / nst.pull_schedule
  - jdl.inventory_snapshot / jdl.pull_log / jdl.pull_schedule

データは元川の PG に閉じる（原価含むため require_admin）。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from shared.i18n import t, lang_selector, get_lang
from shared.db import get_connection

# テーブル列見出し: (中文, 日本語) — UI 言語に追従。生の列名(handling_cd 等)を出さない。
_COL_LABELS = {
    "internal_id":        ("内部ID", "内部ID"),
    "jan":                ("UPC编码", "UPCコード"),
    "item_code":          ("名称", "名前"),
    "display_name":       ("显示名", "表示名"),
    "maker":              ("厂商", "メーカー名"),
    "item_rank":          ("商品等级", "商品ランク"),
    "handling_cd":        ("经销状态", "取扱区分"),
    "carton_qty":         ("整箱入数", "カートン入数"),
    "order_lot":          ("订货批量", "発注ロット"),
    "cost":               ("购入价格", "購入価格"),
    "average_cost":       ("平均成本", "平均原価"),
    "last_purchase_cost": ("上次购入价", "前回購入価格"),
    "cost_estimate":      ("定义成本", "アイテム定義原価"),
    "department":         ("部门", "部門"),
    "last_modified":      ("最终更新日", "最終更新日"),
    "qty_on_hand":        ("现有库存", "手持"),
    "qty_available":      ("可用库存", "利用可能"),
    "qty_on_order":       ("订货中", "注文済"),
    "warehouse":          ("仓库", "在庫保管場所"),
    # 売上
    "shop":               ("店铺", "FB_店舗"),
    "qty_sold":           ("销售数量", "販売数量"),
    "revenue":            ("总收益", "総収益"),
    "teigi_genka":        ("定义原价", "定義原価"),
    "arari":              ("毛利", "粗利"),
    "arari_rate":         ("毛利率", "粗利率"),
    # 仕入先マスタ（vendor_master）
    "vendor_code":        ("供货商代码", "仕入先コード"),
    "company_name":       ("公司名", "会社名"),
    "legal_name":         ("法人名", "正式名称"),
    "payment_terms":      ("付款条件", "支払条件"),
    "category":           ("分类", "カテゴリ"),
    "email":              ("邮箱", "メール"),
    "phone":              ("电话", "電話"),
    "currency":           ("货币", "通貨"),
    "is_inactive":        ("已停用", "無効"),
    "comments":           ("备注", "備考"),
    "date_created":       ("建立日", "登録日"),
}


def _cc(*keys) -> dict:
    """指定列の column_config（UI 言語追従）。"""
    ja = get_lang() == "ja"
    return {k: (_COL_LABELS[k][1] if ja else _COL_LABELS[k][0]) for k in keys}

st.set_page_config(page_title=t("数据获取"), page_icon="📥", layout="wide")
from shared.auth import require_admin, require_extra_password
require_admin()
require_extra_password("sys", "SYS_SETTINGS_PW", default="1001")  # 系统设置二级密码
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📥 数据获取"))
st.caption(t("外部 API 拉回的原始数据 + 调度管理（NST NetSuite / JDL 京东物流海外仓）"))


def _query(sql: str, params: tuple = ()):
    """単一 SELECT を独立 try/except。失敗時 rollback で PG トランザクション復旧。"""
    try:
        cur = conn.execute(sql, params) if params else conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols), None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, str(e)


# カテゴリ / 頻度 の日本語ラベル（技術用語をプレーン表示に）
_CAT_LABEL = {"basic": "📦 商品マスタ", "inventory": "🏬 在庫", "sales": "💰 売上",
              "purchase": "🧾 発注書(PO)", "vendor": "🏭 仕入先マスタ"}
_FREQ_LABEL = {"daily": "毎日", "monthly": "毎月"}


def _job_title(category, frequency, run_time) -> str:
    cat = _CAT_LABEL.get(category, category)
    freq = _FREQ_LABEL.get(frequency, frequency)
    return f"{cat}  ·  {freq} {str(run_time)[:5]} 自動取得"


# ============================================================
# 顶层 tab: NST / JDL / Coupang 三个数据源
# ============================================================
top_nst, top_jdl, top_cp = st.tabs([
    t("🏬 NST（NetSuite）"),
    t("🚚 JDL（京东物流海外仓）"),
    t("🇰🇷 Coupang（韩国店）"),
])

# ──────────────────────────────────────────────────────────────
# NST 顶层下嵌套现有 6 个子 tab
# ──────────────────────────────────────────────────────────────
with top_nst:
    tab1, tab2, tab_sales, tab_vendor, tab_sched, tab_manual, tab3 = st.tabs([
        t("📦 商品マスタ"),
        t("🏬 在庫 (JD-物流-千葉)"),
        t("💰 売上"),
        t("🏭 仕入先マスタ"),
        t("⏰ スケジュール設定"),
        t("▶️ 手動更新"),
        t("📜 取得履歴"),
    ])

# ============================================================
# Tab 1: 商品マスタ nst.item_master_raw
# ============================================================
    with tab1:
        cnt_df, err = _query(
            "SELECT count(*) c, count(maker) m, count(item_rank) r, "
            "count(carton_qty) ca, count(order_lot) lo FROM nst.item_master_raw"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
        else:
            row = cnt_df.iloc[0]
            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric(t("総件数"), f"{int(row['c']):,}")
            c2.metric(t("メーカー名あり"), f"{int(row['m']):,}")
            c3.metric(t("ランク評価あり"), f"{int(row['r']):,}")
            c4.metric(t("カートン入数あり"), f"{int(row['ca']):,}")
            c5.metric(t("発注ロットあり"), f"{int(row['lo']):,}")
    
            # フィルタ
            makers_df, _ = _query(
                "SELECT DISTINCT maker FROM nst.item_master_raw "
                "WHERE maker IS NOT NULL ORDER BY maker"
            )
            ranks_df, _ = _query(
                "SELECT DISTINCT item_rank FROM nst.item_master_raw "
                "WHERE item_rank IS NOT NULL ORDER BY item_rank"
            )
            fc1, fc2, fc3 = st.columns([2, 2, 3])
            maker_opts = makers_df["maker"].tolist() if makers_df is not None else []
            rank_opts = ranks_df["item_rank"].tolist() if ranks_df is not None else []
            sel_maker = fc1.multiselect(t("メーカー名"), maker_opts, placeholder=t("（全て）"))
            sel_rank = fc2.multiselect(t("ランク評価"), rank_opts, placeholder=t("（全て）"))
            kw = fc3.text_input(t("JAN / 商品名 検索"), placeholder="JAN コード or 表示名の一部")
    
            where, params = [], []
            if sel_maker:
                where.append("maker IN (" + ",".join(["?"] * len(sel_maker)) + ")"); params.extend(sel_maker)
            if sel_rank:
                where.append("item_rank IN (" + ",".join(["?"] * len(sel_rank)) + ")"); params.extend(sel_rank)
            if kw.strip():
                where.append("(jan LIKE ? OR display_name LIKE ? OR item_code LIKE ?)")
                like = f"%{kw.strip()}%"; params += [like, like, like]
            where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    
            LIMIT = 2000
            df, err2 = _query(
                "SELECT internal_id, jan, item_code, display_name, maker, item_rank, "
                "handling_cd, carton_qty, order_lot, cost, average_cost, "
                "last_purchase_cost, cost_estimate, department, last_modified "
                f"FROM nst.item_master_raw{where_sql} "
                "ORDER BY display_name LIMIT ?",
                tuple(params) + (LIMIT,),
            )
            if err2:
                st.error(err2)
            elif df is not None:
                st.caption(t("表示件数（最大 {n} 件）: ").format(n=LIMIT) + f"{len(df):,}")
                st.dataframe(
                    df, use_container_width=True, height=560,
                    column_config=_cc(
                        "internal_id", "jan", "item_code", "display_name", "maker",
                        "item_rank", "handling_cd", "carton_qty", "order_lot",
                        "cost", "average_cost", "last_purchase_cost", "cost_estimate",
                        "department", "last_modified",
                    ),
                )
    
    # ============================================================
    # Tab 2: 在庫 nst.inventory_snapshot（最新日 × item マスタ join）
    # ============================================================
    with tab2:
        meta_df, err = _query(
            "SELECT max(snapshot_date) d, count(*) c FROM nst.inventory_snapshot "
            "WHERE snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot)"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
        else:
            m = meta_df.iloc[0]
            c1, c2 = st.columns(2)
            c1.metric(t("最新スナップショット日"), str(m["d"]))
            c2.metric(t("在庫行数"), f"{int(m['c']):,}")
    
            kw = st.text_input(t("JAN / 商品名 検索 "), key="inv_kw",
                               placeholder="JAN コード or 表示名の一部")
            where, params = ["inv.snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot)"], []
            if kw.strip():
                where.append("(im.jan LIKE ? OR im.display_name LIKE ?)")
                like = f"%{kw.strip()}%"; params += [like, like]
            where_sql = " WHERE " + " AND ".join(where)
    
            df, err2 = _query(
                "SELECT im.jan, im.display_name, im.maker, im.item_rank, "
                "inv.qty_on_hand, inv.qty_available, inv.qty_on_order, inv.warehouse "
                "FROM nst.inventory_snapshot inv "
                "LEFT JOIN nst.item_master_raw im ON im.internal_id = inv.item_internal_id "
                f"{where_sql} ORDER BY inv.qty_on_hand DESC LIMIT 2000",
                tuple(params),
            )
            if err2:
                st.error(err2)
            elif df is not None:
                st.caption(t("表示件数（最大 2000 件）: ") + f"{len(df):,}")
                st.dataframe(
                    df, use_container_width=True, height=560,
                    column_config=_cc(
                        "jan", "display_name", "maker", "item_rank",
                        "qty_on_hand", "qty_available", "qty_on_order", "warehouse",
                    ),
                )
    
    # ============================================================
    # Tab 売上 nst.sales_monthly（店舗別 / アイテム別）
    # ============================================================
    with tab_sales:
        months_df, err = _query(
            "SELECT DISTINCT year_month FROM nst.sales_monthly ORDER BY year_month DESC"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
        elif months_df is None or months_df.empty:
            st.info(t("売上データ未取得（sales ジョブ実行後に表示）"))
        else:
            c1, c2 = st.columns([1.5, 3])
            ym = c1.selectbox(t("対象月"), months_df["year_month"].tolist())
            view = c2.radio(t("表示単位"), [t("店舗別"), t("アイテム別")],
                            horizontal=True, key="sales_view")
    
            if view == t("店舗別"):
                df, e2 = _query(
                    "SELECT s.shop, im.jan, im.display_name, im.maker, im.item_rank, "
                    "s.qty_sold, s.revenue, "
                    "(COALESCE(im.cost_estimate,0) * s.qty_sold) AS teigi_genka "
                    "FROM nst.sales_monthly s "
                    "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
                    "WHERE s.year_month = ? ORDER BY s.revenue DESC LIMIT 5000",
                    (ym,),
                )
                cols = ("shop", "jan", "display_name", "maker", "item_rank",
                        "qty_sold", "revenue", "teigi_genka", "arari", "arari_rate")
            else:
                df, e2 = _query(
                    "SELECT im.jan, im.display_name, im.maker, im.item_rank, "
                    "SUM(s.qty_sold) AS qty_sold, SUM(s.revenue) AS revenue, "
                    "(MAX(COALESCE(im.cost_estimate,0)) * SUM(s.qty_sold)) AS teigi_genka "
                    "FROM nst.sales_monthly s "
                    "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
                    "WHERE s.year_month = ? "
                    "GROUP BY im.jan, im.display_name, im.maker, im.item_rank "
                    "ORDER BY revenue DESC LIMIT 5000",
                    (ym,),
                )
                cols = ("jan", "display_name", "maker", "item_rank",
                        "qty_sold", "revenue", "teigi_genka", "arari", "arari_rate")
    
            if e2:
                st.error(e2)
            elif df is not None and not df.empty:
                df["revenue"] = df["revenue"].astype(float)
                df["teigi_genka"] = df["teigi_genka"].astype(float)
                df["arari"] = df["revenue"] - df["teigi_genka"]
                df["arari_rate"] = (df["arari"] / df["revenue"].where(df["revenue"] != 0)).round(4)
                tot_q = df["qty_sold"].astype(float).sum()
                tot_r = df["revenue"].sum()
                tot_g = df["arari"].sum()
                m1, m2, m3 = st.columns(3)
                m1.metric(t("販売数量 計"), f"{tot_q:,.0f}")
                m2.metric(t("総収益 計"), f"¥{tot_r:,.0f}")
                m3.metric(t("粗利 計"), f"¥{tot_g:,.0f}（{(tot_g/tot_r if tot_r else 0):.1%}）")
                st.caption(t("表示件数（最大 5000 件）: ") + f"{len(df):,}")
                st.dataframe(df[list(cols)], use_container_width=True, height=520,
                             column_config=_cc(*cols))
            else:
                st.info(t("この月のデータがありません"))
    
    # ============================================================
    # Tab 仕入先マスタ nst.vendor_master（NetSuite Vendor ミラー）
    #   表示 + 「今すぐ更新」(vendor_daily を run_now 予約)。pull_vendor が UPSERT。
    # ============================================================
    with tab_vendor:
        st.caption(t("NetSuite の仕入先（Vendor）レコード。vendor_daily で日次更新 · 即時は下のボタン。"))

        # 即時更新ボタン（vendor_daily を run_now 予約）
        vjob, _ = _query(
            "SELECT run_now, last_run_at, last_status FROM nst.pull_schedule "
            "WHERE job_key='vendor_daily'"
        )
        vc1, vc2, vc3 = st.columns([2, 2, 2])
        if vjob is not None and not vjob.empty:
            _vr = vjob.iloc[0]
            vc2.caption(t("前回更新") + ": " +
                        (str(_vr["last_run_at"])[:16] if pd.notna(_vr["last_run_at"]) else "-"))
            vc3.caption(t("前回状態") + ": " +
                        (str(_vr["last_status"]) if pd.notna(_vr["last_status"]) else "-"))
            if bool(_vr["run_now"]):
                vc1.info(t("⏳ 更新待ち…"))
            elif vc1.button(t("▶️ 今すぐ更新"), key="run_vendor", type="primary"):
                try:
                    conn.execute(
                        "UPDATE nst.pull_schedule SET run_now=TRUE, updated_at=now() "
                        "WHERE job_key='vendor_daily'")
                    conn.commit()
                    st.success(t("仕入先マスタ更新を予約しました（1分以内に実行）"))
                    st.rerun()
                except Exception as e:
                    conn.rollback()
                    st.error(str(e))
        else:
            vc1.warning(t("vendor_daily ジョブ未登録（017 マイグレーション未適用？）"))

        st.divider()
        vcnt, verr = _query(
            "SELECT count(*) c, count(*) FILTER (WHERE NOT is_inactive) a, "
            "max(pulled_at) p FROM nst.vendor_master"
        )
        if verr:
            st.info(t("仕入先マスタ未取得（テーブル未作成 or 接続エラー）") + f"\n\n{verr}")
        elif vcnt is not None and int(vcnt.iloc[0]["c"]) == 0:
            st.info(t("仕入先マスタが空です。上の「今すぐ更新」で取得してください。"))
        elif vcnt is not None:
            _vrow = vcnt.iloc[0]
            m1, m2, m3 = st.columns(3)
            m1.metric(t("仕入先総数"), f"{int(_vrow['c']):,}")
            m2.metric(t("有効"), f"{int(_vrow['a']):,}")
            m3.caption(t("最終取得") + ": " +
                       (str(_vrow["p"])[:16] if pd.notna(_vrow["p"]) else "-"))

            vf1, vf2 = st.columns([2, 3])
            _show_inactive = vf1.checkbox(t("無効も表示"), value=False, key="vendor_show_inactive")
            vkw = vf2.text_input(t("仕入先名 / コード 検索"),
                                 placeholder=t("会社名・コードの一部"), key="vendor_kw")
            vwhere, vparams = [], []
            if not _show_inactive:
                vwhere.append("NOT is_inactive")
            if vkw.strip():
                vwhere.append("(vendor_code LIKE ? OR company_name LIKE ? OR legal_name LIKE ?)")
                _vl = f"%{vkw.strip()}%"; vparams += [_vl, _vl, _vl]
            vwhere_sql = (" WHERE " + " AND ".join(vwhere)) if vwhere else ""
            vdf, verr2 = _query(
                "SELECT vendor_code, company_name, legal_name, payment_terms, category, "
                "currency, email, phone, is_inactive, comments, date_created "
                f"FROM nst.vendor_master{vwhere_sql} ORDER BY vendor_code LIMIT 2000",
                tuple(vparams),
            )
            if verr2:
                st.error(verr2)
            elif vdf is not None:
                st.caption(t("表示件数: ") + f"{len(vdf):,}")
                st.dataframe(
                    vdf, use_container_width=True, height=560,
                    column_config=_cc(
                        "vendor_code", "company_name", "legal_name", "payment_terms",
                        "category", "currency", "email", "phone", "is_inactive",
                        "comments", "date_created",
                    ),
                )

    # ============================================================
    # Tab スケジュール設定 nst.pull_schedule（編集）
    # ============================================================
    with tab_sched:
        st.subheader(t("定时取得スケジュール"))
        st.caption(t("元川の常駐ディスパッチャが毎分この設定を見て daily_pull を起動します"))
        sched_df, err = _query(
            "SELECT job_key, category, frequency, domains, enabled, run_time, "
            "run_day, last_status, last_run_at, description "
            "FROM nst.pull_schedule ORDER BY job_key"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
        elif sched_df is None or sched_df.empty:
            st.info(t("スケジュール未登録"))
        else:
            with st.form("sched_form"):
                new_vals = {}
                for _, r in sched_df.iterrows():
                    jk = r["job_key"]
                    st.markdown("**" + _job_title(r["category"], r["frequency"], r["run_time"]) + "**")
                    if r.get("description"):
                        st.caption("📋 " + str(r["description"]))
                    st.caption(f"🆔 ジョブID: {jk}　|　取得対象(domains): {r['domains']}")
                    c1, c2, c3, c4 = st.columns([1.2, 1.5, 2, 2])
                    en = c1.checkbox(t("有効"), value=bool(r["enabled"]), key=f"en_{jk}")
                    rt = c2.text_input(t("起動時刻 HH:MM"), value=str(r["run_time"])[:5], key=f"rt_{jk}")
                    rd = None
                    if r["frequency"] == "monthly":
                        rd = c3.number_input(t("実行日(1-28)"), 1, 28,
                                             value=int(r["run_day"] or 1), key=f"rd_{jk}")
                    else:
                        c3.caption(t("（毎日）"))
                    last = r["last_run_at"]
                    c4.caption(t("最終: ") + (str(last)[:16] if last is not None else "-")
                               + f" / {r['last_status'] or '-'}")
                    new_vals[jk] = (en, rt, rd)
                if st.form_submit_button(t("💾 保存"), type="primary"):
                    ok = 0
                    for jk, (en, rt, rd) in new_vals.items():
                        try:
                            conn.execute(
                                "UPDATE nst.pull_schedule SET enabled=?, run_time=?, "
                                "run_day=?, updated_at=now() WHERE job_key=?",
                                (en, rt.strip(), rd, jk),
                            )
                            ok += 1
                        except Exception as e:
                            conn.rollback()
                            st.error(f"{jk}: {e}")
                    conn.commit()
                    st.success(t("保存しました（{n} 件）").format(n=ok))
                    st.rerun()
    
    # ============================================================
    # Tab 手動更新（run_now フラグを立てる）
    # ============================================================
    with tab_manual:
        st.subheader(t("手動でデータ取得"))
        st.caption(t("「今すぐ取得」を押すと、常駐ディスパッチャが1分以内に daily_pull を起動します"))
        jobs_df, err = _query(
            "SELECT job_key, category, frequency, run_time, domains, run_now, "
            "last_status, description FROM nst.pull_schedule WHERE enabled ORDER BY job_key"
        )
        if err:
            st.error(t("接続エラー: ") + err)
        elif jobs_df is None or jobs_df.empty:
            st.info(t("有効なジョブがありません"))
        else:
            for _, r in jobs_df.iterrows():
                jk = r["job_key"]
                c1, c2, c3 = st.columns([3.5, 1.5, 2])
                c1.markdown("**" + _job_title(r["category"], r["frequency"], r["run_time"]) + "**")
                if r.get("description"):
                    c1.caption("📋 " + str(r["description"]))
                c2.caption(f"前回: {r['last_status'] or '-'}")
                if r["run_now"]:
                    c3.info(t("⏳ 実行待ち…"))
                elif c3.button(t("▶️ 今すぐ取得"), key=f"run_{jk}", type="primary"):
                    try:
                        conn.execute(
                            "UPDATE nst.pull_schedule SET run_now=TRUE, updated_at=now() "
                            "WHERE job_key=?", (jk,))
                        conn.commit()
                        st.success(t("{j} を予約しました（1分以内に実行）").format(j=jk))
                        st.rerun()
                    except Exception as e:
                        conn.rollback()
                        st.error(str(e))
    
    # ============================================================
    # Tab 3: 取得履歴 nst._pull_runs
    # ============================================================
    with tab3:
        df, err = _query(
            "SELECT run_id, started_at, finished_at, domains, auth_mode, "
            "overall_status, summary_json "
            "FROM nst._pull_runs ORDER BY run_id DESC LIMIT 50"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
        elif df is not None:
            st.caption(t("直近 50 回の取得実行"))
            st.dataframe(
                df, use_container_width=True, height=480,
                column_config={
                    "run_id": "run_id",
                    "started_at": t("開始"),
                    "finished_at": t("終了"),
                    "domains": t("対象"),
                    "auth_mode": t("認証"),
                    "overall_status": t("結果"),
                },
            )


# ============================================================
# JDL 顶层 tab · 3 个子 tab（库存数据 / 调度+手动同步 / 拉取历史）
# ============================================================
with top_jdl:
    j_inv, j_sched, j_log = st.tabs([
        t("📦 库存数据"),
        t("▶️ 调度 + 手动同步"),
        t("📜 拉取历史"),
    ])

    # ── 库存数据：jdl.inventory_snapshot 最新快照 ─────────────
    with j_inv:
        meta_df, err = _query(
            "SELECT max(snapshot_date) d, count(*) c FROM jdl.inventory_snapshot "
            "WHERE snapshot_date = (SELECT max(snapshot_date) FROM jdl.inventory_snapshot)"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
        elif meta_df is None or meta_df.empty or meta_df["d"].iloc[0] is None:
            st.info(t("尚无 JDL 库存数据。请到「调度 + 手动同步」tab 触发首次拉取。"))
        else:
            m = meta_df.iloc[0]
            c1, c2 = st.columns(2)
            c1.metric(t("最新快照日"), str(m["d"]))
            c2.metric(t("库存行数"), f"{int(m['c']):,}")

            kw = st.text_input(t("JAN / 商品名 模糊搜索"), key="jdl_inv_kw",
                               placeholder="JAN コード or 商品名の一部")
            where = ["snapshot_date = (SELECT max(snapshot_date) FROM jdl.inventory_snapshot)"]
            params: list = []
            if kw.strip():
                where.append("(jan LIKE ? OR goods_name LIKE ?)")
                like = f"%{kw.strip()}%"
                params += [like, like]
            df, err2 = _query(
                "SELECT jan, jd_goods_id, goods_name, warehouse_code, warehouse_name, "
                "       qty_total, qty_available, qty_preoccupied, qty_inbound, "
                "       qty_operator_lock, qty_inventory_lock, stock_level_name "
                "FROM jdl.inventory_snapshot WHERE " + " AND ".join(where) +
                " ORDER BY qty_total DESC LIMIT 2000",
                tuple(params),
            )
            if err2:
                st.error(err2)
            elif df is not None:
                st.caption(t("表示件数（最大 2000 件）: ") + f"{len(df):,}")
                st.dataframe(df, use_container_width=True, height=560,
                             column_config={
                                 "jan": "JAN (customerGoodsId)",
                                 "jd_goods_id": t("JD商品ID"),
                                 "goods_name": t("商品名"),
                                 "warehouse_code": t("仓库代码"),
                                 "warehouse_name": t("仓库名"),
                                 "qty_total": t("总库存"),
                                 "qty_available": t("可用"),
                                 "qty_preoccupied": t("订单预占"),
                                 "qty_inbound": t("在途"),
                                 "qty_operator_lock": t("操作锁定"),
                                 "qty_inventory_lock": t("库存锁定"),
                                 "stock_level_name": t("等级"),
                             })

    # ── 调度 + 手动同步：jdl.pull_schedule ────────────────────
    with j_sched:
        st.subheader(t("JDL 库存定时拉取"))
        st.caption(t(
            "元川的常驻 jdl_scheduler 容器毎分检查此表，到时间或 run_now=TRUE 时启动 daily_pull。"
        ))
        jdl_sched_df, err = _query(
            "SELECT job_key, frequency, enabled, run_time, run_now, "
            "       last_run_at, last_status, description "
            "FROM jdl.pull_schedule ORDER BY job_key"
        )
        if err:
            st.error(t("接続エラー: ") + err)
        elif jdl_sched_df is None or jdl_sched_df.empty:
            st.info(t("スケジュール未登録（跑一次 sql/020_create_pull_schedule.sql）"))
        else:
            for _, r in jdl_sched_df.iterrows():
                jk = r["job_key"]
                st.markdown(f"**🚚 {jk}**  ·  {r['frequency']} {str(r['run_time'])[:5]} 自動")
                if r.get("description"):
                    st.caption("📋 " + str(r["description"]))
                c1, c2, c3 = st.columns([2, 2, 1.5])
                c1.caption(t("上次同步") + ": " +
                           (str(r["last_run_at"])[:16] if r["last_run_at"] is not None else "-"))
                c2.caption(t("上次状态") + ": " + (r["last_status"] or "-"))
                if r["run_now"]:
                    c3.info(t("⏳ 同步中…"))
                else:
                    if c3.button(t("▶️ 立即同步"), key=f"jdl_run_{jk}", type="primary"):
                        try:
                            conn.execute(
                                "UPDATE jdl.pull_schedule SET run_now=TRUE, updated_at=now() "
                                "WHERE job_key=?", (jk,))
                            conn.commit()
                            st.success(t("已触发 · 1 分钟内启动 daily_pull"))
                            st.rerun()
                        except Exception as e:
                            conn.rollback()
                            st.error(str(e))
                st.divider()

    # ── 拉取历史：jdl.pull_log ───────────────────────────────
    with j_log:
        st.caption(t("直近 50 回の取得実行（jdl.pull_log）"))
        df, err = _query(
            "SELECT id, started_at, finished_at, status, trigger_source, "
            "       pages_fetched, rows_written, rows_total, error_code, error_message "
            "FROM jdl.pull_log ORDER BY id DESC LIMIT 50"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
        elif df is not None and not df.empty:
            st.dataframe(df, use_container_width=True, height=480,
                         column_config={
                             "id": "ID",
                             "started_at": t("開始"),
                             "finished_at": t("終了"),
                             "status": t("結果"),
                             "trigger_source": t("触发"),
                             "pages_fetched": t("拉页数"),
                             "rows_written": t("写入行数"),
                             "rows_total": t("总行数(JDL)"),
                             "error_code": t("错误码"),
                             "error_message": t("错误信息"),
                         })
        else:
            st.info(t("尚无拉取记录"))


# ============================================================
# Coupang 顶层 tab · 3 个子 tab（结算数据 / 调度+手动同步 / 拉取历史）
#   coupang.settlement / coupang.pull_schedule / coupang.pull_log
#   展示层在 page 05 店铺毛利「店铺扣减」tab · ここは原始数据 + 調度管理
# ============================================================
with top_cp:
    c_set, c_sched, c_log = st.tabs([
        t("💰 结算数据"),
        t("▶️ 调度 + 手动同步"),
        t("📜 拉取历史"),
    ])

    # ── 结算数据：coupang.settlement ─────────────────────────
    with c_set:
        meta_df, err = _query(
            "SELECT count(*) c, max(settlement_date) d, max(pulled_at) p "
            "FROM coupang.settlement"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
            st.info(t("初次使用需在元川 PG 跑 coupang_api/sql/000 迁移并启动 coupang_scheduler 容器"))
        elif meta_df is None or int(meta_df.iloc[0]["c"]) == 0:
            st.info(t("尚无 Coupang 结算数据。请到「调度 + 手动同步」tab 触发首次拉取。"))
        else:
            m = meta_df.iloc[0]
            c1, c2, c3 = st.columns(3)
            c1.metric(t("结算单行数"), f"{int(m['c']):,}")
            c2.metric(t("最新结算日"), str(m["d"]))
            c3.caption(t("最終取得") + ": " +
                       (str(m["p"])[:16] if pd.notna(m["p"]) else "-"))
            df, err2 = _query(
                "SELECT revenue_recognition_year_month, settlement_type, "
                "settlement_date, status, total_sale, service_fee, "
                "seller_service_fee, seller_discount_coupon, downloadable_coupon, "
                "store_fee_discount, deduction_amount, debt_of_last_week, "
                "courantee_fee, courantee_customer_reward, "
                "settlement_amount, last_amount, final_amount, pulled_at "
                "FROM coupang.settlement "
                "ORDER BY revenue_recognition_year_month DESC, settlement_date "
                "LIMIT 500"
            )
            if err2:
                st.error(err2)
            elif df is not None:
                st.caption(t("金额单位 KRW · 每行 = 销售认知月 × 结算类型 × 结算日"))
                st.dataframe(df, use_container_width=True, height=520,
                             column_config={
                                 "revenue_recognition_year_month": t("销售认知月"),
                                 "settlement_type": t("结算类型"),
                                 "settlement_date": t("结算日"),
                                 "status": t("状态"),
                                 "total_sale": t("实际销售额"),
                                 "service_fee": t("销售手续费"),
                                 "seller_service_fee": t("MyShop手续费"),
                                 "seller_discount_coupon": t("立减优惠券"),
                                 "downloadable_coupon": t("下载优惠券"),
                                 "store_fee_discount": t("店铺手续费折扣"),
                                 "deduction_amount": t("结算扣款"),
                                 "debt_of_last_week": t("上周未清欠款"),
                                 "courantee_fee": "Courantee",
                                 "courantee_customer_reward": t("Courantee补偿"),
                                 "settlement_amount": t("结算额"),
                                 "last_amount": t("尾款保留"),
                                 "final_amount": t("最终支付额"),
                                 "pulled_at": t("取得时刻"),
                             })

    # ── 调度 + 手动同步：coupang.pull_schedule ────────────────
    with c_sched:
        st.subheader(t("Coupang 结算定时拉取"))
        st.caption(t(
            "元川的常驻 coupang_scheduler 容器毎分检查此表，到时间或 run_now=TRUE 时启动 daily_pull。"
            "每次拉取当月+前2个月（覆盖尾款与已付状态翻转）。"
        ))
        cp_sched_df, err = _query(
            "SELECT job_key, frequency, enabled, run_time, run_now, "
            "       last_run_at, last_status, description "
            "FROM coupang.pull_schedule ORDER BY job_key"
        )
        if err:
            st.error(t("接続エラー: ") + err)
        elif cp_sched_df is None or cp_sched_df.empty:
            st.info(t("スケジュール未登録（跑一次 coupang_api/sql/000_create_coupang_schema.sql）"))
        else:
            for _, r in cp_sched_df.iterrows():
                jk = r["job_key"]
                st.markdown(f"**🇰🇷 {jk}**  ·  {r['frequency']} {str(r['run_time'])[:5]} 自動")
                if r.get("description"):
                    st.caption("📋 " + str(r["description"]))
                c1, c2, c3 = st.columns([2, 2, 1.5])
                c1.caption(t("上次同步") + ": " +
                           (str(r["last_run_at"])[:16] if r["last_run_at"] is not None else "-"))
                c2.caption(t("上次状态") + ": " + (r["last_status"] or "-"))
                if r["run_now"]:
                    c3.info(t("⏳ 同步中…"))
                else:
                    if c3.button(t("▶️ 立即同步"), key=f"cp_run_{jk}", type="primary"):
                        try:
                            conn.execute(
                                "UPDATE coupang.pull_schedule SET run_now=TRUE, updated_at=now() "
                                "WHERE job_key=?", (jk,))
                            conn.commit()
                            st.success(t("已触发 · 1 分钟内启动 daily_pull"))
                            st.rerun()
                        except Exception as e:
                            conn.rollback()
                            st.error(str(e))
                st.divider()

    # ── 拉取历史：coupang.pull_log ───────────────────────────
    with c_log:
        st.caption(t("直近 50 回の取得実行（coupang.pull_log）· 403=IP白名单 · 401=key失效(180天)"))
        df, err = _query(
            "SELECT id, started_at, finished_at, status, trigger_source, months, "
            "       fetched, upserted, errors, error_message "
            "FROM coupang.pull_log ORDER BY id DESC LIMIT 50"
        )
        if err:
            st.error(t("テーブル未取得 or 接続エラー: ") + err)
        elif df is not None and not df.empty:
            st.dataframe(df, use_container_width=True, height=480,
                         column_config={
                             "id": "ID",
                             "started_at": t("開始"),
                             "finished_at": t("終了"),
                             "status": t("結果"),
                             "trigger_source": t("触发"),
                             "months": t("对象月"),
                             "fetched": t("拉取行数"),
                             "upserted": t("写入行数"),
                             "errors": t("错误数"),
                             "error_message": t("错误信息"),
                         })
        else:
            st.info(t("尚无拉取记录"))
