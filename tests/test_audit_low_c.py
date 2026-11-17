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


# ── L19: 斑马優先は維持。PG の手動修正と斑马の値が違えば「要確認」を出す ──
from shared import shopify_order as so  # noqa: E402

PAGE44 = next((pathlib.Path(__file__).resolve().parents[1] / "pages").glob("44_*.py")).read_text(encoding="utf-8")


def _order1(jan):
    return {"lineItems": {"nodes": [{"title": "x", "quantity": 1, "currentQuantity": 1, "variant": {"barcode": jan},
                                     "discountedTotalSet": {"shopMoney": {"amount": "10"}}}]}}


def test_l19_manual_vs_banma_conflict_warns_and_banma_wins():
    pg = {"4900000000001": {"hs_code": "330499", "hs_src": "manual", "customs_name_en": "Face cream",
                            "customs_name_src": "manual", "net_weight_g": 100, "weight_src": "manual",
                            "origin_country": "JP"},
          "4900000000002": {"hs_code": "330499", "hs_src": "banma", "origin_country": "JP"}}
    bm = {"4900000000001": {"hs_code": "330749", "customs_name_en": "Face cream", "gross_weight_g": 120},
          "4900000000002": {"hs_code": "330749"}}
    m = so.merge_banma(pg, bm)
    assert m["4900000000001"]["hs_code"] == "330749"            # 斑马優先は変えない
    lines, warn = so.to_ci_lines(_order1("4900000000001"), m)
    hit = [w for w in warn if w["code"] == "manual_overridden"]
    assert len(hit) == 1 and "330499" in hit[0]["arg"] and "330749" in hit[0]["arg"] and "重量" in hit[0]["arg"]
    assert "清関品名" not in hit[0]["arg"]                         # 同じ値なら出さない
    _, warn2 = so.to_ci_lines(_order1("4900000000002"), m)
    assert not [w for w in warn2 if w["code"] == "manual_overridden"]   # manual でなければ出さない


def test_l19_customs_query_reads_src_columns_and_page_copy_honest():
    class C:
        def execute(self, sql, params):
            self.sql = sql
            return self

        def fetchall(self):
            return []
    c = C()
    so.customs_by_jan(c, ["4900000000001"])
    assert "hs_src" in c.sql and "customs_name_src" in c.sql
    assert '"manual_overridden"' in PAGE44
    assert "次回から自動で入る" not in PAGE44


# ── L20: 楽天 API の JAN 検索で、セット品・他 JAN を含む説明文の成分を拾わない ──
from shared import jan_lookup as JL  # noqa: E402

ING_SHORT = "【全成分】水、グリセリン、BG、ペンチレングリコール"
ING_SET = "【全成分】水、グリセリン、BG、エタノール、香料、メチルパラベン、フェノキシエタノール"


def _rakuten(monkeypatch, items):
    monkeypatch.setenv("RAKUTEN_APPLICATION_ID", "x")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "y")
    body = json.dumps({"Items": [{"Item": i} for i in items]}).encode()
    monkeypatch.setattr(JL.urllib.request, "urlopen", lambda req, timeout=None: io.BytesIO(body))


def test_l20_set_with_other_jan_excluded_and_first_hit_wins(monkeypatch):
    jan = "4900000000017"
    _rakuten(monkeypatch, [
        {"itemName": "化粧水 本体 200ml", "itemCaption": ""},
        {"itemName": "化粧水 本体 200ml", "itemCaption": f"JAN:{jan} {ING_SHORT}"},
        {"itemName": "化粧水 + 乳液 2 点", "itemCaption": f"JAN:{jan} / 4900000000024 {ING_SET}"},
        {"itemName": "化粧水 本体 詰め合わせ", "itemCaption": ING_SET + "、ヒアルロン酸Na"},
        {"itemName": "化粧水 本体", "itemCaption": ING_SET},
    ])
    r = JL.rakuten_api(jan)
    assert r["name"] == "化粧水 本体 200ml"
    assert "エタノール" not in r["ingredient"] and r["ingredient"]   # 最長ではなく最初の合格命中


def test_l20_name_skips_set_when_first_item_is_set(monkeypatch):
    _rakuten(monkeypatch, [
        {"itemName": "化粧水 乳液 セット", "itemCaption": "4900000000017 4900000000024"},
        {"itemName": "化粧水 単品", "itemCaption": ING_SHORT},
    ])
    r = JL.rakuten_api("4900000000017")
    assert r["name"] == "化粧水 単品" and r["ingredient"]


def test_l20_multipack_of_same_item_is_not_a_set():
    assert not JL._is_set_listing({"itemName": "化粧水 3個セット", "itemCaption": ""}, "4900000000017")
    assert JL._is_set_listing({"itemName": "化粧水 乳液 2点セット", "itemCaption": ""}, "4900000000017")
