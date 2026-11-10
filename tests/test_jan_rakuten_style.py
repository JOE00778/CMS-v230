"""楽天の出品名 → jancode 形式（Boss 2026-11-10）。サンプルは 2026-11-10 楽天 API 実取得の出品名そのまま。"""
from shared.jan_web import jancode_style_name, pick_jancode_style

KATAOKA = ["【24個】 片岡物産 辻利 さらっととける抹茶 30g×24個 【北海道・沖縄・離島配送不可】",
           "【マラソン限定クーポン配布中】片岡物産 辻利 さらっととける抹茶 30g", "辻利 さらっととける抹茶 (30g入)",
           "エントリーしてポイント5倍！11月11日1時59分まで☆辻利 さらっととける抹茶 30g×24袋　片岡物産 抹茶 粉末 Matcha　green Tea 【送料無料(一部地域を除く)】",
           "片岡物産 辻利 さらっととける抹茶 30g/パック 1セット(3パック)(4901305411132 x3) 取り寄せ商品",
           "■ポスト投函■辻利 さらっととける抹茶 30g【2個セット】"]
ROSSI = ["ロッシモイストエイド 馬油 スキンクリーム 220g【2～6個】[4936201110209] 馬油配合 肌 馬油クリーム 尿素 顔",
         "ロッシ モイストエイド 馬油スキンクリーム 220g [4936201110209] 馬油配合 肌 馬油クリーム 尿素",
         "ロッシモイストエイド 馬油スキンクリーム 220g　4936201110209",
         "【公式】ロッシモイストエイド　馬油スキンクリーム 220g 保湿 ひび割れ カサカサ 馬油",
         "コスメテックスローランド ロッシ モイストエイド　馬油スキンクリーム",
         "【公式限定3個セット/送料無料】ロッシモイストエイド　馬油スキンクリーム 220g 保湿 ひび割れ カサカサ 馬油 おすすめ",
         "コスメテックスローランド ロッシ モイストエイド 馬油スキンクリーム 220g",
         "4936201110209 ロッシ モイストエイド 馬油スキンクリーム 220g【キャンセル不可】"]
TAMIYA = ["GP.392 マスダンパーセット [15392](JAN：4950344153923)", "タミヤ GP.392 マスダンパーセット【15392】",
          "タミヤ ミニ四駆グレードアップパーツ No．392 マスダンパーセット TGP15392マスダンパ-セツト [TGP392マスダンパ]",
          "ミニ四駆 マスダンパーセット【タミヤ ミニ四駆用パーツ GP.392 ITEM15392】",
          "タミヤ ミニ四駆グレードアップパーツ No.392 マスダンパーセット 15392",
          "【メール便発送可】ミニ四駆 GP.392　マスダンパーセット【新品】 グレードアップパーツ 改造",
          "【タミヤ TAMIYA】タミヤ 15392 GP.392 マスダンパーセット",
          "ミニ四駆 GP.392　マスダンパーセット【新品】 グレードアップパーツ 改造"]
HIRITU = ["hiritu ヒリツ バランスリペアシャンプー オーロラ 410mL ヘアケア ダメージ補修 たんぱく質 水分 油分 ダメージケア[ギフトラッピング対応]",
          "[セット]hiritu ヒリツ バランスリペアシャンプー ヘアトリートメント オーロラ 410mL コンディショナー",
          "hiritu ヒリツ バランスリペア シャンプー オーロラ 410ml [1234]",
          "hiritu ヒリツ バランスリペアシャンプー オーロラ 410mL ヘアケア ダメージ補修",
          "hiritu ヒリツ バランスリペアシャンプー オーロラ 410mL ヘアケア"]
GATSBY = ["ギャツビー ムービングラバー エクストリームマット(80g)【ギャツビー(GATSBY)】",
          "ギャツビー ムービングラバー エクストリームマット(80g)【4902806137293】",
          "マンダム ギャツビー ムービングラバー エクストリームマット 80g",
          "マンダム ギャツビー ムービングラバー エクストリームマット 80g",
          "【お買い物マラソン特別価格】11/11 1:59まで！ ギャツビー ムービングラバー エクストリームマット 80gお一人様3点まで",
          "6〜10個セット まとめ買い ギャツビー ムービングラバー エクストリームマット 80g",
          "ギャツビー ムービングラバー エクストリームマット 80gお一人様5点まで"]


def test_real_rakuten_titles_become_jancode_style():
    assert pick_jancode_style(KATAOKA) == "片岡物産 辻利 さらっととける抹茶 30g"          # = jancode
    assert pick_jancode_style(ROSSI) == "ロッシ モイストエイド 馬油スキンクリーム 220g"   # = jancode
    assert pick_jancode_style(TAMIYA) == "タミヤ GP.392 マスダンパーセット"
    assert pick_jancode_style(HIRITU) == "hiritu ヒリツ バランスリペアシャンプー オーロラ 410ml"
    assert pick_jancode_style(GATSBY) == "ギャツビー ムービングラバー エクストリームマット 80g"


def test_sets_and_empty_are_dropped():
    assert jancode_style_name("片岡物産 辻利 さらっととける抹茶 30g×24個") == ""
    assert jancode_style_name("■ポスト投函■辻利 さらっととける抹茶 30g【2個セット】") == ""
    assert pick_jancode_style(["", "6〜10個セット まとめ買い X 80g"]) == ""
    assert jancode_style_name("タミヤ GP.392 マスダンパーセット") == "タミヤ GP.392 マスダンパーセット"   # 商品名のセットは残す
    assert jancode_style_name("8×4メン デオドラントパテ(25g)【8x4 MEN(エイトフォー メン)】") == "8×4メン デオドラントパテ 25g"
    assert jancode_style_name("片岡物産 辻利 30g/パック 1セット(3パック)(4901305411132 x3) 取り寄せ商品") == ""


