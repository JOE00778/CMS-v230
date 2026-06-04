"""模块 #7 商品情报检索 — 薄壳 UI（T-008 + 重量对比恢复）。

逻辑全在 modules/ 下，本文件仅做 UI 调用：
- Tab1 商品检索        → modules/product_search（filters/queries/export）
- Tab2 JDL vs NST 重量对比 → modules/weight_compare（queries：query + diff + 覆盖率）
"""
from __future__ import annotations

import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t, get_lang
from modules.product_search import (
    STOCK_ALL, STOCK_IN, STOCK_OUT, SearchFilters,
    distinct_values, search_items, to_csv_bytes,
)
from modules.weight_compare import (
    compute_compare, coverage_stats, load_compare,
)

st.set_page_config(page_title=t("商品情报检索"), page_icon="🔍", layout="wide")
from shared.auth import require_password  # noqa: E402
from shared.theme import inject_theme  # noqa: E402
require_password(); inject_theme(); lang_selector()
conn = get_connection()

_JA = get_lang() == "ja"


def _L(zh: str, ja: str) -> str:
    return ja if _JA else zh


st.title(t("🔍 商品情报检索"))

_tab_search, _tab_compare = st.tabs([
    _L("🔍 商品检索", "🔍 商品検索"),
    _L("⚖️ JDL vs NST 重量对比", "⚖️ JDL vs NST 重量比較"),
])

# ============================================================
# Tab 1: 商品检索（薄壳 → modules/product_search）
# ============================================================
with _tab_search:
    st.caption(t("按多维度筛选 SKU，看完整商品信息"))

    kw = st.text_input(t("全文搜索"), placeholder=t("商品名 / 厂商"))
    c1, c2, c3 = st.columns(3)
    brands = c1.multiselect(t("品牌"), distinct_values(conn, "maker"))
    cats = c2.multiselect(t("商品等级"), distinct_values(conn, "item_rank"))
    stock = c3.selectbox(t("库存状态"), [STOCK_ALL, STOCK_IN, STOCK_OUT],
                         format_func=lambda s: {STOCK_ALL: t("全部"), STOCK_IN: t("有库存"),
                                                STOCK_OUT: t("无库存")}[s])
    p1, p2, d1, d2 = st.columns(4)
    pmin = p1.number_input(t("最低原价"), min_value=0.0, value=0.0) or None
    pmax = p2.number_input(t("最高原价"), min_value=0.0, value=0.0) or None
    cfrom = (d1.text_input(t("更新日 从"), placeholder="2026-01-01") or "").strip() or None
    cto = (d2.text_input(t("更新日 到"), placeholder="2026-12-31") or "").strip() or None
    try:
        f = SearchFilters(keyword=kw, brands=brands, categories=cats, price_min=pmin,
                          price_max=pmax, stock_status=stock, created_from=cfrom,
                          created_to=cto).validate()
        df = search_items(conn, f)
    except ValueError as e:
        st.error(t("筛选条件非法") + f"：{e}")
        st.stop()

    st.markdown(f"**{len(df):,}** {t('件')}")
    st.dataframe(df, use_container_width=True, hide_index=True, height=560)
    st.download_button(t("📥 导出 CSV"), to_csv_bytes(df),
                       file_name=f"product_search_{len(df)}.csv", mime="text/csv")


# ============================================================
# Tab 2: ⚖️ JDL vs NST 重量对比（薄壳 → modules/weight_compare）
# ============================================================
with _tab_compare:
    st.caption(_L(
        "JDL 仓库实测毛重（wms_gross_weight）vs NST 商品主档「包装重量」（custitem_fb_package_weight）· 同口径含包装总重",
        "JDL 倉庫実測総重量 vs NST 商品マスタ「パッケージ重量」 · 包装込み総重量で同口径比較",
    ))

    try:
        cmp_df = compute_compare(load_compare(conn))
    except Exception as e:  # noqa: BLE001
        try:
            conn.rollback()
        except Exception:
            pass
        st.error(_L("数据读取失败：", "データ読み取り失敗：") + str(e))
        st.stop()

    if cmp_df.empty:
        st.info(_L("暂无数据", "データなし"))
    else:
        s = coverage_stats(cmp_df)
        n_total = s["n_total"]
        n_cmp = s["n_cmp"]
        comparable = s["comparable"]

        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric(_L("活跃 SKU", "アクティブ SKU"), f"{n_total:,}")
        k2.metric(_L("NST 有重量", "NST 重量あり"), f"{s['n_nst']:,}",
                  f"{s['n_nst']/n_total*100:.0f}%" if n_total else "")
        k3.metric(_L("JDL 有重量", "JDL 重量あり"), f"{s['n_jdl']:,}",
                  f"{s['n_jdl']/n_total*100:.0f}%" if n_total else "")
        k4.metric(_L("可对比", "比較可能"), f"{n_cmp:,}",
                  f"{n_cmp/n_total*100:.0f}%" if n_total else "")
        k5.metric(
            _L("差 ≤ 10%", "差 ≤ 10%"), f"{s['n_close']:,}",
            f"{s['n_close']/n_cmp*100:.0f}%" if n_cmp else "",
            help=_L("一致性较好的占比", "一致性良好の割合"),
        )

        if n_cmp > 0:
            st.divider()
            st.markdown(f"**{_L('📋 明细（按差异降序）', '📋 明細（差異降順）')}**")
            show_cols = ["jan", "display_name", "maker", "item_rank",
                         "nst_package_g", "jdl_wms_g", "diff_g", "diff_pct"]
            st.dataframe(
                comparable[show_cols],
                use_container_width=True, height=800, hide_index=True,
                column_config={
                    "jan":           _L("JAN", "JAN"),
                    "display_name":  _L("商品名", "商品名"),
                    "maker":         _L("厂商", "メーカー名"),
                    "item_rank":     _L("等级", "ランク"),
                    "nst_package_g": st.column_config.NumberColumn(
                        _L("NST 包装重量(g)", "NST パッケージ重量(g)"), format="%.0f"),
                    "jdl_wms_g":     st.column_config.NumberColumn(
                        _L("JDL 实测(g)", "JDL 実測(g)"), format="%.0f"),
                    "diff_g":        st.column_config.NumberColumn(
                        _L("差(g) JDL-NST", "差(g) JDL-NST"), format="%+.0f"),
                    "diff_pct":      st.column_config.NumberColumn(
                        _L("差异 %", "差異 %"), format="%+.1f%%"),
                },
            )

            csv2 = comparable[show_cols].to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                _L("📥 对比明细 CSV", "📥 比較明細 CSV"), data=csv2,
                file_name=f"jdl_nst_weight_compare_{n_cmp}.csv", mime="text/csv",
            )
        else:
            st.info(_L(
                "暂无可对比记录 · 需要 NST package_weight + JDL wms_gross_weight 同时存在。"
                "请先到「数据获取 → NST」执行 items 拉取（含重量字段）。",
                "比較可能なレコードなし · NST と JDL 両方の重量データが必要。"
                "「データ取得 → NST」で items 同期を実行してください。",
            ))
