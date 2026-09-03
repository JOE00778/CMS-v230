"""請求書バックフィル — page29 tab1 と同一ロジック（AST 抽出で関数を共有）。

ブラウザアップロードが通らない月のサーバ側実行用（2026-06 で初使用 · Boss 承認済の型）。
コンテナ内で:  docker exec cms_streamlit python tools/backfill_invoice.py <YYYY-MM> <xlsx path>
1〜5 月回灌(2026-08-30)・6 月(2026-09-02)と同じ手順:
解析 → cost_invoice_raw/cost_billing 差替 → 斑马精確取数 → SO 帰類 → 佐川 → 全月重算。
"""
import ast, io, re, sys, datetime, pathlib
sys.path.insert(0, "/app")
import openpyxl
from shared import banma_client, nst_suiteql
from shared.db import get_connection

YM, FILE = sys.argv[1], sys.argv[2]
assert re.fullmatch(r"20\d{2}-\d{2}", YM), f"YYYY-MM 形式で: {YM}"

conn = get_connection()

src = pathlib.Path("/app/pages/29_🚚_物流費用上传.py").read_text()
tree = ast.parse(src)
want = {"_load_wb", "parse_invoice", "parse_billing", "find_col",
        "_cell", "_num", "_to_date", "run_recompute"}
ns = {"io": io, "re": re, "openpyxl": openpyxl, "conn": conn,
      "date": datetime.date, "datetime": datetime.datetime,
      "timedelta": datetime.timedelta, "timezone": datetime.timezone}
got = set()
for n in tree.body:
    if isinstance(n, ast.FunctionDef) and n.name in want:
        exec(compile(ast.Module([n], []), "<page29>", "exec"), ns)
        got.add(n.name)
    if isinstance(n, ast.Assign) and any(
            isinstance(t_, ast.Name) and t_.id == "INV_SHEETS"
            for t_ in n.targets):
        exec(compile(ast.Module([n], []), "<page29>", "exec"), ns)
        got.add("INV_SHEETS")
missing = (want | {"INV_SHEETS"}) - got
assert not missing, f"AST 抽出漏れ: {missing}"

data = open(FILE, "rb").read()
print(f"file {len(data)/1e6:.1f}MB", flush=True)

# ── 1. 請求書 → cost_invoice_raw / cost_billing（page29 と同じ DELETE+INSERT）──
rows, per = ns["parse_invoice"](data, YM)
assert rows, "解析 0 行"
for ct in {r[1] for r in rows}:
    conn.execute("DELETE FROM logistics.cost_invoice_raw WHERE year_month=%s AND cost_type=%s", (YM, ct))
conn.executemany(
    """INSERT INTO logistics.cost_invoice_raw
       (year_month, cost_type, join_key, amount_ex_tax, amount_in_tax,
        material_cd, material_qty, sku, cost_date)
       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
bill = ns["parse_billing"](data, YM)
if bill:
    conn.execute("DELETE FROM logistics.cost_billing WHERE year_month=%s", (YM,))
    conn.executemany(
        """INSERT INTO logistics.cost_billing
           (year_month, seq, item_name, amount_ex_tax, amount_in_tax)
           VALUES (%s,%s,%s,%s,%s)""", bill)
conn.commit()
print(f"invoice rows={len(rows)} per={per} billing={len(bill)}", flush=True)

# ── 2. 斑马 精確取数 ──
banma_client.ensure_store_map_table(conn)
keys = banma_client.missing_join_keys(conn, [YM])
print(f"banma: 未匹配 {len(keys)} 単号", flush=True)
if keys:
    def cb(done, total):
        if done % 5 == 0 or done == total:
            print(f"  banma batch {done}/{total}", flush=True)
    r = banma_client.fetch_shop_map_by_keys(conn, keys, cb)
    print(f"banma: requested={r['requested']} fetched={r['fetched']} "
          f"upserted={r['upserted']} batches={r['batches']}", flush=True)

# ── 3. SO 帰類（NST 店舗字段 · 未設定は NST直販）──
so_keys = [k for k in banma_client.missing_join_keys(conn, [YM]) if re.match(r"^SO\d+", k)]
if so_keys and nst_suiteql.is_configured():
    names = nst_suiteql.lookup_so_shops([k.split("_")[0] for k in so_keys])
    so_rows = [{"parcel_no": k, "order_id": k.split("_")[0], "waybill_no": None,
                "platform": "NST", "shop": names[k.split("_")[0]], "ship_date": None}
               for k in so_keys if k.split("_")[0] in names]
    if so_rows:
        conn.cursor().executemany(banma_client.UPSERT_SHOP_MAP, so_rows)
        conn.commit()
    left = [k for k in so_keys if k.split("_")[0] not in names]
    print(f"SO: {len(so_rows)} 件帰類 / NST に無い {len(left)} 件 {left[:5]}", flush=True)

# ── 4. 佐川 12 位 → 佐川直送(EC) ──
sagawa = [k for k in banma_client.missing_join_keys(conn, [YM])
          if re.fullmatch(r"\d{12}", banma_client.strip_seq_suffix(k))]
if sagawa:
    conn.cursor().executemany(
        banma_client.UPSERT_SHOP_MAP,
        [{"parcel_no": k, "order_id": None, "waybill_no": k,
          "platform": "Sagawa", "shop": "佐川直送", "ship_date": None} for k in sagawa])
    conn.commit()
print(f"佐川: {len(sagawa)} 件", flush=True)

# ── 5. 重算（全月）──
ns["run_recompute"](None)
conn.commit()
print("recompute done", flush=True)

# ── 6. verify ──
for r in conn.execute(
        "SELECT dept, sum(amount)::bigint FROM logistics.cost_monthly "
        "WHERE year_month=%s GROUP BY 1 ORDER BY 1", (YM,)).fetchall():
    print(f"  {YM} {r[0] if not hasattr(r,'keys') else r['dept']}: "
          f"{r[1] if not hasattr(r,'keys') else list(r.values())[1]:,}", flush=True)
rest = banma_client.missing_join_keys(conn, [YM])
print(f"残り未匹配 join_key: {len(rest)}", flush=True)
print("DONE", flush=True)
