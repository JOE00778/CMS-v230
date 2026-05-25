"""模块 #30 供应商采购分析 · 発注書(PO)ベースの仕入先比价 / 月度总订货金额.

数据源:
  nst.po_supplier_monthly       仕入先 × 月 → 総発注金額（总订货金额）
  nst.po_item_supplier_monthly  SKU × 仕入先 × 月 → 数量/金额/加重平均単価（比价 / 历史原価）
  nst.po_export_vendor          输出供应商白名单（人工维护·过滤 CB/国内供货商）

tab1 采购分析（可按白名单过滤）/ tab2 白名单维护（候选勾选 + 手动加）。
PO 数据无字段可自动区分输出/CB → 用白名单人工区分（Boss 2026-05-25）。
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


tab1, tab2 = st.tabs([t("📊 采购分析"), t("🏢 输出供应商名单")])

# ============================================================
# tab1 · 采购分析（可按白名单过滤）
# ============================================================
with tab1:
    only_export = st.checkbox(
        t("仅输出供货商（白名单过滤）"), value=True,
        help=t("勾选则只统计「🏢 输出供应商名单」里确认的输出供货商，排除 CB/国内"),
    )
    wl = "JOIN nst.po_export_vendor ev ON ev.vendor_id = sm.vendor_id" if only_export else ""

    try:
        sm = _df(
            "SELECT sm.vendor_id, sm.vendor_name, sm.year_month, sm.po_count, "
            "sm.qty_ordered, sm.total_amount "
            f"FROM nst.po_supplier_monthly sm {wl}"
        )
    except Exception as e:
        st.error(t("⚠️ nst.po_supplier_monthly 读取失败（PG 未接続 / schema 未部署？）") + f"\n\n{e}")
        st.stop()

    if sm.empty:
        if only_export:
            st.info(t("白名单为空或无匹配。请到「🏢 输出供应商名单」tab 勾选确认输出供货商。"))
        else:
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

    # --- SKU 多供应商比价 / 历史原価 ---
    st.subheader(t("🔍 SKU 比价 / 历史原価"))
    jan_in = st.text_input(t("输入 JAN 查该商品在各供应商的历史采购单价"), "")
    if jan_in.strip():
        iwl = "JOIN nst.po_export_vendor ev ON ev.vendor_id = q.vendor_id" if only_export else ""
        isupp = _df(
            "SELECT q.year_month, q.vendor_name, q.qty_ordered, q.amount, q.avg_unit_price, q.display_name "
            f"FROM nst.po_item_supplier_monthly q {iwl} "
            "WHERE q.jan = %(jan)s ORDER BY q.year_month DESC, q.vendor_name",
            {"jan": jan_in.strip()},
        )
        if isupp.empty:
            st.info(t("该 JAN 无 PO 记录（或不在白名单供货商）。"))
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

# ============================================================
# tab2 · 输出供应商白名单维护
# ============================================================
with tab2:
    st.caption(t(
        "PO 采购数据无字段可自动区分 输出/CB（部门·子公司·仓库都试过不行）→ 人工白名单。"
        "下表列出「采购过输出主档商品(4/9)」的供货商，勾选「是否输出」剔除 CB/国内，保存后只有勾选的进白名单。"
    ))

    try:
        cand = _df(
            "SELECT DISTINCT pol.vendor_id, pol.vendor_name, "
            "       (ev.vendor_id IS NOT NULL) AS is_export "
            "FROM nst.purchase_order_line pol "
            "JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id "
            "LEFT JOIN nst.po_export_vendor ev ON ev.vendor_id = pol.vendor_id "
            "WHERE pol.vendor_id IS NOT NULL "
            "ORDER BY pol.vendor_name"
        )
    except Exception as e:
        st.error(t("⚠️ 候选供货商读取失败（schema 未部署？）") + f"\n\n{e}")
        st.stop()

    if cand.empty:
        st.info(t("暂无候选供货商（PO 数据空？）"))
    else:
        cand["is_export"] = cand["is_export"].astype(bool)
        n_wl = int(cand["is_export"].sum())
        st.write(t("候选 {n} 家 · 当前白名单 {w} 家").format(n=len(cand), w=n_wl))

        edited = st.data_editor(
            cand, hide_index=True, use_container_width=True, height=520,
            column_config={
                "vendor_id": st.column_config.TextColumn(t("供货商ID"), disabled=True),
                "vendor_name": st.column_config.TextColumn(t("仕入先"), disabled=True),
                "is_export": st.column_config.CheckboxColumn(t("是否输出"), default=False),
            },
            key="wl_editor",
        )

        if st.button(t("💾 保存白名单"), type="primary"):
            kept = 0
            for _, r in edited.iterrows():
                vid = str(r["vendor_id"])
                if bool(r["is_export"]):
                    conn.execute(
                        "INSERT INTO nst.po_export_vendor (vendor_id, vendor_name, source, updated_at) "
                        "VALUES (%(vid)s, %(vn)s, 'sku_confirm', NOW()) "
                        "ON CONFLICT (vendor_id) DO UPDATE SET "
                        "vendor_name = EXCLUDED.vendor_name, updated_at = NOW()",
                        {"vid": vid, "vn": r["vendor_name"]},
                    )
                    kept += 1
                else:
                    conn.execute(
                        "DELETE FROM nst.po_export_vendor WHERE vendor_id = %(vid)s", {"vid": vid})
            conn.commit()
            st.success(t("✅ 已保存 · 白名单 {n} 家").format(n=kept))
            st.rerun()

    # 手动加候选外的新供货商
    with st.expander(t("➕ 手动加新供货商（候选列表里没有的）")):
        c1, c2 = st.columns(2)
        new_id = c1.text_input(t("供货商ID（NetSuite vendor internal id）"), key="new_vid")
        new_name = c2.text_input(t("仕入先名"), key="new_vname")
        if st.button(t("加入白名单"), key="add_manual"):
            if new_id.strip():
                conn.execute(
                    "INSERT INTO nst.po_export_vendor (vendor_id, vendor_name, source, updated_at) "
                    "VALUES (%(vid)s, %(vn)s, 'manual', NOW()) "
                    "ON CONFLICT (vendor_id) DO UPDATE SET "
                    "vendor_name = EXCLUDED.vendor_name, updated_at = NOW()",
                    {"vid": new_id.strip(), "vn": new_name.strip() or None},
                )
                conn.commit()
                st.success(t("✅ 已加入：{n}").format(n=new_name or new_id))
                st.rerun()
            else:
                st.warning(t("请填供货商ID"))
