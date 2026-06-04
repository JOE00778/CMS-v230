"""模块 #18 库存风控 · 库存月数（当前库存/月销量）による在庫リスク監視盘。

データ源: nst.inventory_activity_monthly（月销量 sold）+ nst.inventory_snapshot（当前JD库存）
  + nst.item_master_raw（旧 item_monthly_turnover / item_v2 を置換·page25 発注AI と同一源）。
  · 库存月数 = 当前JD库存 / 直近月 sold → 阈値で 3 档:
        < 补货线 → 断货风险(要补货) / > 压库存线 → 压库存 / 中间 → 正常（Boss 随时可调·下記 expander）
  · 完売率 = sold/(opening+received) は **発注結果の参考指标**·分档には使わない（Boss 2026-06-04 訂正）
  · 资金占用 = 当前库存 × cost_estimate（压库存 = 圧迫資金）

⚠️ 本ページは**リスク識別のみ**。発注量・仕入先選択は責務外 → 📦 発注AI v2（page25·唯一の下单引擎）。
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
)

st.set_page_config(page_title=t("库存风控"), page_icon="🛡️", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🛡️ 库存风控"))
st.caption(t("按库存月数(当前JD库存/月销量)识别断货 / 压库存风险 · 完売率仅作发注结果参考 · 🛒 精确补货量请用「📦 発注AI v2」"))


def _df(sql, params=None):
    return _df_conn(conn, sql, params)


# ============================================================
# ⚙️ 风险阈值（Boss 随时可调·独立持久化, 不影响発注AI 系数阈值）
# ============================================================
_saved = load_risk_thresholds()
with st.expander(f"⚙️ {t('风险阈值设定 (库存月数·随时可调)')}", expanded=False):
    tcol1, tcol2, tcol3 = st.columns([1.6, 1.6, 1])
    reorder = tcol1.number_input(
        t("🔴 补货线 (库存月数 <)"),
        min_value=0.0, max_value=24.0, value=float(_saved["reorder_months"]), step=0.5,
        key="risk_th_reorder",
        help=t("库存月数 = 当前JD库存 / 直近月销量。低于此线 = 断货风险, 要补货"))
    overstock = tcol2.number_input(
        t("🟡 压库存线 (库存月数 >)"),
        min_value=0.0, max_value=60.0, value=float(_saved["overstock_months"]), step=0.5,
        key="risk_th_overstock",
        help=t("高于此线 = 压库存, 减少订货 / 优先消化"))
    with tcol3:
        st.write("")
        st.write("")
        if st.button(t("💾 保存阈值"), use_container_width=True):
            save_risk_thresholds({"reorder_months": reorder, "overstock_months": overstock})
            st.success(t("✓ 已保存"))
    if reorder > overstock:
        st.warning(t("⚠️ 补货线应 < 压库存线"))
_th = {"reorder_months": reorder, "overstock_months": overstock}


# ============================================================
# 数据加载（全月份·nst.* 权威源）→ 派生列由 inventory_risk.enrich 补
# ============================================================
try:
    df_all = _df(
        """
        SELECT im.internal_id AS internal_id,
               im.item_code AS item_code, im.jan AS jan,
               COALESCE(im.display_name, '') AS display_name,
               im.item_rank AS rank, im.maker AS maker,
               im.cost_estimate AS cost_estimate,
               im.last_purchase_cost AS last_purchase_cost,
               a.location AS location, a.year_month AS year_month,
               a.opening_qty AS opening_qty, a.received_qty AS received_qty,
               a.sold_qty AS qty_sold, a.closing_qty AS close_qty
        FROM nst.inventory_activity_monthly a
        JOIN nst.item_master_raw im ON im.internal_id = a.item_internal_id
        """
    )
except Exception as e:
    st.error(t("⚠️ 读取 nst.inventory_activity_monthly 失败（需 Postgres/NST 数据源）。"))
    st.caption(str(e))
    st.stop()

if df_all.empty:
    st.warning(t("⚠️ 暂无月度库存活动数据（nst.inventory_activity_monthly 为空）· 等待 NST 受領 daily pull 落数。"))
    st.stop()

# 当前库存（JD 当天手持·最新快照）→ 风险分档分子（库存月数 = 当前库存/月销量）
try:
    _cur = _df(
        "SELECT item_internal_id, SUM(qty_on_hand) AS current_stock "
        "FROM nst.inventory_snapshot "
        "WHERE warehouse LIKE 'JD%' "
        "  AND snapshot_date = (SELECT MAX(snapshot_date) FROM nst.inventory_snapshot) "
        "GROUP BY item_internal_id")
except Exception:
    _cur = pd.DataFrame(columns=["item_internal_id", "current_stock"])
if _cur.empty:
    st.warning(t("⚠️ 当前库存(nst.inventory_snapshot)无数据 → 风险分档不可靠（有销量者都会判为断货）。等待 NST 库存 pull 落数。"))
    df_all["current_stock"] = 0
else:
    df_all = df_all.merge(_cur, how="left", left_on="internal_id", right_on="item_internal_id")
    if "item_internal_id" in df_all.columns:
        df_all = df_all.drop(columns=["item_internal_id"])
df_all["current_stock"] = pd.to_numeric(df_all["current_stock"], errors="coerce").fillna(0)

# 在途残（未关闭 PO 的入荷残）→ 用于「有/无在途」筛选
try:
    _itx_all = _df(
        "SELECT item_internal_id, SUM(quantity - COALESCE(quantity_received,0)) AS in_transit_qty "
        "FROM nst.purchase_order_line "
        "WHERE closed = FALSE AND (quantity - COALESCE(quantity_received,0)) > 0 "
        "GROUP BY item_internal_id")
except Exception:
    _itx_all = pd.DataFrame(columns=["item_internal_id", "in_transit_qty"])
if not _itx_all.empty:
    df_all = df_all.merge(_itx_all, how="left", left_on="internal_id", right_on="item_internal_id")
    if "item_internal_id" in df_all.columns:
        df_all = df_all.drop(columns=["item_internal_id"])
if "in_transit_qty" not in df_all.columns:
    df_all["in_transit_qty"] = 0
df_all["in_transit_qty"] = pd.to_numeric(df_all["in_transit_qty"], errors="coerce").fillna(0)

df_all = enrich(df_all, _th)


# ============================================================
# 我的看板（预设视图）
# ============================================================
months = sorted(df_all["year_month"].dropna().unique().tolist(), reverse=True)
locations_all = sorted([x for x in df_all["location"].dropna().unique().tolist() if str(x).strip()])

PRESETS = {
    "全部 SKU": {"risks": [], "rank": []},
    "断货 + 压库存": {"risks": [RISK_STOCKOUT, RISK_OVERSTOCK], "rank": []},
    "A/B 商品": {"risks": [], "rank": ["A", "B"]},
    "仅 NEW": {"risks": [], "rank": ["NEW"]},
}

st.markdown(f"##### 🗂️ {t('我的看板')}")
preset_options = list(PRESETS.keys())
try:
    sel_preset = st.segmented_control(
        t("预设视图"), options=preset_options, default="全部 SKU", label_visibility="collapsed")
except (AttributeError, TypeError):
    sel_preset = st.radio(
        t("预设视图"), options=preset_options, index=0, horizontal=True, label_visibility="collapsed")
if not sel_preset:
    sel_preset = "全部 SKU"
preset = PRESETS[sel_preset]

_default_risks = preset["risks"] if preset["risks"] else [RISK_STOCKOUT, RISK_OVERSTOCK]
_default_ranks = preset["rank"]

# 预设变化时清掉旧 multiselect 状态, 让 default 生效
_preset_state_key = "page18_last_preset"
if st.session_state.get(_preset_state_key) != sel_preset:
    for k in ("page18_risks", "page18_locs", "page18_ranks"):
        st.session_state.pop(k, None)
    st.session_state[_preset_state_key] = sel_preset

st.divider()


# ============================================================
# 筛选器
# ============================================================
# 商品等级 选项（常驻筛选·item_rank 全量）
rank_opts = (sorted([x for x in df_all["rank"].dropna().unique().tolist() if str(x).strip()])
             if "rank" in df_all.columns else [])

f1, f2, f3, f4, f5 = st.columns([1.1, 1.7, 1.7, 1.7, 1.3])
with f1:
    sel_month = st.selectbox(t("月份"), months, index=0)
with f2:
    sel_risks = st.multiselect(
        t("风险等级"), options=list(RISK_LABELS), default=_default_risks, key="page18_risks")
with f3:
    sel_ranks = st.multiselect(
        t("商品等级"), options=rank_opts,
        default=[r for r in _default_ranks if r in rank_opts], key="page18_ranks")
with f4:
    sel_locations = st.multiselect(
        t("仓库 (location)"), options=locations_all, default=locations_all, key="page18_locs")
with f5:
    sel_intransit = st.selectbox(
        t("在途状态"), [t("全部"), t("有在途"), t("没在途")], index=0, key="page18_intransit")

search_kw = st.text_area(
    t("JAN / item_code 搜索（多个用 空格 / 逗号 / 换行 分隔）"),
    placeholder="4901111  4902222\n01-0641-134",
    height=72, key="page18_search")

# 应用筛选
df = df_all[df_all["year_month"] == sel_month].copy()
if sel_locations:
    df = df[df["location"].isin(sel_locations)]
if sel_risks:
    df = df[df["risk_label"].isin(sel_risks)]
if sel_ranks and "rank" in df.columns:
    df = df[df["rank"].astype(str).isin(sel_ranks)]
if sel_intransit == t("有在途"):
    df = df[df["in_transit_qty"] > 0]
elif sel_intransit == t("没在途"):
    df = df[df["in_transit_qty"] <= 0]
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
        st.caption(t(f"🔎 多关键词搜索: {len(_tokens)} 个 → 命中 {len(df)} 行"))


# ============================================================
# 顶部风险概览卡（当前月 + 筛选后视图）
# ============================================================
risk_counts = df["risk_label"].value_counts()
overstock_capital = float(df.loc[df["risk_label"] == RISK_OVERSTOCK, "capital_exposure"].sum())

k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric(t("覆盖月数"), int(df_all["year_month"].nunique()))
k2.metric(t("SKU 总数"), int(df["item_code"].nunique()))
k3.metric(t("🔴 断货风险"), int(risk_counts.get(RISK_STOCKOUT, 0)))
k4.metric(t("🟡 压库存"), int(risk_counts.get(RISK_OVERSTOCK, 0)))
k5.metric(t("🟢 正常"), int(risk_counts.get(RISK_NORMAL, 0)))
k6.metric(t("💰 压库存资金占用"), f"¥{overstock_capital:,.0f}")

st.caption(t(f"当前筛选结果: {len(df)} 行 · 补货线<{reorder:g}月 / 压库存线>{overstock:g}月 · 当前库存=JD当天手持"))
st.divider()


# ============================================================
# 列显示 toggle
# ============================================================
ALL_COLS = [
    ("item_code", t("item_code")),
    ("jan", t("JAN")),
    ("display_name", t("商品名")),
    ("location", t("仓库")),
    ("qty_sold", t("月销量")),
    ("current_stock", t("当前库存(JD)")),
    ("stock_months", t("库存月数")),
    ("sell_through_rate", t("完売率(参考)")),
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
    show_disp = show.copy()
    rate_col = t("完売率(参考)")
    if rate_col in show_disp.columns:
        show_disp[rate_col] = (
            pd.to_numeric(show_disp[rate_col], errors="coerce").fillna(0) * 100
        ).round(1).astype(str) + "%"
    sm_col = t("库存月数")
    if sm_col in show_disp.columns:
        show_disp[sm_col] = pd.to_numeric(show_disp[sm_col], errors="coerce").fillna(0).round(1)
    cap_col = t("资金占用(¥)")
    if cap_col in show_disp.columns:
        show_disp[cap_col] = pd.to_numeric(show_disp[cap_col], errors="coerce").fillna(0).map(
            lambda v: f"¥{v:,.0f}")
    st.dataframe(show_disp, use_container_width=True, height=420)
    st.download_button(
        t("📥 下载 CSV"),
        data=show.to_csv(index=False).encode("utf-8-sig"),
        file_name=csv_name, mime="text/csv", key=f"dl_{csv_name}")


# ============================================================
# 3 风险清单 Tab
# ============================================================
tab_red, tab_yellow, tab_green, tab_360 = st.tabs([
    t("🔴 断货风险"),
    t("🟡 压库存"),
    t("🟢 正常"),
    t("📋 SKU 360"),
])

with tab_red:
    red = df[df["risk_label"] == RISK_STOCKOUT].sort_values(
        "stock_months", ascending=True, na_position="last")   # 库存月数最少 = 最急
    st.subheader(t(f"🔴 断货风险清单 (库存月数 < {reorder:g}月 · 要补货)"))
    st.caption(t("库存薄 / 快断货 · 库存月数升序(最急在前) · 🛒 下单去発注AI"))
    _render_df_with_csv(red, f"inv_risk_stockout_{sel_month}.csv")

with tab_yellow:
    yellow = df[df["risk_label"] == RISK_OVERSTOCK].sort_values(
        "capital_exposure", ascending=False, na_position="last")
    st.subheader(t(f"🟡 压库存清单 (库存月数 > {overstock:g}月 · 按资金占用降序)"))
    st.caption(t("卖得慢 / 压资金 · 减少订货, 优先消化资金占用最高者"))
    _render_df_with_csv(yellow, f"inv_risk_overstock_{sel_month}.csv")
    if not yellow.empty:
        st.metric(t("本档资金占用合计 (¥)"),
                  f"¥{float(yellow['capital_exposure'].sum()):,.0f}")

with tab_green:
    green = df[df["risk_label"] == RISK_NORMAL].sort_values(
        "stock_months", ascending=True, na_position="last")
    st.subheader(t(f"🟢 正常 SKU ({reorder:g} ≤ 库存月数 ≤ {overstock:g}月)"))
    st.caption(t("库存健康区间 · 参考"))
    _render_df_with_csv(green, f"inv_risk_normal_{sel_month}.csv")


# ----- 📋 SKU 360（决策上下文宽表）-----
with tab_360:
    st.subheader(t("📋 SKU 360 · 决策上下文宽表"))
    st.caption(t("当前筛选范围 · 销量 / 库存 / 在途PO / 采购价 / 等级一览 · 缺数据列显 0"))
    if df.empty:
        st.info(t("当前筛选无数据"))
    else:
        from shared.inventory_risk import inventory_turnover

        def _aux(sql, cols):
            try:
                return _df(sql)
            except Exception:
                return pd.DataFrame(columns=cols)

        # 前30天销量（滚动·sales_daily）
        s30 = _aux(
            "SELECT item_internal_id, SUM(qty_sold) AS sales_30d "
            "FROM nst.sales_daily WHERE sale_date >= CURRENT_DATE - INTERVAL '30 days' "
            "GROUP BY item_internal_id",
            ["item_internal_id", "sales_30d"])
        # 当天库存 by 仓（最新快照·JD / 弁天）
        invc = _aux(
            "SELECT item_internal_id, "
            "SUM(CASE WHEN warehouse LIKE 'JD%' THEN qty_on_hand ELSE 0 END) AS stock_jd, "
            "SUM(CASE WHEN warehouse LIKE '弁天%' THEN qty_on_hand ELSE 0 END) AS stock_benten "
            "FROM nst.inventory_snapshot "
            "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM nst.inventory_snapshot) "
            "GROUP BY item_internal_id",
            ["item_internal_id", "stock_jd", "stock_benten"])
        # 在途（未关闭 PO 的入荷残）+ 供应商列表 + 最近 PO
        itx = _aux(
            "SELECT item_internal_id, "
            "SUM(quantity - COALESCE(quantity_received,0)) AS in_transit_qty, "
            "string_agg(DISTINCT vendor_name, ', ') AS in_transit_suppliers, "
            "MAX(po_number) AS latest_po "
            "FROM nst.purchase_order_line "
            "WHERE closed = FALSE AND (quantity - COALESCE(quantity_received,0)) > 0 "
            "GROUP BY item_internal_id",
            ["item_internal_id", "in_transit_qty", "in_transit_suppliers", "latest_po"])
        # 上月销量（复用 df_all · months 为 reverse=True，后一个即更早月）
        prev_ym = None
        if sel_month in months:
            _i = months.index(sel_month)
            if _i + 1 < len(months):
                prev_ym = months[_i + 1]
        prevm = (df_all[df_all["year_month"] == prev_ym][["internal_id", "qty_sold"]]
                 .rename(columns={"qty_sold": "prev_month_sold"})
                 if prev_ym else pd.DataFrame(columns=["internal_id", "prev_month_sold"]))

        wide = df[["internal_id", "item_code", "jan", "display_name", "maker", "rank",
                   "risk_label", "qty_sold", "stock_months", "sell_through_rate", "close_qty",
                   "capital_exposure", "last_purchase_cost"]].drop_duplicates("internal_id").copy()
        for aux in (s30, invc, itx):
            if not aux.empty:
                wide = wide.merge(aux, how="left", left_on="internal_id", right_on="item_internal_id")
                if "item_internal_id" in wide.columns:
                    wide = wide.drop(columns=["item_internal_id"])
        if not prevm.empty:
            wide = wide.merge(prevm, how="left", on="internal_id")

        for c in ("sales_30d", "prev_month_sold", "stock_jd", "stock_benten", "in_transit_qty",
                  "last_purchase_cost"):
            if c not in wide.columns:
                wide[c] = 0
            wide[c] = pd.to_numeric(wide[c], errors="coerce").fillna(0)
        for c in ("in_transit_suppliers", "latest_po"):
            if c not in wide.columns:
                wide[c] = ""
            wide[c] = wide[c].fillna("")
        wide["inv_turnover"] = [inventory_turnover(s, j)
                                for s, j in zip(wide["qty_sold"], wide["stock_jd"])]

        COLS360 = [
            ("item_code", t("item_code")), ("jan", t("JAN")), ("display_name", t("商品名")),
            ("maker", t("厂家")), ("rank", t("商品等级")), ("risk_label", t("风险")),
            ("qty_sold", t("当月销量")), ("sales_30d", t("前30天销量")),
            ("prev_month_sold", t("上月销量")),
            ("stock_months", t("库存月数")), ("inv_turnover", t("库存周转率")),
            ("sell_through_rate", t("完売率(参考)")),
            ("stock_jd", t("当天库存(JD)")), ("stock_benten", t("当天库存(弁天)")),
            ("in_transit_qty", t("在途残")), ("in_transit_suppliers", t("在途供应商")),
            ("latest_po", t("最近PO")), ("last_purchase_cost", t("最近采购价")),
            ("capital_exposure", t("资金占用(¥)")),
        ]
        order = [k for k, _ in COLS360 if k in wide.columns]
        show360 = wide[order].copy()
        disp = show360.copy()
        disp.columns = [dict(COLS360)[k] for k in order]
        rc = t("完売率(参考)")
        if rc in disp.columns:
            disp[rc] = (pd.to_numeric(disp[rc], errors="coerce").fillna(0) * 100).round(1).astype(str) + "%"
        for _nc in (t("库存周转率"), t("库存月数")):
            if _nc in disp.columns:
                disp[_nc] = pd.to_numeric(disp[_nc], errors="coerce").fillna(0).round(2)
        cap = t("资金占用(¥)")
        if cap in disp.columns:
            disp[cap] = pd.to_numeric(disp[cap], errors="coerce").fillna(0).map(lambda v: f"¥{v:,.0f}")
        lpc = t("最近采购价")
        if lpc in disp.columns:
            disp[lpc] = pd.to_numeric(disp[lpc], errors="coerce").fillna(0).map(lambda v: f"¥{v:,.0f}")

        st.caption(t(f"{len(show360)} 个 SKU · 月份 {sel_month}"))
        st.dataframe(disp, use_container_width=True, height=520)
        st.download_button(
            t("📥 下载 SKU 360 CSV"),
            data=show360.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"sku360_{sel_month}.csv", mime="text/csv", key="dl_sku360")


st.divider()


# ============================================================
# 单 SKU 跨月趋势（nst.* 源）
# ============================================================
st.subheader(t("📈 单 SKU 历史趋势 (跨月完売率)"))
trend_input = st.text_input(
    t("输入 item_code 查看跨月趋势"), placeholder=t("例: 01-0641-134"), key="trend_item_input")

if trend_input.strip():
    item = trend_input.strip()
    trend_df = df_all[df_all["item_code"].astype(str) == item].copy()
    if trend_df.empty:
        st.info(t(f"未找到 item_code = {item} 的历史记录"))
    else:
        agg = (
            trend_df.groupby("year_month", as_index=False)
            .agg(qty_sold=("qty_sold", "sum"),
                 opening_qty=("opening_qty", "sum"),
                 received_qty=("received_qty", "sum"),
                 close_qty=("close_qty", "sum"))
            .sort_values("year_month")
        )
        denom = (agg["opening_qty"] + agg["received_qty"]).replace(0, pd.NA)
        agg["sell_through_rate"] = (agg["qty_sold"] / denom).fillna(0)
        st.dataframe(
            agg.rename(columns={
                "year_month": t("月份"), "qty_sold": t("月销量"),
                "opening_qty": t("期初"), "received_qty": t("入库"),
                "close_qty": t("期末"), "sell_through_rate": t("完売率"),
            }),
            use_container_width=True, hide_index=True)

        if len(agg) >= 2:
            import altair as alt
            _x = alt.X("year_month:N", sort=None, title=None, axis=alt.Axis(labelAngle=0))
            _near = alt.selection_point(nearest=True, on="pointerover",
                                        fields=["year_month"], empty=False)
            _b = alt.Chart(agg).encode(x=_x)
            _ln = _b.mark_line(point=True).encode(
                y=alt.Y("sell_through_rate:Q", title=t("完売率"), axis=alt.Axis(format=".0%")))
            _rl = _b.mark_rule(color="#888").encode(
                opacity=alt.condition(_near, alt.value(0.35), alt.value(0)),
                tooltip=[alt.Tooltip("year_month:N", title=t("月份")),
                         alt.Tooltip("sell_through_rate:Q", title=t("完売率"), format=".1%")],
            ).add_params(_near)
            st.altair_chart(alt.layer(_ln, _rl).properties(height=300), use_container_width=True)
