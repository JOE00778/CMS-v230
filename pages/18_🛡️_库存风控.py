"""模块 #18 订货依据 · 基于月完売率的订货决策面板。

数据源:
- item_monthly_turnover (T-XX, ingest 自【輸出】アイテム月完売率300.xls)
  · sell_through_rate = qty_sold / (open_qty + qty_total_in)
  · risk_label = 断货风险 / 正常 / 压库存 / 无数据
- item_v2 (display_name lookup, LEFT JOIN by item_code)

业务阈值 (Boss):
- ≥ 0.9  → 🔴 断货风险 → 加大次月订货 (建议 = ceil(qty_sold * 1.5))
- 0.5-0.9 → 🟢 正常        (建议 = ceil(qty_sold * 1.0))
- < 0.5   → 🟡 压库存 → 减少次月订货 (建议 = max(0, ceil(qty_sold * 0.5)))
"""
from __future__ import annotations

import math

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("订货依据"), page_icon="📦", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📦 订货依据 (基于月完売率)"))
st.caption(t(
    "依据 月完売率 (sell_through_rate) 区分订货策略 · "
    "🔴 ≥0.9 加大订货 / 🟢 0.5-0.9 正常补 / 🟡 <0.5 减少订货"
))


# ============================================================
# helpers
# ============================================================
def _df(sql: str, params=None) -> pd.DataFrame:
    from shared.db_helpers import df
    return df(conn, sql, params)


