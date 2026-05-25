"""模块 #4 销售数据查询 · NST API 売上データ（nst.sales_monthly）ベース.

2026-05-20 全面改写：旧 sales_line（手動 Excel 時代）→ NST API 直取得データへ。
レポート【輸出】店舗別売上_JO を SuiteQL 再現したデータ（数量完全一致・検証済）。

データ源:
- nst.sales_monthly   店舗 × SKU × 月の売上（販売数量 / 総収益 JPY）
- nst.item_master_raw 商品マスタ（表示名 / メーカー / ランク / 取扱区分 / 定義原価）
- nst.inventory_snapshot 在庫（手持 / 利用可能）最新スナップショット

表示:
- 月選択 + 店舗別 / アイテム別 切替 + 店舗フィルタ + JAN/商品名 検索
- 粗利 = 総収益 − 定義原価(=cost_estimate×数量) / 粗利率 = 粗利/総収益
"""
from __future__ import annotations

import pandas as pd
import altair as alt
import streamlit as st

from shared.db import get_connection
from shared.owners import classify_market
from shared.i18n import lang_selector, t, get_lang

st.set_page_config(page_title=t("销售数据查询"), page_icon="📊", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📊 销售数据查询"))
st.caption(t(
    "NST API 売上データ（店舗別売上 レポート再現・数量完全一致）· "
    "店舗×SKU×月 + 商品マスタ + 在庫 統合 · 粗利/粗利率 自動計算"
))

# 列見出し: (中文, 日本語) — UI 言語追従
_LBL = {
    "shop":          ("店铺", "FB_店舗"),
    "market":        ("市场", "市場"),
    "jan":           ("UPC编码", "UPCコード"),
    "item_code":     ("商品编号", "アイテム"),
    "display_name":  ("商品名", "アイテム名"),
    "maker":         ("品牌", "ブランド"),
    "item_rank":     ("等级", "ランク"),
    "handling_cd":   ("经销状态", "取扱区分"),
    "qty_sold":      ("销售数", "販売数"),
    "revenue":       ("总收益", "総収益"),
    "unit_price":    ("单价", "単価"),
    "teigi_genka":   ("定义原价", "定義原価"),
    "arari":         ("毛利", "粗利"),
    "arari_rate":    ("粗利率", "粗利率"),
    "qty_on_hand":   ("库存数", "在庫数"),
    "qty_available": ("可用库存", "利用可能"),
    "stock_value":   ("库存金额", "在庫金額"),
    "turnover":      ("回转率", "回転率"),
    "avg_stock_days": ("平均库存天数", "平均在庫日数"),
    "cross_ratio":   ("交叉比率", "交差比率"),
    "sku_status":    ("SKU稼働率", "SKU稼働率"),
    "stock_sales_ratio": ("库存销售比", "在庫販売比"),
    "sellthrough":   ("月完売率", "月完売率"),
    "profit_contrib": ("利润贡献率", "利益貢献率"),
}


def _cc(*keys) -> dict:
    ja = get_lang() == "ja"
    return {k: (_LBL[k][1] if ja else _LBL[k][0]) for k in keys}


def _query(sql: str, params: tuple = ()):
    try:
        cur = conn.execute(sql, params) if params else conn.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols), None
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return None, str(e)


