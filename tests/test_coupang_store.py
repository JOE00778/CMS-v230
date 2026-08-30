"""coupang_store の SQL とスキーマの整合（tmp SQLite · 外部通信なし）。

ここが静かに壊れる型:
  · 列名のズレ → 値が落ちるだけで例外は出ない
  · 送信済み行を引き直しで上書き → 送った記録が消える
  · PII の期限切れ削除が効かない → 消えるはずのものが残り続ける
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared import coupang_store as cs


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    import shared.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _row(order="26102557706698", box="725106632278026", status="pending", pulled=None):
    return {
        "order_id": order, "shipment_box_id": box, "ordered_at": "2026-08-28T20:09:48",
        "coupang_status": "INSTRUCT", "receiver_name": "박윤진",
        "receiver_phone": "010-2258-5802", "receiver_postcode": "01058",
        "receiver_addr": "경기도 성남시 분당구 분당동 39",
        "addr_sido": "경기도", "addr_sigungu": "경기도 성남시", "addr_detail": "분당구 분당동 39",
        "pccc": "P842160107476", "pccc_kind": "normal",
        "items": [{"jan": "4573626220481", "qty": 2, "price_usd": 26.49}],
        "total_krw": 77900.0, "total_usd": 52.97, "weight_kg": 1.0, "fx_rate": 0.00068,
        "ecms_status": status,
        "pulled_at": pulled or datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }


def test_逐字段回读():
    assert cs.upsert_queue([_row()]) == (1, 0)
    r = cs.list_queue()[0]
    assert r["receiver_postcode"] == "01058"          # 前ゼロが消えない
    assert r["addr_sigungu"] == "경기도 성남시"
    assert r["pccc"] == "P842160107476"
    assert r["total_usd"] == 52.97
    assert r["fx_rate"] == 0.00068
    assert r["items"] == [{"jan": "4573626220481", "qty": 2, "price_usd": 26.49}]


def test_送信済みは引き直しで上書きしない():
    cs.upsert_queue([_row(status="pending")])
    cs.update_row("26102557706698", "725106632278026",
                  ecms_status="sent", ecms_reference="SJ-1001")
    n, skipped = cs.upsert_queue([_row(status="pending")])   # 再取り込み
    assert (n, skipped) == (0, 1)
    r = cs.list_queue()[0]
    assert r["ecms_status"] == "sent"
    assert r["ecms_reference"] == "SJ-1001"                  # 送った記録が生きている


def test_画面で直した値だけ書き戻る():
    cs.upsert_queue([_row()])
    cs.update_row("26102557706698", "725106632278026",
                  addr_sido="서울특별시", pccc="P111111111111",
                  items_json="[]", pulled_at="1999-01-01T00:00:00+09:00")  # 許可外は無視
    r = cs.list_queue()[0]
    assert r["addr_sido"] == "서울특별시"
    assert r["pccc"] == "P111111111111"
    assert r["items"] == [{"jan": "4573626220481", "qty": 2, "price_usd": 26.49}]
    assert not r["pulled_at"].startswith("1999")   # 期限切れ削除の基準日は書き換えさせない


def test_status絞り込み():
    cs.upsert_queue([_row(order="1"), _row(order="2", status="sent")])
    assert {r["order_id"] for r in cs.list_queue("pending")} == {"1"}
    assert {r["order_id"] for r in cs.list_queue("sent")} == {"2"}
    assert len(cs.list_queue()) == 2


def test_PIIは7日で消える():
    old = (datetime.now(timezone.utc).astimezone() - timedelta(days=8)).isoformat(timespec="seconds")
    recent = (datetime.now(timezone.utc).astimezone() - timedelta(days=6)).isoformat(timespec="seconds")
    cs.upsert_queue([_row(order="old", pulled=old), _row(order="new", pulled=recent)])
    assert cs.purge_old() == 1
    left = cs.list_queue()
    assert [r["order_id"] for r in left] == ["new"]
    assert cs.purge_old() == 0                                # 二度目は何も消えない


def test_商品マスタの取り込みと参照():
    assert cs.upsert_products([
        {"jan": "4573626220481", "name_en": "Shampoo Set", "hscode": "330510",
         "weight_g": 500.0, "url": "https://x"},
        {"jan": "", "name_en": "捨てられる"},                  # JAN 無しは入れない
    ]) == 1
    m = cs.product_map()
    assert set(m) == {"4573626220481"}
    assert m["4573626220481"]["weight_g"] == 500.0
    cs.upsert_products([{"jan": "4573626220481", "name_en": "Shampoo Set v2",
                         "hscode": "330510", "weight_g": 520.0}])
    assert cs.product_map()["4573626220481"]["name_en"] == "Shampoo Set v2"   # 上書き


def test_空入力は何もしない():
    assert cs.upsert_queue([]) == (0, 0)
    assert cs.upsert_products([]) == 0
    assert cs.list_queue() == []
