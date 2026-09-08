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
# 検証結果を**出力に反映させない**（2026-09-08 撤去）
# ------------------------------------------------------------------
# 以前は 오류 の PCCC を自動で消していた（`apply_results`）。運営が実運用で
# 「人工核后确认实际信息是正确的，但是这个功能显示是错误的」——関税庁は
# 番号 + 氏名 + 電話 + **登録済み配送地の郵便番号**を照合するので、客が引っ越し
# 後に関税庁側を更新していないだけで 오류 になる。番号は生きている。
# 自動削除は**正しい番号を捨てる**動きだったので消した。ここは戻さないこと。
PAGE = (Path(__file__).parents[1] / "pages" / "41_📮_ECMS发货.py").read_text("utf-8")


def test_自動削除の関数はもう無い():
    assert not hasattr(P, "apply_results"), (
        "PCCC を自動で消す関数を復活させない。関税庁の 오류 は「登録情報が古い」でも出る")


def test_ページも検証結果で行を書き換えない():
    """UI 側は単体テストで追えないので静的に釘を刺す（`test_ecms_page41_product_import`
    と同じ手）。検証結果を使って PCCC 欄へ代入していたら落とす。"""
    assert "apply_results" not in PAGE
    for line in PAGE.splitlines():
        body = line.strip()
        if body.startswith("#"):
            continue
        assert not (body.startswith(("r[\"AA\"]", "r['AA']")) and "=" in body), \
            f"検証結果で PCCC を消している疑い: {body}"


def test_ページに_150ドル警告がある():
    """運営 2026-09-08「订单金额超过150美金时，可以也增加一个人工提示信息吗」。"""
    assert "over_duty_free" in PAGE
