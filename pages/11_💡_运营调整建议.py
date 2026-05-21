"""模块 ② 运营调整建议 · 基于「毛利 × 周转」双轴矩阵 · B/C 档 SKU。

数据：operation_advice_monthly（由 modules.operation_advice.proposal.generate_advice 写入）
"""
from __future__ import annotations
import streamlit as st
from shared.i18n import t, lang_selector, get_lang
from shared.i18n_columns import localize_df
import pandas as pd
import sqlite3
from pathlib import Path

from shared.owners import classify_market
from modules.operation_advice.proposal import generate_advice
from modules.operation_advice.rules import (
    MARGIN_LOW, MARGIN_HIGH, TURNOVER_LOW, TURNOVER_HIGH
)

st.set_page_config(page_title=t("运营调整建议"), page_icon="💡", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
# 本ページのみ：データ表が画面幅いっぱいに広がるよう全局 1400px 上限を解除
st.markdown(
    "<style>[data-testid='stMainBlockContainer'],.main .block-container"
    "{max-width:100%!important;}</style>",
    unsafe_allow_html=True,
)
lang_selector()

from shared.db import DB_PATH, get_connection
DB = DB_PATH

st.title(t("💡 运营调整建议（B/C 档）"))

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
    st.dataframe(_show, use_container_width=True, height=520)
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
            "SELECT s.sale_date AS d, SUM(s.qty_sold) AS q, SUM(s.revenue) AS r "
            "FROM nst.sales_daily s "
            "JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
            "WHERE " + _extra_where + " AND s.sale_date BETWEEN ? AND ? "
            "GROUP BY s.sale_date",
            tuple(_ps) + (_w_start.isoformat(), _w_end.isoformat()),
        ).fetchall()
        _dq, _dr = {}, {}
        for _tr in _ts_rows:
            _dd = _tr["d"]
            if isinstance(_dd, str):
                _dd = _dtw.date.fromisoformat(_dd[:10])
            _dq[_dd] = _dq.get(_dd, 0.0) + float(_tr["q"] or 0)
            _dr[_dd] = _dr.get(_dd, 0.0) + float(_tr["r"] or 0)
        _period_col = _L("期间", "期間")
        _qty_col = _L("销量", "数量")
        _rev_col = _L("营业额", "売上")
        _ratio_col = (_L("前周比", "前週比") if _period_kind == "week"
                      else _L("前月比", "前月比"))
        _rows, _prev_q = [], None
        for _p in _recent:
            _cs, _ce, _, _ = _bounds(_p)
            _clbl = _cs.strftime("%m/%d") if _period_kind == "week" else _p.strftime("%Y-%m")
            _q = sum(_v for _d, _v in _dq.items() if _cs <= _d <= _ce)
            _r = sum(_v for _d, _v in _dr.items() if _cs <= _d <= _ce)
            _ratio = f"{_q / _prev_q * 100:.1f}%" if _prev_q else "—"
            _rows.append({
                _period_col: _clbl,
                _qty_col: int(round(_q)),
                _rev_col: int(round(_r)),
                _ratio_col: _ratio,
            })
            _prev_q = _q
        _tbl = pd.DataFrame(_rows).set_index(_period_col)
        # 曲线图上方：每期 销量 / 营业额 / 前期比
        st.dataframe(_tbl, use_container_width=True)
        st.line_chart(_tbl[[_qty_col]], height=300)
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
    _f_market = _f1.selectbox(_L("市场", "市場"), [_ALL] + _markets, key=_key + "_tmarket")
    _f_shop = _f2.selectbox(_L("店铺", "店舗"), [_ALL] + _all_shops, key=_key + "_tshop")
    _f_maker = _f3.selectbox(_L("品牌", "メーカー"), [_ALL] + _all_makers, key=_key + "_tmaker")
    _f_jan = _f4.text_input(
        _L("SKU（JAN 搜索）", "SKU（JAN 検索）"), key=_key + "_tjan",
        placeholder=_L("JAN 部分一致（留空=不限）", "JAN 部分一致（空欄=指定なし）"),
    )
    _wc, _wp, _caps = [], [], []
    if _f_market != _ALL:
        _mshops = [_s for _s in _all_shops if _market_of.get(_s) == _f_market]
        if _mshops:
            _wc.append("s.shop IN (" + ",".join(["?"] * len(_mshops)) + ")")
            _wp.extend(_mshops)
            _caps.append(_f_market)
    if _f_shop != _ALL:
        _wc.append("s.shop = ?"); _wp.append(_f_shop); _caps.append(_f_shop)
    if _f_maker != _ALL:
        _wc.append("im.maker = ?"); _wp.append(_f_maker); _caps.append(_f_maker)
    if _f_jan.strip():
        _wc.append("im.jan LIKE ?"); _wp.append(f"%{_f_jan.strip()}%")
        _caps.append("JAN:" + _f_jan.strip())
    _extra = " AND ".join(_wc) if _wc else "1=1"
    _cap_label = " · ".join(_caps) if _caps else _L("全部", "全件")
    _draw_trend(_extra, _wp, _cap_label)
    _wconn.close()


_main_tab, _weekly_tab, _monthly_tab = st.tabs([
    t("💡 月度运营建议（B/C 档）"), t("📉 周次销量落差"), t("📅 月度销量落差"),
])

with _weekly_tab:
    _render_sales_delta("week", "wk")

with _monthly_tab:
    _render_sales_delta("month", "mo")

with _main_tab:
    st.caption(t(
        f"双轴矩阵：毛利率 × 月周转率 → 5 档建议 · "
        f"阈值 毛利{MARGIN_LOW}/{MARGIN_HIGH}% · 周转{TURNOVER_LOW}/{TURNOVER_HIGH}"
    ))

    # 月度选择器 + 重算
    col_ym, col_recalc = st.columns([2, 1])
    with col_ym:
        ym = st.selectbox(t("月度"), ["2026-04"], index=0)
    with col_recalc:
        if st.button(t("🔄 重新计算")):
            with st.spinner(t("生成中...")):
                generate_advice(ym, str(DB))
            st.success(t("✅ 已更新"))
            st.rerun()

    # 数据加载 — 直接字符串拼接, 不用占位符 (ym 是 selectbox 固定值, 安全)
    # 仅保留只允许 [0-9-] 的白名单防御
    import re as _re
    _safe_ym = _re.sub(r"[^0-9-]", "", str(ym))[:10]
    conn = get_connection()
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
            "FROM nst_inventory_snapshot WHERE location='JD-物流-千葉' "
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
        st.markdown(t("**全部 1,681 条建议**"))
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
