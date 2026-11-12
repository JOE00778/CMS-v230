"""SKU 360 の母集団と在庫/販売/在途の取得（口径の単一事実源）.

page06「库存监控 → 库存风控」の SKU 360 宽表と、発注AI v3 が**同じ数字**を見るための
共通取得層。ここを直せば両方が同時に変わる（2026-09-28 に v3 を作った際、同じ
「JDL库存 / 在途残 / 前N天销量」を別ソースで引いていて数字が割れたので統合した）。

⚠️ ソースの選択には意味があるので勝手に変えないこと:
  · 当前库存 = NST 弁天倉庫 bin 快照最新日 ×「输出」棚（输出中国/返品/不良品 除外）の手持合計
               （item_internal_id 突合）。Boss 2026-11-04「京东库存删除・改大分弁天」で
               旧 `jdl.v_inventory_reconciliation.jdl_qty_in_stock`（JD 実物）から置換。
               口径は page06 弁天 vs 斑马 对账の NST 侧（shared.benten_recon.NST_SQL）と同じ
  · 在途残   = `nst.purchase_order_line` の未入荷残（closed=FALSE の quantity − received）
               ← `inventory_snapshot.qty_on_order` **ではない**
  · 母集団   = `item_rank` が入っている商品だけ（等級未設定は対象外）
  · 可售天数 = 在庫 ÷ 日均（`shared.inventory_risk.days_of_supply`）。**在途を含まない**
"""
from __future__ import annotations

import pandas as pd

from shared.i18n import t

DEFAULT_WINDOW_DAYS = 30

BENTEN_EXPORT_STOCK_SQL = """
SELECT b.item_internal_id AS internal_id, SUM(b.qty_on_hand) AS current_stock
FROM nst.inventory_bin_snapshot b
LEFT JOIN nst.bin_category bc ON bc.bin_number = b.bin_number
WHERE b.snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_bin_snapshot)
  AND NOT (COALESCE(bc.is_return, FALSE) OR COALESCE(bc.is_defect, FALSE) OR COALESCE(bc.is_cb, FALSE))
GROUP BY b.item_internal_id
"""


def fetch_sku360(conn, window_days: int = DEFAULT_WINDOW_DAYS,
                 *, errors: list | None = None) -> pd.DataFrame:
    """商品マスタ（等級あり）× 前N日販売 × 弁天输出棚在庫 × 未入荷残。

    戻り列: internal_id / item_code / jan / display_name / rank / handling_cd /
            maker / date_created / cost_estimate / last_purchase_cost /
            avg_unit_price / qty_sold / revenue_30d / gp_30d /
            current_stock / in_transit_qty

    在庫・在途が引けない場合も 0 で埋めて返す（列は必ず存在する）。
    販売 / マスタ側の失敗は例外のまま呼び出し側へ投げる（黙って空にしない）。

    ⚠️ `errors` に list を渡すと、在庫 / 在途の取得失敗を**メッセージとして受け取れる**。
    渡さないと従来どおり 0 で埋めて黙って続行する。2026-10-01 にこれで事故った（当時の在庫源）:
    `jdl.v_inventory_reconciliation` が 73.7 秒まで遅くなり cms_reader の
    statement_timeout=30s に掛かって QueryCanceled → ここが握り潰して current_stock
    全件 0 → 可售天数が算出不能 → 库存风控が「何も出ない」ように見えた。
    画面に出す側は必ず errors を渡して表示すること（AGENTS.md §0.5 失敗は明示）。
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
    # 取扱区分は未設定が普通にあるので空文字に寄せる（表示でも判定でも None を回さない）
    df["handling_cd"] = df["handling_cd"].fillna("").astype(str)

    # 当前库存 = 弁天「输出」棚の手持（nst.inventory_bin_snapshot 最新日 · item_internal_id 突合）
    try:
        _stk = _df(BENTEN_EXPORT_STOCK_SQL)
    except Exception as e:
        if errors is not None:
            errors.append(
                t("弁天在庫（nst.inventory_bin_snapshot）が取得できません → "
                  "在庫・可售天数・リスク分档はすべて 0 / 判定不能として表示されます") + f": {e}")
        _stk = pd.DataFrame(columns=["internal_id", "current_stock"])
    if not _stk.empty:
        df = df.merge(_stk, how="left", on="internal_id")
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
    except Exception as e:
        if errors is not None:
            errors.append(
                t("在途残（nst.purchase_order_line）が取得できません → "
                  "在途はすべて 0 として表示されます") + f": {e}")
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
