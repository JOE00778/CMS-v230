"""shopify_order.py — Shopify の注文番号から CI（コマーシャル・インボイス）の素を作る.

Boss 2026-10-23「CMS に Shopify の注文番号を入れてボタンを押したら invoice が
出る形にする。データは Shopify と斑马の両方にある。斑马を通さなくてもよい」
→ **斑马には一切触らない**。Shopify（読み取りのみ）+ PG `logistics.item_customs`。

Shopify に無いもの（2026-10-23 実測）:
  - 重量   : variant.inventoryItem.measurement.weight は 4,395 件中 1 件しか値が無い
  - 原産国 : countryCodeOfOrigin は全件 null
  どちらも PG `logistics.item_customs` から JAN で引く。
  HS は Shopify の harmonizedSystemCode にあるが、同じ値を PG にも持たせてあるので
  **PG を正**とし、PG に無いときだけ Shopify の値を使う。

設定（元川 `.env` / streamlit secrets。凭证は仓库にもチャットにも置かない）:
  SHOPIFY_STORE   既定 0gaf7p-0j
  SHOPIFY_TOKEN   Admin API アクセストークン（read_orders / read_products）
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime

API_V = "2026-04"
TIMEOUT = 60

# FedEx の CI は国名を正式名の大文字で書く（実物は UNITED STATES OF AMERICA）
COUNTRY_NAME = {
    "US": "UNITED STATES OF AMERICA",
    "JP": "JAPAN",
    "PH": "PHILIPPINES",
    "KR": "KOREA, REPUBLIC OF",
    "CA": "CANADA",
    "AU": "AUSTRALIA",
    "GB": "UNITED KINGDOM",
    "SG": "SINGAPORE",
}

ORDER_Q = """
query($q:String!){
  orders(first:1, query:$q){
    nodes{
      name createdAt currencyCode
      shippingAddress{ firstName lastName company address1 address2
                       city provinceCode zip countryCodeV2 country phone }
      lineItems(first:100){
        nodes{
          title quantity sku
          discountedTotalSet{ shopMoney{ amount } }
          originalUnitPriceSet{ shopMoney{ amount } }
          variant{ barcode inventoryItem{ harmonizedSystemCode } }
        }
      }
    }
  }
}
"""


class ShopifyNotConfigured(RuntimeError):
    """SHOPIFY_TOKEN 未設定。ページは開くがボタンは押せない、を表現する."""


def _secret(name: str, default: str = "") -> str:
    try:
        import streamlit as st
        v = st.secrets.get(name, None)
        if v:
            return str(v)
    except Exception:
        pass
    return os.environ.get(name, "") or default


def _call(query: str, variables: dict) -> dict:
    token = _secret("SHOPIFY_TOKEN")
    if not token:
        raise ShopifyNotConfigured("SHOPIFY_TOKEN が未設定（元川の .env に入れる）")
    shop = _secret("SHOPIFY_STORE", "0gaf7p-0j") + ".myshopify.com"
    req = urllib.request.Request(
        f"https://{shop}/admin/api/{API_V}/graphql.json",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = json.loads(r.read())
    if body.get("errors"):
        raise RuntimeError(f"Shopify GraphQL: {body['errors']}")
    return body["data"]


def normalize_order_no(raw: str) -> str:
    """「1074」「#1074」「 #1074 」どれでも name:#1074 に揃える."""
    s = (raw or "").strip()
    s = re.sub(r"^#", "", s)
    return f"#{s}" if s else ""


def fetch_order(order_no: str) -> dict | None:
    """注文 1 件を取る。見つからなければ None."""
    name = normalize_order_no(order_no)
    if not name:
        return None
    data = _call(ORDER_Q, {"q": f"name:{name}"})
    nodes = (data.get("orders") or {}).get("nodes") or []
    for o in nodes:
        if o.get("name") == name:      # query は前方一致するので name を照合する
            return o
    return None


def customs_by_jan(conn, jans: list[str]) -> dict[str, dict]:
    """PG `logistics.item_customs` を JAN で引く。無い JAN は返らない."""
    jans = [j for j in dict.fromkeys(jans) if j]
    if not jans:
        return {}
    rows = conn.execute(
        "select jan, hs_code, name_en, origin_country, origin_src,"
        "       net_weight_g, gross_weight_g, weight_src,"
        "       customs_name_en,"
        "       extra->'origin_candidates' as origin_candidates,"
        "       extra->>'hs_flag' as hs_flag"
        "  from logistics.item_customs where jan = any(?)", (jans,)).fetchall()
    return {r["jan"]: dict(r) for r in rows}


