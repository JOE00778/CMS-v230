"""模块 #7 商品情报检索 · NST API データ（nst.item_master_raw）ベース.

2026-05-21 全面改写：旧 inventory_snapshot / sales_line / inventory_turnover
（手動 Excel 時代の死表）→ NST API 直取得データへ。

データ源:
- nst.item_master_raw    商品マスタ（全輸出商品 · 表示名/メーカー/ランク/取扱区分/
                          原価各種/カートン入数/発注ロット）
- nst.inventory_snapshot 在庫（手持/利用可能/注文済）最新スナップショット
- nst.sales_monthly      売上（全期間 累計 販売数量/総収益 を SKU 単位で集計）

統一ビュー: SKU ごとに 基礎情報 + 在庫 + 原価 + 累計売上 を横断検索・CSV 出力。
"""
from __future__ import annotations

import html
import re

import pandas as pd
import streamlit as st

from shared.db import get_readonly_connection
from shared.i18n import lang_selector, t, get_lang
from modules.weight_compare import compute_compare, coverage_stats, load_compare

st.set_page_config(page_title=t("商品情报检索"), page_icon="🔍", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_readonly_connection()

_JA = get_lang() == "ja"


def _L(zh: str, ja: str) -> str:
    return ja if _JA else zh


st.title(t("🔍 商品情报检索"))
st.caption(_L(
    "NST 商品主档 + 库存快照 + 累计销售 整合检索 · 多维筛选 · CSV 导出（仅輸出事业商品）",
    "NST 商品マスタ + 在庫スナップショット + 売上累計 を統合検索 · 多軸フィルタ · "
    "CSV 出力（輸出事業の商品のみ）",
))

# 列見出し: (中文, 日本語) — UI 言語追従（page 04/05 と統一）
_LBL = {
    "internal_id":        ("内部ID", "内部ID"),
    "item_code":          ("商品编码", "アイテム"),
    "jan":                ("UPC编码", "UPCコード"),
    "display_name":       ("显示名", "表示名"),
    "maker":              ("厂商", "メーカー名"),
    "item_rank":          ("商品等级", "商品ランク"),
    "handling_cd":        ("经销状态", "取扱区分"),
    "date_created":       ("建立日期", "作成日"),
    "qty_on_hand":        ("现有库存", "手持"),
    "qty_available":      ("可用库存", "利用可能"),
    "qty_on_order":       ("在订数量", "注文済"),
    "stock_amount":       ("库存金额", "在庫金額"),
    "average_cost":       ("平均原价", "平均原価"),
    "cost_estimate":      ("定义原价", "定義原価"),
    "last_purchase_cost": ("前次购入价", "前回購入価格"),
    "wms_gross_weight_g": ("毛重(g)", "総重量(g)"),
    "carton_qty":         ("箱入数", "カートン入数"),
    "order_lot":          ("发注批量", "発注ロット"),
    "qty_sold":           ("累计销量", "累計販売数"),
    "revenue":            ("累计销售额", "累計売上"),
}


def _cc(*keys) -> dict:
    return {k: (_LBL[k][1] if _JA else _LBL[k][0]) for k in keys}


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


# ============================================================
# データ有無チェック
# ============================================================
chk, err = _query("SELECT COUNT(*) AS c FROM nst.item_master_raw")
if err:
    st.error(_L("商品マスタ未取得 or 接続エラー: ", "商品マスタ未取得 or 接続エラー: ") + err)
    st.info(_L("请到 page 27「📥 NST 取得数据」执行 items 任务。",
               "page 27「📥 NST 取得データ」で items ジョブを実行してください。"))
    st.stop()
if chk is None or int(chk.iloc[0]["c"]) == 0:
    st.warning(_L("⚠️ 商品主档为空。请到 page 27「📥 NST 取得数据」执行 items 任务。",
                  "⚠️ 商品マスタが空です。page 27「📥 NST 取得データ」で items ジョブを実行してください。"))
    st.stop()


# ============================================================
# SKU-level 統合ビュー（cache 5 分）
# ============================================================
@st.cache_data(ttl=300)
def load_sku_view() -> pd.DataFrame:
    sql = """
        WITH inv AS (
            -- 主键含 warehouse（JD-千葉/弁天多仓多行）→ 按 item 聚合，否则详情/表格重复行
            SELECT item_internal_id,
                   SUM(qty_on_hand) AS qty_on_hand,
                   SUM(qty_available) AS qty_available,
                   SUM(qty_on_order) AS qty_on_order
            FROM nst.inventory_snapshot
            WHERE snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot)
            GROUP BY item_internal_id
        ),
        sales AS (
            SELECT item_internal_id,
                   SUM(qty_sold) AS qty_sold,
                   SUM(revenue)  AS revenue
            FROM nst.sales_monthly
            GROUP BY item_internal_id
        )
        SELECT
            im.internal_id, im.item_code, im.jan, im.display_name,
            im.maker, im.item_rank, im.handling_cd, im.is_inactive,
            im.average_cost, im.cost_estimate, im.last_purchase_cost,
            im.carton_qty, im.order_lot, im.date_created,
            inv.qty_on_hand, inv.qty_available, inv.qty_on_order,
            (COALESCE(im.average_cost, im.cost_estimate, 0)
                * COALESCE(inv.qty_on_hand, 0)) AS stock_amount,
            sales.qty_sold, sales.revenue,
            gd.wms_gross_weight_g
        FROM nst.item_master_raw im
        LEFT JOIN inv   ON inv.item_internal_id   = im.internal_id
        LEFT JOIN sales ON sales.item_internal_id = im.internal_id
        LEFT JOIN jdl.v_goods_dimensions gd ON gd.jan = im.jan
    """
    rows = conn.execute(sql).fetchall()
    cols = ["internal_id", "item_code", "jan", "display_name", "maker", "item_rank",
            "handling_cd", "is_inactive", "average_cost", "cost_estimate",
            "last_purchase_cost", "carton_qty", "order_lot", "date_created", "qty_on_hand",
            "qty_available", "qty_on_order", "stock_amount", "qty_sold", "revenue",
            "wms_gross_weight_g"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


df = load_sku_view()
total_skus = len(df)

# ============================================================
# Tabs 切换 · tab1 商品检索（原内容）· tab2 JDL vs NST 重量对比
# ============================================================
_tab_search, _tab_compare = st.tabs([
    _L("🔍 商品检索", "🔍 商品検索"),
    _L("⚖️ JDL vs NST 重量对比", "⚖️ JDL vs NST 重量比較"),
])

# ============================================================
# Tab 1: 商品检索（原内容）
# ============================================================
with _tab_search:
    # ============================================================
    # 筛选 UI
    # ============================================================
    _ALL = _L("全部", "全部")

    c1, c2 = st.columns(2)
    keyword_code = c1.text_input(_L("商品编码 / JAN", "アイテム / JAN"), placeholder="例: 4515061012818")
    keyword_name = c2.text_input(_L("商品名（部分匹配）", "商品名（部分一致）"), placeholder="例: パーフェクトジェル")

    with st.expander(_L("📋 批量 JAN（换行 / 逗号分隔）", "📋 複数 JAN（改行・カンマ区切り）")):
        multi_jan = st.text_area(
            "multi_jan", placeholder="4901234567890\n4987654321098",
            height=100, label_visibility="collapsed",
        )


    def _opts(col: str) -> list:
        return sorted({str(v).strip() for v in df[col].dropna().tolist() if str(v).strip()})


    f1, f2, f3, f4 = st.columns(4)
    handle_pick = f1.multiselect(_L("经销状态", "取扱区分"), _opts("handling_cd"), placeholder=_ALL)
    rank_pick = f2.multiselect(_L("商品等级", "商品ランク"), _opts("item_rank"), placeholder=_ALL)
    maker_pick = f3.multiselect(_L("厂商", "メーカー名"), _opts("maker"), placeholder=_ALL)
    with f4:
        st.write("")
        in_stock = st.checkbox(_L("仅有库存（>0）", "在庫あり（>0）"), value=False)
        hide_inactive = st.checkbox(_L("隐藏停用品", "休止品を隠す"), value=True)

    # ============================================================
    # Apply filters
    # ============================================================
    v = df.copy()
    jan_list = [j.strip() for j in re.split(r"[,\n\r、，]+", multi_jan) if j.strip()]
    if jan_list:
        v = v[v["jan"].astype(str).isin(jan_list) | v["item_code"].astype(str).isin(jan_list)]
    elif keyword_code.strip():
        kw = keyword_code.strip()
        v = v[
            v["item_code"].astype(str).str.contains(kw, case=False, na=False)
            | v["jan"].astype(str).str.contains(kw, case=False, na=False)
            | v["internal_id"].astype(str).str.contains(kw, case=False, na=False)
        ]
    if keyword_name.strip():
        v = v[v["display_name"].astype(str).str.contains(keyword_name.strip(), case=False, na=False)]
    if handle_pick:
        v = v[v["handling_cd"].isin(handle_pick)]
    if rank_pick:
        v = v[v["item_rank"].isin(rank_pick)]
    if maker_pick:
        v = v[v["maker"].astype(str).isin(maker_pick)]
    if in_stock:
        v = v[v["qty_on_hand"].fillna(0) > 0]
    if hide_inactive:
        v = v[v["is_inactive"] != True]  # noqa: E712

    # ============================================================
    # 顶部统计 + 表格
    # ============================================================
    hl, hr = st.columns([1, 0.25])
    hl.subheader(_L("商品一览", "商品一覧"))
    hr.markdown(
        f"<h4 style='text-align:right;margin-top:.6em;'>{len(v):,} / {total_skus:,} 件</h4>",
        unsafe_allow_html=True,
    )

    if v.empty:
        st.info(_L("当前条件下没有商品。调整筛选再试。", "この条件の商品がありません。"))
    else:
        # 数値整形
        for col in ("revenue", "stock_amount", "average_cost", "cost_estimate",
                    "last_purchase_cost"):
            v[col] = pd.to_numeric(v[col], errors="coerce")

        sort_options = {
            _L("累计销量降序", "累計販売数 降順"): ("qty_sold", False),
            _L("库存降序", "在庫 降順"): ("qty_on_hand", False),
            _L("库存金额降序", "在庫金額 降順"): ("stock_amount", False),
            _L("累计销售额降序", "累計売上 降順"): ("revenue", False),
            _L("商品编码", "アイテム"): ("item_code", True),
        }
        sort_pick = st.selectbox(_L("排序", "並び替え"), list(sort_options.keys()))
        sc, sa = sort_options[sort_pick]
        v = v.sort_values(sc, ascending=sa, na_position="last")

        cols = ("internal_id", "item_code", "jan", "display_name", "maker", "item_rank",
                "handling_cd", "date_created", "qty_on_hand", "qty_available", "qty_on_order",
                "stock_amount", "cost_estimate", "average_cost", "last_purchase_cost",
                "wms_gross_weight_g", "carton_qty", "order_lot", "qty_sold", "revenue")
        # 行数に応じて高さ自動調整（単品/少数件で空行を出さない · 多件は 560 で頭打ち→スクロール）
        _tbl_h = min(38 + 35 * len(v), 560)
        st.dataframe(
            v[list(cols)], use_container_width=True, height=_tbl_h, hide_index=True,
            column_config=_cc(*cols),
        )

        csv = v[list(cols)].rename(columns=_cc(*cols)).to_csv(index=False).encode("utf-8-sig")
        st.download_button(_L("📥 当前视图 CSV", "📥 現在のビュー CSV"), data=csv,
                           file_name=f"item_search_{len(v)}.csv", mime="text/csv")

        # ============================================================
        # 単 SKU 詳細
        # ============================================================
        st.divider()
        st.subheader(_L("🔎 SKU 详情", "🔎 SKU 詳細"))

        choices = v.apply(lambda r: f"{r['item_code']} · {r['display_name'] or '(无名)'}", axis=1).tolist()
        pick = st.selectbox(_L("选择 SKU", "SKU を選択"), choices)
        row = v.iloc[choices.index(pick)]

        def _lb(k: str) -> str:
            return _LBL[k][1 if _JA else 0]


        def _esc(v) -> str:
            s = "—" if v is None or str(v).strip() == "" else str(v)
            return html.escape(s)


        # 基础信息卡（item_code/handling_cd/jan/item_rank/internal_id/maker/carton_qty/order_lot
        # 统一进一张白卡 · 样式对齐下方 stMetric 卡）
        _fields = [
            (_lb("item_code"), row["item_code"]),
            (_lb("handling_cd"), row["handling_cd"]),
            (_lb("jan"), row["jan"]),
            (_lb("item_rank"), row["item_rank"]),
            (_lb("internal_id"), row["internal_id"]),
            (_lb("maker"), row["maker"]),
            (_lb("date_created"), row["date_created"]),
            (_lb("carton_qty"), row["carton_qty"]),
            (_lb("order_lot"), row["order_lot"]),
        ]
        _cells = "".join(
            f'<div><div style="font-size:13px;color:#6e6e73;margin-bottom:2px;">{html.escape(lbl)}</div>'
            f'<div style="font-size:18px;font-weight:600;color:#1d1d1d;word-break:break-all;">{_esc(val)}</div></div>'
            for lbl, val in _fields
        )
        st.markdown(
            f'<div style="background:#fff;border:1px solid #d2d2d7;border-radius:18px;'
            f'padding:1.25rem 1.5rem;margin-bottom:1rem;">'
            f'<div style="font-size:13px;color:#6e6e73;margin-bottom:2px;">{html.escape(_lb("display_name"))}</div>'
            f'<div style="font-size:22px;font-weight:600;color:#1d1d1d;margin-bottom:1.1rem;">{_esc(row["display_name"])}</div>'
            f'<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1rem 1.5rem;">{_cells}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # 库存
        i1, i2, i3 = st.columns(3)
        i1.metric(_lb("qty_on_hand"), f"{int(row['qty_on_hand'] or 0):,}")
        i2.metric(_lb("qty_available"), f"{int(row['qty_available'] or 0):,}")
        i3.metric(_lb("qty_sold"), f"{int(row['qty_sold'] or 0):,}")

        # 原価（定义原价 → 平均原价 → 前次购入价）
        e1, e2, e3 = st.columns(3)
        e1.metric(_lb("cost_estimate"), f"¥{(row['cost_estimate'] or 0):,.2f}")
        e2.metric(_lb("average_cost"), f"¥{(row['average_cost'] or 0):,.2f}")
        e3.metric(_lb("last_purchase_cost"), f"¥{(row['last_purchase_cost'] or 0):,.2f}")

        # 物流尺重（JDL 仓库实测·替换原货主预报占位值）
        dim_df, _ = _query(
            "SELECT wms_gross_weight_g, wms_length_mm, wms_width_mm, wms_height_mm, wms_volume_cm3 "
            "FROM jdl.v_goods_dimensions WHERE jan = ?",
            (str(row["jan"]) if row["jan"] is not None else "",),
        )
        if dim_df is not None and not dim_df.empty:
            d = dim_df.iloc[0]
            st.markdown(f"**{_L('📦 物流尺寸（JDL 仓库实测）', '📦 物流寸法（JDL 倉庫実測）')}**")
            w1, w2, w3, w4 = st.columns(4)
            w1.metric(
                _L("毛重", "総重量"),
                f"{float(d['wms_gross_weight_g']):.0f} g" if d["wms_gross_weight_g"] is not None else "—",
            )
            w2.metric(
                _L("尺寸 长×宽×高", "寸法 長×幅×高"),
                f"{float(d['wms_length_mm']):.0f}×{float(d['wms_width_mm']):.0f}×{float(d['wms_height_mm']):.0f} mm"
                if all(d[k] is not None for k in ("wms_length_mm", "wms_width_mm", "wms_height_mm")) else "—",
            )
            w3.metric(
                _L("体积", "体積"),
                f"{float(d['wms_volume_cm3']):.0f} cm³" if d["wms_volume_cm3"] is not None else "—",
            )
            w4.metric(
                _L("理论抛重 (÷6000)", "容積重量 (÷6000)"),
                f"{float(d['wms_volume_cm3']) / 6000 * 1000:.0f} g"
                if d["wms_volume_cm3"] is not None else "—",
                help=_L("抛重 = 体积 cm³ ÷ 6000 = 容积重 kg，与实重取大计费",
                        "抛重 = 体積 cm³ ÷ 6000 = 容積重量 kg、実重と比較し大きい方で課金"),
            )
        else:
            st.caption(_L(
                "📦 物流尺寸（JDL 实测）：暂无数据 · 等下次 weekly 拉取或在「数据同步」手动触发",
                "📦 物流寸法（JDL 実測）：データなし · 次回 weekly 拉取またはマニュアル同期で取得",
            ))

        # 店舗×月 売上明細
        st.markdown(f"**{_L('店铺×月 销售明细', '店舗×月 売上明細')}**")
        sd, _ = _query(
            "SELECT shop, year_month, qty_sold, revenue FROM nst.sales_monthly "
            "WHERE item_internal_id = ? ORDER BY year_month DESC, revenue DESC",
            (row["internal_id"],),
        )
        if sd is None or sd.empty:
            st.caption(_L("（无销售记录）", "（売上記録なし）"))
        else:
            st.dataframe(
                sd, use_container_width=True, hide_index=True,
                column_config={
                    "shop": _L("店铺", "FB_店舗"),
                    "year_month": _L("年月", "年月"),
                    "qty_sold": _LBL["qty_sold"][1 if _JA else 0],
                    "revenue": _LBL["revenue"][1 if _JA else 0],
                },
            )


# ============================================================
# Tab 2: ⚖️ JDL vs NST 重量对比（modules/weight_compare）
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
