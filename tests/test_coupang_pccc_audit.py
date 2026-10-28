"""shared.coupang_pccc_audit と page45（Coupang PCCC 检查）。斑马・GSI はモック。"""
import pathlib
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

from shared import coupang_pccc_audit as A
from shared import pccc_check as P

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "45_🪪_CoupangPCCC检查.py"


def _order(oid, pccc, name="김철수", phone="0504-1234-5678", zipc="2585"):
    return {"Master": {"ID": oid, "DisplayOrderID": f"3100{oid}", "OriginalOrderID": f"3100{oid}"},
            "Addresses": [{"AddrType": "收货地址", "Name": name, "TaxNo": pccc, "Phone": phone,
                           "PostalCode": zipc}]}


def test_target_from_order_pads_zip_and_uses_receive_address():
    t = A.target_from_order(_order(1, " p123456789012 "))
    assert t == {"order_id": "1", "display_id": "31001", "name": "김철수", "pccc": "P123456789012",
                 "phone": "0504-1234-5678", "zip": "02585"}
    assert A.target_from_order({"Master": {"ID": 2}, "Addresses": []}) is None


def test_masks_and_reason():
    assert A.mask_name("김철수") == "김*수" and A.mask_name("이영") == "이*"
    assert A.mask_pccc("P123456789012") == "P12*******012"
    assert A.reason_zh("납세의무자 개인통관고유부호가 존재하지 않습니다.") == "PCCC 不存在"
    assert A.reason_zh("납세의무자 전화번호가 일치하지 않습니다") == "电话与 PCCC 登记不一致"


def _row(pccc, status, msg=""):
    return {"name": "", "pccc": pccc, "phone": "", "zip": "", "status": status, "result": "", "message": msg}


def test_audit_ok_ng_fault_retry_format_and_empty():
    ts = [A.target_from_order(_order(i, p, phone="010-1111-2222")) for i, p in
          enumerate(["P000000000001", "P000000000002", "P000000000003", "P12345", ""], 1)]
    calls = []

    def check(rows):
        calls.append([r["pccc"] for r in rows])
        if len(calls) == 1:
            return [_row("P000000000001", P.STATUS_OK), _row("P000000000002", P.STATUS_NG, "전화번호가 일치하지 않습니다"),
                    _row("P000000000003", P.STATUS_FAULT, "장애")], ""
        return [_row("P000000000003", P.STATUS_FAULT, "장애")], "timeout"
    rows, err = A.audit(ts, check=check)
    assert calls == [["P000000000001", "P000000000002", "P000000000003"], ["P000000000003"]]   # 形式不正・空は送らない
    v = {r["order_id"]: (r["verdict"], r["reason"]) for r in rows}
    assert v["1"][0] == A.VERDICT_OK
    assert v["2"] == (A.VERDICT_NG, "电话与 PCCC 登记不一致")
    assert v["3"][0] == A.VERDICT_UNCHECKED                       # 再照会でも 장애 → 正常扱いにしない
    assert v["4"][0] == A.VERDICT_NG and "格式" in v["4"][1]
    assert v["5"] == (A.VERDICT_NG, "PCCC 为空")
    assert err == "timeout"
    assert all("김" not in str(r.values()) or r["name_masked"] == "김*수" for r in rows)


def test_audit_retry_recovers_fault():
    ts = [A.target_from_order(_order(1, "P000000000003"))]
    seq = iter([([_row("P000000000003", P.STATUS_FAULT)], ""), ([_row("P000000000003", P.STATUS_OK)], "")])
    rows, _ = A.audit(ts, check=lambda r: next(seq))
    assert rows[0]["verdict"] == A.VERDICT_OK


class _FakeClient:
    def __init__(self, orders):
        self.orders = {o["Master"]["ID"]: o for o in orders}

    def call(self, method, path, params=None):
        if path == "/v1/order/":
            return {"Orders": [{"Master": {"ID": k}, "Addresses": []} for k in self.orders],
                    "Page": {"HasMore": False}}
        oid = int(path.rsplit("/", 1)[1])
        return {"Order": self.orders[oid]}


def test_fetch_pending_uses_detail_api_and_reports_missing():
    cl = _FakeClient([_order(1, "P000000000001"), {"Master": {"ID": 2}, "Addresses": []}])
    targets, missing = A.fetch_pending(cl)
    assert [t["order_id"] for t in targets] == ["1"] and missing == ["2"]


def test_page_lists_only_problem_orders_and_csv_has_no_pii():
    cl = _FakeClient([_order(1, "P000000000001"), _order(2, "P000000000002", name="박민수")])
    check = lambda rows: ([_row("P000000000001", P.STATUS_OK),
                           _row("P000000000002", P.STATUS_NG, "납세의무자 개인통관고유부호가 존재하지 않습니다.")], "")
    st.cache_data.clear()
    with mock.patch("shared.banma_client.is_configured", return_value=True), \
         mock.patch("shared.banma_client.BanmaClient.from_env", return_value=cl), \
         mock.patch("shared.banma_client.ensure_token"), \
         mock.patch("shared.db.get_connection"), \
         mock.patch("shared.pccc_check.check", side_effect=check), \
         mock.patch("shared.auth.require_admin"):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
        at.button[0].click().run()
    assert not at.exception, [e.value for e in at.exception]
    m = {x.label: x.value for x in at.metric}
    assert (m["待审核订单"], m["正常"], m["PCCC 有误"], m["未能检查"]) == ("2", "1", "1", "0")
    df = at.dataframe[0].value
    assert list(df.iloc[:, 0]) == ["31002"] and df.iloc[0]["PCCC"] == "P00*******002"
    assert "박민수" not in df.to_string() and "박*수" in df.to_string()


def test_safe_number_replaced_by_dummy_mobile_and_phone_mismatch_counts_as_ok():
    ts = [A.target_from_order(_order(1, "P000000000001")),                          # 0504 安心番号
          A.target_from_order(_order(2, "P000000000002", phone="010-9876-5432"))]   # 本物の携帯はそのまま
    sent = []

    def check(rows):
        sent.extend(rows)
        return [_row("P000000000001", P.STATUS_NG, "납세의무자 전화번호가 일치하지 않습니다"),
                _row("P000000000002", P.STATUS_NG, "납세의무자 전화번호가 일치하지 않습니다")], ""
    rows, _ = A.audit(ts, check=check)
    assert [r["phone"] for r in sent] == ["010-0000-0000", "010-9876-5432"]
    v = {r["order_id"]: r for r in rows}
    assert v["1"]["verdict"] == A.VERDICT_OK and "未核对" in v["1"]["reason"]
    assert v["2"]["verdict"] == A.VERDICT_NG            # 本物の番号での電話不一致は誤りのまま
    assert A.reason_zh("현재 사용중이 아닌 개인통관고유부호 입니다.") == "PCCC 已停用（非使用中）"