def _ceil_int(x) -> int:
    """安全 ceil → int (NaN/None → 0)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0
    if pd.isna(v) or v <= 0:
        return 0
    return int(math.ceil(v))


def _suggest_qty(qty_sold, multiplier: float, floor_zero: bool = False) -> int:
    """建议订货量 = ceil(qty_sold * multiplier)."""
    raw = (qty_sold or 0) * multiplier
    if floor_zero and raw < 0:
        return 0
    return _ceil_int(raw)


# ============================================================
# 数据加载 · 全月份 (filter 后再裁剪)
# ============================================================
df_all = _df(
    """
    SELECT mt.*, COALESCE(iv.display_name, '') AS display_name
    FROM item_monthly_turnover mt
    LEFT JOIN item_v2 iv ON iv.item_code = mt.item_code
    """
)

if df_all.empty:
    st.warning(t("⚠️ 当前没有月完売率数据。请先在「⚙️ 数据导入与设置」上传【輸出】アイテム月完売率xls。"))
    st.stop()

# 数据清洗 (NaN-safe)
for col in ("open_qty", "qty_total_in", "qty_sold", "close_qty",
            "close_amount", "out_amount", "sell_through_rate"):
    if col in df_all.columns:
        df_all[col] = pd.to_numeric(df_all[col], errors="coerce").fillna(0)

# 合计可售 = 期初 + 入库
df_all["available_qty"] = df_all["open_qty"] + df_all["qty_total_in"]

# 兜底空 risk_label
df_all["risk_label"] = df_all["risk_label"].fillna("无数据")


# ============================================================
# 顶部 KPI
# ============================================================
months_total = df_all["year_month"].nunique()
sku_total = df_all["item_code"].nunique()
risk_counts = df_all["risk_label"].value_counts()

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric(t("覆盖月数"), int(months_total))
c2.metric(t("SKU 总数"), int(sku_total))
c3.metric(t("🔴 断货风险"), int(risk_counts.get("断货风险", 0)))
c4.metric(t("🟢 正常"), int(risk_counts.get("正常", 0)))
c5.metric(t("🟡 压库存"), int(risk_counts.get("压库存", 0)))
c6.metric(t("⚪ 数据不足"), int(risk_counts.get("数据不足", 0)))

st.divider()


# ============================================================
# 筛选器 (横向)
# ============================================================
months = sorted(df_all["year_month"].dropna().unique().tolist(), reverse=True)
locations_all = sorted([x for x in df_all["location"].dropna().unique().tolist() if str(x).strip()])
risk_options = ["断货风险", "正常", "压库存", "数据不足", "无数据"]

# ============================================================
# T6 · 我的看板（5 个预设视图 · mockup tabs/segmented）
# ============================================================
PRESETS = {
    "全部 SKU": {
        "locations": "all",
        "risks": [],
        "rank": [],
    },
    "断货 + 压库存": {
        "locations": "jd",
        "risks": ["断货风险", "压库存"],
        "rank": [],
    },
    "A/B 商品全部": {
        "locations": "jd",
        "risks": [],
        "rank": ["A", "B"],
    },
    "弁天退品观察": {
        "locations": "benten",
        "risks": [],
        "rank": [],
    },
    "仅 NEW": {
        "locations": "jd",
        "risks": [],
        "rank": ["NEW"],
    },
}

st.markdown(f"##### 🗂️ {t('我的看板')}")

# 兼容老版本 Streamlit 用 radio, 新版本用 segmented_control
preset_options = list(PRESETS.keys())
try:
    sel_preset = st.segmented_control(
        t("预设视图"),
        options=preset_options,
        default="全部 SKU",
        label_visibility="collapsed",
    )
except (AttributeError, TypeError):
    sel_preset = st.radio(
        t("预设视图"),
        options=preset_options,
        index=0,
        horizontal=True,
        label_visibility="collapsed",
    )

# 兜底（segmented_control 在 None 时）
if not sel_preset:
    sel_preset = "全部 SKU"

preset = PRESETS[sel_preset]

# 预设 → 默认值
def _resolve_locs(loc_key: str) -> list[str]:
    if loc_key == "all":
        return locations_all
    if loc_key == "jd":
        return [x for x in locations_all if "JD" in str(x)] or locations_all
    if loc_key == "benten":
        return [x for x in locations_all if "弁天" in str(x) or "Benten" in str(x).lower()] or locations_all
    return locations_all


_default_locs = _resolve_locs(preset["locations"])
_default_risks = preset["risks"] if preset["risks"] else ["断货风险", "压库存"]
_default_ranks = preset["rank"]

# 预设变化时清掉旧 multiselect 状态, 让 default 生效
_preset_state_key = "page18_last_preset"
if st.session_state.get(_preset_state_key) != sel_preset:
    for k in ("page18_locs", "page18_risks", "page18_ranks"):
        st.session_state.pop(k, None)
    st.session_state[_preset_state_key] = sel_preset

st.divider()

f1, f2, f3, f4 = st.columns([1.2, 2, 2, 2])

with f1:
    sel_month = st.selectbox(t("月份"), months, index=0)

with f2:
    # 默认仅 JD-物流-千葉 (主要仓库)
    # 弁天倉庫 = 退回商品暂存仓 (积攒后不定期发到 JD), 不参与订货决策
    sel_locations = st.multiselect(
        t("仓库 (location)"),
        options=locations_all,
        default=_default_locs,
        help=t("默认仅 JD (主仓库)。弁天倉庫 = 退回商品暂存, 不参与订货决策。"),
        key="page18_locs",
    )

with f3:
    sel_risks = st.multiselect(
        t("风险等级"),
        options=risk_options,
        default=_default_risks,
        key="page18_risks",
    )

with f4:
    search_kw = st.text_input(t("JAN / item_code 搜索"), placeholder=t("例: 4901111... 或 01-0641-134"))

# Rank 筛选（A/B/NEW 等）—— 仅当预设需要时显示
sel_ranks = []
if _default_ranks and "rank" in df_all.columns:
    rank_opts = sorted([
        x for x in df_all["rank"].dropna().unique().tolist() if str(x).strip()
    ])
    if rank_opts:
        sel_ranks = st.multiselect(
            t("Rank 筛选 (来自预设)"),
            options=rank_opts,
            default=[r for r in _default_ranks if r in rank_opts],
            key="page18_ranks",
        )

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

st.caption(t(f"当前筛选结果: {len(df)} 行"))


# ============================================================
# T9 · 列显示 toggle  +  T10 · 密度 toggle
# ============================================================
ALL_COLS = [
    ("item_code", t("item_code")),
    ("jan", t("JAN")),
    ("display_name", t("商品名")),
    ("location", t("仓库")),
    ("qty_sold", t("月销量")),
    ("available_qty", t("合计可售")),
    ("sell_through_rate", t("完売率")),
    ("suggest_qty", t("建议订货量")),
]

with st.expander(f"⚙️ {t('显示列设置')}"):
    cols_grid = st.columns(4)
    selected_keys = []
    for i, (key, label) in enumerate(ALL_COLS):
        with cols_grid[i % 4]:
            if st.checkbox(label, value=True, key=f"page18_col_{key}"):
                selected_keys.append(key)

if not selected_keys:
    selected_keys = [k for k, _ in ALL_COLS]  # 至少留全部, 防止空表

DISPLAY_COLS = selected_keys
DISPLAY_HEADER = [dict(ALL_COLS)[k] for k in DISPLAY_COLS]

# 密度 toggle
density_label = st.radio(
    t("密度"),
    options=[t("紧凑"), t("标准"), t("宽松")],
    index=1,
    horizontal=True,
    key="page18_density",
)
_density_class_map = {
    t("紧凑"): "density-compact",
    t("标准"): "",
    t("宽松"): "density-comfy",
}
_density_class = _density_class_map.get(density_label, "")


def _render_df_with_csv(d: pd.DataFrame, csv_name: str):
    if d.empty:
        st.info(t("当前 Tab 无数据"))
        return
    # 缺列兜底（rank 等可能不存在）
    available = [c for c in DISPLAY_COLS if c in d.columns]
    if not available:
        available = list(d.columns)
    show = d[available].copy()
    show.columns = [dict(ALL_COLS).get(c, c) for c in available]
    # 完売率 → 百分比字符串(展示用 copy)
    show_disp = show.copy()
    rate_col = t("完売率")
    if rate_col in show_disp.columns:
        show_disp[rate_col] = (
            pd.to_numeric(show_disp[rate_col], errors="coerce").fillna(0) * 100
        ).round(1).astype(str) + "%"

    if _density_class:
        st.markdown(f"<div class='{_density_class}'>", unsafe_allow_html=True)
    st.dataframe(show_disp, use_container_width=True, height=420)
    if _density_class:
        st.markdown("</div>", unsafe_allow_html=True)

    st.download_button(
        t("📥 下载 CSV"),
        data=show.to_csv(index=False).encode("utf-8-sig"),
        file_name=csv_name,
        mime="text/csv",
        key=f"dl_{csv_name}",
    )


# ============================================================
# 3 Tabs
# ============================================================
tab_red, tab_yellow, tab_green = st.tabs([
    t("🔴 断货风险 (要补货)"),
    t("🟡 压库存 (减少订货)"),
    t("🟢 正常 (参考)"),
])


# ----- 🔴 断货风险 -----
with tab_red:
    red = df[(df["sell_through_rate"] >= 0.9) & (df["qty_sold"] > 0)].copy()
    red = red.sort_values("sell_through_rate", ascending=False, na_position="last")
    red["suggest_qty"] = red["qty_sold"].apply(lambda x: _suggest_qty(x, 1.5))
    st.subheader(t("🔴 断货风险清单 (≥0.9 完売率 + 有销量)"))
    st.caption(t("建议订货量 = ceil(月销量 × 1.5) · 留 50% 安全 buffer"))
    _render_df_with_csv(red, f"order_basis_red_{sel_month}.csv")

    # 金额视角
    if not red.empty:
        st.divider()
        st.markdown(t("##### 💰 金额视角"))
        m1, m2 = st.columns(2)
        close_amt = float(red.get("close_amount", pd.Series(dtype=float)).sum())
        out_amt = float(red.get("out_amount", pd.Series(dtype=float)).sum())
        m1.metric(t("当前在库金额合计 (¥)"), f"¥{close_amt:,.0f}")
        m2.metric(t("上月销售金额合计 (¥)"), f"¥{out_amt:,.0f}")


# ----- 🟡 压库存 -----
with tab_yellow:
    yellow = df[df["sell_through_rate"] < 0.5].copy()
    yellow = yellow.sort_values("close_qty", ascending=False, na_position="last")
    yellow["suggest_qty"] = yellow["qty_sold"].apply(
        lambda x: max(0, _suggest_qty(x, 0.5, floor_zero=True))
    )
    st.subheader(t("🟡 压库存清单 (<0.5 完売率, 库存最高优先)"))
    st.caption(t("建议订货量 = max(0, ceil(月销量 × 0.5)) · 减少订货, 消化库存"))
    _render_df_with_csv(yellow, f"order_basis_yellow_{sel_month}.csv")


# ----- 🟢 正常 -----
with tab_green:
    green = df[(df["sell_through_rate"] >= 0.5) & (df["sell_through_rate"] < 0.9)].copy()
    green = green.sort_values("sell_through_rate", ascending=False, na_position="last")
    green["suggest_qty"] = green["qty_sold"].apply(lambda x: _suggest_qty(x, 1.0))
    st.subheader(t("🟢 正常 SKU (0.5 ≤ 完売率 < 0.9)"))
    st.caption(t("建议订货量 = ceil(月销量 × 1.0) · 按当月销量补"))
    _render_df_with_csv(green, f"order_basis_green_{sel_month}.csv")


st.divider()


# ============================================================
# 历史趋势 (可选 · 单 SKU 多月数据)
# ============================================================
st.subheader(t("📈 单 SKU 历史趋势 (跨月完売率)"))

trend_input = st.text_input(
    t("输入 item_code 查看跨月趋势"),
    placeholder=t("例: 01-0641-134"),
    key="trend_item_input",
)

if trend_input.strip():
    item = trend_input.strip()
    trend_df = df_all[df_all["item_code"].astype(str) == item].copy()
    if trend_df.empty:
        st.info(t(f"未找到 item_code = {item} 的历史记录"))
    else:
        # 同一 item 多 location 时, 按 year_month 聚合 (sum sold/in, 重算 rate)
        agg = (
            trend_df.groupby("year_month", as_index=False)
            .agg(
                qty_sold=("qty_sold", "sum"),
                open_qty=("open_qty", "sum"),
                qty_total_in=("qty_total_in", "sum"),
                close_qty=("close_qty", "sum"),
            )
            .sort_values("year_month")
        )
        denom = (agg["open_qty"] + agg["qty_total_in"]).replace(0, pd.NA)
        agg["sell_through_rate"] = (agg["qty_sold"] / denom).fillna(0)

        st.dataframe(
            agg.rename(columns={
                "year_month": t("月份"),
                "qty_sold": t("月销量"),
                "open_qty": t("期初"),
                "qty_total_in": t("入库"),
                "close_qty": t("期末"),
                "sell_through_rate": t("完売率"),
            }),
            use_container_width=True,
            hide_index=True,
        )

        if len(agg) >= 2:
            import altair as alt
            # 曲线图（page05 と統一: 点線 + 縦軸タイトル + 縦ルール hover）
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
            st.altair_chart(alt.layer(_ln, _rl).properties(height=300),
                            use_container_width=True)
