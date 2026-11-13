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
ORDER_SYNC_DAYS = 2       # 発注書 CSV を出してこの日数を過ぎても NST に PO が無ければ警告（PO 同期は毎日 03:03）
OPEN = ("待确认", "待发注", "已发注待同步", "发注书已出无PO", "在途", "部分到货", "超期")
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
        x = {**r, "ordered": 0, "received": 0, "po_numbers": [], "deadline": None, "overdue_days": 0,
             "po_vendors": [], "po_amount": 0.0, "diffs": []}
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
            x["po_vendors"] = list(dict.fromkeys(p.get("vendor_name") for p in lines if p.get("vendor_name")))
            x["po_amount"] = sum(float(p.get("amount") or 0) for p in lines)
            x["diffs"] = _diffs(r, lines, x) if lines else []
            if not lines:
                od = _day(r.get("ordered_at"))
                x["state"] = ("待发注" if not od else "发注书已出无PO" if (today - od).days > ORDER_SYNC_DAYS
                              else "已发注待同步")
            elif not open_:
                x["state"] = "已到货" if x["received"] >= x["ordered"] else "PO关闭未到齐"
            else:
                x["deadline"] = min(p["expected_receipt_date"] or p["trandate"] + timedelta(days=OVERDUE_DAYS)
                                    for p in open_)
                x["overdue_days"] = max(0, (today - x["deadline"]).days)
                x["state"] = "超期" if x["overdue_days"] else "部分到货" if x["received"] else "在途"
        out.append(x)
    return out


def _diffs(r: dict, lines: list[dict], x: dict) -> list[tuple]:
    """発注書の時の値（申請に保存）と NST PO の実際の違い → [(項目, 発注書, NST)]。値が無い項目は比べない。"""
    out = []
    if r.get("supplier") and x["po_vendors"] and x["po_vendors"] != [r["supplier"]]:
        out.append(("供货商", r["supplier"], "・".join(x["po_vendors"])))
    rates = sorted({float(p["rate"]) for p in lines if p.get("rate") is not None})
    if r.get("unit_price") is not None and rates and rates != [float(r["unit_price"])]:
        out.append(("单价", float(r["unit_price"]), rates[0] if len(rates) == 1 else "・".join(f"{v:g}" for v in rates)))
    if r.get("confirmed_qty") and x["ordered"] != float(r["confirmed_qty"]):
        out.append(("数量", int(r["confirmed_qty"]), int(x["ordered"])))
    return out


def open_jan_conflicts(new_jans: list[str], tracked: list[dict]) -> dict[str, int]:
    """新しく出す JAN のうち、未完成の申請がある JAN → その申請 id（同じ PO を 2 件で取り合わない）。"""
    want = set(new_jans)
    return {x["jan"]: x["id"] for x in tracked if x["jan"] in want and x["state"] in OPEN}


def unit_price_of(item: dict) -> tuple[float | None, str]:
    """NST 商品マスタの行 → (申請時の単価, 出典)。最終仕入原価 > 原価（Boss 2026-11-13「执行」）。どちらも無ければ (None, "")。"""
    for col, src in (("last_purchase_cost", "最終仕入原価"), ("cost", "原価")):
        try:
            v = float(item.get(col))
        except (TypeError, ValueError):
            continue
        if v > 0:          # NaN > 0 は False
            return v, src
    return None, ""


def _match_supplier(names, suppliers: list[str]) -> str | None:
    """名前の候補 → NST 仕入先の表示名（「コード 会社名」）。完全一致 か 会社名部分の一致。"""
    for c in (n for n in names if n):
        if c in suppliers:
            return c
        hit = next((s for s in suppliers if s.split(" ", 1)[-1] == c), None)
        if hit:
            return hit
    return None


def pick_default_supplier(main: dict[str, tuple[str, str | None]], po: dict[str, str],
                          suppliers: list[str]) -> dict[str, tuple[str, str]]:
    """JAN → (既定の仕入先, 出典)。page34 供货商管理の「計画仕入先」と同じ順: 主供指定 > 最近 PO の仕入先
    （Boss 2026-11-13「供货商改成默认供货商，数据应该在供货商管理那里有维护」）。
    main = JAN → (主供の略称, 正式名(NST))、po = JAN → 最近 PO の vendor_name。NST 仕入先一覧に当たらない名前は使わない。"""
    out = {}
    for jan in {*main, *po}:
        m = main.get(jan)
        d = _match_supplier((m[1], m[0]), suppliers) if m else None
        if d:
            out[jan] = (d, "主供")
            continue
        d = _match_supplier((po.get(jan),), suppliers)
        if d:
            out[jan] = (d, "最近 PO")
    return out


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
        ("arrived", ("已到货",)), ("in_transit", ("在途", "部分到货", "已发注待同步")), ("overdue", ("超期",)),
        ("unordered", ("待发注", "发注书已出无PO")), ("closed_short", ("PO关闭未到齐",)))}
    return {"total": len(xs), **n, "rate": n["arrived"] / len(xs) if xs else None}


# ───────────── DB ─────────────

_ADDED_COLS = (("ordered_at", "TIMESTAMPTZ"), ("ordered_by", "TEXT"), ("po_ext_id", "TEXT"))   # 2026-11-13 発注書を出した記録


