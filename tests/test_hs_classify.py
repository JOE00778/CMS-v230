"""shared.hs_classify の単測（純関数・ネットワーク無し）。

源 database/data_warehouse/banma_api/classify_by_fact.py と同じ答えを出すことも確かめる
（database 仓が隣に無い環境＝元川コンテナでは比較テストだけ skip）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from shared.hs_classify import (
    MAX_NAME_EN,
    classify,
    classify_customs,
    extract_spec,
    normalize_maker,
    resolve_brand,
)

# database 側 tests/test_classify_by_fact.py の CASES をそのまま移植（日本語名, brand, 期待品類）
DB_CASES = [
    ("SKATER アナト雪 園児用歯ブラシ3Pキャップ付", "skater", "歯ブラシ"),
    ("DHC エクストラモイスチュアリップクリーム", "dhc", "リップ"),
    ("熊野油脂 ファーマアクト 薬用石けん 100g ×3個パック", "", "固形石けん"),
    ("THERMOS JDG-352C 真空断熱ケータイマグ", "thermos", "魔法瓶・真空容器"),
    ("ROSYROSA マルチファンデブラシ", "rosyrosa", "化粧小物・ブラシ"),
    ("SUNSTAR オーラツーミーマウスウォッシュ", "sunstar", "オーラルケア"),
    ("CANMAKE クリームチーク 25", "canmake", "フェイスパウダー・チーク"),
    ("mandom Bifesta セラムクレンジングオイル 160ml", "mandom", "洗顔・クレンジング"),
    ("ROHTO メディクイックH 頭皮しっとりローション", "rohto", "化粧水"),
    ("MEIKO ナチュラクター マジカルリップ No1", "meiko", "リップ"),
    ("ORBIS スカルプ リファイニング コンディショナー", "orbis", "コンディショナー・トリートメント"),
    ("S460BK SHUPATTO ｺﾝﾊﾟｸﾄﾊﾞｯｸﾞ DROP", "shupatto", "バッグ・ポーチ"),
    ("ROHTO ギュットコルセットヘアマスク", "rohto", "コンディショナー・トリートメント"),
    ("SHISEIDO INTEGRATE プロフィニッシュ コンパクトケース", "integrate", "化粧小物・ブラシ"),
    ("熊野油脂 麗白 ハトムギ ライトアップ UVエッセンス", "kumano-yushi", "日焼け止め"),
    ("Dove モイスチヤーケアコンデイシヨナーポンプ", "dove", "コンディショナー・トリートメント"),
    ("Dove モイスチヤーケア シヤンプー ポンプ", "dove", "シャンプー"),
    ("THERMOS RFF-0041 保冷ﾗﾝﾁﾊﾞｯｸﾞ", "thermos", "バッグ・ポーチ"),
    ("Dreams SMISKI Hugging ブラシダキツキスキー", "smiski", "玩具"),
    ("HOYU シエロ ワンデー白髪かくし ライトブラウン", "hoyu", "白髪染め・ヘアカラー"),
    ("柳屋 ヘアトニック 240ml", "yanagiya", "シャンプー"),
    ("KAI 貝印 関孫六 ツメキリtype101L", "kai", "化粧小物・ブラシ"),
    ("ビタット ミニサイズ ブラック", "bitatto", "日用雑貨"),
    ("Bitatto japanオクチミックスベリー", "bitatto", "オーラルケア"),
    ("KAO メンズビオレ デオドラントBW せっけんの香り", "biore", "石けん・洗浄料"),
    ("小林製薬 ブレスケア ストロングミント 50粒", "kobayashi", "菓子"),
    ("MAYBE rice mask", "maple-international", "フェイスマスク"),
]

# 比較用の追加例（期待値は持たない＝源と同じ答えかだけ見る）。maker 経路・none も混ぜる
EXTRA = [
    ("いち髪 流し前髪キープコーム", "kracie", "クラシエ"),
    ("いち髪 なめらかスムースケア シャンプー 480ml", "kracie", "クラシエホームプロダクツ"),
    ("THERMOS 保冷ランチバッグ RFF-0041", "thermos", "サーモス"),
    ("ﾋﾞｵﾚ ﾒｲｸ落とし ﾊﾟｰﾌｪｸﾄｵｲﾙ 230ml", "kao", "花王"),
    ("謎の商品 X-100", "", ""),
    ("ペリカン石鹸 恋するおしり 80g", "pelican-soap", "ペリカン石鹸"),
    ("無印 ABC", "", "株式会社サロニア"),
    ("ルルルン フェイスマスク 7枚", "lululun", "グライド・エンタープライズ"),
]


def test_db_regression_cases():
    bad = [(t, want, classify(t, b, "")[0]) for t, b, want in DB_CASES if classify(t, b, "")[0] != want]
    assert not bad


def _load_db_module():
    src = Path(__file__).resolve().parents[2] / "database/data_warehouse/banma_api/classify_by_fact.py"
    if not src.exists():
        pytest.skip(f"database 仓が無い: {src}")
    spec = importlib.util.spec_from_file_location("_db_classify_by_fact", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_same_answer_as_database_classify():
    db = _load_db_module()
    inputs = [(t, b, "") for t, b, _ in DB_CASES] + EXTRA
    assert len(inputs) >= 30
    diff = [(i, db.classify(*i), classify(*i)) for i in inputs if db.classify(*i) != classify(*i)]
    assert not diff
    for m in ["Kobayashi Pharma", "興和", "クラシエ", "サーモス株式会社", "", "P&G"]:
        assert db.normalize_maker(m) == normalize_maker(m)


def test_resolve_brand_falls_back_to_prefix_when_alias_misses():
    pm = {"4562344": "サーモス"}
    # 源の版は "サーモス株式会社" を返してプレフィックスに落ちない（仕様書 §6 のバグ）
    assert resolve_brand("", "サーモス株式会社", "4562344000000", pm) == "サーモス"
    assert resolve_brand("", "小林製薬", "4987072000000", {"4987072": "花王"}) == "kobayashi"
    assert resolve_brand("", "", "4901301999999", {"4901301": "kao"}) == "kao"
    assert resolve_brand("", "", "4901301999999", {"4901301": "花王株式会社"}) == "kao"
    assert resolve_brand("", "謎メーカー", "1234567000000", {}) == "謎メーカー"
    assert resolve_brand("", "", "", None) == ""


def test_jp_name_only_new_item():
    c = classify_customs("いち髪 なめらかスムースケア シャンプー 480ml", "クラシエ", "4901417000000")
    assert (c.category, c.hs, c.basis) == ("シャンプー", "330510", "jp-name")
    assert c.name_en == "Shampoo, hair washing preparation, 480ml"


@pytest.mark.xfail(strict=True, reason="規則の既知誤判定: 「コーム」でヘア小物・機器(961511)に落ちる。"
                   "実物はコーム一体型容器のスタイリング剤 9mL（クラシエ 2025-08 発表）。直すなら database 側と両方")
def test_ichikami_keep_comb_is_styling():
    c = classify_customs("いち髪 流し前髪キープコーム", "クラシエ", "4901417627490")
    assert c.category == "スタイリング剤"


def test_ichikami_keep_comb_current_answer_is_hair_related():
    c = classify_customs("いち髪 流し前髪キープコーム", "クラシエ", "4901417627490")
    assert c.category == "ヘア小物・機器" and c.basis == "jp-name"


def test_thermos_lunch_bag_is_not_vacuum_bottle():
    c = classify_customs("THERMOS 保冷ランチバッグ RFF-0041", "THERMOS", "4562344000000")
    assert c.category == "バッグ・ポーチ" and c.hs == "420292"
    assert classify_customs("THERMOS 真空断熱ケータイマグ 0.5L", "THERMOS", "x").hs == "961700"


def test_thermos_via_prefix_when_maker_is_japanese():
    # maker が alias 不命中でもプレフィックス経由で thermos 独占が効く
    c = classify_customs("JNL-504 ブラック", "サーモス株式会社", "4562344000000",
                         prefix_map={"4562344": "THERMOS"})
    assert (c.category, c.basis) == ("魔法瓶・真空容器", "brand-x")


def test_hankaku_kana_normalized():
    c = classify_customs("ｼｬﾝﾌﾟｰ ﾎﾞﾄﾙ 500ml", "", "")
    assert c.category == "シャンプー" and c.name_en.endswith(", 500ml")


def test_unclassifiable_is_empty():
    c = classify_customs("謎の商品 X-100", "", "1234567890123")
    assert (c.hs, c.name_en, c.category, c.basis) == (None, None, None, "none")


def test_extract_spec():
    assert extract_spec("セラムクレンジングオイル 160ml") == "160ml"
    assert extract_spec("ミルク 60mL") == "60ml"
    assert extract_spec("詰替 １．５Ｌ") == "1.5L"      # 全角
    assert extract_spec("石けん 100gパック") == "100g"  # 単位の直後が和文
    assert extract_spec("ソックス 4足 12枚") == ""      # 個数は書かない
    assert extract_spec("100 good") == ""


def test_name_en_max_76_keeps_spec():
    # 最長の品類句（65 字）+ 規格で 76 を超える → 品類句を単語境界で切り、規格は残す
    c = classify_customs("ROSYROSA マルチファンデブラシ 12345.678ml", "", "")
    assert c.category == "化粧小物・ブラシ"
    assert len(c.name_en) <= MAX_NAME_EN
    assert "cosmetics" not in c.name_en  # 実際に切れている
    assert c.name_en.endswith(", 12345.678ml")
    head = c.name_en[: -len(", 12345.678ml")]
    assert "Powder puff / make-up brush / beauty tool, for applying cosmetics".startswith(head)
    assert not head.endswith((" ", ",", "/"))
