"""模块 ② 运营调整建议 · 基于「毛利 × 周转」双轴矩阵 · B/C 档 SKU。

数据：operation_advice_monthly（由 modules.operation_advice.proposal.generate_advice 写入）
"""
from __future__ import annotations
import streamlit as st
from shared.i18n import t, lang_selector
from shared.i18n_columns import localize_df
import pandas as pd
import sqlite3
from pathlib import Path

from modules.operation_advice.proposal import generate_advice
from modules.operation_advice.rules import (
    MARGIN_LOW, MARGIN_HIGH, TURNOVER_LOW, TURNOVER_HIGH
)

st.set_page_config(page_title=t("运营调整建议"), page_icon="💡", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()

from shared.db import DB_PATH, get_connection
DB = DB_PATH
conn = get_connection()

st.title(t("💡 运营调整建议（B/C 档）"))

from modules.operation_advice.settings import load_thresholds
_thv = load_thresholds()
st.caption(
    t("双轴矩阵：毛利率 × 月周转率 → 5 档建议")
    + f" · 阈值 毛利{_thv['margin_low']:.0f}/{_thv['margin_high']:.0f}% · "
    + f"周转{_thv['turn_low']}/{_thv['turn_high']} · "
    + t("（阈值在「⚙️ 系统参数设定」改）")
)

# 月度选择器（动态·从 nst.sales_monthly 读可用月份，最新在前）+ 重算
_months = [r["ym"] for r in conn.execute(
    "SELECT DISTINCT year_month AS ym FROM nst.sales_monthly ORDER BY year_month DESC"
).fetchall()]
if not _months:
    _months = ["2026-04"]
col_ym, col_recalc = st.columns([2, 1])
with col_ym:
    ym = st.selectbox(t("月度"), _months, index=0)
with col_recalc:
    if st.button(t("🔄 重新计算")):
        with st.spinner(t("生成中...")):
            generate_advice(ym, str(DB))
        st.success(t("✅ 已更新"))
        st.rerun()

# 数据加载 — ym 白名单防御（仅 [0-9-]）
import re as _re
_safe_ym = _re.sub(r"[^0-9-]", "", str(ym))[:10]
try:
    cur = conn.execute(
        f"SELECT * FROM operation_advice_monthly WHERE year_month = '{_safe_ym}'"
    )
    cols = [d[0] for d in cur.description] if cur.description else []
    df = pd.DataFrame([dict(zip(cols, r)) if not hasattr(r, "keys") else dict(r)
                       for r in cur.fetchall()])
except Exception as _err:
    st.error(f"❌ 加载 operation_advice_monthly 失败: {_err}")
    st.stop()

if df.empty:
    st.warning(t("⚠️ 暂无数据。请先点【🔄 重新计算】。"))
    st.stop()

# KPI 卡片
st.markdown(t("### 总览"))
c1, c2, c3, c4, c5, c6 = st.columns(6)
counts = df["advice"].value_counts()
c1.metric(t("🔥 重点提价"), int(counts.get("🔥 重点提价", 0)))
c2.metric(t("🔥 重点降价"), int(counts.get("🔥 重点降价", 0)))
c3.metric(t("⬆️ 提价候选"), int(counts.get("⬆️ 提价候选", 0)))
c4.metric(t("⚠️ 降价候选"), int(counts.get("⚠️ 降价候选", 0)))
c5.metric(t("⬇️ 降级候选"), int(counts.get("⬇️ 降级候选", 0)))
c6.metric(t("✅ 维持"), int(counts.get("✅ 维持", 0)))

st.divider()

# 等级 × 建议矩阵
st.markdown(t("### 等级 × 建议 矩阵"))
matrix = pd.crosstab(df["rank"], df["advice"], margins=True, margins_name=t("合计"))
st.dataframe(localize_df(matrix), use_container_width=True)

st.divider()

# 4 个清单 tab
tabs = st.tabs([
    t("🔥 重点降价"),
    t("🔥 重点提价"),
    t("⬇️ 降级候选"),
    t("📋 全部建议"),
])

display_cols = [
    "sku", "name", "rank", "margin_pct", "monthly_turnover",
    "inventory_value", "advice", "reason",
]
display_names = {
    "sku": "SKU",
    "name": "商品名",
    "rank": "等级",
    "margin_pct": "毛利%",
    "monthly_turnover": "月周转",
    "inventory_value": "库存价值",
    "advice": "建议",
    "reason": "理由",
}


def _show(filtered_df: pd.DataFrame, top_n: int = 100):
    if filtered_df.empty:
        st.info(t("无数据"))
        return
    # join name from inventory (用 conn.execute 走 PG adapter, 不依赖 pandas)
    _inv_rows = conn.execute(
        "SELECT item_code AS sku, MIN(display_name) AS name "
        "FROM nst.item_master_raw WHERE item_code IS NOT NULL "
        "GROUP BY item_code"
    ).fetchall()
    inv = pd.DataFrame([dict(r) for r in _inv_rows])
    merged = filtered_df.merge(inv, on="sku", how="left")
    merged = merged.sort_values("inventory_value", ascending=False).head(top_n)
    show = merged[[c for c in display_cols if c in merged.columns]].rename(
        columns=display_names
    )
    st.dataframe(localize_df(show), use_container_width=True, height=500)
    st.caption(t(f"显示前 {min(len(show), top_n)} 行 / 共 {len(filtered_df)} 条"))


with tabs[0]:
    st.markdown(t("**周转低 × 毛利高 — 降价加速周转 / 库存价值降序**"))
    _show(df[df["advice"] == "🔥 重点降价"])

with tabs[1]:
    st.markdown(t("**周转高 × 毛利低 — 提价不影响销量 / 即时增毛利**"))
    _show(df[df["advice"] == "🔥 重点提价"])

with tabs[2]:
    st.markdown(t("**周转低 × 毛利低 — 双低 · 等级下调候选（B→C / C→停售）**"))
    st.caption(t("注：与改廃情報（page 13）不同 · 改廃 = 品牌方迭代外部信号 · 此处为内部数据驱动的渐变降级"))
    _show(df[df["advice"] == "⬇️ 降级候选"])

with tabs[3]:
    st.markdown(f"**{t('全部建议')}** · {len(df):,}")
    advice_filter = st.multiselect(
        t("建议筛选"),
        options=df["advice"].unique().tolist(),
        default=df["advice"].unique().tolist(),
    )
    rank_filter = st.multiselect(
        t("等级筛选"),
        options=df["rank"].unique().tolist(),
        default=df["rank"].unique().tolist(),
    )
    view = df[df["advice"].isin(advice_filter) & df["rank"].isin(rank_filter)]
    _show(view, top_n=500)

conn.close()
