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
    assert jw.parse_sd_links(fx("sd_search_4971671192232.html")) == [
        "https://www.superdelivery.com/p/r/pd_p/11889369/",
        "https://www.superdelivery.com/p/r/pd_p/12285405/"]


# ---------- fetch_weight（スーパーデリバリーのみ · 2026-10-03 NETSEA 廃止）----------

JAN = "4971671192232"
SEARCH = fx("sd_search_4971671192232.html")          # pd_p/11889369 → pd_p/12285405 の順
W200 = fx("sd_detail_kosou_weight.html")              # 個装重量：200g · JAN 入り


def _w(g):
    return W200.replace("個装重量：200g", f"個装重量：{g}g")


def test_fetch_weight_never_calls_netsea(no_sleep):
    op = make_opener({"superdelivery.com/p/do/psl": SEARCH, "pd_p/11889369": W200, "pd_p/12285405": _w(198)})
    jw.fetch_weight(JAN, opener=op)
    assert op.calls and not any("netsea" in u for u in op.calls)
    assert not hasattr(jw, "NETSEA_SEARCH") and not hasattr(jw, "parse_netsea_links")
    assert no_sleep and all(s >= 0.8 for s in no_sleep)


def test_fetch_weight_two_agreeing_shops():
    op = make_opener({"superdelivery.com/p/do/psl": SEARCH, "pd_p/11889369": W200, "pd_p/12285405": _w(198)})
    hit = jw.fetch_weight(JAN, opener=op)
    assert hit.source == "superdelivery" and hit.weight_g in (198.0, 200.0) and "1店舗のみ" not in hit.note


# 実例(2026-10-03): 4971671192232 は先頭店が「個装 19g」と桁落ち、JD 実測 196g。
def test_fetch_weight_typo_shop_is_outvoted():
    links = SEARCH + '<a href="/p/r/pd_p/3/">x</a>'
    op = make_opener({"superdelivery.com/p/do/psl": links, "pd_p/11889369": _w(19),
                      "pd_p/12285405": W200, "pd_p/3/": _w(196)})
    hit = jw.fetch_weight(JAN, opener=op)
    assert hit.weight_g in (196.0, 200.0)
    assert "外れ値を除外" in hit.note and "19g" in hit.note


def test_fetch_weight_two_disagreeing_shops_leave_weight_blank():
    """2 店しか無く値が割れる → どちらも選ばず重量は空。推測で選ばない。"""
    op = make_opener({"superdelivery.com/p/do/psl": SEARCH, "pd_p/11889369": _w(19), "pd_p/12285405": W200})
    hit = jw.fetch_weight(JAN, opener=op)
    assert hit.weight_g is None and "一致せず" in hit.note


def test_fetch_weight_single_shop_is_marked_unverified():
    op = make_opener({"superdelivery.com/p/do/psl": SEARCH, "pd_p/11889369": W200,
                      "pd_p/12285405": "<html>別商品</html>"})
    hit = jw.fetch_weight(JAN, opener=op)
    assert (hit.weight_g, hit.url) == (200.0, "https://www.superdelivery.com/p/r/pd_p/11889369/")
    assert "1店舗のみ" in hit.note


def test_fetch_weight_none_when_only_case_values():
    jan = "4901525010283"
    op = make_opener({"superdelivery.com/p/do/psl": '<a href="/p/r/pd_p/10186509/">x</a>',
                      "pd_p/10186509": fx("sd_detail_case_only.html")})
    assert jw.fetch_weight(jan, opener=op) is None


def test_fetch_weight_http_errors_raise_not_none():
    """通信失敗は「個装の記載なし(None)」と区別して例外(fetch_many が error として残す)。"""
    with pytest.raises(jw.FetchError, match="http=503"):
        jw.fetch_weight(JAN, opener=make_opener({"superdelivery.com": 503}))


def test_fetch_weight_detail_error_moves_to_next_shop_and_all_failed_raises():
    op = make_opener({"superdelivery.com/p/do/psl": SEARCH, "pd_p/11889369": 403, "pd_p/12285405": W200})
    assert jw.fetch_weight(JAN, opener=op).url.endswith("/12285405/")
    op = make_opener({"superdelivery.com/p/do/psl": SEARCH, "pd_p/": 403})
    with pytest.raises(jw.FetchError):
        jw.fetch_weight(JAN, opener=op)


def test_fetch_weight_caps_shops():
    links = "".join(f'<a href="/p/r/pd_p/{i}/">' for i in range(10))
    op = make_opener({"superdelivery.com/p/do/psl": links, "pd_p/": f"<p>{JAN}</p>"})
    jw.fetch_weight(JAN, opener=op)
    assert sum("pd_p/" in u for u in op.calls) == jw.MAX_SHOPS


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
