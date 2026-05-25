"""模块 #9 発注履歴 · 発注書(PO)明細クエリ.

数据源: nst.purchase_order_line（NetSuite PurchaseOrder 明細 · join item_master 出 JAN/品名）
功能: JAN / 仕入先 / PO番号 / 発注日 / 在途 筛选 + CSV 导出
旧 purchase_history 表已弃用（数据源切到 nst schema）。
"""
from __future__ import annotations

import datetime as dt
import re

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("発注履歴"), page_icon="📜", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📜 発注履歴"))
st.caption(t("発注書(PO)明細クエリ · 数据源 NetSuite 発注書"))

col1, col2, col3 = st.columns(3)
with col1:
    jan_filter_multi = st.text_area(
        t("🔍 JAN 搜索（换行 / 逗号分隔多个）"),
        placeholder="例:\n4901234567890\n4987654321098",
        height=100,
    )
with col2:
    vendor_filter = st.text_input(t("🔍 仕入先名（部分匹配）"), "")
    po_filter = st.text_input(t("🔍 PO 番号（部分匹配）"), "")
with col3:
    date_from = st.date_input(t("発注日 起（含当日）"), value=dt.date(2020, 1, 1))
    only_open = st.checkbox(t("仅看未完了（在途）"), value=False)

# --- 动态 WHERE（命名占位符 · 照 page28 PG 模式）---
where = ["pol.item_internal_id IS NOT NULL"]
params: dict = {}

jan_list = [j.strip() for j in re.split(r"[,\n\r]+", jan_filter_multi) if j.strip()]
if jan_list:
    where.append("im.jan = ANY(%(jans)s)")
    params["jans"] = jan_list
if vendor_filter:
    where.append("pol.vendor_name ILIKE %(vendor)s")
    params["vendor"] = f"%{vendor_filter}%"
if po_filter:
    where.append("pol.po_number ILIKE %(po)s")
    params["po"] = f"%{po_filter}%"
if date_from:
    where.append("pol.trandate >= %(dfrom)s")
    params["dfrom"] = date_from
if only_open:
    where.append("COALESCE(pol.closed, FALSE) = FALSE")
    where.append("(pol.quantity - COALESCE(pol.quantity_received, 0)) > 0")

LIMIT = 3000
sql = f"""
SELECT
    pol.po_number             AS "PO番号",
    pol.trandate              AS "発注日",
    pol.vendor_name           AS "仕入先",
    im.jan                    AS "JAN",
    im.display_name           AS "品名",
    pol.quantity              AS "発注数",
    pol.quantity_received     AS "既入荷",
    (pol.quantity - COALESCE(pol.quantity_received, 0)) AS "在途残",
    pol.rate                  AS "単価",
    pol.amount                AS "金額",
    pol.expected_receipt_date AS "入荷予定日",
    pol.status                AS "状態",
    pol.closed                AS "完了"
FROM nst.purchase_order_line pol
LEFT JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id
WHERE {" AND ".join(where)}
ORDER BY pol.trandate DESC, pol.po_number
LIMIT {LIMIT}
"""

try:
    rows = conn.execute(sql, params).fetchall()
except Exception as e:
    st.error(t("⚠️ nst.purchase_order_line 読取失败（PG 未接続 / schema 未部署？）") + f"\n\n{e}")
    st.stop()

df = pd.DataFrame([dict(r) for r in rows])
if df.empty:
    st.info(t("条件に一致する発注明細なし。"))
    st.stop()

n = len(df)
msg = t(f"✅ 命中 {n} 件")
if n >= LIMIT:
    msg += t(f"（已达上限 {LIMIT}，请缩小条件）")
st.success(msg)
st.dataframe(df, use_container_width=True, hide_index=True)

csv = df.to_csv(index=False).encode("utf-8-sig")
st.download_button(t("📥 CSV 下载"), data=csv, file_name="po_lines.csv", mime="text/csv")
