"""模块 #20 定义原价波动图 · SKU 级 std_cost 历史趋势 + 波动分级.

数据源: nst.cost_history（NST API 毎日拉取 · cost_estimate=定義原価 が変化した時に INSERT）
        + nst.item_master_raw（item_code / 表示名）

业务:
- 按 SKU 算 总变更次数 / 当前价 / 历史 min·max / 波动幅度（max-min）/ 波动率（(max-min)/min）
- 4 档分级: 🔴 大波动（≥30%）/ 🟠 中（10-30%）/ 🟡 小（<10%）/ ➖ 无变更（仅 1 次）
- KPI 卡片 + 4 档分布饼图 + 排序列表 + 单 SKU 折线图下钻
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from shared.db import get_connection
from shared.i18n_columns import localize_df
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("定义原价波动图"), page_icon="📈", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📈 定义原价波动图"))
st.caption(t("SKU 级 std_cost 历史趋势 · 4 档波动分级 · 重点关注 🔴 大波动 SKU"))

df = pd.DataFrame([
    dict(r) for r in conn.execute(
        "SELECT ch.internal_id, im.item_code, im.jan, im.display_name, "
        "ch.effective_date AS changed_at, ch.cost_estimate AS std_cost_new "
        "FROM nst.cost_history ch "
        "LEFT JOIN nst.item_master_raw im ON im.internal_id = ch.internal_id "
        "WHERE ch.cost_estimate IS NOT NULL "
        "ORDER BY ch.internal_id, ch.effective_date"
    ).fetchall()
])

if df.empty:
    st.info(t("暂无定义原价变更历史。NST cost_history 累积 cost_estimate 变化后显示（每日拉取自动检测）。"))
    st.stop()

df["changed_at"] = pd.to_datetime(df["changed_at"], errors="coerce")
df["std_cost_new"] = pd.to_numeric(df["std_cost_new"], errors="coerce")
df = df.sort_values(["internal_id", "changed_at"])
# 旧価 = 同一 SKU の前回 effective_date の cost_estimate（NST は old を持たないため shift で算出）
df["std_cost_old"] = df.groupby("internal_id")["std_cost_new"].shift(1)
df["id"] = range(len(df))

# 按 SKU 聚合
agg = df.groupby("internal_id", as_index=False).agg(
    item_code=("item_code", "last"),
    display_name=("display_name", "last"),
    n_changes=("id", "count"),
    cost_min=("std_cost_new", "min"),
    cost_max=("std_cost_new", "max"),
    cost_current=("std_cost_new", "last"),
    last_changed_at=("changed_at", "max"),
    first_changed_at=("changed_at", "min"),
)
agg["amplitude"] = agg["cost_max"] - agg["cost_min"]
agg["amp_pct"] = (agg["amplitude"] / agg["cost_min"].replace({0: pd.NA}) * 100).fillna(0).astype(float)


def _grade(row) -> str:
    if row["n_changes"] <= 1:
        return t("➖ 无变化")
    p = row["amp_pct"]
    if p >= 30:
        return t("🔴 大波动")
    if p >= 10:
        return t("🟠 中波动")
    return t("🟡 小波动")


agg[t("波动等级")] = agg.apply(_grade, axis=1)

# KPI は全量基準（無変化数も把握）· 饼图/Top20/列表/下钻は toggle で無変化を除外
agg_all = agg.copy()
hide_unchanged = st.toggle(t("隐藏无变化 SKU（仅看有波动）"), value=True)
if hide_unchanged:
    agg = agg[agg[t("波动等级")] != t("➖ 无变化")].copy()

# KPI 卡片（全量）
g_counts = agg_all[t("波动等级")].value_counts()
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric(t("SKU 总数"), len(agg_all))
c2.metric(t("🔴 大波动"), int(g_counts.get(t("🔴 大波动"), 0)))
c3.metric(t("🟠 中波动"), int(g_counts.get(t("🟠 中波动"), 0)))
c4.metric(t("🟡 小波动"), int(g_counts.get(t("🟡 小波动"), 0)))
c5.metric(t("➖ 无变化"), int(g_counts.get(t("➖ 无变化"), 0)))

st.divider()

# 分布饼图
left, right = st.columns([1, 1.3])

with left:
    st.subheader(t("📊 波动等级分布"))
    dist = agg[t("波动等级")].value_counts().reset_index()
    dist.columns = [t("波动等级"), t("SKU 数")]
    fig_pie = px.pie(
        dist, values=t("SKU 数"), names=t("波动等级"), hole=0.4,
        color_discrete_map={
            t("🔴 大波动"): "#dc2626",
            t("🟠 中波动"): "#f59e0b",
            t("🟡 小波动"): "#eab308",
            t("➖ 无变化"): "#9ca3af",
        },
    )
    fig_pie.update_traces(textposition="inside", textinfo="percent+label")
    st.plotly_chart(fig_pie, use_container_width=True)

with right:
    st.subheader(t("🏆 波动 Top 20"))
    top = agg.sort_values("amp_pct", ascending=False).head(20).copy()
    top["amp_pct_fmt"] = top["amp_pct"].map(lambda x: f"{x:.1f}%")
    show_cols = ["item_code", "display_name", t("波动等级"), "n_changes",
                 "cost_min", "cost_max", "cost_current", "amp_pct_fmt"]
    top_show = top[show_cols].rename(columns={
        "item_code": t("商品代码"),
        "display_name": t("商品名"),
        "n_changes": t("变更次数"),
        "cost_min": t("历史最低"),
        "cost_max": t("历史最高"),
        "cost_current": t("当前价"),
        "amp_pct_fmt": t("波动率"),
    })
    st.dataframe(localize_df(top_show), use_container_width=True, hide_index=True, height=420)

st.divider()

# 过滤 + 完整列表
st.subheader(t("📋 全部 SKU"))
fc1, fc2 = st.columns([2, 1])
with fc1:
    grades = [t("🔴 大波动"), t("🟠 中波动"), t("🟡 小波动"), t("➖ 无变化")]
    sel_grades = st.multiselect(
        t("波动等级筛选"), grades,
        default=[t("🔴 大波动"), t("🟠 中波动")],
    )
with fc2:
    kw = st.text_input(t("搜索: 商品代码 / 商品名"), "")

view = agg.copy()
if sel_grades:
    view = view[view[t("波动等级")].isin(sel_grades)]
if kw.strip():
    cond = (
        view["item_code"].astype(str).str.contains(kw.strip(), na=False)
        | view["display_name"].astype(str).str.contains(kw.strip(), na=False)
    )
    view = view[cond]

view = view.sort_values("amp_pct", ascending=False)
view_show = view[[
    "item_code", "display_name", t("波动等级"),
    "n_changes", "cost_min", "cost_max", "cost_current", "amp_pct",
]].rename(columns={
    "item_code": t("商品代码"),
    "display_name": t("商品名"),
    "n_changes": t("变更次数"),
    "cost_min": t("历史最低"),
    "cost_max": t("历史最高"),
    "cost_current": t("当前价"),
    "amp_pct": t("波动率(%)"),
})
view_show[t("波动率(%)")] = view_show[t("波动率(%)")].map(lambda x: f"{x:.1f}")
st.dataframe(localize_df(view_show), use_container_width=True, hide_index=True, height=400)
st.caption(t(f"显示 {len(view)} / 共 {len(agg_all)} 个 SKU"))

st.divider()

# 单 SKU 趋势下钻
st.subheader(t("🔍 单 SKU 趋势下钻"))

candidates = view if not view.empty else agg
candidates = candidates.sort_values("amp_pct", ascending=False)
options = candidates.apply(
    lambda r: f"{r['item_code']} · {r['display_name']} · {r[t('波动等级')]}", axis=1
).tolist()
id_map = dict(zip(options, candidates["internal_id"].tolist()))

if options:
    sel = st.selectbox(t("选择 SKU"), options, key="cost_drill")
    sel_id = id_map[sel]
    sub = df[df["internal_id"] == sel_id].sort_values("changed_at").copy()

    # 折线图（含 old/new 价对比）
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=sub["changed_at"], y=sub["std_cost_new"],
        mode="lines+markers", name=t("新价"),
        line=dict(color="#2563eb", width=2),
        marker=dict(size=8),
    ))
    if sub["std_cost_old"].notna().any():
        fig.add_trace(go.Scatter(
            x=sub["changed_at"], y=sub["std_cost_old"],
            mode="markers", name=t("旧价"),
            marker=dict(size=6, color="#9ca3af", symbol="x"),
        ))
    fig.update_layout(
        height=400,
        xaxis_title=t("变更时间"),
        yaxis_title=t("std_cost (¥)"),
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", y=1.1),
    )
    st.plotly_chart(fig, use_container_width=True)

    # 明细表（NST cost_history は source/変更人/備考 を持たないため diff を算出表示）
    sub = sub.copy()
    sub["diff"] = sub["std_cost_new"] - sub["std_cost_old"]
    sub["diff_pct"] = sub["diff"] / sub["std_cost_old"].replace({0: pd.NA})
    sub_show = sub[[
        "changed_at", "std_cost_old", "std_cost_new", "diff", "diff_pct",
    ]].copy()
    sub_show["diff_pct"] = sub_show["diff_pct"].map(
        lambda x: f"{x:+.2%}" if pd.notna(x) else ""
    )
    sub_show.columns = [
        t("变更时间"), t("旧价"), t("新价"), t("差额"), t("差额率"),
    ]
    st.dataframe(localize_df(sub_show), use_container_width=True, hide_index=True)
else:
    st.info(t("当前过滤条件下无 SKU。"))
