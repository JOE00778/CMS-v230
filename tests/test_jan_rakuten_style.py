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


def test_brand_not_borrowed_across_product_lines_and_latin_dupe_dropped():
    """2026-11-11 事故: メーカー不明の 4904810（タカラトミー）で多数派の「ポケットモンスター」がカーズ・KOKUYO に付いた。
    メーカー不明なら何も付けない。先頭の「英字 片仮名」重複は英字だけ外す（出典に関係なく）。"""
    from shared import item_register as R
    names = ["ポケットモンスター トミカ No.24 キャンピングカー 佐賀", "ポケットモンスター トミカ No.136 古河ロックドリル",
             "カーズ トミカ C-22 メーター(アイスレーシングタイプ)", "TAKARATOMY タカラトミー トミカ C-04 メーター(スタンダードタイプ)",
             "KOKUYO コクヨ キャンパスルーズリーフ さらさら書ける 無地 B5 50枚 ノ-837W", "TAKARATOMY ほげ"]
    rows = [{"型番": f"490481000{i:04d}", "JANコード": f"490481000{i:04d}", "アイテム名": n} for i, n in enumerate(names)]
    ex = {r["型番"]: {"auto": ["アイテム名(楽天・要確認)"]} for r in rows}
    ex[rows[4]["型番"]] = {"auto": ["アイテム名(jancode)"]}
    issues = R.unify_shop_brands(rows, ex)
    assert [r["アイテム名"] for r in rows] == [
        names[0], names[1], names[2], "タカラトミー トミカ C-04 メーター(スタンダードタイプ)",
        "コクヨ キャンパスルーズリーフ さらさら書ける 無地 B5 50枚 ノ-837W", "TAKARATOMY ほげ"]
    assert not any(r.get("メーカー名") for r in rows) and len(issues) == 2


def test_rakuten_c30_keeps_variant_in_parens():
    """2026-11-11 実データ（4904810227977）: C−30（U+2212）と C-30、「マックィーン (シェリフタイプ)」と詰め書きで票が割れ、
    括弧なしの 1 店の名前が選ばれていた。"""
    titles = ["【お取り寄せ】タカラトミー カーズ トミカ C-30 ライトニング・マックィーン",
              "タカラトミー カーズ トミカ C−30 ライトニング・マックィーン（シェリフタイプ）",
              "カーズ トミカ C-30 ライトニング・マックィーン(シェリフタイプ) | おもちゃ 男の子 ミニカー 3歳",
              "ディズニー カーズ トミカ C-30 ライトニング・マックィーン（シェリフタイプ） ミニカー 車 おもちゃ",
              "トミカ カーズ C-30 ライトニング・マックィーン (シェリフタイプ)",
              "C-30 ライトニング・マックィーン (シェリフタイプ) カーズトミカ",
              "トミカ C-30 ライトニング・マックィーン （シェリフタイプ）【新品】 カーズ ミニカー TOMICA 【宅配便のみ】",
              "●トミカ カーズトミカ C-30 ライトニング・マックィーン（シェリフタイプ）",
              "タカラトミー ディズニー カーズ トミカ C-30 ライトニング・マックィーン (シェリフタイプ) ミニカー おもちゃ 3歳以上",
              "タカラトミー トミカ カーズ C-30 マックィーン(シェリフタイプ) 227977",
              "タカラトミー トミカ カーズ C－30 マックィーン（シェリフタイプ） 227977",
              "トミカ カーズ C－30 マックィーン（シェリフタイプ） 227977 全1色 全1サイズ",
              "【送料無料】c−01 ライトニング・マックィーン（スタンダードタイプ） 色：マルチカラー、サイズ：1個 (x 1)",
              "タカラトミー トミカ カーズ C−30 マックィーン（シェリフタイプ） 227977 送料無料",
              *["タカラトミー カーズ トミカ C−30 ライトニング・マックィーン（シェリフタイプ）"] * 4,
              "タカラトミー カーズトミカ C-30 ライトニング・マックィーン(シェリフタイプ) 《納期約1－2週間》",
              "★365日毎日出荷OK★カーズ トミカ／C-30 ライトニング・マックィーン(シェリフタイプ)／トミカ／タカラトミー"]
    assert pick_jancode_style(titles) == "タカラトミー カーズ トミカ C-30 ライトニング・マックィーン(シェリフタイプ)"


def test_shop_brand_uses_known_maker_without_corp_suffix():
    from shared import item_register as R
    rows = [{"型番": "4901305411224", "JANコード": "4901305411224", "アイテム名": "辻利 抹茶ミルク お濃い茶仕立て",
             "メーカー名": "片岡物産株式会社"}]
    R.unify_shop_brands(rows, {"4901305411224": {"auto": ["アイテム名(楽天・要確認)"]}})
    assert rows[0]["アイテム名"] == "片岡物産 辻利 抹茶ミルク お濃い茶仕立て"


def test_rakuten_repeated_words_and_brand_first_20261111():
    """2026-11-11 実データ（楽天 30 件ずつ）: 末尾の「トミカ」重複・「ルーズリーフ B5 ノート」の羅列を選ばない。
    同点ならメーカー先頭・核の語が多い名前（短すぎて規格が抜けた名前は選ばない）。"""
    import json
    import pathlib
    data = json.loads((pathlib.Path(__file__).parent / "fixtures" / "rakuten_titles_20261111.json").read_text("utf-8"))
    assert {j: pick_jancode_style(t) for j, t in data.items()} == {
        "4901480233048": "コクヨ キャンパスルーズリーフ さらさら書ける B罫ドット B5 100枚 ノ-836BTN",
        "4904810227984": "タカラトミー カーズ トミカ C-24 ライトニング・マックィーン(アイスレーシングタイプ)",
        "4904810166412": "タカラトミー カーズ トミカ C-26 ラモーン(スタンダードタイプ)"}


def test_latin_only_maker_not_prepended_20261112():
    """NST 表記の英字メーカー（TAKARA TOMY / LION）は名前の先頭に付けない＝日本語で統一（「LION ライオン クリニカ…」の事故）。"""
    from shared import item_register as R
    rows = [{"型番": "4903301182900", "JANコード": "4903301182900", "アイテム名": "ライオン クリニカ ハミガキ マイルドミント 30g",
             "メーカー名": "LION"},
            {"型番": "4904810990109", "JANコード": "4904810990109", "アイテム名": "タカラトミー ポケットモンスター モンコレ",
             "メーカー名": "TAKARA TOMY"}]
    before = [r["アイテム名"] for r in rows]
    R.unify_shop_brands(rows, {r["型番"]: {"auto": ["アイテム名(楽天・要確認)"]} for r in rows})
    assert [r["アイテム名"] for r in rows] == before
