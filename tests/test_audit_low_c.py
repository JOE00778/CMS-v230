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
