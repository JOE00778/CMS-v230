"""模块 #18 库存风控 · 月完売率による在庫リスク監視盘。

データ源: nst.inventory_activity_monthly（NST 受領 daily pull）+ nst.item_master_raw
  （旧 item_monthly_turnover / item_v2 を置換·page25 発注AI エンジンと同一权威源）。
  · 月完売率 = sold_qty / (opening_qty + received_qty)
  · 阈値で 3 档: ≥high 断货风险 / ≥low 正常 / <low 压库存（Boss 随时可调·下記 expander）
  · 资金占用 = closing_qty × cost_estimate（压库存の資金占用 = リスク金額）

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
st.caption(t("按月完売率识别断货 / 压库存风险 · 🛒 精确补货量请用「📦 発注AI v2」"))


def _df(sql, params=None):
    return _df_conn(conn, sql, params)


# ============================================================
# ⚙️ 风险阈值（Boss 随时可调·独立持久化, 不影响発注AI 系数阈值）
# ============================================================
_saved = load_risk_thresholds()
with st.expander(f"⚙️ {t('风险阈值设定 (随时可调)')}", expanded=False):
    tcol1, tcol2, tcol3 = st.columns([1.4, 1.4, 1])
    high = tcol1.number_input(
        t("🔴 断货风险阈值 (完売率 ≥)"),
        min_value=0.0, max_value=1.0, value=float(_saved["high"]), step=0.05,
        key="risk_th_high",
    )
    low = tcol2.number_input(
        t("🟡 压库存阈值 (完売率 <)"),
        min_value=0.0, max_value=1.0, value=float(_saved["low"]), step=0.05,
        key="risk_th_low",
    )
    with tcol3:
        st.write("")
        st.write("")
        if st.button(t("💾 保存阈值"), use_container_width=True):
            save_risk_thresholds({"high": high, "low": low})
            st.success(t("✓ 已保存"))
    if low > high:
        st.warning(t("⚠️ 压库存阈值应 < 断货风险阈值"))
_th = {"high": high, "low": low}


# ============================================================
# 数据加载（全月份·nst.* 权威源）→ 派生列由 inventory_risk.enrich 补
# ============================================================
try:
    df_all = _df(
        """
        SELECT im.item_code AS item_code, im.jan AS jan,
               COALESCE(im.display_name, '') AS display_name,
               im.item_rank AS rank, im.maker AS maker,
               im.cost_estimate AS cost_estimate,
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
    st.warning(t("⚠️ 暂无月完売率数据（nst.inventory_activity_monthly 为空）· 等待 NST 受領 daily pull 落数。"))
    st.stop()

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
f1, f2, f3, f4 = st.columns([1.2, 2, 2, 2])
with f1:
    sel_month = st.selectbox(t("月份"), months, index=0)
with f2:
    sel_locations = st.multiselect(
        t("仓库 (location)"), options=locations_all, default=locations_all, key="page18_locs")
with f3:
    sel_risks = st.multiselect(
        t("风险等级"), options=list(RISK_LABELS), default=_default_risks, key="page18_risks")
with f4:
    search_kw = st.text_input(t("JAN / item_code 搜索"), placeholder=t("例: 4901111... 或 01-0641-134"))

# Rank 筛选（仅当预设需要时）
sel_ranks = []
if _default_ranks and "rank" in df_all.columns:
    rank_opts = sorted([x for x in df_all["rank"].dropna().unique().tolist() if str(x).strip()])
    if rank_opts:
        sel_ranks = st.multiselect(
            t("Rank 筛选 (来自预设)"), options=rank_opts,
            default=[r for r in _default_ranks if r in rank_opts], key="page18_ranks")

# 应用筛选
df = df_all[df_all["year_month"] == sel_month].copy()
if sel_locations:
    df = df[df["location"].isin(sel_locations)]
if sel_risks:
    df = df[df["risk_label"].isin(sel_risks)]
if sel_ranks and "rank" in df.columns:
    df = df[df["rank"].astype(str).isin(sel_ranks)]
if search_kw:
    kw = search_kw.strip()
    df = df[
        df["item_code"].astype(str).str.contains(kw, case=False, na=False)
        | df["jan"].astype(str).str.contains(kw, case=False, na=False)
    ]


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

st.caption(t(f"当前筛选结果: {len(df)} 行 · 阈值 断货≥{high:.0%} / 压库存<{low:.0%}"))
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
    ("available_qty", t("合计可售")),
    ("close_qty", t("期末库存")),
    ("sell_through_rate", t("完売率")),
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
    rate_col = t("完売率")
    if rate_col in show_disp.columns:
        show_disp[rate_col] = (
            pd.to_numeric(show_disp[rate_col], errors="coerce").fillna(0) * 100
        ).round(1).astype(str) + "%"
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
tab_red, tab_yellow, tab_green = st.tabs([
    t("🔴 断货风险"),
    t("🟡 压库存"),
    t("🟢 正常"),
])

with tab_red:
    red = df[df["risk_label"] == RISK_STOCKOUT].sort_values(
        "sell_through_rate", ascending=False, na_position="last")
    st.subheader(t(f"🔴 断货风险清单 (完売率 ≥ {high:.0%})"))
    st.caption(t("卖得快 / 快断货 · 关注补货优先级 · 🛒 下单去発注AI"))
    _render_df_with_csv(red, f"inv_risk_stockout_{sel_month}.csv")

with tab_yellow:
    yellow = df[df["risk_label"] == RISK_OVERSTOCK].sort_values(
        "capital_exposure", ascending=False, na_position="last")
    st.subheader(t(f"🟡 压库存清单 (完売率 < {low:.0%}, 按资金占用降序)"))
    st.caption(t("卖得慢 / 压资金 · 减少订货, 优先消化资金占用最高者"))
    _render_df_with_csv(yellow, f"inv_risk_overstock_{sel_month}.csv")
    if not yellow.empty:
        st.metric(t("本档资金占用合计 (¥)"),
                  f"¥{float(yellow['capital_exposure'].sum()):,.0f}")

with tab_green:
    green = df[df["risk_label"] == RISK_NORMAL].sort_values(
        "sell_through_rate", ascending=False, na_position="last")
    st.subheader(t(f"🟢 正常 SKU ({low:.0%} ≤ 完売率 < {high:.0%})"))
    st.caption(t("健康区间 · 参考"))
    _render_df_with_csv(green, f"inv_risk_normal_{sel_month}.csv")


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