def ensure_table(conn) -> None:
    """表・列が無い時だけ DDL（毎回 DDL を出すと他タブの居残りトランザクションでロック待ちになり得る・page34 2026-08-07 障害）。"""
    if conn.execute("SELECT to_regclass(?) AS r", (TABLE,)).fetchone()["r"] is None:
        conn.execute("CREATE SCHEMA IF NOT EXISTS sourcing")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} ("
            "id BIGSERIAL PRIMARY KEY, jan TEXT NOT NULL, item_name TEXT, qty INTEGER NOT NULL, note TEXT, "
            "requested_by TEXT, requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), "
            "status TEXT NOT NULL DEFAULT 'requested', supplier TEXT, unit_price NUMERIC(14,2), confirmed_qty INTEGER, "
            "confirmed_by TEXT, confirmed_at TIMESTAMPTZ, cancelled_by TEXT, cancelled_at TIMESTAMPTZ)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_new_item_request_jan ON {TABLE} (jan)")
    have = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'sourcing' "
        "AND table_name = 'new_item_request'").fetchall()}
    for col, typ in _ADDED_COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {col} {typ}")
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
        "pol.quantity_received, pol.expected_receipt_date, pol.closed, pol.vendor_name, pol.rate, pol.amount "
        "FROM nst.purchase_order_line pol JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id "
        "WHERE regexp_replace(trim(im.jan), '^0{5,}', '') = ANY(?) AND pol.trandate IS NOT NULL", (jans,)).fetchall()
    return [dict(r) for r in rows]


def load_supplier_sources(conn, jans: list[str]) -> tuple[dict, dict]:
    """(主供 = JAN → (略称, 正式名), 最近 PO = JAN → vendor_name)。sourcing の表が無ければ主供は空。"""
    jans = sorted({j for j in jans if j})
    if not jans:
        return {}, {}
    try:
        main = {r["jan"]: (r["supplier_name"], r["official_name"]) for r in conn.execute(
            "SELECT m.jan, m.supplier_name, s.official_name FROM sourcing.item_main_supplier m "
            "LEFT JOIN sourcing.supplier s ON s.supplier_name = m.supplier_name WHERE m.jan = ANY(?)", (jans,)).fetchall()}
    except Exception:  # noqa: BLE001 — page34 を開いたことが無い環境（表なし）では主供なし
        conn.rollback()
        main = {}
    po = {r["jan"]: r["vendor_name"] for r in conn.execute(
        "SELECT DISTINCT ON (jan) jan, vendor_name FROM ("
        "SELECT regexp_replace(trim(im.jan), '^0{5,}', '') AS jan, pol.vendor_name, pol.trandate, pol.po_number "
        "FROM nst.purchase_order_line pol JOIN nst.item_master_raw im ON im.internal_id = pol.item_internal_id "
        "WHERE pol.vendor_name IS NOT NULL) x WHERE jan = ANY(?) ORDER BY jan, trandate DESC, po_number DESC",
        (jans,)).fetchall()}
    return main, po


def insert_requests(conn, rows: list[dict], user: str) -> list[int]:
    """rows = {jan, qty, note, item_name, unit_price}（単価は申請時の参考値・確認で変えられる）。→ 作った id。"""
    ids = []
    for r in rows:
        ids.append(conn.execute(
            f"INSERT INTO {TABLE} (jan, item_name, qty, note, unit_price, requested_by) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (r["jan"], r.get("item_name") or None, int(r["qty"]), r.get("note") or None, r.get("unit_price"),
             user)).fetchone()["id"])
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


def mark_ordered(conn, items: list[dict], user: str) -> int:
    """発注書 CSV を出した品 → 発注書の時の 仕入先・単価・数量 と 外部 ID を保存し「已发注待同步」へ（Boss 2026-11-13）。
    items = {id, supplier, unit_price, confirmed_qty, po_ext_id}。確認済で未記録のものだけ。→ 件数。"""
    n = 0
    for it in items:
        n += len(conn.execute(
            f"UPDATE {TABLE} SET supplier = ?, unit_price = ?, confirmed_qty = ?, ordered_at = NOW(), ordered_by = ?, "
            "po_ext_id = ? WHERE id = ? AND status = 'confirmed' AND ordered_at IS NULL RETURNING id",
            (it["supplier"], it["unit_price"], int(it["confirmed_qty"]), user, it.get("po_ext_id"),
             int(it["id"]))).fetchall())
    conn.commit()
    return n


def unmark_ordered(conn, ids: list[int]) -> int:
    """「↩️ 退回待发注」: 発注書を出した記録を消す（CSV を NST に入れなかった時）。"""
    n = len(conn.execute(f"UPDATE {TABLE} SET ordered_at = NULL, ordered_by = NULL, po_ext_id = NULL "
                         "WHERE id = ANY(?) AND status = 'confirmed' RETURNING id", ([int(i) for i in ids],)).fetchall())
    conn.commit()
    return n


def cancel(conn, ids: list[int], user: str) -> int:
    n = len(conn.execute(
        f"UPDATE {TABLE} SET status = 'cancelled', cancelled_by = ?, cancelled_at = NOW() "
        "WHERE id = ANY(?) AND status <> 'cancelled' RETURNING id", (user, [int(i) for i in ids])).fetchall())
    conn.commit()
    return n
