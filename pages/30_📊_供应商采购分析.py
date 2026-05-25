"""模块 #30 供应商采购分析 · 発注書(PO)ベースの仕入先比价 / 月度总订货金额.

数据源:
  nst.po_supplier_monthly       仕入先 × 月 → 総発注金額（总订货金额）
  nst.po_item_supplier_monthly  SKU × 仕入先 × 月 → 数量/金额/加重平均単価（比价 / 历史原価）
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("供应商采购分析"), page_icon="📊", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📊 供应商采购分析（発注書ベース）"))
st.caption(t("数据源 NetSuite 発注書 · 仕入先月度总订货金额 + SKU 多供应商比价 / 历史原価"))


def _df(sql: str, params=None) -> pd.DataFrame:
    rs = conn.execute(sql, params or {}).fetchall()
    return pd.DataFrame([dict(r) for r in rs])


# ============================================================
# 仕入先 × 月（总订货金额）
# ============================================================
try:
    sm = _df(
        "SELECT vendor_id, vendor_name, year_month, po_count, qty_ordered, total_amount "
        "FROM nst.po_supplier_monthly"
    )
except Exception as e:
    st.error(t("⚠️ nst.po_supplier_monthly 读取失败（PG 未接続 / schema 未部署？）") + f"\n\n{e}")
    st.stop()

if sm.empty:
    st.info(t("暂无 PO 数据。请先在元川跑 daily_pull --domains po。"))
    st.stop()

sm["total_amount"] = pd.to_numeric(sm["total_amount"], errors="coerce").fillna(0.0)
sm["qty_ordered"] = pd.to_numeric(sm["qty_ordered"], errors="coerce").fillna(0.0)
sm["po_count"] = pd.to_numeric(sm["po_count"], errors="coerce").fillna(0).astype(int)

months = sorted(sm["year_month"].dropna().unique().tolist(), reverse=True)
sel_month = st.selectbox(t("月份"), months, index=0)

# --- KPI（当月）---
cur = sm[sm["year_month"] == sel_month]
prev_idx = months.index(sel_month) + 1
prev_month = months[prev_idx] if prev_idx < len(months) else None
prev = sm[sm["year_month"] == prev_month] if prev_month else pd.DataFrame()

tot_cur = float(cur["total_amount"].sum())
tot_prev = float(prev["total_amount"].sum()) if not prev.empty else 0.0
mom = (tot_cur / tot_prev - 1) * 100 if tot_prev else None

k1, k2, k3 = st.columns(3)
k1.metric(t("总订货金额 合计 (¥)"), f"¥{tot_cur:,.0f}",
          delta=(f"{mom:+.1f}% {t('环比上月')}" if mom is not None else None),
          delta_color="off")  # 总订货金额升降好坏取决于备货策略，不强行着色
k2.metric(t("PO 件数"), f"{int(cur['po_count'].sum()):,}")
k3.metric(t("仕入先 数"), f"{cur['vendor_id'].nunique():,}")

st.divider()

# --- 月度总订货金额趋势（Top 8 供应商）---
st.subheader(t("📈 仕入先 月度总订货金额推移（Top 8）"))
top_vendors = (sm.groupby("vendor_name")["total_amount"].sum()
               .sort_values(ascending=False).head(8).index.tolist())
trend = sm[sm["vendor_name"].isin(top_vendors)]
trend_piv = trend.groupby(["year_month", "vendor_name"])["total_amount"].sum().reset_index()

if not trend_piv.empty:
    line = alt.Chart(trend_piv).mark_line(point=True).encode(
        x=alt.X("year_month:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("total_amount:Q", title=t("总订货金额 (日元)")),
        color=alt.Color("vendor_name:N", title=t("仕入先")),
        tooltip=[alt.Tooltip("year_month:N", title=t("月份")),
                 alt.Tooltip("vendor_name:N", title=t("仕入先")),
                 alt.Tooltip("total_amount:Q", title=t("总订货金额"), format=",.0f")],
    )
    st.altair_chart(line.properties(height=320).configure_legend(orient="top"),
                    use_container_width=True)

st.divider()

# --- 当月供应商排行 ---
st.subheader(t("🏆 仕入先总订货金额排行（{ym}）").format(ym=sel_month))
rank = (cur.groupby("vendor_name")
        .agg(amount=("total_amount", "sum"), po=("po_count", "sum"), qty=("qty_ordered", "sum"))
        .reset_index().sort_values("amount", ascending=False))
rank = rank.rename(columns={"vendor_name": t("仕入先"), "amount": t("总订货金额(¥)"),
                            "po": t("PO件数"), "qty": t("発注数")})
st.dataframe(rank, hide_index=True, use_container_width=True, height=400,
             column_config={t("总订货金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})
st.download_button(t("📥 CSV 下载"), rank.to_csv(index=False).encode("utf-8-sig"),
                   file_name=f"po_supplier_{sel_month}.csv", mime="text/csv")

st.divider()

# ============================================================
# SKU 多供应商比价 / 历史原価
# ============================================================
st.subheader(t("🔍 SKU 比价 / 历史原価"))
jan_in = st.text_input(t("输入 JAN 查该商品在各供应商的历史采购单价"), "")
if jan_in.strip():
    isupp = _df(
        "SELECT year_month, vendor_name, qty_ordered, amount, avg_unit_price, display_name "
        "FROM nst.po_item_supplier_monthly WHERE jan = %(jan)s "
        "ORDER BY year_month DESC, vendor_name",
        {"jan": jan_in.strip()},
    )
    if isupp.empty:
        st.info(t("该 JAN 无 PO 记录。"))
    else:
        isupp["avg_unit_price"] = pd.to_numeric(isupp["avg_unit_price"], errors="coerce")
        isupp["qty_ordered"] = pd.to_numeric(isupp["qty_ordered"], errors="coerce").fillna(0.0)
        isupp["amount"] = pd.to_numeric(isupp["amount"], errors="coerce").fillna(0.0)
        nm = isupp["display_name"].dropna().iloc[0] if isupp["display_name"].notna().any() else ""
        st.caption(t("品名：") + str(nm))

        chart = alt.Chart(isupp).mark_line(point=True).encode(
            x=alt.X("year_month:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("avg_unit_price:Q", title=t("加重平均单价 (日元)")),
            color=alt.Color("vendor_name:N", title=t("仕入先")),
            tooltip=[alt.Tooltip("year_month:N", title=t("月份")),
                     alt.Tooltip("vendor_name:N", title=t("仕入先")),
                     alt.Tooltip("avg_unit_price:Q", title=t("単価"), format=",.2f"),
                     alt.Tooltip("qty_ordered:Q", title=t("数量"), format=",.0f")],
        )
        st.altair_chart(chart.properties(height=300).configure_legend(orient="top"),
                        use_container_width=True)

        disp = isupp[["year_month", "vendor_name", "qty_ordered", "avg_unit_price", "amount"]].copy()
        disp = disp.rename(columns={"year_month": t("月份"), "vendor_name": t("仕入先"),
                                    "qty_ordered": t("発注数"), "avg_unit_price": t("加重平均単価"),
                                    "amount": t("金额")})
        st.dataframe(disp, hide_index=True, use_container_width=True,
                     column_config={
                         t("加重平均単価"): st.column_config.NumberColumn(format="¥%,.2f"),
                         t("金额"): st.column_config.NumberColumn(format="¥%,.0f")})
