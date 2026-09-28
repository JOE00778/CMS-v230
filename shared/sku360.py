"""SKU 360 の母集団と在庫/販売/在途の取得（口径の単一事実源）.

page06「库存监控 → 库存风控」の SKU 360 宽表と、発注AI v3 が**同じ数字**を見るための
共通取得層。ここを直せば両方が同時に変わる（2026-09-28 に v3 を作った際、同じ
「JDL库存 / 在途残 / 前N天销量」を別ソースで引いていて数字が割れたので統合した）。

⚠️ ソースの選択には意味があるので勝手に変えないこと:
  · JDL库存  = `jdl.v_inventory_reconciliation.jdl_qty_in_stock`（JD 実物在庫・JAN 突合）
               ← `nst.inventory_snapshot.qty_on_hand`（NST 帳簿在庫）**ではない**
  · 在途残   = `nst.purchase_order_line` の未入荷残（closed=FALSE の quantity − received）
               ← `inventory_snapshot.qty_on_order` **ではない**
  · 母集団   = `item_rank` が入っている商品だけ（等級未設定は対象外）
  · 可售天数 = 在庫 ÷ 日均（`shared.inventory_risk.days_of_supply`）。**在途を含まない**
"""
from __future__ import annotations

import pandas as pd

DEFAULT_WINDOW_DAYS = 30


def fetch_sku360(conn, window_days: int = DEFAULT_WINDOW_DAYS) -> pd.DataFrame:
    """商品マスタ（等級あり）× 前N日販売 × JDL 実在庫 × 未入荷残。

    戻り列: internal_id / item_code / jan / display_name / rank / handling_cd /
            maker / date_created / cost_estimate / last_purchase_cost /
            avg_unit_price / qty_sold / revenue_30d / gp_30d /
            current_stock / in_transit_qty

    在庫・在途が引けない場合も 0 で埋めて返す（列は必ず存在する）。
    販売 / マスタ側の失敗は例外のまま呼び出し側へ投げる（黙って空にしない）。
    """
    from shared.cache import cached_df, data_version

    def _df(sql, params=None):
        return cached_df(conn, sql, params,
                         ver=data_version("basic", "inventory", "sales", "purchase"))

    df = _df(
        f"""
        WITH s30 AS (
            SELECT item_internal_id, SUM(qty_sold) AS qty_sold,
                   SUM(revenue) AS revenue_30d, SUM(gross_profit) AS gp_30d
            FROM nst.sales_daily
            WHERE sale_date >= CURRENT_DATE - INTERVAL '{int(window_days)} days'
            GROUP BY item_internal_id
        )
        SELECT im.internal_id AS internal_id, im.item_code AS item_code, im.jan AS jan,
               COALESCE(im.display_name, '') AS display_name,
               im.item_rank AS rank, im.handling_cd AS handling_cd,
               im.maker AS maker, im.date_created AS date_created,
               im.cost_estimate AS cost_estimate, im.last_purchase_cost AS last_purchase_cost,
               COALESCE(im.average_cost, im.cost_estimate) AS avg_unit_price,
               COALESCE(s30.qty_sold, 0) AS qty_sold,
               COALESCE(s30.revenue_30d, 0) AS revenue_30d,
               COALESCE(s30.gp_30d, 0) AS gp_30d
        FROM nst.item_master_raw im
        LEFT JOIN s30 ON s30.item_internal_id = im.internal_id
        WHERE im.item_rank IS NOT NULL AND btrim(im.item_rank) <> ''
        """
    )
    if df.empty:
        return df

    # 当前库存 = JDL 実物在庫（jdl.v_inventory_reconciliation · jan 突合）
    try:
        _jdl = _df("SELECT jan, jdl_qty_in_stock AS current_stock "
                   "FROM jdl.v_inventory_reconciliation")
    except Exception:
        _jdl = pd.DataFrame(columns=["jan", "current_stock"])
    if not _jdl.empty:
        df = df.merge(_jdl, how="left", on="jan")
    if "current_stock" not in df.columns:
        df["current_stock"] = 0
    df["current_stock"] = pd.to_numeric(df["current_stock"], errors="coerce").fillna(0)

    # 在途残 = 未クローズ PO の入荷残（item_internal_id 突合）
    try:
        _itx = _df(
            "SELECT item_internal_id, SUM(quantity - COALESCE(quantity_received,0)) AS in_transit_qty "
            "FROM nst.purchase_order_line "
            "WHERE closed = FALSE AND (quantity - COALESCE(quantity_received,0)) > 0 "
            "GROUP BY item_internal_id")
    except Exception:
        _itx = pd.DataFrame(columns=["item_internal_id", "in_transit_qty"])
    if not _itx.empty:
        df = df.merge(_itx, how="left", left_on="internal_id",
                      right_on="item_internal_id")
        if "item_internal_id" in df.columns:
            df = df.drop(columns=["item_internal_id"])
    if "in_transit_qty" not in df.columns:
        df["in_transit_qty"] = 0
    df["in_transit_qty"] = pd.to_numeric(df["in_transit_qty"], errors="coerce").fillna(0)
    return df
