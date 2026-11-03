"""模块 #06 库存监控 · 库存构成分析 + 库存健康监控 2 タブ統合（Boss 2026-06-02）.

旧 page33 库存构成分析 + page18 库存风控 を 1 ページ 2 タブに統合。
- tab1 构成分析：弁天(+CGF) 用途/等级構成（内部に 棚別/SKU別/弁天-斑马対账 の 3 子タブ）
- tab2 库存风控：可售天数(弁天输出棚库存/日均销量)による断货 / 压库存リスク監視（shared.risk_control_view）
Boss 2026-11-04: 京东(JD-物流-千葉)库存を全面削除 → 大分弁天へ。NST vs JDL 対账 → 弁天 vs 斑马 対账、京东库存查询タブ削除。
旧「库存健康监控」タブは 2026-06-04 削除。各 body は関数化し st.stop()→return。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st
import plotly.express as px

from shared.bin_categories import CATEGORIES
from shared.db import get_readonly_connection
from shared.i18n import lang_selector, t
from shared.i18n_columns import localize_df
from shared.risk_control_view import render as render_risk_control

st.set_page_config(page_title=t("库存监控"), page_icon="📦", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_readonly_connection()

st.title(t("📦 库存监控"))


def _render_composition():
    st.title(t("📦 库存构成分析（弁天）"))
    st.caption(t("弁天按棚番号分类（输出 / 输出中国 / 返品 / 不良品）· CGF(Coupang) 不分 bin 归通常输出 · 不含京东仓"))


    def _df(sql: str, params=None) -> pd.DataFrame:
        from shared.cache import cached_df, data_version
        return cached_df(conn, sql, params, ver=data_version("inventory", "basic", "sales"))


    # 最新 snapshot_date
    try:
        inv_date = _df("SELECT max(snapshot_date) AS d FROM nst.inventory_snapshot")["d"].iloc[0]
        bin_date_row = _df("SELECT max(snapshot_date) AS d FROM nst.inventory_bin_snapshot")
        bin_date = bin_date_row["d"].iloc[0] if not bin_date_row.empty else None
    except Exception as e:
        st.error(t("⚠️ 库存表读取失败（schema 未部署？）") + f"\n\n{e}")
        return

    if not inv_date:
        st.info(t("尚无库存快照数据。"))
        return

    st.caption(t("库存快照：{d1} · 弁天 bin 快照：{d2}").format(
        d1=str(inv_date), d2=str(bin_date) if bin_date else t("（暂无）")))

    # 全仓库 × item × bin level → 加 category 列（item × cost_estimate → 金额）
    # JD:  除外（Boss 2026-11-04 京东库存を表示しない）
    # CGF: bin_number=NULL → 类别='输出'（Coupang 海外仓·2026-08 新设·bin 无し）
    # 弁天: LEFT JOIN nst.bin_category 按 bool 标识判定（优先级 返品 > 不良品 > 输出中国 > 输出）
    _SQL = """
    WITH inv_all AS (
        SELECT warehouse, item_internal_id, qty_on_hand, NULL::TEXT AS bin_number
        FROM nst.inventory_snapshot
        WHERE snapshot_date = %(d_jd)s AND warehouse NOT IN ('弁天倉庫', 'JD-物流-千葉')
        UNION ALL
        SELECT '弁天倉庫' AS warehouse, item_internal_id, qty_on_hand, bin_number
        FROM nst.inventory_bin_snapshot
        WHERE snapshot_date = %(d_bn)s
    )
    SELECT
        inv_all.warehouse,
        inv_all.bin_number,
        CASE
            WHEN inv_all.warehouse <> '弁天倉庫' THEN '输出'
            WHEN COALESCE(bc.is_return, FALSE) THEN '返品'
            WHEN COALESCE(bc.is_defect, FALSE) THEN '不良品'
            WHEN COALESCE(bc.is_cb,     FALSE) THEN '输出中国'
            ELSE '输出'
        END AS category,
        inv_all.item_internal_id,
        im.item_code, im.jan, im.display_name, im.maker, im.item_rank,
        inv_all.qty_on_hand,
        (inv_all.qty_on_hand * COALESCE(im.cost_estimate, 0))::NUMERIC(16,2) AS amt
    FROM inv_all
    LEFT JOIN nst.item_master_raw im ON im.internal_id = inv_all.item_internal_id
    LEFT JOIN nst.bin_category bc     ON bc.bin_number  = inv_all.bin_number
    """

    try:
        df = _df(_SQL, {"d_jd": inv_date, "d_bn": bin_date or inv_date})
    except Exception as e:
        st.error(t("⚠️ 构成查询失败") + f"\n\n{e}")
        return

    if df.empty:
        st.info(t("无库存数据"))
        return

    df["qty_on_hand"] = pd.to_numeric(df["qty_on_hand"], errors="coerce").fillna(0)
    df["amt"] = pd.to_numeric(df["amt"], errors="coerce").fillna(0)

    # ============================================================
    # 顶部 4 类别构成卡（全仓库合算）
    # ============================================================
    st.subheader(t("📊 构成 KPI（全仓库合算）"))
    by_cat = df.groupby("category", as_index=False).agg(qty=("qty_on_hand", "sum"), amt=("amt", "sum"))
    cat_map = {r["category"]: r for _, r in by_cat.iterrows()}
    tot_amt = float(by_cat["amt"].sum())
    tot_qty = float(by_cat["qty"].sum())

    cc = st.columns(4)
    _labels = {
        "输出": t("通常输出"),
        "输出中国": t("输出中国"),
        "返品": t("返品（HENPIN-EX）"),
        "不良品": t("不良品（FF-3）"),
    }
    for col, cat in zip(cc, CATEGORIES):
        r = cat_map.get(cat)
        amt = float(r["amt"]) if r is not None else 0.0
        qty = float(r["qty"]) if r is not None else 0.0
        pct = (amt / tot_amt * 100) if tot_amt else 0.0
        col.metric(f"{_labels[cat]} (¥)", f"¥{amt:,.0f}",
                   delta=f"{pct:.0f}% {t('占比')} · {qty:,.0f} {t('数量')}",
                   delta_color="off")

    st.caption(t("库存合計 ¥{a:,.0f} · 总数量 {q:,.0f}").format(a=tot_amt, q=tot_qty))

    import altair as alt

    def _rank_label(d: pd.DataFrame) -> pd.Series:
        """item_rank → 表示用ラベル（NULL / 'nan' は「未分类」に寄せる）。"""
        return d["item_rank"].astype(str).where(
            d["item_rank"].notna() & (d["item_rank"].astype(str) != "nan"),
            t("未分类"),
        )

    # 固定顺序: NEW / Aランク / Bランク / Cランク / 取扱中止 / 未分类 / 其他
    _RANK_ORDER = ["NEW", "Aランク", "Bランク", "Cランク", "取扱中止", t("未分类")]

    # ----- 月次推移 -----
    # 弁天は bin スナップショットの月末日（2026-05-27 開始＝それ以前の月は点が無い）。
    #   JD 主倉（inventory_activity_monthly）は Boss 2026-11-04 京东库存削除で外した。
    # 金額 = その月の数量 × **現在の** cost_estimate。原価は履歴を持たないので、
    #   線は数量の増減だけを表す（原価改定のノイズが混ざらない）。ランクも現在値。
    st.markdown("##### " + t("📈 月次推移（近 12 个月）"))
    _TREND_SQL = """
    WITH win AS (
        SELECT (date_trunc('month', CURRENT_DATE) - INTERVAL '11 months')::DATE AS d0,
               to_char(date_trunc('month', CURRENT_DATE) - INTERVAL '11 months', 'YYYY-MM') AS ym0
    ), bin_me AS (
        SELECT to_char(snapshot_date,'YYYY-MM') AS ym, max(snapshot_date) AS d
        FROM nst.inventory_bin_snapshot, win WHERE snapshot_date >= win.d0 GROUP BY 1
    ), snap_me AS (
        -- 月次入出庫の集計が無い倉庫（CGF）は日次スナップショットの月末で代用
        SELECT warehouse, to_char(snapshot_date,'YYYY-MM') AS ym, max(snapshot_date) AS d
        FROM nst.inventory_snapshot, win
        WHERE snapshot_date >= win.d0 AND warehouse NOT IN ('JD-物流-千葉', '弁天倉庫')
        GROUP BY 1, 2
    ), inv_all AS (
        SELECT m.ym, m.d AS as_of, '弁天倉庫' AS warehouse, b.item_internal_id,
               b.qty_on_hand, b.bin_number
        FROM nst.inventory_bin_snapshot b JOIN bin_me m ON m.d = b.snapshot_date
        UNION ALL
        SELECT m.ym, m.d, s.warehouse, s.item_internal_id, s.qty_on_hand, NULL::TEXT
        FROM nst.inventory_snapshot s
        JOIN snap_me m ON m.d = s.snapshot_date AND m.warehouse = s.warehouse
    )
    SELECT
        inv_all.ym, max(inv_all.as_of) AS as_of, inv_all.warehouse,
        CASE
            WHEN inv_all.warehouse <> '弁天倉庫' THEN '输出'
            WHEN COALESCE(bc.is_return, FALSE) THEN '返品'
            WHEN COALESCE(bc.is_defect, FALSE) THEN '不良品'
            WHEN COALESCE(bc.is_cb,     FALSE) THEN '输出中国'
            ELSE '输出'
        END AS category,
        im.item_rank,
        SUM(inv_all.qty_on_hand)::NUMERIC(16,2) AS qty,
        SUM(inv_all.qty_on_hand * COALESCE(im.cost_estimate, 0))::NUMERIC(16,2) AS amt
    FROM inv_all
    LEFT JOIN nst.item_master_raw im ON im.internal_id = inv_all.item_internal_id
    LEFT JOIN nst.bin_category bc     ON bc.bin_number  = inv_all.bin_number
    GROUP BY inv_all.ym, inv_all.warehouse, 4, im.item_rank
    """

    def _trend_chart(d: pd.DataFrame, color_col: str | None, legend_title: str,
                     scheme: str, order: list[str] | None = None,
                     *, zero: bool = True, labels: bool = False) -> alt.Chart:
        """月次推移の折れ線。

        zero=False は Y 軸を 0 起点にしない＝振れ幅を拡大して見せる（単系列向け）。
        複数系列で使うと系列間の大小関係が誤読されるので既定は 0 起点のまま。
        labels=True は各点に金額を直書き（点が数個しかない月次なので成立する）。
        """
        tooltip = [alt.Tooltip("ym:O", title=t("月"))]
        enc = {}
        if color_col:
            enc["color"] = alt.Color(f"{color_col}:N", title=legend_title,
                                     scale=alt.Scale(scheme=scheme),
                                     **({"sort": order} if order else {}))
            tooltip.append(alt.Tooltip(f"{color_col}:N", title=legend_title))
        tooltip += [alt.Tooltip("amt:Q", title=t("金额(¥)"), format=",.0f"),
                    alt.Tooltip("qty:Q", title=t("数量"), format=",.0f")]
        base = alt.Chart(d).encode(
            x=alt.X("ym:O", title=t("月")),
            y=alt.Y("amt:Q", title=t("库存金额(¥)"),
                    scale=alt.Scale(zero=zero, nice=True), axis=alt.Axis(format="~s")),
            tooltip=tooltip, **enc,
        )
        line = base.mark_line(point=alt.OverlayMarkDef(size=70), strokeWidth=2)
        if not labels:
            return line.properties(height=300)
        text = base.mark_text(dy=-14, fontSize=12, fontWeight="bold").encode(
            text=alt.Text("amt:Q", format=",.0f"))
        return (line + text).properties(height=300)

    try:
        tdf = _df(_TREND_SQL)
    except Exception as e:                                    # SQLite 空态 / schema 未部署
        tdf = pd.DataFrame()
        st.caption(t("📈 月次推移暂不可用") + f"（{e}）")

    if not tdf.empty:
        tdf["qty"] = pd.to_numeric(tdf["qty"], errors="coerce").fillna(0)
        tdf["amt"] = pd.to_numeric(tdf["amt"], errors="coerce").fillna(0)

        # 用途 推移（全仓库合算 · 上の構成 KPI カードと同じ口径）
        # 4 類別を重ねると 通常输出(¥70M) が他(¥0.5〜2.4M)を潰して平線に見えるため、
        # 1 類別ずつ表示 + Y 軸 0 起点なし で振れ幅を拡大する（Boss 2026-08-07）。
        sel_cat = st.radio(
            t("用途类别（推移）"), list(CATEGORIES), horizontal=True, index=0,
            format_func=lambda c: _labels.get(c, c), key="trend_cat_sel",
        )
        t_cat = (tdf[tdf["category"] == sel_cat]
                 .groupby("ym", as_index=False).agg(qty=("qty", "sum"), amt=("amt", "sum")))
        st.altair_chart(
            _trend_chart(t_cat, None, t("用途"), "tableau10", zero=False, labels=True)
            .properties(title=f"{_labels.get(sel_cat, sel_cat)} {t('推移')}"),
            use_container_width=True)
        if len(t_cat) >= 2:
            _prev, _last = float(t_cat["amt"].iloc[-2]), float(t_cat["amt"].iloc[-1])
            _diff = _last - _prev
            _pct = (_diff / _prev * 100) if _prev else 0.0
            st.caption(t("最新月 ¥{a:,.0f} · 环比 {s}¥{d:,.0f}（{p:+.1f}%）· 期间内 最小 ¥{lo:,.0f} / 最大 ¥{hi:,.0f}").format(
                a=_last, s="+" if _diff >= 0 else "-", d=abs(_diff), p=_pct,
                lo=float(t_cat["amt"].min()), hi=float(t_cat["amt"].max())))

        _bn_from = tdf.loc[tdf["as_of"].notna(), "ym"].min()
        st.caption(t("弁天＝bin 快照月末值，{b} 起才有 · 不含京东仓 · "
                     "当月为进行中月份 · 金额按当前定义原价换算").format(
            b=str(_bn_from) if pd.notna(_bn_from) else "—"))

    st.divider()

    # ----- 各 ランク 库存金额（A/B/C/NEW/取扱中止/未分类 等） -----
    st.markdown("##### " + t("📊 各等级 库存金额"))
    _wh_opts_rank = [t("全部")] + sorted(df["warehouse"].dropna().unique().tolist())
    sel_wh_rank = st.radio(
        t("仓库范围（等级构成）"), _wh_opts_rank, horizontal=True, index=0, key="rank_wh_sel",
    )
    df_rank = df.copy() if sel_wh_rank == t("全部") else df[df["warehouse"] == sel_wh_rank].copy()
    df_rank["rank_label"] = _rank_label(df_rank)
    by_rank = (df_rank.groupby("rank_label", as_index=False)
               .agg(qty=("qty_on_hand", "sum"), amt=("amt", "sum")))
    by_rank["_ord"] = by_rank["rank_label"].map(
        lambda x: _RANK_ORDER.index(x) if x in _RANK_ORDER else len(_RANK_ORDER))
    by_rank = by_rank.sort_values(["_ord", "rank_label"]).drop(columns=["_ord"])
    if not by_rank.empty:
        _per_row = 3
        _rank_rows = list(by_rank.iterrows())
        for _i in range(0, len(_rank_rows), _per_row):
            rank_cols = st.columns(_per_row)
            for col, (_, r) in zip(rank_cols, _rank_rows[_i:_i + _per_row]):
                pct_r = (float(r["amt"]) / tot_amt * 100) if tot_amt else 0.0
                col.metric(f"{r['rank_label']} (¥)", f"¥{float(r['amt']):,.0f}",
                           delta=f"{pct_r:.0f}% {t('占比')} · {float(r['qty']):,.0f} {t('数量')}",
                           delta_color="off")

    # ----- 等级别 月次推移（上の「仓库范围」ラジオに追従） -----
    if not tdf.empty:
        t_rank_src = tdf.copy() if sel_wh_rank == t("全部") else tdf[tdf["warehouse"] == sel_wh_rank].copy()
        t_rank_src["rank_label"] = _rank_label(t_rank_src)
        t_rank = t_rank_src.groupby(["ym", "rank_label"], as_index=False).agg(
            qty=("qty", "sum"), amt=("amt", "sum"))
        st.altair_chart(
            _trend_chart(t_rank, "rank_label", t("等级"), "set2",
                         _RANK_ORDER).properties(title=t("等级别 推移")),
            use_container_width=True)

    # ----- 环状图（用途 + 等级 并排） -----
    st.markdown("##### " + t("🍩 构成 环状图"))
    g1, g2 = st.columns(2)

    # 用途 donut
    _donut_cat = pd.DataFrame([
        {"category": _labels.get(c, c), "amt": float(cat_map[c]["amt"]) if c in cat_map else 0.0}
        for c in CATEGORIES
    ])
    _donut_cat = _donut_cat[_donut_cat["amt"] > 0]
    if not _donut_cat.empty:
        chart_cat = (
            alt.Chart(_donut_cat)
            .mark_arc(innerRadius=60, outerRadius=110)
            .encode(
                theta=alt.Theta("amt:Q", stack=True),
                color=alt.Color("category:N", title=t("用途"),
                                scale=alt.Scale(scheme="tableau10")),
                tooltip=[alt.Tooltip("category:N", title=t("用途")),
                         alt.Tooltip("amt:Q", title=t("金额(¥)"), format=",.0f")],
            )
            .properties(height=300, title=t("用途构成"))
        )
        g1.altair_chart(chart_cat, use_container_width=True)
    else:
        g1.info(t("无数据"))

    # 等级 donut
    _donut_rank = by_rank[by_rank["amt"] > 0].copy()
    if not _donut_rank.empty:
        chart_rank = (
            alt.Chart(_donut_rank)
            .mark_arc(innerRadius=60, outerRadius=110)
            .encode(
                theta=alt.Theta("amt:Q", stack=True),
                color=alt.Color("rank_label:N", title=t("等级"),
                                scale=alt.Scale(scheme="set2")),
                tooltip=[alt.Tooltip("rank_label:N", title=t("等级")),
                         alt.Tooltip("amt:Q", title=t("金额(¥)"), format=",.0f")],
            )
            .properties(height=300, title=t("等级构成"))
        )
        g2.altair_chart(chart_rank, use_container_width=True)
    else:
        g2.info(t("无数据"))

    st.divider()

    # ============================================================
    # 仓库 × 类别 交叉表
    # ============================================================
    st.subheader(t("🏬 仓库 × 类别 交叉表"))
    wh_cat = df.groupby(["warehouse", "category"], as_index=False).agg(
        qty=("qty_on_hand", "sum"), amt=("amt", "sum"))
    piv = wh_cat.pivot(index="warehouse", columns="category", values="amt").fillna(0)
    for c in CATEGORIES:
        if c not in piv.columns:
            piv[c] = 0.0
    piv = piv[list(CATEGORIES)]
    piv["合計"] = piv.sum(axis=1)
    piv.loc["合計"] = piv.sum(axis=0)
    piv = piv.reset_index()
    piv.columns = [t("仓库")] + [_labels.get(c, c) for c in CATEGORIES] + [t("合計")]
    st.dataframe(
        piv, hide_index=True, use_container_width=True,
        column_config={
            c: st.column_config.NumberColumn(format="¥%,.0f")
            for c in piv.columns if c != t("仓库")
        },
    )

    st.divider()

    # ============================================================
    # tab 内 构成 KPI 渲染辅助（拆 2 步：先 pick 类别 · 再 render KPI 卡）
    # ============================================================
    def _pick_categories(key_prefix: str) -> list[str]:
        return st.multiselect(
            t("用途类别（多选 · 留空 = 全部）"), list(CATEGORIES),
            default=[], format_func=lambda c: _labels.get(c, c),
            key=f"{key_prefix}_kpi_cat",
        )


    def _render_kpi_cards(d: pd.DataFrame, sel: list[str]) -> None:
        by_c = d.groupby("category", as_index=False).agg(
            qty=("qty_on_hand", "sum"), amt=("amt", "sum"))
        cmap = {r["category"]: r for _, r in by_c.iterrows()}
        tot = float(by_c["amt"].sum())
        cards = sel if sel else list(CATEGORIES)
        if not cards:
            return
        cols = st.columns(len(cards))
        for col, cat in zip(cols, cards):
            r = cmap.get(cat)
            amt_v = float(r["amt"]) if r is not None else 0.0
            qty_v = float(r["qty"]) if r is not None else 0.0
            pct = (amt_v / tot * 100) if tot else 0.0
            col.metric(f"{_labels[cat]} (¥)", f"¥{amt_v:,.0f}",
                       delta=f"{pct:.0f}% {t('占比')} · {qty_v:,.0f} {t('数量')}",
                       delta_color="off")


    # ============================================================
    # tab1 按棚番号查商品（弁天）
    # tab2 按 SKU 查棚分布（弁天 + CGF）
    # ============================================================
    tab1, tab2, tab4 = st.tabs([
        t("🔎 按棚番号查商品（弁天）"),
        t("🔍 按 SKU 查库存分布（全仓库）"),
        t("📊 弁天 vs 斑马 库存对账"),
    ])

    bn_df = df[df["warehouse"] == "弁天倉庫"].copy()

    with tab1:
        # 用途类别 multi-select 置顶
        sel_cats_t1 = _pick_categories("t1")

        _bin_opts_t1 = sorted(bn_df["bin_number"].dropna().astype(str).unique().tolist())
        _maker_opts_t1 = sorted(bn_df["maker"].dropna().astype(str).unique().tolist())
        _rank_opts_t1 = sorted(bn_df["item_rank"].dropna().astype(str).unique().tolist())
        c1, c2, c3, c4 = st.columns([3, 3, 2, 2])
        sel_bins_t1 = c1.multiselect(
            t("棚番号(多选 · 留空 = 全部棚)"), _bin_opts_t1, default=[], key="bin_filter_ms",
        )
        sel_makers_t1 = c2.multiselect(
            t("メーカー(多选 · 留空 = 全部)"), _maker_opts_t1, default=[], key="t1_maker",
        )
        sel_ranks_t1 = c3.multiselect(
            t("等级(多选 · 留空 = 全部)"), _rank_opts_t1, default=[], key="t1_rank",
        )
        show_zero = c4.checkbox(t("含 0 库存"), value=False)

        sub = bn_df.copy()
        if sel_cats_t1:
            sub = sub[sub["category"].isin(sel_cats_t1)]
        if sel_bins_t1:
            sub = sub[sub["bin_number"].astype(str).isin(sel_bins_t1)]
        if sel_makers_t1:
            sub = sub[sub["maker"].astype(str).isin(sel_makers_t1)]
        if sel_ranks_t1:
            sub = sub[sub["item_rank"].astype(str).isin(sel_ranks_t1)]
        if not show_zero:
            sub = sub[sub["qty_on_hand"] > 0]

        # KPI 卡（按上方筛选实时算）
        _render_kpi_cards(sub, sel_cats_t1)
        st.divider()

        if sub.empty:
            st.info(t("无匹配数据"))
        else:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric(t("命中棚数"), f"{sub['bin_number'].nunique():,}")
            k2.metric(t("商品行数"), f"{len(sub):,}")
            k3.metric(t("在庫合計"), f"{sub['qty_on_hand'].sum():,.0f}")
            k4.metric(t("金额合計 (¥)"), f"¥{sub['amt'].sum():,.0f}")

            disp = sub[["bin_number", "category", "item_code", "jan", "display_name",
                        "maker", "item_rank", "qty_on_hand", "amt"]].rename(columns={
                "bin_number": t("棚番号"), "category": t("用途"),
                "item_code": t("商品コード"), "jan": t("JAN"),
                "display_name": t("商品名"), "maker": t("メーカー"),
                "item_rank": t("等级"), "qty_on_hand": t("手持"),
                "amt": t("金额(¥)"),
            }).sort_values([t("棚番号"), t("商品コード")])
            st.dataframe(disp, hide_index=True, use_container_width=True, height=540,
                         column_config={t("金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})
            st.download_button(t("📥 CSV 下载"),
                               disp.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"bin_query_{bin_date}.csv", mime="text/csv")

    with tab2:
        # 用途类别 multi-select 置顶
        sel_cats_t2 = _pick_categories("t2")

        _maker_opts_t2 = sorted(df["maker"].dropna().astype(str).unique().tolist())
        _rank_opts_t2 = sorted(df["item_rank"].dropna().astype(str).unique().tolist())
        c1, c2, c3 = st.columns([4, 3, 3])
        sku_in = c1.text_input(t("商品コード / JAN（部分匹配 · 留空 = 全部）"), "", key="sku_filter")
        sel_makers_t2 = c2.multiselect(
            t("メーカー(多选 · 留空 = 全部)"), _maker_opts_t2, default=[], key="t2_maker",
        )
        sel_ranks_t2 = c3.multiselect(
            t("等级(多选 · 留空 = 全部)"), _rank_opts_t2, default=[], key="t2_rank",
        )

        if sku_in.strip():
            s = sku_in.strip().lower()
            sub2 = df[
                df["item_code"].astype(str).str.lower().str.contains(s, na=False)
                | df["jan"].astype(str).str.lower().str.contains(s, na=False)
            ].copy()
        else:
            sub2 = df.copy()
        if sel_cats_t2:
            sub2 = sub2[sub2["category"].isin(sel_cats_t2)]
        if sel_makers_t2:
            sub2 = sub2[sub2["maker"].astype(str).isin(sel_makers_t2)]
        if sel_ranks_t2:
            sub2 = sub2[sub2["item_rank"].astype(str).isin(sel_ranks_t2)]
        sub2 = sub2[sub2["qty_on_hand"] > 0]

        # KPI 卡（按上方筛选实时算）
        _render_kpi_cards(sub2, sel_cats_t2)
        st.divider()

        if sub2.empty:
            st.info(t("无匹配数据"))
        else:
            if not sku_in.strip():
                st.caption(t("提示：输入商品コード / JAN 可精确定位 · 当前展示全仓库筛选后明细"))
            disp2 = sub2[["item_code", "jan", "display_name", "warehouse",
                          "bin_number", "category", "qty_on_hand", "amt"]].rename(columns={
                "item_code": t("商品コード"), "jan": t("JAN"),
                "display_name": t("商品名"), "warehouse": t("仓库"),
                "bin_number": t("棚番号"), "category": t("用途"),
                "qty_on_hand": t("手持"), "amt": t("金额(¥)"),
            }).sort_values([t("商品コード"), t("仓库"), t("棚番号")])
            disp2[t("棚番号")] = disp2[t("棚番号")].fillna(t("（不分 bin）"))
            st.dataframe(disp2, hide_index=True, use_container_width=True, height=480,
                         column_config={t("金额(¥)"): st.column_config.NumberColumn(format="¥%,.0f")})
            st.download_button(t("📥 CSV 下载"),
                               disp2.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"sku_inv_{inv_date}.csv", mime="text/csv",
                               key="sku_csv")

    # ============================================================
    # tab4 弁天 vs 斑马 库存对账（Boss 2026-11-04 · 旧 NST vs JDL 对账を置換）
    #   NST 弁天「输出」棚 vs 斑马 三金弁天仓库 × Export 合格品 · 口径は shared.benten_recon
    #   斑马側の拉取: banma_inventory_daily（毎日 03:20 JST）
    # ============================================================
    with tab4:
        from shared import benten_recon as BR
        from shared.jdl_recon_settings import load_recon_params

        try:
            rec = BR.load(_df)
        except Exception as e:
            st.error(t("⚠️ 对账数据读取失败") + f"\n\n{e}")
            return
        if rec.empty:
            st.info(t("尚无对账数据（斑马库存每天 03:20 拉取）。"))
            return

        _nd = rec["nst_date"].dropna().max() if "nst_date" in rec else None
        _bd = rec["bm_date"].dropna().max() if "bm_date" in rec else None
        st.caption(t(
            "NST=弁天「输出」棚账面（不含输出中国/返品/不良品）vs 斑马=三金弁天仓库 × Export 类目 合格品 · "
            "NST 快照: {n} · 斑马快照: {b}"
        ).format(n=str(_nd) if pd.notna(_nd) else "—", b=str(_bd) if pd.notna(_bd) else "—"))

        exclude_zero_only = st.checkbox(
            t("排除 仅 NST 有 / 仅斑马有 中实际数量为 0 的项"),
            value=True, key="rec_exclude_zero_only",
            help=t("一侧没有记录、另一侧数量也是 0 的行无需对账"),
        )
        if exclude_zero_only:
            zero_only = (((rec["status"] == "NST_ONLY") & (rec["nst_qty"].fillna(0) == 0))
                         | ((rec["status"] == "BANMA_ONLY") & (rec["bm_qty"].fillna(0) == 0)))
            rec = rec[~zero_only].copy()

        _recon_p = load_recon_params()
        _abs = int(_recon_p["minor_abs_threshold"])
        _pct = float(_recon_p["minor_pct_threshold"])

        def _status_label(code: str) -> str:
            if code == "OK":
                return t("✅ 一致")
            if code == "MINOR_DIFF":
                if _pct > 0:
                    return t("🟡 轻微差异 (≤{n} 或 {p:.1f}%)").format(n=_abs, p=_pct)
                return t("🟡 轻微差异 (≤{n})").format(n=_abs)
            if code == "MAJOR_DIFF":
                return t("🔴 重大差异")
            if code == "NST_ONLY":
                return t("🟠 仅 NST 有")
            if code == "BANMA_ONLY":
                return t("🟣 仅斑马有")
            return code

        counts = rec["status"].value_counts().to_dict()
        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric(_status_label("OK"), counts.get("OK", 0))
        s2.metric(_status_label("MINOR_DIFF"), counts.get("MINOR_DIFF", 0))
        s3.metric(_status_label("MAJOR_DIFF"), counts.get("MAJOR_DIFF", 0),
                  delta=t("需调查") if counts.get("MAJOR_DIFF", 0) > 0 else None,
                  delta_color="inverse")
        s4.metric(_status_label("NST_ONLY"), counts.get("NST_ONLY", 0),
                  help=t("NST 弁天输出棚有 · 斑马弁天仓 Export 无"))
        s5.metric(_status_label("BANMA_ONLY"), counts.get("BANMA_ONLY", 0),
                  help=t("斑马弁天仓 Export 有 · NST 弁天输出棚无"))

        f1, f2, f3 = st.columns([2, 2, 3])
        sel_status = f1.multiselect(
            t("状态筛选"), list(BR.STATUSES),
            default=["MAJOR_DIFF", "NST_ONLY", "BANMA_ONLY"],
            format_func=_status_label, key="rec_status",
        )
        _maker_opts = sorted(rec["maker"].dropna().astype(str).unique().tolist())
        sel_maker = f2.multiselect(t("メーカー"), _maker_opts, default=[], key="rec_maker")
        search_jan = f3.text_input(t("🔍 JAN / 名称模糊搜索"), key="rec_search")

        df_rec = rec.copy()
        if sel_status:
            df_rec = df_rec[df_rec["status"].isin(sel_status)]
        if sel_maker:
            df_rec = df_rec[df_rec["maker"].astype(str).isin(sel_maker)]
        if search_jan.strip():
            q = search_jan.strip()
            df_rec = df_rec[
                df_rec["jan"].astype(str).str.contains(q, case=False, na=False)
                | df_rec["display_name"].astype(str).str.contains(q, case=False, na=False)
            ]
        df_rec = df_rec.assign(_abs_diff=df_rec["diff"].abs()).sort_values(
            "_abs_diff", ascending=False).drop(columns=["_abs_diff"])
        df_rec["status"] = df_rec["status"].map(_status_label)
        st.caption(t("命中 {n} 条").format(n=len(df_rec)))

        show_cols = ["status", "jan", "item_code", "display_name", "maker", "item_rank",
                     "nst_qty", "bm_qty", "diff", "bm_available", "bm_lock", "bm_last_out"]
        col_names = {
            "status": t("状态"), "jan": "JAN", "item_code": t("商品コード"),
            "display_name": t("商品名"), "maker": t("メーカー"), "item_rank": t("等级"),
            "nst_qty": t("NST 弁天输出"), "bm_qty": t("斑马 合格品"),
            "diff": t("差异 (NST−斑马)"), "bm_available": t("斑马 可用"),
            "bm_lock": t("斑马 锁定"), "bm_last_out": t("斑马 最后出库"),
        }
        st.dataframe(
            df_rec[show_cols], hide_index=True, use_container_width=True, height=560,
            column_config={
                "status": st.column_config.TextColumn(col_names["status"], width="small"),
                "jan": st.column_config.TextColumn("JAN", width="small"),
                "item_code": st.column_config.TextColumn(col_names["item_code"], width="small"),
                "display_name": st.column_config.TextColumn(col_names["display_name"], width="large"),
                "maker": st.column_config.TextColumn(col_names["maker"], width="small"),
                "item_rank": st.column_config.TextColumn(col_names["item_rank"], width="small"),
                "nst_qty": st.column_config.NumberColumn(col_names["nst_qty"], format="%d"),
                "bm_qty": st.column_config.NumberColumn(col_names["bm_qty"], format="%d"),
                "diff": st.column_config.NumberColumn(col_names["diff"], format="%+d"),
                "bm_available": st.column_config.NumberColumn(col_names["bm_available"], format="%d"),
                "bm_lock": st.column_config.NumberColumn(col_names["bm_lock"], format="%d"),
                "bm_last_out": st.column_config.DatetimeColumn(col_names["bm_last_out"]),
            },
        )
        st.download_button(
            t("📥 下载对账 CSV"),
            df_rec[show_cols].rename(columns=col_names).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"benten_banma_reconciliation_{_bd}.csv", mime="text/csv", key="rec_csv",
        )


tab_comp, tab_risk = st.tabs([t("📦 库存构成分析"), t("🛡️ 库存风控")])
with tab_comp:
    _render_composition()
with tab_risk:
    render_risk_control(conn)
