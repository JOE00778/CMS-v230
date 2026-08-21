"""cms_api — CMS 只读/事件 API sidecar（给 N8N workflow 调）

补 README v2.0 缺口 ①② + automation_runs 回调（T-309 设计但未实装的 endpoint）。

挂在 docker external network smikie_shared，N8N 通过 http://cms-api:8789/ 访问。
不暴露公网。

后端选择（按 shared/db.py 同款逻辑）：
  - 有 DATABASE_URL=postgresql://... → Postgres（Inspiron 部署用，主路径）
  - 否则 → SQLite 文件 CMS_DB_PATH（Mac 本地开发兜底）

端点：
  GET  /health
  POST /api/automation/callback
  POST /api/automation/xlsx-upload
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# psycopg2 在 SQLite 模式下不会被使用，但导入失败应是软错误（Mac 开发不强求装）
try:
    import psycopg2
    import psycopg2.extras
    _HAS_PG = True
except ImportError:
    _HAS_PG = False

# ────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH = Path(os.environ.get("CMS_DB_PATH", "/data/warehouse.db"))
OUTPUTS_DIR = Path(os.environ.get("CMS_OUTPUTS_DIR", "/data/outputs"))
CMS_PUBLIC_BASE = os.environ.get("CMS_PUBLIC_BASE", "https://smikie-cms.cc")

IS_PG = DATABASE_URL.startswith(("postgresql://", "postgres://"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cms_api")

app = FastAPI(title="Smikie CMS API sidecar", version="0.2.0")


@contextmanager
def _conn():
    """统一连接 context manager；返回带 row_factory（key 访问）的连接。"""
    if IS_PG:
        if not _HAS_PG:
            raise HTTPException(500, "DATABASE_URL=postgres 但 psycopg2 未安装")
        c = psycopg2.connect(DATABASE_URL)
        c.cursor_factory = psycopg2.extras.RealDictCursor
        try:
            yield c
        finally:
            c.close()
    else:
        if not DB_PATH.exists():
            raise HTTPException(503, f"SQLite warehouse.db 不存在: {DB_PATH}")
        c = sqlite3.connect(str(DB_PATH))
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()


def _qmark(sql: str) -> str:
    """SQLite 用 `?`，Postgres 用 `%s`。SQL 写 `?`，按后端自动替换。"""
    return sql.replace("?", "%s") if IS_PG else sql


def _placeholders(n: int) -> str:
    return ",".join(["%s"] * n) if IS_PG else ",".join(["?"] * n)


def _safe_jan_list(raw: str) -> list[str]:
    """防 SQL injection：白名单 JAN 字符（数字 + 字母 + dash + underscore）"""
    jans = [j.strip() for j in raw.split(",") if j.strip()]
    safe = [j for j in jans if re.fullmatch(r"[\w\-]+", j)]
    if len(safe) != len(jans):
        log.warning("dropped %d malformed JAN values", len(jans) - len(safe))
    return safe


def _row_get(row, key: str, default=None):
    """SQLite Row 和 psycopg2 RealDictRow 都支持 row[key]，但访问语义有差。"""
    try:
        v = row[key]
        return v if v is not None else default
    except (KeyError, IndexError):
        return default


@app.on_event("startup")
def _startup():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    log.info(f"backend = {'postgres' if IS_PG else 'sqlite'}")
    if IS_PG:
        safe_url = re.sub(r":[^:@]*@", ":***@", DATABASE_URL)
        log.info(f"DATABASE_URL = {safe_url}")
    else:
        log.info(f"CMS_DB_PATH = {DB_PATH} (exists={DB_PATH.exists()})")
    log.info(f"CMS_OUTPUTS_DIR = {OUTPUTS_DIR}")


# ────────────────────────────────────────────────────────────────
# GET /health
# ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    backend = "postgres" if IS_PG else "sqlite"
    ok = True
    item_count = None
    err = None
    try:
        with _conn() as c:
            cur = c.cursor() if IS_PG else c
            # 単一事実源へ切替（旧 item_v2 は廃止予定 · 2026-08-18）
            cur.execute("SELECT COUNT(*) AS n FROM nst.item_master_raw")
            row = cur.fetchone()
            item_count = row["n"] if row else None
    except Exception as e:
        ok = False
        err = str(e)[:200]
        log.error("health db probe failed: %s", e)
    return {
        "status": "ok" if ok else "degraded",
        "backend": backend,
        "item_count": item_count,
        "error": err,
        "outputs_dir": str(OUTPUTS_DIR),
    }


# ────────────────────────────────────────────────────────────────
# GET /api/sku/master — 2026-08-21 削除
#   唯一のデータ源 v_item_master → item_v2 が 2026-05-09 の一度きりの
#   取込(3,854行)で以後未更新。全 SKU の 24% しか返せず、on_hand/on_order は
#   5月9日時点の値だった。呼び出し元は CMS 27ページ・4リポジトリ・
#   n8n 9 workflow のいずれにも無し(実測)。item_v2 の廃止に伴い削除。
#   再び必要になれば nst.item_master_raw + nst.inventory_snapshot で作り直す。
# ────────────────────────────────────────────────────────────────

# ────────────────────────────────────────────────────────────────
# POST /api/automation/callback
# ────────────────────────────────────────────────────────────────
class CallbackReq(BaseModel):
    run_id: str
    module: str
    status: str
    summary: dict[str, Any] | None = None
    message: str | None = None


@app.post("/api/automation/callback")
def automation_callback(req: CallbackReq):
    now = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    summary_json = json.dumps(req.summary, ensure_ascii=False) if req.summary else None
    completed_at = now if req.status in ("completed", "failed") else None

    with _conn() as c:
        cur = c.cursor() if IS_PG else c
        update_sql = _qmark(
            "UPDATE automation_runs SET status=?, summary=COALESCE(?, summary), "
            "completed_at=COALESCE(?, completed_at) WHERE run_id=?"
        )
        cur.execute(update_sql, (req.status, summary_json, completed_at, req.run_id))
        # SQLite cursor.rowcount 与 psycopg2 cursor.rowcount 行为一致
        if cur.rowcount == 0:
            insert_sql = _qmark(
                "INSERT INTO automation_runs "
                "(run_id, module, status, summary, triggered_by, triggered_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"
            )
            cur.execute(
                insert_sql,
                (req.run_id, req.module, req.status, summary_json, "n8n-callback", now, completed_at),
            )
        c.commit()
    return {"run_id": req.run_id, "status": req.status, "updated_at": now}


# ────────────────────────────────────────────────────────────────
# POST /api/automation/xlsx-upload
# ────────────────────────────────────────────────────────────────
@app.post("/api/automation/xlsx-upload")
async def xlsx_upload(
    file: UploadFile = File(...),
    x_run_id: str = Header(..., alias="X-Run-Id"),
):
    if not re.fullmatch(r"[\w\-]+", x_run_id):
        raise HTTPException(400, "invalid X-Run-Id")
    safe_name = re.sub(r"[^\w\.\-]", "_", file.filename or "upload.xlsx")
    dst = OUTPUTS_DIR / f"{x_run_id}_{safe_name}"
    body = await file.read()
    dst.write_bytes(body)
    log.info("xlsx saved: %s (%d bytes)", dst, len(body))
    return {
        "run_id": x_run_id,
        "saved_path": str(dst),
        "bytes": len(body),
        "filename": dst.name,
    }


# ────────────────────────────────────────────────────────────────
# POST /v1/kaihai/confirm （T-318 · 改廃確認 N8N 化）
#
# CMS page 13 按钮 → N8N webhook → 本端点：
#   1) UPDATE discontinue_alerts (acknowledged_by/at/action)
#   2) action == 取扱中止 → 連動停売（⚠️ 現在機能していない。下の詳細コメント参照）
#   3) 返回联动结果给 N8N（N8N 据此组装飞书消息）
#
# 原 page 13 直写 SQLite 的 handle_action() 逻辑已迁到这里，page 13 退化为 webhook 调用方。
# LEGACY_KAIHAI=true 时 page 13 走老路径（紧急回退），本端点继续可用（互不干扰）。
# ────────────────────────────────────────────────────────────────
class KaihaiConfirmReq(BaseModel):
    run_id: str
    jan: str
    source: str
    signal_type: str
    detected_at: str
    action: str  # 取扱中止 | 継続 | 代替品調査
    operator: str = "BOSS"


_KAIHAI_ACTIONS = {"取扱中止", "継続", "代替品調査"}


@app.post("/v1/kaihai/confirm")
def kaihai_confirm(req: KaihaiConfirmReq):
    if req.action not in _KAIHAI_ACTIONS:
        raise HTTPException(400, f"action must be one of {_KAIHAI_ACTIONS}, got {req.action!r}")
    if not re.fullmatch(r"[\w\-]+", req.jan):
        raise HTTPException(400, "invalid jan")

    ts = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"

    with _conn() as c:
        cur = c.cursor() if IS_PG else c

        # 1) UPDATE discontinue_alerts
        upd_alerts = _qmark(
            "UPDATE discontinue_alerts "
            "SET acknowledged_by = ?, acknowledged_at = ?, action = ? "
            "WHERE jan = ? AND source = ? AND signal_type = ? AND detected_at = ? "
            "AND acknowledged_by IS NULL"
        )
        cur.execute(upd_alerts, (req.operator, ts, req.action, req.jan,
                                 req.source, req.signal_type, req.detected_at))
        updated_alerts = cur.rowcount

        # 2) 連動停売（取扱中止 のときだけ）
        #
        # ⚠️ 2026-08-21 時点で**この連動は機能していない**。直し方が未決のため
        #    ロジックは当時のまま残し、診断だけここに記録する。
        #
        #    現状の二重の壊れ:
        #      ・public.item_master は 0 行の空テーブル（SQLite 時代の名残）。
        #        rowcount は常に 0 → linkage_applied も常に false。例外は出ない。
        #      ・値 '停売' は NST のランク語彙に無い。
        #        実在するのは Aランク/Bランク/Cランク/NEW/取扱中止 の 5 種のみ。
        #
        #    実害: 2026-05-18 に 4901301447647（KAO ビオレUV アクアリッチ）を
        #    取扱中止と判定したが、NST 側は Bランク のまま 3ヶ月放置されている。
        #
        #    宛先を nst.item_master_raw.item_rank に変えるのは**不可**。
        #    ingester(items_daily · 毎日 03:02) が
        #      ON CONFLICT (internal_id) DO UPDATE SET item_rank = EXCLUDED.item_rank
        #    で NetSuite から全量上書きするため、書いた値は同日中に消える。
        #
        #    正しい形は nst.item_msrp と同じ「独立テーブル + jan で関連」方式
        #    （015_create_item_msrp.sql の設計コメント参照:
        #     「item_master_raw を汚さない」）。CMS 側の判定を別テーブルに持ち、
        #    発注 AI・在庫健全度などの読み出し側で overlay する。
        #    → テーブル設計と読み出し側の改修が要るため Boss 判断待ち。
        linkage_applied = False
        if req.action == "取扱中止":
            upd_rank = _qmark("UPDATE item_master SET rank = ? WHERE jan = ?")
            cur.execute(upd_rank, ("停売", req.jan))
            linkage_applied = cur.rowcount > 0

        c.commit()

    log.info("kaihai_confirm: run_id=%s jan=%s action=%s updated_alerts=%d linkage=%s",
             req.run_id, req.jan, req.action, updated_alerts, linkage_applied)

    return {
        "run_id": req.run_id,
        "jan": req.jan,
        "action": req.action,
        "operator": req.operator,
        "updated_alerts": updated_alerts,
        "linkage_applied": linkage_applied,
        "ts": ts,
    }


# ────────────────────────────────────────────────────────────────
# Shopee tokens persistence （v2.3 给 shopee-mass-upload n02b 节点用）
# 文件存储，0 DB 改动；每次 N8N workflow 触发时 refresh 一次 access_token
# 并把新的 refresh_token 持久化（refresh_token 每次也会变）。
# ────────────────────────────────────────────────────────────────
SHOPEE_TOKENS_FILE = OUTPUTS_DIR / "shopee_tokens.json"
# Shopee Open Platform v2 host（默认 Live；Test 环境 .env 覆盖为 sandbox host）
#   Test (东南亚 7 国): https://openplatform.sandbox.test-stable.shopee.sg
#   Test (中国大陆):     https://openplatform.sandbox.test-stable.shopee.cn
#   Live (东南亚 7 国): https://openplatform.shopee.sg
SHOPEE_API_BASE = os.environ.get("SHOPEE_API_BASE", "https://openplatform.shopee.sg")
SHOPEE_PARTNER_ID = os.environ.get("SHOPEE_PARTNER_ID", "")
# Shopee partner_key 处理（v2.10 修正 v2.7 的 strip 假设）：
#   Test API Partner Key 显示为 'shpk' + 60 char hex，shpk 是 key 本体的一部分
#   而不是 UI prefix；签名时整段 64 char 直接 utf-8 encode 当 HMAC-SHA256 key
#   与 Shopee 官方 Python SDK (pyshopee2) 一致：hmac.new(partner_key.encode(), ...)
SHOPEE_PARTNER_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "")


@app.get("/api/automation/shopee/tokens")
def get_shopee_tokens():
    if SHOPEE_TOKENS_FILE.exists():
        try:
            return json.loads(SHOPEE_TOKENS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("read shopee_tokens.json failed: %s", e)
            return {"refresh_tokens": {}, "updated_at": None, "error": str(e)}
    return {"refresh_tokens": {}, "updated_at": None}


class ShopeeTokensReq(BaseModel):
    refresh_tokens: dict[str, str]
    updated_at: str | None = None


@app.put("/api/automation/shopee/tokens")
def put_shopee_tokens(req: ShopeeTokensReq):
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    body = {
        "refresh_tokens": req.refresh_tokens,
        "updated_at": req.updated_at or dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    SHOPEE_TOKENS_FILE.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": True, "path": str(SHOPEE_TOKENS_FILE), "markets": list(req.refresh_tokens.keys())}


# ────────────────────────────────────────────────────────────────
# Shopee OAuth helper（v2.4 新增）
# Boss 拿到 Partner ID/Key 后，从 cms-api 拿 7 国授权链接 → 浏览器登录 → 自动回写 refresh_token
# 替代手工构造 OAuth URL 和手工换 token 的繁琐流程
# ────────────────────────────────────────────────────────────────
def _shopee_sign(base_string: str) -> str:
    """HMAC-SHA256(partner_key.utf8, base_string.utf8) → hex.

    与 Shopee 官方 Python SDK (pyshopee2.client._sign) 完全一致。
    partner_key 直接 utf-8 encode，不做 strip / hex decode。
    """
    import hashlib
    import hmac

    return hmac.new(
        SHOPEE_PARTNER_KEY.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@app.get("/api/automation/shopee/debug-sign")
def debug_sign():
    """诊断端点：返回当前 host / partner_id / base_string / sign，定位环境配置问题。

    Wrong sign 常见根因（v2.10 起按此优先级排查）：
      1. SHOPEE_API_BASE host 不对（v2 Test 入口必须用 openplatform.sandbox.test-stable.shopee.sg
         而非 partner.test-stable.shopeemobile.com — 后者是 v1 老域名，partner_id 不识别）
      2. partner_id / partner_key 跨 Test/Live 串用
      3. partner_key 复制时被截断或混入不可见字符（首尾 repr 可见）
    """
    path = "/api/v2/shop/auth_partner"
    ts = int(time.time())
    base = f"{SHOPEE_PARTNER_ID}{path}{ts}"
    pk = SHOPEE_PARTNER_KEY
    return {
        "api_base": SHOPEE_API_BASE,
        "partner_id": SHOPEE_PARTNER_ID,
        "partner_key_len": len(pk),
        "partner_key_first6": repr(pk[:6]),
        "partner_key_last4": repr(pk[-4:]),
        "base_string": base,
        "timestamp": ts,
        "sign": _shopee_sign(base),
    }


@app.get("/api/automation/shopee/oauth-url/{market}")
def shopee_oauth_url(market: str, redirect: str | None = None):
    """生成某国的 Shopee OAuth 授权链接。

    Shopee 公式: HMAC-SHA256(partner_id + path + ts, partner_key)
    path = /api/v2/shop/auth_partner
    """
    if not SHOPEE_PARTNER_ID or not SHOPEE_PARTNER_KEY:
        raise HTTPException(503, "SHOPEE_PARTNER_ID/KEY 未在 cms-api 容器 env 设置")
    path = "/api/v2/shop/auth_partner"
    ts = int(time.time())
    base = f"{SHOPEE_PARTNER_ID}{path}{ts}"
    sign = _shopee_sign(base)
    cb = redirect or f"{CMS_PUBLIC_BASE}/api/automation/shopee/oauth-callback?market={market}"
    url = (
        f"{SHOPEE_API_BASE}{path}"
        f"?partner_id={SHOPEE_PARTNER_ID}&timestamp={ts}&sign={sign}&redirect={cb}"
    )
    return {
        "market": market,
        "authorize_url": url,
        "redirect_url": cb,
        "instruction": (
            f"1) 浏览器打开 authorize_url 登录 Shopee {market} 卖家账号；"
            f"2) 授权后 Shopee 会跳转 redirect_url，带上 code 和 shop_id (Local) 或 main_account_id (CB merchant)；"
            f"3) cms-api callback 自动完成 refresh_token 持久化（v2.11 起返回 HTML 结果页）。"
        ),
    }


def _oauth_result_html(title: str, color: str, lines: list[tuple[str, str]]) -> str:
    """简易授权结果页 HTML，避免浏览器看到裸 JSON 或白屏。"""
    import html as _html
    rows = "\n".join(
        f"<tr><td style='padding:6px 12px;color:#888;'>{_html.escape(k)}</td>"
        f"<td style='padding:6px 12px;font-family:monospace;'>{_html.escape(str(v))}</td></tr>"
        for k, v in lines
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title>"
        "<style>body{font-family:-apple-system,Segoe UI,sans-serif;max-width:640px;margin:48px auto;padding:24px;}"
        "h1{margin:0 0 24px;}table{border-collapse:collapse;width:100%;}"
        "tr:nth-child(odd){background:#fafafa;}</style></head>"
        f"<body><h1 style='color:{color};'>{title}</h1><table>{rows}</table>"
        "<p style='margin-top:24px;color:#888;'>本页面由 cms-api OAuth helper 渲染。可以关闭。</p>"
        "</body></html>"
    )


@app.get("/api/automation/shopee/oauth-callback")
def shopee_oauth_callback(
    market: str,
    code: str,
    shop_id: int | None = None,
    main_account_id: int | None = None,
):
    """Shopee OAuth 回调；用 code 换 refresh_token，自动写入 shopee_tokens.json。

    Shopee v2 OAuth 两种授权流程参数不同：
      - Local Shop（单店）授权：URL 含 shop_id
      - CB Merchant（跨境主体）授权：URL 含 main_account_id

    本端点二选一传入即可；调 /auth/token/get 时按对应字段构造 body。
    """
    if not SHOPEE_PARTNER_ID or not SHOPEE_PARTNER_KEY:
        return HTMLResponse(
            _oauth_result_html("❌ 配置缺失", "#c62828",
                               [("缺失", "SHOPEE_PARTNER_ID 或 SHOPEE_PARTNER_KEY 未在 .env 设置")]),
            status_code=503,
        )
    if not shop_id and not main_account_id:
        return HTMLResponse(
            _oauth_result_html("❌ 参数缺失", "#c62828",
                               [("说明", "URL 必须含 shop_id (Local) 或 main_account_id (CB merchant)"),
                                ("当前 query", f"market={market}, code={code[:8]}...")]),
            status_code=400,
        )
    import urllib.request

    path = "/api/v2/auth/token/get"
    ts = int(time.time())
    base = f"{SHOPEE_PARTNER_ID}{path}{ts}"
    sign = _shopee_sign(base)
    url = f"{SHOPEE_API_BASE}{path}?partner_id={SHOPEE_PARTNER_ID}&timestamp={ts}&sign={sign}"
    body_dict: dict = {"code": code, "partner_id": int(SHOPEE_PARTNER_ID)}
    if shop_id:
        body_dict["shop_id"] = int(shop_id)
    if main_account_id:
        body_dict["main_account_id"] = int(main_account_id)
    body = json.dumps(body_dict).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return HTMLResponse(
            _oauth_result_html("❌ Shopee /token/get 失败", "#c62828",
                               [("error", str(e)), ("market", market)]),
            status_code=502,
        )
    if data.get("error"):
        return HTMLResponse(
            _oauth_result_html("❌ Shopee 拒绝 token 换取", "#c62828",
                               [("error", data.get("error", "")),
                                ("message", data.get("message", "")),
                                ("request_id", data.get("request_id", "")),
                                ("market", market)]),
            status_code=502,
        )
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        return HTMLResponse(
            _oauth_result_html("❌ Shopee 响应缺 refresh_token", "#c62828",
                               [("响应", json.dumps(data)), ("market", market)]),
            status_code=502,
        )

    # 持久化：合并进 shopee_tokens.json
    existing = {}
    if SHOPEE_TOKENS_FILE.exists():
        try:
            existing = json.loads(SHOPEE_TOKENS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    refresh_tokens = existing.get("refresh_tokens", {})
    refresh_tokens[market] = refresh_token
    body_out = {
        "refresh_tokens": refresh_tokens,
        "updated_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    SHOPEE_TOKENS_FILE.write_text(json.dumps(body_out, ensure_ascii=False, indent=2), encoding="utf-8")

    shop_ids = data.get("shop_id_list") or ([shop_id] if shop_id else [])
    return HTMLResponse(_oauth_result_html(
        f"✅ {market} 授权成功", "#2e7d32",
        [
            ("market", market),
            ("refresh_token", "✅ 已写入 shopee_tokens.json"),
            ("access_token expire_in", f"{data.get('expire_in', '?')} 秒"),
            ("shop_id_list", ", ".join(str(s) for s in shop_ids) or "—"),
            ("merchant_id_list", ", ".join(str(m) for m in data.get("merchant_id_list") or []) or "—"),
            ("已授权市场", ", ".join(sorted(refresh_tokens.keys()))),
            ("提示", f"记得把 shop_id 加进 .env 的 SHOPEE_SHOP_IDS: \"{market}\": \"{shop_ids[0] if shop_ids else '?'}\""),
        ],
    ))
