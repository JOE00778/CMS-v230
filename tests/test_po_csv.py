"""shared/po_csv（page10 発注書作成の NST 订货单 CSV 生成・page48 と共用）。"""
from datetime import date

import pandas as pd

from shared import po_csv


def test_build_output_columns_tax_and_missing():
    order = pd.DataFrame({"jan": ["4900000000011", "4900000000099"], "数量": [3, 2], "単価": [100, 50]})
    item = pd.DataFrame({"jan": ["4900000000011"], "item_code": ["A-1"], "display_name": ["名"],
                         "tax_schedule": ["仕入8%＆販売免税"]})
    out, missing = po_csv.build_output(order, item, ext_id="X1", sup="0123 仕入先", order_date=date(2026, 11, 13),
                                       employee=po_csv.EMPLOYEES[0], department=po_csv.DEPARTMENTS[0], memo="m",
                                       location=po_csv.LOCATIONS[0], tax_pct=10)
    assert list(out.columns) == ["外部ID", "仕入先", "日付", "従業員", "部門", "メモ", "場所", "アイテム",
                                 "数量", "単価/率", "金額", "税額", "総額"]
    row = out[out["アイテム"] == "A-1 名"].iloc[0]
    assert (row["金額"], row["税額"], row["総額"], row["日付"]) == (300, 24, 324, "2026/11/13")
    assert list(missing["jan"]) == ["4900000000099"]


def test_tax_rate_from_schedule():
    assert po_csv.tax_rate_from_schedule("免税") == 0
    assert po_csv.tax_rate_from_schedule("消費税10%") == 10
    assert po_csv.tax_rate_from_schedule(None) is None
