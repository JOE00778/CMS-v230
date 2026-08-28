"""ECMS 发货留痕（ecms_shipment / ecms_tracking_event 的读写）。

page41 只管界面，SQL 全在这里——这样列名与 schema 的一致性可以离线测（写错列名
不会崩，只会静默丢字段）。env(uat/pro) 自动带上：UAT 单号与生产单号绝不能混看。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from shared.db import get_connection
from shared.ecms_client import env_name


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def fetch_shipment(reference_code: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM ecms_shipment WHERE reference_code = ? AND env = ?",
            (reference_code, env_name()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_shipment(
    *,
    reference_code: str,
    status: str,
    request_json: Any = None,
    response_json: Any = None,
    shipment_id: str = "",
    tracking_no: str = "",
    label_url: str = "",
    receiver_name: str = "",
    receiver_country: str = "",
    created_by: str = "",
) -> None:
    """一单一行，reference_code + env 为键（同号重建先取消，见 page41 的拦截）。"""
    def _j(v):
        return v if v is None or isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ecms_shipment (reference_code, env, shipment_id, tracking_no,"
            " status, receiver_name, receiver_country, label_url, request_json, response_json,"
            " created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (reference_code, env_name(), shipment_id, tracking_no, status, receiver_name,
             receiver_country, label_url, _j(request_json), _j(response_json),
             created_by, now_iso(), now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def update_status(reference_code: str, status: str) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE ecms_shipment SET status = ?, updated_at = ?"
            " WHERE reference_code = ? AND env = ?",
            (status, now_iso(), reference_code, env_name()),
        )
        conn.commit()
    finally:
        conn.close()


def save_events(events: list[dict]) -> int:
    """落追踪事件，(trackingNo, code, date) 重复的自动跳过。返回本次收到的条数。"""
    if not events:
        return 0
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO ecms_tracking_event (tracking_no, event_code, reason_code,"
            " event_time, description, remark, location, fetched_at) VALUES (?,?,?,?,?,?,?,?)",
            [(e["trackingNo"], e["code"], e.get("reasonCode"), e["date"], e.get("description"),
              e.get("remark"), e.get("location"), now_iso()) for e in events],
        )
        conn.commit()
        return len(events)
    finally:
        conn.close()


def local_events(tracking_no: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT event_time, event_code, description, location FROM ecms_tracking_event"
            " WHERE tracking_no = ? ORDER BY event_time DESC", (tracking_no,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def recent_shipments(limit: int = 200) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT reference_code, env, status, tracking_no, shipment_id, receiver_name,"
            " receiver_country, created_by, created_at FROM ecms_shipment"
            f" ORDER BY created_at DESC LIMIT {int(limit)}").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
