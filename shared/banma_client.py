"""斑马 ERP 开放平台客户端（物流费配赋专用 · 只读）。

用途只有一个（Boss 2026-08-29 拍板）：上传 JD 请求书时，按费用发生窗口从
斑马拉包裹（GET /v1/order/package），生成 logistics.order_shop_map 行，
替代 BM 手动导出。不做定时同步、不落任何新拉取表。

  ensure_token(conn)                    token 经 PG banma.api_token 缓存
                                        （与 database 仓 banma_api 共用同一张表）
  fetch_shop_map_by_keys(conn, keys)    按请求书 join_key 精确批量拉取（主路径 ·
                                        Boss 2026-08-29「先入库再只拉需要的」：
                                        IDs / OrderDisplayID 各支持 200 个/批，
                                        4 万单号 ≈ 200 次调用 ≈ 2 分钟）
  iter_packages(client, start, end)     按 CreateTime 窗口分页迭代（回灌用后备）
  package_to_row(pkg, shop_by_store)    包裹 → order_shop_map 行（PII 不落地）
  invoice_window(dates, ym)             请求书费用日期 → 拉取窗口（后备）

凭据 BANMA_APP_ID / BANMA_APP_SECRET（元川 .env → compose 映射，照 ECMS_* 先例）。
签名/重试与 database 仓 data_warehouse/banma_api/client.py 同算法：
  - SHA256(method + path + sorted(k=v&) + timestamp + body) 小写 hex，末尾带 &
  - 429/5xx/网络例外/非 JSON 200 指数退避（2026-08-28 全 ingester 统一判据）
  - AccessToken 3 天 / RefreshToken 30 天（实测 · 文档的 7 天是错的）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator

GATEWAY = "https://gateway.banmaerp.com"
PAGE_SIZE = 50                      # API 上限
RETRY_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_MARGIN = dt.timedelta(hours=24)    # token 期限先読み更新（TZ ずれ吸収）


class BanmaError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Banma HTTP {status}: {body[:300]}")
        self.status = status


class BanmaAuthError(BanmaError):
    """token 全失効 → ERP 画面（服务>开放平台>APP管理）で手動更新が必要。"""


class BanmaNotConfigured(BanmaError):
    """BANMA_APP_ID / BANMA_APP_SECRET 未配置（元川 .env / compose 映射）。"""

    def __init__(self):
        super().__init__(0, "BANMA_APP_ID / BANMA_APP_SECRET 未配置")


def _secret(name: str) -> str:
    """优先 streamlit secrets，fallback env var（与 shared.ecms_client 一致）。
    页面外（テスト等）でも import できるよう streamlit は遅延 import。"""
    try:
        import streamlit as st
        v = st.secrets.get(name, None)
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name, "")


def is_configured() -> bool:
    return bool(_secret("BANMA_APP_ID") and _secret("BANMA_APP_SECRET"))


class BanmaClient:
    def __init__(self, app_id: str, app_secret: str,
                 min_interval: float = 0.25):     # 5QPS/app に余裕
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = ""
        self.min_interval = min_interval
        self._last_call = 0.0

    @classmethod
    def from_env(cls) -> "BanmaClient":
        app_id, secret = _secret("BANMA_APP_ID"), _secret("BANMA_APP_SECRET")
        if not app_id or not secret:
            raise BanmaNotConfigured()
        return cls(app_id, secret)

    # ── 署名（database 仓 banma_api/client.py と同一アルゴリズム）──
    def sign_params(self, method: str, path: str, params: dict,
                    timestamp: str, body: str = "") -> str:
        """**URL エンコード前の生値**で署名する（文書の JS 例と同じ）。

        ⚠️ エンコード後の値で署名すると、非 ASCII を含む単号で必ず
        `401 invalid sign` になる。2026-08-30 実測: 1〜5 月の請求書に
        `CB用商品`（JD 手書き行）が居て、`CB%E7%94%A8...` で署名 →
        サーバは復号値で検証するため不一致 → バッチ全体が 401。
        7 月は全単号が ASCII の unreserved 文字だったので露呈しなかった。
        """
        p = {"app_id": self.app_id, "app_secret": self.app_secret}
        for k, v in (params or {}).items():
            p[k.lower()] = str(v)
        text = method.upper() + path
        for k in sorted(p):
            text += f"{k}={p[k]}&"
        text += str(timestamp)
        if body:
            text += body
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def sign(self, method: str, path: str, query: str,
             timestamp: str, body: str = "") -> str:
        """query 文字列版（署名ベクトル検証・後方互換用）。"""
        d = {}
        if query:
            for pair in query.split("&"):
                k, _, v = pair.partition("=")
                d[k] = v
        return self.sign_params(method, path, d, timestamp, body)

    def _throttle(self) -> None:
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def call(self, method: str, path: str, params: dict | None = None,
             max_retries: int = 5):
        """→ 応答 envelope の Data。再試行判据は 2026-08-28 全 ingester 統一形。"""
        query = urllib.parse.urlencode(params, doseq=True, safe=":,-T") \
            if params else ""
        url = GATEWAY + path + (("?" + query) if query else "")
        last_err: Exception | None = None
        for attempt in range(max_retries):
            self._throttle()
            ts = str(int(time.time()))
            headers = {
                "X-BANMA-APP-ID": self.app_id,
                "X-BANMA-TIMESTAMP": ts,
                # 署名は生値（params）で作る——query はエンコード済みなので使わない
                "X-BANMA-SIGN": self.sign_params(method, path, params or {}, ts),
                "X-BANMA-SIGN-METHOD": "SHA256",
                "Content-Type": "application/json",
            }
            if self.access_token:
                headers["X-BANMA-ACCESS-TOKEN"] = self.access_token
            req = urllib.request.Request(url, method=method.upper(),
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                # ⚠️ HTTPError ⊂ URLError ⊂ OSError なのでこの branch が必ず先
                text = e.read().decode("utf-8", "replace")
                if e.code in RETRY_HTTP_STATUS:
                    last_err = e
                    time.sleep(float(2 ** attempt))
                    continue
                if e.code == 401:
                    raise BanmaAuthError(e.code, text) from e
                raise BanmaError(e.code, text) from e
            except (OSError, http.client.HTTPException,
                    json.JSONDecodeError) as e:
                # read 段の裸 TimeoutError / 接続断 / 非 JSON 200。GET のみで冪等
                last_err = e
                if attempt + 1 >= max_retries:
                    break
                time.sleep(float(2 ** attempt))
                continue
            if not data.get("Success"):
                code = int(data.get("Code") or 0)
                msg = str(data.get("Message") or "")
                if code == 401 or "token" in msg.lower():
                    raise BanmaAuthError(code, msg)
                raise BanmaError(code, msg)
            return data.get("Data")
        raise BanmaError(0, f"retries exhausted: {last_err}") from last_err


# ══════════════════════════════════════════════════════════
# token（banma.api_token 共用 · database 仓 ensure_token と同ロジック）
# ══════════════════════════════════════════════════════════

def parse_dt(v) -> dt.datetime | None:
    if not v:
        return None
    s = str(v).strip().replace("T", " ")[:19]
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def ensure_token(conn, client: BanmaClient,
                 now: dt.datetime | None = None) -> str:
    """PG キャッシュ → 期限近ければ Refresh → 無ければ GetToken。
    期限は中国標準時 naive（Boss 2026-08-28 確定：斑马サーバ＝中国）。"""
    now = now or dt.datetime.now(
        dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
    row = conn.execute(
        "SELECT access_token, access_expiry, refresh_token, refresh_expiry "
        "FROM banma.api_token WHERE app_id = %s", (client.app_id,)).fetchone()
    if row and row[1] and row[1] - _MARGIN > now:
        client.access_token = row[0]
        return row[0]
    if row and row[2] and row[3] and row[3] > now:
        try:
            data = client.call("GET", "/v1/Auth/RefreshToken",
                               {"refreshToken": row[2]})
            return _store_token(conn, client, data, now)
        except BanmaError:
            pass                                    # GetToken に回退
    data = client.call("GET", "/v1/Auth/GetToken")
    return _store_token(conn, client, data, now)


def _store_token(conn, client: BanmaClient, data: dict,
                 now: dt.datetime) -> str:
    access = (data or {}).get("AccessToken") or ""
    access_exp = parse_dt((data or {}).get("AccessTokenExpiryTime"))
    if not access or (access_exp and access_exp <= now):
        raise BanmaAuthError(
            401, "token 完全失効: ERP 画面（服务>开放平台>APP管理）で手動更新")
    conn.execute(
        "INSERT INTO banma.api_token (app_id, access_token, access_expiry, "
        "refresh_token, refresh_expiry, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, NOW()) "
        "ON CONFLICT (app_id) DO UPDATE SET "
        "access_token = EXCLUDED.access_token, "
        "access_expiry = EXCLUDED.access_expiry, "
        "refresh_token = EXCLUDED.refresh_token, "
        "refresh_expiry = EXCLUDED.refresh_expiry, updated_at = NOW()",
        (client.app_id, access, access_exp,
         data.get("RefreshToken"), parse_dt(data.get("RefreshTokenExpiryTime"))))
    conn.commit()
    client.access_token = access
    return access


# ══════════════════════════════════════════════════════════
# 包裹 → order_shop_map 行
# ══════════════════════════════════════════════════════════

def invoice_window(cost_dates: list[dt.date | None], ym: str,
                   pad_days: int = 10) -> tuple[str, str]:
    """请求书费用日期 min/max ± pad → 斑马拉取窗口（ISO 秒精度）。
    费用日期全空时 fallback 对象月 ± pad。"""
    ds = [d for d in cost_dates if d]
    if ds:
        lo, hi = min(ds), max(ds)
    else:
        y, m = int(ym[:4]), int(ym[5:7])
        lo = dt.date(y, m, 1)
        hi = (dt.date(y + 1, 1, 1) if m == 12 else dt.date(y, m + 1, 1)) \
            - dt.timedelta(days=1)
    pad = dt.timedelta(days=pad_days)
    return (f"{lo - pad:%Y-%m-%d}T00:00:00", f"{hi + pad:%Y-%m-%d}T23:59:59")


def iter_packages(client: BanmaClient, start: str, end: str,
                  progress: Callable[[int, int], None] | None = None,
                  ) -> Iterator[dict]:
    """GET /v1/order/package を CreateTime 窓で分頁イテレート（1 件ずつ yield）。
    progress(page, page_count) で UI 進捗を更新できる。"""
    page = 1
    while True:
        data = client.call("GET", "/v1/order/package", {
            "PageNumber": page, "PageSize": PAGE_SIZE,
            "SearchTimeField": "CreateTime",
            "SearchTimeStart": start, "SearchTimeEnd": end,
            "SortField": "CreateTime", "SortBy": "ASC",
        })
        items = (data or {}).get("Packages") or []
        pg = (data or {}).get("Page") or {}
        if progress:
            progress(page, int(pg.get("PageCount") or page))
        for it in items:
            yield it
        if not pg.get("HasMore"):
            return
        page += 1


def package_to_row(item: dict, shop_by_store: dict[str, str]) -> dict | None:
    """包裹 1 件 → order_shop_map 行。PII（收件人姓名/电话/地址等）不读取不保存。

    shop 名の優先順: logistics.banma_store_map（BM 店名との対照表）
    → banma.store.name → StoreID 生値。⚠️ BM 導出の店名と store.name は
    同一 StoreID でも不一致（2026-08-29 実測）——対照表が正、store.name は
    新店舗が対照表に無い間の暫定値（tab② の未分類フローで Boss が分類）。
    """
    pkg = item.get("Package") or item
    pid = pkg.get("ID")
    if not pid:
        return None
    store_id = str(pkg.get("StoreID") or "")
    order_id = None
    for d in item.get("Details") or []:
        if d.get("OrderDisplayID"):
            order_id = str(d["OrderDisplayID"])
            break
    ship = parse_dt(pkg.get("DeliveryTime")) or parse_dt(pkg.get("CreateTime"))
    return {
        "parcel_no": str(pid),
        "order_id": order_id,
        "waybill_no": (str(pkg.get("ExpressNo")) if pkg.get("ExpressNo") else None),
        "platform": (str(pkg.get("Platform")) if pkg.get("Platform") else None),
        "shop": shop_by_store.get(store_id) or store_id or None,
        "ship_date": ship.date() if ship else None,
    }


def load_shop_by_store(conn) -> dict[str, str]:
    """StoreID → shop 名。対照表（banma_store_map）優先、無い店は store.name。"""
    out: dict[str, str] = {}
    try:
        for sid, name in conn.execute(
                "SELECT id::text, name FROM banma.store"):
            if name:
                out[sid] = name
    except Exception:
        conn.rollback()                 # banma schema 不在でも動く
    for sid, shop in conn.execute(
            "SELECT store_id, shop FROM logistics.banma_store_map"):
        out[sid] = shop
    return out


UPSERT_SHOP_MAP = """
INSERT INTO logistics.order_shop_map
    (parcel_no, order_id, waybill_no, platform, shop, ship_date)
