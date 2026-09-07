"""PCCC 検証（GSI Express の無料ツール経由）のテスト。全部オフライン。

fixture `pccc_check_response.html` は 2026-09-04 に**ダミーデータ**（テスト/P123412341234）
で実際に叩いて保存した本物の応答。顧客情報は入っていない。

ここが静かに壊れる型:
  · 先方の HTML が変わって 0 件解析 → 「全部 OK」に見えてしまう（実際は何も検証できていない）
  · 장애（関税庁側の一時障害）を NG と混同 → 正しい PCCC を消してしまう
  · 例外が外に出る → 他社サイトが落ちただけで ECMS 出力まで止まる
"""
from __future__ import annotations

from pathlib import Path

import pytest
import requests

from shared import pccc_check as P

FIX = Path(__file__).parent / "fixtures" / "pccc_check_response.html"
REAL_HTML = FIX.read_text(encoding="utf-8")


def test_本物の応答を解析できる():
    rows = P._parse(REAL_HTML)
    assert len(rows) == 1, "先方の HTML 構造が変わった可能性"
    r = rows[0]
    assert r["pccc"] == "P123412341234"
    assert r["status"] == P.STATUS_NG
    assert "존재하지 않습니다" in r["message"]


def test_使用方法の説明表を結果として拾わない():
    """応答 HTML には『사용방법』の例示テーブルも入っている。あれを結果と誤認すると
    実在しない検証結果をでっち上げることになる。"""
    for row in P._parse(REAL_HTML):
        assert row["pccc"].upper().startswith("P") or row["result"] == "오류"
    assert len(P._parse(REAL_HTML)) == 1


@pytest.mark.parametrize("result,expect", [
    ("정상", P.STATUS_OK),
    ("오류", P.STATUS_NG),
    ("장애", P.STATUS_FAULT),
])
def test_3種の結果を正しく分ける(result, expect):
    """장애 は関税庁側の一時障害。NG と混ぜると正しい PCCC を消してしまう。"""
    html = (f"<tr><td>홍길동</td><td>P111111111111</td><td>010</td>"
            f"<td>12345</td><td>{result}</td><td>메시지</td></tr>")
    rows = P._parse(html)
    assert len(rows) == 1 and rows[0]["status"] == expect


def test_空入力は何もしない(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("PCCC が無いのにリクエストを投げた")
    monkeypatch.setattr(requests, "post", boom)
    assert P.check([]) == ([], "")
    assert P.check([{"name": "x", "pccc": ""}]) == ([], "")


def test_通信失敗でも例外を投げず_fault_を返す(monkeypatch):
    """他社の無料ツールなので落ちて当たり前。落ちたら ECMS 出力まで止まる、では困る。"""
    def boom(*a, **kw):
        raise requests.ConnectionError("no route")
    monkeypatch.setattr(requests, "post", boom)
    res, err = P.check([{"name": "홍길동", "pccc": "P111111111111",
                         "phone": "010", "zip": "12345"}])
    assert len(res) == 1
    assert res[0]["status"] == P.STATUS_FAULT
    assert "ConnectionError" in err


def test_返却件数が足りなければ残りを_fault_にする(monkeypatch):
    """2 件送って 1 件しか返らないのに『残りは OK』にしてはいけない。"""
    class _Resp:
        encoding = "utf-8"
        text = ("<tr><td>A</td><td>P111111111111</td><td>010</td>"
                "<td>12345</td><td>정상</td><td></td></tr>")

    monkeypatch.setattr(requests, "post", lambda *a, **kw: _Resp())
    res, err = P.check([
        {"name": "A", "pccc": "P111111111111", "phone": "010", "zip": "12345"},
        {"name": "B", "pccc": "P222222222222", "phone": "011", "zip": "12346"},
    ])
    by = {r["pccc"]: r for r in res}
    assert by["P111111111111"]["status"] == P.STATUS_OK
    assert by["P222222222222"]["status"] == P.STATUS_FAULT
    assert "不一致" in err


def test_送信フォーマットは_名前_부호_전화_우편(monkeypatch):
    sent = {}

    class _Resp:
        encoding = "utf-8"
        text = ""

    def fake_post(url, data=None, headers=None, timeout=None):
        sent["url"] = url
        sent["data"] = data
        return _Resp()

    monkeypatch.setattr(requests, "post", fake_post)
    P.check([{"name": "홍길동", "pccc": "P111111111111", "phone": "010-1234-5678",
              "zip": "01058"}])
    assert sent["url"] == P.ENDPOINT
    assert sent["data"]["action_type"] == "query"
    assert sent["data"]["chk_data"] == "홍길동/P111111111111/010-1234-5678/01058"


def test_status_map_は大文字で引ける():
    m = P.status_map([{"pccc": " p111111111111 ", "status": P.STATUS_OK}])
    assert "P111111111111" in m


# ------------------------------------------------------------------
# 検証結果の反映（ここを間違えると正しい PCCC を消す）
# ------------------------------------------------------------------
def _row(pccc):
    return {"B": "order1", "AA": pccc}


def test_NG_は落とす_OK_はそのまま():
    rows = [_row("P111111111111"), _row("P222222222222")]
    smap = {
        "P111111111111": {"status": P.STATUS_OK, "message": ""},
        "P222222222222": {"status": P.STATUS_NG, "message": "존재하지 않습니다"},
    }
    assert P.apply_results(rows, smap) == 1
    assert rows[0]["AA"] == "P111111111111"
    assert rows[1]["AA"] == ""


def test_関税庁の障害では落とさない():
    """장애 は相手側の一時障害。ここで消すと**正しい番号を捨てる**ことになる。
    運営が再試行できるよう、番号は残したまま画面に理由だけ出す。"""
    rows = [_row("P333333333333")]
    smap = {"P333333333333": {"status": P.STATUS_FAULT, "message": "시스템 장애"}}
    assert P.apply_results(rows, smap) == 0
    assert rows[0]["AA"] == "P333333333333"


def test_未検証の行は触らない():
    rows = [_row("P444444444444"), _row("")]
    assert P.apply_results(rows, {}) == 0
    assert rows[0]["AA"] == "P444444444444"
