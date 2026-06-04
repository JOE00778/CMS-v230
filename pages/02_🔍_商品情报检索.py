"""模块 #7 商品情报检索 — 薄壳 UI（T-008）。

逻辑全在 modules/product_search/（filters/queries/export）。本文件仅：
取连接 → 收筛选 UI → 调 search_items → 表格 + CSV 导出按钮。
"""
from __future__ import annotations

import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t
from modules.product_search import (
    STOCK_ALL, STOCK_IN, STOCK_OUT, SearchFilters,
    distinct_values, search_items, to_csv_bytes,
)

st.set_page_config(page_title=t("商品情报检索"), page_icon="🔍", layout="wide")
from shared.auth import require_password  # noqa: E402
from shared.theme import inject_theme  # noqa: E402
require_password(); inject_theme(); lang_selector()
conn = get_connection()

st.title(t("🔍 商品情报检索"))
st.caption(t("按多维度筛选 SKU，看完整商品信息"))

kw = st.text_input(t("全文搜索"), placeholder=t("商品名 / 厂商"))
c1, c2, c3 = st.columns(3)
brands = c1.multiselect(t("品牌"), distinct_values(conn, "maker"))
cats = c2.multiselect(t("商品等级"), distinct_values(conn, "item_rank"))
stock = c3.selectbox(t("库存状态"), [STOCK_ALL, STOCK_IN, STOCK_OUT],
                     format_func=lambda s: {STOCK_ALL: t("全部"), STOCK_IN: t("有库存"),
                                            STOCK_OUT: t("无库存")}[s])
p1, p2, d1, d2 = st.columns(4)
pmin = p1.number_input(t("最低原价"), min_value=0.0, value=0.0) or None
pmax = p2.number_input(t("最高原价"), min_value=0.0, value=0.0) or None
cfrom = (d1.text_input(t("更新日 从"), placeholder="2026-01-01") or "").strip() or None
cto = (d2.text_input(t("更新日 到"), placeholder="2026-12-31") or "").strip() or None
try:
    f = SearchFilters(keyword=kw, brands=brands, categories=cats, price_min=pmin,
                      price_max=pmax, stock_status=stock, created_from=cfrom,
                      created_to=cto).validate()
    df = search_items(conn, f)
except ValueError as e:
    st.error(t("筛选条件非法") + f"：{e}")
    st.stop()

st.markdown(f"**{len(df):,}** {t('件')}")
st.dataframe(df, use_container_width=True, hide_index=True, height=560)
st.download_button(t("📥 导出 CSV"), to_csv_bytes(df),
                   file_name=f"product_search_{len(df)}.csv", mime="text/csv")
