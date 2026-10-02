"""shared/jan_web の単体テスト。実 HTML を最小化したフィクスチャ + URL ルーティングの偽 opener(網に出ない)。"""
from __future__ import annotations

import io
import urllib.error
from pathlib import Path

import pytest

from shared import jan_web as jw

FX = Path(__file__).parent / "fixtures" / "jan_web"


def fx(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(jw.time, "sleep", slept.append)
    return slept


def make_opener(routes: dict):
    """routes: URL の部分文字列 -> HTML 文字列 | int(HTTP エラーコード) | Exception。未登録は 404。"""
    calls = []

    def opener(req, timeout=None):
        url = req.full_url
        calls.append(url)
        for key, val in routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                if isinstance(val, int):
                    raise urllib.error.HTTPError(url, val, "x", {}, None)
                return io.BytesIO(val.encode("utf-8"))
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)

    opener.calls = calls
    return opener


# ---------- jancode ----------

def test_parse_jancode_fields_and_genre_join():
    m = jw.parse_jancode("4510085137974", fx("jancode_4510085137974.html"))
    assert m.status == "ok"
    assert m.name == "合皮 Wファスナーポーチ ベーシックカラー 11×16cm 100均一 100均"
    assert m.maker == "株式会社シナップス・ジャパン"      # 会社名カナ を拾わない
    assert m.genre == "バッグ・小物・ブランド雑貨 > バッグ > レディースバッグ > アクセサリーポーチ"


def test_fetch_meta_not_found_vs_error():
    op = make_opener({"jancode.xyz/1111111111116": fx("jancode_no_detail.html"),
                      "jancode.xyz/2222222222222": 403,
                      "jancode.xyz/3333333333333": TimeoutError("t/o")})
    assert jw.fetch_meta("1111111111116", opener=op).status == "not_found"
    assert jw.fetch_meta("4444444444444", opener=op).status == "not_found"   # 404
    e403 = jw.fetch_meta("2222222222222", opener=op)
    assert e403.status == "error" and "403" in e403.error                    # 403 を「該当なし」にしない
    assert jw.fetch_meta("3333333333333", opener=op).status == "error"


def test_fetch_meta_ok_and_url():
    op = make_opener({"jancode.xyz/4510085137974/": fx("jancode_4510085137974.html")})
    assert jw.fetch_meta("4510085137974", opener=op).maker == "株式会社シナップス・ジャパン"
    assert op.calls == ["https://www.jancode.xyz/4510085137974/"]


# ---------- 個装パーサ ----------

def test_parse_kosou_wdh_mm_converts_and_ignores_gaisou():
    assert jw.parse_kosou(fx("netsea_detail_wdh_mm.html")) == (
        422.0, {"width": 14.0, "depth": 5.8, "height": 22.0}, "")


def test_parse_kosou_tate_okuyuki_yoko():
    assert jw.parse_kosou(fx("netsea_detail_tate_cm.html")) == (
        195.0, {"height": 13.5, "depth": 4.5, "width": 7.6}, "")


def test_parse_kosou_excludes_body_net_weight():
    w, d, _ = jw.parse_kosou(fx("netsea_detail_body_weight.html"))
    assert w is None                                   # 「重量（約）70g」は本体正味
    assert d == {"width": 3.5, "depth": 32.0, "height": 15.0}


@pytest.mark.parametrize("name", ["netsea_detail_case_only.html", "sd_detail_case_only.html"])
def test_parse_kosou_excludes_case_values(name):
    assert jw.parse_kosou(fx(name)) == (None, {}, "")


def test_parse_kosou_sd_weight_only():
    assert jw.parse_kosou(fx("sd_detail_kosou_weight.html")) == (200.0, {}, "")


def test_parse_kosou_unlabeled_axes_and_kg():
    w, d, note = jw.parse_kosou("<p>個装サイズ：10×20×30mm 個装重量：1.2kg</p>")
    assert w == 1200.0
    assert d == {"height": 1.0, "width": 2.0, "depth": 3.0} and "軸推定" in note


