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

import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t, get_lang
from shared.markets import ALL_MARKETS, add_market_column
from shared.owners import OWNER_EXCLUDED, add_owner_column

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
    "owner":         ("店铺负责人", "担当者"),
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
# 未来日付（NST に未来 dated の取引が混入）を除外：今日(JST)まで
_today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
df = df[df["sale_date"] <= _today]
df = add_market_column(df, store_col="shop")
df = add_owner_column(df, shop_col="shop")

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

_owner_tab = "👤 担当者別" if get_lang() == "ja" else "👤 店铺负责人"
tab_day, tab_owner, tab_shop, tab_market, tab_sku = st.tabs(
    [t("📈 月内日次推移"), _owner_tab, t("🏪 店舗別"), t("🌐 市場別"), t("🏆 TOP SKU")]
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

    # 前日データ = 昨日(今日-1)の実績。当月以外/欠測時は「昨日以前の最新日」に回退
    _yest = _today - dt.timedelta(days=1)
    _elig = daily[daily["sale_date"] <= _yest]
    _src = _elig if not _elig.empty else daily
    _last = _src.iloc[-1]
    _prev = _src.iloc[-2] if len(_src) >= 2 else None
    _ld = _last["sale_date"]
    _hdr = "前日データ" if get_lang() == "ja" else "前日数据"
    st.markdown(f"**{_hdr}** · {_ld.month}月{_ld.day}日")
    if _prev is not None:
        _pdt = _prev["sale_date"]
        st.caption(
            f"増減は前々日（{_pdt.month}月{_pdt.day}日）比"
            if get_lang() == "ja"
            else f"涨跌为环比前前日（{_pdt.month}月{_pdt.day}日）"
        )

    def _delta(key, pct=False):
        if _prev is None:
            return None
        diff = float(_last[key]) - float(_prev[key])
        return f"{diff:+.2f}pt" if pct else f"{diff:+,.0f}"

    pq, pr, pg, pm = st.columns(4)
    pq.metric(_col("qty"), f"{int(_last['qty']):,}", _delta("qty"))
    pr.metric(_col("revenue"), f"¥{_last['revenue']:,.0f}", _delta("revenue"))
    pg.metric(_col("gross_profit"), f"¥{_last['gross_profit']:,.0f}", _delta("gross_profit"))
    pm.metric(_col("gross_margin"), f"{_last['gross_margin']:.2f}%", _delta("gross_margin", pct=True))

    # x 軸 = 日付。月選択済みなので "N日" 形式で簡潔表示（chart 用に datetime 化）
    chart_src = daily.copy()
    chart_src["sale_date"] = pd.to_datetime(chart_src["sale_date"])
    _x = alt.X("sale_date:T", title=None,
               axis=alt.Axis(format="%-d日", labelAngle=0, tickCount="day"))
    _date_tip = alt.Tooltip("sale_date:T", title=_col("sale_date"), format="%Y-%m-%d")

    # 曲線図：日次 総収益 / 粗利
    rev_lbl, gp_lbl = _col("revenue"), _col("gross_profit")
    line_long = chart_src.melt(
        id_vars=["sale_date"], value_vars=["revenue", "gross_profit"],
        var_name="metric", value_name="value",
    )
    line_long["metric"] = line_long["metric"].map({"revenue": rev_lbl, "gross_profit": gp_lbl})
    line_chart = alt.Chart(line_long).mark_line(point=True).encode(
        x=_x,
        y=alt.Y("value:Q", title=None),
        color=alt.Color("metric:N", legend=alt.Legend(title=None, orient="top")),
        tooltip=[_date_tip, alt.Tooltip("metric:N", title=""),
                 alt.Tooltip("value:Q", title="", format=",.0f")],
    ).properties(height=320)
    st.altair_chart(line_chart, use_container_width=True)

    # 销量条形图
    qty_lbl = _col("qty")
    bar_chart = alt.Chart(chart_src).mark_bar().encode(
        x=_x,
        y=alt.Y("qty:Q", title=qty_lbl),
        tooltip=[_date_tip, alt.Tooltip("qty:Q", title=qty_lbl, format=",.0f")],
    ).properties(height=220)
    st.altair_chart(bar_chart, use_container_width=True)

    # 明细表
    day_cols = ("sale_date", "qty", "revenue", "defined_cost",
                "gross_profit", "gross_margin", "n_shop", "n_sku")
    st.dataframe(_disp(daily, day_cols), use_container_width=True, hide_index=True)

# ============================================================
# Tab：担当者別（日本店=対象外 は除外）
# ============================================================
with tab_owner:
    _od = df[df["owner"] != OWNER_EXCLUDED]
    if _od.empty:
        st.info(t("この条件のデータがありません"))
    else:
        g = _od.groupby("owner", as_index=False).agg(
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
        owner_cols = ("owner", "qty", "revenue", "defined_cost",
                      "gross_profit", "gross_margin", "n_shop", "n_sku")
        st.dataframe(_disp(g, owner_cols), use_container_width=True, hide_index=True)
        chart = g.set_index("owner")[["gross_profit"]].copy()
        chart.columns = [_col("gross_profit")]
        st.bar_chart(chart, horizontal=True, use_container_width=True)

# ============================================================
# Tab 1：店舗別
# ============================================================
with tab_shop:
    g = df.groupby(["shop", "owner"], as_index=False).agg(
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

    shop_cols = ("shop", "owner", "qty", "revenue", "defined_cost",
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
