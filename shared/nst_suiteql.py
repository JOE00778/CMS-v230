"""NST SuiteQL 极小クライアント（物流費配賦の SO 帰類専用 · 読み取りのみ）。

用途は一つ: JD 請求書の join_key が `SOxxxxxxxx_nnnnn`（NST 注文）のとき、
SuiteQL で **NST の店舗字段**を引いて order_shop_map の shop に据える
（Boss 2026-08-30 是正：顧客名は販売渠道ではないので使わない）。
店舗未設定の直録注文（B2B 卸/保証補発）は「NST直販」（dept=EC 登録済 ·
Boss 2026-08-30「归EC 也就是现在的CB事业部」）。

認証は TBA（OAuth 1.0a HMAC-SHA256 · NST_AUTH_MODE=tba）——四つの文字列だけで
署名でき、純標準ライブラリで済む（OAuth2/JWT は cert+PyJWT が要るため CMS
コンテナでは使わない）。署名アルゴリズムは database 仓
data_warehouse/nst_api/client.py の TBAAuth と同一。

env（compose → deploy/windows/.env · database/.env と同値）:
    NST_ACCOUNT_ID / NST_TBA_CONSUMER_KEY / NST_TBA_CONSUMER_SECRET /
    NST_TBA_TOKEN_ID / NST_TBA_TOKEN_SECRET
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})


class NstError(RuntimeError):
    pass


def _secret(name: str) -> str:
    try:
        import streamlit as st
        v = st.secrets.get(name, None)
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name, "")


def is_configured() -> bool:
    return all(_secret(k) for k in (
        "NST_ACCOUNT_ID", "NST_TBA_CONSUMER_KEY", "NST_TBA_CONSUMER_SECRET",
        "NST_TBA_TOKEN_ID", "NST_TBA_TOKEN_SECRET"))


def tba_header(method: str, url: str, *, account_id: str,
               consumer_key: str, consumer_secret: str,
               token_id: str, token_secret: str,
               nonce: str | None = None, ts: str | None = None) -> str:
    """OAuth 1.0a HMAC-SHA256 Authorization ヘッダ（database 仓 TBAAuth と同一）。
    nonce/ts はテスト用に固定注入可。"""
    nonce = nonce or secrets.token_hex(16)
    ts = ts or str(int(time.time()))
    params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_timestamp": ts,
        "oauth_token": token_id,
        "oauth_version": "1.0",
    }
    parsed = urllib.parse.urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    all_params = {**params, **dict(urllib.parse.parse_qsl(parsed.query))}
    param_str = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
        for k, v in sorted(all_params.items()))
    base_string = "&".join(
        urllib.parse.quote(s, safe="")
        for s in (method.upper(), base_url, param_str))
    signing_key = (f"{urllib.parse.quote(consumer_secret, safe='')}&"
                   f"{urllib.parse.quote(token_secret, safe='')}")
    signature = base64.b64encode(
        hmac.new(signing_key.encode(), base_string.encode(),
                 hashlib.sha256).digest()).decode()
    params["oauth_signature"] = signature
    params["realm"] = account_id
    return "OAuth " + ", ".join(
        f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in params.items())


def suiteql(sql: str, *, limit: int = 1000, max_retries: int = 4) -> list[dict]:
    """SuiteQL 1 ページ照会（配賦用途は数行 → 分頁不要 · hasMore なら fail-loud）。
    再試行判据は 2026-08-28 全 ingester 統一形。"""
    if not is_configured():
        raise NstError("NST_TBA_* 未配置（deploy/windows/.env）")
    account = _secret("NST_ACCOUNT_ID")
    url = (f"https://{account.lower()}.suitetalk.api.netsuite.com"
           f"/services/rest/query/v1/suiteql?limit={limit}")
    body = json.dumps({"q": sql}).encode()
    last_err: Exception | None = None
    for attempt in range(max_retries):
        headers = {
            "Authorization": tba_header(
                "POST", url, account_id=account,
                consumer_key=_secret("NST_TBA_CONSUMER_KEY"),
                consumer_secret=_secret("NST_TBA_CONSUMER_SECRET"),
                token_id=_secret("NST_TBA_TOKEN_ID"),
                token_secret=_secret("NST_TBA_TOKEN_SECRET")),
            "Content-Type": "application/json",
            "Prefer": "transient",
        }
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in RETRY_HTTP_STATUS:
                last_err = e
                time.sleep(float(2 ** attempt))
                continue
            raise NstError(f"NST HTTP {e.code}: {text[:300]}") from e
        except (OSError, http.client.HTTPException,
                json.JSONDecodeError) as e:
            last_err = e
            if attempt + 1 >= max_retries:
                break
            time.sleep(float(2 ** attempt))
            continue
        if data.get("hasMore"):
            raise NstError("suiteql: hasMore=true（配賦用途で想定外の大結果）")
        return data.get("items", [])
    raise NstError(f"retries exhausted: {last_err}") from last_err


NST_DIRECT_SHOP = "NST直販"     # 店舗未設定の直録注文（B2B 卸/保証補発）の帰属先


def lookup_so_shops(so_nos: list[str]) -> dict[str, str]:
    """SO 番号 → NST の店舗名（custbody_fb_ne_ro_shop · sales_invoice 鏡像と同字段）。

    ⚠️ 顧客（entity）は使わない——顧客は取引相手であって販売渠道ではない
    （Boss 2026-08-30「NST店铺和斑马店铺搞混了」の是正）。
    - 店舗あり（平台系 SO）: 表示名の「nn:」内部 ID 前綴を外した店名
    - 店舗なし（直録 B2B/保証補発 · 実測で大半）: NST_DIRECT_SHOP
    NST に存在しない SO は結果に含まれない。
    """
    so_nos = sorted({s for s in so_nos if s})
    if not so_nos:
        return {}
    quoted = ",".join("'" + s.replace("'", "''") + "'" for s in so_nos)
    rows = suiteql(
        "SELECT t.tranid, BUILTIN.DF(t.custbody_fb_ne_ro_shop) AS shop "
        f"FROM transaction t WHERE t.tranid IN ({quoted}) "
        "AND t.type = 'SalesOrd'")
    out: dict[str, str] = {}
    for r in rows:
        if not r.get("tranid"):
            continue
        shop = (r.get("shop") or "").partition(":")[2] or r.get("shop") or ""
        out[r["tranid"]] = shop.strip() or NST_DIRECT_SHOP
    return out
