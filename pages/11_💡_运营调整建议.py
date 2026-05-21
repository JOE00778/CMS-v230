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

st.title(t("💡 运营调整建议（B/C 档）"))

_main_tab, _weekly_tab = st.tabs([t("💡 月度运营建议（B/C 档）"), t("📉 周次销量落差")])

with _weekly_tab:
    import datetime as _dtw
    _L = lambda zh, ja: ja if get_lang() == "ja" else zh
    st.markdown("#### " + _L("周次 · 店铺 × 商品 销量落差", "週次 · 店舗 × 商品 売上落差"))
    st.caption(_L(
        "本周 vs 上周 销量环比 · 落差大的按店铺列出 · 便于分析原因（缺货 / 活动 / 竞品 等）",
        "今週 vs 前週 の販売数量を環比 · 落差の大きい順に店舗別で一覧 · 原因分析用（欠品 / 施策 / 競合 等）",
    ))
    _wconn = get_connection()
    try:
        _rng = _wconn.execute(
            "SELECT MIN(sale_date) AS mn, MAX(sale_date) AS mx FROM nst.sales_daily"
        ).fetchone()
    except Exception as _ew:
        _rng = None
        st.error(_L("売上日次データ未取得: ", "売上日次データ未取得: ") + str(_ew))
    if not _rng or not _rng["mx"]:
        st.info(_L("売上日次データがありません（page 27 で sales を取得）",
                   "売上日次データがありません（page 27 で sales を取得）"))
    else:
        _mx = _rng["mx"]
        if isinstance(_mx, str):
            _mx = _dtw.date.fromisoformat(_mx[:10])
        _this_mon = _mx - _dtw.timedelta(days=_mx.weekday())
        _weeks = [_this_mon - _dtw.timedelta(weeks=_i) for _i in range(12)]

        def _wk_label(_m):
            return f"{_m.isoformat()} ~ {(_m + _dtw.timedelta(days=6)).isoformat()}"

        _c1, _c2, _c3 = st.columns([2, 1, 1])
        _sel_mon = _c1.selectbox(_L("对象周（周一起点）", "対象週（月曜起点）"),
                                 _weeks, format_func=_wk_label, index=0)
        _direction = _c2.radio(_L("方向", "方向"),
                               [_L("销量下降", "数量減少"), _L("销量上升", "数量増加"),
                                _L("落差绝对值", "落差絶対値")], index=0)
        _min_delta = _c3.number_input(_L("最小落差（件）", "最小落差（件）"),
                                      min_value=0, value=5, step=1)
        _cur_s, _cur_e = _sel_mon, _sel_mon + _dtw.timedelta(days=6)
        _prev_s, _prev_e = _sel_mon - _dtw.timedelta(days=7), _sel_mon - _dtw.timedelta(days=1)
        if _cur_e >= _dtw.date.today():
            st.caption(_L("⚠️ 对象周可能进行中（截至当日）· 落差为暂定值",
                          "⚠️ 対象週は進行中の可能性あり（当日まで）· 落差は暫定値"))
        _shops = [_r["shop"] for _r in _wconn.execute(
            "SELECT DISTINCT shop FROM nst.sales_daily "
            "WHERE sale_date BETWEEN ? AND ? ORDER BY shop",
            (_prev_s.isoformat(), _cur_e.isoformat()),
        ).fetchall()]
        _ALLW = _L("（全部店铺）", "（全店舗）")
        _shop_sel = st.selectbox(_L("店铺", "店舗"), [_ALLW] + _shops)
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
        else:
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
            _k1.metric(_L("对象周", "対象週"), _wk_label(_sel_mon))
            _k2.metric(_L("落差 SKU 数", "落差 SKU 数"), f"{len(_wdf):,}")
            _k3.metric(_L("销量净变化（件）", "数量純変化（件）"), f"{_wdf['delta'].sum():,.0f}")
            for _col in ("qty_cur", "qty_prev", "delta", "rev_cur", "rev_prev"):
                _wdf[_col] = _wdf[_col].round(0).astype(int)
            _ja = get_lang() == "ja"
            _hdr = {
                "shop": ("店铺", "店舗"), "jan": ("UPC编码", "UPCコード"),
                "name": ("商品名", "商品名"), "item_rank": ("等级", "ランク"),
                "qty_prev": ("上周销量", "前週数量"), "qty_cur": ("本周销量", "今週数量"),
                "delta": ("落差", "落差"), "delta_pct": ("落差%", "落差%"),
                "rev_prev": ("上周营业额", "前週売上"), "rev_cur": ("本周营业额", "今週売上"),
            }
            _cols = ["shop", "jan", "name", "item_rank", "qty_prev", "qty_cur",
                     "delta", "delta_pct", "rev_prev", "rev_cur"]
            _show = _wdf[_cols].rename(
                columns={_k2c: (_v[1] if _ja else _v[0]) for _k2c, _v in _hdr.items()})
            st.dataframe(_show, use_container_width=True, height=520)
            st.caption(_L(
                "落差 = 本周销量 − 上周销量 · 负值=下降 · 选择店铺可逐店分析原因",
                "落差 = 今週数量 − 前週数量 · マイナス=減少 · 店舗を選んで個別に原因分析",
            ))
    _wconn.close()

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
