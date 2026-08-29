"""shared/banma_client の単体テスト — 网络/凭証/PG 不要。

物流費配賦の斑马自動補齊（2026-08-29 Boss 拍板）の要:
  - 署名が database 仓 banma_api/client.py と同一アルゴリズムであること
  - 包裹 → order_shop_map 行変換で PII が絶対に落ちないこと
  - 網絡瞬断で run が死なないこと（2026-08-28 全 ingester 統一判据）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from shared import banma_client as B


def _client() -> B.BanmaClient:
    return B.BanmaClient("APPID", "SECRET", min_interval=0)


# ── 署名（database 仓 test_banma_client と同ベクトル）──────────

def test_sign_matches_database_repo_algorithm():
    ts = "1640157247"
    query = "PageNumber=1&PageSize=50&SearchTimeField=CreateTime"
    got = _client().sign("GET", "/v1/order/package", query, ts)
    text = ("GET/v1/order/package"
            "app_id=APPID&app_secret=SECRET&"
            "pagenumber=1&pagesize=50&searchtimefield=CreateTime&"
            + ts)
    assert got == hashlib.sha256(text.encode()).hexdigest()


# ── 窓計算 ────────────────────────────────────────────────

def test_invoice_window_from_cost_dates():
    start, end = B.invoice_window(
        [dt.date(2026, 7, 3), None, dt.date(2026, 7, 28)], "2026-07")
    assert start == "2026-06-23T00:00:00"
    assert end == "2026-08-07T23:59:59"


def test_invoice_window_fallback_month_and_year_end():
    start, end = B.invoice_window([], "2026-12")
    assert start == "2026-11-21T00:00:00"
    assert end == "2027-01-10T23:59:59"


# ── 包裹 → 行変換 ─────────────────────────────────────────

_PKG = {
    "Package": {
        "ID": "1965711036740812800",
        "ExpressNo": "365619526252",
        "TrackingID": "JDW101189909578",
        "StoreID": "1830570072565882880",
        "Platform": "Rakuten",
        "CreateTime": "2026-06-07T15:53:52",
        "DeliveryTime": "2026-06-07T16:30:17",
        # PII（絶対に行へ出ない）
        "Consignee": "山田太郎", "PhoneNumber": "090", "Email": "x@x",
        "Address1": "東京都…", "PostalCode": "100-0001",
    },
    "Details": [
        {"OrderDisplayID": "269580-20260607-0488932098", "Quantity": 1},
        {"OrderDisplayID": "269580-20260607-0488932098", "Quantity": 2},
    ],
}


def test_package_to_row_maps_all_columns():
    row = B.package_to_row(_PKG, {"1830570072565882880": "ロートレーベル楽天"})
    assert row == {
        "parcel_no": "1965711036740812800",
        "order_id": "269580-20260607-0488932098",
        "waybill_no": "365619526252",
        "platform": "Rakuten",
        "shop": "ロートレーベル楽天",       # 対照表が最優先
        "ship_date": dt.date(2026, 6, 7),
    }


def test_package_to_row_never_leaks_pii():
    row = B.package_to_row(_PKG, {})
    dump = json.dumps(row, ensure_ascii=False, default=str)
    for pii in ("山田", "090", "x@x", "東京都", "100-0001"):
        assert pii not in dump


def test_package_to_row_store_fallback_and_missing_id():
    row = B.package_to_row(_PKG, {})          # 対照表無し → StoreID 生値
    assert row["shop"] == "1830570072565882880"
    assert B.package_to_row({"Package": {}, "Details": []}, {}) is None


def test_package_to_row_delivery_time_missing_falls_back_to_create():
    pkg = {"Package": {"ID": "1", "CreateTime": "2026-07-01T10:00:00"},
           "Details": []}
    assert B.package_to_row(pkg, {})["ship_date"] == dt.date(2026, 7, 1)


# ── call 再試行（2026-08-28 統一判据の回帰）──────────────────

class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _ok(payload):
    return _Resp(json.dumps(
        {"Success": True, "Code": 200, "Data": payload}).encode())


def test_call_retries_timeout_then_succeeds(monkeypatch):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) <= 2:
            raise TimeoutError("read timed out")
        return _ok({"ok": 1})
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(B.time, "sleep", lambda s: None)
    assert _client().call("GET", "/v1/store") == {"ok": 1}
    assert len(calls) == 3


def test_call_401_raises_auth_error(monkeypatch):
    err = urllib.error.HTTPError("http://x", 401, "e", Message(),
                                 io.BytesIO(b"{}"))
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(err))
    with pytest.raises(B.BanmaAuthError):
        _client().call("GET", "/v1/store")


# ── iter_packages 分頁 ────────────────────────────────────

def test_iter_packages_paginates_until_no_more(monkeypatch):
    pages = {
        1: {"Packages": [{"Package": {"ID": "1"}}, {"Package": {"ID": "2"}}],
            "Page": {"HasMore": True, "PageCount": 2}},
        2: {"Packages": [{"Package": {"ID": "3"}}],
            "Page": {"HasMore": False, "PageCount": 2}},
    }
    seen_progress = []
    c = _client()
    monkeypatch.setattr(c, "call",
                        lambda m, p, params=None: pages[params["PageNumber"]])
    got = list(B.iter_packages(c, "s", "e",
                               progress=lambda p, n: seen_progress.append((p, n))))
    assert [g["Package"]["ID"] for g in got] == ["1", "2", "3"]
    assert seen_progress == [(1, 2), (2, 2)]


# ── ensure_token 分岐（fake conn）─────────────────────────

class _FakeConn:
    def __init__(self, row):
        self.row = row
        self.executed = []
    def execute(self, sql, params=None):
        self.executed.append(sql)
        class _R:
            def __init__(s2, row): s2._row = row
            def fetchone(s2): return s2._row
        return _R(self.row)
    def commit(self): pass


def test_ensure_token_uses_valid_cache():
    now = dt.datetime(2026, 8, 29, 12, 0)
    conn = _FakeConn(("TOK", now + dt.timedelta(days=2), "RT",
                      now + dt.timedelta(days=20)))
    c = _client()
    assert B.ensure_token(conn, c, now=now) == "TOK"
    assert c.access_token == "TOK"


def test_ensure_token_fetches_when_cache_expired(monkeypatch):
    now = dt.datetime(2026, 8, 29, 12, 0)
    conn = _FakeConn(None)
    c = _client()
    monkeypatch.setattr(c, "call", lambda m, p, params=None: {
        "AccessToken": "NEW",
        "AccessTokenExpiryTime": "2026-09-01T00:00:00",
        "RefreshToken": "RT2",
        "RefreshTokenExpiryTime": "2026-09-27T00:00:00"})
    assert B.ensure_token(conn, c, now=now) == "NEW"
    assert any("INSERT INTO banma.api_token" in q for q in conn.executed)
