"""加国登録照会の純関数部分(ネットワークは叩かない)。"""
from __future__ import annotations

from shared.ca_registry import brand_query, dpd_by_brand


def test_brand_query_prefers_maker_english_word():
    assert brand_query("アネッサ パーフェクトUV", "Shiseido") == "Shiseido"


def test_brand_query_falls_back_to_name_and_skips_stopwords():
    assert brand_query("The New ANESSA Perfect UV Milk", "") == "ANESSA"


def test_brand_query_empty_when_no_ascii_brand():
    assert brand_query("アネッサ パーフェクトUV", "資生堂") == ""


def test_dpd_by_brand_rejects_too_short_without_network():
    assert dpd_by_brand("") is None
    assert dpd_by_brand("ab") is None


def test_dpd_by_brand_distinguishes_failure_from_empty(monkeypatch):
    """None=照会失敗 / [] =未登録。混同すると「未登録」と誤読させる。"""
    from shared import ca_registry
    monkeypatch.setattr(ca_registry.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("net")))
    assert ca_registry.dpd_by_brand("ANESSA") is None
