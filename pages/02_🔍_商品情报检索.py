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

import re

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t, get_lang

st.set_page_config(page_title=t("商品情报检索"), page_icon="🔍", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
# 本ページのみ：データ表が画面幅いっぱいに広がるよう全局 1400px 上限を解除
st.markdown(
    "<style>[data-testid='stMainBlockContainer'],.main .block-container"
    "{max-width:100%!important;}</style>",
    unsafe_allow_html=True,
)
lang_selector()
conn = get_connection()

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
    "item_code":          ("商品编码", "アイテム"),
    "jan":                ("UPC编码", "UPCコード"),
    "display_name":       ("显示名", "表示名"),
    "maker":              ("厂商", "メーカー名"),
    "item_rank":          ("商品等级", "商品ランク"),
    "handling_cd":        ("经销状态", "取扱区分"),
    "qty_on_hand":        ("现有库存", "手持"),
    "qty_available":      ("可用库存", "利用可能"),
    "qty_on_order":       ("在订数量", "注文済"),
    "stock_amount":       ("库存金额", "在庫金額"),
    "cost":               ("标准原价", "標準原価"),
    "average_cost":       ("平均原价", "平均原価"),
    "cost_estimate":      ("定义原价", "定義原価"),
    "last_purchase_cost": ("前次购入价", "前回購入価格"),
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
            SELECT item_internal_id, qty_on_hand, qty_available, qty_on_order
            FROM nst.inventory_snapshot
            WHERE snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot)
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
            im.cost, im.average_cost, im.cost_estimate, im.last_purchase_cost,
            im.carton_qty, im.order_lot,
            inv.qty_on_hand, inv.qty_available, inv.qty_on_order,
            (COALESCE(im.average_cost, im.cost_estimate, 0)
                * COALESCE(inv.qty_on_hand, 0)) AS stock_amount,
            sales.qty_sold, sales.revenue
        FROM nst.item_master_raw im
        LEFT JOIN inv   ON inv.item_internal_id   = im.internal_id
        LEFT JOIN sales ON sales.item_internal_id = im.internal_id
    """
    rows = conn.execute(sql).fetchall()
    cols = ["internal_id", "item_code", "jan", "display_name", "maker", "item_rank",
            "handling_cd", "is_inactive", "cost", "average_cost", "cost_estimate",
            "last_purchase_cost", "carton_qty", "order_lot", "qty_on_hand",
            "qty_available", "qty_on_order", "stock_amount", "qty_sold", "revenue"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows], columns=cols)


df = load_sku_view()
total_skus = len(df)

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
handle_pick = f1.selectbox(_L("经销状态", "取扱区分"), [_ALL] + _opts("handling_cd"))
rank_pick = f2.selectbox(_L("商品等级", "商品ランク"), [_ALL] + _opts("item_rank"))
maker_pick = f3.selectbox(_L("厂商", "メーカー名"), [_ALL] + _opts("maker"))
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
if handle_pick != _ALL:
    v = v[v["handling_cd"] == handle_pick]
if rank_pick != _ALL:
    v = v[v["item_rank"] == rank_pick]
if maker_pick != _ALL:
    v = v[v["maker"].astype(str) == maker_pick]
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
    st.stop()

# 数値整形
for col in ("revenue", "stock_amount", "cost", "average_cost", "cost_estimate",
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

cols = ("item_code", "jan", "display_name", "maker", "item_rank", "handling_cd",
        "qty_on_hand", "qty_available", "qty_on_order", "stock_amount",
        "cost", "average_cost", "cost_estimate", "last_purchase_cost",
        "carton_qty", "order_lot", "qty_sold", "revenue")
st.dataframe(
    v[list(cols)], use_container_width=True, height=560, hide_index=True,
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

d1, d2, d3 = st.columns(3)
with d1:
    st.markdown(f"**{_LBL['item_code'][1 if _JA else 0]}**: `{row['item_code']}`")
    st.markdown(f"**{_LBL['jan'][1 if _JA else 0]}**: `{row['jan']}`")
    st.markdown("**Internal ID**: `%s`" % row["internal_id"])
    st.markdown(f"**{_LBL['display_name'][1 if _JA else 0]}**: {row['display_name']}")
with d2:
    st.markdown(f"**{_LBL['handling_cd'][1 if _JA else 0]}**: {row['handling_cd'] or '—'}")
    st.markdown(f"**{_LBL['item_rank'][1 if _JA else 0]}**: {row['item_rank'] or '—'}")
    st.markdown(f"**{_LBL['maker'][1 if _JA else 0]}**: {row['maker'] or '—'}")
    st.markdown(f"**{_LBL['carton_qty'][1 if _JA else 0]}**: {row['carton_qty'] or '—'}"
                f" ｜ **{_LBL['order_lot'][1 if _JA else 0]}**: {row['order_lot'] or '—'}")
with d3:
    st.metric(_LBL["qty_on_hand"][1 if _JA else 0], f"{int(row['qty_on_hand'] or 0):,}")
    st.metric(_LBL["qty_available"][1 if _JA else 0], f"{int(row['qty_available'] or 0):,}")
    st.metric(_LBL["qty_sold"][1 if _JA else 0], f"{int(row['qty_sold'] or 0):,}")

# 原価各種
e1, e2, e3, e4 = st.columns(4)
e1.metric(_LBL["cost"][1 if _JA else 0], f"¥{(row['cost'] or 0):,.2f}")
e2.metric(_LBL["average_cost"][1 if _JA else 0], f"¥{(row['average_cost'] or 0):,.2f}")
e3.metric(_LBL["cost_estimate"][1 if _JA else 0], f"¥{(row['cost_estimate'] or 0):,.2f}")
e4.metric(_LBL["last_purchase_cost"][1 if _JA else 0], f"¥{(row['last_purchase_cost'] or 0):,.2f}")

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
