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
 tab_payout, tab_loss, tab_ff3) = st.tabs(
    [t("📈 月内日次推移"), _owner_tab, t("🏪 店舗別"), t("🌐 市場別"),
     t("🏆 TOP SKU"), t("⚠️ 价格预警"), t("🧾 店铺扣减"), t("💵 拨款明细"),
     t("🩸 未结算/损失"), t("💸 退款明细")]
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
    # key は中文に統一（旧版は 1 文字列に中日混在で、どちらの言語でも読めなかった）
    st.caption(t(
        "把各店「当日」(最新确认日) 与 近7日平均 对比，检出毛利异常 · "
        "🔴=规则1/2/3(毛利侵蚀 / 降价冲量 / 破红线) · 🟡=规则4(收益涨了但毛利率跌) · 阈值可在下方调整"
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

        # 発火した規則名。t() を通していなかったため ja 表示でも中文が出ていた
        _RULE_LBL = {pa.R1: t("①跌破7日均"), pa.R2: t("②降价冲量"),
                     pa.R3: t("③破红线"), pa.R4: t("④收益涨毛利没跟")}
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
    # 出荷日基準の損益（市場 → 国 → 店舗）
    #   JO 2026-07-29:「NST の注文番号を基準に、入金も注文番号で突合する。
    #                   判断基準は出荷日の注文番号だけ」
    #   nst.v_shipped_settlement は NST 請求書の注文番号を展開して
    #   プラットフォーム側の手数料・入金を結んだもの。すべて出荷日で括る。
    # ============================================================
    _ja0 = get_lang() == "ja"

    def _u(zh: str, ja: str) -> str:
        return ja if _ja0 else zh

    st.caption(t(
        "以【发货日】为唯一基准 · NST 销售额（发货日）× 平台手续费/回款（按订单号匹配）· 日元 · "
        "「费用匹配率」= 该店发货订单中能在平台侧查到费用明细的比例，低于 100% 时扣减偏小、净利估偏高 · "
        "「回款率」= 已到账订单比例，发货后拨款需 1~2 周，故月内偏低属正常"
    ))

    @st.cache_data(ttl=300, show_spinner=False)
    def _ship_ver() -> str:
        vs = []
        for sql in ("SELECT max(pulled_at) FROM nst.sales_invoice",
                    "SELECT max(finished_at) FROM shopee.pull_log"):
            try:
                row = get_readonly_connection().execute(sql).fetchone()
                if row and row[0]:
                    vs.append(str(row[0]))
            except Exception:
                pass
        return "|".join(vs) or dt.datetime.now(
            dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H")

    def _shq(sql: str):
        try:
            from shared.cache import cached_df
            return cached_df(conn, sql, None, ver=_ship_ver()), None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return None, str(e)

    _sh, _sh_err = _shq(
        "SELECT ym, market, platform, country, shop, invoices, orders, "
        "matched_orders, settled_orders, matched_pct, settled_pct, "
        "sales_jpy, deduction_jpy, shipping_jpy, payout_jpy, "
        "gross_profit_jpy, revenue_jpy, gross_margin_pct, "
        "deduction_pct_with_shipping, "
        "net_margin_est_pct "
        f"FROM nst.v_shipped_settlement WHERE ym = '{ym}'")

    if _sh_err:
        st.error(t("发货日基准视图未取得或连接错误: ") + _sh_err)
        st.info(t("需在元川 PG 执行 nst_api/sql/023,024 并跑 pull_sales_invoice"))
    elif _sh is None or _sh.empty:
        st.info(t("当月无发货数据 · 请先跑 nst_api.pull_sales_invoice"))
    else:
        d = _sh.copy()
        # PG の NUMERIC は decimal.Decimal で載るため演算前に float 化
        for c in ("invoices", "orders", "matched_orders", "settled_orders",
                  "matched_pct", "settled_pct", "sales_jpy", "revenue_jpy", "deduction_jpy",
                  "shipping_jpy", "payout_jpy", "gross_profit_jpy",
                  "gross_margin_pct", "deduction_pct_with_shipping",
                  "net_margin_est_pct"):
            d[c] = pd.to_numeric(d[c], errors="coerce").astype(float).fillna(0)
        d["ded_total"] = d["deduction_jpy"] + d["shipping_jpy"]

        if mk:
            from shared.markets import MARKET_KOREA as _MKK, MARKET_SEA as _MKS
            _keep = set()
            if _MKS in mk:
                _keep.add("ASEAN")
            if _MKK in mk:
                _keep.add("KOREA")
            if _keep:
                d = d[d["market"].isin(_keep)]

        if d.empty:
            st.info(t("当前市场筛选下无数据"))
        else:
            def _y(v) -> str:
                return f"¥{v:,.0f}"

            def _pct(num, den) -> str:
                return f"{(num / den * 100):.1f}%" if den else "—"

            _tot = d[["sales_jpy", "revenue_jpy", "ded_total", "payout_jpy",
                      "gross_profit_jpy"]].sum()
            # ⚠️ 率は **分子と分母を同じ表から** 取る。
            #    粗利は nst.sales_daily 由来 → 分母も sales_daily の revenue_jpy。
            #    控除は請求書額に対する割合 → 分母は sales_jpy（請求書由来）。
            #    混ぜると 2026-06 で粗利率が 59.57% → 60.77% に虚高した（JO 指摘）。
            _gm = _tot["gross_profit_jpy"] / _tot["revenue_jpy"] * 100 \
                if _tot["revenue_jpy"] else None
            _dr = _tot["ded_total"] / _tot["sales_jpy"] * 100 \
                if _tot["sales_jpy"] else None
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(_u("销售额（出荷日）", "売上（出荷日）"), _y(_tot["sales_jpy"]))
            k2.metric(_u("扣减合计", "控除合計"), _y(_tot["ded_total"]),
                      _pct(_tot["ded_total"], _tot["sales_jpy"]), delta_color="inverse")
            k3.metric(_u("粗利率", "粗利率"),
                      f"{_gm:.2f}%" if _gm is not None else "—",
                      _u("分母=NST 日次売上", "分母=NST 日次売上"), delta_color="off")
            # 净利估 = 粗利率 − 控除率。それぞれ自分の分母で出した率を引き算する
            k4.metric(_u("净利估", "純利益(推定)"),
                      f"{_gm - _dr:.1f}%" if (_gm is not None and _dr is not None) else "—")

            # 売上が 2 つあるのは正常。理由を出しておかないと「どちらかが壊れている」と読まれる
            _gap = float(_tot["revenue_jpy"] - _tot["sales_jpy"])
            if abs(_gap) > 1000:
                st.caption(_u(
                    f"ℹ️ 上方 KPI 的销售额（NST 日次 {_y(_tot['revenue_jpy'])}）与本 tab"
                    f"（请求书 {_y(_tot['sales_jpy'])}）差 {_y(_gap)} · 两者都对，口径不同："
                    "① 贷方票（退货冲销）只进请求书侧 ② 日次是 sale_date、请求书是 shipdate，"
                    "跨月的部分会落在不同月（符号按月反转）· 粗利率用日次侧分母，扣减率用请求书侧分母",
                    f"ℹ️ 上部 KPI の売上（NST 日次 {_y(_tot['revenue_jpy'])}）と本タブ"
                    f"（請求書 {_y(_tot['sales_jpy'])}）の差 {_y(_gap)} は正常です："
                    "① 貸方票（返品の取消伝票）は請求書側にのみ入る ② 日次は sale_date、"
                    "請求書は shipdate なので月をまたぐ分がズレる（符号は月により反転）· "
                    "粗利率は日次側、控除率は請求書側の分母で算出しています"))

            # 突合率が低い店は手数料が過少に出る → 数字を信じてよいか一目で分かるように
            _low = d[(d["orders"] > 0) & (d["matched_pct"] < 90)]
            if not _low.empty:
                st.warning(_u(
                    f"⚠️ {len(_low)} 家店铺的费用匹配率不足 90%"
                    f"（{', '.join(_low.nlargest(3,'sales_jpy')['shop'].tolist())} 等）· "
                    "这些店的扣减偏小、净利估偏高 · 补齐: pull_escrow --country <店> --since/--until",
                    f"⚠️ 突合率 90% 未満の店舗が {len(_low)} 件あります（控除が過少・純利が過大に出ます）"))

            _DL = _u("⬇️ 下载 CSV", "⬇️ CSV ダウンロード")

            def _dl(df_, name, key):
                st.download_button(_DL, df_.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=name, mime="text/csv", key=key)

            _AGG = {"orders": "sum", "sales_jpy": "sum", "ded_total": "sum",
                    "payout_jpy": "sum", "gross_profit_jpy": "sum",
                    "matched_orders": "sum", "settled_orders": "sum"}

            def _rows(g, cols):
                out = []
                for _, r in g.iterrows():
                    row = {lbl: r[c] for c, lbl in cols}
                    row.update({
                        _u("订单数", "注文数"): f"{int(r['orders']):,}",
                        _u("销售额", "売上"): _y(r["sales_jpy"]),
                        _u("扣减合计", "控除合計"): _y(r["ded_total"]),
                        _u("扣减率", "控除率"): _pct(r["ded_total"], r["sales_jpy"]),
                        # 分子(sales_daily)と分母(sales_daily)を揃える
                        _u("粗利率", "粗利率"): _pct(r["gross_profit_jpy"],
                                                   r["revenue_jpy"]),
                        _u("净利估", "純利(推定)"):
                            # 净利估 = 粗利率 − 控除率（各自の分母で出した率の差）
                            f"{r['gross_profit_jpy'] / r['revenue_jpy'] * 100 - r['ded_total'] / r['sales_jpy'] * 100:.1f}%"
                            if r["revenue_jpy"] and r["sales_jpy"] else "—",
                        _u("已回款", "入金済"): _y(r["payout_jpy"]),
                        _u("回款率", "入金済率"):
                            f"{(r['settled_orders'] / r['orders'] * 100):.0f}%" if r["orders"] else "—",
                        _u("费用匹配率", "突合率"):
                            f"{(r['matched_orders'] / r['orders'] * 100):.0f}%" if r["orders"] else "—",
                    })
                    out.append(row)
                return pd.DataFrame(out)

            st.markdown("**" + _u("平台", "プラットフォーム") + "**")
            _g1 = d.groupby("platform", as_index=False).agg(_AGG).sort_values("sales_jpy", ascending=False)
            html_table(_rows(_g1, [("platform", _u("平台", "プラットフォーム"))]))
            _dl(_g1, f"ship_platform_{ym}.csv", "sh_dl_m")

            st.markdown("**" + _u("国家", "国別") + "**")
            _g2 = (d.groupby(["platform", "country"], as_index=False).agg(_AGG)
                   .sort_values("sales_jpy", ascending=False))
            html_table(_rows(_g2, [("platform", _u("平台", "プラットフォーム")),
                                   ("country", _u("国家", "国"))]))
            _dl(_g2, f"ship_country_{ym}.csv", "sh_dl_c")

            st.markdown("**" + _u("店铺", "店舗") + "**")
            _g3 = d.sort_values("sales_jpy", ascending=False)
            html_table(_rows(_g3, [("platform", _u("平台", "プラットフォーム")),
                                   ("country", _u("国家", "国")),
                                   ("shop", _u("店铺", "店舗"))]))
            _dl(_g3, f"ship_shop_{ym}.csv", "sh_dl_s")


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
        st.error(t("Shopee 财务视图未取得或连接错误: ") + _po_err)
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
                      f"{pd.to_numeric(_po['payout_amount'], errors='coerce').sum():,.0f}")
            # 取得できていない明細を必ず件数で見せる（黙って 0 円扱いしない）
            m3.metric(_pl("费用明细缺失", "費用内訳の欠落"), f"{_miss:,}",
                      delta=None if not _miss else _pl("需补拉", "要再取得"),
                      delta_color="inverse")

            _view = _po.drop(columns=["fee_missing"]).copy()
            # Decimal のままだと Arrow 変換や書式化で落ちるため float 化
            for _c in _view.columns:
                if _c not in ("country_code", "shop_name", "currency", "payout_date",
                              "order_sn", "buyer_payment_method", "order_create_time"):
                    _view[_c] = pd.to_numeric(_view[_c], errors="coerce").astype(float)
            st.dataframe(_view, use_container_width=True, height=460,
                         hide_index=True)
            st.download_button(
                _pl("⬇️ 下载 CSV（本次拨款全明细）", "⬇️ CSV ダウンロード"),
                _view.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"shopee_payout_{_sel_d}.csv", mime="text/csv",
                key="po_dl")


# ============================================================
# Tab 8：未结算/损失（発送したのに入金が立たない注文）
#   JO 2026-07-29:「0 で計上されているのは返金や紛失で結算されなかった可能性。
#                   これは当社の純損失。KPI にして一覧も出す」
#   nst.v_shipped_order が注文 1 件 = 1 行で状態を 4 つに分類する:
#     settled 入金済 / pending 未入金(正常) / zero 実収≤0 / unmatched 明細に無い
# ============================================================
with tab_loss:
    _lj = get_lang() == "ja"

    def _ll(zh: str, ja: str) -> str:
        return ja if _lj else zh

    st.caption(t(
        "发货了但没收到钱的订单 · 以【发货日】为基准 · "
        "判定分界是**货有没有出日本**（物流状态到「已揽收」以后就算出了）· "
        "「损失」= 出货后被取消 / 退款 / 实收≤0 → 商品和运费都回不来，这才是真损失 · "
        "「出货前取消」= 还没揽收就取消 → 库存能回收，**不计入损失**，单独看数量就行 · "
        "「未匹配」= 平台明细里没有这个订单号 → 三种原因混在一起（拉取范围没覆盖 / 平台确认周期未到 / 该平台没接明细API），"
        "要先按平台排除才能谈损失 · 「金额缺失」= 匹配上了、既没取消也查不到金额 → 数据缺口，需补拉 · "
        "「未入金」是发货后正常的等待期，不算损失"
    ))

    def _lq(sql: str):
        try:
            from shared.cache import cached_df
            return cached_df(conn, sql, None, ver=_ship_ver()), None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return None, str(e)

    _lo, _lo_err = _lq(
        "SELECT ym, ship_date, market, platform, country, shop, order_no, "
        "invoice_no, order_ym_prefix, settle_status, order_status, "
        "logistics_status, shipped_out, cancel_reason, refund_reason, "
        "refund_status, refund_amount_local, "
        "gmv_local, received_local, gmv_jpy, received_jpy, nst_amount_jpy "
        f"FROM nst.v_shipped_order WHERE ym = '{ym}'")

    if _lo_err:
        st.error(t("订单级视图未取得或连接错误: ") + _lo_err)
        st.info(t("需在元川 PG 执行 nst_api/sql/025_create_shipped_order_view.sql"))
    elif _lo is None or _lo.empty:
        st.info(t("当月无发货数据 · 请先跑 nst_api.pull_sales_invoice"))
    else:
        L = _lo.copy()
        for c in ("gmv_local", "received_local", "gmv_jpy", "received_jpy",
                  "nst_amount_jpy"):
            L[c] = pd.to_numeric(L[c], errors="coerce").astype(float).fillna(0)

        if mk:
            from shared.markets import MARKET_KOREA as _MK2, MARKET_SEA as _MS2
            _kp = set()
            if _MS2 in mk:
                _kp.add("ASEAN")
            if _MK2 in mk:
                _kp.add("KOREA")
            if _kp:
                L = L[L["market"].isin(_kp)]

    if _lo is not None and not _lo.empty and L.empty:
        # 市場フィルタで全部落ちた場合。この先の groupby / 除算が壊れる
        st.info(t("当前市场筛选下无数据"))

    if _lo is not None and not _lo.empty and not L.empty:
        # 損失は **出荷後のみ**（JO 2026-07-30）。出荷前の取消は在庫が戻るので
        # 金額を損失に積まない —— 積むと 7 月実測で 2.85 倍に膨らむ
        _LOSS_ST = ("loss",)
        L["loss_jpy"] = L.apply(
            lambda r: r["nst_amount_jpy"] if r["settle_status"] == "unmatched"
            else (r["gmv_jpy"] if r["settle_status"] in _LOSS_ST else 0.0), axis=1)

        _loss = L[L["settle_status"] == "loss"]
        _pre = L[L["settle_status"] == "preship_void"]
        _unm = L[L["settle_status"] == "unmatched"]
        _noamt = L[L["settle_status"] == "no_amount"]
        _risk = L[L["settle_status"].isin(_LOSS_ST + ("unmatched",))]
        _n_all = len(L)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric(_ll("🩸 损失（出货后）", "🩸 損失（出荷後）"),
                  f"{len(_loss):,}", f"¥{_loss['loss_jpy'].sum():,.0f}",
                  delta_color="inverse")
        k2.metric(_ll("↩️ 出货前取消（不算损失）", "↩️ 出荷前の取消（損失外）"),
                  f"{len(_pre):,}",
                  _ll("库存可回收", "在庫は戻る"), delta_color="off")
        k3.metric(_ll("损失率（对发货单）", "損失率（出荷注文比）"),
                  f"{(len(_loss) / _n_all * 100):.2f}%" if _n_all else "—",
                  f"{len(_loss):,} / {_n_all:,}", delta_color="off")
        k4.metric(_ll("已入金 / 发货订单", "入金済 / 出荷注文"),
                  f"{len(L[L['settle_status'] == 'settled']):,} / {_n_all:,}",
                  None, delta_color="off")

        if len(_noamt):
            st.caption(_ll(
                f"🧩 另有 {len(_noamt):,} 单匹配上了、但既没取消也查不到金额 → 数据缺口，"
                "补拉 escrow/logistics 明细后才能定性",
                f"🧩 他に {len(_noamt):,} 件、突合済みだが取消でも金額取得済でもない注文があります"
                "（データ欠落 · 明細の再取得が必要）"))

        # 未匹配は原因が 3 つ混在する。プラットフォーム別に出さないと誤読される
        if not _unm.empty:
            _up = (_unm.groupby("platform", as_index=False)
                   .agg(orders=("order_no", "nunique"), jpy=("loss_jpy", "sum")))
            _base = L.groupby("platform", as_index=False).agg(all_o=("order_no", "nunique"))
            _up = _up.merge(_base, on="platform", how="left")
            _lines = []
            for _, r in _up.sort_values("orders", ascending=False).iterrows():
                _rate = r["orders"] / r["all_o"] * 100 if r["all_o"] else 0
                if _rate > 95:
                    _why = _ll("该平台明细 API 未接入 → 全部未匹配是必然，不是损失",
                               "明細 API 未接続のため全件が未突合になります（損失ではありません）")
                elif r["platform"] == "Coupang":
                    _why = _ll("Coupang 的销售确认日在配送完成后才生成 → 本月发货的下月才确认，属结构性滞后",
                               "売上認識日は配送完了後に立つため、構造的なラグです")
                else:
                    _why = _ll("多为跨月下单，费用明细只拉了本月 → 回填上月即可消除",
                               "前月の注文が多く、費用明細を当月分しか取得していません → 前月を回填すれば解消します")
                _lines.append(f"- **{r['platform']}** {int(r['orders']):,} "
                              + _ll("单", "件") + f"（¥{r['jpy']:,.0f}）· {_why}")
            st.info(_ll("**未匹配 ≠ 损失。按平台拆开看原因：**\n",
                       "**未突合 ≠ 損失です。プラットフォーム別に原因を切り分けます：**\n")
                    + "\n".join(_lines))

        if _risk.empty:
            st.success(_ll("✅ 本月发货订单全部有结算记录，无损失候选",
                           "✅ 当月の出荷注文はすべて精算済みです"))
        else:
            _LDL = _ll("⬇️ 下载 CSV", "⬇️ CSV ダウンロード")

            # 集計表は **確定損失（取消 + 実収≤0）のみ**。unmatched を混ぜると
            # 未接続プラットフォームの件数に埋もれ、実際に金を落としている店舗が消える
            def _grp(keys, label_cols, key):
                g = (_loss.groupby(keys, as_index=False)
                     .agg(orders=("order_no", "nunique"),
                          loss_jpy=("loss_jpy", "sum"))
                     .sort_values("loss_jpy", ascending=False))
                if g.empty:
                    st.caption(_ll("（无确认损失订单）", "（損失確定の注文なし）"))
                    return
                # 分母は同じ切り口の全出荷注文（率で読めるように）
                base = (L.groupby(keys, as_index=False)
                        .agg(all_orders=("order_no", "nunique")))
                g = g.merge(base, on=keys, how="left")
                rows = []
                for _, r in g.iterrows():
                    row = {lbl: r[c] for c, lbl in label_cols}
                    row.update({
                        _ll("损失订单", "損失注文"): f"{int(r['orders']):,}",
                        _ll("发货订单", "出荷注文"): f"{int(r['all_orders']):,}",
                        _ll("发生率", "発生率"):
                            f"{(r['orders'] / r['all_orders'] * 100):.2f}%"
                            if r["all_orders"] else "—",
                        _ll("损失额(估算)", "損失額(推定)"): f"¥{r['loss_jpy']:,.0f}",
                    })
                    rows.append(row)
                # 画面と CSV の列見出しを揃える（英語の生列名で落とさない）
                _tbl = pd.DataFrame(rows)
                html_table(_tbl)
                st.download_button(_LDL, _tbl.to_csv(index=False).encode("utf-8-sig"),
                                   file_name=f"loss_{key}_{ym}.csv",
                                   mime="text/csv", key=f"loss_dl_{key}")

            # 損失の内訳。7 月実測では「送達失敗」1 項目に 63% が集中していた。
            # 店舗別より先にこれを出す —— 打つ手が変わるのは理由の側だから
            st.markdown("**" + _ll("损失原因构成（出货后）", "損失の内訳（出荷後）") + "**")
            _LS = {"LOGISTICS_DELIVERY_FAILED": _ll("送达失败", "配達失敗"),
                   "LOGISTICS_DELIVERY_DONE": _ll("已送达", "配達完了"),
                   "LOGISTICS_PICKUP_DONE": _ll("已揽收在途", "集荷済・輸送中"),
                   "LOGISTICS_LOST": _ll("丢件", "紛失")}
            _RS = {"ITEM_MISSING": _ll("少件", "商品不足"),
                   "WRONG_ITEM": _ll("发错货", "商品違い"),
                   "SPILLED_CONTENTS": _ll("内容物漏出", "内容物漏れ"),
                   "NOT_RECEIPT": _ll("买家称未收到", "未受領の申告"),
                   "DAMAGED_OTHERS": _ll("破损", "破損"),
                   "FUNCTIONAL_DMG": _ll("功能损坏", "機能不良"),
                   "SUSPICIOUS_PARCEL": _ll("包裹异常", "不審な小包"),
                   "EXPIRED_PRODUCT": _ll("过期品", "期限切れ"),
                   "SLIGHT_SCRATCH_DENTS": _ll("轻微划痕/凹陷", "軽微な傷・凹み"),
                   "CHANGE_MIND": _ll("买家改变主意", "気が変わった"),
                   "EXPECTATION_FAILED": _ll("与预期不符", "期待と違う")}
            _cz = _loss.copy()
            _cz["_ls"] = _cz["logistics_status"].map(
                lambda x: _LS.get(x, x or "—"))
            _cz["_rs"] = _cz["refund_reason"].map(
                lambda x: _RS.get(x, x) if x else _ll("无退款申请", "返金申請なし"))
            _cg = (_cz.groupby(["_ls", "_rs"], as_index=False)
                   .agg(orders=("order_no", "nunique"), jpy=("loss_jpy", "sum"))
                   .sort_values("jpy", ascending=False))
            _tot = _cg["jpy"].sum()
            # 画面と CSV は同じ表から作る（片方だけ翻訳すると CSV が _ls/_rs/jpy になる）
            _rt = pd.DataFrame({
                _ll("物流状态", "配送状態"): _cg["_ls"],
                _ll("退款原因", "返金理由"): _cg["_rs"],
                _ll("订单", "注文"): _cg["orders"].map(lambda v: f"{int(v):,}"),
                _ll("损失额", "損失額"): _cg["jpy"].map(lambda v: f"¥{v:,.0f}"),
                _ll("占比", "構成比"): _cg["jpy"].map(
                    lambda v: f"{v / _tot * 100:.1f}%" if _tot else "—"),
            })
            html_table(_rt)
            st.download_button(
                _LDL, _rt.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"loss_reason_{ym}.csv", mime="text/csv",
                key="loss_dl_reason")

            st.markdown("**" + _ll("确认损失 · 平台", "損失確定 · プラットフォーム") + "**")
            _grp(["platform"], [("platform", _ll("平台", "プラットフォーム"))], "platform")

            st.markdown("**" + _ll("确认损失 · 国家", "損失確定 · 国別") + "**")
            _grp(["platform", "country"],
                 [("platform", _ll("平台", "プラットフォーム")),
                  ("country", _ll("国家", "国"))], "country")

            st.markdown("**" + _ll("确认损失 · 店铺", "損失確定 · 店舗") + "**")
            _grp(["platform", "country", "shop"],
                 [("platform", _ll("平台", "プラットフォーム")),
                  ("country", _ll("国家", "国")),
                  ("shop", _ll("店铺", "店舗"))], "shop")

            st.markdown("**" + _ll("明细清单", "明細一覧") + "**")
            _STATUS = {"loss": _ll("损失（出货后）", "損失（出荷後）"),
                       "preship_void": _ll("出货前取消（不算损失）", "出荷前の取消（損失外）"),
                       "unmatched": _ll("未匹配（平台无此单）", "未突合"),
                       "no_amount": _ll("金额缺失（数据缺口）", "金額欠落")}
            # options は状態コード（ラベルを options にすると言語切替で選択が消える）
            _sel = st.multiselect(
                _ll("状态", "状態"), options=list(_STATUS.keys()),
                default=["loss"], format_func=lambda k: _STATUS[k],
                key="loss_status")
            _det = L[L["settle_status"].isin(_sel)].copy()
            _det["settle_status"] = _det["settle_status"].map(_STATUS)
            _det["logistics_status"] = _det["logistics_status"].map(
                lambda x: _LS.get(x, x or "—"))
            _det["refund_reason"] = _det["refund_reason"].map(
                lambda x: _RS.get(x, x) if x else "—")
            _det = (_det[["ship_date", "platform", "country", "shop", "order_no",
                          "invoice_no", "settle_status", "logistics_status",
                          "refund_reason", "cancel_reason", "gmv_local",
                          "received_local", "loss_jpy"]]
                    .rename(columns={
                        "ship_date": _ll("发货日", "出荷日"),
                        "platform": _ll("平台", "プラットフォーム"),
                        "country": _ll("国家", "国"),
                        "shop": _ll("店铺", "店舗"),
                        "order_no": _ll("订单号", "注文番号"),
                        "invoice_no": _ll("NST 请求书", "NST 請求書"),
                        "settle_status": _ll("状态", "状態"),
                        "logistics_status": _ll("物流状态", "配送状態"),
                        "refund_reason": _ll("退款原因", "返金理由"),
                        "cancel_reason": _ll("取消原因", "取消理由"),
                        "gmv_local": _ll("订单金额(当地币)", "注文金額(現地)"),
                        "received_local": _ll("实收(当地币)", "実収(現地)"),
                        "loss_jpy": _ll("损失额(估算 ¥)", "損失額(推定 ¥)")})
                    .sort_values(_ll("损失额(估算 ¥)", "損失額(推定 ¥)"),
                                 ascending=False))
            st.dataframe(_det, use_container_width=True, height=460,
                         hide_index=True)
            st.download_button(
                _ll("⬇️ 下载 CSV（损失订单全明细）", "⬇️ CSV ダウンロード"),
                _det.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"loss_orders_{ym}.csv", mime="text/csv",
                key="loss_dl_detail")


# ============================================================
# Tab 9：退款明细 / 返金詳細（出荷後に入金が立たなかった注文 · 在庫は FF-3 棚に仮置き）
#   JO 2026-07-30:「FF-3 は已发货未入金の注文に対応する在庫の仮置き棚。
#                   戻ってきたら通常倉庫へ、戻らなければ処理（全損）。
#                   Shopee は部分賠償なので賠償を引いた残りが全損。
#                   人が核対しやすい形に整理したリストが欲しい」
#   nst.v_ff3_reconcile は **1 行 = 注文 × SKU**。金額系（gmv/賠償/純損失）は
#   注文単位の値なので SKU 行のまま合計すると多重計上する → order_no で
#   畳んでから KPI と action 集計を出す。cost_total_jpy だけが SKU 単位。
# ============================================================
with tab_ff3:
    _fj = get_lang() == "ja"

    def _fl(zh: str, ja: str) -> str:
        return ja if _fj else zh

    st.caption(t(
        "FF-3 = 已发货但还没入金的订单，其对应库存的**临时暂存棚**。货已经出了日本，"
        "被取消/退款后现物不会自己回来，但 NST 上要做库存处理，所以先移到 FF-3 · "
        "之后由人逐单看：**回来了 → 退回正常仓库**；**回不来 → 处理（全损）** · "
        "Shopee 是**部分赔偿**，赔偿以外的残额才是全损 · "
        "赔偿要晚 6~8 个月才到账，所以「先去要赔偿」的单子别急着当全损处理"
    ))

    def _f3q(sql: str, ver: str):
        try:
            from shared.cache import cached_df
            return cached_df(conn, sql, None, ver=ver), None
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return None, str(e)

    _f3, _f3_err = _f3q(
        "SELECT ym, ship_date, shop, platform, country, order_no, invoice_no, "
        "item_internal_id, jan, display_name, qty, cost_estimate, "
        "cost_total_jpy, ff3_qty_now, order_status, logistics_status, "
        "shipped_out, cancel_reason, cancel_by, refund_reason, refund_status, "
        "refund_amount_local, needs_logistics, is_arrived_at_warehouse, "
        "return_tracking_number, gmv_local, received_local, gmv_jpy, "
        "compensation_local, compensation_modules, compensation_rate_pct, "
        "net_loss_jpy, action "
        f"FROM nst.v_ff3_reconcile WHERE ym = '{ym}'", _ship_ver())

    if _f3_err:
        st.error(t("退款明细视图未取得或连接错误: ") + _f3_err)
        st.info(t("需在元川 PG 执行 nst_api/sql/026_create_ff3_reconcile_view.sql"))
    elif _f3 is None or _f3.empty:
        st.info(t("当月无退款明细对象（出货后未入金的订单）"))
    else:
        F = _f3.copy()
        # PG の NUMERIC は decimal.Decimal で載る → float × Decimal の TypeError 防止
        for _c in ("qty", "cost_estimate", "cost_total_jpy", "ff3_qty_now",
                   "refund_amount_local", "gmv_local", "received_local",
                   "gmv_jpy", "compensation_local", "compensation_rate_pct",
                   "net_loss_jpy"):
            F[_c] = pd.to_numeric(F[_c], errors="coerce").astype(float).fillna(0)
        # action が NULL だと groupby が黙って行を落とし、集計の件数が対象注文数と
        # 合わなくなる（核対で最悪の壊れ方）→ 空文字にして「—」で見えるようにする
        F["action"] = F["action"].fillna("")

        # ビューに market 列は無い → 店舗名から分類（page 上部と同じ classify_market）
        F = add_market_column(F, store_col="shop")
        if mk:
            F = F[F["market"].isin(mk)]

        if F.empty:
            st.info(t("当前市场筛选下无数据"))
        else:
            # 注文単位に畳んだ表。金額 KPI と action 集計はこれを使う
            O = F.drop_duplicates(subset=["country", "order_no"])

            # 賠償が着金している注文だけ（賠償は 6〜8 ヶ月遅れて入るので、
            # 未着金を 0% として平均に混ぜると「Shopee はほぼ払わない」と誤読される）
            _wc = O[O["compensation_local"] > 0]

            k1, k2, k3, k4 = st.columns(4)
            k1.metric(_fl("📦 对象订单数", "📦 対象注文数"), f"{len(O):,}",
                      _fl(f"{len(F):,} 行（订单×SKU）", f"{len(F):,} 行（注文×SKU）"),
                      delta_color="off")
            k2.metric(_fl("🩸 净损失合计", "🩸 純損失 合計"),
                      f"¥{O['net_loss_jpy'].sum():,.0f}",
                      _fl("扣掉已到账赔偿后的残额", "着金済の賠償を引いた残額"),
                      delta_color="inverse")
            k3.metric(_fl("💰 赔偿已到账", "💰 賠償 着金済"),
                      f"¥{float(_wc['gmv_jpy'].sum() - _wc['net_loss_jpy'].sum()):,.0f}",
                      _fl(f"{len(_wc):,} / {len(O):,} 单 · 赔偿滞后 6~8 个月",
                          f"{len(_wc):,} / {len(O):,} 件 · 賠償は 6〜8ヶ月遅れ"),
                      delta_color="off")
            # ⚠️ 平均は **着金済の注文だけ** で取る。0% の未着金を分母に入れない
            k4.metric(_fl("📊 赔偿率（已到账单）", "📊 賠償率（着金済のみ）"),
                      f"{_wc['compensation_rate_pct'].mean():.1f}%" if len(_wc) else "—",
                      _fl("Shopee 是部分赔偿", "Shopee は部分賠償"),
                      delta_color="off")

            # 判定不能（物流データが無く shipped_out を判定できない注文）を必ず併記する。
            # ⚠️ これを隠すと、人は 673 件を「全部」だと読む。実測では判定不能が
            #    2,998 件あり、リストの 4 倍以上あった（2026-07-30 の監査で発覚）
            _und, _und_err = _f3q(
                "SELECT coalesce(sum(orders),0) AS orders, "
                "coalesce(sum(nst_amount_jpy),0) AS jpy "
                f"FROM nst.v_ff3_undecidable WHERE ym = '{ym}'", _ship_ver())
            if _und is not None and not _und.empty:
                _un = int(pd.to_numeric(_und["orders"], errors="coerce").fillna(0).iloc[0])
                if _un:
                    st.warning(_fl(
                        f"⚠️ 另有 {_un:,} 单**判定不能**，没进上面的清单 —— "
                        "这些单缺物流数据，无法判断货有没有出日本（不是「没出货」）。"
                        "定向物流回填跑完后会自动补进来。",
                        f"⚠️ 別に {_un:,} 件が**判定不能**でこのリストに入っていません —— "
                        "物流データが無く、日本を出たかを判定できない状態です"
                        "（「出荷していない」ではありません）。物流の取得後に流入します。"))

            # --- action 别汇总 = 人的作业队列（人的作业顺序に並べる）---
            _ACT = {
                "claim_first":     _fl("① 先去要赔偿（未申请/未到账）",
                                       "① まず賠償を請求（未申請/未着金）"),
                "await_return":    _fl("② 等返送到仓（用追踪号追）",
                                       "② 返送の到着待ち（追跡番号で追う）"),
                "return_to_stock": _fl("③ 退回正常仓库（已确认到仓）",
                                       "③ 通常倉庫へ戻す（入庫確認済）"),
                "writeoff":        _fl("④ 可以处理（丢件确定 or 赔偿已结）",
                                       "④ 処理してよい（紛失確定 or 賠償済）"),
                "monitor":         _fl("⑤ 观察（判断材料不足）",
                                       "⑤ 監視（判断材料が足りない）"),
            }
            _ORDER = list(_ACT.keys())

            st.markdown("**" + _fl("按建议动作分组（这就是作业队列）",
                                   "推奨アクション別（これが作業キュー）") + "**")
            _ag = (O.groupby("action", as_index=False)
                   .agg(orders=("order_no", "nunique"),
                        net_loss=("net_loss_jpy", "sum"),
                        rate=("compensation_rate_pct", "mean")))
            # SKU 行数と FF-3 に積まれる原価は SKU 単位の F 側から取る
            _as = (F.groupby("action", as_index=False)
                   .agg(cost=("cost_total_jpy", "sum")))
            _ag = _ag.merge(_as, on="action", how="left")
            # ⚠️ 0 件の action も行として出す。出さないと「該当なし」と
            #    「シグナルが来ていない」を人が区別できない（return_to_stock が該当 ·
            #    Shopee の入庫確認フラグが一度も真にならないため構造的に 0 件）
            _ag = (_ag.set_index("action").reindex(_ORDER)
                   .fillna(0).reset_index())
            _ag["_o"] = _ag["action"].map(lambda a: _ORDER.index(a))
            _ag = _ag.sort_values("_o")
            html_table(pd.DataFrame({
                _fl("建议动作", "推奨アクション"):
                    _ag["action"].map(lambda a: _ACT.get(a, a or "—")),
                _fl("订单数", "注文数"): _ag["orders"].map(lambda v: f"{int(v):,}"),
                _fl("净损失(¥)", "純損失(¥)"): _ag["net_loss"].map(lambda v: f"¥{v:,.0f}"),
                _fl("平均赔偿率", "平均賠償率"): _ag["rate"].map(lambda v: f"{v:.1f}%"),
                _fl("FF-3 原价(¥)", "FF-3 原価(¥)"):
                    _ag["cost"].fillna(0).map(lambda v: f"¥{v:,.0f}"),
            }))

            # --- FF-3 在庫の現況 vs ビューの原価合計 ---
            from shared.cache import data_version as _dv
            _inv, _inv_err = _f3q(
                "SELECT max(s.snapshot_date)::TEXT AS snap_date, "
                "count(DISTINCT s.item_internal_id) AS items, "
                "sum(s.qty_on_hand) AS qty, "
                "sum(s.qty_on_hand * COALESCE(im.cost_estimate, 0)) AS cost_jpy "
                "FROM nst.inventory_bin_snapshot s "
                "LEFT JOIN nst.item_master_raw im "
                "  ON im.internal_id = s.item_internal_id "
                "WHERE s.bin_number = 'FF-3' AND s.snapshot_date = "
                "  (SELECT max(snapshot_date) FROM nst.inventory_bin_snapshot)",
                _dv("inventory", "basic"))

            st.markdown("**" + _fl("FF-3 在库现况 ↔ 本月对象的原价",
                                   "FF-3 在庫の現況 ↔ 当月対象の原価") + "**")
            _view_cost = float(F["cost_total_jpy"].sum())
            if _inv_err or _inv is None or _inv.empty:
                st.caption(_fl("（FF-3 库存快照读取失败，只显示视图侧金额：",
                               "（FF-3 在庫スナップショットが読めません。ビュー側のみ表示：")
                           + f"¥{_view_cost:,.0f}）")
            else:
                _r = _inv.iloc[0]

                def _n1(col: str) -> float:
                    """1 行目を float 化。NULL/NaN は 0（`nan or 0` は nan が真値なので不可）。"""
                    return float(pd.to_numeric(_inv[col], errors="coerce")
                                 .fillna(0).iloc[0])

                _iq, _ic, _ii = _n1("qty"), _n1("cost_jpy"), int(_n1("items"))
                i1, i2, i3, i4 = st.columns(4)
                i1.metric(_fl("FF-3 品目数", "FF-3 品目数"), f"{_ii:,}",
                          str(_r["snap_date"] or "—"), delta_color="off")
                i2.metric(_fl("FF-3 个数", "FF-3 個数"), f"{_iq:,.0f}", None,
                          delta_color="off")
                i3.metric(_fl("FF-3 原价(¥)", "FF-3 原価(¥)"), f"¥{_ic:,.0f}", None,
                          delta_color="off")
                i4.metric(_fl("差额（FF-3 − 本月对象）", "差額（FF-3 − 当月対象）"),
                          f"¥{_ic - _view_cost:,.0f}",
                          f"{_fl('本月对象', '当月対象')} ¥{_view_cost:,.0f}",
                          delta_color="off")
                st.caption(t(
                    "差额叫**未说明额**，不是错 —— FF-3 是累计残高（往月未处理的还在棚上），"
                    "本月对象只有该月发货的单。实测差额的三个主因："
                    "① 本视图只覆盖 Shopee（Coupang/Lazada 没有 SKU 明细 API，进了 FF-3 也出不来）"
                    "② JAN 未匹配的行成本算 0（Shopee 侧 model_sku 为空或对不上 NST）"
                    "③ 物流/费用明细的拉取起点在 2026-05，更早的单判定不出来"
                ))

            # --- 明细（人工核对用）---
            st.caption(_fl(
                "⚠️ 「③ 回来了放回仓库」机器判不出来 —— Shopee 的入库确认字段"
                "（is_arrived_at_warehouse）实测 93 件全部返 false，一次都没变过。"
                "所以这一档永远 0 件，**不代表没有货回来**，要在 Shopee 后台目视确认。"
                "「② 等返送」里也只有一部分有追踪号，其余只能后台查。",
                "⚠️ 「③ 戻ってきたら在庫に戻す」は機械判定できません —— Shopee の入庫確認"
                "フィールド（is_arrived_at_warehouse）は実測 93 件すべて false で、"
                "一度も真になりません。この区分が 0 件でも**戻りが無いとは限らない**ので、"
                "Shopee 管理画面で目視してください。「② 返送待ち」も追跡番号があるのは一部です。"))

            st.markdown("**" + _fl("明细清单（人工核对）", "明細一覧（人手での照合用）") + "**")
            # ⚠️ options は **action コード**。ラベルを options にすると、言語を切り替えた
            #    とき session_state に旧言語の文字列が残り、Streamlit は例外を出さずに
            #    それを保持するので選択が空になり明細が消える（実測 · streamlit 1.57）
            _sel3 = st.multiselect(
                _fl("建议动作", "推奨アクション"), options=_ORDER,
                default=["claim_first", "await_return"],
                format_func=lambda a: _ACT[a], key="ff3_action")
            D = F[F["action"].isin(_sel3)].copy()
            if D.empty:
                st.caption(_fl("（该动作下没有订单）", "（該当するアクションの注文はありません）"))
            else:
                def _yn(v) -> str:
                    if v is None or (isinstance(v, float) and pd.isna(v)):
                        return "—"
                    return _fl("是", "はい") if bool(v) else _fl("否", "いいえ")

                D["action"] = D["action"].map(lambda a: _ACT.get(a, a or "—"))
                # ⚠️ 注文単位の金額を SKU 行に繰り返すと、operator が CSV を Excel で
                #    合計したとき純損失が 2.84 倍になる（実測 ¥708,156 → ¥2,010,650）。
                #    直前のコミット fd768f6 で直した「旧口径 2.85 倍」と同型の誤りを
                #    CSV 経由で再現させないため、2 行目以降は空欄にする。
                #    SKU 単位で足して良いのは qty / cost_total_jpy / ff3_qty_now だけ。
                _dup = D.duplicated(subset=["country", "order_no"])
                D.loc[_dup, ["gmv_local", "received_local", "compensation_local",
                             "compensation_rate_pct", "net_loss_jpy"]] = None
                for _b in ("shipped_out", "needs_logistics",
                           "is_arrived_at_warehouse"):
                    D[_b] = D[_b].map(_yn)
                _cols3 = {
                    "ship_date":               _fl("发货日", "出荷日"),
                    "action":                  _fl("建议动作", "推奨アクション"),
                    "platform":                _fl("平台", "プラットフォーム"),
                    "country":                 _fl("国家", "国"),
                    "shop":                    _fl("店铺", "店舗"),
                    "order_no":                _fl("订单号", "注文番号"),
                    "invoice_no":              _fl("NST 请求书", "NST 請求書"),
                    "jan":                     _fl("JAN", "JAN"),
                    "display_name":            _fl("商品名", "商品名"),
                    "qty":                     _fl("数量", "数量"),
                    "cost_total_jpy":          _fl("原价合计(¥)", "原価合計(¥)"),
                    "ff3_qty_now":             _fl("FF-3 现有数", "FF-3 現在数"),
                    "order_status":            _fl("订单状态", "注文状態"),
                    "logistics_status":        _fl("物流状态", "配送状態"),
                    "shipped_out":             _fl("已出日本", "日本を出た"),
                    "cancel_reason":           _fl("取消原因", "取消理由"),
                    "cancel_by":               _fl("取消方", "取消者"),
                    "refund_reason":           _fl("退款原因", "返金理由"),
                    "refund_status":           _fl("退款状态", "返金状態"),
                    "needs_logistics":         _fl("需返送", "返送が必要"),
                    "is_arrived_at_warehouse": _fl("已到仓", "倉庫着"),
                    "return_tracking_number":  _fl("返送追踪号", "返送追跡番号"),
                    "gmv_local":               _fl("订单金额(当地币)", "注文金額(現地)"),
                    "received_local":          _fl("实收(当地币)", "実収(現地)"),
                    "compensation_local":      _fl("赔偿(当地币)", "賠償(現地)"),
                    "compensation_rate_pct":   _fl("赔偿率(%)", "賠償率(%)"),
                    "net_loss_jpy":            _fl("净损失(¥)", "純損失(¥)"),
                }
                D = (D[list(_cols3)].rename(columns=_cols3)
                     .sort_values(_cols3["net_loss_jpy"], ascending=False))
                st.dataframe(D, use_container_width=True, height=460,
                             hide_index=True)
                st.download_button(
                    _fl("⬇️ 下载 CSV（退款明细）", "⬇️ CSV ダウンロード（返金詳細）"),
                    D.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"ff3_reconcile_{ym}.csv", mime="text/csv",
                    key="ff3_dl_detail")


st.divider()
st.caption(
    t("対象月") + f"：{ym} · " + t("市場") + f"：{mk} · "
    + t("表示行（明細）: ") + f"{len(df):,}"
)
