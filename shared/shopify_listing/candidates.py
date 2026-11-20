"""自建站上架 · 候选品（NST）与台账（PG）。

候选 = 商品等级 NEW · 取扱中 · 非無効 · 本体行（item_code = jan）· JAN 8/13 位（14 位 = 整箱 = 组合，排除）
       · 定義原価 > 1 · Shopify 查无此 JAN（在页面层用 existing_jans 去掉）。
厂商/品牌取 brand.item_vendor_brand（Boss 拍板口径）；表里没有的由 AI 按规则填，上传时标需人工确认。
⚠️ brand schema 只读账号 cms_reader 无权限 → 用 get_connection() 读。
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
    """→ (候选[dict, 含预填列 + vendor_known], 排除计数{原因: 件数})。"""
    rows = [dict(r) for r in conn.execute(_SQL).fetchall()]
    out, skipped = [], {"原価缺失(空或≤1)": 0}
    for r in rows:
        price = S.price_usd(r.get("cost"))
        if price is None:
            skipped["原価缺失(空或≤1)"] += 1
            continue
        vendor, brand = (r.get("vendor") or "").strip(), (r.get("brand") or "").strip()
        # 厂商未进口径表（10 月之后的新 NEW 品）→ 厂商/品牌留给 AI 填，上传时要人工确认
        out.append({"jan": r["jan"], "nst_name": r.get("nst_name") or "", "vendor": vendor, "brand": brand,
                    "brand_display": S.display_name(vendor, brand) if vendor else "", "price_usd": price,
                    "stock": r.get("stock"), "vendor_known": bool(vendor)})
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


def drafted_ids(conn, jans: list[str]) -> dict[str, str]:
    """JAN → 本页最近一次建草稿成功的 product_id（Shopify 搜索索引延迟时挂图用）。"""
    if not jans:
        return {}
    rows = conn.execute(
        f"SELECT DISTINCT ON (jan) jan, product_id FROM {LEDGER} WHERE action = 'create_draft' AND ok "
        "AND jan = ANY(?) ORDER BY jan, id DESC", (list(jans),)).fetchall()
    return {r["jan"]: r["product_id"] for r in rows}


def recent(conn, limit: int = 300) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT at, operator, batch, action, jan, handle, product_id, ok, detail FROM {LEDGER} "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
