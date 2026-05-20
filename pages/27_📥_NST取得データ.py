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
from shared.i18n import t, lang_selector
from shared.db import get_connection

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


tab1, tab2, tab_sched, tab_manual, tab3 = st.tabs([
    t("📦 商品マスタ"),
    t("🏬 在庫 (JD-物流-千葉)"),
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
                column_config={
                    "jan": "JAN",
                    "item_code": t("アイテム"),
                    "display_name": t("表示名"),
                    "maker": t("メーカー"),
                    "item_rank": t("ランク評価"),
                    "carton_qty": t("カートン入数"),
                    "order_lot": t("発注ロット"),
                    "cost": t("原価"),
                    "average_cost": t("平均原価"),
                    "cost_estimate": t("定義原価"),
                    "department": t("部門"),
                },
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
                column_config={
                    "jan": "JAN",
                    "display_name": t("表示名"),
                    "maker": t("メーカー"),
                    "item_rank": t("ランク評価"),
                    "qty_on_hand": t("手持"),
                    "qty_available": t("利用可能"),
                    "qty_on_order": t("注文済(入荷待ち)"),
                    "warehouse": t("倉庫"),
                },
            )

# ============================================================
# Tab スケジュール設定 nst.pull_schedule（編集）
# ============================================================
with tab_sched:
    st.subheader(t("定时取得スケジュール"))
    st.caption(t("元川の常駐ディスパッチャが毎分この設定を見て daily_pull を起動します"))
    sched_df, err = _query(
        "SELECT job_key, category, frequency, domains, enabled, run_time, "
        "run_day, last_status, last_run_at FROM nst.pull_schedule ORDER BY job_key"
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
                st.markdown(f"**{jk}**  ·  {r['category']} / {r['frequency']} / domains={r['domains']}")
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
        "SELECT job_key, domains, run_now, last_status FROM nst.pull_schedule "
        "WHERE enabled ORDER BY job_key"
    )
    if err:
        st.error(t("接続エラー: ") + err)
    elif jobs_df is None or jobs_df.empty:
        st.info(t("有効なジョブがありません"))
    else:
        for _, r in jobs_df.iterrows():
            jk = r["job_key"]
            c1, c2, c3 = st.columns([2, 2, 2])
            c1.markdown(f"**{jk}**")
            c2.caption(f"domains={r['domains']} / {r['last_status'] or '-'}")
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
