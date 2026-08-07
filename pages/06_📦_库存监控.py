"""模块 #06 库存监控 · 库存构成分析 + 库存健康监控 2 タブ統合（Boss 2026-06-02）.

旧 page33 库存构成分析 + page18 库存风控 を 1 ページ 2 タブに統合。
- tab1 构成分析：JD+弁天 用途/等级構成（内部に 棚別/SKU別/NST-JDL対账/JDL照会 の 4 子タブ）
- tab2 库存风控：可售天数(JDL库存/日均销量)による断货 / 压库存リスク監視（shared.risk_control_view）
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
    st.title(t("📦 库存构成分析（JD + 弁天）"))
    st.caption(t("JD 不分 bin 全部归通常输出 · 弁天按棚番号分类（输出 / 输出中国 / 返品 / 不良品）"))


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

    st.caption(t("JD 快照：{d1} · 弁天 bin 快照：{d2}").format(
        d1=str(inv_date), d2=str(bin_date) if bin_date else t("（暂无）")))

    # 全仓库 × item × bin level → 加 category 列（item × cost_estimate → 金额）
    # JD: bin_number=NULL → 类别='输出'（JD 不分 bin）
    # 弁天: LEFT JOIN nst.bin_category 按 bool 标识判定（优先级 返品 > 不良品 > 输出中国 > 输出）
    _SQL = """
    WITH inv_all AS (
        SELECT 'JD-物流-千葉' AS warehouse, item_internal_id, qty_on_hand, NULL::TEXT AS bin_number
        FROM nst.inventory_snapshot
        WHERE snapshot_date = %(d_jd)s AND warehouse = 'JD-物流-千葉'
        UNION ALL
        SELECT '弁天倉庫' AS warehouse, item_internal_id, qty_on_hand, bin_number
        FROM nst.inventory_bin_snapshot
        WHERE snapshot_date = %(d_bn)s
    )
    SELECT
        inv_all.warehouse,
        inv_all.bin_number,
        CASE
            WHEN inv_all.warehouse = 'JD-物流-千葉' THEN '输出'
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

    # ----- 各 ランク 库存金额（A/B/C/NEW/取扱中止/未分类 等） -----
    st.markdown("##### " + t("📊 各等级 库存金额"))
    _wh_opts_rank = [t("全部")] + sorted(df["warehouse"].dropna().unique().tolist())
    sel_wh_rank = st.radio(
        t("仓库范围（等级构成）"), _wh_opts_rank, horizontal=True, index=0, key="rank_wh_sel",
    )
    def _rank_label(d: pd.DataFrame) -> pd.Series:
        """item_rank → 表示用ラベル（NULL / 'nan' は「未分类」に寄せる）。"""
        return d["item_rank"].astype(str).where(
            d["item_rank"].notna() & (d["item_rank"].astype(str) != "nan"),
            t("未分类"),
        )

    df_rank = df.copy() if sel_wh_rank == t("全部") else df[df["warehouse"] == sel_wh_rank].copy()
    df_rank["rank_label"] = _rank_label(df_rank)
    by_rank = (df_rank.groupby("rank_label", as_index=False)
               .agg(qty=("qty_on_hand", "sum"), amt=("amt", "sum")))
    # 固定顺序: NEW / Aランク / Bランク / Cランク / 取扱中止 / 未分类 / 其他
    _RANK_ORDER = ["NEW", "Aランク", "Bランク", "Cランク", "取扱中止", t("未分类")]
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

    import altair as alt

    # ----- 月次推移（各月の最終スナップショット日を月末点として採る） -----
    # 金額 = その月の数量 × **現在の** cost_estimate。原価は日次スナップショットが
    # 無いので、線は数量の増減だけを表す（原価改定のノイズが混ざらない）。
    # ランクも同じく現在値での遡及ラベル（ランク履歴テーブルは無い）。
    st.markdown("##### " + t("📈 月次推移（近 12 个月）"))
    _TREND_SQL = """
    WITH win AS (
        SELECT (date_trunc('month', CURRENT_DATE) - INTERVAL '11 months')::DATE AS d0
    ), inv_me AS (
        SELECT to_char(snapshot_date,'YYYY-MM') AS ym, max(snapshot_date) AS d
        FROM nst.inventory_snapshot, win WHERE snapshot_date >= win.d0 GROUP BY 1
    ), bin_me AS (
        SELECT to_char(snapshot_date,'YYYY-MM') AS ym, max(snapshot_date) AS d
        FROM nst.inventory_bin_snapshot, win WHERE snapshot_date >= win.d0 GROUP BY 1
    ), inv_all AS (
        SELECT m.ym, m.d AS as_of, 'JD-物流-千葉' AS warehouse,
               s.item_internal_id, s.qty_on_hand, NULL::TEXT AS bin_number
        FROM nst.inventory_snapshot s JOIN inv_me m ON m.d = s.snapshot_date
        WHERE s.warehouse = 'JD-物流-千葉'
        UNION ALL
        SELECT m.ym, m.d, '弁天倉庫', b.item_internal_id, b.qty_on_hand, b.bin_number
        FROM nst.inventory_bin_snapshot b JOIN bin_me m ON m.d = b.snapshot_date
    )
    SELECT
        inv_all.ym, max(inv_all.as_of) AS as_of, inv_all.warehouse,
        CASE
            WHEN inv_all.warehouse = 'JD-物流-千葉' THEN '输出'
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

    def _trend_chart(d: pd.DataFrame, color_col: str, legend_title: str,
                     scheme: str, order: list[str] | None = None) -> alt.Chart:
        enc_color = alt.Color(f"{color_col}:N", title=legend_title,
                              scale=alt.Scale(scheme=scheme),
                              **({"sort": order} if order else {}))
        return (
            alt.Chart(d)
            .mark_line(point=True, strokeWidth=2)
            .encode(
                x=alt.X("ym:O", title=t("月")),
                y=alt.Y("amt:Q", title=t("库存金额(¥)"), axis=alt.Axis(format="~s")),
                color=enc_color,
                tooltip=[alt.Tooltip("ym:O", title=t("月")),
                         alt.Tooltip(f"{color_col}:N", title=legend_title),
                         alt.Tooltip("amt:Q", title=t("金额(¥)"), format=",.0f"),
                         alt.Tooltip("qty:Q", title=t("数量"), format=",.0f")],
            )
            .properties(height=300)
        )

    try:
        tdf = _df(_TREND_SQL)
    except Exception as e:                                    # SQLite 空态 / schema 未部署
        tdf = pd.DataFrame()
        st.caption(t("📈 月次推移暂不可用") + f"（{e}）")

    if not tdf.empty:
        tdf["qty"] = pd.to_numeric(tdf["qty"], errors="coerce").fillna(0)
        tdf["amt"] = pd.to_numeric(tdf["amt"], errors="coerce").fillna(0)

        # 用途 推移（全仓库合算 · 上の構成 KPI カードと同じ口径）
        t_cat = tdf.groupby(["ym", "category"], as_index=False).agg(
            qty=("qty", "sum"), amt=("amt", "sum"))
        t_cat["label"] = t_cat["category"].map(lambda c: _labels.get(c, c))
        st.altair_chart(
            _trend_chart(t_cat, "label", t("用途"), "tableau10",
                         [_labels[c] for c in CATEGORIES]).properties(title=t("用途别 推移")),
            use_container_width=True)

        # 等级 推移（上の「仓库范围」ラジオに追従）
        t_rank_src = tdf.copy() if sel_wh_rank == t("全部") else tdf[tdf["warehouse"] == sel_wh_rank].copy()
        t_rank_src["rank_label"] = _rank_label(t_rank_src)
        t_rank = t_rank_src.groupby(["ym", "rank_label"], as_index=False).agg(
            qty=("qty", "sum"), amt=("amt", "sum"))
        st.altair_chart(
            _trend_chart(t_rank, "rank_label", t("等级"), "set2",
                         _RANK_ORDER).properties(title=t("等级别 推移")),
            use_container_width=True)

        _as_of = tdf.groupby("ym")["as_of"].max().sort_index()
        st.caption(t("各月＝该月最后一个快照日时点（最新 {ym}={d}）· 金额按当前定义原价换算").format(
            ym=str(_as_of.index[-1]), d=str(_as_of.iloc[-1])))

    st.divider()

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
    # tab2 按 SKU 查棚分布（JD + 弁天）
    # ============================================================
    tab1, tab2, tab4, tab5 = st.tabs([
        t("🔎 按棚番号查商品（弁天）"),
        t("🔍 按 SKU 查库存分布（全仓库）"),
        t("📊 NST vs JDL 账实对账"),
        t("🚚 京东库存查询"),
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
    # tab4 NST vs JDL 账实对账（JD-物流-千葉 NST 账面 vs JDL 海外仓物理）
    #   数据源: jdl.v_inventory_reconciliation（view 算 raw diff）
    #   状态分类: Python 端用 shared.jdl_recon_settings 的阈值动态算
    #   手动同步: UPDATE jdl.pull_schedule SET run_now=TRUE → jdl_scheduler 检测后跑 daily_pull
    # ============================================================
    with tab4:
        from shared.jdl_recon_settings import load_recon_params, classify_status

        _recon_p = load_recon_params()

        # 一行 caption 合并：来源说明 + 最新快照日期
        try:
            meta_df = _df(
                "SELECT MAX(snapshot_date) AS d FROM jdl.inventory_snapshot"
            )
        except Exception as e:
            st.error(t("⚠️ jdl schema 未部署？跑一次 sql/000~020 migration") + f"\n\n{e}")
            return

        last_jdl_date = meta_df["d"].iloc[0] if not meta_df.empty else None
        _jdl_d = str(last_jdl_date) if last_jdl_date else t("（无）")

        # ── 拉对账数据 ──────────────────────────────────────────
        try:
            rec = _df(
                "SELECT jan, item_code, display_name, maker, item_rank, "
                "       nst_qty_on_hand, jdl_qty_in_stock, diff_main, "
                "       jdl_qty_available, jdl_qty_unavailable, "
                "       jdl_qty_preoccupied, jdl_qty_operator_lock, jdl_qty_inventory_lock, "
                "       jdl_qty_inbound, nst_qty_on_order, "
                "       nst_snapshot_date, jdl_snapshot_date "
                "FROM jdl.v_inventory_reconciliation"
            )
        except Exception as e:
            st.error(t("⚠️ 对账 view 读取失败") + f"\n\n{e}")
            return

        if rec.empty:
            st.info(t("尚无对账数据。点上方「手动同步」拉一次 JDL 库存后再来看。"))
            return

        # ── Python 端用配置阈值动态算 status（先算后过滤） ──
        rec["status"] = rec.apply(
            lambda r: classify_status(
                r["nst_qty_on_hand"] if pd.notna(r["nst_qty_on_hand"]) else None,
                r["jdl_qty_in_stock"] if pd.notna(r["jdl_qty_in_stock"]) else None,
                _recon_p,
            ),
            axis=1,
        )

        # ── 用 NST item_master 的 JAN 白名单过滤非输出业务 ─────
        _filter_msg = ""
        try:
            nst_jans_df = _df(
                "SELECT DISTINCT jan FROM nst.item_master_raw "
                "WHERE jan IS NOT NULL AND jan <> ''"
            )
            nst_jan_set = set(nst_jans_df["jan"].astype(str))
            before = len(rec)
            keep_mask = (rec["status"] != "JDL_ONLY") | (rec["jan"].astype(str).isin(nst_jan_set))
            rec = rec[keep_mask].reset_index(drop=True)
            if before != len(rec):
                _filter_msg = t(" · 已过滤非输出 {n}→{m}").format(n=before, m=len(rec))
        except Exception as e:
            st.warning(t("⚠️ 输出部门白名单过滤失败（继续显示全量）: ") + str(e))

        # 顶部单行汇总 caption（合并：数据源说明 + 最新快照 + 过滤数）
        st.caption(t(
            "NST=NetSuite 账面 vs JDL=京东物流实物 · JDL 最新快照: {d}{f}"
        ).format(d=_jdl_d, f=_filter_msg))

        # ── 排除两端实际为 0 的 ONLY 项（默认勾上）──────────────
        exclude_zero_only = st.checkbox(
            t("排除 NST_ONLY / JDL_ONLY 中实际数量为 0 的项"),
            value=True, key="rec_exclude_zero_only",
            help=t("NST 账面 0 但残留记录 / JDL 实物 0 的 ONLY 行通常是历史垃圾，无需对账"),
        )
        if exclude_zero_only:
            zero_nst_only = (rec["status"] == "NST_ONLY") & (rec["nst_qty_on_hand"].fillna(0) == 0)
            zero_jdl_only = (rec["status"] == "JDL_ONLY") & (rec["jdl_qty_in_stock"].fillna(0) == 0)
            rec = rec[~(zero_nst_only | zero_jdl_only)].copy()

        # ── 状态 code → 双语 label（i18n 走 t()）──────────────
        _abs = int(_recon_p["minor_abs_threshold"])
        _pct = float(_recon_p["minor_pct_threshold"])

        def _status_label(code: str) -> str:
            """status code → 双语 label。MINOR 带阈值参数。"""
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
            if code == "JDL_ONLY":
                return t("🟣 仅 JDL 有")
            return code

        # ── 5 档状态分布卡 ─────────────────────────────────────
        counts = rec["status"].value_counts().to_dict()
        s1, s2, s3, s4, s5 = st.columns(5)
        s1.metric(_status_label("OK"), counts.get("OK", 0))
        s2.metric(_status_label("MINOR_DIFF"), counts.get("MINOR_DIFF", 0))
        s3.metric(_status_label("MAJOR_DIFF"), counts.get("MAJOR_DIFF", 0),
                  delta=t("需调查") if counts.get("MAJOR_DIFF", 0) > 0 else None,
                  delta_color="inverse")
        s4.metric(_status_label("NST_ONLY"), counts.get("NST_ONLY", 0),
                  help=t("NST 账面有 · JDL 实物无（已停售或未报 JDL）"))
        s5.metric(_status_label("JDL_ONLY"), counts.get("JDL_ONLY", 0),
                  help=t("JDL 有 · NST 账面无（未登录商品 / item_master 缺失）"))

        # ── 筛选 + 表格（multiselect 内部 value 仍是英文 code）─
        f1, f2, f3 = st.columns([2, 2, 3])
        sel_status = f1.multiselect(
            t("状态筛选"),
            ["OK", "MINOR_DIFF", "MAJOR_DIFF", "NST_ONLY", "JDL_ONLY"],
            default=["MAJOR_DIFF", "NST_ONLY", "JDL_ONLY"],
            format_func=_status_label,
            key="rec_status",
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

        # 按差异绝对值倒序
        df_rec = df_rec.assign(_abs_diff=df_rec["diff_main"].abs().fillna(99999))
        df_rec = df_rec.sort_values("_abs_diff", ascending=False).drop(columns=["_abs_diff"])

        # 表格里 status 列改成双语 label（内部 code 仅用于筛选已经做完）
        df_rec["status"] = df_rec["status"].map(_status_label)

        st.caption(t("命中 {n} 条").format(n=len(df_rec)))

        show_cols = [
            "status", "jan", "item_code", "display_name", "maker", "item_rank",
            "nst_qty_on_hand",
            "jdl_qty_available", "jdl_qty_unavailable", "jdl_qty_in_stock",
            "diff_main",
            "jdl_qty_inbound", "nst_qty_on_order",
            "nst_snapshot_date", "jdl_snapshot_date",
        ]
        st.dataframe(
            df_rec[show_cols],
            hide_index=True,
            use_container_width=True,
            height=560,
            column_config={
                "status": st.column_config.TextColumn(t("状态"), width="small"),
                "jan": st.column_config.TextColumn("JAN", width="small"),
                "item_code": st.column_config.TextColumn(t("商品コード"), width="small"),
                "display_name": st.column_config.TextColumn(t("商品名"), width="large"),
                "maker": st.column_config.TextColumn(t("メーカー"), width="small"),
                "item_rank": st.column_config.TextColumn(t("等级"), width="small"),
                "nst_qty_on_hand": st.column_config.NumberColumn(t("NST 账面"), format="%d"),
                "jdl_qty_available": st.column_config.NumberColumn(t("JDL 可用"), format="%d"),
                "jdl_qty_unavailable": st.column_config.NumberColumn(t("JDL 不可用"), format="%d",
                                                                     help=t("预占 + 操作锁 + 库存锁")),
                "jdl_qty_in_stock": st.column_config.NumberColumn(t("JDL 总库存"), format="%d",
                                                                  help=t("可用 + 不可用（不含在途）")),
                "diff_main": st.column_config.NumberColumn(t("差异 (NST−JDL总库存)"), format="%+d"),
                "jdl_qty_inbound": st.column_config.NumberColumn(t("JDL 在途"), format="%d"),
                "nst_qty_on_order": st.column_config.NumberColumn(t("NST 在途"), format="%d"),
                "nst_snapshot_date": st.column_config.DateColumn(t("NST 快照日")),
                "jdl_snapshot_date": st.column_config.DateColumn(t("JDL 快照日")),
            },
        )

        st.download_button(
            t("📥 下载对账 CSV"),
            df_rec[show_cols].rename(columns={
                "status": t("状态"), "jan": "JAN", "item_code": t("商品コード"),
                "display_name": t("商品名"), "maker": t("メーカー"), "item_rank": t("等级"),
                "nst_qty_on_hand": t("NST 账面"), "jdl_qty_available": t("JDL 可用"),
                "jdl_qty_unavailable": t("JDL 不可用"), "jdl_qty_in_stock": t("JDL 总库存"),
                "diff_main": t("差异 (NST−JDL总库存)"), "jdl_qty_inbound": t("JDL 在途"),
                "nst_qty_on_order": t("NST 在途"), "nst_snapshot_date": t("NST 快照日"),
                "jdl_snapshot_date": t("JDL 快照日"),
            }).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"nst_jdl_reconciliation_{inv_date}.csv",
            mime="text/csv",
            key="rec_csv",
        )

    # ============================================================
    # tab5 京东库存查询（纯 JDL 数据浏览·jdl.inventory_snapshot 最新快照）
    #   字段公式：
    #     在库 = 可用 + 不可用（预占 + 操作锁 + 库存锁）
    #     总库存 = 在库 + 在途
    # ============================================================
    with tab5:
        st.caption(t(
            "JDL 京东物流海外仓物理库存查询。"
            "总库存 = 可用 + 不可用（预占 + 操作锁 + 库存锁），在途独立显示。"
        ))

        try:
            jdl_meta = _df(
                "SELECT max(snapshot_date) AS d, count(*) AS c "
                "FROM jdl.inventory_snapshot "
                "WHERE snapshot_date = (SELECT max(snapshot_date) FROM jdl.inventory_snapshot)"
            )
        except Exception as e:
            st.error(t("⚠️ jdl.inventory_snapshot 读取失败") + f"\n\n{e}")
            return

        if jdl_meta.empty or jdl_meta["d"].iloc[0] is None:
            st.info(t("尚无 JDL 库存数据。请到「📥 数据获取 → 🚚 JDL → ▶️ 调度 + 手动同步」拉一次。"))
            return

        j_date = jdl_meta["d"].iloc[0]
        j_rows = int(jdl_meta["c"].iloc[0])
        cm1, cm2 = st.columns(2)
        cm1.metric(t("最新快照日"), str(j_date))
        cm2.metric(t("库存行数"), f"{j_rows:,}")

        # 筛选：JAN/商品名 + 仓库 + 是否显示 0 库存
        jf1, jf2, jf3 = st.columns([3, 2, 1.5])
        j_kw = jf1.text_input(t("🔍 JAN / 商品名 模糊搜索"), key="jdl_q_kw")
        try:
            wh_df = _df(
                "SELECT DISTINCT warehouse_code, warehouse_name "
                "FROM jdl.inventory_snapshot "
                "WHERE snapshot_date = (SELECT max(snapshot_date) FROM jdl.inventory_snapshot) "
                "ORDER BY warehouse_code"
            )
            wh_opts = [
                (r["warehouse_code"], f"{r['warehouse_code']} ({r['warehouse_name']})")
                for _, r in wh_df.iterrows()
            ]
        except Exception:
            wh_opts = []
        sel_wh = jf2.multiselect(
            t("仓库"),
            [c for c, _ in wh_opts],
            default=[],
            format_func=lambda c: dict(wh_opts).get(c, c),
            key="jdl_q_wh",
        )
        show_zero_jdl = jf3.checkbox(t("含 0 库存"), value=False, key="jdl_q_zero")

        where = ["snapshot_date = (SELECT max(snapshot_date) FROM jdl.inventory_snapshot)"]
        qparams: dict = {}
        if j_kw.strip():
            where.append("(jan LIKE %(kw)s OR goods_name LIKE %(kw)s)")
            qparams["kw"] = f"%{j_kw.strip()}%"
        if sel_wh:
            wh_names = []
            for i, wh in enumerate(sel_wh):
                key = f"wh{i}"
                wh_names.append(f"%({key})s")
                qparams[key] = wh
            where.append("warehouse_code IN (" + ",".join(wh_names) + ")")
        if not show_zero_jdl:
            where.append("(qty_total > 0 OR qty_inbound > 0)")

        try:
            j_df = _df(
                "SELECT jan, jd_goods_id, goods_name, warehouse_code, warehouse_name, "
                "       qty_available, "
                "       (COALESCE(qty_preoccupied,0)+COALESCE(qty_operator_lock,0)"
                "        +COALESCE(qty_inventory_lock,0)) AS qty_unavailable, "
                # 在库 = 可用 + 不可用（JDL.totalQuantity 含在途，不可用）
                "       (COALESCE(qty_available,0)+COALESCE(qty_preoccupied,0)"
                "        +COALESCE(qty_operator_lock,0)+COALESCE(qty_inventory_lock,0))"
                "         AS qty_grand_total, "
                "       qty_inbound, "
                "       qty_preoccupied, qty_operator_lock, qty_inventory_lock, "
                "       stock_level_name "
                "FROM jdl.inventory_snapshot WHERE " + " AND ".join(where) +
                " ORDER BY qty_grand_total DESC LIMIT 5000",
                qparams,
            )
        except Exception as e:
            st.error(str(e))
            return

        # ── 用 NST item_master JAN 白名单过滤非输出业务 ─────
        _j_filter_msg = ""
        try:
            _nst_jans = _df(
                "SELECT DISTINCT jan FROM nst.item_master_raw "
                "WHERE jan IS NOT NULL AND jan <> ''"
            )
            _nst_jan_set = set(_nst_jans["jan"].astype(str))
            _before = len(j_df)
            j_df = j_df[j_df["jan"].astype(str).isin(_nst_jan_set)].reset_index(drop=True)
            if _before != len(j_df):
                _j_filter_msg = t(" · 已过滤非输出 {n}→{m}").format(n=_before, m=len(j_df))
        except Exception as e:
            st.warning(t("⚠️ 输出部门白名单过滤失败（继续显示全量）: ") + str(e))

        st.caption(t("命中 {n} 条（最大 5000）").format(n=len(j_df)) + _j_filter_msg)

        # Boss 业务公式：总库存 = 可用 + 不可用（不含在途）· 在途独立列
        j_show_cols = [
            "jan", "jd_goods_id", "goods_name", "warehouse_code", "warehouse_name",
            "qty_available", "qty_unavailable", "qty_grand_total",
            "qty_inbound",
            "qty_preoccupied", "qty_operator_lock", "qty_inventory_lock",
            "stock_level_name",
        ]
        st.dataframe(
            j_df[j_show_cols],
            hide_index=True,
            use_container_width=True,
            height=560,
            column_config={
                "jan": st.column_config.TextColumn("JAN", width="small"),
                "jd_goods_id": st.column_config.TextColumn(t("JD商品ID"), width="small"),
                "goods_name": st.column_config.TextColumn(t("商品名"), width="large"),
                "warehouse_code": st.column_config.TextColumn(t("仓库代码"), width="small"),
                "warehouse_name": st.column_config.TextColumn(t("仓库名"), width="small"),
                "qty_available": st.column_config.NumberColumn(t("可用"), format="%d"),
                "qty_unavailable": st.column_config.NumberColumn(t("不可用"), format="%d",
                                                                 help=t("预占 + 操作锁 + 库存锁")),
                "qty_grand_total": st.column_config.NumberColumn(t("总库存"), format="%d",
                                                                 help=t("可用 + 不可用（不含在途）")),
                "qty_inbound": st.column_config.NumberColumn(t("在途"), format="%d"),
                "qty_preoccupied": st.column_config.NumberColumn(t("预占"), format="%d"),
                "qty_operator_lock": st.column_config.NumberColumn(t("操作锁"), format="%d"),
                "qty_inventory_lock": st.column_config.NumberColumn(t("库存锁"), format="%d"),
                "stock_level_name": st.column_config.TextColumn(t("等级"), width="small"),
            },
        )

        st.download_button(
            t("📥 下载 JDL 库存 CSV"),
            j_df[j_show_cols].rename(columns={
                "jan": "JAN", "jd_goods_id": t("JD商品ID"), "goods_name": t("商品名"),
                "warehouse_code": t("仓库代码"), "warehouse_name": t("仓库名"),
                "qty_available": t("可用"), "qty_unavailable": t("不可用"),
                "qty_grand_total": t("总库存"), "qty_inbound": t("在途"),
                "qty_preoccupied": t("预占"), "qty_operator_lock": t("操作锁"),
                "qty_inventory_lock": t("库存锁"), "stock_level_name": t("等级"),
            }).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"jdl_inventory_{j_date}.csv",
            mime="text/csv",
            key="jdl_q_csv",
        )


tab_comp, tab_risk = st.tabs([t("📦 库存构成分析"), t("🛡️ 库存风控")])
with tab_comp:
    _render_composition()
with tab_risk:
    render_risk_control(conn)
