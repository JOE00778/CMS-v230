"""模块 #33 库存构成分析 · JD + 弁天 全仓库按用途分类.

数据源:
  - JD-物流-千葉: nst.inventory_snapshot（不分 bin·全部归「通常输出」）
  - 弁天倉庫:     nst.inventory_bin_snapshot（按 bin 分类）

弁天 bin 用途分类（shared/bin_categories.py·Boss 2026-05-27）:
  返品 (HENPIN-EX) > 不良品 (FF-3) > 输出中国 (1-0105A/1-0106A/1-0107A/yusyutu2F) > 通常输出
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.bin_categories import CATEGORIES
from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("库存构成分析"), page_icon="📦", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("📦 库存构成分析（JD + 弁天）"))
st.caption(t("JD 不分 bin 全部归通常输出 · 弁天按棚番号分类（输出 / 输出中国 / 返品 / 不良品）"))


def _df(sql: str, params=None) -> pd.DataFrame:
    rs = conn.execute(sql, params or {}).fetchall()
    return pd.DataFrame([dict(r) for r in rs])


# 最新 snapshot_date
try:
    inv_date = _df("SELECT max(snapshot_date) AS d FROM nst.inventory_snapshot")["d"].iloc[0]
    bin_date_row = _df("SELECT max(snapshot_date) AS d FROM nst.inventory_bin_snapshot")
    bin_date = bin_date_row["d"].iloc[0] if not bin_date_row.empty else None
except Exception as e:
    st.error(t("⚠️ 库存表读取失败（schema 未部署？）") + f"\n\n{e}")
    st.stop()

if not inv_date:
    st.info(t("尚无库存快照数据。"))
    st.stop()

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
    st.stop()

if df.empty:
    st.info(t("无库存数据"))
    st.stop()

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
df_rank = df.copy()
df_rank["rank_label"] = df_rank["item_rank"].astype(str).where(
    df_rank["item_rank"].notna() & (df_rank["item_rank"].astype(str) != "nan"),
    t("未分类"),
)
by_rank = (df_rank.groupby("rank_label", as_index=False)
           .agg(qty=("qty_on_hand", "sum"), amt=("amt", "sum")))
# 固定顺序: NEW / Aランク / Bランク / Cランク / 取扱中止 / 未分类 / 其他
_RANK_ORDER = ["NEW", "Aランク", "Bランク", "Cランク", "取扱中止", t("未分类")]
by_rank["_ord"] = by_rank["rank_label"].map(
    lambda x: _RANK_ORDER.index(x) if x in _RANK_ORDER else len(_RANK_ORDER))
by_rank = by_rank.sort_values(["_ord", "rank_label"]).drop(columns=["_ord"])
if not by_rank.empty:
    rank_cols = st.columns(min(len(by_rank), 6))
    for col, (_, r) in zip(rank_cols, by_rank.iterrows()):
        pct_r = (float(r["amt"]) / tot_amt * 100) if tot_amt else 0.0
        col.metric(f"{r['rank_label']} (¥)", f"¥{float(r['amt']):,.0f}",
                   delta=f"{pct_r:.0f}% {t('占比')} · {float(r['qty']):,.0f} {t('数量')}",
                   delta_color="off")

# ----- 环状图（用途 + 等级 并排） -----
import altair as alt
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
tab1, tab2, tab3 = st.tabs([
    t("🔎 按棚番号查商品（弁天）"),
    t("🔍 按 SKU 查库存分布（全仓库）"),
    t("🏷️ 货架用途指定"),
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
# tab3 货架用途指定（行=货架号·列=输出中国/返品/不良品）
#   未勾任何 → 通常输出。优先级: 返品 > 不良品 > 输出中国 > 通常输出。
# ============================================================
with tab3:
    st.caption(t(
        "行=弁天棚番号·列=输出中国/返品/不良品。勾选保存后影响所有用途分类视图。"
        "未勾任何 → 通常输出。优先级: 返品 > 不良品 > 输出中国 > 通常输出。"
    ))

    try:
        cand_bins = _df(
            "SELECT DISTINCT ibs.bin_number, "
            "       COALESCE(bc.is_cb, FALSE) AS is_cb, "
            "       COALESCE(bc.is_return, FALSE) AS is_return, "
            "       COALESCE(bc.is_defect, FALSE) AS is_defect, "
            "       bc.note AS note "
            "FROM nst.inventory_bin_snapshot ibs "
            "LEFT JOIN nst.bin_category bc ON bc.bin_number = ibs.bin_number "
            "WHERE ibs.snapshot_date = %(d)s AND ibs.bin_number IS NOT NULL "
            "ORDER BY ibs.bin_number",
            {"d": bin_date or inv_date},
        )
    except Exception as e:
        st.error(t("⚠️ 候选棚号读取失败") + f"\n\n{e}")
        cand_bins = pd.DataFrame()

    if cand_bins.empty:
        st.info(t("暂无候选棚号（弁天 bin 快照为空？）"))
    else:
        cand_bins["is_cb"] = cand_bins["is_cb"].astype(bool)
        cand_bins["is_return"] = cand_bins["is_return"].astype(bool)
        cand_bins["is_defect"] = cand_bins["is_defect"].astype(bool)
        n_total = len(cand_bins)
        n_cb = int(cand_bins["is_cb"].sum())
        n_ret = int(cand_bins["is_return"].sum())
        n_def = int(cand_bins["is_defect"].sum())
        st.write(t("候选 {n} 棚 · 已指定 输出中国 {c} · 返品 {r} · 不良品 {d}")
                 .format(n=n_total, c=n_cb, r=n_ret, d=n_def))

        edited = st.data_editor(
            cand_bins, hide_index=True, use_container_width=True, height=560,
            column_config={
                "bin_number": st.column_config.TextColumn(t("棚番号"), disabled=True),
                "is_cb": st.column_config.CheckboxColumn(t("输出中国"), default=False),
                "is_return": st.column_config.CheckboxColumn(t("返品"), default=False),
                "is_defect": st.column_config.CheckboxColumn(t("不良品"), default=False),
                "note": st.column_config.TextColumn(t("备注"), required=False),
            },
            key="bin_cat_editor",
        )

        if st.button(t("💾 保存货架用途"), type="primary"):
            kept = 0
            removed = 0
            for _, r in edited.iterrows():
                bn = str(r["bin_number"])
                any_flag = bool(r["is_cb"]) or bool(r["is_return"]) or bool(r["is_defect"])
                note_v = str(r["note"]).strip() if pd.notna(r["note"]) else None
                if any_flag or note_v:
                    conn.execute(
                        "INSERT INTO nst.bin_category "
                        "(bin_number, is_cb, is_return, is_defect, note, updated_at) "
                        "VALUES (%(b)s, %(c)s, %(r)s, %(d)s, %(n)s, NOW()) "
                        "ON CONFLICT (bin_number) DO UPDATE SET "
                        "is_cb=EXCLUDED.is_cb, is_return=EXCLUDED.is_return, "
                        "is_defect=EXCLUDED.is_defect, note=EXCLUDED.note, "
                        "updated_at=NOW()",
                        {"b": bn, "c": bool(r["is_cb"]), "r": bool(r["is_return"]),
                         "d": bool(r["is_defect"]), "n": note_v},
                    )
                    kept += 1
                else:
                    res = conn.execute(
                        "DELETE FROM nst.bin_category WHERE bin_number = %(b)s",
                        {"b": bn},
                    )
                    if getattr(res, "rowcount", 0):
                        removed += 1
            conn.commit()
            st.success(t("✅ 已保存 · 有用途指定 {k} 棚 · 清除 {r} 棚").format(k=kept, r=removed))
            st.rerun()

    with st.expander(t("➕ 手动加棚号（候选列表里没有的，比如新建棚）")):
        c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
        new_bin = c1.text_input(t("棚番号"), key="new_bin")
        new_cb = c2.checkbox(t("输出中国"), value=False, key="new_cb")
        new_ret = c3.checkbox(t("返品"), value=False, key="new_ret")
        new_def = c4.checkbox(t("不良品"), value=False, key="new_def")
        if st.button(t("加入指定"), key="add_bin_cat"):
            bn = new_bin.strip()
            if bn:
                conn.execute(
                    "INSERT INTO nst.bin_category "
                    "(bin_number, is_cb, is_return, is_defect, updated_at) "
                    "VALUES (%(b)s, %(c)s, %(r)s, %(d)s, NOW()) "
                    "ON CONFLICT (bin_number) DO UPDATE SET "
                    "is_cb=EXCLUDED.is_cb, is_return=EXCLUDED.is_return, "
                    "is_defect=EXCLUDED.is_defect, updated_at=NOW()",
                    {"b": bn, "c": bool(new_cb), "r": bool(new_ret), "d": bool(new_def)},
                )
                conn.commit()
                st.success(t("✅ 已加入：{b}").format(b=bn))
                st.rerun()
            else:
                st.warning(t("请填棚番号"))