def _render_sales_delta(_period_kind, _key):
    """周次/月次 共通：対象期 vs 前期 の販売数量落差を店舗別で一覧（_period_kind: 'week'|'month'）。"""
    import datetime as _dtw
    _L = lambda zh, ja: ja if get_lang() == "ja" else zh
    _wconn = get_connection()
    try:
        _rng = _wconn.execute(
            "SELECT MAX(sale_date) AS mx FROM nst.sales_daily"
        ).fetchone()
    except Exception as _ew:
        st.error(_L("売上日次データ未取得: ", "売上日次データ未取得: ") + str(_ew))
        _wconn.close()
        return
    if not _rng or not _rng["mx"]:
        st.info(_L("売上日次データがありません（page 27 で sales を取得）",
                   "売上日次データがありません（page 27 で sales を取得）"))
        _wconn.close()
        return
    _mx = _rng["mx"]
    if isinstance(_mx, str):
        _mx = _dtw.date.fromisoformat(_mx[:10])
    _today = _dtw.date.today()

    def _month_end(_d):
        if _d.month == 12:
            _nm = _d.replace(year=_d.year + 1, month=1, day=1)
        else:
            _nm = _d.replace(month=_d.month + 1, day=1)
        return _nm - _dtw.timedelta(days=1)

    if _period_kind == "week":
        _this = _mx - _dtw.timedelta(days=_mx.weekday())
        _periods = [_this - _dtw.timedelta(weeks=_i) for _i in range(13)]
        _periods = [_p for _p in _periods if _p + _dtw.timedelta(days=6) < _today]
        if not _periods:
            _periods = [_this - _dtw.timedelta(weeks=1)]

        def _lbl(_p):
            return f"{_p.isoformat()} ~ {(_p + _dtw.timedelta(days=6)).isoformat()}"

        def _bounds(_p):
            return (_p, _p + _dtw.timedelta(days=6),
                    _p - _dtw.timedelta(days=7), _p - _dtw.timedelta(days=1))

        _sel_label = _L("对象周（仅完整周·默认上周）", "対象週（完了週のみ·既定は前週）")
        _file_prefix = _L("周次销量落差_", "週次売上落差_")
        _prev_name, _cur_name = _L("上周", "前週"), _L("本周", "今週")
    else:
        _first = _mx.replace(day=1)
        _periods, _mp = [], _mx.replace(day=1)
        for _ in range(13):
            _periods.append(_mp)
            _mp = (_mp - _dtw.timedelta(days=1)).replace(day=1)
        _periods = [_p for _p in _periods if _month_end(_p) < _today]
        if not _periods:
            _periods = [_first]

        def _lbl(_p):
            return _p.strftime("%Y-%m")

        def _bounds(_p):
            _pe = _p - _dtw.timedelta(days=1)
            return _p, _month_end(_p), _pe.replace(day=1), _pe

        _sel_label = _L("对象月（仅完整月·默认上月）", "対象月（完了月のみ·既定は前月）")
        _file_prefix = _L("月度销量落差_", "月次売上落差_")
        _prev_name, _cur_name = _L("上月", "前月"), _L("本月", "今月")

    _c1, _c2, _c3 = st.columns([2, 1, 1])
    _sel = _c1.selectbox(_sel_label, _periods, format_func=_lbl, index=0, key=_key + "_sel")
    _direction = _c2.radio(_L("方向", "方向"),
                           [_L("销量下降", "数量減少"), _L("销量上升", "数量増加"),
                            _L("落差绝对值", "落差絶対値")], index=0, key=_key + "_dir")
    _min_delta = _c3.number_input(_L("最小落差（件）", "最小落差（件）"),
                                  min_value=0, value=5, step=1, key=_key + "_min")
    _cur_s, _cur_e, _prev_s, _prev_e = _bounds(_sel)
    _shops = [_r["shop"] for _r in _wconn.execute(
        "SELECT DISTINCT shop FROM nst.sales_daily "
        "WHERE sale_date BETWEEN ? AND ? ORDER BY shop",
        (_prev_s.isoformat(), _cur_e.isoformat()),
    ).fetchall()]
    _ALLW = _L("（全部店铺）", "（全店舗）")
    _shop_sel = st.selectbox(_L("店铺", "店舗"), [_ALLW] + _shops, key=_key + "_shop")
    _sql = (
        "SELECT s.shop AS shop, MIN(im.jan) AS jan, MIN(im.display_name) AS name, "
        "MIN(im.item_rank) AS item_rank, "
        "SUM(CASE WHEN s.sale_date BETWEEN ? AND ? THEN s.qty_sold ELSE 0 END) AS qty_cur, "
        "SUM(CASE WHEN s.sale_date BETWEEN ? AND ? THEN s.qty_sold ELSE 0 END) AS qty_prev, "
        "SUM(CASE WHEN s.sale_date BETWEEN ? AND ? THEN s.revenue ELSE 0 END) AS rev_cur, "
        "SUM(CASE WHEN s.sale_date BETWEEN ? AND ? THEN s.revenue ELSE 0 END) AS rev_prev "
        "FROM nst.sales_daily s "
        "JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
        "WHERE s.sale_date BETWEEN ? AND ? "
    )
    _params = [
        _cur_s.isoformat(), _cur_e.isoformat(),
        _prev_s.isoformat(), _prev_e.isoformat(),
        _cur_s.isoformat(), _cur_e.isoformat(),
        _prev_s.isoformat(), _prev_e.isoformat(),
        _prev_s.isoformat(), _cur_e.isoformat(),
    ]
    if _shop_sel != _ALLW:
        _sql += "AND s.shop = ? "
        _params.append(_shop_sel)
    _sql += "GROUP BY s.shop, s.item_internal_id"
    try:
        _wrows = _wconn.execute(_sql, tuple(_params)).fetchall()
    except Exception as _ew2:
        _wrows = []
        st.error(_L("汇总失败: ", "集計失敗: ") + str(_ew2))
    _wdf = pd.DataFrame([dict(_r) for _r in _wrows])
    if _wdf.empty:
        st.info(_L("此条件下无数据", "この条件のデータがありません"))
        _wconn.close()
        return
    for _col in ("qty_cur", "qty_prev", "rev_cur", "rev_prev"):
        _wdf[_col] = _wdf[_col].astype(float)
    _wdf["delta"] = _wdf["qty_cur"] - _wdf["qty_prev"]
    _wdf["delta_pct"] = _wdf.apply(
        lambda _r: round((_r["delta"] / _r["qty_prev"] * 100), 1)
        if _r["qty_prev"] else (100.0 if _r["delta"] > 0 else 0.0),
        axis=1,
    )
    if _direction == _L("销量下降", "数量減少"):
        _wdf = _wdf[_wdf["delta"] <= -_min_delta].sort_values("delta")
    elif _direction == _L("销量上升", "数量増加"):
        _wdf = _wdf[_wdf["delta"] >= _min_delta].sort_values("delta", ascending=False)
    else:
        _wdf = _wdf[_wdf["delta"].abs() >= _min_delta]
        _wdf = _wdf.reindex(_wdf["delta"].abs().sort_values(ascending=False).index)
    _k1, _k2, _k3 = st.columns(3)
    _k1.metric(_L("对象期间", "対象期間"), _lbl(_sel))
    _k2.metric(_L("落差 SKU 数", "落差 SKU 数"), f"{len(_wdf):,}")
    _k3.metric(_L("销量净变化（件）", "数量純変化（件）"), f"{_wdf['delta'].sum():,.0f}")
    for _col in ("qty_cur", "qty_prev", "delta", "rev_cur", "rev_prev"):
        _wdf[_col] = _wdf[_col].round(0).astype(int)
    _ja = get_lang() == "ja"
    _hdr = {
        "shop": ("店铺", "店舗"), "jan": ("UPC编码", "UPCコード"),
        "name": ("商品名", "商品名"), "item_rank": ("等级", "ランク"),
        "qty_prev": (_prev_name + "销量", _prev_name + "数量"),
        "qty_cur": (_cur_name + "销量", _cur_name + "数量"),
        "delta": ("落差", "落差"), "delta_pct": ("落差%", "落差%"),
        "rev_prev": (_prev_name + "营业额", _prev_name + "売上"),
        "rev_cur": (_cur_name + "营业额", _cur_name + "売上"),
    }
    _cols = ["shop", "jan", "name", "item_rank", "qty_prev", "qty_cur",
             "delta", "delta_pct", "rev_prev", "rev_cur"]
    _show = _wdf[_cols].rename(
        columns={_kk: (_v[1] if _ja else _v[0]) for _kk, _v in _hdr.items()})

    # 落差 / 落差% 涨跌着色：正绿负红（Boss 2026-05-25）
    def _pos_neg_color(_val):
        try:
            _f = float(_val)
        except (TypeError, ValueError):
            return ""
        return "color:#16A34A" if _f > 0 else ("color:#DC2626" if _f < 0 else "")
    _dcols = [_c for _c in ("落差", "落差%") if _c in _show.columns]
    st.dataframe(_show.style.map(_pos_neg_color, subset=_dcols),
                 use_container_width=True, height=520)
    _csv = _show.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        _L("📥 下载 CSV", "📥 CSV ダウンロード"),
        _csv,
        file_name=_file_prefix + _lbl(_sel).replace(" ~ ", "_").replace(" ", "") + ".csv",
        mime="text/csv",
        key=_key + "_dl",
    )
    st.caption(_L(
        "落差 = " + _cur_name + "销量 − " + _prev_name + "销量 · 负值=下降 · 选择店铺可逐店分析原因",
        "落差 = " + _cur_name + "数量 − " + _prev_name + "数量 · マイナス=減少 · 店舗を選んで個別に原因分析",
    ))

    # ── 销量推移分析：店铺 / 品牌 / SKU 维度を組合せ筛选 ──
    # （周次=最近10周 / 月度=最近6ヶ月・不足分はある分だけ）
    def _draw_trend(_extra_where, _ps, _cap_label):
        _N = 10 if _period_kind == "week" else 6
        _recent = list(reversed(_periods[:_N]))
        _w_start = _bounds(_recent[0])[0]
        _w_end = _bounds(_recent[-1])[1]
        _ts_rows = _wconn.execute(
            "SELECT s.sale_date AS d, SUM(s.qty_sold) AS q, SUM(s.revenue) AS r, "
            "SUM(s.gross_profit) AS g "
            "FROM nst.sales_daily s "
            "JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
            "WHERE " + _extra_where + " AND s.sale_date BETWEEN ? AND ? "
            "GROUP BY s.sale_date",
            tuple(_ps) + (_w_start.isoformat(), _w_end.isoformat()),
        ).fetchall()
        _dq, _dr, _dg = {}, {}, {}
        for _tr in _ts_rows:
            _dd = _tr["d"]
            if isinstance(_dd, str):
                _dd = _dtw.date.fromisoformat(_dd[:10])
            _dq[_dd] = _dq.get(_dd, 0.0) + float(_tr["q"] or 0)
            _dr[_dd] = _dr.get(_dd, 0.0) + float(_tr["r"] or 0)
            _dg[_dd] = _dg.get(_dd, 0.0) + float(_tr["g"] or 0)
        _period_col = _L("期间", "期間")
        _qty_col = _L("销量", "数量")
        _rev_col = _L("营业额", "売上")
        _profit_col = _L("利润", "利益")
        _margin_col = _L("利润率", "利益率")
        _ratio_col = (_L("前周比", "前週比") if _period_kind == "week"
                      else _L("前月比", "前月比"))
        _rows, _prev_q = [], None
        for _p in _recent:
            _cs, _ce, _, _ = _bounds(_p)
            _clbl = _cs.strftime("%m/%d") if _period_kind == "week" else _p.strftime("%Y-%m")
            _q = sum(_v for _d, _v in _dq.items() if _cs <= _d <= _ce)
            _r = sum(_v for _d, _v in _dr.items() if _cs <= _d <= _ce)
            _g = sum(_v for _d, _v in _dg.items() if _cs <= _d <= _ce)
            _ratio = f"{_q / _prev_q * 100:.1f}%" if _prev_q else "—"
            _mg = f"{_g / _r * 100:.1f}%" if _r else "—"
            _rows.append({
                _period_col: _clbl,
                _qty_col: int(round(_q)),
                _rev_col: int(round(_r)),
                _profit_col: int(round(_g)),
                _margin_col: _mg,
                _ratio_col: _ratio,
            })
            _prev_q = _q
        _tbl = pd.DataFrame(_rows).set_index(_period_col)
        # 曲线图上方：每期 销量 / 营业额 / 利润 / 利润率 / 前期比
        st.dataframe(_tbl, use_container_width=True)
        # 曲线图（page05 と統一: 销量(左軸)+利润(右軸) 点線 + 縦ルール hover で全値表示）
        _cdf = _tbl.reset_index()
        _xp = alt.X(field=_period_col, type="nominal", sort=None, title=None,
                    axis=alt.Axis(labelAngle=0))
        _near = alt.selection_point(nearest=True, on="pointerover",
                                    fields=[_period_col], empty=False)
        _bse = alt.Chart(_cdf).encode(x=_xp)
        _l_q = _bse.mark_line(point=True).encode(
            y=alt.Y(field=_qty_col, type="quantitative", title=_qty_col,
                    axis=alt.Axis(format=",.0f")),
            color=alt.datum(_qty_col),
        )
        _l_g = _bse.mark_line(point=True, strokeDash=[5, 3]).encode(
            y=alt.Y(field=_profit_col, type="quantitative", title=_profit_col,
                    axis=alt.Axis(orient="right", format=",.0f")),
            color=alt.datum(_profit_col),
        )
        _rule = _bse.mark_rule(color="#888").encode(
            opacity=alt.condition(_near, alt.value(0.35), alt.value(0)),
            tooltip=[alt.Tooltip(field=_period_col, type="nominal"),
                     alt.Tooltip(field=_qty_col, type="quantitative", format=",.0f"),
                     alt.Tooltip(field=_profit_col, type="quantitative", format=",.0f")],
        ).add_params(_near)
        _chart = (alt.layer(_l_q, _l_g, _rule)
                  .resolve_scale(y="independent", color="shared")
                  .properties(height=320)
                  .configure_legend(orient="top", title=None))
        st.altair_chart(_chart, use_container_width=True)
        if _period_kind == "week":
            _tc = _L(f"最近 {len(_recent)} 周销量推移", f"直近 {len(_recent)} 週の販売数量推移")
        else:
            _tc = _L(f"最近 {len(_recent)} 个月销量推移", f"直近 {len(_recent)} ヶ月の販売数量推移")
        st.caption((_cap_label + " · " if _cap_label else "") + _tc
                   + " · " + _L("前期比 = 本期销量 / 上期销量", "前期比 = 当期数量 / 前期数量"))

    st.divider()
    st.markdown("#### " + _L("销量推移分析（市场 / 店铺 / 品牌 / SKU 维度）",
                            "販売数量推移分析（市場 / 店舗 / メーカー / SKU）"))
    st.caption(_L("4 维度可任意组合 · 留空=不限 · 周次=最近10周 / 月度=最近6个月",
                  "4 軸は任意組合せ可 · 空欄=指定なし · 周次=直近10週 / 月次=直近6ヶ月"))
    _ALL = _L("（全部）", "（全て）")
    _all_shops = [_sr["shop"] for _sr in _wconn.execute(
        "SELECT DISTINCT shop FROM nst.sales_daily WHERE shop IS NOT NULL ORDER BY shop"
    ).fetchall()]
    _market_of = {_s: classify_market(_s) for _s in _all_shops}
    _markets = sorted(set(_market_of.values()))
    _all_makers = [_mr["maker"] for _mr in _wconn.execute(
        "SELECT DISTINCT maker FROM nst.item_master_raw "
        "WHERE maker IS NOT NULL AND maker <> '' ORDER BY maker"
    ).fetchall()]
    _f1, _f2, _f3, _f4 = st.columns(4)
    _f_market = _f1.multiselect(_L("市场", "市場"), _markets, key=_key + "_tmarket",
                                placeholder=_L("全部", "全件"))  # 空選＝不限
    _f_shop = _f2.selectbox(_L("店铺", "店舗"), [_ALL] + _all_shops, key=_key + "_tshop")
    _f_maker = _f3.multiselect(_L("品牌", "メーカー"), _all_makers, key=_key + "_tmaker",
                               placeholder=_L("全部", "全件"))  # 空選＝不限
    _f_jan = _f4.text_input(
        _L("SKU（JAN 搜索）", "SKU（JAN 検索）"), key=_key + "_tjan",
        placeholder=_L("JAN 部分一致（留空=不限）", "JAN 部分一致（空欄=指定なし）"),
    )
    _wc, _wp, _caps = [], [], []
    if _f_market:
        _mshops = [_s for _s in _all_shops if _market_of.get(_s) in _f_market]
        if _mshops:
            _wc.append("s.shop IN (" + ",".join(["?"] * len(_mshops)) + ")")
            _wp.extend(_mshops)
            _caps.append("/".join(_f_market))
    if _f_shop != _ALL:
        _wc.append("s.shop = ?"); _wp.append(_f_shop); _caps.append(_f_shop)
    if _f_maker:
        _wc.append("im.maker IN (" + ",".join(["?"] * len(_f_maker)) + ")")
        _wp.extend(_f_maker); _caps.append("/".join(_f_maker))
    if _f_jan.strip():
        _wc.append("im.jan LIKE ?"); _wp.append(f"%{_f_jan.strip()}%")
        _caps.append("JAN:" + _f_jan.strip())
    _extra = " AND ".join(_wc) if _wc else "1=1"
    _cap_label = " · ".join(_caps) if _caps else _L("全部", "全件")
    _draw_trend(_extra, _wp, _cap_label)
    _wconn.close()


