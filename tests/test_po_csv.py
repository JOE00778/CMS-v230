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


def _build(order, item):
    return po_csv.build_output(order, item, ext_id="X1", sup="0123 仕入先", order_date=date(2026, 11, 17),
                               employee=po_csv.EMPLOYEES[0], department=po_csv.DEPARTMENTS[0], memo="m",
                               location=po_csv.LOCATIONS[0], tax_pct=10)


ITEM = pd.DataFrame({"jan": ["4900000000011", "4900000000028"], "item_code": ["A", "B"], "display_name": ["名A", "名B"],
                     "tax_schedule": ["消費税10%", "消費税10%"]})


def test_decimal_unit_price_kept_20261117():
    """単価の小数を切り捨てない（監査 M2: 793.6 → 793 で PO 金額が低くなっていた）。"""
    out, _ = _build(pd.DataFrame({"jan": ["4900000000011"], "数量": [1200], "単価": [793.6]}), ITEM)
    r = out.iloc[0]
    assert (r["単価/率"], r["金額"], r["税額"], r["総額"]) == ("793.6", 952320, 95232, 1047552)


def test_unit_price_text_4_decimals_and_half_up_20261118():
    """単価/率 は整数なら「100」（混在でも 100.0 にしない）・小数 4 桁まで・金額は 0.5 を切り上げ（再審査）。"""
    out, _ = _build(pd.DataFrame({"jan": ["4900000000011", "4900000000011", "4900000000011"],
                                  "数量": [3, 1, 1], "単価": [100.0, 12.3456, 2.5]}), ITEM)
    assert out["単価/率"].tolist() == ["100", "12.3456", "2.5"]
    assert out["金額"].tolist() == [300, 12, 3]
    assert "100.0" not in out.to_csv(index=False)


def test_float_jan_and_blank_rows_20261117():
    """空行・合計行で JAN 列が float（'4900000000011.0'）になっても照合でき、空 JAN 行・未登録 JAN は CSV に出さない（監査 M3）。"""
    order = pd.DataFrame({"jan": [4900000000011.0, 4900000000028.0, None, 4900000000099.0],
                          "数量": [2, 3, 5, 1], "単価": [100, 200, None, 50]})
    out, missing = _build(order, ITEM)
    assert list(out["アイテム"]) == ["A 名A", "B 名B"]
    assert list(missing["jan"]) == ["4900000000099"]


def test_parse_items_text_thousands_yen_and_numbering_20261117():
    """貼り付けテキスト: 千位カンマ・¥・行頭の番号・小数（監査 M12: ¥1,280 が 1 になっていた）。"""
    df = po_csv.parse_items_text("4901234567890 12 1,280\n1. 4901234567891 10 ¥500\n2) 4901234567892 6 793.6")
    assert df.to_dict("records") == [{"jan": "4901234567890", "数量": 12, "単価": 1280.0},
                                     {"jan": "4901234567891", "数量": 10, "単価": 500.0},
                                     {"jan": "4901234567892", "数量": 6, "単価": 793.6}]


def test_parse_items_text_csv_line_and_jan_at_end_20261118():
    """カンマ区切りの行はカンマを千位として消さない・JAN が行末でも数量/単価を読む（再審査 M12: 24,500 が 24500 になった）。"""
    df = po_csv.parse_items_text("4901234567890,24,500\n4901234567891,100,500\n12 1,280 4901234567892\n"
                                 "4901234567893\t6\t1280")
    assert df.to_dict("records") == [{"jan": "4901234567890", "数量": 24, "単価": 500.0},
                                     {"jan": "4901234567891", "数量": 100, "単価": 500.0},
                                     {"jan": "4901234567892", "数量": 12, "単価": 1280.0},
                                     {"jan": "4901234567893", "数量": 6, "単価": 1280.0}]
