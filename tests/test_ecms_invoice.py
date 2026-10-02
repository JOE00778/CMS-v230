"""ECMS 請求書の取り込み検証.

実ファイル 2 ヶ月分（2026-05 / 2026-09）で踏んだ罠をそのまま固定する:
  · 月によって PDF のテキスト改行位置が変わる
  · 賠償がマイナス金額で入る（符号を落とすと行ごと消える）
  · xlsx だけでは請求総額に届かない
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from shared import ecms_invoice as ei

# ── 実ファイルから起こしたテキスト ────────────────────────────
# 2026-05: 連番/費目/金額/課税区分 が **同じ行**
TXT_202605 = """〒870-0048
請 求 書
大分県大分市碩田町3-1-35 Mitsukin bldg 発行日： 2026年5月31日
請求書番号： ECMS20260538
支払期限： 2026年6月30日
1 航空運賃 776,092 課税対象外
2 返送貨物 1,327 課税対象
3 追加請求 454 課税対象
小計 776,092 1,781
消費税等（10％） 178
請求金額合計 ¥776,092 ¥1,959 ¥778,051
"""

# 2026-09: 各項目が **別々の行**、かつ賠償がマイナス
TXT_202609 = """請 求 書
発行日：2026年9月30日
請求書番号：ECMS20260938
支払期限：2026年10月31日
1
航空運賃
774,364
課税対象外
2
賠償
-5,031
課税対象外
小計
769,333
0
消費税等（10％）
0
請求金額合計
¥769,333
¥0
¥769,333
"""


def test_parse_202605_same_line_layout():
    h = ei.parse_invoice_text(TXT_202605)
    assert h.invoice_no == "ECMS20260538"
    assert h.issue_date == dt.date(2026, 5, 31)
    assert [(l.seq, l.item_name, l.amount, l.tax_class) for l in h.lines] == [
        (1, "航空運賃", 776092.0, "課税対象外"),
        (2, "返送貨物", 1327.0, "課税対象"),
        (3, "追加請求", 454.0, "課税対象"),
    ]
    assert h.tax == 178.0
    assert h.total == 778051.0
    assert h.lines_total == 777873.0
    assert h.warnings == []          # 明細 + 税 = 総額 なので警告なし


def test_parse_202609_split_line_layout_and_negative():
    """改行位置が変わっても拾える & 賠償のマイナスを落とさない。"""
    h = ei.parse_invoice_text(TXT_202609)
    assert h.invoice_no == "ECMS20260938"
    assert h.issue_date == dt.date(2026, 9, 30)
    assert len(h.lines) == 2
    assert h.lines[1].item_name == "賠償"
    assert h.lines[1].amount == -5031.0      # ← 符号を落とすとここで落ちる
    assert h.lines_total == 769333.0
    assert h.total == 769333.0
    assert h.warnings == []


def test_cost_type_mapping():
    h = ei.parse_invoice_text(TXT_202605)
    assert [l.cost_type for l in h.lines] == [
        "ecms_freight", "ecms_return", "ecms_extra"]
    # 未知の費目は捨てずに ecms_other へ
    unknown = ei.InvoiceLine(9, "新しい費目", 100.0, "課税対象")
    assert unknown.cost_type == "ecms_other"


@pytest.mark.parametrize("raw,expected", [
    ("774,364", 774364.0), ("-5,031", -5031.0), ("−5,031", -5031.0),
    ("△5,031", -5031.0), ("▲5,031", -5031.0), ("0", 0.0),
])
def test_amount_sign_variants(raw, expected):
    assert ei._to_amount(raw) == expected


def test_warns_when_total_does_not_match():
    bad = TXT_202609.replace("¥769,333\n¥0\n¥769,333", "¥769,333\n¥0\n¥999,999")
    h = ei.parse_invoice_text(bad)
    assert any("一致しません" in w for w in h.warnings)


def test_warns_when_no_lines():
    h = ei.parse_invoice_text("請求書番号：ECMS20260000\n発行日：2026年1月31日\n")
    assert any("明細行" in w for w in h.warnings)


# ── 請求対象月はファイル名から（Ship Date からではない）──────────
@pytest.mark.parametrize("name,expected", [
    ("202609LBF様輸送費.xlsx", "2026-09"),
    ("ご請求書_20260938LBF.pdf", "2026-09"),
    ("202512LBF様輸送費.xlsx", "2025-12"),
    ("なにもない.xlsx", ""),
])
def test_year_month_from_name(name, expected):
    assert ei.year_month_from_name(name) == expected


# ── xlsx 明細 ────────────────────────────────────────────
def _detail_df(**over):
    base = {
        "Customer Name": ["MITSUKIN"], "Tracking No.": ["ECLBF26082500032"],
        "Order No.": ["9102513392212"], "# Of PKG": [1],
        "Freight（JPY）": [665], "Fuel Surcharge（JPY）": [242],
        "Permit Fee（JPY）": [0], "ESE care（JPY）": [0],
        "AU SurCharge（JPY）": [0], "Other（JPY）": [0], "Total（JPY）": [907],
        "Chargeable Weight\n（kg）": [3.5], "Gross Weight\n（kg）": [0.9],
        "Volume Weight（kg）": [3.0713], "Length": [42], "Width": [32.5],
        "Height": [13.5], "Destination": ["ICN"], "Ship Date": ["2026-08-25"],
    }
    base.update(over)
    return pd.DataFrame(base)


def test_normalize_detail_basic():
    out, warns = ei.normalize_detail(_detail_df())
    assert warns == []
    r = out.iloc[0]
    assert r["order_no"] == "9102513392212"
    assert r["ecms_freight"] == 665 and r["ecms_fuel"] == 242
    assert r["total_jpy"] == 907
    assert r["chargeable_kg"] == 3.5 and r["gross_kg"] == 0.9
    assert r["destination"] == "ICN"
    assert r["ship_date"] == dt.date(2026, 8, 25)


def test_order_no_keeps_leading_zero_and_strips_float():
    out, _ = ei.normalize_detail(_detail_df(**{"Order No.": ["0091025133922"]}))
    assert out.iloc[0]["order_no"] == "0091025133922"
    out, _ = ei.normalize_detail(_detail_df(**{"Order No.": ["9102513392212.0"]}))
    assert out.iloc[0]["order_no"] == "9102513392212"


def test_detail_warns_on_internal_mismatch():
    # 内訳 665+242=907 なのに Total を 999 にする
    _, warns = ei.normalize_detail(_detail_df(**{"Total（JPY）": [999]}))
    assert any("内訳合計" in w for w in warns)


def test_detail_warns_on_blank_order_no():
    _, warns = ei.normalize_detail(_detail_df(**{"Order No.": [None]}))
    assert any("Order No." in w for w in warns)


def test_missing_required_column_raises():
    df = _detail_df().drop(columns=["Order No."])
    with pytest.raises(ValueError, match="Order No."):
        ei.normalize_detail(df)


def test_total_column_is_not_double_counted():
    """Total は検算専用。cost_type の集計に混ざっていないこと。"""
    out, _ = ei.normalize_detail(_detail_df())
    amounts = out[list(ei.XLSX_AMOUNT_COLS.values())].sum(axis=1).iloc[0]
    assert amounts == 907
    assert "ecms_total" not in out.columns


# ── 明細 dict のキーと DB 列の対応（ズレると黙って NULL になる）──────
def test_normalize_detail_covers_all_db_columns():
    """ecms_cost が INSERT するキーを normalize_detail が全部持っていること。

    length/width/height ↔ length_cm/width_cm/height_cm のズレを実際にやらかした。
    INSERT は通ってしまい、その列だけ NULL になるので気づけない。
    """
    from shared import ecms_cost as ec

    out, _ = ei.normalize_detail(_detail_df())
    missing = [k for k in ec.detail_row_keys() if k not in out.columns]
    assert missing == [], f"normalize_detail に無いキー: {missing}"
