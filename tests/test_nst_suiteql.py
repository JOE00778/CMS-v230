"""shared/nst_suiteql の単体テスト — 网络/凭証不要。"""
from __future__ import annotations

import io
import json
import urllib.request

import pytest

from shared import nst_suiteql as N


def test_tba_header_matches_database_repo_algorithm():
    """固定 nonce/ts で database 仓 TBAAuth と同一の署名になること（独立再構成）。"""
    import base64, hashlib, hmac, urllib.parse as up
    url = "https://acct.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql?limit=1000"
    hdr = N.tba_header("POST", url, account_id="ACCT",
                       consumer_key="CK", consumer_secret="CS",
                       token_id="TK", token_secret="TS",
                       nonce="abcd", ts="1700000000")
    params = {
        "oauth_consumer_key": "CK", "oauth_nonce": "abcd",
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": "1700000000", "oauth_token": "TK",
        "oauth_version": "1.0", "limit": "1000",
    }
    param_str = "&".join(f"{up.quote(k, safe='')}={up.quote(v, safe='')}"
                         for k, v in sorted(params.items()))
    base = "&".join(up.quote(s, safe="") for s in (
        "POST",
        "https://acct.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql",
        param_str))
    sig = base64.b64encode(hmac.new(b"CS&TS", base.encode(),
                                    hashlib.sha256).digest()).decode()
    assert up.quote(sig, safe="") in hdr
    assert 'realm="ACCT"' in hdr


def _env(monkeypatch):
    for k, v in {"NST_ACCOUNT_ID": "acct", "NST_TBA_CONSUMER_KEY": "ck",
                 "NST_TBA_CONSUMER_SECRET": "cs", "NST_TBA_TOKEN_ID": "tk",
                 "NST_TBA_TOKEN_SECRET": "ts"}.items():
        monkeypatch.setenv(k, v)


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_lookup_so_shops_prefix_strip_and_direct_fallback(monkeypatch):
    """店舗ありは「nn:」前綴を外した店名、店舗なしは NST直販。顧客字段は使わない。"""
    _env(monkeypatch)
    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["q"] = json.loads(req.data)["q"]
        return _Resp(json.dumps({"hasMore": False, "items": [
            {"tranid": "SO00504423", "shop": "21:MTK SHOP アマゾン店"},
            {"tranid": "SO00504371", "shop": None},
        ]}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    got = N.lookup_so_shops(["SO00504371", "SO00504423", "SO00504371"])
    assert got == {"SO00504423": "MTK SHOP アマゾン店",
                   "SO00504371": N.NST_DIRECT_SHOP}
    assert "'SO00504371','SO00504423'" in seen["q"]      # 去重+排序
    assert "custbody_fb_ne_ro_shop" in seen["q"]
    assert "entity" not in seen["q"]                      # 顧客は引かない
    assert "type = 'SalesOrd'" in seen["q"]


def test_suiteql_retries_timeout_then_succeeds(monkeypatch):
    _env(monkeypatch)
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("t")
        return _Resp(json.dumps({"hasMore": False, "items": []}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(N.time, "sleep", lambda s: None)
    assert N.suiteql("SELECT 1") == []
    assert len(calls) == 2


def test_suiteql_hasmore_fails_loud(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(json.dumps(
                            {"hasMore": True, "items": [{}]}).encode()))
    with pytest.raises(N.NstError, match="hasMore"):
        N.suiteql("SELECT 1")