def test_search_link_parsers_keep_order_and_dedupe():
    assert jw.parse_netsea_links(fx("netsea_search_4971671192232.html")) == [
        "https://www.netsea.jp/shop/84918/85706994",
        "https://www.netsea.jp/shop/3018/N00422903",
        "https://www.netsea.jp/shop/833120/s6s0345068"]
    assert jw.parse_sd_links(fx("sd_search_4971671192232.html")) == [
        "https://www.superdelivery.com/p/r/pd_p/11889369/",
        "https://www.superdelivery.com/p/r/pd_p/12285405/"]


# ---------- fetch_weight 優先順位 ----------

JAN = "4971671192232"


def test_fetch_weight_single_netsea_shop_is_corroborated_by_sd_then_kept(no_sleep):
    """NETSEA で重量を書いた店が 1 店だけ → SD にも照合に行く。SD に無ければその 1 店を「1店舗のみ」で返す。"""
    op = make_opener({"netsea.jp/search": fx("netsea_search_4971671192232.html"),
                      "netsea.jp/shop/84918/": fx("netsea_detail_case_only.html").replace("4901525010283", JAN),
                      "netsea.jp/shop/3018/": fx("netsea_detail_tate_cm.html")})
    hit = jw.fetch_weight(JAN, opener=op)
    assert (hit.source, hit.url, hit.weight_g) == ("netsea", "https://www.netsea.jp/shop/3018/N00422903", 195.0)
    assert (hit.height_cm, hit.width_cm, hit.depth_cm) == (13.5, 7.6, 4.5)
    assert any("superdelivery" in u for u in op.calls)
    assert "1店舗のみ" in hit.note
    assert no_sleep and all(s >= 0.8 for s in no_sleep)


# 実例(2026-10-03): 4971671192232 は NETSEA 先頭店が「個装 19g」と桁落ち、JD 実測 196g。
_TYPO_19G = fx("netsea_detail_tate_cm.html").replace("重量195", "重量19")


def test_fetch_weight_typo_shop_is_outvoted():
    op = make_opener({"netsea.jp/search": fx("netsea_search_4971671192232.html"),
                      "netsea.jp/shop/84918/": _TYPO_19G,
                      "netsea.jp/shop/3018/": fx("netsea_detail_tate_cm.html"),
                      "superdelivery.com/p/do/psl": fx("sd_search_4971671192232.html"),
                      "pd_p/11889369": fx("sd_detail_kosou_weight.html")})
    hit = jw.fetch_weight(JAN, opener=op)
    assert hit.weight_g in (195.0, 200.0)
    assert "外れ値を除外" in hit.note and "19g" in hit.note


def test_fetch_weight_two_disagreeing_shops_leave_weight_blank():
    """2 店しか無く値が割れる → どちらも選ばず重量は空(寸法は残す)。推測で選ばない。"""
    op = make_opener({"netsea.jp/search": fx("netsea_search_4971671192232.html"),
                      "netsea.jp/shop/84918/": _TYPO_19G,
                      "netsea.jp/shop/3018/": fx("netsea_detail_tate_cm.html")})
    hit = jw.fetch_weight(JAN, opener=op)
    assert hit.weight_g is None and hit.height_cm == 13.5
    assert "一致せず" in hit.note


def test_fetch_weight_two_agreeing_netsea_shops_skip_sd():
    op = make_opener({"netsea.jp/search": fx("netsea_search_4971671192232.html"),
                      "netsea.jp/shop/84918/": fx("netsea_detail_tate_cm.html").replace("重量195", "重量197"),
                      "netsea.jp/shop/3018/": fx("netsea_detail_tate_cm.html")})
    hit = jw.fetch_weight(JAN, opener=op)
    assert hit.weight_g in (195.0, 197.0)
    assert not any("superdelivery" in u for u in op.calls)


def test_fetch_weight_skips_page_without_the_jan():
    op = make_opener({"netsea.jp/search": fx("netsea_search_4971671192232.html"),
                      "netsea.jp/shop/84918/": fx("netsea_detail_wdh_mm.html"),     # 別 JAN の頁
                      "netsea.jp/shop/3018/": fx("netsea_detail_tate_cm.html")})
    assert jw.fetch_weight(JAN, opener=op).url.endswith("/3018/N00422903")


def test_fetch_weight_falls_back_to_superdelivery():
    op = make_opener({"netsea.jp/search": "<html>0件</html>",
                      "superdelivery.com/p/do/psl": fx("sd_search_4971671192232.html"),
                      "pd_p/11889369": fx("sd_detail_kosou_weight.html")})
    hit = jw.fetch_weight(JAN, opener=op)
    assert hit.source == "superdelivery" and hit.weight_g == 200.0 and hit.height_cm is None
    assert hit.url == "https://www.superdelivery.com/p/r/pd_p/11889369/"


