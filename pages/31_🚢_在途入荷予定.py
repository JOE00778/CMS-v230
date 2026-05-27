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


wl = "JOIN nst.po_export_vendor ev ON ev.vendor_id = pol.vendor_id" if only_export else ""
try:
    # 直接从 purchase_order_line 取（视图 po_open_lines 不含 rate/amount）
    df = _df(
        "SELECT pol.item_internal_id, im.jan, pol.po_number, pol.vendor_name, pol.location, "
        "(pol.quantity - COALESCE(pol.quantity_received, 0)) AS qty_outstanding, "
        "pol.trandate, pol.expected_receipt_date, pol.status, pol.vendor_id, "
        "COALESCE(ev2.is_prepay, FALSE) AS is_prepay, "
        "((pol.quantity - COALESCE(pol.quantity_received, 0)) * pol.rate) AS amt_outstanding "
        "FROM nst.purchase_order_line pol "
        "LEFT JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id "
        "LEFT JOIN nst.po_export_vendor ev2 ON ev2.vendor_id = pol.vendor_id "
        f"{wl} "
        "WHERE COALESCE(pol.closed, FALSE) = FALSE "
        "  AND (pol.quantity - COALESCE(pol.quantity_received, 0)) > 0"
    )
except Exception as e:
    st.error(t("⚠️ nst.purchase_order_line 读取失败（PG 未接続 / schema 未部署？）") + f"\n\n{e}")
    st.stop()

if df.empty:
    if only_export:
        st.info(t("白名单为空或无匹配在途。请到「📊 供应商采购分析 → 🏢 输出供应商名单」维护，或取消勾选看全部。"))
    else:
        st.info(t("当前无在途 PO（全部已入荷 / close）。"))
    st.stop()

df["qty_outstanding"] = pd.to_numeric(df["qty_outstanding"], errors="coerce").fillna(0.0)
df["amt_outstanding"] = pd.to_numeric(df["amt_outstanding"], errors="coerce").fillna(0.0)
df["is_prepay"] = df["is_prepay"].astype(bool)
df["trandate"] = pd.to_datetime(df["trandate"], errors="coerce").dt.date
df["expected_receipt_date"] = pd.to_datetime(df["expected_receipt_date"], errors="coerce").dt.date

# --- KPI ---
k1, k2, k3, k4 = st.columns(4)
k1.metric(t("在途明细行"), f"{len(df):,}")
k2.metric(t("在途总量"), f"{df['qty_outstanding'].sum():,.0f}")
k3.metric(t("涉及仕入先"), f"{df['vendor_name'].nunique():,}")
k4.metric(t("涉及 PO 单"), f"{df['po_number'].nunique():,}")

# 挂账 / 预付款 在途金额拆分（vendor 级 is_prepay·勾=現金払い，未勾=掛け払い）
tot_amt = float(df["amt_outstanding"].sum())
amt_pp = float(df.loc[df["is_prepay"], "amt_outstanding"].sum())
amt_cr = float(df.loc[~df["is_prepay"], "amt_outstanding"].sum())
pct_pp = (amt_pp / tot_amt * 100) if tot_amt else 0.0
pct_cr = (amt_cr / tot_amt * 100) if tot_amt else 0.0

p1, p2 = st.columns(2)
p1.metric(t("挂账（掛け払い）在途金额 (¥)"), f"¥{amt_cr:,.0f}",
          delta=f"{pct_cr:.0f}% {t('占比')}", delta_color="off")
p2.metric(t("预付款（現金払い）在途金额 (¥)"), f"¥{amt_pp:,.0f}",
          delta=f"{pct_pp:.0f}% {t('占比')}", delta_color="off")

st.divider()

# --- 仕入先别 在途量 ---
st.subheader(t("🏢 仕入先别 在途量"))
by_v = (df.groupby(["vendor_name", "is_prepay"], as_index=False)
        .agg(qty=("qty_outstanding", "sum"),
             amt=("amt_outstanding", "sum"),
             lines=("po_number", "count"))
        .sort_values("amt", ascending=False))
by_v["prepay_mark"] = by_v["is_prepay"].map(lambda b: "✓" if bool(b) else "")
by_v = by_v[["vendor_name", "prepay_mark", "qty", "amt", "lines"]].rename(columns={
    "vendor_name": t("仕入先"), "prepay_mark": t("预付款"),
    "qty": t("在途量"), "amt": t("在途金额(¥)"), "lines": t("明细行"),
})
st.dataframe(by_v, hide_index=True, use_container_width=True, height=300,
             column_config={t("在途金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})

st.divider()

# --- 在途明细（可筛）---
st.subheader(t("📋 在途明细"))
c1, c2, c3 = st.columns(3)
with c1:
    vfil = st.text_input(t("🔍 仕入先（部分匹配）"), "")
with c2:
    jfil = st.text_input(t("🔍 JAN（部分匹配）"), "")
with c3:
    pay_filter = st.selectbox(t("支付方式"), [t("全部"), t("挂账（掛け払い）"),
                                              t("预付款（現金払い）")])

view = df.copy()
if vfil:
    view = view[view["vendor_name"].astype(str).str.contains(vfil, na=False)]
if jfil:
    view = view[view["jan"].astype(str).str.contains(jfil, na=False)]
if pay_filter == t("挂账（掛け払い）"):
    view = view[~view["is_prepay"]]
elif pay_filter == t("预付款（現金払い）"):
    view = view[view["is_prepay"]]

view["prepay_mark"] = view["is_prepay"].map(lambda b: "✓" if bool(b) else "")
disp = view[["trandate", "po_number", "vendor_name", "prepay_mark", "jan",
             "qty_outstanding", "amt_outstanding", "expected_receipt_date", "status"]].copy()
disp = disp.rename(columns={
    "trandate": t("発注日"), "po_number": t("PO番号"), "vendor_name": t("仕入先"),
    "prepay_mark": t("预付款"), "jan": t("JAN"), "qty_outstanding": t("在途残"),
    "amt_outstanding": t("在途金额(¥)"), "expected_receipt_date": t("入荷予定日"),
    "status": t("状態"),
})
disp = disp.sort_values(t("発注日"), na_position="last")
st.dataframe(disp, hide_index=True, use_container_width=True, height=480,
             column_config={t("在途金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})
st.download_button(t("📥 CSV 下载"), disp.to_csv(index=False).encode("utf-8-sig"),
                   file_name="po_open_lines.csv", mime="text/csv")
