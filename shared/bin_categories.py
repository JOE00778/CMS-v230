"""弁天倉庫 bin 番号 → 用途分类（Boss 2026-05-27）.

弁天 1 SKU 可能多个棚。用 SKU 命中哪些「特殊」棚来分类商品用途：
  - 返品（HENPIN-EX）:    返品入庫の暫定棚
  - 不良品（FF-3）:       不良品の隔离棚
  - 输出中国（CB）:       1-0105A / 1-0106A / 1-0107A / yusyutu2F
  - 通常输出:             どの特殊棚にも入っていない SKU

PG での判定 SQL は `_BIN_CAT_CTE` をそのまま埋め込めばよい。
返り値 category 列の値: '返品' / '不良品' / '输出中国' / '输出'。
優先順位（複数該当時）: 返品 > 不良品 > 输出中国 > 输出。
"""
from __future__ import annotations

BIN_CB = ("1-0105A", "1-0106A", "1-0107A", "yusyutu2F")
BIN_RETURN = "HENPIN-EX"
BIN_DEFECT = "FF-3"

# SKU × 棚 snapshot → SKU × 用途フラグ（最新 snapshot_date のみ）
# 利用例:
#   WITH bin_cat AS ({_BIN_CAT_CTE})
#   SELECT ... FROM pol LEFT JOIN bin_cat bc ON bc.item_internal_id = pol.item_internal_id
_BIN_CAT_CTE = """
SELECT item_internal_id,
       BOOL_OR(bin_number = '{ret}') AS is_return,
       BOOL_OR(bin_number = '{def_}') AS is_defect,
       BOOL_OR(bin_number IN ('{cb0}','{cb1}','{cb2}','{cb3}')) AS is_cb
FROM nst.inventory_bin_snapshot
WHERE snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_bin_snapshot)
GROUP BY item_internal_id
""".format(ret=BIN_RETURN, def_=BIN_DEFECT,
           cb0=BIN_CB[0], cb1=BIN_CB[1], cb2=BIN_CB[2], cb3=BIN_CB[3])


def category_cte() -> str:
    """CTE 本体（WITH bin_cat AS (...) で囲む用）·優先順位は SELECT 側 CASE で実現."""
    return _BIN_CAT_CTE


# CASE 式（item の最終分類）·優先順位: 返品 > 不良品 > 输出中国 > 输出
CATEGORY_CASE = """
CASE
    WHEN COALESCE(bc.is_return, FALSE) THEN '返品'
    WHEN COALESCE(bc.is_defect, FALSE) THEN '不良品'
    WHEN COALESCE(bc.is_cb,     FALSE) THEN '输出中国'
    ELSE '输出'
END
"""

CATEGORIES = ("输出", "输出中国", "返品", "不良品")