_q_tab, _wk_tab, _mo_tab = st.tabs([
    t("📊 销售数据查询"), t("📉 周次销量落差"), t("📅 月度销量落差"),
])

with _wk_tab:
    _render_sales_delta("week", "wk")

with _mo_tab:
    _render_sales_delta("month", "mo")

with _q_tab:


    # ============================================================
    # データ有無チェック
    # ============================================================

    months_df, err = _query(
        "SELECT DISTINCT year_month FROM nst.sales_monthly ORDER BY year_month DESC"
    )
    if err:
        st.error(t("売上テーブル未取得 or 接続エラー: ") + err)
        st.info(t("page 27「📥 NST 取得データ」→ 手動更新 で sales を実行してください。"))
        st.stop()
    if months_df is None or months_df.empty:
        st.warning(t("⚠️ 売上データ未取得。page 27「📥 NST 取得データ」で sales ジョブを実行してください。"))
        st.stop()

    # ============================================================
    # フィルタ
    # ============================================================
    c1, c2 = st.columns([1, 3])
    ym = c1.selectbox(t("対象月"), months_df["year_month"].tolist())
    kw = c2.text_input(t("JAN / 商品名 検索"), placeholder="JAN コード or 表示名の一部")

    # 当月売上商品の各次元 distinct → 絞り込み候補
    _opt_df, _ = _query(
        "SELECT DISTINCT s.shop, im.item_rank, im.maker, im.handling_cd "
        "FROM nst.sales_monthly s "
        "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
        "WHERE s.year_month = ?",
        (ym,),
    )


    def _opts(col: str) -> list:
        if _opt_df is None or col not in _opt_df.columns:
            return []
        return sorted({str(v).strip() for v in _opt_df[col].dropna().tolist() if str(v).strip()})


    _ALL = t("（全部）")
    _month_shops = _opts("shop")
    _market_opts = sorted({classify_market(_s) for _s in _month_shops})
    f1, f2, f3, f4 = st.columns(4)
    market_filter = f1.multiselect(t("市场"), _market_opts, placeholder=_ALL)  # 空選＝全部
    rank_filter = f2.multiselect(t("商品ランク"), _opts("item_rank"), placeholder=_ALL)
    maker_filter = f3.multiselect(t("メーカー名"), _opts("maker"), placeholder=_ALL)
    handling_filter = f4.multiselect(t("取扱区分"), _opts("handling_cd"), placeholder=_ALL)

    with st.expander(t("📋 複数 JAN / 商品名 一括検索（改行・カンマ区切り）")):
        multi_kw = st.text_area(
            t("複数 JAN / 商品名"), placeholder="1 行 1 件 or カンマ区切り",
            height=120, label_visibility="collapsed",
        )
    _mk = multi_kw.replace(",", "\n").replace("，", "\n").replace("、", "\n")
    multi_terms = [x.strip() for x in _mk.split("\n") if x.strip()]

    # ============================================================
    # クエリ組み立て
    # ============================================================
    _INV = ("(SELECT item_internal_id, qty_on_hand FROM nst.inventory_snapshot "
            "WHERE snapshot_date=(SELECT max(snapshot_date) FROM nst.inventory_snapshot))")

    # 月份以外の筛选（下方の推移図 = 全月集計で再利用するため分離）
    _filt: list = []
    _filtp: list = []
    if market_filter:
        _mshops = [_s for _s in _month_shops if classify_market(_s) in market_filter]
        if _mshops:
            _filt.append("s.shop IN (" + ",".join(["?"] * len(_mshops)) + ")")
            _filtp.extend(_mshops)
        else:
            _filt.append("1=0")
    if rank_filter:
        _filt.append("im.item_rank IN (" + ",".join(["?"] * len(rank_filter)) + ")"); _filtp.extend(rank_filter)
    if maker_filter:
        _filt.append("im.maker IN (" + ",".join(["?"] * len(maker_filter)) + ")"); _filtp.extend(maker_filter)
    if handling_filter:
        _filt.append("im.handling_cd IN (" + ",".join(["?"] * len(handling_filter)) + ")"); _filtp.extend(handling_filter)
    if kw.strip():
        _filt.append("(im.jan LIKE ? OR im.display_name LIKE ?)")
        like = f"%{kw.strip()}%"; _filtp += [like, like]
    if multi_terms:
        _ors = []
        for _term in multi_terms:
            _ors.append("(im.jan LIKE ? OR im.display_name LIKE ?)")
            _like = f"%{_term}%"; _filtp += [_like, _like]
        _filt.append("(" + " OR ".join(_ors) + ")")
    where = ["s.year_month = ?"] + _filt
    params: list = [ym] + _filtp
    where_sql = " AND ".join(where)

    def _enrich(_d, with_stock):
        """派生指标（销售类 + 可选库存类）。"""
        _d = _d.copy()
        for _c in ("qty_sold", "revenue", "gross_profit"):
            _d[_c] = _d[_c].astype(float)
        _d["arari"] = _d["gross_profit"]
        _d["arari_rate"] = (_d["arari"] / _d["revenue"].where(_d["revenue"] != 0) * 100).round(1)
        _d["unit_price"] = (_d["revenue"] / _d["qty_sold"].where(_d["qty_sold"] != 0)).round(0)
        _tg = _d["arari"].sum()
        _d["profit_contrib"] = (_d["arari"] / _tg * 100).round(1) if _tg else 0.0
        if with_stock:
            _d["qty_on_hand"] = _d["qty_on_hand"].astype(float)
            _d["cost_estimate"] = _d["cost_estimate"].astype(float)
            _soh = _d["qty_on_hand"].where(_d["qty_on_hand"] != 0)
            _sld = _d["qty_sold"].where(_d["qty_sold"] != 0)
            _d["stock_value"] = (_d["qty_on_hand"] * _d["cost_estimate"]).round(0)
            _d["turnover"] = (_d["qty_sold"] / _soh).round(2)            # 月販売数 / 当前在庫（近似）
            _d["stock_sales_ratio"] = (_d["qty_on_hand"] / _sld).round(2)
            _d["avg_stock_days"] = (_d["qty_on_hand"] / (_sld / 30)).round(1)
            _d["cross_ratio"] = (_d["arari_rate"] * _d["turnover"]).round(1)  # 粗利率(%)×回転率 → %表示
        return _d

    # SKU 別 17 指標 + 月完売率（市场/品牌/取扱区分 は上の筛选で絞る · 在庫類は全局口径）
    df, e2 = _query(
        "SELECT MIN(im.item_code) AS item_code, im.jan, im.display_name, im.maker, "
        "im.item_rank, MIN(im.handling_cd) AS handling_cd, "
        "SUM(s.qty_sold) AS qty_sold, SUM(s.revenue) AS revenue, "
        "SUM(s.gross_profit) AS gross_profit, "
        "MAX(im.cost_estimate) AS cost_estimate, MAX(inv.qty_on_hand) AS qty_on_hand "
        "FROM nst.sales_monthly s "
        "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
        f"LEFT JOIN {_INV} inv ON inv.item_internal_id = s.item_internal_id "
        f"WHERE {where_sql} "
        "GROUP BY im.jan, im.display_name, im.maker, im.item_rank "
        "ORDER BY revenue DESC",
        tuple(params),
    )
    if df is not None and not df.empty:
        df = _enrich(df, with_stock=True)
        # 月完売率 = 当月販売数 ÷（月初在庫 + 当月入庫）×100% · inventory_activity_monthly（jan 集計）
        _st_df, _ = _query(
            "SELECT im.jan AS jan, SUM(a.sold_qty) AS st_sold, "
            "SUM(a.opening_qty) AS st_open, SUM(a.received_qty) AS st_recv "
            "FROM nst.inventory_activity_monthly a "
            "JOIN nst.item_master_raw im ON im.internal_id = a.item_internal_id "
            "WHERE a.year_month = ? GROUP BY im.jan",
            (ym,),
        )
        if _st_df is not None and not _st_df.empty:
            for _c in ("st_sold", "st_open", "st_recv"):
                _st_df[_c] = _st_df[_c].astype(float)
            _den = (_st_df["st_open"] + _st_df["st_recv"])
            _st_df["sellthrough"] = (_st_df["st_sold"] / _den.where(_den != 0) * 100).round(1)
            df = df.merge(_st_df[["jan", "sellthrough"]], on="jan", how="left")
        else:
            df["sellthrough"] = None
    cols = ("item_code", "maker", "display_name", "item_rank",
            "qty_sold", "revenue", "unit_price", "arari", "arari_rate",
            "qty_on_hand", "stock_value", "turnover", "avg_stock_days",
            "cross_ratio", "sellthrough", "stock_sales_ratio", "profit_contrib")

    # ============================================================
    # 表示
    # ============================================================
    if e2:
        st.error(e2)
    elif df is None or df.empty:
        st.info(t("この条件のデータがありません"))
    else:
        tot_q = df["qty_sold"].astype(float).sum()
        tot_r = df["revenue"].astype(float).sum()
        tot_g = df["arari"].astype(float).sum()
        # 库存总金额/数量 = 全库存（JD-物流-千葉·最新快照·全SKU，不限当月销售）
        _invn, _ = _query(
            "SELECT SUM(inv.qty_on_hand * im.cost_estimate) sv, SUM(inv.qty_on_hand) soh "
            "FROM nst.inventory_snapshot inv "
            "JOIN nst.item_master_raw im ON im.internal_id = inv.item_internal_id "
            "WHERE inv.warehouse = 'JD-物流-千葉' "
            "  AND inv.snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot)"
        )
        tot_sv = float(_invn.iloc[0]["sv"] or 0) if _invn is not None and not _invn.empty else 0
        tot_soh = float(_invn.iloc[0]["soh"] or 0) if _invn is not None and not _invn.empty else 0
        _turnover = (tot_r / tot_sv) if tot_sv else 0          # 月周转 = 销售额 / 库存金额
        _ssr = (tot_sv / tot_r) if tot_r else 0                # 存销比 = 库存金额 / 销售额
        cur_margin = (tot_g / tot_r) if tot_r else 0

        # ── 上月同比（同筛选 _filt·销售=上月 sales_monthly·库存=上月末快照）──
        import datetime as _dt
        _pym = (_dt.datetime.strptime(ym, "%Y-%m").replace(day=1)
                - _dt.timedelta(days=1)).strftime("%Y-%m")
        _pwhere = " AND ".join(["s.year_month = ?"] + _filt)
        _pv, _ = _query(
            "SELECT SUM(s.qty_sold) q, SUM(s.revenue) r, SUM(s.gross_profit) g "
            "FROM nst.sales_monthly s "
            "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
            f"WHERE {_pwhere}",
            tuple([_pym] + _filtp),
        )
        _pinv, _ = _query(
            "SELECT SUM(inv.qty_on_hand * im.cost_estimate) sv, SUM(inv.qty_on_hand) soh "
            "FROM nst.inventory_snapshot inv "
            "JOIN nst.item_master_raw im ON im.internal_id = inv.item_internal_id "
            "WHERE inv.warehouse = 'JD-物流-千葉' "
            "  AND inv.snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot "
            "      WHERE to_char(snapshot_date,'YYYY-MM') = ?)",
            (_pym,),
        )

        def _g0(_dfp, _c):
            if _dfp is None or _dfp.empty or _dfp.iloc[0][_c] is None:
                return None
            return float(_dfp.iloc[0][_c])
        prev_q, prev_r, prev_g = _g0(_pv, "q"), _g0(_pv, "r"), _g0(_pv, "g")
        prev_sv, prev_soh = _g0(_pinv, "sv"), _g0(_pinv, "soh")
        prev_margin = (prev_g / prev_r) if (prev_g is not None and prev_r) else None
        prev_turn = (prev_r / prev_sv) if (prev_r is not None and prev_sv) else None   # 销售额/库存金额
        prev_ssr = (prev_sv / prev_r) if (prev_sv is not None and prev_r) else None    # 库存金额/销售额

        def _dpct(cur, prev):
            if prev is None or prev == 0:
                return None
            return f"{(cur - prev) / abs(prev) * 100:+.1f}%"

        def _dpp(cur, prev):  # 百分点差（粗利率用）
            return None if prev is None else f"{(cur - prev) * 100:+.1f}pp"

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric(t("対象月"), ym)
        m2.metric(t("販売数量 計"), f"{tot_q:,.0f}", _dpct(tot_q, prev_q))
        m3.metric(t("総収益 計"), f"¥{tot_r:,.0f}", _dpct(tot_r, prev_r))
        m4.metric(t("粗利 計"), f"¥{tot_g:,.0f}", _dpct(tot_g, prev_g))
        m5.metric(t("粗利率"), f"{cur_margin:.1%}", _dpp(cur_margin, prev_margin))
        n1, n2, n3 = st.columns(3)
        n1.metric(t("库存总金额"), f"¥{tot_sv:,.0f}", _dpct(tot_sv, prev_sv), delta_color="inverse")
        n2.metric(t("平均月周转率"), f"{_turnover:.2f}", _dpct(_turnover, prev_turn))
        n3.metric(t("整体库存销售比"), f"{_ssr:.2f}", _dpct(_ssr, prev_ssr), delta_color="inverse")

        st.caption(t("表示件数: ") + f"{len(df):,}" + t("（粗利率/利润贡献率=%, 回転率=月販売/当前在庫·近似）"))
        _colcfg = dict(_cc(*cols))
        for _pc in ("cross_ratio", "sellthrough", "profit_contrib"):
            if _pc in _colcfg:
                _colcfg[_pc] = st.column_config.NumberColumn(_colcfg[_pc], format="%.1f%%")
        # 本表は区域末尾 → 行数を増やす
        st.dataframe(
            df[list(cols)], use_container_width=True, height=900,
            column_config=_colcfg, hide_index=True,
        )
        _ja_dl = get_lang() == "ja"
        _csv = df[list(cols)].rename(columns=dict(_cc(*cols))).to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 CSV ダウンロード" if _ja_dl else "📥 下载 CSV",
            _csv,
            file_name=(f"売上データ_{ym}.csv" if _ja_dl else f"销售数据_{ym}.csv"),
            mime="text/csv",
        )

        # 表格下方：総収益合計 + 粗利合計 の月次推移（上の筛选に追従・全月集計）
        st.divider()
        _tw_sql = " AND ".join(_filt) if _filt else "1=1"
        _trend_df, _terr = _query(
            "SELECT s.year_month AS ym, SUM(s.revenue) AS revenue, "
            "SUM(s.gross_profit) AS gp "
            "FROM nst.sales_monthly s "
            "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
            f"WHERE {_tw_sql} GROUP BY s.year_month ORDER BY s.year_month",
            tuple(_filtp),
        )
        if _trend_df is not None and not _trend_df.empty:
            _ja_t = get_lang() == "ja"
            _rev_l = "総収益" if _ja_t else "总收益"
            _gp_l = "粗利" if _ja_t else "毛利"
            _trend_df["revenue"] = _trend_df["revenue"].astype(float)
            _trend_df["gp"] = _trend_df["gp"].astype(float)
            # page05 と統一: 点線 + 縦ルール hover で総収益/粗利 同時表示（同一金額軸）
            _tx = alt.X("ym:N", sort=None, title=None, axis=alt.Axis(labelAngle=0))
            _tnear = alt.selection_point(nearest=True, on="pointerover",
                                         fields=["ym"], empty=False)
            _tb = alt.Chart(_trend_df).encode(x=_tx)
            _lr = _tb.mark_line(point=True).encode(
                y=alt.Y("revenue:Q", title=("金額" if _ja_t else "金额"),
                        axis=alt.Axis(format=",.0f")),
                color=alt.datum(_rev_l))
            _lg = _tb.mark_line(point=True).encode(
                y=alt.Y("gp:Q", title=None), color=alt.datum(_gp_l))
            _tr = _tb.mark_rule(color="#888").encode(
                opacity=alt.condition(_tnear, alt.value(0.35), alt.value(0)),
                tooltip=[alt.Tooltip("ym:N"),
                         alt.Tooltip("revenue:Q", title=_rev_l, format=",.0f"),
                         alt.Tooltip("gp:Q", title=_gp_l, format=",.0f")],
            ).add_params(_tnear)
            _tch = (alt.layer(_lr, _lg, _tr)
                    .resolve_scale(color="shared")
                    .properties(height=320)
                    .configure_legend(orient="top", title=None))
            st.altair_chart(_tch, use_container_width=True)
            st.caption("総収益合計 / 粗利合計 の月次推移（上の筛选に追従）" if _ja_t
                       else "总收益合计 / 毛利合计 月度趋势（跟随上方筛选变化）")
