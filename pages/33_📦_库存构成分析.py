"""模块 #33 库存构成分析 · JD + 弁天 全仓库按用途分类.

数据源:
  - JD-物流-千葉: nst.inventory_snapshot（不分 bin·全部归「通常输出」）
  - 弁天倉庫:     nst.inventory_bin_snapshot（按 bin 分类）

弁天 bin 用途分类（shared/bin_categories.py·Boss 2026-05-27）:
  返品 (HENPIN-EX) > 不良品 (FF-3) > 输出中国 (1-0105A/1-0106A/1-0107A/yusyutu2F) > 通常输出
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.bin_categories import BIN_CB, BIN_DEFECT, BIN_RETURN, CATEGORIES
from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("库存构成分析"), page_icon="📦", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📦 库存构成分析（JD + 弁天）"))
st.caption(t("JD 不分 bin 全部归通常输出 · 弁天按棚番号分类（输出 / 输出中国 / 返品 / 不良品）"))


def _df(sql: str, params=None) -> pd.DataFrame:
    rs = conn.execute(sql, params or {}).fetchall()
    return pd.DataFrame([dict(r) for r in rs])


# 最新 snapshot_date
try:
    inv_date = _df("SELECT max(snapshot_date) AS d FROM nst.inventory_snapshot")["d"].iloc[0]
    bin_date_row = _df("SELECT max(snapshot_date) AS d FROM nst.inventory_bin_snapshot")
    bin_date = bin_date_row["d"].iloc[0] if not bin_date_row.empty else None
except Exception as e:
    st.error(t("⚠️ 库存表读取失败（schema 未部署？）") + f"\n\n{e}")
    st.stop()

if not inv_date:
    st.info(t("尚无库存快照数据。"))
    st.stop()

st.caption(t("JD 快照：{d1} · 弁天 bin 快照：{d2}").format(
    d1=str(inv_date), d2=str(bin_date) if bin_date else t("（暂无）")))

# 全仓库 × item × bin level → 加 category 列（item × cost_estimate → 金额）
# JD: bin_number=NULL → 类别='输出'（JD 不分 bin）
# 弁天: 按 bin 直接判定（同 item 多 bin 时按行各自归类）
_SQL = """
WITH inv_all AS (
    SELECT 'JD-物流-千葉' AS warehouse, item_internal_id, qty_on_hand, NULL::TEXT AS bin_number
    FROM nst.inventory_snapshot
    WHERE snapshot_date = %(d_jd)s AND warehouse = 'JD-物流-千葉'
    UNION ALL
    SELECT '弁天倉庫' AS warehouse, item_internal_id, qty_on_hand, bin_number
    FROM nst.inventory_bin_snapshot
    WHERE snapshot_date = %(d_bn)s
)
SELECT
    inv_all.warehouse,
    inv_all.bin_number,
    CASE
        WHEN inv_all.warehouse = 'JD-物流-千葉' THEN '输出'
        WHEN inv_all.bin_number = %(b_ret)s THEN '返品'
        WHEN inv_all.bin_number = %(b_def)s THEN '不良品'
        WHEN inv_all.bin_number = ANY(%(b_cb)s) THEN '输出中国'
        ELSE '输出'
    END AS category,
    inv_all.item_internal_id,
    im.item_code, im.jan, im.display_name, im.maker, im.item_rank,
    inv_all.qty_on_hand,
    (inv_all.qty_on_hand * COALESCE(im.cost_estimate, 0))::NUMERIC(16,2) AS amt
