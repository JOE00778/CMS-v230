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
