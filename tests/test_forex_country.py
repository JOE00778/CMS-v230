"""国コード → 円レートの回帰テスト。

2026-07-29 発見: page14 が country(PH/VN…) を FX_TO_JPY(キーは通貨コード PHP/VND…)
で直接引いており、全件 miss → fillna(1.0) で「現地通貨 1 = 1 円」になっていた。
VND は本来 0.0055 なので 180 倍過大に計上されていた。二度と起こさないため固定する。
"""
from shared.forex import COUNTRY_TO_CURRENCY, FX_TO_JPY, country_to_jpy


def test_country_code_is_not_a_currency_key():
    """バグの本質: 国コードは FX_TO_JPY のキーではない。"""
    for cc in ("PH", "VN", "TW", "SG", "MY", "TH", "BR"):
        assert cc not in FX_TO_JPY, f"{cc} が通貨キーとして存在すると再発を検知できない"


def test_country_to_jpy_resolves_via_currency():
    assert country_to_jpy("PH") == FX_TO_JPY["PHP"] == 2.4
    assert country_to_jpy("VN") == FX_TO_JPY["VND"] == 0.0055
    assert country_to_jpy("KR") == FX_TO_JPY["KRW"] == 0.095
    assert country_to_jpy("ph") == 2.4          # 大文字小文字を問わない


def test_unknown_country_returns_none_not_one():
    """1.0 に落とすと『換算した』ように見えてしまうので None を返す。"""
    assert country_to_jpy("XX") is None
    assert country_to_jpy(None) is None
    assert country_to_jpy("") is None


def test_all_shopee_markets_are_mapped():
    """Shopee 出店 7 国 + Coupang(KR) が全て引けること。"""
    for cc in ("PH", "MY", "SG", "TH", "TW", "VN", "BR", "KR"):
        assert country_to_jpy(cc) is not None, cc
        assert COUNTRY_TO_CURRENCY[cc] in FX_TO_JPY