FROM inv_all
LEFT JOIN nst.item_master_raw im ON im.internal_id = inv_all.item_internal_id
"""

try:
    df = _df(_SQL, {
        "d_jd": inv_date, "d_bn": bin_date or inv_date,
        "b_ret": BIN_RETURN, "b_def": BIN_DEFECT, "b_cb": list(BIN_CB),
    })
except Exception as e:
    st.error(t("⚠️ 构成查询失败") + f"\n\n{e}")
    st.stop()

if df.empty:
    st.info(t("无库存数据"))
    st.stop()

df["qty_on_hand"] = pd.to_numeric(df["qty_on_hand"], errors="coerce").fillna(0)
df["amt"] = pd.to_numeric(df["amt"], errors="coerce").fillna(0)

# ============================================================
# 顶部 4 类别构成卡（全仓库合算）
# ============================================================
st.subheader(t("📊 构成 KPI（全仓库合算）"))
by_cat = df.groupby("category", as_index=False).agg(qty=("qty_on_hand", "sum"), amt=("amt", "sum"))
cat_map = {r["category"]: r for _, r in by_cat.iterrows()}
tot_amt = float(by_cat["amt"].sum())
tot_qty = float(by_cat["qty"].sum())

cc = st.columns(4)
_labels = {
    "输出": t("通常输出"),
    "输出中国": t("输出中国（CB）"),
    "返品": t("返品（HENPIN-EX）"),
    "不良品": t("不良品（FF-3）"),
}
for col, cat in zip(cc, CATEGORIES):
    r = cat_map.get(cat)
    amt = float(r["amt"]) if r is not None else 0.0
    qty = float(r["qty"]) if r is not None else 0.0
    pct = (amt / tot_amt * 100) if tot_amt else 0.0
    col.metric(f"{_labels[cat]} (¥)", f"¥{amt:,.0f}",
               delta=f"{pct:.0f}% {t('占比')} · {qty:,.0f} {t('数量')}",
               delta_color="off")

st.caption(t("库存合計 ¥{a:,.0f} · 总数量 {q:,.0f}").format(a=tot_amt, q=tot_qty))
st.divider()

# ============================================================
# 仓库 × 类别 交叉表
# ============================================================
st.subheader(t("🏬 仓库 × 类别 交叉表"))
wh_cat = df.groupby(["warehouse", "category"], as_index=False).agg(
    qty=("qty_on_hand", "sum"), amt=("amt", "sum"))
piv = wh_cat.pivot(index="warehouse", columns="category", values="amt").fillna(0)
for c in CATEGORIES:
    if c not in piv.columns:
        piv[c] = 0.0
piv = piv[list(CATEGORIES)]
piv["合計"] = piv.sum(axis=1)
piv.loc["合計"] = piv.sum(axis=0)
piv = piv.reset_index()
piv.columns = [t("仓库")] + [_labels.get(c, c) for c in CATEGORIES] + [t("合計")]
st.dataframe(
    piv, hide_index=True, use_container_width=True,
    column_config={
        c: st.column_config.NumberColumn(format="¥%,.0f")
        for c in piv.columns if c != t("仓库")
    },
)

st.divider()

# ============================================================
# tab 内 构成 KPI 渲染辅助（用途类别多选 · 联动 tab 数据）
# ============================================================
def _render_tab_kpi(d: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    sel = st.multiselect(
        t("用途类别（多选 · 留空 = 全部）"), list(CATEGORIES),
        default=[], format_func=lambda c: _labels.get(c, c),
        key=f"{key_prefix}_kpi_cat",
    )
    d_show = d[d["category"].isin(sel)].copy() if sel else d.copy()
    by_c = d_show.groupby("category", as_index=False).agg(
        qty=("qty_on_hand", "sum"), amt=("amt", "sum"))
    cmap = {r["category"]: r for _, r in by_c.iterrows()}
    tot = float(by_c["amt"].sum())

    cards = sel if sel else list(CATEGORIES)
    if cards:
        cols = st.columns(len(cards))
        for col, cat in zip(cols, cards):
            r = cmap.get(cat)
            amt_v = float(r["amt"]) if r is not None else 0.0
            qty_v = float(r["qty"]) if r is not None else 0.0
            pct = (amt_v / tot * 100) if tot else 0.0
            col.metric(f"{_labels[cat]} (¥)", f"¥{amt_v:,.0f}",
                       delta=f"{pct:.0f}% {t('占比')} · {qty_v:,.0f} {t('数量')}",
                       delta_color="off")
    return d_show


# ============================================================
# tab1 按棚番号查商品（弁天）
# tab2 按 SKU 查棚分布（JD + 弁天）
# ============================================================
tab1, tab2 = st.tabs([t("🔎 按棚番号查商品（弁天）"), t("🔍 按 SKU 查库存分布（全仓库）")])

bn_df = df[df["warehouse"] == "弁天倉庫"].copy()

with tab1:
    _bin_opts_t1 = sorted(bn_df["bin_number"].dropna().astype(str).unique().tolist())
    c1, c2 = st.columns([3, 2])
    sel_bins_t1 = c1.multiselect(
        t("棚番号（多选 · 留空 = 全部棚）"), _bin_opts_t1, default=[], key="bin_filter_ms",
    )
    show_zero = c2.checkbox(t("含 0 库存"), value=False)

    sub = bn_df.copy()
    if sel_bins_t1:
        sub = sub[sub["bin_number"].astype(str).isin(sel_bins_t1)]
    if not show_zero:
        sub = sub[sub["qty_on_hand"] > 0]

    # tab 内 构成 KPI（用途类别多选 · 联动）
    sub = _render_tab_kpi(sub, "t1")
    st.divider()

    if sub.empty:
        st.info(t("无匹配数据"))
    else:
        k1, k2, k3, k4 = st.columns(4)
        k1.metric(t("命中棚数"), f"{sub['bin_number'].nunique():,}")
        k2.metric(t("商品行数"), f"{len(sub):,}")
        k3.metric(t("在庫合計"), f"{sub['qty_on_hand'].sum():,.0f}")
        k4.metric(t("金额合計 (¥)"), f"¥{sub['amt'].sum():,.0f}")

        disp = sub[["bin_number", "category", "item_code", "jan", "display_name",
                    "maker", "item_rank", "qty_on_hand", "amt"]].rename(columns={
            "bin_number": t("棚番号"), "category": t("用途"),
            "item_code": t("商品コード"), "jan": t("JAN"),
            "display_name": t("商品名"), "maker": t("メーカー"),
            "item_rank": t("等级"), "qty_on_hand": t("手持"),
            "amt": t("金额(¥)"),
        }).sort_values([t("棚番号"), t("商品コード")])
        st.dataframe(disp, hide_index=True, use_container_width=True, height=540,
                     column_config={t("金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})
        st.download_button(t("📥 CSV 下载"),
                           disp.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"bin_query_{bin_date}.csv", mime="text/csv")

with tab2:
    sku_in = st.text_input(t("商品コード / JAN（部分匹配 · 留空 = 全部）"), "", key="sku_filter")
    if sku_in.strip():
        s = sku_in.strip().lower()
        sub2 = df[
            df["item_code"].astype(str).str.lower().str.contains(s, na=False)
            | df["jan"].astype(str).str.lower().str.contains(s, na=False)
        ].copy()
    else:
        sub2 = df.copy()
    sub2 = sub2[sub2["qty_on_hand"] > 0]

    # tab 内 构成 KPI（用途类别多选 · 联动）
    sub2 = _render_tab_kpi(sub2, "t2")
    st.divider()

    if sub2.empty:
        st.info(t("无匹配数据"))
    else:
        if not sku_in.strip():
            st.caption(t("提示：输入商品コード / JAN 可精确定位 · 当前展示全仓库筛选后明细"))
        disp2 = sub2[["item_code", "jan", "display_name", "warehouse",
                      "bin_number", "category", "qty_on_hand", "amt"]].rename(columns={
            "item_code": t("商品コード"), "jan": t("JAN"),
            "display_name": t("商品名"), "warehouse": t("仓库"),
            "bin_number": t("棚番号"), "category": t("用途"),
            "qty_on_hand": t("手持"), "amt": t("金额(¥)"),
        }).sort_values([t("商品コード"), t("仓库"), t("棚番号")])
        disp2[t("棚番号")] = disp2[t("棚番号")].fillna(t("（不分 bin）"))
        st.dataframe(disp2, hide_index=True, use_container_width=True, height=480,
                     column_config={t("金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})
        st.download_button(t("📥 CSV 下载"),
                           disp2.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"sku_inv_{inv_date}.csv", mime="text/csv",
                           key="sku_csv")
