"""模块 #20 价格波动分析 · 进货价(PO rate) / 定义原价(cost_estimate) 两类价格的 SKU 级波动.

数据源:
- 进货价格波动: nst.purchase_order_line（発注時 rate · trandate）+ nst.item_master_raw
- 定义原价波动: nst.cost_history（cost_estimate 变化时 INSERT）+ nst.item_master_raw

业务（两 tab 共用 _render_volatility）:
- 按 SKU 算 变更次数 / 当前价 / 历史 min·max / 波动幅度(max-min) / 波动率((max-min)/min)
- 4 档分级 🔴≥30% / 🟠10-30% / 🟡<10% / ➖无变化
- KPI 卡片 + 4 档分布饼图 + Top20 + 筛选列表 + 单 SKU 折线下钻
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from shared.db import get_connection
from shared.i18n_columns import localize_df
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("价格波动分析"), page_icon="📊", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📊 价格波动分析"))
st.caption(t("SKU 级价格历史趋势 · 4 档波动分级 · 重点关注 🔴 大波动 SKU"))


def _load(sql: str) -> pd.DataFrame:
    from shared.cache import cached_df, data_version
    return cached_df(conn, sql, ver=data_version())


def _pct_color(v):
    """进货价增幅着色：涨=红（成本上升·警示）/ 降=绿（成本下降·利好）。"""
    if pd.isna(v):
        return ""
    if v > 0:
        return "color:#DC2626"  # 涨价=红
    if v < 0:
        return "color:#16A34A"  # 降价=绿
    return ""


def _pct_fmt(v) -> str:
    return f"{v:+.0f}%" if pd.notna(v) else "—"


def _render_volatility(df: pd.DataFrame, *, value_label: str, key_prefix: str,
                       empty_msg: str, show_latest: bool = False) -> None:
    """通用价格波动分析。df 列: internal_id / item_code / display_name / changed_at / std_cost_new。"""
    if df.empty:
        st.info(empty_msg)
        return

    df = df.copy()
    df["changed_at"] = pd.to_datetime(df["changed_at"], errors="coerce")
    df["std_cost_new"] = pd.to_numeric(df["std_cost_new"], errors="coerce")
    df = df.dropna(subset=["std_cost_new"])
    df = df.sort_values(["internal_id", "changed_at"])
    # 旧価 = 同一 SKU の前回 値（shift で算出）
    df["std_cost_old"] = df.groupby("internal_id")["std_cost_new"].shift(1)
    df["id"] = range(len(df))

    agg = df.groupby("internal_id", as_index=False).agg(
        item_code=("item_code", "last"),
        display_name=("display_name", "last"),
        n_changes=("id", "count"),
        cost_min=("std_cost_new", "min"),
        cost_max=("std_cost_new", "max"),
        cost_current=("std_cost_new", "last"),
        cost_prev=("std_cost_old", "last"),
        last_changed_at=("changed_at", "max"),
        first_changed_at=("changed_at", "min"),
    )
    agg["amplitude"] = agg["cost_max"] - agg["cost_min"]
    agg["amp_pct"] = (agg["amplitude"] / agg["cost_min"].replace({0: pd.NA}) * 100).fillna(0).astype(float)
    # 最近一次 vs 上次 进货价 增幅%
    agg["latest_pct"] = ((agg["cost_current"] - agg["cost_prev"])
                         / agg["cost_prev"].replace({0: pd.NA}) * 100)

    grade_col = t("波动等级")

    def _grade(row) -> str:
        if row["n_changes"] <= 1 or row["amp_pct"] < 0.05:
            return t("➖ 无变化")
        p = row["amp_pct"]
        if p >= 30:
            return t("🔴 大波动")
        if p >= 10:
            return t("🟠 中波动")
        return t("🟡 小波动")

    agg[grade_col] = agg.apply(_grade, axis=1)

    # KPI は全量基準· 饼图/Top20/列表/下钻は toggle で無変化を除外
    agg_all = agg.copy()
    hide_unchanged = st.toggle(t("隐藏无变化 SKU（仅看有波动）"), value=True,
                               key=f"{key_prefix}_hide")
    if hide_unchanged:
        agg = agg[agg[grade_col] != t("➖ 无变化")].copy()

    g_counts = agg_all[grade_col].value_counts()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(t("SKU 总数"), len(agg_all))
    c2.metric(t("🔴 大波动"), int(g_counts.get(t("🔴 大波动"), 0)))
    c3.metric(t("🟠 中波动"), int(g_counts.get(t("🟠 中波动"), 0)))
    c4.metric(t("🟡 小波动"), int(g_counts.get(t("🟡 小波动"), 0)))
    c5.metric(t("➖ 无变化"), int(g_counts.get(t("➖ 无变化"), 0)))

    st.divider()

    left, right = st.columns([1, 1.3])
    with left:
        st.subheader(t("📊 波动等级分布"))
        dist = agg[grade_col].value_counts().reset_index()
        dist.columns = [grade_col, t("SKU 数")]
        fig_pie = px.pie(
            dist, values=t("SKU 数"), names=grade_col, hole=0.4,
            color_discrete_map={
                t("🔴 大波动"): "#dc2626",
                t("🟠 中波动"): "#f59e0b",
                t("🟡 小波动"): "#eab308",
                t("➖ 无变化"): "#9ca3af",
            },
        )
        fig_pie.update_traces(textposition="inside", textinfo="percent+label")
        st.plotly_chart(fig_pie, use_container_width=True, key=f"{key_prefix}_pie")
    with right:
        st.subheader(t("🏆 波动 Top 20"))
        top = agg.sort_values("amp_pct", ascending=False).head(20).copy()
        top["amp_pct_fmt"] = top["amp_pct"].map(lambda x: f"{x:.0f}%")
        for _c in ("cost_min", "cost_max", "cost_current", "cost_prev"):
            top[_c] = top[_c].round(0).astype("Int64")
        _cols = ["item_code", "display_name", grade_col, "n_changes", "cost_current"]
        _ren = {
            "item_code": t("商品代码"), "display_name": t("商品名"),
            "n_changes": t("变更次数"), "cost_current": t("当前价"),
        }
        if show_latest:
            _cols += ["cost_prev", "latest_pct"]
            _ren["cost_current"] = t("最近进货价")
            _ren["cost_prev"] = t("上次进货价")
            _ren["latest_pct"] = t("最近增幅%")
        _cols += ["amp_pct_fmt", "cost_min", "cost_max"]
        _ren["amp_pct_fmt"] = t("波动率")
        _ren["cost_min"] = t("历史最低")
        _ren["cost_max"] = t("历史最高")
        top_show = top[_cols].rename(columns=_ren)
        if show_latest:
            st.dataframe(
                top_show.style.map(_pct_color, subset=[t("最近增幅%")])
                .format({t("最近增幅%"): _pct_fmt}),
                use_container_width=True, hide_index=True, height=420)
        else:
            st.dataframe(localize_df(top_show), use_container_width=True, hide_index=True, height=420)

    st.divider()

    st.subheader(t("📋 全部 SKU"))
    fc1, fc2 = st.columns([2, 1])
    with fc1:
        grades = [t("🔴 大波动"), t("🟠 中波动"), t("🟡 小波动"), t("➖ 无变化")]
        sel_grades = st.multiselect(
            t("波动等级筛选"), grades,
            default=[t("🔴 大波动"), t("🟠 中波动")],
            key=f"{key_prefix}_grades",
        )
    with fc2:
        kw = st.text_input(t("搜索: 商品代码 / 商品名"), "", key=f"{key_prefix}_kw")

    view = agg.copy()
    if sel_grades:
        view = view[view[grade_col].isin(sel_grades)]
    if kw.strip():
        cond = (
            view["item_code"].astype(str).str.contains(kw.strip(), na=False)
            | view["display_name"].astype(str).str.contains(kw.strip(), na=False)
        )
        view = view[cond]

    view = view.sort_values("amp_pct", ascending=False).copy()
    for _c in ("cost_min", "cost_max", "cost_current", "cost_prev"):
        view[_c] = view[_c].round(0).astype("Int64")
    _vcols = ["item_code", "display_name", grade_col, "n_changes", "cost_current"]
    _vren = {
        "item_code": t("商品代码"), "display_name": t("商品名"),
        "n_changes": t("变更次数"), "cost_current": t("当前价"),
    }
    if show_latest:
        _vcols += ["cost_prev", "latest_pct"]
        _vren["cost_current"] = t("最近进货价")
        _vren["cost_prev"] = t("上次进货价")
        _vren["latest_pct"] = t("最近增幅%")
    _vcols += ["amp_pct", "cost_min", "cost_max"]
    _vren["amp_pct"] = t("波动率(%)")
    _vren["cost_min"] = t("历史最低")
    _vren["cost_max"] = t("历史最高")
    view_show = view[_vcols].rename(columns=_vren)
    view_show[t("波动率(%)")] = view_show[t("波动率(%)")].map(lambda x: f"{x:.0f}")
    if show_latest:
        st.dataframe(
            view_show.style.map(_pct_color, subset=[t("最近增幅%")])
            .format({t("最近增幅%"): _pct_fmt}),
            use_container_width=True, hide_index=True, height=400)
    else:
        st.dataframe(localize_df(view_show), use_container_width=True, hide_index=True, height=400)
    st.caption(t("显示 {n} / 共 {total} 个 SKU").format(n=len(view), total=len(agg_all)))

    st.divider()

    st.subheader(t("🔍 单 SKU 趋势下钻"))
    candidates = view if not view.empty else agg
    candidates = candidates.sort_values("amp_pct", ascending=False)
    options = candidates.apply(
        lambda r: f"{r['item_code']} · {r['display_name']} · {r[grade_col]}", axis=1
    ).tolist()
    id_map = dict(zip(options, candidates["internal_id"].tolist()))

    if options:
        sel = st.selectbox(t("选择 SKU"), options, key=f"{key_prefix}_drill")
        sel_id = id_map[sel]
        sub = df[df["internal_id"] == sel_id].sort_values("changed_at").copy()

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
            yaxis_title=value_label,
            margin=dict(l=10, r=10, t=10, b=10),
            legend=dict(orientation="h", y=1.1),
        )
        fig.update_yaxes(tickformat=",.0f")  # y 轴取整
        st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_line")

        sub = sub.copy()
        sub["diff"] = sub["std_cost_new"] - sub["std_cost_old"]
        sub["diff_pct"] = sub["diff"] / sub["std_cost_old"].replace({0: pd.NA})
        sub_show = sub[[
            "changed_at", "std_cost_old", "std_cost_new", "diff", "diff_pct",
        ]].copy()
        for _c in ("std_cost_old", "std_cost_new", "diff"):
            sub_show[_c] = sub_show[_c].round(0).astype("Int64")
        sub_show["diff_pct"] = sub_show["diff_pct"].map(
            lambda x: f"{x:+.0%}" if pd.notna(x) else ""
        )
        sub_show.columns = [
            t("变更时间"), t("旧价"), t("新价"), t("差额"), t("差额率"),
        ]
        st.dataframe(localize_df(sub_show), use_container_width=True, hide_index=True)
    else:
        st.info(t("当前过滤条件下无 SKU。"))


# ── 数据源 SQL ──
_SQL_COST = (
    "SELECT ch.internal_id, im.item_code, im.jan, im.display_name, "
    "ch.effective_date AS changed_at, ch.cost_estimate AS std_cost_new "
    "FROM nst.cost_history ch "
    "LEFT JOIN nst.item_master_raw im ON im.internal_id = ch.internal_id "
    "WHERE ch.cost_estimate IS NOT NULL "
    "ORDER BY ch.internal_id, ch.effective_date"
)
_SQL_PO = (
    "SELECT pol.item_internal_id AS internal_id, im.item_code, im.jan, im.display_name, "
    "pol.trandate AS changed_at, pol.rate AS std_cost_new "
    "FROM nst.purchase_order_line pol "
    # INNER JOIN：主档未収録（已停售/历史旧商品）直接忽略，只看有商品名的
    "JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id "
    "WHERE pol.rate IS NOT NULL AND pol.rate > 0 AND pol.trandate IS NOT NULL "
    "ORDER BY pol.item_internal_id, pol.trandate"
)

tab_po, tab_cost = st.tabs([t("🛒 进货价格波动"), t("📐 定义原价波动")])

with tab_po:
    _render_volatility(
        _load(_SQL_PO), value_label=t("进货价 (¥)"), key_prefix="po",
        empty_msg=t("暂无进货价（PO）历史。nst.purchase_order_line 取得后显示。"),
        show_latest=True,
    )

with tab_cost:
    _render_volatility(
        _load(_SQL_COST), value_label=t("定义原价 (¥)"), key_prefix="cost",
        empty_msg=t("暂无定义原价变更历史。NST cost_history 累积 cost_estimate 变化后显示。"),
    )

conn.close()
