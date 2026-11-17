"""文件名になる jan / spu_key / sku_jans / shop_key は WHITEBG_ROOT の外へ出られない（CMS 監査 L15）。rembg・網なし。"""
import importlib
import pathlib
import sys
import types

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WHITEBG_ROOT", str(tmp_path / "whitebg"))
    monkeypatch.setitem(sys.modules, "rembg", types.SimpleNamespace(new_session=lambda *a, **k: None,
                                                                     remove=lambda b, **k: b))
    monkeypatch.syspath_prepend(str(ROOT))
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    return TestClient(app.app), tmp_path


B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "a\\b", ".hidden"])
def test_traversal_rejected(client, bad):
    c, tmp = client
    assert c.post("/upscale", json={"jan": bad, "image_b64": B64}).status_code == 400
    assert c.post("/manual-image", json={"jan": bad, "image_b64": B64 * 2}).status_code == 400
    assert c.post("/compose-spu", json={"spu_key": bad, "sku_jans": ["4900000000011"]}).status_code == 400
    assert c.post("/compose-spu", json={"spu_key": "S1", "sku_jans": [bad]}).status_code == 400
    assert c.get(f"/list/branded/x", params={"shop_key": bad}).status_code == 400
    assert not list(tmp.glob("*.jpg"))


def test_normal_names_still_work(client):
    c, tmp = client
    r = c.post("/upscale", json={"jan": "4900000000011", "image_b64": B64})
    assert r.status_code == 200, r.text
    r = c.post("/compose-spu", json={"spu_key": "花王-4900000000011", "sku_jans": ["4900000000011"],
                                     "source": "upscaled"})
    assert r.status_code == 200, r.text
