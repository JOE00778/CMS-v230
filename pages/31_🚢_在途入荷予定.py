"""模块 #31 在途 / 入荷予定 · 未完了の発注書(PO)残.

数据源: nst.po_open_lines（未close × 入荷残>0 · 订货引擎の精確在途と同一ビュー）
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("在途入荷予定"), page_icon="🚢", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🚢 在途 / 入荷予定（未完了 PO）"))
st.caption(t("数据源 NetSuite 発注書 · 未 close 且入荷残>0 的明细（= 订货引擎的精確在途）"))

only_export = st.checkbox(
    t("仅输出供货商（白名单过滤）"), value=True,
    help=t("勾选则只看「📊 供应商采购分析 → 🏢 输出供应商名单」里确认的输出供货商"),
)


def _df(sql: str, params=None) -> pd.DataFrame:
    rs = conn.execute(sql, params or {}).fetchall()
    return pd.DataFrame([dict(r) for r in rs])


wl = "JOIN nst.po_export_vendor ev ON ev.vendor_id = ol.vendor_id" if only_export else ""
try:
    df = _df(
        "SELECT ol.item_internal_id, ol.jan, ol.po_number, ol.vendor_name, ol.location, "
        "ol.qty_outstanding, ol.expected_receipt_date, ol.status "
        f"FROM nst.po_open_lines ol {wl}"
    )
except Exception as e:
    st.error(t("⚠️ nst.po_open_lines 读取失败（PG 未接続 / schema 未部署？）") + f"\n\n{e}")
    st.stop()

if df.empty:
    if only_export:
        st.info(t("白名单为空或无匹配在途。请到「📊 供应商采购分析 → 🏢 输出供应商名单」维护，或取消勾选看全部。"))
    else:
        st.info(t("当前无在途 PO（全部已入荷 / close）。"))
    st.stop()

df["qty_outstanding"] = pd.to_numeric(df["qty_outstanding"], errors="coerce").fillna(0.0)
df["expected_receipt_date"] = pd.to_datetime(df["expected_receipt_date"], errors="coerce").dt.date

# --- KPI ---
k1, k2, k3, k4 = st.columns(4)
k1.metric(t("在途明细行"), f"{len(df):,}")
k2.metric(t("在途总量"), f"{df['qty_outstanding'].sum():,.0f}")
k3.metric(t("涉及仕入先"), f"{df['vendor_name'].nunique():,}")
k4.metric(t("涉及 PO 单"), f"{df['po_number'].nunique():,}")

st.divider()

# --- 仕入先别 在途量 ---
st.subheader(t("🏢 仕入先别 在途量"))
by_v = (df.groupby("vendor_name")
        .agg(qty=("qty_outstanding", "sum"), lines=("po_number", "count"))
        .reset_index().sort_values("qty", ascending=False))
by_v = by_v.rename(columns={"vendor_name": t("仕入先"), "qty": t("在途量"), "lines": t("明细行")})
st.dataframe(by_v, hide_index=True, use_container_width=True, height=300)

st.divider()

# --- 在途明细（可筛）---
st.subheader(t("📋 在途明细"))
c1, c2 = st.columns(2)
with c1:
    vfil = st.text_input(t("🔍 仕入先（部分匹配）"), "")
with c2:
    jfil = st.text_input(t("🔍 JAN（部分匹配）"), "")

view = df.copy()
if vfil:
    view = view[view["vendor_name"].astype(str).str.contains(vfil, na=False)]
if jfil:
    view = view[view["jan"].astype(str).str.contains(jfil, na=False)]

disp = view[["po_number", "vendor_name", "jan", "qty_outstanding",
             "expected_receipt_date", "status"]].copy()
disp = disp.rename(columns={"po_number": t("PO番号"), "vendor_name": t("仕入先"), "jan": t("JAN"),
                            "qty_outstanding": t("在途残"), "expected_receipt_date": t("入荷予定日"),
                            "status": t("状態")})
disp = disp.sort_values(t("入荷予定日"), na_position="last")
st.dataframe(disp, hide_index=True, use_container_width=True, height=480)
st.download_button(t("📥 CSV 下载"), disp.to_csv(index=False).encode("utf-8-sig"),
                   file_name="po_open_lines.csv", mime="text/csv")
