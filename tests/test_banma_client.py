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


# ── 精確批量取得（Boss 2026-08-29「先入库再只拉需要的」）──────────

def test_is_parcel_id_split():
    assert B.is_parcel_id("1965711036740812800")          # 19 位雪花 = 包裹 ID
    assert not B.is_parcel_id("12100195565099")           # Coupang 14 位注文番号
    assert not B.is_parcel_id("PO-100-19404121580150087") # Temu
    assert not B.is_parcel_id("269580-20260607-0488932098")


def test_fetch_shop_map_by_keys_batches_and_routes(monkeypatch):
    """19 位 → IDs / 其他 → OrderDisplayID、200 個/批で分割されること。"""
    calls = []

    class _FakeClient:
        access_token = "T"
        def call(self, method, path, params=None):
            calls.append(params)
            key0 = (params.get("IDs") or params.get("OrderDisplayID")).split(",")[0]
            return {"Packages": [{"Package": {"ID": key0 if key0.isdigit() else "9",
                                              "StoreID": "5"},
                                  "Details": []}],
                    "Page": {"HasMore": False}}

    monkeypatch.setattr(B.BanmaClient, "from_env", classmethod(lambda cls: _FakeClient()))
    monkeypatch.setattr(B, "ensure_token", lambda conn, c: "T")
    monkeypatch.setattr(B, "load_shop_by_store", lambda conn: {"5": "SHOP"})

    class _Cur:
        def executemany(self, sql, rows): pass
    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass

    keys = [str(10**18 + i) for i in range(250)] + ["12100195565099", "PO-1"]
    prog = []
    r = B.fetch_shop_map_by_keys(_Conn(), keys, progress=lambda d, n: prog.append((d, n)))
    ids_batches = [c for c in calls if "IDs" in c]
    oid_batches = [c for c in calls if "OrderDisplayID" in c]
    # 19 位 ID は 1,600 字符予算 → 80 個/批 → 250 個 = 4 批
    assert len(ids_batches) == 4
    assert len(oid_batches) == 1
    for b in ids_batches:
        assert len(b["IDs"]) <= B.BATCH_CHAR_BUDGET   # IIS 2,048 上限の回帰
    assert r["requested"] == 252 and r["batches"] == 5
    assert prog[-1] == (5, 5)


def test_fetch_shop_map_by_keys_empty_is_noop(monkeypatch):
    monkeypatch.setattr(B.BanmaClient, "from_env",
                        classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("不应调用"))))
    r = B.fetch_shop_map_by_keys(object(), [])
    assert r == {"requested": 0, "fetched": 0, "upserted": 0, "batches": 0}


def test_chunk_by_budget_respects_char_limit_and_hard_max():
    # 19 位 ID: 80 個/批
    ids = [str(10**18 + i) for i in range(100)]
    chunks = B.chunk_by_budget(ids)
    assert all(len(",".join(c)) <= B.BATCH_CHAR_BUDGET for c in chunks)
    assert [len(c) for c in chunks] == [80, 20]
    # 短い key は hard_max=200 で切れる
    short = ["k"] * 450
    assert [len(c) for c in B.chunk_by_budget(short)] == [200, 200, 50]


# ── 連番後綴 `_N`（2026-08-30 Boss 指摘の回帰）──────────────────

def test_strip_seq_suffix():
    assert B.strip_seq_suffix("260723CNMX7QSX_1") == "260723CNMX7QSX"
    assert B.strip_seq_suffix("260713K1JXYHVW_3") == "260713K1JXYHVW"
    assert B.strip_seq_suffix("4101058683725-2") == "4101058683725"   # 連字符版（Coupang）
    assert B.strip_seq_suffix("SO00504371_7458145") == "SO00504371_7458145"  # 7 位は剥がない
    assert B.strip_seq_suffix("269580-20260607-0488932098") == "269580-20260607-0488932098"  # 楽天
    assert B.strip_seq_suffix("1965711036740812800") == "1965711036740812800"


def test_fetch_maps_suffixed_keys_back_to_originals(monkeypatch):
    """`_1`/`_2` 付き key は base で照会し、元 key ごとに 1 行ずつ upsert される。"""
    class _FakeClient:
        access_token = "T"
        def call(self, method, path, params=None):
            assert params.get("OrderDisplayID") == "260713K1JXYHVW"
            return {"Packages": [{"Package": {"ID": "1979000120594665472",
                                              "StoreID": "5"},
                                  "Details": [{"OrderDisplayID": "260713K1JXYHVW"}]}],
                    "Page": {"HasMore": False}}
    written = []
    class _Cur:
        def executemany(self, sql, rows): written.extend(rows)
    class _Conn:
        def cursor(self): return _Cur()
        def commit(self): pass
    monkeypatch.setattr(B.BanmaClient, "from_env", classmethod(lambda cls: _FakeClient()))
    monkeypatch.setattr(B, "ensure_token", lambda conn, c: "T")
    monkeypatch.setattr(B, "load_shop_by_store", lambda conn: {"5": "SHOP"})
    r = B.fetch_shop_map_by_keys(
        _Conn(), ["260713K1JXYHVW_1", "260713K1JXYHVW_2", "260713K1JXYHVW_3"])
    assert r["upserted"] == 3
    assert sorted(w["parcel_no"] for w in written) == [
        "260713K1JXYHVW_1", "260713K1JXYHVW_2", "260713K1JXYHVW_3"]
    assert all(w["order_id"] == "260713K1JXYHVW" and w["shop"] == "SHOP"
               for w in written)