def test_fetcher_stops_jancode_on_403_and_falls_back_to_rakuten(monkeypatch):
    from shared import jan_web as J
    calls = []

    def jc(jan):
        calls.append(jan)
        return J.JanMeta(jan, "", "", "", "error", "http=403 https://www.jancode.xyz/x/")
    monkeypatch.setattr(J, "fetch_meta", jc)
    monkeypatch.setattr(J, "rakuten_meta", lambda jan: J.JanMeta(jan, "楽天名 30g", "", "", "ok", source="rakuten"))
    f = J.make_meta_fetcher(limit=40)
    out = [f(j) for j in ("1", "2", "3")]
    assert calls == ["1"] and f.state["blocked"]                      # 403 の後は jancode を叩かない
    assert all(m.source == "rakuten" and m.name == "楽天名 30g" for m in out)


def test_fetcher_caps_jancode_requests(monkeypatch):
    from shared import jan_web as J
    calls = []
    monkeypatch.setattr(J, "fetch_meta", lambda jan: calls.append(jan) or J.JanMeta(jan, "名", "社", "", "ok"))
    monkeypatch.setattr(J, "rakuten_meta", lambda jan: J.JanMeta(jan, "", "", "", "not_found", source="rakuten"))
    monkeypatch.setattr(J, "yahoo_meta", lambda jan: J.JanMeta(jan, "", "", "", "not_found", source="yahoo"))
    monkeypatch.setattr(J, "INTERVAL", 0)
    f = J.make_meta_fetcher(limit=2)
    out = [f(j) for j in ("1", "2", "3")]
    assert calls == ["1", "2"] and [m.status for m in out] == ["ok", "ok", "error"]
    assert "上限" in out[2].error


def test_enrich_marks_rakuten_name_for_review():
    from shared import jan_web as J
    from shared import item_register as R
    from tests.test_item_register import CH, _cus, _row, _weight
    nst, extra, issues, st = R.enrich(
        [_row("4901234567894")], choices=CH, prefix_makers={}, classify_customs=_cus, fetch_weight=_weight,
        fetch_meta=lambda j: J.JanMeta(j, "楽天名 30g", "", "", "ok", source="rakuten"), want_weight=False, interval=0)
    assert nst[0]["アイテム名"] == "楽天名 30g" and st["name_rakuten"] == 1 and st["jancode_ok"] == 0
    assert "アイテム名(楽天・要確認)" in extra["4901234567894"]["auto"]
    assert any("楽天" in i.message and i.level == "warn" for i in issues)


def test_shop_brand_unified_to_japanese_per_jan_prefix():
    """同じメーカー（JAN 先頭 7 桁）の楽天名は先頭ブランドを日本語 1 つに揃える（Boss 2026-11-10）。2026-11-10 実データ。"""
    from shared import item_register as R
    names = ["TAKARATOMY タカラトミー トミカ C-04 メーター (スタンダードタイプ)", "カーズ トミカ C-11 チック・ヒックス",
             "C-09 シェリフ (スタンダードタイプ)", "タカラトミー カーズ トミカ C-15 クルーズ・ラミレス(道路パトロールカータイプ)",
             "タカラトミー トミカ C-17 フランチェスコ・ベルヌーイ(スタンダードタイプ)"]
    rows = [{"型番": f"490481000{i:04d}", "JANコード": f"490481000{i:04d}", "アイテム名": n} for i, n in enumerate(names)]
    rows.append({"型番": "4904810999999", "JANコード": "4904810999999", "アイテム名": "TAKARATOMY ほげ"})   # jancode 由来
    ex = {r["型番"]: {"auto": ["アイテム名(楽天・要確認)"]} for r in rows[:-1]}
    ex["4904810999999"] = {"auto": ["アイテム名(jancode)"]}
    issues = R.unify_shop_brands(rows, ex)
    assert [r["アイテム名"] for r in rows] == [
        "タカラトミー トミカ C-04 メーター (スタンダードタイプ)", "タカラトミー カーズ トミカ C-11 チック・ヒックス",
        "タカラトミー C-09 シェリフ (スタンダードタイプ)", "タカラトミー カーズ トミカ C-15 クルーズ・ラミレス(道路パトロールカータイプ)",
        "タカラトミー トミカ C-17 フランチェスコ・ベルヌーイ(スタンダードタイプ)", "TAKARATOMY ほげ"]    # jancode 名は触らない
    assert all(r.get("メーカー名") == "タカラトミー" for r in rows[:-1])
    assert len(issues) == 3


def test_shop_brand_uses_known_maker_without_corp_suffix():
    from shared import item_register as R
    rows = [{"型番": "4901305411224", "JANコード": "4901305411224", "アイテム名": "辻利 抹茶ミルク お濃い茶仕立て",
             "メーカー名": "片岡物産株式会社"}]
    R.unify_shop_brands(rows, {"4901305411224": {"auto": ["アイテム名(楽天・要確認)"]}})
    assert rows[0]["アイテム名"] == "片岡物産 辻利 抹茶ミルク お濃い茶仕立て"
