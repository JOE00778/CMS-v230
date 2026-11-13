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


def test_parse_new_rows_validates_and_blocks_open_jan(monkeypatch):
    monkeypatch.setattr("shared.i18n.get_lang", lambda: "zh")
    tracked = [{"id": 9, "jan": "4900000000011", "state": "在途"}, {"id": 8, "jan": "4900000000028", "state": "已到货"}]
    recs = [{"JAN": " 4900000000011 ", "数量": 2}, {"JAN": "4900000000028", "数量": 3.0, "商品名": " 名 ", "备注": "PH 用"},
            {"JAN": "", "数量": None}, {"JAN": "nan"}, {"JAN": "12ab", "数量": 1}, {"JAN": "4900000000035", "数量": 0},
            {"JAN": "4900000000028", "数量": 1}]
    rows, errs = NR.parse_new_rows(recs, tracked)
    assert [r["jan"] for r in rows] == ["4900000000011", "4900000000028"]
    assert rows[1] == {"jan": "4900000000028", "qty": 3, "item_name": "名", "note": "PH 用"}
    assert len(errs) == 4 and "#9" in errs[-1] and "12ab" in errs[0]   # 不正 JAN・数量 0・重複・未完成申請あり
    assert NR.parse_new_rows([{"JAN": "4900000000042", "数量": 1}], tracked) == (
        [{"jan": "4900000000042", "qty": 1, "item_name": "", "note": ""}], [])


def test_lot_check_multiple_of_order_lot_20261113():
    assert NR.lot_check(48, 24) == (True, 48)
    assert NR.lot_check(30, 24) == (False, 48)       # 整倍数でない → 切り上げを提案
    assert NR.lot_check(10, 24) == (False, 24)       # 起批量未満
    assert NR.lot_check(7, None) == (True, 7) and NR.lot_check(7, 0) == (True, 7)   # 起批量なし = 1


def test_parse_new_rows_enforces_lot_only_for_known_jan(monkeypatch):
    monkeypatch.setattr("shared.i18n.get_lang", lambda: "zh")
    recs = [{"JAN": "4900000000011", "数量": 30}, {"JAN": "4900000000028", "数量": 3}]
    rows, errs = NR.parse_new_rows(recs, [], lots={"4900000000011": 24})
    assert [r["jan"] for r in rows] == ["4900000000011", "4900000000028"]   # 行は返す・エラーがあれば呼び出し側が入れない
    assert len(errs) == 1 and "24" in errs[0] and "48" in errs[0]           # NST 未登録（lots に無い）は止めない
    assert NR.parse_new_rows([{"JAN": "4900000000011", "数量": 48}], [], lots={"4900000000011": 24})[1] == []


def test_unit_price_prefers_last_purchase_cost_then_cost_20261113():
    from decimal import Decimal
    assert NR.unit_price_of({"last_purchase_cost": Decimal("120.00"), "cost": Decimal("100")}) == (120.0, "最終仕入原価")
    assert NR.unit_price_of({"last_purchase_cost": None, "cost": Decimal("900.0000")}) == (900.0, "原価")
    assert NR.unit_price_of({"last_purchase_cost": float("nan"), "cost": 0}) == (None, "")
    assert NR.unit_price_of({}) == (None, "")


def test_default_supplier_main_then_latest_po_20261113():
    """供货商管理の主供指定（→ 正式名で NST 仕入先名へ）> 最近 PO の仕入先。NST 仕入先一覧に無い名前は出さない。"""
    sups = ["0535 株式会社エーアンドティー", "0001 ハリマ共和物産株式会社", "0474 株式会社　五洲"]
    main = {"J1": ("A&T", "0535 株式会社エーアンドティー"), "J2": ("ハリマ", "ハリマ共和物産株式会社"), "J4": ("謎", None)}
    po = {"J2": "0535 株式会社エーアンドティー", "J3": "0535 株式会社エーアンドティー", "J4": "0474 株式会社　五洲",
          "J5": "9999 不明"}
    assert NR.pick_default_supplier(main, po, sups) == {
        "J1": ("0535 株式会社エーアンドティー", "主供"), "J2": ("0001 ハリマ共和物産株式会社", "主供"),
        "J3": ("0535 株式会社エーアンドティー", "最近 PO"), "J4": ("0474 株式会社　五洲", "最近 PO")}


def test_ordered_waiting_sync_and_missing_po_20261113():
    """発注書 CSV を出した品は NST 同期待ち（発注書タブから消す）。2 日を過ぎても PO が無ければ警告。"""
    def r(i, jan, od):
        x = _req(i, jan, req=date(2026, 11, 10), conf=date(2026, 11, 10))
        x.update(ordered_at=datetime.combine(od, datetime.min.time()), supplier="0457 カネイシ株式会社",
                 unit_price=1285, confirmed_qty=24)
        return x
    t = {x["id"]: x for x in NR.track([r(1, "A", date(2026, 11, 13)), r(2, "B", date(2026, 11, 10))], [], TODAY)}
    assert (t[1]["state"], t[2]["state"]) == ("已发注待同步", "发注书已出无PO")
    assert "已发注待同步" in NR.OPEN and "发注书已出无PO" in NR.OPEN


def test_po_actuals_and_diffs_against_order_values():
    x = _req(1, "A", req=date(2026, 11, 10), conf=date(2026, 11, 10))
    x.update(ordered_at=datetime(2026, 11, 13), supplier="0457 カネイシ株式会社", unit_price=1285, confirmed_qty=24)
    po = [dict(_po("A", "PO9", date(2026, 11, 13), 36, 0), vendor_name="0535 株式会社エーアンドティー", rate=1300, amount=46800)]
    y = NR.track([x], po, TODAY)[0]
    assert (y["state"], y["po_vendors"], y["po_amount"]) == ("在途", ["0535 株式会社エーアンドティー"], 46800.0)
    assert y["diffs"] == [("供货商", "0457 カネイシ株式会社", "0535 株式会社エーアンドティー"), ("单价", 1285.0, 1300.0),
                          ("数量", 24, 36)]
    po2 = [dict(_po("A", "PO9", date(2026, 11, 13), 24, 0), vendor_name="0457 カネイシ株式会社", rate=1285, amount=30840)]
    assert NR.track([x], po2, TODAY)[0]["diffs"] == []
