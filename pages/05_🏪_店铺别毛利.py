"""模块 #6 店铺毛利 · NST API 売上データ（nst.sales_daily）ベース.

2026-05-20 全面改写：旧 sales_line（手動 Excel 時代）→ NST API 日次データへ。
店舗 × 月 の粗利を集計し、月内の日次推移を曲線で可視化（Boss 2026-05-20 依頼）。

データ源:
- nst.sales_daily      店舗 × SKU × 日 の売上（販売数量 / 総収益 JPY）
- nst.item_master_raw  商品マスタ（定義原価 cost_estimate / 表示名 / メーカー / ランク）

表示:
- 月選択 + 市場フィルタ
- 月内日次の粗利推移（曲線図）+ 店舗別 / 市場別 / TOP SKU 集計
- 粗利 = 総収益 − 定義原価(=cost_estimate×数量) / 粗利率 = 粗利/総収益
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t, get_lang
from shared.markets import ALL_MARKETS, add_market_column

st.set_page_config(page_title=t("店铺毛利"), page_icon="🏪", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("🏪 店铺毛利"))
st.caption(t(
    "NST API 売上データ（日次）· 店舗×月の粗利集計 + 月内日次推移 曲線 · "
    "粗利/粗利率 自動計算（定義原価ベース）"
))

# 列見出し: (中文, 日本語) — UI 言語追従
_LBL = {
    "shop":          ("店铺", "FB_店舗"),
    "market":        ("市场", "市場"),
    "sale_date":     ("日期", "日付"),
    "display_name":  ("显示名", "表示名"),
    "maker":         ("厂商", "メーカー名"),
    "item_rank":     ("商品等级", "商品ランク"),
    "qty":           ("销售数量", "販売数量"),
    "revenue":       ("总收益", "総収益"),
    "defined_cost":  ("定义原价", "定義原価"),
    "gross_profit":  ("毛利", "粗利"),
    "gross_margin":  ("毛利率", "粗利率"),
    "n_shop":        ("店铺数", "店舗数"),
    "n_sku":         ("SKU数", "SKU数"),
}

_MONEY = {"revenue", "defined_cost", "gross_profit"}
_PCT = {"gross_margin"}
_INT = {"qty", "n_shop", "n_sku"}


def _col(key: str) -> str:
    return _LBL[key][1] if get_lang() == "ja" else _LBL[key][0]


def _disp(g: pd.DataFrame, cols: tuple) -> pd.DataFrame:
    """聚合 DataFrame → 双语列名 + 金额/比率/整数格式化（表示専用）。"""
    d = g[list(cols)].copy()
    for c in cols:
        if c in _MONEY:
            d[c] = d[c].apply(lambda x: f"¥{x:,.0f}")
        elif c in _PCT:
            d[c] = d[c].apply(lambda x: f"{x:.2f}%")
        elif c in _INT:
            d[c] = d[c].apply(lambda x: f"{int(x):,}")
    d.columns = [_col(c) for c in cols]
    return d


def _query(sql: str, params: tuple = ()):
    try:
        cur = conn.execute(sql, params) if params else conn.execute(sql)
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description] if cur.description else []
        return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols), None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, str(e)


# ============================================================
# データ有無チェック
# ============================================================
months_df, err = _query(
    "SELECT DISTINCT to_char(sale_date,'YYYY-MM') AS ym "
    "FROM nst.sales_daily ORDER BY ym DESC"
)
if err:
    st.error(t("売上テーブル未取得 or 接続エラー: ") + err)
    st.info(t("page 27「📥 NST 取得データ」→ 手動更新 で sales を実行してください。"))
    st.stop()
if months_df is None or months_df.empty:
    st.warning(t("⚠️ 日次売上データ未取得。page 27「📥 NST 取得データ」で sales ジョブを実行してください。"))
    st.stop()

# ============================================================
# フィルタ
# ============================================================
c1, c2 = st.columns([1, 2])
ym = c1.selectbox(t("対象月"), months_df["ym"].tolist())
mk = c2.selectbox(t("市場"), [t("全部市场")] + ALL_MARKETS)

# ============================================================
# クエリ（当月の日次明細 + 商品マスタ join）
# ============================================================
df, e2 = _query(
    "SELECT s.shop, s.sale_date, s.item_internal_id, "
    "im.display_name, im.maker, im.item_rank, "
    "s.qty_sold, s.revenue, "
    "(COALESCE(im.cost_estimate,0)*s.qty_sold) AS defined_cost "
    "FROM nst.sales_daily s "
    "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
    "WHERE to_char(s.sale_date,'YYYY-MM') = ? "
    "ORDER BY s.sale_date",
    (ym,),
)
if e2:
    st.error(e2)
    st.stop()
if df is None or df.empty:
    st.info(t("この条件のデータがありません"))
    st.stop()

df["qty_sold"] = df["qty_sold"].astype(float)
df["revenue"] = df["revenue"].astype(float)
df["defined_cost"] = df["defined_cost"].astype(float)
df["gross_profit"] = df["revenue"] - df["defined_cost"]
df = add_market_column(df, store_col="shop")

if mk != t("全部市场"):
    df = df[df["market"] == mk]
    if df.empty:
        st.info(t("この市場のデータがありません"))
        st.stop()

# ============================================================
# KPI（総）
# ============================================================
tot_q = df["qty_sold"].sum()
tot_r = df["revenue"].sum()
tot_c = df["defined_cost"].sum()
tot_g = df["gross_profit"].sum()
margin = (tot_g / tot_r * 100) if tot_r else 0

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric(t("販売数量 計"), f"{tot_q:,.0f}")
m2.metric(t("総収益 計"), f"¥{tot_r:,.0f}")
m3.metric(t("定義原価 計"), f"¥{tot_c:,.0f}")
m4.metric(t("粗利 計"), f"¥{tot_g:,.0f}")
m5.metric(t("粗利率"), f"{margin:.2f}%")

st.divider()

tab_day, tab_shop, tab_market, tab_sku = st.tabs(
    [t("📈 月内日次推移"), t("🏪 店舗別"), t("🌐 市場別"), t("🏆 TOP SKU")]
)

# ============================================================
# Tab 0：月内日次推移（曲線図）← Boss 依頼の核心
# ============================================================
with tab_day:
    daily = df.groupby("sale_date", as_index=False).agg(
        qty=("qty_sold", "sum"),
        revenue=("revenue", "sum"),
        defined_cost=("defined_cost", "sum"),
        gross_profit=("gross_profit", "sum"),
        n_shop=("shop", "nunique"),
        n_sku=("item_internal_id", "nunique"),
    ).sort_values("sale_date")
    daily["gross_margin"] = (
        daily["gross_profit"] / daily["revenue"].where(daily["revenue"] != 0)
    ).fillna(0) * 100

    # 曲線図：日次 総収益 / 粗利
    line = daily.set_index("sale_date")[["revenue", "gross_profit"]].copy()
    line.columns = [_col("revenue"), _col("gross_profit")]
    st.line_chart(line, use_container_width=True, height=320)

    # 销量条形图
    bar = daily.set_index("sale_date")[["qty"]].copy()
    bar.columns = [_col("qty")]
    st.bar_chart(bar, use_container_width=True, height=220)

    # 明细表
    day_cols = ("sale_date", "qty", "revenue", "defined_cost",
                "gross_profit", "gross_margin", "n_shop", "n_sku")
    st.dataframe(_disp(daily, day_cols), use_container_width=True, hide_index=True)

# ============================================================
# Tab 1：店舗別
# ============================================================
with tab_shop:
    g = df.groupby("shop", as_index=False).agg(
        qty=("qty_sold", "sum"),
        revenue=("revenue", "sum"),
        defined_cost=("defined_cost", "sum"),
        gross_profit=("gross_profit", "sum"),
        n_sku=("item_internal_id", "nunique"),
    )
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = g.sort_values("gross_profit", ascending=False)

    shop_cols = ("shop", "qty", "revenue", "defined_cost",
                 "gross_profit", "gross_margin", "n_sku")
    st.dataframe(_disp(g, shop_cols), use_container_width=True, hide_index=True)
    chart = g.set_index("shop")[["gross_profit"]].copy()
    chart.columns = [_col("gross_profit")]
    st.bar_chart(chart, horizontal=True, use_container_width=True)

# ============================================================
# Tab 2：市場別
# ============================================================
with tab_market:
    g = df.groupby("market", as_index=False).agg(
        qty=("qty_sold", "sum"),
        revenue=("revenue", "sum"),
        defined_cost=("defined_cost", "sum"),
        gross_profit=("gross_profit", "sum"),
        n_shop=("shop", "nunique"),
        n_sku=("item_internal_id", "nunique"),
    )
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = g.sort_values("gross_profit", ascending=False)

    mkt_cols = ("market", "qty", "revenue", "defined_cost",
                "gross_profit", "gross_margin", "n_shop", "n_sku")
    st.dataframe(_disp(g, mkt_cols), use_container_width=True, hide_index=True)
    chart = g.set_index("market")[["gross_profit"]].copy()
    chart.columns = [_col("gross_profit")]
    st.bar_chart(chart, horizontal=True, use_container_width=True)

# ============================================================
# Tab 3：TOP SKU
# ============================================================
with tab_sku:
    n_top = st.slider(t("Top N"), 10, 100, 30, 10)
    g = df.groupby(["item_internal_id", "display_name", "maker", "item_rank"],
                   as_index=False, dropna=False).agg(
        qty=("qty_sold", "sum"),
        revenue=("revenue", "sum"),
        gross_profit=("gross_profit", "sum"),
    )
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = g.sort_values("gross_profit", ascending=False).head(n_top)

    sku_cols = ("display_name", "maker", "item_rank", "qty",
                "revenue", "gross_profit", "gross_margin")
    st.dataframe(_disp(g, sku_cols), use_container_width=True, hide_index=True)

st.divider()
st.caption(
    t("対象月") + f"：{ym} · " + t("市場") + f"：{mk} · "
    + t("表示行（明細）: ") + f"{len(df):,}"
)
