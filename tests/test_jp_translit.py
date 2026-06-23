"""shared/jp_translit 离线转写の純ロジックテスト（API/网络不要）.

词典命中 + 假名罗马字兜底 + 罗马字保留 を固定。質はドラフト前提·主要動作のみ担保。
"""
from shared.jp_translit import to_english_title as T


def test_loanword_hits_and_roman_kept():
    # 外来语命中英文 + 品牌/尺寸罗马字原样
    assert T("DHC リップクリーム シアーレッド 1.5g") == "DHC Lip Balm Sheer Red 1.5g"


def test_color_and_size_kept():
    assert T("シアーボルドー1.5g") == "Sheer Bordeaux 1.5g"


def test_brand_term_inside_kana_run():
    # 串中间的品牌（ビクトリノックス）应被词典抓到，不被整段罗马字吞掉
    out = T("マイファーストビクトリノックス Rabbit")
    assert "Victorinox" in out and out.endswith("Rabbit")


def test_kanji_term_and_particle_drop():
    out = T("キンモクセイの香り")
    assert out == "Osmanthus Scent"  # の 丢弃 · 香り→Scent · キンモクセイ→Osmanthus


def test_pure_ascii_unchanged():
    assert T("Lidora Plus") == "Lidora Plus"


def test_empty():
    assert T("") == "" and T(None) == ""


def test_household_terms():
    assert T("コンパクト電動ドライバー") == "Compact Electric Driver"


def test_unknown_kana_falls_back_romaji():
    # 词典未命中的假名 → 罗马字（不空、不杜撰英文）
    out = T("リフトミスト")
    assert "Mist" in out  # ミスト 命中
    assert out  # 非空
