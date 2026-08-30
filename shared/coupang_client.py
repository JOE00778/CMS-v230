"""Coupang Open API · 発送対象の注文を取るぶんだけ（page41 用）。

database リポの `data_warehouse/coupang_api/client.py` とは別実装。あちらは元川で回る
ingester（PII を捨てて注文数を集計する）で、こちらは CMS 画面から**発送のために PII 込みで**
その場で引く。用途が違うので共有しない。

環境変数（元川 .env）:
    COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY / COUPANG_VENDOR_ID

API の癖（database 側の実測メモより）:
    · status 必須。省略すると HTTP 400
    · 返却粒度は shipmentBox。1 注文が複数箱に割れる
    · nextToken でページング（maxPerPage 上限 50）。辿らないと取り逃す
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
from urllib.parse import urlencode

import requests
import streamlit as st

GATEWAY = "https://api-gateway.coupang.com"
TIMEOUT = 60
PAGE_SIZE = 50
MAX_PAGES = 200          # 暴走ガード
# 発送前の状態。ここに入っている箱が「これから ECMS に出す」対象
SHIPPABLE_STATUSES = ("ACCEPT", "INSTRUCT")


class CoupangError(RuntimeError):
    pass


class CoupangNotConfigured(CoupangError):
    pass


def _secret(name: str, default: str = "") -> str:
    try:
        v = st.secrets.get(name, None)
        if v:
            return str(v)
    except (FileNotFoundError, KeyError):
        pass
    return os.environ.get(name, "") or default


def is_configured() -> bool:
    return all(_secret(k) for k in
               ("COUPANG_ACCESS_KEY", "COUPANG_SECRET_KEY", "COUPANG_VENDOR_ID"))


def vendor_id() -> str:
    return _secret("COUPANG_VENDOR_ID")


def _auth_header(method: str, path: str, query: str) -> dict:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%y%m%dT%H%M%SZ")
    msg = ts + method.upper() + path + query
    sig = hmac.new(_secret("COUPANG_SECRET_KEY").encode(), msg.encode(),
                   hashlib.sha256).hexdigest()
    return {
        "Authorization": (f"CEA algorithm=HmacSHA256, access-key={_secret('COUPANG_ACCESS_KEY')}, "
                          f"signed-date={ts}, signature={sig}"),
        "Content-Type": "application/json;charset=UTF-8",
    }


def _get(path: str, params: dict) -> dict:
    if not is_configured():
        raise CoupangNotConfigured(
            "COUPANG_ACCESS_KEY / SECRET_KEY / VENDOR_ID 未配置（元川 .env）")
    query = urlencode(params, doseq=True)
    try:
        resp = requests.get(f"{GATEWAY}{path}?{query}",
                            headers=_auth_header("GET", path, query), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise CoupangError(f"Coupang 請求失敗（{path}）: {e}") from e
    try:
        data = resp.json()
    except ValueError as e:
        raise CoupangError(
            f"Coupang 返回非 JSON（HTTP {resp.status_code}）: {resp.text[:200]}") from e
    if resp.status_code != 200 or data.get("code") not in (200, "200", None):
        raise CoupangError(f"Coupang {path} 返回 {data.get('code', resp.status_code)}: "
                           f"{data.get('message', '')}")
    return data


def fetch_shippable(days: int = 3, statuses=SHIPPABLE_STATUSES) -> list[dict]:
    """直近 `days` 日の発送対象の箱を全部返す（PII 込み · 呼び出し側で使い切る）。

    同じ箱が複数 status で返ることは無い（status は排他）が、念のため
    (orderId, shipmentBoxId) で重複を落とす。
    """
    today = dt.date.today()
    since = today - dt.timedelta(days=max(1, days) - 1)
    path = f"/v2/providers/openapi/apis/api/v4/vendors/{vendor_id()}/ordersheets"

    seen: set[tuple] = set()
    out: list[dict] = []
    for status in statuses:
        token = None
        for _ in range(MAX_PAGES):
            params = {"createdAtFrom": since.isoformat(), "createdAtTo": today.isoformat(),
                      "status": status, "maxPerPage": PAGE_SIZE}
            if token:
                params["nextToken"] = token
            data = _get(path, params)
            for box in data.get("data") or []:
                key = (box.get("orderId"), box.get("shipmentBoxId"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(box)
            token = data.get("nextToken")
            if not token:
                break
    return out
