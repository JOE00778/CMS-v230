"""shared/period.py の純関数（月数・前期・シフト）の単体テスト。

ウィジェット本体（month_range_selector）は Streamlit ランタイム依存なので、
ここでは月計算の境界（年跨ぎ・単月・逆順）だけを固める。
"""
from shared.period import _month_count, _shift, prev_range, range_caption


def test_month_count_inclusive():
    assert _month_count("2026-01", "2026-01") == 1
    assert _month_count("2026-01", "2026-03") == 3


def test_month_count_across_year():
    assert _month_count("2025-11", "2026-02") == 4
    assert _month_count("2024-12", "2026-01") == 14


def test_shift_backwards_across_year():
    assert _shift("2026-01", -1) == "2025-12"
    assert _shift("2026-03", -3) == "2025-12"
    assert _shift("2026-01", -12) == "2025-01"


def test_shift_forward():
    assert _shift("2025-12", 1) == "2026-01"
    assert _shift("2026-11", 2) == "2027-01"


def test_prev_range_same_length():
    # 3 ヶ月 → 直前の 3 ヶ月
    assert prev_range("2026-06", "2026-08") == ("2026-03", "2026-05")
    # 単月 → 前月
    assert prev_range("2026-01", "2026-01") == ("2025-12", "2025-12")


def test_range_caption():
    assert range_caption("2026-07", "2026-07") == "2026-07"
    assert "2026-05" in range_caption("2026-05", "2026-08")
    assert range_caption("", "") == ""