VALUES (%(parcel_no)s, %(order_id)s, %(waybill_no)s,
        %(platform)s, %(shop)s, %(ship_date)s)
ON CONFLICT (parcel_no) DO UPDATE SET
    order_id = EXCLUDED.order_id, waybill_no = EXCLUDED.waybill_no,
    platform = EXCLUDED.platform, shop = EXCLUDED.shop,
    ship_date = EXCLUDED.ship_date, imported_at = now()
"""


# 批量照会の 1 批あたり文字数予算。API 仕様は 200 個/批だが、ゲートウェイ前段の
# IIS が query ≤2,048 字符しか受けない（2026-08-29 実測: 2,029 OK / 2,429 → 404
# の gb2312 HTML）。19 位 ID ×200 = 4,029 字符で即死するため、個数でなく
# 文字数で切る。1,600 字符 ≈ 19 位 ID 80 個 / 楽天 26 位単号 59 個。
BATCH_CHAR_BUDGET = 1600
BATCH_MAX = 200                     # API 仕様上の個数上限


def chunk_by_budget(keys: list[str], budget: int = BATCH_CHAR_BUDGET,
                    hard_max: int = BATCH_MAX) -> list[list[str]]:
    """カンマ連結後が budget 字符以内に収まるように分割。"""
    out: list[list[str]] = []
    cur: list[str] = []
    used = 0
    for k in keys:
        add = len(k) + (1 if cur else 0)
        if cur and (used + add > budget or len(cur) >= hard_max):
            out.append(cur)
            cur, used = [], 0
            add = len(k)
        cur.append(k)
        used += add
    if cur:
        out.append(cur)
    return out


import re as _re

_SEQ_SUFFIX = _re.compile(r"^(.{6,}?)[_-](\d{1,3})$")


def strip_seq_suffix(key: str) -> str:
    """JD 請求書の連番後綴 `_1`/`-1`〜`_999`/`-999` を外して照会用 base を得る。

    実例（2026-08-30 Boss 指摘）: `260723CNMX7QSX_1`（Shopee）、
    `4101058683725-2`（Coupang · 連字符版も実在）——同一注文の複数行に
    JD が振る連番で、base が平台注文番号（OrderDisplayID で斑马命中を実証）。
    ⚠️ 1-3 位に限定する理由: NST 直録の `SO00504371_7458145`（7 位の内部 ID）や
    楽天注文番号 `269580-20260607-0488932098`（尾部 10 位）を誤って剥がないため。
    後綴なしの key はそのまま返る。
    """
    m = _SEQ_SUFFIX.match(key)
    return m.group(1) if m else key


def is_parcel_id(key: str) -> bool:
    """join_key が斑马の包裹 ID か（19 位の雪花数字）。
    それ以外（Coupang 13/14 位注文番号等 · 2026-05 以降の請求書に混在）は
    OrderDisplayID として検索する。"""
    return key.isdigit() and len(key) == 19


def fetch_shop_map_by_keys(conn, keys: list[str],
                           progress: Callable[[int, int], None] | None = None,
                           ) -> dict:
    """請求書の join_key 集合だけを斑马から精確取得 → order_shop_map upsert。

    実測（2026-08-29）: IDs は**包裹 ID** で過滤する（文書の「订单ID」は誤記 ·
    実在 2+架空 1 を投げて実在 2 だけ返った）。OrderDisplayID は Coupang
    注文番号から包裹を逆引きできる。どちらも 200 個/批。
    バッチ毎に upsert + commit（冪等 · 中断後の再実行無害）。
    """
    keys = [str(k).strip() for k in keys if k]
    # 連番後綴 `_N` を剥いだ base で照会し、結果は元 key ごとに 1 行ずつ書く
    # （parcel_no=元 key にすることで recompute の join がそのまま命中する）
    by_base: dict[str, list[str]] = {}
    for k in keys:
        by_base.setdefault(strip_seq_suffix(k), []).append(k)
    pids = [b for b in by_base if is_parcel_id(b)]
    oids = [b for b in by_base if not is_parcel_id(b)]
    batches = ([("IDs", c) for c in chunk_by_budget(pids)]
               + [("OrderDisplayID", c) for c in chunk_by_budget(oids)])
    if not batches:
        return {"requested": 0, "fetched": 0, "upserted": 0, "batches": 0}

    client = BanmaClient.from_env()
    ensure_token(conn, client)
    shop_by_store = load_shop_by_store(conn)
    cur = conn.cursor()
    fetched = upserted = 0
    for i, (param, chunk) in enumerate(batches, 1):
        page = 1
        while True:                      # 200 個指定でも PageSize=50 → 最大 4 頁
            data = client.call("GET", "/v1/order/package", {
                "PageNumber": page, "PageSize": PAGE_SIZE,
                param: ",".join(chunk),
            })
            items = (data or {}).get("Packages") or []
            rows: list[dict] = []
            for it in items:
                r = package_to_row(it, shop_by_store)
                if not r:
                    continue
                # 応答 → 照会 base（IDs は包裹 ID、OrderDisplayID は注文番号）
                base = r["parcel_no"] if param == "IDs" else (r["order_id"] or "")
                for orig in by_base.get(base, [r["parcel_no"]]):
                    rows.append({**r, "parcel_no": orig,
                                 "order_id": r["order_id"] or base})
            if rows:
                cur.executemany(UPSERT_SHOP_MAP, rows)
                conn.commit()
                upserted += len(rows)
            fetched += len(items)
            if not ((data or {}).get("Page") or {}).get("HasMore"):
                break
            page += 1
        if progress:
            progress(i, len(batches))
    return {"requested": len(keys), "fetched": fetched,
            "upserted": upserted, "batches": len(batches)}


def missing_join_keys(conn, yms: list[str]) -> list[str]:
    """対象月の請求書行のうち、parcel_no でも order_id でも未マッチの join_key。"""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT r.join_key
           FROM logistics.cost_invoice_raw r
           LEFT JOIN logistics.order_shop_map mp ON r.join_key = mp.parcel_no
           WHERE r.year_month = ANY(%s) AND r.join_key IS NOT NULL
             AND mp.parcel_no IS NULL
             AND NOT EXISTS (SELECT 1 FROM logistics.order_shop_map o
                             WHERE o.order_id = r.join_key)""", (yms,))]


