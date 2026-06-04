"""模块 #18 库存风控 · 可售天数（JDL库存 / 前30天日均销量）による在庫リスク監視盘。

データ源（current-snapshot · 月份次元なし · Boss 2026-06-04）:
  · 前30天销量   = nst.sales_daily（直近30日 SUM·item_internal_id 単位）
  · 当前库存(JDL) = jdl.v_inventory_reconciliation.jdl_qty_in_stock（JD物流実物在仓·jan 単位）
  · 可售天数      = JDL库存 / 日均销量(前30天/30) → 阈値(天)で 3 档:
        < 断货线 → 断货风险(要补货) / > 压库存线 → 压库存 / 中间 → 正常（Boss 随时可调）
  · 完売率 / 月末在库 / 月度活动表 / 月份选择 は不使用。
  · 资金占用 = JDL库存 × cost_estimate。只取有商品等级的（item_rank 非空·无等级忽略）。

⚠️ リスク識別のみ。発注量・仕入先選択 → 📦 発注AI v2（page25·唯一の下单引擎）。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.db_helpers import df as _df_conn
from shared.i18n import lang_selector, t
from shared.inventory_risk import (
    RISK_STOCKOUT, RISK_NORMAL, RISK_OVERSTOCK, RISK_NO_DATA, RISK_LABELS,
    enrich, load_risk_thresholds, save_risk_thresholds,
    is_stockout, stockout_rate_by_rank,
)

st.set_page_config(page_title=t("库存风控"), page_icon="🛡️", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🛡️ 库存风控"))
st.caption(t("按可售天数(JDL库存/前30天日均销量)识别断货 / 压库存风险 · 仅有等级商品 · "
             "🛒 精确补货量请用「📦 発注AI v2」"))


def _df(sql, params=None):
    return _df_conn(conn, sql, params)


# ============================================================
# ⚙️ 风险阈值（可售天数·Boss 随时可调·独立持久化）
# ============================================================
_saved = load_risk_thresholds()
with st.expander(f"⚙️ {t('风险阈值设定 (可售天数·随时可调)')}", expanded=False):
    tcol1, tcol2, tcol3 = st.columns([1.6, 1.6, 1])
    reorder = tcol1.number_input(
        t("🔴 断货线 (可售天数 <)"),
        min_value=0.0, max_value=365.0, value=float(_saved["reorder_days"]), step=5.0,
        key="risk_th_reorder",
        help=t("可售天数 = JDL库存 / 日均销量(前30天/30)。低于此线 = 断货风险, 要补货"))
    overstock = tcol2.number_input(
        t("🟡 压库存线 (可售天数 >)"),
        min_value=0.0, max_value=999.0, value=float(_saved["overstock_days"]), step=5.0,
        key="risk_th_overstock",
        help=t("高于此线 = 压库存, 减少订货 / 优先消化"))
    with tcol3:
        st.write("")
        st.write("")
        if st.button(t("💾 保存阈值"), use_container_width=True):
            save_risk_thresholds({"reorder_days": reorder, "overstock_days": overstock})
            st.success(t("✓ 已保存"))
    if reorder > overstock:
        st.warning(t("⚠️ 断货线应 < 压库存线"))
_th = {"reorder_days": reorder, "overstock_days": overstock}


# ============================================================
# 数据加载（current-snapshot·只取有等级）
#   qty_sold = 前30天销量（sales_daily）· current_stock = JDL库存（recon view）
# ============================================================
try:
    df_all = _df(
        """
        WITH s30 AS (
            SELECT item_internal_id, SUM(qty_sold) AS qty_sold
            FROM nst.sales_daily
            WHERE sale_date >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY item_internal_id
        )
        SELECT im.internal_id AS internal_id, im.item_code AS item_code, im.jan AS jan,
               COALESCE(im.display_name, '') AS display_name,
               im.item_rank AS rank, im.maker AS maker,
               im.cost_estimate AS cost_estimate, im.last_purchase_cost AS last_purchase_cost,
               COALESCE(s30.qty_sold, 0) AS qty_sold
        FROM nst.item_master_raw im
        LEFT JOIN s30 ON s30.item_internal_id = im.internal_id
        WHERE im.item_rank IS NOT NULL AND btrim(im.item_rank) <> ''
        """
    )
except Exception as e:
    st.error(t("⚠️ 读取 nst.sales_daily / item_master_raw 失败（需 Postgres/NST 数据源）。"))
    st.caption(str(e))
    st.stop()

if df_all.empty:
    st.warning(t("⚠️ 暂无有等级商品数据。"))
    st.stop()

# 当前库存 = JDL 实物在仓（jdl.v_inventory_reconciliation·按 jan）
try:
    _jdl = _df("SELECT jan, jdl_qty_in_stock AS current_stock FROM jdl.v_inventory_reconciliation")
except Exception:
    _jdl = pd.DataFrame(columns=["jan", "current_stock"])
if not _jdl.empty:
    df_all = df_all.merge(_jdl, how="left", on="jan")
if "current_stock" not in df_all.columns:
    df_all["current_stock"] = 0
df_all["current_stock"] = pd.to_numeric(df_all["current_stock"], errors="coerce").fillna(0)

# 在途残（未关闭 PO 的入荷残·按 item_internal_id）
try:
    _itx = _df(
        "SELECT item_internal_id, SUM(quantity - COALESCE(quantity_received,0)) AS in_transit_qty "
        "FROM nst.purchase_order_line "
        "WHERE closed = FALSE AND (quantity - COALESCE(quantity_received,0)) > 0 "
        "GROUP BY item_internal_id")
except Exception:
    _itx = pd.DataFrame(columns=["item_internal_id", "in_transit_qty"])
if not _itx.empty:
    df_all = df_all.merge(_itx, how="left", left_on="internal_id", right_on="item_internal_id")
    if "item_internal_id" in df_all.columns:
        df_all = df_all.drop(columns=["item_internal_id"])
if "in_transit_qty" not in df_all.columns:
    df_all["in_transit_qty"] = 0
df_all["in_transit_qty"] = pd.to_numeric(df_all["in_transit_qty"], errors="coerce").fillna(0)

# 派生列（可售天数 / risk_label / capital_exposure）
df_all = enrich(df_all, _th)
# 断货标记：前30天有销量 且 当前 JDL 库存 = 0
df_all["is_stockout"] = [is_stockout(s, k) for s, k in zip(df_all["qty_sold"], df_all["current_stock"])]


# ============================================================
# 筛选器（无月份 / 无仓库）
# ============================================================
rank_opts = sorted([x for x in df_all["rank"].dropna().unique().tolist() if str(x).strip()])

f1, f2, f3, f4 = st.columns([2, 2, 1.4, 1.4])
with f1:
    sel_risks = st.multiselect(
        t("风险等级"), options=list(RISK_LABELS), default=[], key="page18_risks")
with f2:
    sel_ranks = st.multiselect(t("商品等级"), options=rank_opts, default=[], key="page18_ranks")
with f3:
    sel_intransit = st.selectbox(
        t("在途状态"), [t("全部"), t("有在途"), t("没在途")], index=0, key="page18_intransit")
with f4:
    sel_instock = st.selectbox(
        t("库存状态"), [t("全部"), t("有货"), t("断货")], index=0, key="page18_instock",
        help=t("断货 = 前30天有销量 但 当前JDL库存=0"))

search_kw = st.text_area(
    t("JAN / item_code 搜索（多个用 空格 / 逗号 / 换行 分隔）"),
    placeholder="4901111  4902222\n01-0641-134", height=72, key="page18_search")

# 应用筛选
df = df_all.copy()
if sel_risks:
    df = df[df["risk_label"].isin(sel_risks)]
if sel_ranks:
    df = df[df["rank"].astype(str).isin(sel_ranks)]
if sel_intransit == t("有在途"):
    df = df[df["in_transit_qty"] > 0]
elif sel_intransit == t("没在途"):
    df = df[df["in_transit_qty"] <= 0]
if sel_instock == t("有货"):
    df = df[df["current_stock"] > 0]
elif sel_instock == t("断货"):
    df = df[df["is_stockout"]]
if search_kw and search_kw.strip():
    import re as _re
    _tokens = [tk for tk in _re.split(r"[\s,，、;；]+", search_kw.strip()) if tk]
    if _tokens:
        _ic = df["item_code"].astype(str)
        _jn = df["jan"].astype(str)
        _mask = pd.Series(False, index=df.index)
        for _tk in _tokens:
            _mask = (_mask
                     | _ic.str.contains(_tk, case=False, na=False, regex=False)
                     | _jn.str.contains(_tk, case=False, na=False, regex=False))
        df = df[_mask]


# ============================================================
# 顶部风险概览卡
# ============================================================
risk_counts = df["risk_label"].value_counts()
overstock_capital = float(df.loc[df["risk_label"] == RISK_OVERSTOCK, "capital_exposure"].sum())

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric(t("SKU 总数"), int(df["item_code"].nunique()))
k2.metric(t("🔴 断货风险"), int(risk_counts.get(RISK_STOCKOUT, 0)))
k3.metric(t("🟡 压库存"), int(risk_counts.get(RISK_OVERSTOCK, 0)))
k4.metric(t("🟢 正常"), int(risk_counts.get(RISK_NORMAL, 0)))
k5.metric(t("💰 压库存资金占用"), f"¥{overstock_capital:,.0f}")

st.caption(t(f"前30天销售 + JDL实物库存 · 断货线<{reorder:g}天 / 压库存线>{overstock:g}天 · "
             f"仅有等级商品 · 当前筛选 {len(df)} 行"))

# 断货率（按商品等级·有等级全量·断货=前30天有销量+JDL库存0）
_so = stockout_rate_by_rank(df_all)
if not _so.empty:
    with st.expander(f"📊 {t('断货率（按商品等级 · 前30天有销量+JDL库存0）')}", expanded=False):
        _sod = _so.copy()
        _sod["rate"] = (pd.to_numeric(_sod["rate"], errors="coerce").fillna(0) * 100).round(1).astype(str) + "%"
        _sod.columns = [t("商品等级"), t("总数"), t("断货数"), t("断货率")]
        st.dataframe(_sod, use_container_width=True, hide_index=True)
st.divider()


# ============================================================
# 列显示 toggle
# ============================================================
ALL_COLS = [
    ("item_code", t("item_code")),
    ("jan", t("JAN")),
    ("display_name", t("商品名")),
    ("maker", t("厂家")),
    ("rank", t("商品等级")),
    ("is_stockout", t("断货")),
    ("qty_sold", t("前30天销量")),
    ("current_stock", t("JDL库存")),
    ("days_of_supply", t("可售天数")),
    ("in_transit_qty", t("在途残")),
    ("last_purchase_cost", t("最近采购价")),
    ("capital_exposure", t("资金占用(¥)")),
]
with st.expander(f"⚙️ {t('显示列设置')}"):
    cols_grid = st.columns(4)
    selected_keys = []
    for i, (key, label) in enumerate(ALL_COLS):
        with cols_grid[i % 4]:
            if st.checkbox(label, value=True, key=f"page18_col_{key}"):
                selected_keys.append(key)
if not selected_keys:
    selected_keys = [k for k, _ in ALL_COLS]
DISPLAY_COLS = selected_keys


def _render_df_with_csv(d: pd.DataFrame, csv_name: str):
    if d.empty:
        st.info(t("当前 Tab 无数据"))
        return
    available = [c for c in DISPLAY_COLS if c in d.columns] or list(d.columns)
    show = d[available].copy()
    show.columns = [dict(ALL_COLS).get(c, c) for c in available]
    so_col = t("断货")
    if so_col in show.columns:
        show[so_col] = show[so_col].map(lambda v: "🚫断货" if bool(v) else "")
    show_disp = show.copy()
    dos_col = t("可售天数")
    if dos_col in show_disp.columns:
        show_disp[dos_col] = pd.to_numeric(show_disp[dos_col], errors="coerce").round(0)
    for _c in (t("资金占用(¥)"), t("最近采购价")):
        if _c in show_disp.columns:
            show_disp[_c] = pd.to_numeric(show_disp[_c], errors="coerce").fillna(0).map(lambda v: f"¥{v:,.0f}")
    st.dataframe(show_disp, use_container_width=True, height=460)
    st.download_button(
        t("📥 下载 CSV"),
        data=show.to_csv(index=False).encode("utf-8-sig"),
        file_name=csv_name, mime="text/csv", key=f"dl_{csv_name}")


# ============================================================
# 3 风险清单 Tab + SKU 360
# ============================================================
tab_red, tab_yellow, tab_green, tab_360 = st.tabs([
    t("🔴 断货风险"), t("🟡 压库存"), t("🟢 正常"), t("📋 SKU 360"),
])

with tab_red:
    red = df[df["risk_label"] == RISK_STOCKOUT].sort_values(
        "days_of_supply", ascending=True, na_position="last")   # 可售天数最少 = 最急
    st.subheader(t(f"🔴 断货风险清单 (可售天数 < {reorder:g}天 · 要补货)"))
    st.caption(t("库存薄 / 快断货 · 可售天数升序(最急在前) · 🛒 下单去発注AI"))
    _render_df_with_csv(red, "inv_risk_stockout.csv")

with tab_yellow:
    yellow = df[df["risk_label"] == RISK_OVERSTOCK].sort_values(
        "capital_exposure", ascending=False, na_position="last")
    st.subheader(t(f"🟡 压库存清单 (可售天数 > {overstock:g}天 · 按资金占用降序)"))
    st.caption(t("卖得慢 / 压资金 · 减少订货, 优先消化资金占用最高者"))
    _render_df_with_csv(yellow, "inv_risk_overstock.csv")
    if not yellow.empty:
        st.metric(t("本档资金占用合计 (¥)"), f"¥{float(yellow['capital_exposure'].sum()):,.0f}")

with tab_green:
    green = df[df["risk_label"] == RISK_NORMAL].sort_values(
        "days_of_supply", ascending=True, na_position="last")
    st.subheader(t(f"🟢 正常 SKU ({reorder:g} ≤ 可售天数 ≤ {overstock:g}天)"))
    st.caption(t("库存健康区间 · 参考"))
    _render_df_with_csv(green, "inv_risk_normal.csv")

with tab_360:
    st.subheader(t("📋 SKU 360 · 决策上下文宽表"))
    st.caption(t("当前筛选范围 · 销量 / 库存 / 在途PO / 采购价 / 等级一览"))
    if df.empty:
        st.info(t("当前筛选无数据"))
    else:
        # 在途供应商 + 最近 PO（按 item_internal_id）
        try:
            sup = _df(
                "SELECT item_internal_id, string_agg(DISTINCT vendor_name, ', ') AS in_transit_suppliers, "
                "MAX(po_number) AS latest_po FROM nst.purchase_order_line "
                "WHERE closed = FALSE AND (quantity - COALESCE(quantity_received,0)) > 0 "
                "GROUP BY item_internal_id")
        except Exception:
            sup = pd.DataFrame(columns=["item_internal_id", "in_transit_suppliers", "latest_po"])
        wide = df[["internal_id", "item_code", "jan", "display_name", "maker", "rank",
                   "risk_label", "is_stockout", "qty_sold", "current_stock", "days_of_supply",
                   "in_transit_qty", "last_purchase_cost", "capital_exposure"]].drop_duplicates("internal_id").copy()
        if not sup.empty:
            wide = wide.merge(sup, how="left", left_on="internal_id", right_on="item_internal_id")
            if "item_internal_id" in wide.columns:
                wide = wide.drop(columns=["item_internal_id"])
        for c in ("in_transit_suppliers", "latest_po"):
            if c not in wide.columns:
                wide[c] = ""
            wide[c] = wide[c].fillna("")

        COLS360 = [
            ("item_code", t("item_code")), ("jan", t("JAN")), ("display_name", t("商品名")),
            ("maker", t("厂家")), ("rank", t("商品等级")), ("risk_label", t("风险")),
            ("is_stockout", t("断货")), ("qty_sold", t("前30天销量")),
            ("current_stock", t("JDL库存")), ("days_of_supply", t("可售天数")),
            ("in_transit_qty", t("在途残")), ("in_transit_suppliers", t("在途供应商")),
            ("latest_po", t("最近PO")), ("last_purchase_cost", t("最近采购价")),
            ("capital_exposure", t("资金占用(¥)")),
        ]
        order = [k for k, _ in COLS360 if k in wide.columns]
        show360 = wide[order].copy()
        disp = show360.copy()
        disp.columns = [dict(COLS360)[k] for k in order]
        if t("断货") in disp.columns:
            disp[t("断货")] = disp[t("断货")].map(lambda v: "🚫断货" if bool(v) else "")
        if t("可售天数") in disp.columns:
            disp[t("可售天数")] = pd.to_numeric(disp[t("可售天数")], errors="coerce").round(0)
        for _c in (t("资金占用(¥)"), t("最近采购价")):
            if _c in disp.columns:
                disp[_c] = pd.to_numeric(disp[_c], errors="coerce").fillna(0).map(lambda v: f"¥{v:,.0f}")
        st.caption(t(f"{len(show360)} 个 SKU"))
        st.dataframe(disp, use_container_width=True, height=520)
        st.download_button(
            t("📥 下载 SKU 360 CSV"),
            data=show360.to_csv(index=False).encode("utf-8-sig"),
            file_name="sku360.csv", mime="text/csv", key="dl_sku360")
