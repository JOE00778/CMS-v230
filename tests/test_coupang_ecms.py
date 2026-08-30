"""Coupang → ECMS 変換の回帰テスト。

数値の丸めと SKU の入数は**間違えても例外が出ない**（申告数量や重量が静かにズレるだけ）。
運営 Excel の数式（`coupang通关文件.xlsx`「JD 用发货文件」）を正として固定する。
"""
from __future__ import annotations

import pytest

from shared import coupang_ecms as ce


@pytest.fixture(autouse=True)
def fixed_rate(monkeypatch):
    monkeypatch.delenv("COUPANG_KRW_USD_RATE", raising=False)


# ---------- 換算 ----------
def test_krw_usd_は運営の固定係数と小数2桁():
    assert ce.DEFAULT_KRW_USD == 0.00068          # Excel: ROUND(S2*0.00068, 2)
    assert ce.usd_from_krw(77900) == 52.97        # 52.972 → 52.97
    assert ce.usd_from_krw(16800) == 11.42        # 11.424 → 11.42
    assert ce.usd_from_krw(0) == 0.0


def test_レートは環境変数で差し替えられる(monkeypatch):
    monkeypatch.setenv("COUPANG_KRW_USD_RATE", "0.00075")
    assert ce.fx_rate() == 0.00075
    monkeypatch.setenv("COUPANG_KRW_USD_RATE", "壊れた値")
    assert ce.fx_rate() == ce.DEFAULT_KRW_USD     # 壊れていても既定に落ちる
    monkeypatch.setenv("COUPANG_KRW_USD_RATE", "0")
    assert ce.fx_rate() == ce.DEFAULT_KRW_USD     # 0 は無効


def test_150ドル線(monkeypatch):
    assert ce.clearance_type(149.99) == "1"       # 目録通関
    assert ce.clearance_type(150.0) == "2"        # ちょうど 150 は一般申告（Excel: >=150）
    assert ce.clearance_type(220.0) == "2"


def test_重量は小数1桁の切り上げ():
    assert ce.roundup_1(0.085) == 0.1             # 85g → 0.1kg
    assert ce.roundup_1(0.10) == 0.1              # ちょうどは上げない
    assert ce.roundup_1(0.11) == 0.2
    assert ce.roundup_1(1.0) == 1.0
    assert ce.roundup_1(0.3000000001) == 0.3      # 浮動小数の誤差で 0.4 にしない


# ---------- SKU ----------
def test_SKUから入数を取る():
    assert ce.split_sku("4573626220481_2") == ("4573626220481", 2)
    assert ce.split_sku("4901616011014") == ("4901616011014", 1)   # "_" 無しは 1
    assert ce.split_sku("4901616011014_") == ("4901616011014", 1)  # 壊れていても 1
    assert ce.split_sku("") == ("", 1)


# ---------- 住所 ----------
# 分割そのものは shared/kr_address.py のテスト（tests/test_kr_address.py）が持つ。
# ここは queue 行に正しく載るかだけ見る（下の test_queue行）。


# ---------- PCCC / 電話 ----------
def _box(**over):
    b = {
        "orderId": 26102557706698, "shipmentBoxId": 725106632278026,
        "orderedAt": "2026-08-28T20:09:48", "status": "INSTRUCT",
        "receiver": {"name": "박윤진", "safeNumber": "0502-5402-8059",
                     "receiverNumber": None,
                     "addr1": "경기도 성남시 분당구 분당동 39",
                     "addr2": "샛별마을삼부아파트 405동1304호", "postCode": "13581"},
        "overseaShippingInfoDto": {"personalCustomsClearanceCode": "P842160107476",
                                   "ordererPhoneNumber": "010-2258-5802"},
        "orderItems": [{"externalVendorSkuCode": "4573626220481_2", "shippingCount": 1,
                        "cancelCount": 0, "salesPrice": 77900}],
    }
    b.update(over)
    return b


def _lookup(jan):
    return {"4573626220481": {"name_en": "&Honey Sakura Shampoo Treatment Set",
                              "hscode": "330510", "weight_g": 500.0,
                              "url": "https://www.coupang.com/vp/products/9660564393"}}.get(jan)


def test_安心番号は使わない():
    """0502/0503 の安心番号を通関に出すと弾かれる。実番号だけを返すこと。"""
    assert ce.customs_phone(_box()) == "010-2258-5802"
    no_oversea = _box(overseaShippingInfoDto={})
    assert ce.customs_phone(no_oversea) == ""          # safeNumber には落ちない
    assert "0502" not in ce.customs_phone(no_oversea)


def test_一回限りPCCCも拾う():
    assert ce.pccc_of(_box()) == ("P842160107476", "normal")
    onetime = _box(overseaShippingInfoDto={"oneTimePccc": "P999999999999"})
    assert ce.pccc_of(onetime) == ("P999999999999", "onetime")
    assert ce.pccc_of(_box(overseaShippingInfoDto={})) == ("", "")


# ---------- 行の組み立て ----------
def test_queue行():
    row = ce.to_queue_row(_box(), _lookup, "2026-08-30T09:00:00+09:00")
    assert row["order_id"] == "26102557706698"
    assert row["receiver_postcode"] == "13581"
    assert row["addr_sigungu"] == "경기도 성남시"
    assert row["addr_detail"] == "분당구 분당동 39 샛별마을삼부아파트 405동1304호"
    assert row["pccc"] == "P842160107476"
    it = row["items"][0]
    assert it["qty"] == 2                     # 入数 2 × 出荷 1
    assert it["weight_kg"] == 1.0             # 500g × 2
    assert row["weight_kg"] == 1.0
    assert row["total_krw"] == 77900
    assert row["total_usd"] == 52.97
    assert row["fx_rate"] == 0.00068          # 使ったレートを残す
    assert row["ecms_status"] == "pending"
    assert ce.missing_fields(row) == []


def test_前ゼロの邮编が消えない():
    row = ce.to_queue_row(_box(receiver={**_box()["receiver"], "postCode": "01058"}),
                          _lookup, "t")
    assert row["receiver_postcode"] == "01058"


def test_キャンセル分は数えない():
    b = _box()
    b["orderItems"] = [{"externalVendorSkuCode": "4573626220481_2", "shippingCount": 3,
                        "cancelCount": 3, "salesPrice": 77900}]
    row = ce.to_queue_row(b, _lookup, "t")
    assert row["items"] == []                 # 全部キャンセル → 明細ゼロ
    assert "申告明細" in ce.missing_fields(row)


def test_商品マスタ未登録は空のまま返して赤で出す():
    row = ce.to_queue_row(_box(), lambda jan: None, "t")
    miss = ce.missing_fields(row)
    assert "重量（商品マスタ未登録）" in miss
    assert any("英語品名" in m for m in miss)
    assert row["weight_kg"] is None           # 勝手に 0 を入れない