def ensure_store_map_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS logistics.banma_store_map ("
        "store_id TEXT PRIMARY KEY, "
        "shop     TEXT NOT NULL, "          # = BM 導出/shop_dept_map の店名口径
        "updated_at TIMESTAMPTZ DEFAULT NOW())")
    conn.commit()


def fill_shop_map_from_banma(conn, start: str, end: str,
                             progress: Callable[[int, int], None] | None = None,
                             flush_every: int = 500) -> dict:
    """窓内の包裹を斑马から取得 → order_shop_map へ upsert（ページ毎に途中 commit）。
    戻り値: {fetched, upserted, window}。"""
    client = BanmaClient.from_env()
    ensure_token(conn, client)
    shop_by_store = load_shop_by_store(conn)
    cur = conn.cursor()
    fetched = upserted = 0
    buf: list[dict] = []
    for item in iter_packages(client, start, end, progress):
        fetched += 1
        row = package_to_row(item, shop_by_store)
        if row:
            buf.append(row)
        if len(buf) >= flush_every:
            cur.executemany(UPSERT_SHOP_MAP, buf)
            conn.commit()
            upserted += len(buf)
            buf.clear()
    if buf:
        cur.executemany(UPSERT_SHOP_MAP, buf)
        conn.commit()
        upserted += len(buf)
    return {"fetched": fetched, "upserted": upserted, "window": f"{start}..{end}"}
