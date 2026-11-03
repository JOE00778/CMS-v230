"""shared.benten_recon.reconcile（page06 弁天 vs 斑马 对账）."""
import pandas as pd

from shared import benten_recon as BR

P = {"minor_abs_threshold": 1, "minor_pct_threshold": 0.0}


def test_reconcile_statuses_diff_and_item_info():
    nst = pd.DataFrame({"jan": ["A", "B", "C", "D"], "nst_qty": [10, 10, 10, 5]})
    bm = pd.DataFrame({"jan": ["A", "B", "C", "E"], "bm_qty": [10, 9, 3, 4]})
    items = pd.DataFrame({"jan": ["A"], "item_code": ["X-1"], "display_name": ["商品A"],
                          "maker": ["M"], "item_rank": ["Aランク"]})
    r = BR.reconcile(nst, bm, items, P).set_index("jan")
    assert r["status"].to_dict() == {"A": "OK", "B": "MINOR_DIFF", "C": "MAJOR_DIFF",
                                     "D": "NST_ONLY", "E": "BANMA_ONLY"}
    assert r.loc["C", "diff"] == 7 and r.loc["D", "diff"] == 5 and r.loc["E", "diff"] == -4
    assert r.loc["A", "display_name"] == "商品A" and pd.isna(r.loc["B", "display_name"])
    assert set(r["status"]) <= set(BR.STATUSES)


def test_sql_scope_is_benten_export_qualified_only():
    assert "三金弁天仓库" in BR.BANMA_SQL and "category_top = 'Export'" in BR.BANMA_SQL
    assert "qty_qualified)" in BR.BANMA_SQL
    assert "is_cb" in BR.NST_SQL and "is_return" in BR.NST_SQL and "is_defect" in BR.NST_SQL
