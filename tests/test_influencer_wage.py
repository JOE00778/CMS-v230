"""shared/influencer_wage の純ロジックテスト（DB/Streamlit 不要）.

解析(时长/peso) + 直播名→人マッチ + 階梯抽成 + 給与式 を フィリピンバイト料 仕様で固定。
"""
from __future__ import annotations

import pytest

from shared import influencer_wage as iw
from shared.influencer_wage import WageParams

_P = WageParams()  # 既定 61.49 / 155 / 1.045 / 20万 / 1% / 1.5%

_ROSTER = {
    "Bella": ["bella", "bell", "bel"],
    "Charm": ["charm"],
}


# ---- parse_duration_hours ----
def test_duration_hms():
    assert iw.parse_duration_hours("08:14:00") == pytest.approx(8 + 14 / 60)
    assert iw.parse_duration_hours("01:25:23") == pytest.approx(1 + 25 / 60 + 23 / 3600)
    assert iw.parse_duration_hours("00:00:11") == pytest.approx(11 / 3600)


def test_duration_edge():
    assert iw.parse_duration_hours("") == 0.0
    assert iw.parse_duration_hours(None) == 0.0
    assert iw.parse_duration_hours("90:00") == pytest.approx(1.5)  # MM:SS 補位


# ---- parse_peso ----
def test_peso_variants():
    assert iw.parse_peso("₱48,672") == 48672.0
    assert iw.parse_peso("48,672") == 48672.0
    assert iw.parse_peso(46735) == 46735.0
    assert iw.parse_peso("₱1,234.5") == 1234.5
    assert iw.parse_peso("") == 0.0
    assert iw.parse_peso(None) == 0.0


# ---- match_influencer（D列乱后缀）----
def test_match_charm_variants():
    assert iw.match_influencer("🇯🇵PAYDAY MEGASALE🇯🇵_CHARM", _ROSTER) == "Charm"
    assert iw.match_influencer("🇯🇵Japan Essentials🇯🇵_Charm", _ROSTER) == "Charm"


def test_match_bella_variants():
    for nm in ["🇯🇵J-Beauty SULIT DEALS🇯🇵_Bella", "🇯🇵Extended Sale🇯🇵_Bell",
               "🇯🇵 J-Beauty Extended Sale 🇯🇵_Bel", "J-Beauty PAYDAY MEGASALE🇯🇵Bell"]:
        assert iw.match_influencer(nm, _ROSTER) == "Bella"


def test_match_none_when_no_suffix():
    # 実ファイルにあった無后缀の 2 場
    assert iw.match_influencer("🇯🇵Japan Essentials Live Sale🇯🇵", _ROSTER) is None
    assert iw.match_influencer("", _ROSTER) is None


def test_match_longest_keyword_wins():
    # 'bella' は bella/bell/bel 全てに当たるが最長优先で Bella（同一人なので結果同じ·優先ロジック確認）
    assert iw.match_influencer("xxx_bella", _ROSTER) == "Bella"


# ---- incentive_peso 階梯 ----
def test_incentive_below_threshold():
    assert iw.incentive_peso(100000, _P) == pytest.approx(1000.0)  # 10万×1%


def test_incentive_at_threshold():
    assert iw.incentive_peso(200000, _P) == pytest.approx(2000.0)  # 20万×1%


def test_incentive_above_threshold():
    # 30万 = 20万×1% + 10万×1.5% = 2000 + 1500 = 3500
    assert iw.incentive_peso(300000, _P) == pytest.approx(3500.0)


# ---- compute_wage 全链路 ----
def test_compute_wage_full():
    # 时薪2 · 10h · 売上30万peso
    r = iw.compute_wage(total_hours=10.0, sales_peso=300000.0, hourly_wage=2.0, params=_P)
    assert r["salary_usd"] == 20.0                       # round(10×2)
    assert r["incentive_peso"] == pytest.approx(3500.0)
    assert r["incentive_usd"] == pytest.approx(3500.0 / 61.49)
    assert r["total_usd"] == pytest.approx(20.0 + 3500.0 / 61.49)
    assert r["total_fee_usd"] == pytest.approx(r["total_usd"] * 1.045)
    assert r["total_jpy"] == pytest.approx(r["total_usd"] * 155.0)
    assert r["total_fee_jpy"] == pytest.approx(r["total_fee_usd"] * 155.0)


def test_compute_wage_salary_rounded():
    # 1.4230h × 1.5 = 2.1345 → round 2
    r = iw.compute_wage(total_hours=1.4230, sales_peso=0.0, hourly_wage=1.5, params=_P)
    assert r["salary_usd"] == 2.0
    assert r["incentive_peso"] == 0.0


def test_compute_wage_zero_rate_safe():
    p = WageParams(php_usd=0.0)
    r = iw.compute_wage(total_hours=5.0, sales_peso=100000.0, hourly_wage=2.0, params=p)
    assert r["incentive_usd"] == 0.0  # 除零保护
