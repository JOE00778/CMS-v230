"""斑马 401 を複数スレッドで同時に受けても GetToken は 1 回、全スレッド成功（監査 M17 2026-11-17）。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from shared import banma_client as BC


def test_concurrent_401_refreshes_token_once():
    c = BC.BanmaClient("app", "secret", min_interval=0)
    c.access_token = "old"
    lock = threading.Lock()
    state = {"valid": "new0", "gets": 0}

    def fake_call(method, path, *a, **k):
        if path == "/v1/Auth/GetToken":
            with lock:
                state["gets"] += 1
                state["valid"] = f"new{state['gets']}"   # 取るたびに前の token は無効
                return {"AccessToken": state["valid"]}
        time.sleep(0.05)
        if c.access_token != state["valid"]:
            raise BC.BanmaAuthError(401, "invalid access_token")
        return {"ok": True}

    c._call = fake_call
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda _: c.call("GET", "/v1/product"), range(8)))
    assert res == [{"ok": True}] * 8 and state["gets"] == 1
