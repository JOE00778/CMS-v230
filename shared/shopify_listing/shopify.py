"""自建站上架 · Shopify Admin GraphQL（读 + 建草稿 + 挂图）。

只建 DRAFT（Boss 2026-11-20：改上架由人在 Shopify 后台做，CMS 不做上架按钮）。
建草稿逻辑沿 shopify/scripts/list_from_feishu.py build_input()（metafield 对位 / DENY+tracked / 富文本）。
美国价 = variant 价 + 美国价目表固定价同值（07 规则第 7 章：只填基准价，美国前台会 ÷1.1）。
发布 = 网店 + 美国市场；PH 市场目录 autoPublish=true 会自动收录新品 → 显式撤出（2026-11-20 实查）。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from shared import shopify_order as _so
from shared.shopify_listing import spec as S
from shared.shopify_listing.validate import final_tags

ONLINE_STORE = "gid://shopify/Publication/187994570967"
US_PUB = "gid://shopify/Publication/198344016087"
PH_PUB = "gid://shopify/Publication/189746708695"
US_PRICE_LIST = "gid://shopify/PriceList/20712358103"


def gql(query: str, variables: dict | None = None, tries: int = 5) -> dict:
    for i in range(tries):
        try:
            return _so._call(query, variables or {})
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
        except RuntimeError as e:
            if "THROTTLED" in str(e).upper() and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
    raise RuntimeError("Shopify: retries exhausted")


# ───────────── 读 ─────────────

def existing_jans() -> set[str]:
    """全店 variant 的 barcode / sku（查重用）。"""
    out, after = set(), None
    while True:
        d = gql("""query($a:String){ productVariants(first:250, after:$a){ pageInfo{ hasNextPage endCursor }
                  nodes{ barcode sku } } }""", {"a": after})["productVariants"]
        for n in d["nodes"]:
            for v in (n.get("barcode"), n.get("sku")):
                if v and str(v).strip():
                    out.add(str(v).strip())
        if not d["pageInfo"]["hasNextPage"]:
            return out
        after = d["pageInfo"]["endCursor"]


def live_cat_tags() -> set[str]:
    """线上智能集合按 tag = cat-* 收录的标签（小类必须在这里，否则进不了类目页）。"""
    out, after = set(), None
    while True:
        d = gql("""query($a:String){ collections(first:250, after:$a){ pageInfo{ hasNextPage endCursor }
                  nodes{ ruleSet{ rules{ column relation condition } } } } }""", {"a": after})["collections"]
        for n in d["nodes"]:
            for r in ((n.get("ruleSet") or {}).get("rules") or []):
                if r["column"] == "TAG" and r["relation"] == "EQUALS" and r["condition"].startswith("cat-"):
                    out.add(r["condition"])
        if not d["pageInfo"]["hasNextPage"]:
            return out
        after = d["pageInfo"]["endCursor"]


def handles_taken(handles: list[str]) -> set[str]:
    out = set()
    hs = [h for h in dict.fromkeys(handles) if h and S.HANDLE_RE.match(h)]
    for i in range(0, len(hs), 25):
        chunk = hs[i:i + 25]
        q = " OR ".join(f"handle:{h}" for h in chunk)
        d = gql("query($q:String!){ products(first:50, query:$q){ nodes{ handle } } }", {"q": q})
        out |= {n["handle"] for n in d["products"]["nodes"]} & set(chunk)
    return out


def products_by_jan(jans: list[str]) -> dict[str, dict]:
    """JAN → {id, title, status, media:[{id, alt}]}（按 barcode 找；挂图用）。"""
    out: dict[str, dict] = {}
    js = [j for j in dict.fromkeys(jans) if j]
    for i in range(0, len(js), 25):
        chunk = js[i:i + 25]
        q = " OR ".join(f"barcode:{j}" for j in chunk)
        d = gql("""query($q:String!){ productVariants(first:100, query:$q){ nodes{ barcode
                  product{ id title status handle media(first:50){ nodes{ id alt mediaContentType } } } } } }""", {"q": q})
        for n in d["productVariants"]["nodes"]:
            b = str(n.get("barcode") or "")
            if b in chunk and b not in out:
                p = n["product"]
                out[b] = {"id": p["id"], "title": p["title"], "status": p["status"], "handle": p["handle"],
                          "media": [m for m in p["media"]["nodes"] if m["mediaContentType"] == "IMAGE"]}
    return out


# ───────────── 建草稿 ─────────────

def _txt(s, bold=False):
    n = {"type": "text", "value": s}
    if bold:
        n["bold"] = True
    return n


def _para(s):
    return {"type": "paragraph", "children": [_txt(s)]}


def richtext(s, kind: str) -> str | None:
    """纯文本 → Shopify rich_text JSON（同 list_from_feishu.build_richtext 的纯文本分支）。"""
    s = (str(s or "")).strip()
    if not s:
        return None
    lines = [p.strip() for p in re.split(r"\s*[\n|]\s*", s) if p.strip()]
    ch: list = []
    if kind == "ingredients":
        if len(lines) == 1 and " — " not in lines[0] and " - " not in lines[0] and lines[0].count(",") >= 5:
            ch.append(_para(lines[0]))
        else:
            items = []
            for ln in lines:
                sep = " — " if " — " in ln else (" - " if " - " in ln else None)
                if sep:
                    name, desc = ln.split(sep, 1)
                    kids = [_txt(name.strip(), True), _txt(sep + desc.strip())]
                else:
                    kids = [_txt(ln)]
                items.append({"type": "list-item", "children": kids})
            ch.append({"type": "list", "listType": "unordered", "children": items})
    elif kind == "howto":
        ch.append({"type": "list", "listType": "ordered", "children": [
            {"type": "list-item", "children": [_txt(re.sub(r"^\s*\d+[.\)、]\s*", "", ln))]} for ln in lines]})
    elif kind == "faq":
        for qa in re.split(r"(?=Q[:：])", " ".join(lines)):
            qa = qa.strip()
            if not qa:
                continue
            m = re.match(r"Q[:：]\s*(.*?)\s*A[:：]\s*(.*)", qa, re.S)
            if m:
                ch.append({"type": "paragraph", "children": [_txt("Q: " + m.group(1).strip(), True)]})
                ch.append(_para("A: " + m.group(2).strip()))
            else:
                ch.append(_para(qa))
    elif kind == "highlights" and len(lines) > 1:
        ch.append({"type": "list", "listType": "unordered",
                   "children": [{"type": "list-item", "children": [_txt(ln)]} for ln in lines]})
    else:
        ch += [_para(ln) for ln in lines]
    return json.dumps({"type": "root", "children": ch}, ensure_ascii=False) if ch else None


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def build_input(row: dict, expected: dict) -> dict:
    jan = _s(row["jan"])
    pt = _s(row.get("page_type")) or "standard"
    mf: list[dict] = []

    def add(ns, key, typ, val):
        if val:
            mf.append({"namespace": ns, "key": key, "type": typ, "value": val})

    add("smikie", "jan_code", "single_line_text_field", jan)
    add("smikie", "origin_city", "single_line_text_field", "Japan")
    vendor = _s(expected.get("vendor")) or _s(row.get("vendor"))
    brand = _s(expected.get("brand")) if _s(expected.get("vendor")) else _s(row.get("brand"))
    add("smikie", "brand_display", "single_line_text_field", S.display_name(vendor, brand))
    add("smikie", "acc_intro_html", "rich_text_field", richtext(row.get("description"), "intro"))
    add("smikie", "acc_geo_us_html", "rich_text_field", richtext(row.get("geo_us"), "geo"))
    add("smikie", "brand_subtitle", "single_line_text_field", _s(row.get("brand_subtitle")))
    add("smikie", "short_description", "single_line_text_field", _s(row.get("short_description")))
    add("smikie", "acc_ingredients_html", "rich_text_field", richtext(row.get("ingredients"), "ingredients"))
    add("smikie", "acc_howto_html", "rich_text_field", richtext(row.get("howto"), "howto"))
    if pt == "otc":
        add("smikie", "otc_class", "single_line_text_field", _s(row.get("otc_class")))
        add("smikie", "acc_efficacy_html", "rich_text_field", richtext(row.get("otc_efficacy"), "intro"))
        add("smikie", "acc_precautions_html", "rich_text_field", richtext(row.get("otc_precautions"), "intro"))
        add("smikie", "acc_specs_html", "rich_text_field", richtext(row.get("otc_specs"), "intro"))
    else:
        add("smikie", "highlights_html", "rich_text_field", richtext(row.get("highlights"), "highlights"))
        add("custom", "acc_04_1_supplementary_explanation_html", "rich_text_field", richtext(row.get("faq"), "faq"))
        if pt == "food":
            add("smikie", "acc_nutrition_html", "rich_text_field", richtext(row.get("food_nutrition"), "intro"))
            add("smikie", "acc_allergen_html", "rich_text_field", richtext(row.get("food_allergen"), "intro"))
            add("smikie", "acc_storage_html", "rich_text_field", richtext(row.get("food_storage"), "intro"))
    price = f"{float(expected['price_usd']):.2f}"
    return {
        "title": _s(row["title"]), "handle": _s(row["handle"]), "vendor": vendor,
        "productType": f"{_s(row['l1'])} {_s(row['l2'])}".strip(),
        "status": "DRAFT", "templateSuffix": S.TEMPLATES.get(pt, "smikie-standard"),
        "tags": final_tags(row, expected),
        "descriptionHtml": _s(row.get("description")),
        "seo": {"title": _s(row.get("seo_title")), "description": _s(row.get("seo_description"))},
        "metafields": mf,
        "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
        "variants": [{
            "optionValues": [{"optionName": "Title", "name": "Default Title"}],
            "sku": jan, "barcode": jan, "price": price,
            "inventoryPolicy": "DENY", "inventoryItem": {"tracked": True},   # Boss 2026-11-20：卖完不可下单
        }],
    }


_PRODUCT_SET = """mutation($input:ProductSetInput!){ productSet(synchronous:true, input:$input){
  product{ id handle status variants(first:1){ nodes{ id } } } userErrors{ field message } } }"""


def create_draft(row: dict, expected: dict) -> dict:
    """→ {'ok', 'product_id', 'handle', 'variant_id', 'steps': {...}, 'error'}。每步单独报，不捆绑。"""
    inp = build_input(row, expected)
    d = gql(_PRODUCT_SET, {"input": inp})["productSet"]
    if d["userErrors"] or not d["product"]:
        return {"ok": False, "error": json.dumps(d["userErrors"], ensure_ascii=False)[:500], "steps": {}}
    p = d["product"]
    vid = p["variants"]["nodes"][0]["id"]
    steps = {}
    e = gql("""mutation($id:ID!,$p:[PriceListPriceInput!]!){ priceListFixedPricesAdd(priceListId:$id, prices:$p){
              userErrors{ field message } } }""",
            {"id": US_PRICE_LIST, "p": [{"variantId": vid, "price": {"amount": inp["variants"][0]["price"],
                                                                    "currencyCode": "USD"}}]})
    steps["us_price"] = e["priceListFixedPricesAdd"]["userErrors"] or "ok"
    e = gql("""mutation($id:ID!,$in:[PublicationInput!]!){ publishablePublish(id:$id, input:$in){ userErrors{ message } } }""",
            {"id": p["id"], "in": [{"publicationId": ONLINE_STORE}, {"publicationId": US_PUB}]})
    steps["publish"] = e["publishablePublish"]["userErrors"] or "ok"
    e = gql("""mutation($id:ID!,$in:[PublicationInput!]!){ publishableUnpublish(id:$id, input:$in){ userErrors{ message } } }""",
            {"id": p["id"], "in": [{"publicationId": PH_PUB}]})
    steps["unpublish_ph"] = e["publishableUnpublish"]["userErrors"] or "ok"
    ok = all(v == "ok" for v in steps.values())
    return {"ok": ok, "product_id": p["id"], "handle": p["handle"], "variant_id": vid, "steps": steps,
            "error": "" if ok else json.dumps(steps, ensure_ascii=False)[:500]}


def readback(product_id: str, variant_id: str) -> dict:
    d = gql("""query($id:ID!,$pl:ID!,$q:String!){ product(id:$id){ title handle status vendor productType tags templateSuffix
              seo{ title description } metafields(first:40){ nodes{ namespace key } }
              variants(first:1){ nodes{ price barcode sku inventoryPolicy inventoryItem{ tracked } } }
              us: publishedOnPublication(publicationId:"%s") ph: publishedOnPublication(publicationId:"%s")
              web: publishedOnPublication(publicationId:"%s") }
              priceList(id:$pl){ prices(first:1, query:$q){ nodes{ price{ amount } } } } }""" % (US_PUB, PH_PUB, ONLINE_STORE),
            {"id": product_id, "pl": US_PRICE_LIST, "q": "variant_id:" + variant_id.rsplit("/", 1)[-1]})
    return d


def verify(row: dict, expected: dict, rb: dict) -> list[str]:
    """回读与应写值比对 → 不一致项。"""
    p = rb["product"]
    inp = build_input(row, expected)
    v = p["variants"]["nodes"][0]
    bad = []
    for k in ("title", "handle", "vendor", "productType", "templateSuffix"):
        if (p.get(k) or "") != (inp.get(k) or ""):
            bad.append(f"{k}: {p.get(k)!r} ≠ {inp.get(k)!r}")
    if p["status"] != "DRAFT":
        bad.append(f"status={p['status']}")
    if set(p["tags"]) != set(inp["tags"]):
        bad.append(f"tags 差 {sorted(set(inp['tags']) ^ set(p['tags']))}")
    if p["seo"]["title"] != inp["seo"]["title"]:
        bad.append("seo.title")
    want_mf = {(m["namespace"], m["key"]) for m in inp["metafields"]}
    got_mf = {(m["namespace"], m["key"]) for m in p["metafields"]["nodes"]}
    if want_mf - got_mf:
        bad.append(f"metafield 缺 {sorted(want_mf - got_mf)}")
    if v["price"] != inp["variants"][0]["price"] or v["barcode"] != inp["variants"][0]["barcode"]:
        bad.append(f"price/barcode {v['price']}/{v['barcode']}")
    if v["inventoryPolicy"] != "DENY" or not v["inventoryItem"]["tracked"]:
        bad.append("库存设置不是 DENY+tracked")
    pr = rb["priceList"]["prices"]["nodes"]
    if not pr or f"{float(pr[0]['price']['amount']):.2f}" != inp["variants"][0]["price"]:
        bad.append("美国价目表固定价不一致")
    if not p["us"] or not p["web"] or p["ph"]:
        bad.append(f"发布状态 web={p['web']} us={p['us']} ph={p['ph']}")
    return bad


# ───────────── 挂图 ─────────────

def _staged_upload(name: str, data: bytes) -> str:
    import requests
    t = gql("""mutation($in:[StagedUploadInput!]!){ stagedUploadsCreate(input:$in){
              stagedTargets{ url resourceUrl parameters{ name value } } userErrors{ message } } }""",
            {"in": [{"filename": name, "mimeType": "image/webp", "resource": "IMAGE", "httpMethod": "POST",
                     "fileSize": str(len(data))}]})["stagedUploadsCreate"]
    if t["userErrors"]:
        raise RuntimeError(f"stagedUploadsCreate: {t['userErrors']}")
    tg = t["stagedTargets"][0]
    r = requests.post(tg["url"], data={p["name"]: p["value"] for p in tg["parameters"]},
                      files={"file": (name, data, "image/webp")}, timeout=120)
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"图片上传 HTTP {r.status_code}")
    return tg["resourceUrl"]


def attach_images(product_id: str, title: str, files: list[tuple[str, bytes]], mode: str,
                  old_media_ids: list[str]) -> dict:
    """files 已按位置排好（主图在前）。mode: append / replace（先挂新图成功后再删旧图）。"""
    srcs = [_staged_upload(n, b) for n, b in files]
    d = gql("""mutation($id:ID!,$m:[CreateMediaInput!]!){ productCreateMedia(productId:$id, media:$m){
              media{ id status } mediaUserErrors{ field message } } }""",
            {"id": product_id, "m": [{"originalSource": s, "mediaContentType": "IMAGE", "alt": title} for s in srcs]})
    r = d["productCreateMedia"]
    if r["mediaUserErrors"]:
        return {"ok": False, "error": json.dumps(r["mediaUserErrors"], ensure_ascii=False)[:300], "new": []}
    new_ids = [m["id"] for m in r["media"]]
    # 等处理完成（READY）再删旧图，避免新图失败时商品无图
    for _ in range(30):
        st = gql("""query($id:ID!){ product(id:$id){ media(first:100){ nodes{ id status } } } }""",
                 {"id": product_id})["product"]["media"]["nodes"]
        stat = {m["id"]: m["status"] for m in st}
        if any(stat.get(i) == "FAILED" for i in new_ids):
            return {"ok": False, "error": "Shopify 处理图片失败（FAILED）", "new": new_ids}
        if all(stat.get(i) == "READY" for i in new_ids):
            break
        time.sleep(2)
    else:
        return {"ok": False, "error": "图片处理超时（60 秒未 READY）", "new": new_ids}
    if mode == "replace" and old_media_ids:
        e = gql("""mutation($id:ID!,$m:[ID!]!){ productDeleteMedia(productId:$id, mediaIds:$m){
                  mediaUserErrors{ message } } }""", {"id": product_id, "m": old_media_ids})
        if e["productDeleteMedia"]["mediaUserErrors"]:
            return {"ok": False, "error": "新图已挂，旧图删除失败：" + str(e["productDeleteMedia"]["mediaUserErrors"]),
                    "new": new_ids}
    return {"ok": True, "error": "", "new": new_ids}
