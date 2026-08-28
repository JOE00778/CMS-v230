"""ecms_store 与 schema 的一致性（SQLite 临时库，零外网零 PG）。

这层写错列名 / 占位符数量不会崩——只会静默丢字段或整条不落库，所以逐字段验回读。
同时钉住两个行为：UAT 与 PRO 记录互不可见、同一事件重复拉取不重复落库。
"""
from __future__ import annotations

import pytest

from shared import ecms_store as store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """把 SQLite 落到 tmp，绝不碰仓库里的 warehouse.db。"""
    import shared.db as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setenv("ECMS_ENV", "uat")
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_建单记录逐字段回读():
    store.save_shipment(
        reference_code="SJ-1001", status="created",
        shipment_id="ESE001", tracking_no="ECESE9",
        label_url="http://x/l.pdf", receiver_name="Juan", receiver_country="PH",
        created_by="a@b.c",
        request_json={"referenceCode": "SJ-1001"}, response_json={"shipmentId": "ESE001"},
    )
    row = store.fetch_shipment("SJ-1001")
    assert row["env"] == "uat"
    assert row["status"] == "created"
    assert row["shipment_id"] == "ESE001"
    assert row["tracking_no"] == "ECESE9"
    assert row["label_url"] == "http://x/l.pdf"
    assert row["receiver_name"] == "Juan"
    assert row["receiver_country"] == "PH"
    assert row["created_by"] == "a@b.c"
    assert '"referenceCode": "SJ-1001"' in row["request_json"]   # dict 自动转 JSON 文本
    assert row["created_at"] and row["updated_at"]


def test_失败也留痕并可被重建覆盖():
    store.save_shipment(reference_code="SJ-2", status="failed",
                        response_json={"error": "Param error"})
    assert store.fetch_shipment("SJ-2")["status"] == "failed"
    store.save_shipment(reference_code="SJ-2", status="created", tracking_no="ECESE1")
    row = store.fetch_shipment("SJ-2")
    assert row["status"] == "created" and row["tracking_no"] == "ECESE1"


def test_UAT与PRO记录互不可见(monkeypatch):
    store.save_shipment(reference_code="SJ-3", status="created", tracking_no="UAT-1")
    monkeypatch.setenv("ECMS_ENV", "pro")
    assert store.fetch_shipment("SJ-3") is None, "生产环境读到了 UAT 的单——单号会混"
    store.save_shipment(reference_code="SJ-3", status="created", tracking_no="PRO-1")
    assert store.fetch_shipment("SJ-3")["tracking_no"] == "PRO-1"
    monkeypatch.setenv("ECMS_ENV", "uat")
    assert store.fetch_shipment("SJ-3")["tracking_no"] == "UAT-1"


def test_取消更新状态():
    store.save_shipment(reference_code="SJ-4", status="created", tracking_no="ECESE4")
    store.update_status("SJ-4", "cancelled")
    assert store.fetch_shipment("SJ-4")["status"] == "cancelled"


def _ev(code="S01N100", date="2026-08-28T10:00:00+0900", desc="Electronic manifest received"):
    return {"trackingNo": "ECESE9", "code": code, "reasonCode": "N", "date": date,
            "description": desc, "remark": "", "location": "Tokyo, Japan"}


def test_事件逐字段回读与去重():
    assert store.save_events([_ev()]) == 1
    store.save_events([_ev(), _ev(code="S05N500", desc="Flight departure")])  # 第一条重复
    rows = store.local_events("ECESE9")
    assert len(rows) == 2, "重复事件被重复落库了"
    latest = rows[0]                                   # 按 event_time DESC
    assert latest["event_code"] in {"S01N100", "S05N500"}
    first = [r for r in rows if r["event_code"] == "S01N100"][0]
    assert first["description"] == "Electronic manifest received"
    assert first["location"] == "Tokyo, Japan"


def test_空事件列表不写库():
    assert store.save_events([]) == 0
    assert store.local_events("NOPE") == []


def test_记录列表按时间倒序且带env():
    store.save_shipment(reference_code="SJ-A", status="created")
    store.save_shipment(reference_code="SJ-B", status="failed")
    rows = store.recent_shipments(limit=10)
    assert {r["reference_code"] for r in rows} == {"SJ-A", "SJ-B"}
    assert all(r["env"] == "uat" for r in rows)


def test_PG_upsert冲突列已登记():
    """元川跑的是 PG：INSERT OR REPLACE 的表没在 _UPSERT_CONFLICT 登记会直接 RuntimeError。

    本机 SQLite 测不到这条路径，所以在这里钉死——漏登记等于页面在元川一按就炸。
    """
    from shared.db import _PostgresAdapter
    assert _PostgresAdapter._UPSERT_CONFLICT.get("ecms_shipment") == ("reference_code", "env")
    sql = _PostgresAdapter._rewrite_upsert(
        "INSERT OR REPLACE INTO ecms_shipment (reference_code, env, status) VALUES (?,?,?)")
    assert "ON CONFLICT (reference_code, env) DO UPDATE SET status=EXCLUDED.status" in sql
