"""page48 新品订货申请（Boss 2026-11-13「运营提申请 → 采购确认 → 发注书 → 每周确认 PO 是否到货」「主要用于新品」）.

申请は PG sourcing.new_item_request に置く（status = requested / confirmed / cancelled だけ）。
発注・到货は表に持たず、毎回 nst.purchase_order_line ⋈ nst.item_master_raw（毎日同期）から track() で現算する:
  対応する PO 行 = 同じ JAN・発注日 ≥ 申請日（採購が先に発注して後で確認を押すこともある）
  期限 = 未着の行の 入荷予定日（無ければ 発注日 + OVERDUE_DAYS）の最も早いもの。今日 > 期限 = 超期
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

OVERDUE_DAYS = 14          # Boss 2026-11-13: 入荷予定日が無い PO は 発注日 + 14 日（予定日の入力は 7.5% だけ）
TABLE = "sourcing.new_item_request"
OPEN = ("待确认", "待发注", "在途", "部分到货", "超期")
STATES = (*OPEN, "已到货", "PO关闭未到齐", "已取消")


def normalize_jan(s) -> str:
    """前後空白・先頭の 0 埋め（5 個以上）を落とす（page10 と同じ）。8〜14 桁の数字でなければ ""。"""
    j = re.sub(r"^0{5,}", "", str(s if s is not None else "").strip())
    return j if re.fullmatch(r"\d{8,14}", j) else ""


def _day(x) -> date | None:
    return x.date() if hasattr(x, "date") and callable(x.date) else x


def track(reqs: list[dict], po_lines: list[dict], today: date) -> list[dict]:
    """申請 → state・ordered・received・po_numbers・deadline・overdue_days を足した写し。"""
    by_jan: dict[str, list[dict]] = {}
    for p in po_lines:
        by_jan.setdefault(p["jan"], []).append(p)
    out = []
    for r in reqs:
        x = {**r, "ordered": 0, "received": 0, "po_numbers": [], "deadline": None, "overdue_days": 0}
        if r["status"] == "cancelled":
            x["state"] = "已取消"
        elif r["status"] != "confirmed":
            x["state"] = "待确认"
        else:
            since = _day(r["requested_at"])
            lines = sorted((p for p in by_jan.get(r["jan"], []) if p["trandate"] >= since),
                           key=lambda p: (p["trandate"], p["po_number"]))
            x["ordered"] = sum(float(p["quantity"] or 0) for p in lines)
            x["received"] = sum(float(p["quantity_received"] or 0) for p in lines)
            x["po_numbers"] = list(dict.fromkeys(p["po_number"] for p in lines))
            open_ = [p for p in lines if float(p["quantity_received"] or 0) < float(p["quantity"] or 0)
                     and not p.get("closed")]
            if not lines:
                x["state"] = "待发注"
            elif not open_:
                x["state"] = "已到货" if x["received"] >= x["ordered"] else "PO关闭未到齐"
            else:
                x["deadline"] = min(p["expected_receipt_date"] or p["trandate"] + timedelta(days=OVERDUE_DAYS)
                                    for p in open_)
                x["overdue_days"] = max(0, (today - x["deadline"]).days)
                x["state"] = "超期" if x["overdue_days"] else "部分到货" if x["received"] else "在途"
        out.append(x)
    return out


def open_jan_conflicts(new_jans: list[str], tracked: list[dict]) -> dict[str, int]:
    """新しく出す JAN のうち、未完成の申請がある JAN → その申請 id（同じ PO を 2 件で取り合わない）。"""
    want = set(new_jans)
    return {x["jan"]: x["id"] for x in tracked if x["jan"] in want and x["state"] in OPEN}


def lot_check(qty: int, lot) -> tuple[bool, int]:
    """数量は 起批量（NST 発注ロット）の整倍数（Boss 2026-11-13「数量是需要根据起批量来订货」「执行」= 整倍数）。
    → (ok, 提案数 = 切り上げた整倍数)。起批量が空・0 なら 1 扱い（制限なし）。"""
    lot = int(lot) if lot and lot >= 1 else 1
    ok_qty = max(lot, math.ceil(qty / lot) * lot)
    return ok_qty == qty, ok_qty


def parse_new_rows(records: list[dict], tracked: list[dict],
                   lots: dict[str, int] | None = None) -> tuple[list[dict], list[str]]:
    """申請表の行（JAN・数量・商品名・备注）→ (登録する行, エラー文)。JAN 空の行は無視。エラーが 1 つでもあれば呼び出し側は何も入れない。
    lots = NST 登録済み JAN → 起批量。lots に無い JAN（NST 未登録）は起批量を判定しない（発注書の前に登録が要る）。"""
    from shared.i18n import t
    rows, errs, seen = [], [], set()
    for rec in records:
        raw = str(rec.get("JAN") or "").strip()
        if not raw or raw == "nan":
            continue
        jan = normalize_jan(raw)
        try:
            qty = float(rec.get("数量"))
        except (TypeError, ValueError):
            qty = float("nan")
        if not jan:
            errs.append(t("JAN「{j}」不是 8〜14 位数字").format(j=raw))
        elif not qty >= 1:
            errs.append(t("JAN {j} 的数量要 ≥ 1").format(j=jan))
        elif jan in seen:
            errs.append(t("JAN {j} 在这次提交里重复了").format(j=jan))
        else:
            seen.add(jan)
            ok, sug = lot_check(int(qty), (lots or {}).get(jan, 1))
            if jan in (lots or {}) and not ok:
                errs.append(t("JAN {j} 的数量 {q} 不是起批量 {lot} 的整倍数（建议 {s}）").format(
                    j=jan, q=int(qty), lot=lots[jan], s=sug))
            rows.append({"jan": jan, "qty": int(qty), "item_name": str(rec.get("商品名") or "").strip(),
                         "note": str(rec.get("备注") or "").strip()})
    for jan, rid in open_jan_conflicts([r["jan"] for r in rows], tracked).items():
        errs.append(t("JAN {j} 已有未完成的申请 #{id}（到货或取消后才能再提）").format(j=jan, id=rid))
    return rows, errs


def arrival_summary(tracked: list[dict], today: date, days: int = 90) -> dict:
    """直近 days 日に確認した申請（取消除く）の到货率と内訳。"""
    since = today - timedelta(days=days)
    xs = [x for x in tracked if x["state"] not in ("待确认", "已取消")
          and x.get("confirmed_at") and _day(x["confirmed_at"]) >= since]
    n = {k: sum(x["state"] in v for x in xs) for k, v in (
        ("arrived", ("已到货",)), ("in_transit", ("在途", "部分到货")), ("overdue", ("超期",)),
        ("unordered", ("待发注",)), ("closed_short", ("PO关闭未到齐",)))}
    return {"total": len(xs), **n, "rate": n["arrived"] / len(xs) if xs else None}


# ───────────── DB ─────────────

def ensure_table(conn) -> None:
    """表が無い時だけ DDL（毎回 DDL を出すと他タブの居残りトランザクションでロック待ちになり得る・page34 2026-08-07 障害）。"""
    if conn.execute("SELECT to_regclass(?) AS r", (TABLE,)).fetchone()["r"] is not None:
        return
    conn.execute("CREATE SCHEMA IF NOT EXISTS sourcing")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "id BIGSERIAL PRIMARY KEY, jan TEXT NOT NULL, item_name TEXT, qty INTEGER NOT NULL, note TEXT, "
        "requested_by TEXT, requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
        "status TEXT NOT NULL DEFAULT 'requested', supplier TEXT, unit_price NUMERIC(14,2), confirmed_qty INTEGER, "
        "confirmed_by TEXT, confirmed_at TIMESTAMPTZ, cancelled_by TEXT, cancelled_at TIMESTAMPTZ)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_new_item_request_jan ON {TABLE} (jan)")
    conn.commit()


def load_requests(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE} ORDER BY id").fetchall()]


def load_po_lines(conn, jans: list[str]) -> list[dict]:
    """申請の JAN の PO 行（JAN は両側 normalize_jan と同じ規則で揃える）。"""
    jans = sorted({j for j in jans if j})
    if not jans:
        return []
    rows = conn.execute(
        "SELECT regexp_replace(trim(im.jan), '^0{5,}', '') AS jan, pol.po_number, pol.trandate, pol.quantity, "
        "pol.quantity_received, pol.expected_receipt_date, pol.closed "
        "FROM nst.purchase_order_line pol JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id "
        "WHERE regexp_replace(trim(im.jan), '^0{5,}', '') = ANY(?) AND pol.trandate IS NOT NULL", (jans,)).fetchall()
    return [dict(r) for r in rows]


def insert_requests(conn, rows: list[dict], user: str) -> list[int]:
    """rows = {jan, qty, note, item_name}。→ 作った id。"""
    ids = []
    for r in rows:
        ids.append(conn.execute(
            f"INSERT INTO {TABLE} (jan, item_name, qty, note, requested_by) VALUES (?, ?, ?, ?, ?) RETURNING id",
            (r["jan"], r.get("item_name") or None, int(r["qty"]), r.get("note") or None, user)).fetchone()["id"])
    conn.commit()
    return ids


def confirm(conn, items: list[dict], user: str) -> int:
    """items = {id, supplier, unit_price, confirmed_qty}。待确认（requested）のものだけ確認にする。→ 件数。"""
    n = 0
    for it in items:
        n += len(conn.execute(
            f"UPDATE {TABLE} SET status = 'confirmed', supplier = ?, unit_price = ?, confirmed_qty = ?, "
            "confirmed_by = ?, confirmed_at = NOW() WHERE id = ? AND status = 'requested' RETURNING id",
            (it.get("supplier") or None, it.get("unit_price"), int(it["confirmed_qty"]), user, int(it["id"]))).fetchall())
    conn.commit()
    return n


def cancel(conn, ids: list[int], user: str) -> int:
    n = len(conn.execute(
        f"UPDATE {TABLE} SET status = 'cancelled', cancelled_by = ?, cancelled_at = NOW() "
        "WHERE id = ANY(?) AND status <> 'cancelled' RETURNING id", (user, [int(i) for i in ids])).fetchall())
    conn.commit()
    return n
