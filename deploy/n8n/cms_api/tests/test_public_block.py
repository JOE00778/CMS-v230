"""cms-api は公網（Cloudflare 経由）からは oauth-callback 以外見えない（2026-11-17 監査 C1）。
N8N（http://cms-api:8789）・本機（localhost:8789）からは今まで通り。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CMS_OUTPUTS_DIR", str(Path(__file__).resolve().parent / "_tmp_outputs"))
from fastapi.testclient import TestClient  # noqa: E402

import app as cms_app  # noqa: E402

CF = {"cf-ray": "8d1234567890-NRT", "cf-connecting-ip": "203.0.113.9"}
c = TestClient(cms_app.app)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/automation/shopee/tokens"),
    ("put", "/api/automation/shopee/tokens"),
    ("get", "/api/automation/shopee/debug-sign"),
    ("get", "/api/automation/shopee/oauth-url/PH"),
    ("get", "/health"),
])
def test_cloudflare_requests_get_404_except_callback(method, path):
    r = getattr(c, method)(path, headers=CF, **({"json": {"refresh_tokens": {"PH": "x"}}} if method == "put" else {}))
    assert r.status_code == 404


def test_internal_requests_still_work(tmp_path, monkeypatch):
    monkeypatch.setattr(cms_app, "SHOPEE_TOKENS_FILE", tmp_path / "t.json")
    monkeypatch.setattr(cms_app, "OUTPUTS_DIR", tmp_path)
    assert c.get("/health").status_code == 200
    assert c.put("/api/automation/shopee/tokens", json={"refresh_tokens": {"PH": "x"}}).status_code == 200
    assert c.get("/api/automation/shopee/tokens").json()["refresh_tokens"] == {"PH": "x"}


def test_callback_rejects_unknown_market_and_escapes_html():
    r = c.get("/api/automation/shopee/oauth-callback", headers=CF,
              params={"market": "<script>alert(1)</script>", "code": "abc", "shop_id": 1})
    assert r.status_code == 400 and "<script>" not in r.text


CB = "/api/automation/shopee/oauth-callback"
IDS = '{"BR":"227547879","PH":"227466553"}'


def test_br_market_is_allowed_20261118(monkeypatch):
    """BR は tokens にある市場 → 白名单で 400 にしない（11/17 の白名单で BR が漏れていた・再審査 C1）。"""
    monkeypatch.setenv("SHOPEE_SHOP_IDS", IDS)
    r = c.get(CB, headers=CF, params={"market": "BR", "code": "abc", "shop_id": 1})
    assert r.status_code == 403       # 市場は通り、登録と違う shop_id で止まる（Shopee へは行かない）


def test_unregistered_market_rejected_when_registry_set_20261118(monkeypatch):
    """登録表があるのに、その市場が未登録 → 拒否（どの shop の code でも上書きできた・再審査 C1）。"""
    monkeypatch.setenv("SHOPEE_SHOP_IDS", IDS)
    assert c.get(CB, headers=CF, params={"market": "TW", "code": "abc", "shop_id": 1}).status_code == 403


def test_main_account_id_must_be_registered_20261118(monkeypatch):
    """main_account_id（CB merchant）経路も登録値と一致しなければ拒否（この経路は照合を素通りしていた・再審査 C1）。"""
    monkeypatch.setenv("SHOPEE_SHOP_IDS", IDS)
    monkeypatch.delenv("SHOPEE_MAIN_ACCOUNT_IDS", raising=False)
    assert c.get(CB, headers=CF, params={"market": "PH", "code": "abc", "main_account_id": 9}).status_code == 403
    monkeypatch.setenv("SHOPEE_MAIN_ACCOUNT_IDS", '{"PH":"555"}')
    assert c.get(CB, headers=CF, params={"market": "PH", "code": "abc", "main_account_id": 9}).status_code == 403
