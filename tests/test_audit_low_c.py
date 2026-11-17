"""監査 低危 C 組（L8 / L11 / L12 / L17 / L19 / L20 / L24 / U1）の回帰テスト。外部 API はすべてモック。"""
from shared import coupang_pccc_audit as A
from shared import pccc_check as P


# ── L8: 同じ PCCC の別注文の結果が上書きされない ──
def _o(oid, name, phone="010-1111-2222"):
    return {"order_id": str(oid), "display_id": f"9{oid}", "name": name,
            "pccc": "P000000000001", "phone": phone, "zip": "02585"}


def test_l8_same_pccc_results_aligned_per_order():
    ts = [_o(1, "김철수"), _o(2, "이영희")]
    res = [{"name": "김철수", "pccc": "P000000000001", "phone": "", "zip": "", "status": P.STATUS_OK,
            "result": "정상", "message": ""},
           {"name": "이영희", "pccc": "P000000000001", "phone": "", "zip": "", "status": P.STATUS_NG,
            "result": "오류", "message": "납세의무자명이 일치하지 않습니다"}]
    rows, _ = A.audit(ts, check=lambda r: (res, ""))
    v = {r["order_id"]: r["verdict"] for r in rows}
    assert v == {"1": A.VERDICT_OK, "2": A.VERDICT_NG}


def test_l8_dummy_judged_by_order_id():
    # 注文1 は実番号で電話不一致 → NG のまま。注文2 は安心番号（dummy）で電話不一致 → OK 扱い
    ts = [_o(1, "김철수"), _o(2, "김철수", phone="0504-1234-5678")]
    msg = "납세의무자 전화번호가 일치하지 않습니다"
    res = [{"name": "김철수", "pccc": "P000000000001", "phone": "", "zip": "", "status": P.STATUS_NG,
            "result": "오류", "message": msg}] * 2
    rows, _ = A.audit(ts, check=lambda r: (res, ""))
    v = {r["order_id"]: r["verdict"] for r in rows}
    assert v == {"1": A.VERDICT_NG, "2": A.VERDICT_OK}


# ── L11 / L12: page39（合規検出）。関数・式だけ AST で取り出して試す ──
import ast
import pathlib

import pandas as pd
import pytest
import streamlit as st

P39 = (pathlib.Path(__file__).resolve().parents[1] / "pages" / "39_🛡️_合规检测.py").read_text(encoding="utf-8")
P39_TREE = ast.parse(P39)


def test_l11_batch_skips_external_only_when_all_countries_blocked():
    fn = next(n for n in ast.walk(P39_TREE) if isinstance(n, ast.FunctionDef) and n.name == "_manual_screen")
    assign = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "blocked" for t in n.targets))
    assert isinstance(assign.value, ast.Call) and assign.value.func.id == "all"


def _p39_fn(name, conn):
    fn = next(n for n in P39_TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {"st": st, "pd": pd, "_conn": conn}
    exec(compile(ast.Module([fn], []), "page39", "exec"), ns)
    return ns[name]


@pytest.mark.parametrize("name", ["_load_rules", "_load_overview"])
def test_l12_read_failure_is_not_cached(name, monkeypatch):
    calls = {"n": 0}

    def conn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("一時的な接続失敗")
        return "CONN"

    monkeypatch.setattr(pd, "read_sql_query", lambda sql, c: pd.DataFrame([{"n": 1}]))
    f = _p39_fn(name, conn)
    f.clear()
    with pytest.raises(RuntimeError):
        f()
    assert f() is not None                  # 失敗はキャッシュされず、2 回目で読み直す
    f.clear()


# ── L17: 飞书 webhook が HTTP 200 + エラーコードを返したら失敗にする ──
import io
import json
import sys
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
import daily_lark_digest as D  # noqa: E402


def _urlopen(body):
    return mock.patch.object(D.urllib.request, "urlopen",
                             lambda req, timeout=None: io.BytesIO(json.dumps(body).encode()))


@pytest.mark.parametrize("body", [{"code": 19021, "msg": "sign match fail"},
                                  {"code": 19024, "msg": "Key Words Not Found"},
                                  {"StatusCode": 9499, "StatusMessage": "Bad Request"}])
def test_l17_lark_error_code_raises(body):
    with _urlopen(body), pytest.raises(RuntimeError):
        D.send({}, "https://example.invalid/hook")


def test_l17_lark_success_passes():
    with _urlopen({"StatusCode": 0, "StatusMessage": "success", "code": 0, "msg": "success"}):
        assert D.send({}, "https://example.invalid/hook")["code"] == 0
