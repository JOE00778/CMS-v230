"""自建站上架 · 候选品（NST）与台账（PG）。

候选 = 商品等级 NEW · 取扱中 · 非無効 · 本体行（item_code = jan）· JAN 8/13 位（14 位 = 整箱 = 组合，排除）
       · 定義原価 > 1 · 厂商已定（brand.item_vendor_brand）· Shopify 查无此 JAN（在页面层用 existing_jans 去掉）。
handling_cd 与 item_rank 是两个独立字段，两个都要筛（2026-11 实证：只看等级会把取扱中止误上架）。
"""
from __future__ import annotations

from shared.shopify_listing import spec as S

LEDGER = "shopify_listing.ledger"

_SQL = """
SELECT im.jan, im.display_name AS nst_name, im.cost::float AS cost,
       ivb.vendor, ivb.brand, inv.stock
FROM nst.item_master_raw im
LEFT JOIN brand.item_vendor_brand ivb ON ivb.jan = im.jan
LEFT JOIN (
    SELECT sku_code, sum(qty_qualified_available)::int AS stock
    FROM banma.inventory_snapshot
    WHERE snapshot_date = (SELECT max(snapshot_date) FROM banma.inventory_snapshot)
    GROUP BY 1) inv ON inv.sku_code = im.jan
WHERE im.item_rank = 'NEW' AND im.handling_cd = '取扱中' AND NOT im.is_inactive
  AND im.item_code = im.jan AND im.jan ~ '^([0-9]{8}|[0-9]{13})$'
"""


def load(conn) -> tuple[list[dict], dict]:
    """→ (候选[dict, 含预填列 + brand_tags], 排除计数{原因: 件数})。"""
    rows = [dict(r) for r in conn.execute(_SQL).fetchall()]
    out, skipped = [], {"原価缺失(空或≤1)": 0, "厂商未定": 0}
    for r in rows:
        price = S.price_usd(r.get("cost"))
        if price is None:
            skipped["原価缺失(空或≤1)"] += 1
            continue
        vendor, brand = (r.get("vendor") or "").strip(), (r.get("brand") or "").strip()
        if not vendor:
            skipped["厂商未定"] += 1
            continue
        tags = []
        for x in (vendor, brand):
            t = "brand-" + S.slug(x) if x and S.slug(x) else ""
            if t and t not in tags:
                tags.append(t)
        out.append({"jan": r["jan"], "nst_name": r.get("nst_name") or "", "vendor": vendor, "brand": brand,
                    "brand_display": S.display_name(vendor, brand), "price_usd": price,
                    "stock": r.get("stock"), "brand_tags": tags})
    return out, skipped


def ensure_ledger(conn) -> None:
    """台账表不存在时才建（page48 同款：每次都发 DDL 会撞别的 tab 的居残り事务锁）。"""
    if conn.execute("SELECT to_regclass(?) AS r", (LEDGER,)).fetchone()["r"] is None:
        conn.execute("CREATE SCHEMA IF NOT EXISTS shopify_listing")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {LEDGER} ("
            "id BIGSERIAL PRIMARY KEY, at TIMESTAMPTZ NOT NULL DEFAULT NOW(), operator TEXT, batch TEXT, "
            "action TEXT NOT NULL, jan TEXT, product_id TEXT, handle TEXT, ok BOOLEAN, detail TEXT)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_shopify_listing_ledger_jan ON {LEDGER} (jan)")
        conn.commit()


def log(conn, *, operator: str, batch: str, action: str, jan: str, product_id: str = "", handle: str = "",
        ok: bool, detail: str = "") -> None:
    """一件一条、写完即 commit（长批次中途被杀也不丢已写记录）。"""
    conn.execute(f"INSERT INTO {LEDGER} (operator, batch, action, jan, product_id, handle, ok, detail) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (operator, batch, action, jan, product_id, handle, ok, detail[:2000]))
    conn.commit()


def recent(conn, limit: int = 300) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT at, operator, batch, action, jan, handle, product_id, ok, detail FROM {LEDGER} "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
