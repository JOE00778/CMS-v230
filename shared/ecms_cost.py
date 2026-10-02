"""ECMS 輸送費の DB 書き込みと配賦（page29 から呼ぶ · SQL はここに閉じる）.

流れは JD と同じ:
    xlsx 明細 + PDF 請求書 → raw テーブル（year_month 単位で洗替）
      → order_no で NST の店舗へ寄せる → logistics.cost_monthly → page28 / page05

**店舗の寄せ方**（2026-10-02 実測で決めた）:
    order_no → nst.invoice_order → nst.sales_invoice.shop
    page05 の「京东费用」と同じ経路。ECMS の請求書は注文番号を持っているので、
    JD のような「包裹番号 → 注文番号」の中間マッピングは要らない。
    実測 1,071 行中 1,065 行（99.4%）が NST で店舗に当たった。

    ⚠️ 斑马（logistics.order_shop_map）は使わない。同じ 1,071 行で 133 行（12.4%）しか
    当たらない。斑马の取得が 2026-08-29 に停止され Coupang 分が 8/31 までしか無いため。

    ⚠️ Coupang と自社サイトの注文番号は**どちらも 13-14 桁の数字で見分けがつかない**
    （Shopify US: 6808851906775 / Coupang: 10100173210749）。だから番号の形では判定せず、
    NST の店舗帰属に任せる。請求書に自社サイト韓国の注文が混ざっても自動で分かれる。
"""
from __future__ import annotations

# 配賦の母集団。xlsx の費目列と対応（ecms_invoice.XLSX_AMOUNT_COLS と同じ並び）
_DETAIL_COST_TYPES = ("ecms_freight", "ecms_fuel", "ecms_permit",
                      "ecms_ese_care", "ecms_au_surcharge", "ecms_other")

# 店舗が引けなかった明細の置き場（JD の '(未マッチ包裹)' に相当）
UNMATCHED_SHOP = "(未突合注文)"
# 明細を持たない請求書費目（賠償など）の置き場
UNALLOCATED_SHOP = "(請求書のみ・明細なし)"
AMBIGUOUS_SHOP = "(要確認・複数店舗)"


# DB 列名 → normalize_detail() が出す dict のキー。
# ⚠️ 名前がズレても INSERT は通り、その列が黙って NULL になるだけ
# （実際に length/width/height ↔ length_cm/... でやらかした）。対応表を 1 箇所に置く。
_DETAIL_COL_MAP = (
    ("tracking_no", "tracking_no"), ("order_no", "order_no"), ("n_pkg", "n_pkg"),
    ("ecms_freight", "ecms_freight"), ("ecms_fuel", "ecms_fuel"),
    ("ecms_permit", "ecms_permit"), ("ecms_ese_care", "ecms_ese_care"),
    ("ecms_au_surcharge", "ecms_au_surcharge"), ("ecms_other", "ecms_other"),
    ("total_jpy", "total_jpy"),
    ("chargeable_kg", "chargeable_kg"), ("gross_kg", "gross_kg"),
    ("volume_kg", "volume_kg"),
    ("length_cm", "length"), ("width_cm", "width"), ("height_cm", "height"),
    ("destination", "destination"), ("ship_date", "ship_date"),
)


def detail_row_keys() -> tuple[str, ...]:
    """normalize_detail() が持っていなければならないキー（テストで突き合わせる）。"""
    return tuple(src for _, src in _DETAIL_COL_MAP)


def replace_detail(conn, year_month: str, rows: list[dict], source_file: str) -> int:
    """xlsx 明細を year_month 単位で洗い替える。戻り値 = 挿入行数。

    year_month と source_file は呼び出し側の引数から入れる（明細側は持っていない）。
    """
    conn.execute("DELETE FROM logistics.ecms_invoice_detail WHERE year_month = %s",
                 (year_month,))
    if not rows:
        conn.commit()
        return 0
    db_cols = ["year_month"] + [c for c, _ in _DETAIL_COL_MAP] + ["source_file"]
    ph = ", ".join(["%s"] * len(db_cols))
    conn.executemany(
        f"INSERT INTO logistics.ecms_invoice_detail ({', '.join(db_cols)}) VALUES ({ph})",
        [(year_month,) + tuple(r.get(src) for _, src in _DETAIL_COL_MAP) + (source_file,)
         for r in rows])
    conn.commit()
    return len(rows)


def replace_header(conn, year_month: str, header, detail_total, source_file: str) -> None:
    """PDF 請求書ヘッダ + 費目行を year_month 単位で洗い替える。

    航空運賃は xlsx 明細と同じ金額なので is_detailed=TRUE を立て、配賦では明細側だけ使う
    （両方積むと二重計上になる）。
    """
    conn.execute("DELETE FROM logistics.ecms_invoice_line   WHERE year_month = %s", (year_month,))
    conn.execute("DELETE FROM logistics.ecms_invoice_header WHERE year_month = %s", (year_month,))
    conn.execute(
        "INSERT INTO logistics.ecms_invoice_header "
        "(year_month, invoice_no, issue_date, tax_amount, total_amount, detail_total, source_file) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (year_month, header.invoice_no, header.issue_date,
         header.tax, header.total, detail_total, source_file))
    if header.lines:
        conn.executemany(
            "INSERT INTO logistics.ecms_invoice_line "
            "(year_month, seq, item_name, cost_type, amount, tax_class, is_detailed) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [(year_month, l.seq, l.item_name, l.cost_type, l.amount, l.tax_class,
              l.cost_type == "ecms_freight") for l in header.lines])
    conn.commit()


