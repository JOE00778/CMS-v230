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


SUNDRUG_PAGE = """<html><head><title>【楽天市場】TSUBAKI シャンプー(400ml)：サンドラッグ</title></head>
<body><p>お問い合わせの際に必要な場合があります。 成分／分量 水、ラウレス硫酸Na、コカミドプロピルベタイン、
ジステアリン酸グリコール、ポリクオタニウム-10、香料 ※パッケージ変更</p></body></html>"""

MATSUKIYO_PAGE = """<html><head><title>【楽天市場】キャンメイク クイックラッシュカーラー：マツモトキヨシ</title></head>
<body><p>原料・成分等 【成分】 シクロペンタシロキサン、イソドデカン、パラフィン、ミツロウ、シリカ</p></body></html>"""

PROSE_PAGE = """<html><body><p>有効成分について 詳しくはメーカーにお問い合わせください。
この商品は日本国内で製造されており品質管理を徹底しています。</p></body></html>"""


def test_parse_ingredient_handles_sundrug_slash_label():
    ing = parse_ingredient(SUNDRUG_PAGE)
    assert ing.startswith("水、ラウレス硫酸Na")
    assert "※" not in ing


def test_parse_ingredient_handles_matsukiyo_label():
    assert parse_ingredient(MATSUKIYO_PAGE).startswith("シクロペンタシロキサン")


def test_parse_ingredient_rejects_prose_without_separators():
    """「有効成分について…」のような散文を成分表と誤認しない。"""
    assert parse_ingredient(PROSE_PAGE) == ""


def test_clean_shop_name_strips_promo_keeps_quasi_drug():
    from shared.jan_lookup import clean_shop_name
    assert clean_shop_name("●国内正規品 コスメデコルテ アイグロウジェム") == "国内正規品 コスメデコルテ アイグロウジェム"
    assert clean_shop_name("【送料無料】【ポイント10倍】TSUBAKI シャンプー") == "TSUBAKI シャンプー"
    # 医薬部外品は判定に必要 → 落とさない
    assert clean_shop_name("【医薬部外品】ロート製薬 OXY").startswith("【医薬部外品】")


def test_rakuten_api_returns_empty_without_credentials(monkeypatch):
    from shared import jan_lookup
    monkeypatch.delenv("RAKUTEN_APPLICATION_ID", raising=False)
    monkeypatch.delenv("RAKUTEN_ACCESS_KEY", raising=False)
    assert jan_lookup.rakuten_api("4909978147105") == {}


FOOD_PAGE = """<html><body><p>原材料名 大麦若葉粉末、還元麦芽糖水飴、乳酸菌(殺菌)、抹茶、
デキストリン、香料 内容量 150g</p></body></html>"""


def test_parse_ingredient_handles_food_raw_material_label():
    """全品類判定なので食品の「原材料名」も成分表として扱う。"""
    ing = parse_ingredient(FOOD_PAGE)
    assert ing.startswith("大麦若葉粉末")
    assert "内容量" not in ing
