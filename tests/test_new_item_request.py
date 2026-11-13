"""shared/new_item_request（page48 新品订货申请）の到货判定 — 網・DB 不要。"""
from datetime import date, datetime

from shared import new_item_request as NR

TODAY = date(2026, 11, 13)


def _req(i, jan, status="confirmed", req=date(2026, 11, 1), conf=date(2026, 11, 2)):
    return {"id": i, "jan": jan, "status": status, "requested_at": datetime.combine(req, datetime.min.time()),
            "confirmed_at": datetime.combine(conf, datetime.min.time()) if status == "confirmed" else None}


def _po(jan, po, d, qty, rec, eta=None, closed=False):
    return {"jan": jan, "po_number": po, "trandate": d, "quantity": qty, "quantity_received": rec,
            "expected_receipt_date": eta, "closed": closed}


def _track():
    reqs = [_req(1, "A", "requested"), _req(2, "B", "cancelled"), _req(3, "C"), _req(4, "D"),
            _req(5, "E", req=date(2026, 10, 15), conf=date(2026, 10, 16)), _req(6, "F"), _req(7, "G"), _req(8, "H")]
    po = [_po("C", "PO1", date(2026, 10, 20), 5, 0),                       # 申請日より前の PO は数えない
          _po("D", "PO2", date(2026, 11, 5), 10, 10), _po("D", "PO3", date(2026, 11, 10), 5, 0),
          _po("E", "PO4", date(2026, 10, 20), 6, 0),
          _po("F", "PO5", date(2026, 11, 2), 4, 0, eta=date(2026, 11, 20)),
          _po("G", "PO6", date(2026, 11, 2), 4, 4),
          _po("H", "PO7", date(2026, 11, 2), 4, 1, closed=True)]
    return {r["id"]: r for r in NR.track(reqs, po, TODAY)}


def test_states_deadline_and_overdue_20261113():
    t = _track()
    assert [t[i]["state"] for i in range(1, 9)] == ["待确认", "已取消", "待发注", "部分到货", "超期", "在途",
                                                   "已到货", "PO关闭未到齐"]
    assert (t[4]["ordered"], t[4]["received"], t[4]["deadline"], t[4]["overdue_days"]) == (15, 10, date(2026, 11, 24), 0)
    assert t[4]["po_numbers"] == ["PO2", "PO3"]
    assert (t[5]["deadline"], t[5]["overdue_days"]) == (date(2026, 11, 3), 10)   # 予定日なし = 発注日 + 14
    assert t[6]["deadline"] == date(2026, 11, 20)                                # 予定日があれば予定日
    assert t[3]["ordered"] == 0 and t[3]["po_numbers"] == []


def test_normalize_jan():
    assert NR.normalize_jan(" 0000004901234567890 ") == "4901234567890"
    assert NR.normalize_jan("abc") == "" and NR.normalize_jan("123") == ""
    assert NR.normalize_jan(4901234567890) == "4901234567890"


def test_open_conflicts_only_unfinished():
    t = list(_track().values())
    assert NR.open_jan_conflicts(["C", "E", "G", "B", "Z"], t) == {"C": 3, "E": 5}


def test_arrival_summary_90_days():
    s = NR.arrival_summary(list(_track().values()), TODAY)
    assert (s["total"], s["arrived"], s["in_transit"], s["overdue"], s["unordered"], s["closed_short"]) == (6, 1, 2, 1, 1, 1)
    assert round(s["rate"], 4) == round(1 / 6, 4)
    assert NR.arrival_summary([], TODAY)["rate"] is None
