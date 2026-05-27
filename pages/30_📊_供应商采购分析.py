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


# 预付款列幂等迁移（schema 文件: database/.../009_add_prepay_vendor.sql）
try:
    conn.execute("ALTER TABLE nst.po_export_vendor "
                 "ADD COLUMN IF NOT EXISTS is_prepay BOOLEAN NOT NULL DEFAULT FALSE")
    conn.commit()
except Exception:
    conn.rollback()


tab1, tab2 = st.tabs([t("📊 采购分析"), t("🏢 输出供应商名单")])

# ============================================================
# tab1 · 采购分析（可按白名单过滤）
# ============================================================
def _render_analysis():
    only_export = st.checkbox(
        t("仅输出供货商（白名单过滤）"), value=True,
        help=t("勾选则只统计「🏢 输出供应商名单」里确认的输出供货商，排除 CB/国内"),
    )
    wl = "JOIN nst.po_export_vendor ev ON ev.vendor_id = sm.vendor_id" if only_export else ""

    try:
        sm = _df(
            "SELECT sm.vendor_id, sm.vendor_name, sm.year_month, sm.po_count, "
            "sm.qty_ordered, sm.total_amount, "
            "COALESCE(ev2.is_prepay, FALSE) AS is_prepay "
            f"FROM nst.po_supplier_monthly sm {wl} "
            "LEFT JOIN nst.po_export_vendor ev2 ON ev2.vendor_id = sm.vendor_id"
        )
    except Exception as e:
        st.error(t("⚠️ nst.po_supplier_monthly 读取失败（PG 未接続 / schema 未部署？）") + f"\n\n{e}")
        return

    if sm.empty:
        if only_export:
            st.info(t("白名单为空或无匹配。请到「🏢 输出供应商名单」tab 勾选确认输出供货商。"))
        else:
            st.info(t("暂无 PO 数据。请先在元川跑 daily_pull --domains po。"))
        return

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

    # 按支付方式拆分（vendor 级 is_prepay 标识·勾选=预付款/現金払い，未勾=挂账/掛け払い）
    pp_mask_cur = cur["is_prepay"].astype(bool)
    tot_pp_cur = float(cur.loc[pp_mask_cur, "total_amount"].sum())
    tot_cr_cur = float(cur.loc[~pp_mask_cur, "total_amount"].sum())
    if not prev.empty:
        pp_mask_prev = prev["is_prepay"].astype(bool)
        tot_pp_prev = float(prev.loc[pp_mask_prev, "total_amount"].sum())
        tot_cr_prev = float(prev.loc[~pp_mask_prev, "total_amount"].sum())
    else:
        tot_pp_prev = tot_cr_prev = 0.0
    mom_pp = (tot_pp_cur / tot_pp_prev - 1) * 100 if tot_pp_prev else None
    mom_cr = (tot_cr_cur / tot_cr_prev - 1) * 100 if tot_cr_prev else None
    pct_pp = (tot_pp_cur / tot_cur * 100) if tot_cur else 0.0
    pct_cr = (tot_cr_cur / tot_cur * 100) if tot_cur else 0.0

    k1, k2, k3 = st.columns(3)
    k1.metric(t("总订货金额 合计 (¥)"), f"¥{tot_cur:,.0f}",
              delta=(f"{mom:+.1f}% {t('环比上月')}" if mom is not None else None),
              delta_color="off")  # 总订货金额升降好坏取决于备货策略，不强行着色
    k2.metric(t("PO 件数"), f"{int(cur['po_count'].sum()):,}")
    k3.metric(t("仕入先 数"), f"{cur['vendor_id'].nunique():,}")

    p1, p2 = st.columns(2)
    p1.metric(t("挂账（掛け払い）金额 (¥)"), f"¥{tot_cr_cur:,.0f}",
              delta=(f"{mom_cr:+.1f}% {t('环比上月')} · {pct_cr:.0f}% {t('占比')}"
                     if mom_cr is not None
                     else f"{pct_cr:.0f}% {t('占比')}"),
              delta_color="off")
    p2.metric(t("预付款（現金払い）金额 (¥)"), f"¥{tot_pp_cur:,.0f}",
              delta=(f"{mom_pp:+.1f}% {t('环比上月')} · {pct_pp:.0f}% {t('占比')}"
                     if mom_pp is not None
                     else f"{pct_pp:.0f}% {t('占比')}"),
              delta_color="off")

    st.divider()

    # --- 月度总订货金额合计（全体）---
    st.subheader(t("📈 月度总订货金额推移（全体合计）"))
    total_by_month = (sm.groupby("year_month", as_index=False)["total_amount"].sum()
                      .sort_values("year_month"))
    if not total_by_month.empty:
        bar = alt.Chart(total_by_month).mark_bar().encode(
            x=alt.X("year_month:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("total_amount:Q", title=t("总订货金额 (日元)"),
                    axis=alt.Axis(format=",.0f", grid=True, labels=True, ticks=True)),
            tooltip=[alt.Tooltip("year_month:N", title=t("月份")),
                     alt.Tooltip("total_amount:Q", title=t("总订货金额"), format=",.0f")],
        )
        st.altair_chart(bar.properties(height=260), use_container_width=True)

    st.divider()

    # --- 月度总订货金额趋势（Top 5 供应商）---
    st.subheader(t("📈 仕入先 月度总订货金额推移（Top 5）"))
    top_vendors = (sm.groupby("vendor_name")["total_amount"].sum()
                   .sort_values(ascending=False).head(5).index.tolist())
    trend = sm[sm["vendor_name"].isin(top_vendors)]
    trend_piv = trend.groupby(["year_month", "vendor_name"])["total_amount"].sum().reset_index()

    if not trend_piv.empty:
        line = alt.Chart(trend_piv).mark_line(point=True).encode(
            x=alt.X("year_month:N", sort=None, title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("total_amount:Q", title=t("总订货金额 (日元)"),
                    axis=alt.Axis(format=",.0f", grid=True, labels=True, ticks=True)),
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
    rank = (cur.groupby(["vendor_name", "is_prepay"], as_index=False)
            .agg(amount=("total_amount", "sum"), po=("po_count", "sum"), qty=("qty_ordered", "sum"))
            .sort_values("amount", ascending=False))
    rank["prepay_mark"] = rank["is_prepay"].map(lambda b: "✓" if bool(b) else "")
    rank = rank[["vendor_name", "prepay_mark", "amount", "po", "qty"]].rename(columns={
        "vendor_name": t("仕入先"), "prepay_mark": t("预付款"),
        "amount": t("总订货金额(¥)"), "po": t("PO件数"), "qty": t("発注数"),
    })
    st.dataframe(rank, hide_index=True, use_container_width=True, height=400,
                 column_config={t("总订货金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})
    st.download_button(t("📥 CSV 下载"), rank.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"po_supplier_{sel_month}.csv", mime="text/csv")

    st.divider()

    # --- 预付款 PO 明细（当月） ---
    st.subheader(t("💰 预付款 PO 明细（{ym}）").format(ym=sel_month))
    st.caption(t("仅列出「输出供应商名单」里勾了「预付款」的供货商 PO·按 PO 单聚合·用于现金流管理"))
    try:
        prepay_po = _df(
            "SELECT pol.po_number, pol.vendor_name, MIN(pol.trandate) AS trandate, "
            "       SUM(pol.amount) AS amount, SUM(pol.quantity) AS qty, COUNT(*) AS line_cnt "
            "FROM nst.purchase_order_line pol "
            "JOIN nst.po_export_vendor ev ON ev.vendor_id = pol.vendor_id "
            "WHERE ev.is_prepay = TRUE "
            "  AND to_char(pol.trandate, 'YYYY-MM') = %(ym)s "
            "GROUP BY pol.po_number, pol.vendor_name "
            "ORDER BY amount DESC NULLS LAST",
            {"ym": sel_month},
        )
    except Exception as e:
        st.error(t("⚠️ 预付款 PO 读取失败") + f"\n\n{e}")
        prepay_po = pd.DataFrame()

    if prepay_po.empty:
        st.info(t("当月无预付款 PO（白名单里没勾「预付款」或当月没单）"))
    else:
        prepay_po["amount"] = pd.to_numeric(prepay_po["amount"], errors="coerce").fillna(0.0)
        prepay_po["qty"] = pd.to_numeric(prepay_po["qty"], errors="coerce").fillna(0.0)
        tot_pp = float(prepay_po["amount"].sum())
        ratio = (tot_pp / tot_cur * 100) if tot_cur else 0.0
        p1, p2, p3 = st.columns(3)
        p1.metric(t("预付款 PO 件数"), f"{len(prepay_po):,}")
        p2.metric(t("预付款金额合计 (¥)"), f"¥{tot_pp:,.0f}")
        p3.metric(t("占当月总订货比"), f"{ratio:.1f}%")

        disp = prepay_po[["trandate", "po_number", "vendor_name",
                          "amount", "qty", "line_cnt"]].rename(columns={
            "trandate": t("発注日"), "po_number": t("PO番号"), "vendor_name": t("仕入先"),
            "amount": t("金额(¥)"), "qty": t("数量"), "line_cnt": t("行数"),
        })
        disp = disp.sort_values(t("発注日"), na_position="last")
        st.dataframe(disp, hide_index=True, use_container_width=True, height=320,
                     column_config={t("金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})
        st.download_button(t("📥 预付款 PO CSV 下载"),
                           disp.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"po_prepay_{sel_month}.csv", mime="text/csv",
                           key="prepay_csv")

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
def _render_whitelist():
    st.caption(t(
        "PO 采购数据无字段可自动区分 输出/CB（部门·子公司·仓库都试过不行）→ 人工白名单。"
        "下表列出「采购过输出主档商品(4/9)」的供货商，勾选「是否输出」剔除 CB/国内，保存后只有勾选的进白名单。"
    ))

    try:
        cand = _df(
            "SELECT DISTINCT pol.vendor_id, pol.vendor_name, "
            "       (ev.vendor_id IS NOT NULL) AS is_export, "
            "       COALESCE(ev.is_prepay, FALSE) AS is_prepay "
            "FROM nst.purchase_order_line pol "
            "JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id "
            "LEFT JOIN nst.po_export_vendor ev ON ev.vendor_id = pol.vendor_id "
            "WHERE pol.vendor_id IS NOT NULL "
            "ORDER BY pol.vendor_name"
        )
    except Exception as e:
        st.error(t("⚠️ 候选供货商读取失败（schema 未部署？）") + f"\n\n{e}")
        return

    if cand.empty:
        st.info(t("暂无候选供货商（PO 数据空？）"))
    else:
        cand["is_export"] = cand["is_export"].astype(bool)
        cand["is_prepay"] = cand["is_prepay"].astype(bool)
        n_wl = int(cand["is_export"].sum())
        n_pp = int((cand["is_export"] & cand["is_prepay"]).sum())
        st.write(t("候选 {n} 家 · 当前白名单 {w} 家 · 其中预付款 {p} 家")
                 .format(n=len(cand), w=n_wl, p=n_pp))

        edited = st.data_editor(
            cand, hide_index=True, use_container_width=True, height=520,
            column_config={
                "vendor_id": st.column_config.TextColumn(t("供货商ID"), disabled=True),
                "vendor_name": st.column_config.TextColumn(t("仕入先"), disabled=True),
                "is_export": st.column_config.CheckboxColumn(t("是否输出"), default=False),
                "is_prepay": st.column_config.CheckboxColumn(
                    t("预付款"), default=False,
                    help=t("勾选则该供货商所有 PO 视为预付款（采购分析页单独列出）"),
                ),
            },
            key="wl_editor",
        )

        if st.button(t("💾 保存白名单"), type="primary"):
            kept = 0
            for _, r in edited.iterrows():
                vid = str(r["vendor_id"])
                if bool(r["is_export"]):
                    conn.execute(
                        "INSERT INTO nst.po_export_vendor "
                        "(vendor_id, vendor_name, is_prepay, source, updated_at) "
                        "VALUES (%(vid)s, %(vn)s, %(pp)s, 'sku_confirm', NOW()) "
                        "ON CONFLICT (vendor_id) DO UPDATE SET "
                        "vendor_name = EXCLUDED.vendor_name, "
                        "is_prepay = EXCLUDED.is_prepay, "
                        "updated_at = NOW()",
                        {"vid": vid, "vn": r["vendor_name"], "pp": bool(r["is_prepay"])},
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
        c1, c2, c3 = st.columns([2, 2, 1])
        new_id = c1.text_input(t("供货商ID（NetSuite vendor internal id）"), key="new_vid")
        new_name = c2.text_input(t("仕入先名"), key="new_vname")
        new_pp = c3.checkbox(t("预付款"), value=False, key="new_pp")
        if st.button(t("加入白名单"), key="add_manual"):
            if new_id.strip():
                conn.execute(
                    "INSERT INTO nst.po_export_vendor "
                    "(vendor_id, vendor_name, is_prepay, source, updated_at) "
                    "VALUES (%(vid)s, %(vn)s, %(pp)s, 'manual', NOW()) "
                    "ON CONFLICT (vendor_id) DO UPDATE SET "
                    "vendor_name = EXCLUDED.vendor_name, "
                    "is_prepay = EXCLUDED.is_prepay, "
                    "updated_at = NOW()",
                    {"vid": new_id.strip(), "vn": new_name.strip() or None, "pp": bool(new_pp)},
                )
                conn.commit()
                st.success(t("✅ 已加入：{n}").format(n=new_name or new_id))
                st.rerun()
            else:
                st.warning(t("请填供货商ID"))


with tab1:
    _render_analysis()
with tab2:
    _render_whitelist()
