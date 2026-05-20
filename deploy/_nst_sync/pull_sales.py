"""店舗別売上（日次）→ PG nst.sales_daily / 再集計 → nst.sales_monthly

レポート【輸出】店舗別売上_JO を SuiteQL で再現（2026-05-20 実機照合・数量完全一致）。
2026-05-20 日次化: trandate を日別に保持（sales_daily）。月次（sales_monthly）は本表から再集計。

照合で確定した抽出条件:
  · 輸出判定 = 顧客(customer)の custentity_fb_dept = 4（輸出事業）
                ★ 明細部門(transactionline.department)ではない
  · 取引タイプ = CustInvc + CashSale + CustCred（返品含めて純額）
  · アイテム種類 = InvtPart（在庫アイテム）
  · 店舗 = BUILTIN.DF(custbody_fb_ne_ro_shop)
  · 数量 = -SUM(quantity)（売上行はマイナス符号）
  · 売上 = -SUM(foreignamount × exchangerate)（取引時レートで JPY 換算）

照合結果（2026-04 Shopee PH）: 数量 16,401 完全一致 / 売上 ¥18,548,234（file ¥18,550,920・差0.014%）

writes:
  1. UPSERT nst.sales_daily（shop × item × sale_date）← 単一事実源
  2. 再集計 UPSERT nst.sales_monthly（shop × item × year_month）← sales_daily から SUM
"""
from __future__ import annotations

import datetime as dt
import logging

from .audit import record_error
from .client import NSTClient

log = logging.getLogger(__name__)

_SUITEQL = """
SELECT
    BUILTIN.DF(t.custbody_fb_ne_ro_shop)        AS shop,
    tl.item                                     AS item_internal_id,
    TO_CHAR(t.trandate, 'YYYY-MM-DD')           AS sale_date,
    -SUM(tl.quantity)                           AS qty_sold,
    -SUM(tl.foreignamount * t.exchangerate)     AS revenue
FROM transactionline tl
JOIN transaction t ON t.id = tl.transaction
JOIN customer cust ON cust.id = t.entity
WHERE cust.custentity_fb_dept = 4
  AND t.type IN ('CustInvc', 'CashSale', 'CustCred')
  AND tl.itemtype = 'InvtPart'
  AND tl.item IS NOT NULL
  AND t.custbody_fb_ne_ro_shop IS NOT NULL
  AND t.trandate >= TO_DATE('{start}', 'YYYY-MM-DD')
  AND t.trandate <= TO_DATE('{end}', 'YYYY-MM-DD')
GROUP BY BUILTIN.DF(t.custbody_fb_ne_ro_shop), tl.item, TO_CHAR(t.trandate, 'YYYY-MM-DD')
"""

_UPSERT_DAILY_SQL = """
INSERT INTO nst.sales_daily (
    shop, item_internal_id, sale_date, qty_sold, revenue, pulled_at
) VALUES (
    %(shop)s, %(item_internal_id)s, %(sale_date)s, %(qty_sold)s, %(revenue)s, %(pulled_at)s
)
ON CONFLICT (shop, item_internal_id, sale_date) DO UPDATE SET
    qty_sold  = EXCLUDED.qty_sold,
    revenue   = EXCLUDED.revenue,
    pulled_at = EXCLUDED.pulled_at
"""

# 当月分を sales_daily から再集計して sales_monthly へ UPSERT（月次は派生）
_RECALC_MONTHLY_SQL = """
INSERT INTO nst.sales_monthly (
    shop, item_internal_id, year_month, qty_sold, revenue, pulled_at
)
SELECT shop, item_internal_id, %(ym)s,
       SUM(qty_sold), SUM(revenue), %(pulled_at)s
FROM nst.sales_daily
WHERE sale_date >= %(start)s AND sale_date <= %(end)s
GROUP BY shop, item_internal_id
ON CONFLICT (shop, item_internal_id, year_month) DO UPDATE SET
    qty_sold  = EXCLUDED.qty_sold,
    revenue   = EXCLUDED.revenue,
    pulled_at = EXCLUDED.pulled_at
"""


def _month_bounds(since: str | None) -> tuple[str, str, str]:
    """対象月を決める。since 指定なし→当月。戻り: (start, end, year_month)。"""
    if since:
        d = dt.date.fromisoformat(since[:10])
    else:
        d = dt.date.today()
    start = d.replace(day=1)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    end = nxt - dt.timedelta(days=1)
    return start.isoformat(), end.isoformat(), start.strftime("%Y-%m")


def run(*, client: NSTClient, db_url: str, since: str | None, dry_run: bool,
        run_id: int = 0) -> dict:
    if dry_run:
        rows = client.suiteql("SELECT 1 AS ok FROM dual", limit=1)
        return {"dry_run": True, "ping": "ok" if rows else "no-rows", "domain": "sales"}

    start, end, ym = _month_bounds(since)
    sql = _SUITEQL.format(start=start, end=end)
    raw_rows = client.suiteql(sql, limit=500000)
    pulled_at = dt.datetime.now(dt.timezone.utc)

    if not db_url:
        return {"domain": "sales", "fetched": len(raw_rows), "note": "db_url 空 · skip 写入"}

    try:
        import psycopg
    except ImportError:
        return {"domain": "sales", "fetched": len(raw_rows), "error": "psycopg 未装"}

    upserted = errors = 0
    with psycopg.connect(db_url) as conn:
        cur = conn.cursor()
        for i, raw in enumerate(raw_rows, 1):
            try:
                cur.execute(_UPSERT_DAILY_SQL, {
                    "shop": raw.get("shop"),
                    "item_internal_id": str(raw.get("item_internal_id") or "").strip(),
                    "sale_date": raw.get("sale_date"),
                    "qty_sold": _num(raw.get("qty_sold")),
                    "revenue": _num(raw.get("revenue")),
                    "pulled_at": pulled_at,
                })
                upserted += 1
            except Exception as e:
                errors += 1
                record_error(db_url=db_url, run_id=run_id, domain="sales",
                             row_number=i, message=str(e), raw_row=raw)
        # 当月分を sales_daily から再集計 → sales_monthly（月次は派生）
        cur.execute(_RECALC_MONTHLY_SQL,
                    {"ym": ym, "start": start, "end": end, "pulled_at": pulled_at})
        monthly_rows = cur.rowcount
        conn.commit()

    return {
        "domain": "sales",
        "year_month": ym,
        "fetched": len(raw_rows),
        "upserted_daily": upserted,
        "monthly_recalc": monthly_rows,
        "errors": errors,
    }


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
