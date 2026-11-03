"""弁天 vs 斑马 库存对账（page06「库存构成分析」子 tab）· Boss 2026-11-04.

旧「NST vs JDL 账实对账」（JD 仓账面 vs 京东实物）を置換。口径（Boss 拍板）:
  · NST 侧 = 弁天倉庫 bin 快照最新日 ×「输出」棚だけ（输出中国=CB 部门 / 返品 / 不良品 は除外）· JAN 単位で合算
  · 斑马侧 = banma.inventory_snapshot 最新日 × 三金弁天仓库 × 类目 Export（banma.sku_dims.category_top）
            × 合格品数（qty_qualified）。他 2 仓（TEMU / 予約販売）と弁天仓内の EC 等は CB 部门なので見ない
差異档位は旧対账と同じ閾値（shared.jdl_recon_settings · 系统参数设定で変更）。
"""
from __future__ import annotations

import pandas as pd

from shared.jdl_recon_settings import classify_status

BENTEN_WH = "三金弁天仓库"
EXPORT_CAT = "Export"
STATUSES = ("OK", "MINOR_DIFF", "MAJOR_DIFF", "NST_ONLY", "BANMA_ONLY")

NST_SQL = """
SELECT im.jan, SUM(b.qty_on_hand) AS nst_qty, MAX(b.snapshot_date) AS nst_date
FROM nst.inventory_bin_snapshot b
LEFT JOIN nst.bin_category bc ON bc.bin_number = b.bin_number
JOIN nst.item_master_raw im ON im.internal_id = b.item_internal_id
WHERE b.snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_bin_snapshot)
  AND NOT (COALESCE(bc.is_return, FALSE) OR COALESCE(bc.is_defect, FALSE) OR COALESCE(bc.is_cb, FALSE))
  AND COALESCE(im.jan, '') <> ''
GROUP BY im.jan
"""

BANMA_SQL = f"""
SELECT s.sku_code AS jan, SUM(s.qty_qualified) AS bm_qty,
       SUM(s.qty_qualified_available) AS bm_available, SUM(s.qty_qualified_lock) AS bm_lock,
       MAX(s.last_out_time) AS bm_last_out, MAX(s.snapshot_date) AS bm_date
FROM banma.inventory_snapshot s
JOIN banma.sku_dims k ON k.sku_id = s.sku_id
WHERE s.snapshot_date = (SELECT max(snapshot_date) FROM banma.inventory_snapshot)
  AND s.warehouse_name = '{BENTEN_WH}' AND k.category_top = '{EXPORT_CAT}'
GROUP BY s.sku_code
"""

ITEM_SQL = """
SELECT DISTINCT ON (jan) jan, item_code, display_name, maker, item_rank
FROM nst.item_master_raw WHERE COALESCE(jan, '') <> ''
ORDER BY jan, internal_id DESC
"""


def reconcile(nst: pd.DataFrame, bm: pd.DataFrame, items: pd.DataFrame,
              params: dict | None = None) -> pd.DataFrame:
    """JAN で外部結合 → status / diff（NST − 斑马）。片側に無い JAN は ONLY。"""
    rec = nst.merge(bm, how="outer", on="jan")
    for c in ("nst_qty", "bm_qty"):
        rec[c] = pd.to_numeric(rec[c], errors="coerce")
    rec["status"] = [
        "BANMA_ONLY" if pd.isna(n) else
        classify_status(n, None if pd.isna(b) else b, params)
        for n, b in zip(rec["nst_qty"], rec["bm_qty"])]
    rec["diff"] = rec["nst_qty"].fillna(0) - rec["bm_qty"].fillna(0)
    return rec.merge(items, how="left", on="jan")


def load(df_fn) -> pd.DataFrame:
    """df_fn(sql) → DataFrame（page 側の cached_df）。"""
    from shared.jdl_recon_settings import load_recon_params
    return reconcile(df_fn(NST_SQL), df_fn(BANMA_SQL), df_fn(ITEM_SQL), load_recon_params())
