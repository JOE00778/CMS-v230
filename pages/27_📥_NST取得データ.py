"""模块 #27 NST 取得データ · NetSuite API で日次取得した生データを確認.

NST API daily_pull（database リポジトリ）が PG に書き込んだ nst.* スキーマを表示:
  - nst.item_master_raw     商品マスタ（メーカー/ランク/原価/カートン入数/発注ロット）
  - nst.inventory_snapshot  JD-物流-千葉 在庫（手持/利用可能/注文済）
  - nst._pull_runs          取得実行履歴（成否/件数/次回）

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
}


def _cc(*keys) -> dict:
    """指定列の column_config（UI 言語追従）。"""
    ja = get_lang() == "ja"
    return {k: (_COL_LABELS[k][1] if ja else _COL_LABELS[k][0]) for k in keys}

st.set_page_config(page_title=t("NST 取得データ"), page_icon="📥", layout="wide")
from shared.auth import require_admin
require_admin()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📥 NST 取得データ"))
st.caption(t("NetSuite API で日次取得した生データ（商品マスタ / 在庫 / 取得履歴）"))


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
_CAT_LABEL = {"basic": "📦 商品マスタ", "inventory": "🏬 在庫", "sales": "💰 売上"}
_FREQ_LABEL = {"daily": "毎日", "monthly": "毎月"}


def _job_title(category, frequency, run_time) -> str:
    cat = _CAT_LABEL.get(category, category)
    freq = _FREQ_LABEL.get(frequency, frequency)
    return f"{cat}  ·  {freq} {str(run_time)[:5]} 自動取得"


tab1, tab2, tab_sales, tab_sched, tab_manual, tab3 = st.tabs([
    t("📦 商品マスタ"),
    t("🏬 在庫 (JD-物流-千葉)"),
    t("💰 売上"),
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
        maker_opts = [t("（全て）")] + (makers_df["maker"].tolist() if makers_df is not None else [])
        rank_opts = [t("（全て）")] + (ranks_df["item_rank"].tolist() if ranks_df is not None else [])
        sel_maker = fc1.selectbox(t("メーカー名"), maker_opts)
        sel_rank = fc2.selectbox(t("ランク評価"), rank_opts)
        kw = fc3.text_input(t("JAN / 商品名 検索"), placeholder="JAN コード or 表示名の一部")

        where, params = [], []
        if sel_maker != t("（全て）"):
            where.append("maker = ?"); params.append(sel_maker)
        if sel_rank != t("（全て）"):
            where.append("item_rank = ?"); params.append(sel_rank)
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