def test_fetch_weight_netsea_size_only_kept_when_sd_has_no_weight():
    jan = "4562344403054"
    op = make_opener({"netsea.jp/search": '<a href="https://www.netsea.jp/shop/812005/1355999">x</a>',
                      "netsea.jp/shop/812005/": fx("netsea_detail_body_weight.html"),
                      "superdelivery.com/p/do/psl": '<a href="/p/r/pd_p/1/">x</a>',
                      "pd_p/1/": fx("sd_detail_case_only.html").replace("4901525010283", jan)})
    hit = jw.fetch_weight(jan, opener=op)
    assert hit.source == "netsea" and hit.weight_g is None and hit.depth_cm == 32.0


def test_fetch_weight_netsea_size_only_then_sd_weight_wins():
    op = make_opener({"netsea.jp/search": '<a href="https://www.netsea.jp/shop/812005/1">x</a>',
                      "netsea.jp/shop/812005/": fx("netsea_detail_body_weight.html").replace("4562344403054", JAN),
                      "superdelivery.com/p/do/psl": fx("sd_search_4971671192232.html"),
                      "pd_p/11889369": fx("sd_detail_kosou_weight.html")})
    assert jw.fetch_weight(JAN, opener=op).source == "superdelivery"


def test_fetch_weight_none_when_only_case_values():
    jan = "4901525010283"
    op = make_opener({"netsea.jp/search": '<a href="https://www.netsea.jp/shop/357136/4901525010283">x</a>',
                      "netsea.jp/shop/357136/": fx("netsea_detail_case_only.html"),
                      "superdelivery.com/p/do/psl": '<a href="/p/r/pd_p/10186509/">x</a>',
                      "pd_p/10186509": fx("sd_detail_case_only.html")})
    assert jw.fetch_weight(jan, opener=op) is None


def test_fetch_weight_http_errors_raise_not_none():
    """通信失敗は「個装の記載なし(None)」と区別して例外(fetch_many が error として残す)。"""
    op = make_opener({"netsea.jp": 503, "superdelivery.com": urllib.error.URLError("down")})
    with pytest.raises(jw.FetchError, match="http=503"):
        jw.fetch_weight(JAN, opener=op)


def test_fetch_weight_detail_error_moves_to_next_shop_and_partial_error_raises():
    op = make_opener({"netsea.jp/search": fx("netsea_search_4971671192232.html"),
                      "netsea.jp/shop/84918/": 403,
                      "netsea.jp/shop/3018/": fx("netsea_detail_tate_cm.html")})
    assert jw.fetch_weight(JAN, opener=op).url.endswith("/3018/N00422903")
    # NETSEA は記載なし・SD は 403 → 調べ切れていないので None ではなく例外
    op = make_opener({"netsea.jp/search": "<html>0件</html>", "superdelivery.com": 403})
    with pytest.raises(jw.FetchError):
        jw.fetch_weight(JAN, opener=op)


def test_fetch_weight_caps_shops_per_site():
    links = "".join(f'<a href="https://www.netsea.jp/shop/{i}/x{i}">' for i in range(10))
    op = make_opener({"netsea.jp/search": links, "netsea.jp/shop/": f"<p>{JAN}</p>",
                      "superdelivery.com": "<html></html>"})
    jw.fetch_weight(JAN, opener=op)
    assert sum("/shop/" in u for u in op.calls) == jw.MAX_SHOPS


# ---------- fetch_many ----------

def test_fetch_many_keeps_errors_and_reports_progress(no_sleep):
    def fn(j):
        if j == "bad":
            raise ValueError("boom")
        return j.upper()

    seen = []
    out = jw.fetch_many(["a", "bad", "c"], fn, interval=1.0, on_progress=lambda *a: seen.append(a))
    assert out["a"] == "A" and out["c"] == "C"
    assert out["bad"] == {"error": "ValueError: boom"}
    assert seen == [(1, 3, "a"), (2, 3, "bad"), (3, 3, "c")]
    assert no_sleep == [1.0, 1.0]                     # 1 件目の前は待たない