def to_ci_lines(order: dict, customs: dict[str, dict]) -> tuple[list, list[str]]:
    """注文 → CiLine の一覧 と、欠けている項目の警告。

    **欠けた値を勝手に埋めない**。原産国・HS・重量が無い行はそのまま空で返し、
    どの JAN の何が無いかを呼び出し元に返す（画面で赤く出して人が直す）。
    """
    from shared.invoice_ci import CiLine

    lines, warn = [], []
    for li in (order.get("lineItems") or {}).get("nodes") or []:
        v = li.get("variant") or {}
        jan = (v.get("barcode") or li.get("sku") or "").strip()
        c = customs.get(jan, {})
        qty = int(li.get("quantity") or 0)
        total = float((li.get("discountedTotalSet") or {}).get("shopMoney", {}).get("amount") or 0)
        unit = round(total / qty, 6) if qty else 0.0
        hs = c.get("hs_code") or (v.get("inventoryItem") or {}).get("harmonizedSystemCode") or ""
        w_g = c.get("net_weight_g") or c.get("gross_weight_g")
        title = li.get("title") or ""
        cname = c.get("customs_name_en") or ""
        # 警告は「問題コード + 引数」で返す。文言は画面側が言語に合わせて出す（中/日 UI）
        if not cname:
            warn.append({"jan": jan, "code": "no_name"})
        if not hs:
            warn.append({"jan": jan, "code": "no_hs"})
        if c.get("hs_flag"):
            # 2026-10-24 HS 審査で「事実どおりにしか書けない」とした品（虫よけ・殺菌剤・医薬品）
            warn.append({"jan": jan, "code": "hs_flag", "arg": c["hs_flag"]})
        if not c.get("origin_country"):
            warn.append({"jan": jan, "code": "no_origin"})
        elif c.get("origin_src") == "default-conflict":
            # Boss 2026-10-24「全部 JP で、invoice を作る時に警告」
            warn.append({"jan": jan, "code": "origin_conflict",
                         "arg": " / ".join(c.get("origin_candidates") or [])})
        if not w_g:
            warn.append({"jan": jan, "code": "no_weight"})
        lines.append(CiLine(
            description=cname,          # Boss 2026-10-25: 商品名ではなく清関用の英文品名
            units=qty,
            net_weight_kg=round(float(w_g) / 1000, 3) if w_g else 0.0,
            hs_code=hs,
            origin=c.get("origin_country") or "",
            unit_value=unit,
        ))
    return lines, warn


def to_ci_doc(order: dict, lines: list, awb: str = "",
              total_packages: int = 1, total_gross_kg: float | None = None):
    from shared.invoice_ci import CiDoc

    a = order.get("shippingAddress") or {}
    cc = (a.get("countryCodeV2") or "").upper()
    country = COUNTRY_NAME.get(cc) or (a.get("country") or "").upper()
    name = " ".join(x for x in (a.get("firstName"), a.get("lastName")) if x).strip()
    city = " ".join(x for x in (a.get("city"), a.get("provinceCode"), a.get("zip")) if x)
    addr = [x for x in (a.get("company"), a.get("address1"), a.get("address2")) if x]
    ship = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00")).date()
    return CiDoc(
        invoice_no="SJ-" + (order.get("name") or "").lstrip("#"),
        ship_date=ship,
        consignee_name=name or (a.get("company") or ""),
        consignee_addr=addr,
        consignee_city=city.upper(),
        consignee_country=country,
        consignee_contact=name,
        consignee_tel=re.sub(r"\D", "", a.get("phone") or ""),
        awb=awb,
        total_packages=total_packages,
        total_gross_kg=total_gross_kg,
        lines=lines,
    )


def save_manual(conn, rows: list[dict]) -> int:
    """発票画面で人が補った値を PG に書き戻す（src='manual'）。

    rows: {"jan", "hs_code", "origin_country", "net_weight_g"} のうち
    **変更された項目だけ**を持つ dict。空・None の項目は触らない。
    行が無い JAN（主档未登録）は新規に作る。戻り値は書いた行数。
    """
    n = 0
    for r in rows:
        jan = r["jan"]
        conn.execute("insert into logistics.item_customs (jan) values (?) "
                     "on conflict (jan) do nothing", (jan,))
        if r.get("hs_code"):
            conn.execute("update logistics.item_customs set hs_code=?, hs_src='manual',"
                         " updated_at=now() where jan=?", (r["hs_code"], jan))
        if r.get("origin_country"):
            conn.execute("update logistics.item_customs set origin_country=?,"
                         " origin_src='manual', updated_at=now() where jan=?",
                         (r["origin_country"], jan))
        if r.get("customs_name_en"):
            conn.execute("update logistics.item_customs set customs_name_en=?,"
                         " customs_name_src='manual', updated_at=now() where jan=?",
                         (r["customs_name_en"], jan))
        if r.get("net_weight_g"):
            conn.execute("update logistics.item_customs set net_weight_g=?,"
                         " weight_src='manual', updated_at=now() where jan=?",
                         (r["net_weight_g"], jan))
        n += 1
    conn.commit()
    return n