# 明細 → 店舗。order_no が複数店舗に当たる場合は AMBIGUOUS に落とす
# （page05 の JD 費用は LIMIT 1 で黙って 1 つ選んでいるが、ここでは表に出す）。
_ALLOC_SQL = f"""
INSERT INTO logistics.cost_monthly
    (year_month, dept, shop, cost_type, amount, qty, source, computed_at)
SELECT x.year_month,
       COALESCE(d.dept, '不明'),
       x.shop,
       x.cost_type,
       SUM(x.amount),
       COUNT(*),
       'ecms',
       now()
FROM (
    SELECT t.year_month,
           CASE WHEN s.n = 1 THEN s.shop
                WHEN s.n > 1 THEN '{AMBIGUOUS_SHOP}'
                ELSE '{UNMATCHED_SHOP}' END AS shop,
           u.cost_type,
           u.amount
    FROM logistics.ecms_invoice_detail t
    CROSS JOIN LATERAL (VALUES
        ('ecms_freight',      t.ecms_freight),
        ('ecms_fuel',         t.ecms_fuel),
        ('ecms_permit',       t.ecms_permit),
        ('ecms_ese_care',     t.ecms_ese_care),
        ('ecms_au_surcharge', t.ecms_au_surcharge),
        ('ecms_other',        t.ecms_other)
    ) AS u(cost_type, amount)
    LEFT JOIN LATERAL (
        SELECT min(trim(si.shop)) AS shop, count(DISTINCT trim(si.shop)) AS n
        FROM nst.invoice_order io
        JOIN nst.sales_invoice si ON si.invoice_id = io.invoice_id
        WHERE io.order_no = t.order_no
          AND si.shop IS NOT NULL AND btrim(si.shop) <> ''
    ) s ON TRUE
    WHERE t.year_month = %s
      AND u.amount <> 0
) x
LEFT JOIN logistics.shop_dept_map d ON d.shop = x.shop
GROUP BY x.year_month, COALESCE(d.dept, '不明'), x.shop, x.cost_type
"""

# 請求書にしか無い費目（賠償など）。明細が無いので店舗には割れない。
_ALLOC_HEADER_SQL = f"""
INSERT INTO logistics.cost_monthly
    (year_month, dept, shop, cost_type, amount, qty, source, computed_at)
SELECT year_month, '不明', '{UNALLOCATED_SHOP}', cost_type, SUM(amount), COUNT(*),
       'ecms', now()
FROM logistics.ecms_invoice_line
WHERE year_month = %s AND is_detailed = FALSE AND amount <> 0
GROUP BY year_month, cost_type
"""


def recompute(conn, year_month: str) -> list[tuple]:
    """対象月の ECMS 分だけ cost_monthly を作り直す。戻り値 = (shop, cost_type, amount, qty)。

    ⚠️ JD 分（source='recompute'）には触らない。ECMS 分は source='ecms' で区別する。
    """
    conn.execute(
        "DELETE FROM logistics.cost_monthly WHERE year_month = %s AND source = 'ecms'",
        (year_month,))
    conn.execute(_ALLOC_SQL, (year_month,))
    conn.execute(_ALLOC_HEADER_SQL, (year_month,))
    conn.commit()
    cur = conn.execute(
        "SELECT shop, cost_type, SUM(amount), SUM(qty) FROM logistics.cost_monthly "
        "WHERE year_month = %s AND source = 'ecms' "
        "GROUP BY shop, cost_type ORDER BY SUM(amount) DESC",
        (year_month,))
    return [tuple(r) for r in cur.fetchall()]


def reconcile(conn, year_month: str) -> dict:
    """請求書と明細の突合結果。page29 が取り込み直後に見せる。"""
    cur = conn.execute(
        "SELECT COALESCE(SUM(ecms_freight + ecms_fuel + ecms_permit + ecms_ese_care "
        "       + ecms_au_surcharge + ecms_other), 0), COUNT(*) "
        "FROM logistics.ecms_invoice_detail WHERE year_month = %s", (year_month,))
    detail_sum, detail_rows = cur.fetchone()
    cur = conn.execute(
        "SELECT invoice_no, total_amount, tax_amount FROM logistics.ecms_invoice_header "
        "WHERE year_month = %s", (year_month,))
    h = cur.fetchone()
    cur = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM logistics.ecms_invoice_line "
        "WHERE year_month = %s AND cost_type = 'ecms_freight'", (year_month,))
    freight = cur.fetchone()[0]
    cur = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM logistics.cost_monthly "
        "WHERE year_month = %s AND source = 'ecms'", (year_month,))
    allocated = cur.fetchone()[0]
    return {
        "detail_rows": int(detail_rows or 0),
        "detail_sum": float(detail_sum or 0),
        "invoice_no": (h[0] if h else "") or "",
        "invoice_total": float(h[1]) if h else 0.0,
        "invoice_tax": float(h[2]) if h else 0.0,
        "invoice_freight": float(freight or 0),
        "allocated": float(allocated or 0),
    }
