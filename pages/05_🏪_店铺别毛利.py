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

from shared.db import get_readonly_connection
from shared.i18n import lang_selector, t, get_lang
from shared.markets import ALL_MARKETS, add_market_column
from shared.owners import OWNER_EXCLUDED, add_owner_column
from shared import price_alert as pa

st.set_page_config(page_title=t("店铺毛利"), page_icon="🏪", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme, html_table
require_password()
inject_theme()
lang_selector()
conn = get_readonly_connection()

st.title(t("🏪 店铺毛利"))
# データ更新日時（sales_daily 最新 pulled_at · UTC→JST）
try:
    _u = conn.execute("SELECT max(pulled_at) AS u FROM nst.sales_daily").fetchone()
    _uval = _u["u"] if _u else None
except Exception:
    try:
        conn.rollback()
    except Exception:
        pass
    _uval = None
if _uval is not None:
    _uval = _uval.astimezone(dt.timezone(dt.timedelta(hours=9)))
    _upd_str = _uval.strftime("%Y-%m-%d %H:%M")
else:
    _upd_str = "—"
_upd_lbl = "データ更新" if get_lang() == "ja" else "数据更新"
st.caption(t(
    "NST API 売上データ（日次）· 店舗×月の粗利集計 + 月内日次推移 曲線 · "
    "粗利/粗利率 自動計算（定義原価ベース）"
) + f" · {_upd_lbl}: {_upd_str}")

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
# 环比対象（相対%）。gross_margin は _PCT で百分点(pp)环比、n_shop/n_sku は構造カウントで环比なし。
_MOM_VALUE_COLS = {"qty", "revenue", "defined_cost", "gross_profit"}


def _col(key: str) -> str:
    return _LBL[key][1] if get_lang() == "ja" else _LBL[key][0]


def _mom_suffix(cur, key, metric, prev_idx, *, pp: bool = False) -> str:
    """整月环比サフィックス「 (±X.X%)」。上月データなし/0 は「 (—)」。pp=True は百分点(pp)差。"""
    if prev_idx is None or key not in prev_idx.index:
        return " (—)"
    prev = prev_idx.loc[key, metric]
    if pd.isna(prev) or prev == 0:
        return " (—)"
    if pp:
        return f" ({cur - prev:+.1f}pp)"
    return f" ({(cur - prev) / abs(prev) * 100:+.1f}%)"


def _disp(g: pd.DataFrame, cols: tuple, *, mom_prev=None, dim: str = None) -> pd.DataFrame:
    """聚合 DataFrame → 双语列名 + 金额/比率/整数格式化（表示専用）。

    mom_prev(index=dim の上月 dim 别集計) を渡すと、各値指標に「 (±%)」整月环比を内嵌
    （金额/数量=相対%、毛利率=百分点pp）。num 列右对齐は html_table 側で処理。
    """
    d = g[list(cols)].copy()
    keys = list(g[dim]) if (mom_prev is not None and dim) else None
    for c in cols:
        if c in _MONEY:
            if keys is not None and c in _MOM_VALUE_COLS:
                d[c] = [f"¥{v:,.0f}{_mom_suffix(v, k, c, mom_prev)}"
                        for v, k in zip(d[c], keys)]
            else:
                d[c] = d[c].apply(lambda x: f"¥{x:,.0f}")
        elif c in _PCT:
            if keys is not None:
                d[c] = [f"{v:.2f}%{_mom_suffix(v, k, c, mom_prev, pp=True)}"
                        for v, k in zip(d[c], keys)]
            else:
                d[c] = d[c].apply(lambda x: f"{x:.2f}%")
        elif c in _INT:
            if keys is not None and c in _MOM_VALUE_COLS:
                d[c] = [f"{int(v):,}{_mom_suffix(v, k, c, mom_prev)}"
                        for v, k in zip(d[c], keys)]
            else:
                d[c] = d[c].apply(lambda x: f"{int(x):,}")
    d.columns = [_col(c) for c in cols]
    return d


# 柱状图字号（全页统一·缩小）。st.bar_chart 不支持字号参数，横向粗利图改用 Altair 才能控字号。
_CHART_LABEL_FS = 10
_CHART_TITLE_FS = 11


def _hbar(g: pd.DataFrame, dim: str):
    """gross_profit 横向条形图（小字号·主题靛蓝·按值降序），替代 st.bar_chart 以控字号。"""
    gp_lbl = _col("gross_profit")
    h = max(160, len(g) * 24)
    return (
        alt.Chart(g)
        .mark_bar(color="#4F46E5")
        .encode(
            x=alt.X("gross_profit:Q", title=None, axis=None),  # 底部数值刻度删除(表格/tooltip 已有精确值)
            y=alt.Y(f"{dim}:N", title=None, sort="-x"),
            tooltip=[
                alt.Tooltip(f"{dim}:N", title=_col(dim)),
                alt.Tooltip("gross_profit:Q", title=gp_lbl, format=",.0f"),
            ],
        )
        .properties(height=h)
        .configure_axis(labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS)
    )


def _prev_month(ym: str) -> str:
    """'YYYY-MM' → 上一个月 'YYYY-MM'。"""
    cur = dt.datetime.strptime(ym, "%Y-%m")
    prev = cur.replace(day=1) - dt.timedelta(days=1)
    return prev.strftime("%Y-%m")


def _agg_prev(prev_ym: str, mk_sel: str, dim: str, max_day: int = 31) -> pd.DataFrame:
    """上月 dim别 集計（index=dim）。环比の分母用。dim∈{'owner','shop'}。空なら空DF。

    环比口径(Boss 2026-05-25):
      - 完结の過去月 → max_day=31 で整月対整月。
      - 当月(未完结) → max_day=当月の最終データ日(=前一日) で同日範囲対比。
    """
    dfp, _ = _query(
        "SELECT s.shop, s.sale_date, s.qty_sold, s.revenue, s.gross_profit "
        "FROM nst.sales_daily s "
        "WHERE to_char(s.sale_date,'YYYY-MM') = ?",
        (prev_ym,),
    )
    if dfp is None or dfp.empty:
        return pd.DataFrame()
    dfp = dfp[pd.to_datetime(dfp["sale_date"]).dt.day <= max_day]
    if dfp.empty:
        return pd.DataFrame()
    for c in ("qty_sold", "revenue", "gross_profit"):
        dfp[c] = dfp[c].astype(float)
    dfp["defined_cost"] = dfp["revenue"] - dfp["gross_profit"]
    dfp = add_market_column(dfp, store_col="shop")
    dfp = add_owner_column(dfp, shop_col="shop")
    if mk_sel:
        dfp = dfp[dfp["market"].isin(mk_sel)]
    dfp = dfp[dfp["owner"] != OWNER_EXCLUDED]
    g = dfp.groupby(dim, as_index=False).agg(
        qty=("qty_sold", "sum"),
        revenue=("revenue", "sum"),
        defined_cost=("defined_cost", "sum"),
        gross_profit=("gross_profit", "sum"),
    )
    g = _ns_round_money(g)
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    return g.set_index(dim)


def _rhu(v: float) -> float:
    """円丸め HALF_UP(away from zero)= NST 表示丸め。Python round は banker's のため不可。"""
    import math
    return float(math.floor(v + 0.5) if v >= 0 else math.ceil(v - 0.5))


def _ns_round_money(g: pd.DataFrame) -> pd.DataFrame:
    """NST 報表の表示丸め順を再現:収益/定義原価を先に円丸め → 粗利=差。

    粗利を直接丸めると原価合計が半円(.5)境界の時に NST と ±1 円ズレる
    (2026-06 Shopee SG で実証·Boss 100%一致指示)。"""
    g = g.copy()
    if "defined_cost" not in g.columns:
        g["defined_cost"] = g["revenue"] - g["gross_profit"]
    g["revenue"] = g["revenue"].map(_rhu)
    g["defined_cost"] = g["defined_cost"].map(_rhu)
    g["gross_profit"] = g["revenue"] - g["defined_cost"]
    return g


def _query(sql: str, params: tuple = ()):
    try:
        from shared.cache import cached_df, data_version
        return cached_df(conn, sql, params or None, ver=data_version("basic", "sales")), None
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
mk = c2.multiselect(t("市場"), ALL_MARKETS, placeholder=t("全部市场"))  # 空選＝全部市场

# ============================================================
# クエリ（当月の日次明細 + 商品マスタ join）
# ============================================================
df, e2 = _query(
    "SELECT s.shop, s.sale_date, s.item_internal_id, "
    "im.display_name, im.maker, im.item_rank, "
    "s.qty_sold, s.revenue, "
    "s.gross_profit "
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
df["gross_profit"] = df["gross_profit"].astype(float)    # 報表口径 粗利(=売上−行級定義原価·2026-07-10)
df["defined_cost"] = df["revenue"] - df["gross_profit"]  # NetSuite 口径の原価（売上−毛利）
# 未来日付除外 + 当日は未確定（07:00 に前日分取得）のため除外：前日まで
_today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
_cutoff = _today - dt.timedelta(days=1)
df = df[df["sale_date"] <= _cutoff]
if df.empty:
    # 当月初日など、確定データ（前日分まで）がまだ無いケース
    st.info(t("確定済みの売上データがまだありません（前日分は翌07:00に取得）"))
    st.stop()
df = add_market_column(df, store_col="shop")
df = add_owner_column(df, shop_col="shop")

if mk:
    df = df[df["market"].isin(mk)]
    if df.empty:
        st.info(t("この市場のデータがありません"))
        st.stop()

# 上月集計（环比の分母）· 担当者別 / 店舗別
# 当月(=今の暦月)は未完结 → 上月も「当月の最終データ日(前一日)」まで同日範囲で対比。
# 完结の過去月は整月対整月(max_day=31)。
_real_cur_ym = _today.strftime("%Y-%m")
_max_day = int(pd.to_datetime(df["sale_date"]).dt.day.max()) if ym == _real_cur_ym else 31
_prev_ym = _prev_month(ym)
_prev_owner = _agg_prev(_prev_ym, mk, "owner", _max_day)
_prev_shop = _agg_prev(_prev_ym, mk, "shop", _max_day)

# ============================================================
# KPI（総）
# ============================================================
tot_r = _rhu(df["revenue"].sum())
tot_c = _rhu(df["defined_cost"].sum())
tot_g = tot_r - tot_c  # NST 表示丸め順(原価丸め→差)·半円境界の±1円ズレ防止
margin = (tot_g / tot_r * 100) if tot_r else 0

m2, m3, m4, m5 = st.columns(4)
m2.metric(t("総収益 計"), f"¥{tot_r:,.0f}")
m3.metric(t("定義原価 計"), f"¥{tot_c:,.0f}")
m4.metric(t("粗利 計"), f"¥{tot_g:,.0f}")
m5.metric(t("粗利率"), f"{margin:.2f}%")

st.divider()

_owner_tab = "👤 担当者別" if get_lang() == "ja" else "👤 店铺负责人"
(tab_day, tab_owner, tab_shop, tab_market, tab_sku, tab_alert, tab_deduct,
 tab_payout) = st.tabs(
    [t("📈 月内日次推移"), _owner_tab, t("🏪 店舗別"), t("🌐 市場別"),
     t("🏆 TOP SKU"), t("⚠️ 价格预警"), t("🧾 店铺扣减"), t("💵 拨款明细")]
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
        cur, prev = float(_last[key]), float(_prev[key])
        diff = cur - prev
        if pct:  # 粗利率は百分点差
            return f"{diff:+.2f}pt"
        rate = (diff / prev * 100) if prev else 0.0
        return f"{diff:+,.0f} ({rate:+.1f}%)"

    pq, pr, pg, pm = st.columns(4)
    pq.metric(_col("qty"), f"{int(_last['qty']):,}", _delta("qty"))
    pr.metric(_col("revenue"), f"¥{_last['revenue']:,.0f}", _delta("revenue"))
    pg.metric(_col("gross_profit"), f"¥{_last['gross_profit']:,.0f}", _delta("gross_profit"))
    pm.metric(_col("gross_margin"), f"{_last['gross_margin']:.2f}%", _delta("gross_margin", pct=True))

    # x 軸 = 日付。月選択済みなので "N日" 形式で簡潔表示（chart 用に datetime 化）
    chart_src = daily.copy()
    chart_src["sale_date"] = pd.to_datetime(chart_src["sale_date"])
    chart_src["margin_disp"] = chart_src["gross_margin"].apply(lambda x: f"{x:.2f}%")
    _x = alt.X("sale_date:T", title=None,
               axis=alt.Axis(format="%-d日", labelAngle=0, tickCount="day"))
    _date_tip = alt.Tooltip("sale_date:T", title=_col("sale_date"), format="%Y-%m-%d")

    # 曲線図：日次 総収益 / 粗利（日付軸どこに hover しても両方表示）
    rev_lbl, gp_lbl = _col("revenue"), _col("gross_profit")
    _tip = [_date_tip,
            alt.Tooltip("revenue:Q", title=rev_lbl, format=",.0f"),
            alt.Tooltip("gross_profit:Q", title=gp_lbl, format=",.0f"),
            alt.Tooltip("margin_disp:N", title=_col("gross_margin"))]
    mg_lbl = _col("gross_margin")
    _nearest = alt.selection_point(nearest=True, on="pointerover",
                                   fields=["sale_date"], empty=False)
    _base = alt.Chart(chart_src).encode(x=_x)
    _amt_lbl = "金額" if get_lang() == "ja" else "金额"
    # 金額（左軸）：総収益 / 粗利（軸タイトルは layer 先頭の _l_rev が保持）
    _l_rev = _base.mark_line(point=True).encode(
        y=alt.Y("revenue:Q", title=_amt_lbl), color=alt.datum(rev_lbl),
    )
    _l_gp = _base.mark_line(point=True).encode(
        y=alt.Y("gross_profit:Q", title=None), color=alt.datum(gp_lbl),
    )
    # 粗利率（右軸・破線）
    _l_mg = _base.mark_line(point=True, strokeDash=[5, 3]).encode(
        y=alt.Y("gross_margin:Q", title=mg_lbl,
                scale=alt.Scale(domain=[50, 70]),
                axis=alt.Axis(orient="right", format=".0f")),
        color=alt.datum(mg_lbl),
    )
    # 透明な縦ルールで日付軸全体を hover 対象に → 最近傍の日に縦線+全値 tooltip
    _rule = _base.mark_rule(color="#888").encode(
        opacity=alt.condition(_nearest, alt.value(0.35), alt.value(0)),
        tooltip=_tip,
    ).add_params(_nearest)
    _money = alt.layer(_l_rev, _l_gp)   # 左軸共有
    line_chart = (
        alt.layer(_money, _l_mg, _rule)
        .resolve_scale(y="independent", color="shared")
        .properties(height=320)
        .configure_legend(orient="top", title=None)
    )
    st.altair_chart(line_chart, use_container_width=True)

    # 销量条形图
    qty_lbl = _col("qty")
    bar_chart = alt.Chart(chart_src).mark_bar(size=22).encode(
        x=_x,
        y=alt.Y("qty:Q", title=qty_lbl),
        tooltip=[_date_tip, alt.Tooltip("qty:Q", title=qty_lbl, format=",.0f")],
    ).properties(height=220).configure_axis(
        labelFontSize=_CHART_LABEL_FS, titleFontSize=_CHART_TITLE_FS
    )
    st.altair_chart(bar_chart, use_container_width=True)

    # 明细表（日付倒序·最近日在上。daily 本体は昇順維持＝前日指標/曲線図が依存）
    day_cols = ("sale_date", "qty", "revenue", "defined_cost",
                "gross_profit", "gross_margin", "n_shop", "n_sku")
    html_table(_disp(daily.sort_values("sale_date", ascending=False), day_cols))

# ============================================================
# Tab：担当者別（日本店=対象外 は除外）
# ============================================================
with tab_owner:
    _od = df[df["owner"] != OWNER_EXCLUDED]
    if _od.empty:
        st.info(t("この条件のデータがありません"))
    else:
        # ① 负责人汇总（総数据总览）+ 柱状图
        g = _od.groupby("owner", as_index=False).agg(
            qty=("qty_sold", "sum"),
            revenue=("revenue", "sum"),
            defined_cost=("defined_cost", "sum"),
            gross_profit=("gross_profit", "sum"),
            n_shop=("shop", "nunique"),
            n_sku=("item_internal_id", "nunique"),
        )
        g = _ns_round_money(g)
        g["gross_margin"] = (
            g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
        ).fillna(0) * 100
        g = g.sort_values("gross_profit", ascending=False)
        owner_cols = ("owner", "qty", "revenue", "defined_cost",
                      "gross_profit", "gross_margin", "n_shop", "n_sku")
        html_table(_disp(g, owner_cols, mom_prev=_prev_owner, dim="owner"))
        st.altair_chart(_hbar(g, "owner"), use_container_width=True)

        st.divider()

        # ② 各负责人ごと：名称(総計) + 配下の店舗別明細
        for _ow in g["owner"].tolist():   # gross_profit 降順
            sub = _od[_od["owner"] == _ow]
            t_ns = sub["shop"].nunique()
            t_q = sub["qty_sold"].sum()
            t_r = _rhu(sub["revenue"].sum())
            t_g = t_r - _rhu(sub["defined_cost"].sum())  # NST 丸め順
            t_m = (t_g / t_r * 100) if t_r else 0
            st.markdown(
                f"**👤 {_ow}** &nbsp;｜&nbsp; {_col('n_shop')}: {t_ns} ｜ "
                f"{_col('qty')}: {int(t_q):,} ｜ "
                f"{_col('revenue')}: ¥{t_r:,.0f} ｜ {_col('gross_profit')}: ¥{t_g:,.0f} ｜ "
                f"{_col('gross_margin')}: {t_m:.2f}%"
            )
            sg = sub.groupby("shop", as_index=False).agg(
                qty=("qty_sold", "sum"),
                revenue=("revenue", "sum"),
                defined_cost=("defined_cost", "sum"),
                gross_profit=("gross_profit", "sum"),
                n_sku=("item_internal_id", "nunique"),
            )
            sg = _ns_round_money(sg)
            sg["gross_margin"] = (
                sg["gross_profit"] / sg["revenue"].where(sg["revenue"] != 0)
            ).fillna(0) * 100
            sg = sg.sort_values("gross_profit", ascending=False)
            sg_cols = ("shop", "qty", "revenue", "defined_cost",
                       "gross_profit", "gross_margin", "n_sku")
            html_table(_disp(sg, sg_cols, mom_prev=_prev_shop, dim="shop"))

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
    g = _ns_round_money(g)
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = g.sort_values("gross_profit", ascending=False)

    shop_cols = ("shop", "owner", "qty", "revenue", "defined_cost",
                 "gross_profit", "gross_margin", "n_sku")
    html_table(_disp(g, shop_cols, mom_prev=_prev_shop, dim="shop"))
    st.altair_chart(_hbar(g, "shop"), use_container_width=True)

    st.divider()
    # ── 単店の日次 粗利率トレンド（Boss 2026-06-22 依頼）──
    #   月/市場フィルタは df 段階で適用済 · 店舗だけ選んで日次 粗利率曲線を見る。
    _sub_hdr = ("🏪 単店の日次 粗利率トレンド" if get_lang() == "ja"
                else "🏪 单店日次毛利率趋势")
    st.markdown("##### " + _sub_hdr)
    _shop_opts = g["shop"].tolist()  # gross_profit 降順（g は上で sort 済）
    sel_shop = st.selectbox(_col("shop"), _shop_opts, key="shop_daily_sel")
    _sdd = (df[df["shop"] == sel_shop]
            .groupby("sale_date", as_index=False)
            .agg(qty=("qty_sold", "sum"), revenue=("revenue", "sum"),
                 gross_profit=("gross_profit", "sum"))
            .sort_values("sale_date"))
    _sdd["gross_margin"] = (
        _sdd["gross_profit"] / _sdd["revenue"].where(_sdd["revenue"] != 0)
    ).fillna(0) * 100
    if _sdd.empty:
        st.info(t("この条件のデータがありません"))
    else:
        _sc = _sdd.copy()
        _sc["sale_date"] = pd.to_datetime(_sc["sale_date"])
        _sc["margin_disp"] = _sc["gross_margin"].apply(lambda x: f"{x:.2f}%")
        _sx = alt.X("sale_date:T", title=None,
                    axis=alt.Axis(format="%-d日", labelAngle=0, tickCount="day"))
        _mg_lbl = _col("gross_margin")
        _stip = [
            alt.Tooltip("sale_date:T", title=_col("sale_date"), format="%Y-%m-%d"),
            alt.Tooltip("revenue:Q", title=_col("revenue"), format=",.0f"),
            alt.Tooltip("gross_profit:Q", title=_col("gross_profit"), format=",.0f"),
            alt.Tooltip("margin_disp:N", title=_mg_lbl),
            alt.Tooltip("qty:Q", title=_col("qty"), format=",.0f"),
        ]
        # 月内日次推移 tab と同じ「透明縦ルール＋最近傍 hover」方式で線上以外でも tooltip。
        _sbase = alt.Chart(_sc).encode(x=_sx)
        _snear = alt.selection_point(nearest=True, on="pointerover",
                                     fields=["sale_date"], empty=False)
        _sline = _sbase.mark_line(point=True, color="#4F46E5").encode(
            y=alt.Y("gross_margin:Q", title=_mg_lbl,
                    scale=alt.Scale(zero=False)),  # 単店は 50-70% 外も有り得る→自動缩放
        )
        _srule = _sbase.mark_rule(color="#888").encode(
            opacity=alt.condition(_snear, alt.value(0.35), alt.value(0)),
            tooltip=_stip,
        ).add_params(_snear)
        _schart = (alt.layer(_sline, _srule).properties(height=300)
                   .configure_axis(labelFontSize=_CHART_LABEL_FS,
                                   titleFontSize=_CHART_TITLE_FS))
        st.altair_chart(_schart, use_container_width=True)

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
    g = _ns_round_money(g)
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = g.sort_values("gross_profit", ascending=False)

    mkt_cols = ("market", "qty", "revenue", "defined_cost",
                "gross_profit", "gross_margin", "n_shop", "n_sku")
    html_table(_disp(g, mkt_cols))
    st.altair_chart(_hbar(g, "market"), use_container_width=True)

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
    g = _ns_round_money(g)
    g["gross_margin"] = (
        g["gross_profit"] / g["revenue"].where(g["revenue"] != 0)
    ).fillna(0) * 100
    g = g.sort_values("gross_profit", ascending=False).head(n_top)

    sku_cols = ("display_name", "maker", "item_rank", "qty",
                "revenue", "gross_profit", "gross_margin")
    html_table(_disp(g, sku_cols))

# ============================================================
# Tab 5：价格预警（当日 vs 近7日平均 · 参数可调 · Boss 2026-06-22）
#   判定ロジックは shared/price_alert.py（純関数·tests/test_price_alert.py）
# ============================================================
with tab_alert:
    st.caption(t(
        "各店「当日」(最新确认日) を 近7日平均 と比べて毛利异常を检知 · "
        "🔴=規則1/2/3(毛利侵蚀/降价冲量/破红线) · 🟡=規則4(収益↑だが毛利率↓) · 閾値は下で可変"
    ))
    _pc1, _pc2, _pc3, _pc4 = st.columns(4)
    _p_dev = _pc1.number_input(t("①跌破近7日均(pp)"), 0.0, 50.0, 5.0, 0.5, key="pa_dev")
    _p_qty = _pc2.number_input(t("②销量上涨(%)"), 0.0, 2000.0, 100.0, 10.0, key="pa_qty")
    _p_mgn = _pc3.number_input(t("②毛利率下降(pp)"), 0.0, 50.0, 5.0, 0.5, key="pa_mgn")
    _p_red = _pc4.number_input(t("③红线毛利率(%)"), 0.0, 100.0, 45.0, 1.0, key="pa_red")
    _params = pa.AlertParams(dev_pp=_p_dev, qty_up_pct=_p_qty,
                             mgn_drop_pp=_p_mgn, red_line_pct=_p_red)

    # 当日 = 当前 df 内最新确认日 · 近7日は月境界をまたぐため sales_daily を直接クエリ
    _cur_day = pd.to_datetime(df["sale_date"]).max().date()
    _win_start = _cur_day - dt.timedelta(days=7)
    _aw, _ae = _query(
        "SELECT s.shop, s.sale_date, s.qty_sold, s.revenue, s.gross_profit "
        "FROM nst.sales_daily s WHERE s.sale_date >= ? AND s.sale_date <= ?",
        (_win_start.isoformat(), _cur_day.isoformat()),
    )
    if _ae:
        st.error(_ae)
    elif _aw is None or _aw.empty:
        st.info(t("この条件のデータがありません"))
    else:
        for _c in ("qty_sold", "revenue", "gross_profit"):
            _aw[_c] = _aw[_c].astype(float)
        _aw["sale_date"] = pd.to_datetime(_aw["sale_date"]).dt.date
        _aw = add_market_column(_aw, store_col="shop")
        _aw = add_owner_column(_aw, shop_col="shop")
        _aw = _aw[_aw["owner"] != OWNER_EXCLUDED]
        if mk:
            _aw = _aw[_aw["market"].isin(mk)]
        _owner_map = (_aw.drop_duplicates("shop").set_index("shop")["owner"]
                      if not _aw.empty else {})
        _dd = _aw.groupby(["shop", "sale_date"], as_index=False).agg(
            qty=("qty_sold", "sum"), revenue=("revenue", "sum"),
            gross_profit=("gross_profit", "sum"))
        _dd["margin"] = (_dd["gross_profit"]
                         / _dd["revenue"].where(_dd["revenue"] != 0)).fillna(0) * 100

        _RULE_LBL = {pa.R1: "①跌破7日均", pa.R2: "②降价冲量",
                     pa.R3: "③破红线", pa.R4: "④收益涨毛利没跟"}
        _EMO = {pa.ALERT_RED: "🔴", pa.ALERT_YELLOW: "🟡", pa.ALERT_OK: "✅"}
        _rows = []
        for _shop, _g in _dd.groupby("shop"):
            _cur = _g[_g["sale_date"] == _cur_day]
            if _cur.empty:
                continue  # 当日に売上の無い店舗は判定対象外
            _cur = _cur.iloc[0]
            _base = _g[_g["sale_date"] < _cur_day]
            _hb = not _base.empty
            _am = float(_base["margin"].mean()) if _hb else None
            _aq = float(_base["qty"].mean()) if _hb else None
            _ar = float(_base["revenue"].mean()) if _hb else None
            _status, _fired = pa.evaluate(
                cur_margin=float(_cur["margin"]), avg7_margin=_am,
                cur_qty=float(_cur["qty"]), avg7_qty=_aq,
                cur_rev=float(_cur["revenue"]), avg7_rev=_ar,
                params=_params, has_baseline=_hb)
            _rows.append({
                "_sev": pa.severity(_status), "_st": _status,
                "shop": _shop, "owner": _owner_map.get(_shop, ""),
                "cur_m": float(_cur["margin"]), "avg_m": _am,
                "cur_q": float(_cur["qty"]), "avg_q": _aq,
                "cur_r": float(_cur["revenue"]), "avg_r": _ar,
                "fired": "、".join(_RULE_LBL[r] for r in _fired),
            })
        _adf = pd.DataFrame(_rows)
        if _adf.empty:
            st.info(t("当日に売上のある店舗がありません"))
        else:
            _ka, _kb, _kc, _kd = st.columns(4)
            _ka.metric(t("当日"), f"{_cur_day.month}月{_cur_day.day}日")
            _kb.metric("🔴 " + t("严重"), f"{int((_adf['_st'] == pa.ALERT_RED).sum())}")
            _kc.metric("🟡 " + t("注意"), f"{int((_adf['_st'] == pa.ALERT_YELLOW).sum())}")
            _kd.metric("✅ " + t("正常"), f"{int((_adf['_st'] == pa.ALERT_OK).sum())}")
            _only = st.checkbox(t("只看预警(隐藏正常)"), value=True, key="pa_only")
            _v = _adf.sort_values(["_sev", "cur_m"], ascending=[False, True])
            if _only:
                _v = _v[_v["_st"] != pa.ALERT_OK]
            if _v.empty:
                st.success(t("✅ 当日 全店毛利正常，无预警"))
            else:
                def _f_pct(x):
                    return "—" if x is None or pd.isna(x) else f"{x:.2f}%"

                def _f_yen(x):
                    return "—" if x is None or pd.isna(x) else f"¥{x:,.0f}"

                def _f_num(x):
                    return "—" if x is None or pd.isna(x) else f"{x:,.0f}"

                _disp_alert = pd.DataFrame({
                    t("状态"): [_EMO[s] for s in _v["_st"]],
                    _col("shop"): _v["shop"].tolist(),
                    _col("owner"): _v["owner"].tolist(),
                    t("当日毛利率"): [_f_pct(x) for x in _v["cur_m"]],
                    t("近7日均毛利率"): [_f_pct(x) for x in _v["avg_m"]],
                    t("当日销量"): [_f_num(x) for x in _v["cur_q"]],
                    t("近7日均销量"): [_f_num(x) for x in _v["avg_q"]],
                    t("当日总收益"): [_f_yen(x) for x in _v["cur_r"]],
                    t("近7日均收益"): [_f_yen(x) for x in _v["avg_r"]],
                    t("触发规则"): _v["fired"].tolist(),
                })
                html_table(_disp_alert)

# ============================================================
# Tab 6：店铺扣减（Coupang 결산 · coupang.settlement · Boss 2026-07-07 依頼）
#   Coupang Open API 지급내역を元川 PG に日次取込 → 扣減構成を可視化。
#   KRW 建て・Coupang 韓国店のみ → 上部の市場フィルタ / 対象月とは独立。
# ============================================================
with tab_deduct:

    # ============================================================
    # Shopee（ASEAN）· **現金主義** = 入金(payout)ベース
    #   Boss 2026-07-29: 財務は必ず現金主義。注文発生日ではなく着金した回で括る。
    #   官方エクスポート（我的收入 > 已完成拨款）の Summary と数値一致を確認済み。
    # ============================================================
    from shared.markets import MARKET_SEA as _MK_SEA

    _sp_ja = get_lang() == "ja"

    def _spl(zh: str, ja: str) -> str:
        return ja if _sp_ja else zh

    if mk and _MK_SEA not in mk:
        st.info(t("Shopee（东南亚）扣减 · 请在市场筛选中包含东南亚或清空筛选"))
    else:
        st.markdown("#### " + _spl("🌏 ASEAN · Shopee", "🌏 ASEAN · Shopee"))
        st.caption(t(
            "Shopee 各店「已完成拨款」· 现金主义（与 Shopee 后台 我的收入 > 已完成拨款 同口径）· "
            "按拨款批次归集而非下单日 · 结算币 USD · 每日自动拉取（page 27 可手动同步）"
        ))

        @st.cache_data(ttl=300, show_spinner=False)
        def _shopee_ver() -> str:
            """shopee 域の缓存版本。

            回填は docker exec 直起動で pull_schedule を更新しないため、
            pull_schedule ではなく pull_log の最終書き込み時刻を版本にする
            （さもないと回填直後も古いキャッシュを 1 時間返し続ける）。
            """
            try:
                row = get_readonly_connection().execute(
                    "SELECT max(finished_at) FROM shopee.pull_log").fetchone()
                if row and row[0]:
                    return str(row[0])
            except Exception:
                pass
            return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H")

        def _sq(sql: str, params: tuple = ()):
            try:
                from shared.cache import cached_df
                return cached_df(conn, sql, params or None, ver=_shopee_ver()), None
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return None, str(e)

        _sp_all, _sp_err = _sq(
            "SELECT ym, market, country_code, shop_key, shop_name, currency, "
            "payouts, orders, commission_fee, service_fee, transaction_fee, "
            "ams_commission, seller_shipping, shopee_shipping_rebate, "
            "deduction_total, escrow_total, adjustment_total, ads_fee, "
            "from_amount, payout_amount, matched_pct, pulled_at "
            "FROM shopee.v_payout_monthly ORDER BY ym DESC, country_code, shop_key")

        if _sp_err:
            st.error(t("shopee 财务视图未取得 or 连接错误: ") + _sp_err)
            st.info(t("需在元川 PG 执行 shopee_api/sql/008,009（建表与视图）"))
        elif _sp_all is None or _sp_all.empty:
            st.info(t("Shopee 拨款数据为空 · 请在 page 27 手动同步或等待每日自动拉取"))
        else:
            _sp = _sp_all[_sp_all["ym"] == str(ym)].copy()
            if _sp.empty:
                st.info(t("当月尚无已完成拨款 · 以下趋势为全期间"))

            # --- 表示通貨: USD（Shopee の決済通貨）/ JPY 換算 -------------
            _spc = st.radio(_spl("展示货币", "表示通貨"), ["USD", "JPY"],
                            horizontal=True, key="sp_ded_cur")
            from shared.forex import usd_export_rate
            _sp_rate = 1.0 if _spc == "USD" else usd_export_rate(str(ym))
            _sp_sym = "$" if _spc == "USD" else "¥"

            # 費用は現地通貨建て → 入金の実効レート(USD/現地)で USD 化してから表示通貨へ
            def _to_disp(d: "pd.DataFrame") -> "pd.DataFrame":
                d = d.copy()
                _r = (d["payout_amount"] / d["from_amount"].replace(0, pd.NA)).fillna(0)
                for c in ("commission_fee", "service_fee", "transaction_fee",
                          "ams_commission", "seller_shipping", "shopee_shipping_rebate",
                          "deduction_total", "escrow_total", "adjustment_total",
                          "ads_fee"):
                    d[c] = (pd.to_numeric(d[c], errors="coerce").fillna(0) * _r
                            * _sp_rate).round(0)
                d["payout_disp"] = (pd.to_numeric(d["payout_amount"], errors="coerce")
                                    .fillna(0) * _sp_rate).round(0)
                return d

            _spd = _to_disp(_sp) if not _sp.empty else _sp

            def _m(v) -> str:
                return f"{_sp_sym}{v:,.0f}"

            # --- KPI ---------------------------------------------------
            if not _spd.empty:
                _tot_payout = _spd["payout_disp"].sum()
                _tot_ded = _spd["deduction_total"].sum()
                _tot_esc = _spd["escrow_total"].sum()
                _tot_ads = _spd["ads_fee"].sum()
                k1, k2, k3, k4 = st.columns(4)
                k1.metric(_spl("总拨款额", "総入金額"), _m(_tot_payout))
                k2.metric(_spl("扣减合计", "控除合計"), _m(_tot_ded))
                k3.metric(_spl("扣减率", "控除率"),
                          f"{(_tot_ded / _tot_esc * 100):.1f}%" if _tot_esc else "—")
                k4.metric(_spl("广告费", "広告費"), _m(_tot_ads))

                _mp = _spd["matched_pct"].astype(float).mean()
                if _mp < 95:
                    st.warning(_spl(
                        f"⚠️ 费用明细覆盖率 {_mp:.0f}% — 拨款中含尚未取得明细的订单，"
                        "扣减各项偏小（拨款额本身准确）。补齐: pull_escrow --from-payout",
                        f"⚠️ 費用内訳のカバー率 {_mp:.0f}% — 入金に未取得の注文が含まれます"))

            _DL = _spl("⬇️ 下载 CSV", "⬇️ CSV ダウンロード")

            def _dl_btn(d: "pd.DataFrame", fname: str, key: str) -> None:
                st.download_button(
                    _DL, d.to_csv(index=False).encode("utf-8-sig"),
                    file_name=fname, mime="text/csv", key=key)

            _FEE_COLS = [
                ("commission_fee", _spl("佣金", "手数料")),
                ("service_fee", _spl("服务费", "サービス料")),
                ("transaction_fee", _spl("交易手续费", "取引手数料")),
                ("ams_commission", _spl("联盟营销佣金", "アフィリエイト報酬")),
                ("seller_shipping", _spl("卖家支付运费", "売り手負担送料")),
                ("ads_fee", _spl("广告费", "広告費")),
            ]

            # --- ① 市場層 ------------------------------------------------
            if not _spd.empty:
                st.markdown("**" + _spl("① 市场", "① 市場") + "**")
                _g1 = _spd.groupby("market", as_index=False).agg(
                    {"orders": "sum", "escrow_total": "sum", "deduction_total": "sum",
                     "ads_fee": "sum", "payout_disp": "sum"})
                _r1 = [{
                    _spl("市场", "市場"): r["market"],
                    _spl("订单数", "注文数"): f"{int(r['orders']):,}",
                    _spl("订单收入", "注文収入"): _m(r["escrow_total"]),
                    _spl("扣减合计", "控除合計"): _m(r["deduction_total"]),
                    _spl("扣减率", "控除率"): f"{(r['deduction_total']/r['escrow_total']*100):.1f}%" if r["escrow_total"] else "—",
                    _spl("广告费", "広告費"): _m(r["ads_fee"]),
                    _spl("拨款额", "入金額"): _m(r["payout_disp"]),
                } for _, r in _g1.iterrows()]
                html_table(pd.DataFrame(_r1))
                _dl_btn(_g1, f"shopee_market_{ym}.csv", "sp_dl_market")

                # --- ② 国家層 --------------------------------------------
                st.markdown("**" + _spl("② 国家", "② 国別") + "**")
                _g2 = _spd.groupby("country_code", as_index=False).agg(
                    {"shop_key": "nunique", "orders": "sum", "escrow_total": "sum",
                     "deduction_total": "sum", "ads_fee": "sum", "payout_disp": "sum",
                     **{c: "sum" for c, _ in _FEE_COLS if c != "ads_fee"}})
                _g2 = _g2.sort_values("payout_disp", ascending=False)
                _r2 = [{
                    _spl("国家", "国"): r["country_code"],
                    _spl("店铺数", "店舗数"): int(r["shop_key"]),
                    _spl("订单数", "注文数"): f"{int(r['orders']):,}",
                    _spl("订单收入", "注文収入"): _m(r["escrow_total"]),
                    **{lbl: _m(r[c]) for c, lbl in _FEE_COLS},
                    _spl("扣减合计", "控除合計"): _m(r["deduction_total"]),
                    _spl("拨款额", "入金額"): _m(r["payout_disp"]),
                } for _, r in _g2.iterrows()]
                html_table(pd.DataFrame(_r2))
                _dl_btn(_g2, f"shopee_country_{ym}.csv", "sp_dl_country")

                # --- ③ 店舗層 --------------------------------------------
                st.markdown("**" + _spl("③ 店铺", "③ 店舗") + "**")
                _g3 = _spd.sort_values("payout_disp", ascending=False)
                _r3 = [{
                    _spl("国家", "国"): r["country_code"],
                    _spl("店铺", "店舗"): r["shop_name"] or r["shop_key"],
                    _spl("拨款次数", "入金回数"): int(r["payouts"]),
                    _spl("订单数", "注文数"): f"{int(r['orders']):,}",
                    _spl("订单收入", "注文収入"): _m(r["escrow_total"]),
                    **{lbl: _m(r[c]) for c, lbl in _FEE_COLS},
                    _spl("扣减合计", "控除合計"): _m(r["deduction_total"]),
                    _spl("拨款额", "入金額"): _m(r["payout_disp"]),
                } for _, r in _g3.iterrows()]
                html_table(pd.DataFrame(_r3))
                _dl_btn(_g3, f"shopee_shop_{ym}.csv", "sp_dl_shop")

            # --- ④ 12ヶ月トレンド ---------------------------------------
            st.markdown("**" + _spl("④ 近 12 个月趋势", "④ 直近 12 ヶ月推移") + "**")
            _trend = _to_disp(_sp_all).groupby("ym", as_index=False).agg(
                {c: "sum" for c, _ in _FEE_COLS}).sort_values("ym").tail(12)
            _long = _trend.melt(id_vars="ym", var_name="item", value_name="amount")
            _lbl_map = dict(_FEE_COLS)
            _long["item"] = _long["item"].map(_lbl_map)
            if not _long.empty:
                _ch = (alt.Chart(_long).mark_bar()
                       .encode(x=alt.X("ym:N", title=None),
                               y=alt.Y("amount:Q", title=None),
                               color=alt.Color("item:N", title=None,
                                               legend=alt.Legend(orient="top")),
                               tooltip=["ym", "item", alt.Tooltip("amount:Q", format=",.0f")])
                       .properties(height=280)
                       .configure_axis(labelFontSize=_CHART_LABEL_FS,
                                       titleFontSize=_CHART_TITLE_FS))
                st.altair_chart(_ch, use_container_width=True)
            _dl_btn(_trend, "shopee_trend.csv", "sp_dl_trend")

            if _sp_all is not None and not _sp_all.empty:
                _pa = pd.to_datetime(_sp_all["pulled_at"], errors="coerce").max()
                if pd.notna(_pa):
                    st.caption(t("データ更新: ") + str(_pa)[:19])

    st.divider()
    # 扣減は Coupang（韓国店）の売上に対応するもの。東南亜/日本は扣減データ未接続
    # → 市場フィルタが韓国を含まない場合は表示しない（Boss 2026-07-07）。
    from shared.markets import MARKET_KOREA
    if mk and MARKET_KOREA not in mk:
        st.info(t("店铺扣减目前仅有 Coupang（韩国店）数据 · 东南亚/日本暂无 · 请在市场筛选中包含韩国或清空筛选"))
    else:
        st.caption(t(
            "Coupang（韩国店）结算单扣减构成，对应 Coupang 自身销售额（KRW）· 东南亚/日本暂无扣减数据 · "
            "Coupang Open API 每日自动拉取（page 27 可手动同步）· 结算单在销售认知月结束后生成，当月暂无属正常"
        ))

        @st.cache_data(ttl=300, show_spinner=False)
        def _coupang_ver() -> str:
            """coupang 域の缓存版本（coupang.pull_schedule.last_run_at 最大値）。"""
            try:
                row = get_readonly_connection().execute(
                    "SELECT max(last_run_at) FROM coupang.pull_schedule").fetchone()
                if row and row[0]:
                    return str(row[0])
            except Exception:
                pass
            return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")

        def _cq(sql: str, params: tuple = ()):
            try:
                from shared.cache import cached_df
                return cached_df(conn, sql, params or None, ver=_coupang_ver()), None
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return None, str(e)

        # 扣減項目（DB列, 中文, 日本語）— WING「预计最终支付金额」までの控除全項目
        _DED_ITEMS = [
            ("service_fee",              "销售手续费",      "販売手数料"),
            ("seller_service_fee",       "卖家手续费",      "販売者手数料"),
            ("seller_discount_coupon",   "立减优惠券",      "即時割引クーポン"),
            ("downloadable_coupon",      "下载优惠券",      "DLクーポン"),
            ("store_fee_discount",       "店铺手续费折扣",  "店舗手数料割引"),
            ("courantee_fee",            "Courantee费",     "Courantee費"),
            ("courantee_customer_reward", "Courantee补偿",  "Courantee補償"),
            ("deduction_amount",         "广告费",          "広告費"),
            ("debt_of_last_week",        "上周未清欠款",    "前週未払金"),
            ("dedicated_delivery_amount", "专用快递费",     "専用宅配費"),
        ]
        _TYPE_LBL = {"MONTHLY": ("月结", "月次"), "WEEKLY": ("周结", "週次"),
                     "ADDITIONAL": ("追加结算", "追加"), "RESERVE": ("尾款支付", "最終金")}
        _ST_LBL = {"DONE": ("✅ 已打款", "✅ 支払済"), "SUBJECT": ("⏳ 待打款", "⏳ 未払")}
        _ja = get_lang() == "ja"

        def _dl(zh: str, ja: str) -> str:
            return ja if _ja else zh

        _ded_cols = [c for c, _, _ in _DED_ITEMS]
        cdf, cerr = _cq(
            "SELECT revenue_recognition_year_month AS ym, settlement_type, "
            "settlement_date, status, total_sale, settlement_amount, "
            "pending_released_amount, last_amount, final_amount, pulled_at, "
            + ", ".join(_ded_cols) +
            " FROM coupang.settlement "
            "ORDER BY revenue_recognition_year_month DESC, settlement_date"
        )
        if cerr:
            st.error(t("coupang.settlement 未取得 or 接続エラー: ") + cerr)
            st.info(t("初次使用需在元川 PG 跑 coupang_api/sql/000 迁移并启动 coupang_scheduler 容器"))
        elif cdf is None or cdf.empty:
            st.info(t("尚无 Coupang 结算数据。请到 page 27「数据获取」→ Coupang tab 触发首次拉取。"))
        else:
            for _c in ["total_sale", "settlement_amount", "pending_released_amount",
                       "last_amount", "final_amount"] + _ded_cols:
                cdf[_c] = pd.to_numeric(cdf[_c], errors="coerce").fillna(0.0)
            cdf["deduction_total"] = cdf[_ded_cols].sum(axis=1)

            _pull_max = pd.to_datetime(cdf["pulled_at"]).max()
            _pull_str = (_pull_max.tz_convert("Asia/Tokyo").strftime("%Y-%m-%d %H:%M")
                         if _pull_max is not pd.NaT and _pull_max.tzinfo
                         else str(_pull_max)[:16])
            st.caption(("データ更新: " if _ja else "数据更新: ") + _pull_str)

            # ── 展示货币（KRW 原币 / JPY / USD）──
            # 認識月ごとに「その月に適用中」の三金レートで換算（当月更新なしは
            # 前月沿用 · nst.currency_rate · Boss 2026-07-10）。USD は出口レート。
            from shared.forex import FX_SYMBOLS, nst_monthly_rates, usd_export_rate
            _CUR_OPTS = [("KRW", "₩ 韩元（原币）", "₩ ウォン（原貨）"),
                         ("JPY", "¥ 日元", "¥ 円"),
                         ("USD", "$ 美元", "$ ドル")]
            _cur_lbls = [_dl(z, j) for _, z, j in _CUR_OPTS]
            _cur_pick = st.radio(_dl("展示货币", "表示通貨"), _cur_lbls,
                                 horizontal=True, key="cp_ded_cur")
            _cur = _CUR_OPTS[_cur_lbls.index(_cur_pick)][0]
            _sym = FX_SYMBOLS[_cur]
            if _cur != "KRW":
                _rate_yms = sorted(set(cdf["ym"].astype(str)) | {str(ym)})
                _krw_ym = nst_monthly_rates(conn, "KRW", _rate_yms)   # 1 KRW = X 円
                _fx_ym = (_krw_ym if _cur == "JPY" else
                          {_y: _krw_ym[_y] / usd_export_rate(_y) for _y in _rate_yms})
                _fxs = cdf["ym"].astype(str).map(_fx_ym)
                for _c in ["total_sale", "settlement_amount", "pending_released_amount",
                           "last_amount", "final_amount", "deduction_total"] + _ded_cols:
                    cdf[_c] = cdf[_c] * _fxs
                _usd_zh = (f" · 1 USD = {usd_export_rate(str(ym)):.0f} 円（出口汇率）"
                           if _cur == "USD" else "")
                _usd_ja = (f" · 1 USD = {usd_export_rate(str(ym)):.0f} 円（輸出レート）"
                           if _cur == "USD" else "")
                st.caption(_dl(
                    f"按各月当月三金汇率换算 · {ym}：1 KRW = {_krw_ym[str(ym)]:g} 円{_usd_zh}"
                    "（nst.currency_rate · 当月无更新沿用上月）",
                    f"各月その月の三金レートで換算 · {ym}：1 KRW = {_krw_ym[str(ym)]:g} 円{_usd_ja}"
                    "（nst.currency_rate · 当月更新なしは前月沿用）"))

            # ── 対象月 = ページ上部「対象月」に追従（Boss 2026-07-07: 月選択の重複排除）──
            _yms = cdf["ym"].drop_duplicates().tolist()   # DESC 済（トレンド用）
            sub = cdf[cdf["ym"] == ym]

            if sub.empty:
                st.info(
                    (f"{ym} の Coupang 結算単はまだ生成されていません（売上認識月の終了後に生成）。"
                     "下のトレンドは全期間を表示しています。") if _ja else
                    (f"{ym} 的 Coupang 结算单尚未生成（认知月结束后由 Coupang 出单）。"
                     "下方趋势图仍显示全期间。"))
            else:
                # ── KPI（該当月の全結算単合算）──
                _k_sale = sub["total_sale"].sum()
                _k_ded = sub["deduction_total"].sum()
                _k_fin = sub["final_amount"].sum()
                _k_rate = (_k_ded / _k_sale * 100) if _k_sale else 0
                k1, k2, k3, k4 = st.columns(4)
                k1.metric(_dl("实际销售额", "実際販売額"), f"{_sym}{_k_sale:,.0f}")
                k2.metric(_dl("扣减合计", "控除合計"), f"{_sym}{_k_ded:,.0f}")
                k3.metric(_dl("扣减率", "控除率"), f"{_k_rate:.2f}%")
                k4.metric(_dl("最终支付额", "最終支払額"), f"{_sym}{_k_fin:,.0f}")

                # ── 該当月の結算単一覧（型 / 結算日 / 状態 / 金額）──
                _rows = []
                for _, r in sub.iterrows():
                    _ty = _TYPE_LBL.get(r["settlement_type"],
                                        (r["settlement_type"], r["settlement_type"]))
                    _stt = _ST_LBL.get(r["status"], (r["status"] or "-",) * 2)
                    _rows.append({
                        _dl("结算类型", "結算タイプ"): _dl(*_ty),
                        _dl("结算日", "支払日"): str(r["settlement_date"]),
                        _dl("实际销售额", "実際販売額"): f"{_sym}{r['total_sale']:,.0f}",
                        _dl("扣减合计", "控除合計"): f"{_sym}{r['deduction_total']:,.0f}",
                        _dl("最终支付额", "最終支払額"): f"{_sym}{r['final_amount']:,.0f}",
                        _dl("状态", "状態"): _dl(*_stt),
                    })
                html_table(pd.DataFrame(_rows))

                # ── 扣減構成明細（該当月合算 · 非ゼロのみ · 占比付き）──
                st.markdown("##### " + _dl("🧾 扣减构成明细", "🧾 控除内訳"))
                _items = []
                for col, zh, ja in _DED_ITEMS:
                    amt = sub[col].sum()
                    if amt == 0:
                        continue
                    _items.append({
                        _dl("扣减项目", "控除項目"): _dl(zh, ja),
                        _dl("金额", "金額"): f"{_sym}{amt:,.0f}",
                        _dl("占实际销售额", "対販売額比"):
                            f"{(amt / _k_sale * 100) if _k_sale else 0:.2f}%",
                    })
                if _items:
                    html_table(pd.DataFrame(_items))
                else:
                    st.info(_dl("该月无任何扣减项", "当月に控除項目はありません"))

            # ── 近 12 ヶ月トレンド（扣減構成 積上げ + 明細表）──
            st.markdown("##### " + _dl("📊 近12个月扣减趋势", "📊 直近12ヶ月の控除トレンド"))
            _m12 = sorted(_yms)[-12:]
            hist = cdf[cdf["ym"].isin(_m12)]
            _long = []
            for col, zh, ja in _DED_ITEMS:
                g = hist.groupby("ym", as_index=False)[col].sum()
                g = g[g[col] != 0]
                for _, r in g.iterrows():
                    _long.append({"ym": r["ym"], "item": _dl(zh, ja), "amount": float(r[col])})
            if _long:
                _ldf = pd.DataFrame(_long)
                st.altair_chart(
                    alt.Chart(_ldf).mark_bar().encode(
                        x=alt.X("ym:N", title=None, axis=alt.Axis(labelAngle=0)),
                        y=alt.Y("amount:Q", title=_dl(f"扣减 ({_cur})", f"控除 ({_cur})")),
                        color=alt.Color("item:N", title=None,
                                        legend=alt.Legend(orient="top")),
                        tooltip=[
                            alt.Tooltip("ym:N", title=_dl("月", "月")),
                            alt.Tooltip("item:N", title=_dl("扣减项目", "控除項目")),
                            alt.Tooltip("amount:Q", title=_dl("金额", "金額"), format=",.0f"),
                        ],
                    ).properties(height=280)
                    .configure_axis(labelFontSize=_CHART_LABEL_FS,
                                    titleFontSize=_CHART_TITLE_FS),
                    use_container_width=True,
                )
            _mg = hist.groupby("ym", as_index=False).agg(
                total_sale=("total_sale", "sum"),
                deduction_total=("deduction_total", "sum"),
                final_amount=("final_amount", "sum"),
            ).sort_values("ym", ascending=False)
            _mg["rate"] = (_mg["deduction_total"]
                           / _mg["total_sale"].where(_mg["total_sale"] != 0)).fillna(0) * 100
            html_table(pd.DataFrame({
                _dl("销售认知月", "売上認識月"): _mg["ym"],
                _dl("实际销售额", "実際販売額"): _mg["total_sale"].apply(lambda x: f"{_sym}{x:,.0f}"),
                _dl("扣减合计", "控除合計"): _mg["deduction_total"].apply(lambda x: f"{_sym}{x:,.0f}"),
                _dl("扣减率", "控除率"): _mg["rate"].apply(lambda x: f"{x:.2f}%"),
                _dl("最终支付额", "最終支払額"): _mg["final_amount"].apply(lambda x: f"{_sym}{x:,.0f}"),
            }))


# ============================================================
# Tab 7：拨款明细（注文 1 件 = 1 行 · 官方「已完成拨款」相当）
# ============================================================
with tab_payout:
    _pj = get_lang() == "ja"

    def _pl(zh: str, ja: str) -> str:
        return ja if _pj else zh

    st.caption(t(
        "Shopee 各笔拨款包含的订单明细（现金主义）· 与 Shopee 后台「我的收入 > 已完成拨款 > 导出」同口径 · "
        "金额为当地币原值 · 费用为空表示该单明细尚未取得"
    ))

    @st.cache_data(ttl=300, show_spinner=False)
    def _po_ver() -> str:
        try:
            row = get_readonly_connection().execute(
                "SELECT max(finished_at) FROM shopee.pull_log").fetchone()
            if row and row[0]:
                return str(row[0])
        except Exception:
            pass
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H")

    def _poq(sql: str, params: tuple = ()):
        try:
            from shared.cache import cached_df
            return cached_df(conn, sql, params or None, ver=_po_ver()), None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return None, str(e)

    _po_opt, _po_err = _poq(
        "SELECT DISTINCT payout_date, country_code, shop_key, shop_name "
        "FROM shopee.v_payout_order ORDER BY payout_date DESC, shop_key")

    if _po_err:
        st.error(t("shopee 财务视图未取得 or 连接错误: ") + _po_err)
    elif _po_opt is None or _po_opt.empty:
        st.info(t("Shopee 拨款数据为空 · 请在 page 27 手动同步或等待每日自动拉取"))
    else:
        c1, c2 = st.columns([1, 2])
        _dates = _po_opt["payout_date"].dropna().unique().tolist()
        _sel_d = c1.selectbox(_pl("拨款日", "入金日"), _dates, key="po_date")
        _shops = _po_opt[_po_opt["payout_date"] == _sel_d]
        _shop_labels = {
            f"{r['country_code']} · {r['shop_name'] or r['shop_key']}": r["shop_key"]
            for _, r in _shops.iterrows()}
        _sel_s = c2.multiselect(_pl("店铺（空=全部）", "店舗（空=全部）"),
                                list(_shop_labels), key="po_shop")
        _keys = [_shop_labels[k] for k in _sel_s] or list(_shop_labels.values())

        _ph = ",".join(["%s"] * len(_keys))
        _po, _e2 = _poq(
            "SELECT country_code, shop_name, currency, payout_date, order_sn, "
            "order_create_time, buyer_payment_method, order_original_price, "
            "order_seller_discount, voucher_from_seller, buyer_paid_shipping_fee, "
            "shopee_shipping_rebate, actual_shipping_fee, commission_fee, "
            "service_fee, seller_transaction_fee, credit_card_transaction_fee, "
            "order_ams_commission_fee, seller_return_refund, payout_amount, "
            "fee_missing FROM shopee.v_payout_order "
            f"WHERE payout_date = %s AND shop_key IN ({_ph}) ORDER BY order_sn",
            tuple([_sel_d] + _keys))

        if _e2:
            st.error(_e2)
        elif _po is None or _po.empty:
            st.info(t("该拨款日无明细"))
        else:
            _miss = int(_po["fee_missing"].sum())
            m1, m2, m3 = st.columns(3)
            m1.metric(_pl("订单数", "注文数"), f"{len(_po):,}")
            m2.metric(_pl("拨款合计（当地币）", "入金合計（現地通貨）"),
                      f"{_po['payout_amount'].sum():,.0f}")
            # 取得できていない明細を必ず件数で見せる（黙って 0 円扱いしない）
            m3.metric(_pl("费用明细缺失", "費用内訳の欠落"), f"{_miss:,}",
                      delta=None if not _miss else _pl("需补拉", "要再取得"),
                      delta_color="inverse")

            _view = _po.drop(columns=["fee_missing"])
            st.dataframe(_view, use_container_width=True, height=460,
                         hide_index=True)
            st.download_button(
                _pl("⬇️ 下载 CSV（本次拨款全明细）", "⬇️ CSV ダウンロード"),
                _view.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"shopee_payout_{_sel_d}.csv", mime="text/csv",
                key="po_dl")


st.divider()
st.caption(
    t("対象月") + f"：{ym} · " + t("市場") + f"：{mk} · "
    + t("表示行（明細）: ") + f"{len(df):,}"
)
