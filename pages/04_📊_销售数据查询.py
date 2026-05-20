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
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t, get_lang

st.set_page_config(page_title=t("销售数据查询"), page_icon="📊", layout="wide")
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

st.title(t("📊 销售数据查询"))
st.caption(t(
    "NST API 売上データ（店舗別売上 レポート再現・数量完全一致）· "
    "店舗×SKU×月 + 商品マスタ + 在庫 統合 · 粗利/粗利率 自動計算"
))

# 列見出し: (中文, 日本語) — UI 言語追従
_LBL = {
    "shop":          ("店铺", "FB_店舗"),
    "jan":           ("UPC编码", "UPCコード"),
    "display_name":  ("显示名", "表示名"),
    "maker":         ("厂商", "メーカー名"),
    "item_rank":     ("商品等级", "商品ランク"),
    "handling_cd":   ("经销状态", "取扱区分"),
    "qty_sold":      ("销售数量", "販売数量"),
    "revenue":       ("总收益", "総収益"),
    "teigi_genka":   ("定义原价", "定義原価"),
    "arari":         ("毛利", "粗利"),
    "arari_rate":    ("毛利率", "粗利率"),
    "qty_on_hand":   ("现有库存", "手持"),
    "qty_available": ("可用库存", "利用可能"),
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
c1, c2, c3 = st.columns([1.2, 2, 3])
ym = c1.selectbox(t("対象月"), months_df["year_month"].tolist())
view = c2.radio(t("表示単位"), [t("店舗別"), t("アイテム別")], horizontal=True)
kw = c3.text_input(t("JAN / 商品名 検索"), placeholder="JAN コード or 表示名の一部")

shop_filter = None
if view == t("店舗別"):
    shops_df, _ = _query(
        "SELECT DISTINCT shop FROM nst.sales_monthly WHERE year_month=? ORDER BY shop", (ym,)
    )
    shop_opts = [t("（全店舗）")] + (shops_df["shop"].tolist() if shops_df is not None else [])
    shop_filter = st.selectbox(t("店舗"), shop_opts)

# ============================================================
# クエリ組み立て
# ============================================================
_INV = ("(SELECT item_internal_id, qty_on_hand, qty_available FROM nst.inventory_snapshot "
        "WHERE snapshot_date=(SELECT max(snapshot_date) FROM nst.inventory_snapshot))")

where = ["s.year_month = ?"]
params: list = [ym]
if view == t("店舗別") and shop_filter and shop_filter != t("（全店舗）"):
    where.append("s.shop = ?"); params.append(shop_filter)
if kw.strip():
    where.append("(im.jan LIKE ? OR im.display_name LIKE ?)")
    like = f"%{kw.strip()}%"; params += [like, like]
where_sql = " AND ".join(where)

if view == t("店舗別"):
    df, e2 = _query(
        "SELECT s.shop, im.jan, im.display_name, im.maker, im.item_rank, im.handling_cd, "
        "s.qty_sold, s.revenue, "
        "(COALESCE(im.cost_estimate,0)*s.qty_sold) AS teigi_genka, "
        "inv.qty_on_hand, inv.qty_available "
        "FROM nst.sales_monthly s "
        "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
        f"LEFT JOIN {_INV} inv ON inv.item_internal_id = s.item_internal_id "
        f"WHERE {where_sql} ORDER BY s.revenue DESC LIMIT 5000",
        tuple(params),
    )
    cols = ("shop", "jan", "display_name", "maker", "item_rank", "handling_cd",
            "qty_sold", "revenue", "teigi_genka", "arari", "arari_rate",
            "qty_on_hand", "qty_available")
else:
    df, e2 = _query(
        "SELECT im.jan, im.display_name, im.maker, im.item_rank, im.handling_cd, "
        "SUM(s.qty_sold) AS qty_sold, SUM(s.revenue) AS revenue, "
        "(MAX(COALESCE(im.cost_estimate,0))*SUM(s.qty_sold)) AS teigi_genka, "
        "MAX(inv.qty_on_hand) AS qty_on_hand, MAX(inv.qty_available) AS qty_available "
        "FROM nst.sales_monthly s "
        "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
        f"LEFT JOIN {_INV} inv ON inv.item_internal_id = s.item_internal_id "
        f"WHERE {where_sql} "
        "GROUP BY im.jan, im.display_name, im.maker, im.item_rank, im.handling_cd "
        "ORDER BY revenue DESC LIMIT 5000",
        tuple(params),
    )
    cols = ("jan", "display_name", "maker", "item_rank", "handling_cd",
            "qty_sold", "revenue", "teigi_genka", "arari", "arari_rate",
            "qty_on_hand", "qty_available")

# ============================================================
# 表示
# ============================================================
if e2:
    st.error(e2)
elif df is None or df.empty:
    st.info(t("この条件のデータがありません"))
else:
    df["revenue"] = df["revenue"].astype(float)
    df["teigi_genka"] = df["teigi_genka"].astype(float)
    df["arari"] = df["revenue"] - df["teigi_genka"]
    df["arari_rate"] = (df["arari"] / df["revenue"].where(df["revenue"] != 0)).round(4)

    tot_q = df["qty_sold"].astype(float).sum()
    tot_r = df["revenue"].sum()
    tot_g = df["arari"].sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(t("対象月"), ym)
    m2.metric(t("販売数量 計"), f"{tot_q:,.0f}")
    m3.metric(t("総収益 計"), f"¥{tot_r:,.0f}")
    m4.metric(t("粗利 計"), f"¥{tot_g:,.0f}", f"{(tot_g/tot_r if tot_r else 0):.1%}")

    st.caption(t("表示件数（最大 5000 件）: ") + f"{len(df):,}")
    st.dataframe(
        df[list(cols)], use_container_width=True, height=560,
        column_config=_cc(*cols),
    )
