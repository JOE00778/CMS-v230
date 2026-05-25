"""模块 #22 数据库管理 · 一眼看清上传是否真入库 + 失败原因.

主要解决问题: Boss 在 page 99 上传 xls 显示成功, 但业务页 (04/05/14)
还是空数据.根因往往是 ingester 中途异常被 swallow.

本 page 提供 3 个视图:
1. 业务表实时行数 + 最新写入时间
2. _ingest_runs 历史 (最近 50 次上传, 含 inserted/errors)
3. _ingest_errors 失败行明细 (近 100 条, 含 error_message)
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
from shared.i18n import t, lang_selector
from shared.db import get_connection

st.set_page_config(page_title=t("数据库管理"), page_icon="🗄️", layout="wide")
from shared.auth import require_admin, require_extra_password
require_admin()
require_extra_password("sys", "SYS_SETTINGS_PW", default="1001")  # 系统设置二级密码
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🗄️ 数据库管理"))
st.caption(t(
    "诊断上传是否真入库 · 看 ingester 失败原因 · 业务表行数概览"
))


# ============================================================
# Tab 1: 业务表行数总览
# ============================================================
TABLES_TO_CHECK = [
    # (table_name, business_meaning)
    ("sales_line", t("销售明细 (NST 4 类导出)")),
    ("nst_inventory_snapshot", t("库存快照 (在庫のスナップショット)")),
    ("nst_turnover", t("库存周转率 (在庫回転率)")),
    ("inventory_snapshot", t("库存快照 (旧版输出)")),
    ("inventory_turnover", t("库存周转率 (旧版输出)")),
    ("item_v2", t("商品主档 v2")),
    ("item_inventory_snapshot_v2", t("库存快照 v2 (多仓 + bin)")),
    ("shop_sales", t("店铺销售 v2 (按颗粒度)")),
    ("item_monthly_turnover", t("月度完売率")),
    ("shopee_orders_raw", t("Shopee 订单导出 (财务源)")),
    ("shopee_income_lines", t("Shopee 拨款明细 (财务源)")),
    ("operation_advice_monthly", t("运营建议月度")),
    ("rank_history", t("等级历史")),
    ("discontinue_alerts", t("改廃信号")),
    ("difficult_items", t("入荷困难商品")),
    ("purchase_history", t("订货历史")),
]

tab1, tab2, tab3 = st.tabs([
    t("📊 业务表概览"),
    t("📜 上传历史 (_ingest_runs)"),
    t("❌ 失败行明细 (_ingest_errors)"),
])

def _safe_query(sql: str):
    """每个 SELECT 独立 try/except, 失败 rollback 恢复 PG 事务,
    避免 'current transaction is aborted' 影响后续 query。"""
    try:
        return conn.execute(sql).fetchall(), None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, str(e)


with tab1:
    st.subheader(t("各业务表当前行数"))
    rows = []
    for tbl, meaning in TABLES_TO_CHECK:
        cnt_rows, err = _safe_query(f"SELECT COUNT(*) AS c FROM {tbl}")
        if err:
            rows.append({
                t("表名"): tbl,
                t("业务含义"): meaning,
                t("行数"): 0,
                t("最新写入"): "-",
                t("状态"): f"❌ {err[:60]}",
            })
            continue
        cnt = cnt_rows[0]["c"] if cnt_rows else 0
        latest_rows, _ = _safe_query(f"SELECT MAX(imported_at) AS m FROM {tbl}")
        latest = (
            str(latest_rows[0]["m"])[:19]
            if latest_rows and latest_rows[0]["m"] else "-"
        )
        rows.append({
            t("表名"): tbl,
            t("业务含义"): meaning,
            t("行数"): cnt,
            t("最新写入"): latest,
            t("状态"): "✅" if cnt > 0 else "⚠️ 空",
        })

    df_tables = pd.DataFrame(rows)
    st.dataframe(df_tables, use_container_width=True, hide_index=True, height=560)


with tab2:
    st.subheader(t("最近 50 次上传记录"))
    runs, err = _safe_query(
        "SELECT run_id, ingestor, source_file, total_rows, "
        "inserted, errors, run_at, notes "
        "FROM _ingest_runs ORDER BY run_id DESC LIMIT 50"
    )
    if err:
        st.error(f"_ingest_runs 表读取失败: {err}")
    elif not runs:
        st.info(t("暂无上传记录。到「⚙️ 数据导入与设置」上传文件后会自动生成。"))
    else:
        df_runs = pd.DataFrame([dict(r) for r in runs])
        df_runs[t("状态")] = df_runs.apply(
            lambda r: "❌ 全失败" if r["inserted"] == 0 and r["total_rows"] > 0
            else ("⚠️ 部分失败" if r["errors"] > 0 else "✅"),
            axis=1,
        )
        st.dataframe(
            df_runs, use_container_width=True, hide_index=True, height=560,
        )
        failed = df_runs[df_runs["inserted"] == 0]
        if not failed.empty:
            st.error(t(
                f"⚠️ {len(failed)} 次上传 inserted=0,文件读取了但没有任何行入库。"
                "切到「❌ 失败行明细」Tab 看具体错误信息。"
            ))


with tab3:
    st.subheader(t("最近 100 条失败行"))
    errs, err = _safe_query(
        "SELECT e.id, e.run_id, r.ingestor, r.source_file, "
        "e.row_number, e.error_message "
        "FROM _ingest_errors e "
        "LEFT JOIN _ingest_runs r ON r.run_id = e.run_id "
        "ORDER BY e.id DESC LIMIT 100"
    )
    if err:
        st.error(f"_ingest_errors 表读取失败: {err}")
    elif not errs:
        st.success(t("✅ 没有失败行记录。"))
    else:
        df_errs = pd.DataFrame([dict(r) for r in errs])
        st.dataframe(
            df_errs, use_container_width=True, hide_index=True, height=560,
        )
