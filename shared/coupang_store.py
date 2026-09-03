"""coupang_shipment_queue / coupang_product_info の読み書き。

queue は **PII を持つ**（受取人氏名・電話・住所・PCCC）。Boss 2026-08-30 の方針で
「発送が終われば消してよい・1 週間残ればいい」→ `purge_old()` を取り込みのたびに回す。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from shared.db import get_connection

RETENTION_DAYS = 7          # PII の保持期間。Boss 2026-08-30 指示


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------
# 商品マスタ
# ------------------------------------------------------------------
def upsert_products(rows: list[dict]) -> int:
    """キーは **SKU**（`JAN_入数`）。同じ JAN でも規格違いは別 OptionID・別英語品名。"""
    if not rows:
        return 0
    valid = [r for r in rows if r.get("sku")]
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO coupang_product_info"
            " (sku, jan, pack, name_en, weight_kg, brand, hscode, product_id, option_id,"
            "  updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(r["sku"], r.get("jan") or str(r["sku"]).split("_")[0], r.get("pack"),
              r.get("name_en"), r.get("weight_kg"), r.get("brand"), r.get("hscode"),
              r.get("product_id"), r.get("option_id"), _now()) for r in valid],
        )
        conn.commit()
        return len(valid)
    finally:
        conn.close()


def product_map() -> dict[str, dict]:
    """SKU → マスタ 1 行。件数はたかだか数千なので丸ごと読んで dict にする。"""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT sku, jan, pack, name_en, weight_kg, brand, hscode, product_id, option_id"
            " FROM coupang_product_info").fetchall()
        return {str(r["sku"]): dict(r) for r in rows}
    finally:
        conn.close()


def nst_master_map(jans: list[str]) -> tuple[dict[str, dict], str]:
    """JAN → {maker, weight(g)}。戻り値は (マップ, エラー文字列)。

    品牌 = `nst.item_master_raw.maker`
    毛重 = **`jdl.v_goods_dimensions.wms_gross_weight_g`**（倉庫実測。page02「毛重(g)」と同じ）
           ⚠️ `nst.item_master_raw` に `weight` 列は**無い**。2026-09-03 に存在しない列を
           SELECT していて、しかも例外を握り潰していたため品牌と毛重が両方とも空のまま
           46 行出力された。**エラーは握り潰さず呼び出し元に返す**。

    jan は一意ではない（15,589 行 / 15,473 distinct）ので、重量が取れた行を優先する。
    """
    if not jans:
        return {}, ""
    conn = get_connection()
    try:
        marks = ",".join("?" * len(jans))
        rows = conn.execute(
            "SELECT im.jan, im.maker, gd.wms_gross_weight_g AS weight"
            " FROM nst.item_master_raw im"
            " LEFT JOIN jdl.v_goods_dimensions gd ON gd.jan = im.jan"
            f" WHERE im.jan IN ({marks})", tuple(jans)).fetchall()
    except Exception as e:                       # SQLite 本機や列変更を握り潰さない
        return {}, str(e)
    finally:
        conn.close()

    out: dict[str, dict] = {}
    for r in rows:
        jan = str(r["jan"])
        cur = out.get(jan)
        # 同じ JAN が複数行ある。重量が取れている行を優先
        if cur is None or (cur.get("weight") is None and r["weight"] is not None):
            out[jan] = {"maker": r["maker"], "weight": r["weight"]}
    return out, ""


# ------------------------------------------------------------------
# 発送キュー
# ------------------------------------------------------------------
def upsert_queue(rows: list[dict]) -> tuple[int, int]:
    """取り込み。**すでに sent の行は上書きしない**（送信済みの記録を引き直しで壊さない）。

    戻り値は (新規・更新した件数, 送信済みで飛ばした件数)。
    """
    if not rows:
        return 0, 0
    existing = {(r["order_id"], r["shipment_box_id"]): r["ecms_status"]
                for r in list_queue()}
    fresh, skipped = [], 0
    for r in rows:
        key = (r["order_id"], r["shipment_box_id"])
        if existing.get(key) == "sent":
            skipped += 1
            continue
        fresh.append(r)
    if not fresh:
        return 0, skipped

    conn = get_connection()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO coupang_shipment_queue"
            " (order_id, shipment_box_id, ordered_at, coupang_status, receiver_name,"
            "  receiver_phone, receiver_postcode, receiver_addr, addr_sido, addr_sigungu,"
            "  addr_detail, pccc, pccc_kind, items_json, total_krw, total_usd, weight_kg,"
            "  fx_rate, ecms_status, ecms_reference, note, pulled_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["order_id"], r["shipment_box_id"], r.get("ordered_at"),
              r.get("coupang_status"), r.get("receiver_name"), r.get("receiver_phone"),
              r.get("receiver_postcode"), r.get("receiver_addr"), r.get("addr_sido"),
              r.get("addr_sigungu"), r.get("addr_detail"), r.get("pccc"), r.get("pccc_kind"),
              json.dumps(r.get("items") or [], ensure_ascii=False),
              r.get("total_krw"), r.get("total_usd"), r.get("weight_kg"), r.get("fx_rate"),
              r.get("ecms_status") or "pending", r.get("ecms_reference"), r.get("note"),
              r["pulled_at"], _now()) for r in fresh],
        )
        conn.commit()
        return len(fresh), skipped
    finally:
        conn.close()


def list_queue(status: str | None = None) -> list[dict]:
    conn = get_connection()
    try:
        sql = "SELECT * FROM coupang_shipment_queue"
        params: tuple = ()
        if status:
            sql += " WHERE ecms_status = ?"
            params = (status,)
        sql += " ORDER BY ordered_at DESC, order_id"
        out = []
        for r in conn.execute(sql, params).fetchall():
            d = dict(r)
            try:
                d["items"] = json.loads(d.get("items_json") or "[]")
            except json.JSONDecodeError:
                d["items"] = []
            out.append(d)
        return out
    finally:
        conn.close()


def update_row(order_id: str, box_id: str, **fields) -> None:
    """画面で直した住所や PCCC を書き戻す。渡された列だけ更新する。"""
    allowed = {"receiver_name", "receiver_phone", "receiver_postcode", "addr_sido",
               "addr_sigungu", "addr_detail", "pccc", "weight_kg", "ecms_status",
               "ecms_reference", "note"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    conn = get_connection()
    try:
        cols = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(
            f"UPDATE coupang_shipment_queue SET {cols}, updated_at = ?"
            " WHERE order_id = ? AND shipment_box_id = ?",
            (*sets.values(), _now(), order_id, box_id))
        conn.commit()
    finally:
        conn.close()


def purge_old(days: int = RETENTION_DAYS) -> int:
    """PII を持つ行を期限切れで消す。戻り値は消した件数。"""
    cutoff = (datetime.now(timezone.utc).astimezone()
              - timedelta(days=days)).isoformat(timespec="seconds")
    conn = get_connection()
    try:
        before = conn.execute(
            "SELECT count(*) FROM coupang_shipment_queue WHERE pulled_at < ?",
            (cutoff,)).fetchone()[0]
        conn.execute("DELETE FROM coupang_shipment_queue WHERE pulled_at < ?", (cutoff,))
        conn.commit()
        return int(before or 0)
    finally:
        conn.close()


# ------------------------------------------------------------------
# 品牌の日→英
# ------------------------------------------------------------------
def brand_alias_map() -> dict[str, str]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT maker, brand_en FROM coupang_brand_alias").fetchall()
        return {str(r["maker"]): r["brand_en"] for r in rows if r["brand_en"]}
    finally:
        conn.close()


def upsert_brand_alias(pairs: dict[str, str]) -> int:
    """{メーカー名: 英語品牌}。空文字の行は削除する。"""
    conn = get_connection()
    try:
        add = [(k, v.strip(), _now()) for k, v in pairs.items() if k and v and v.strip()]
        drop = [(k,) for k, v in pairs.items() if k and not (v or "").strip()]
        if add:
            conn.executemany(
                "INSERT OR REPLACE INTO coupang_brand_alias (maker, brand_en, updated_at)"
                " VALUES (?,?,?)", add)
        if drop:
            conn.executemany("DELETE FROM coupang_brand_alias WHERE maker = ?", drop)
        conn.commit()
        return len(add)
    finally:
        conn.close()
