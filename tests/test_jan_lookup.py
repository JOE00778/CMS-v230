"""JAN 外部查询的解析层单测(纯函数,不打网络)。"""
from __future__ import annotations

from shared.jan_lookup import (
    decode,
    lookup,
    parse_ingredient,
    parse_rakuten_title,
    parse_yahoo_name,
)

RAKUTEN_PAGE = """<html><head><title>【楽天市場】アネッサ パーフェクトUV スキンケアミルク NA(60ml)：楽天24</title></head>
<body><script>var x="成分】ダミー";</script>
<div class="spec"><p>【全成分】 ジメチコン、水、酸化亜鉛、エタノール、オクトクリレン、
サリチル酸エチルヘキシル、タルク、シリカ、グリセリン、塩化Na、チャ葉エキス ※パッケージ変更あり</p></div>
</body></html>"""


def test_parse_rakuten_title_strips_prefix_and_shop():
    assert parse_rakuten_title(RAKUTEN_PAGE) == "アネッサ パーフェクトUV スキンケアミルク NA(60ml)"


def test_parse_ingredient_extracts_and_cuts_promo_tail():
    ing = parse_ingredient(RAKUTEN_PAGE)
    assert ing.startswith("ジメチコン、水、酸化亜鉛")
    assert "チャ葉エキス" in ing
    assert "※" not in ing and "パッケージ変更" not in ing


def test_parse_ingredient_absent_returns_empty():
    assert parse_ingredient("<html><body>商品説明のみ</body></html>") == ""


def test_decode_prefers_readable_encoding():
    raw = "【全成分】水、グリセリン、エタノール、トコフェロール、香料".encode("euc-jp")
    assert "グリセリン" in decode(raw)


def test_parse_yahoo_name_skips_chrome_strings():
    page = ('<img alt="Yahoo!ショッピング"><img alt="ふるさと納税商品券増量CP">'
            '<img alt="【2025年モデル】ANESSA アネッサ パーフェクトUV スキンケアミルク NA">')
    assert parse_yahoo_name(page).startswith("【2025年モデル】ANESSA")


def test_parse_yahoo_name_none_found():
    assert parse_yahoo_name('<img alt="Yahoo!ショッピング">') == ""


def test_lookup_rejects_non_jan_without_network():
    assert lookup("アネッサ") == {}
    assert lookup("") == {}
    assert lookup("123") == {}
