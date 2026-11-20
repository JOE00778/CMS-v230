"""上传的「商品」表逐行检查（纯函数 · 不碰 DB / Shopify）。

error = 不能建草稿（要回 Excel 改）；warn = 可以建，但要人勾选确认（合规 / 易误伤的词）。
口径见 spec.py。
"""
from __future__ import annotations

import re

from shared.shopify_listing import spec as S

_CAPACITY = re.compile(r"\b\d+(\.\d+)?\s?(ml|mL|ML|l|L|g|kg|mg|oz|sheets?|pcs|pieces|tablets|capsules|packs?|bags|sticks|cm|mm|m)\b")


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v != v:   # NaN
        return ""
    return str(v).strip()


def _split_tags(v: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n|;，、]", v or "") if t.strip()]


def final_tags(row: dict, expected: dict) -> list[str]:
    """系统生成的 cat-/brand-/template- + AI 填的其他标签。"""
    l1, l2 = _s(row.get("l1")), _s(row.get("l2"))
    tags = []
    if l1 in S.CATEGORIES:
        t1, subs = S.CATEGORIES[l1]
        tags.append(t1)
        if l2 in subs:
            tags.append(subs[l2])
    vendor = _s(expected.get("vendor")) or _s(row.get("vendor"))
    brand = _s(expected.get("brand")) if _s(expected.get("vendor")) else _s(row.get("brand"))
    tags += S.brand_tags(vendor, brand)
    tags.append("template-" + (_s(row.get("page_type")) or "standard"))
    tags += _split_tags(_s(row.get("tags")))
    out, seen = [], set()
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _banned(text: str, pats) -> list[str]:
    return [msg for p, msg in pats if re.search(p, text, re.I)]


def check_row(row: dict, expected: dict | None, *, existing_jans: set, existing_handles: set,
              live_cat_tags: set, dup_handles: set) -> dict:
    """1 行 → {'jan', 'errors': [(列, 原因)], 'warns': [(列, 原因)]}。
    expected = 下载时（=现在 PG 现算）的预填值；None = JAN 不是可上架候选。"""
    E, W = [], []
    jan = _s(row.get("jan"))
    if re.fullmatch(r"\d+(\.\d+)?[eE][+-]?\d+", jan) or re.fullmatch(r"\d+\.0", jan):
        E.append(("jan", "JAN 被 Excel 变成了科学计数 / 小数，JAN 列要设成文本"))
    elif not S.JAN_RE.match(jan):
        E.append(("jan", "JAN 不是 8 位或 13 位数字"))
    if jan in existing_jans:
        E.append(("jan", "Shopify 已有这个 JAN 的商品（查重）"))
    if expected is None and S.JAN_RE.match(jan):
        E.append(("jan", "不在可上架候选里（非 NEW / 非取扱中 / 原価缺失 / 组合品）"))
    exp = expected or {}

    # 预填列不许改（厂商口径表里没有的商品：厂商/品牌由 AI 填，改为人工确认）
    vendor_open = bool(exp) and not _s(exp.get("vendor"))
    if vendor_open:
        if not _s(row.get("vendor")):
            E.append(("vendor", "必填（这件商品的厂商还没进口径表，要按官网查填）"))
        else:
            W.append(("vendor", f"厂商「{_s(row.get('vendor'))}」/ 品牌「{_s(row.get('brand'))}」由 AI 填写，"
                                "请确认写法（官网为准，查不到用全大写，无重音）"))
            if S.deacc(_s(row.get("vendor")) + _s(row.get("brand"))) != _s(row.get("vendor")) + _s(row.get("brand")):
                E.append(("vendor", "厂商/品牌名不能带重音符号"))
    for k in S.PRE_KEYS:
        if k == "jan" or not exp or (vendor_open and k in ("vendor", "brand")):
            continue
        got, want = _s(row.get(k)), exp.get(k)
        if k == "price_usd":
            try:
                ok = want is not None and abs(float(got) - float(want)) < 0.005
            except ValueError:
                ok = False
            if not ok:
                E.append((k, f"美国价被改了或原価已变：表里 {got or '空'}，现在应为 {want}"))
        elif got != _s(want):
            E.append((k, f"预填值被改了：表里「{got}」，应为「{want}」"))

    for k in S.REQUIRED:
        if k in S.PRE_KEYS:
            continue
        if not _s(row.get(k)):
            E.append((k, "必填，没有填"))
    for k, n in S.MAXLEN.items():
        v = _s(row.get(k))
        if len(v) > n:
            E.append((k, f"超过 {n} 字符（现在 {len(v)}）"))
    for k in S.COL_KEYS:
        if S.PENDING in _s(row.get(k)):
            E.append((k, "含「待查」：查不到的商品先不上架（Boss 2026-09-04）"))

    title = _s(row.get("title"))
    if title:
        if S.deacc(title) != title:
            E.append(("title", "标题里有重音符号（Biore 不是 Bioré）"))
        if S.KANA_KANJI.search(title):
            E.append(("title", "标题里有日文"))
        if re.search(r"\bmedicated\b", title, re.I) and re.search(r"\bwhiten", title, re.I):
            E.append(("title", "Medicated 和 Whitening 不能同时出现"))
        disp = exp.get("brand_display") or (S.display_name(_s(row.get("vendor")), _s(row.get("brand"))) if vendor_open else "")
        if disp and not title.lower().startswith(disp.lower()):
            W.append(("title", f"标题没有以品牌展示名「{disp}」开头"))
        caps = list(_CAPACITY.finditer(title))
        tail = re.sub(r"\s*\(Pack of \d+\)\s*$", "", title)
        if caps and not tail.rstrip().endswith(caps[-1].group(0)):
            W.append(("title", "容量/规格看起来不在最后"))
        if re.search(r"[×xX]\s?\d+\s*$", title):
            E.append(("title", "件数要写 (Pack of N)，不写 ×N"))

    handle = _s(row.get("handle"))
    if handle:
        if not S.HANDLE_RE.match(handle):
            E.append(("handle", "只能用小写字母、数字和 -"))
        if handle in existing_handles:
            E.append(("handle", "Shopify 已有这个 handle"))
        if handle in dup_handles:
            E.append(("handle", "表里有重复的 handle"))
        if title and handle != S.slug(title):
            W.append(("handle", f"和标题转出来的不一致（应为 {S.slug(title)}）"))

    l1, l2 = _s(row.get("l1")), _s(row.get("l2"))
    if l1 and l1 not in S.CATEGORIES:
        E.append(("l1", "大类不在「选项」表里"))
    elif l1 and l2:
        sub = S.CATEGORIES[l1][1].get(l2)
        if not sub:
            E.append(("l2", f"「{l2}」不是 {l1} 的小类"))
        elif live_cat_tags and sub not in live_cat_tags:
            E.append(("l2", f"线上没有 {sub} 集合，商品会不进类目页"))

    pt = _s(row.get("page_type"))
    if pt and pt not in S.TEMPLATES:
        E.append(("page_type", "只能填 standard / otc / food"))
    if pt == "food" and not _s(row.get("food_allergen")):
        E.append(("food_allergen", "食品必须填过敏原"))
    if pt == "otc" and not _s(row.get("otc_class")):
        E.append(("otc_class", "医药品必须填 OTC 类别"))
    if l1 == "Food & Drinks" and pt and pt != "food":
        W.append(("page_type", "大类是食品，商品页类型却不是 food"))

    for t in _split_tags(_s(row.get("tags"))):
        if t != t.lower() or " " in t or S.deacc(t) != t:
            E.append(("tags", f"{t}：标签要全小写、无空格、无重音"))
        elif t.startswith(S.SYSTEM_PREFIXES):
            E.append(("tags", f"{t}：cat-/brand-/template- 由系统生成，sale-/promo-/status- 不许打"))
        elif not t.startswith(S.FREE_PREFIXES):
            E.append(("tags", f"{t}：只能用 series-/item-/feat-/concern-/skin- 开头"))
        elif t.startswith("concern-") and t not in S.CONCERN:
            E.append(("tags", f"{t}：不在 concern- 固定清单"))
        elif t.startswith("skin-") and t not in S.SKIN:
            E.append(("tags", f"{t}：不在 skin- 固定清单"))
        elif t.startswith("feat-") and t not in S.FEAT:
            W.append(("tags", f"{t}：不在 feat- 固定清单（非美妆类照同类在售品写法时可确认）"))
        if t.startswith("skin-") and not (l1 == "Beauty" and l2 == "Skincare"):
            W.append(("tags", f"{t}：skin- 只用于护肤品"))

    geo = _s(row.get("geo_us"))
    if geo and not geo.rstrip().endswith(S.GEO_US_END):
        E.append(("geo_us", "最后一句不是固定结尾句"))
    sub = _s(row.get("brand_subtitle"))
    if sub and sub.count(" — ") != 1:
        E.append(("brand_subtitle", "要写成「品牌名 — 介绍」，恰好一个 ' — '（em dash 两边各一个空格）"))
    seo_d = _s(row.get("seo_description"))
    if seo_d and "Ships from Japan to the US, Philippines and South Korea." not in seo_d:
        W.append(("seo_description", "没有覆盖国家固定句"))

    for k in S.TEXT_KEYS:
        v = _s(row.get(k))
        if not v:
            continue
        for msg in _banned(v, S.BANNED_ERROR):
            E.append((k, "禁用表达：" + msg))
        for msg in _banned(v, S.BANNED_WARN):
            W.append((k, "疑似禁用：" + msg))
    if l1 in S.COMPLIANCE_L1:
        W.append(("l1", f"{l1} 属上架前须过合规的品类，请确认"))
    return {"jan": jan, "errors": E, "warns": W}


def check_all(rows: list[dict], expected_by_jan: dict, *, existing_jans: set, existing_handles: set,
              live_cat_tags: set) -> list[dict]:
    hs = [_s(r.get("handle")) for r in rows]
    dup = {h for h in hs if h and hs.count(h) > 1}
    js = [_s(r.get("jan")) for r in rows]
    out = []
    for r in rows:
        res = check_row(r, expected_by_jan.get(_s(r.get("jan"))), existing_jans=existing_jans,
                        existing_handles=existing_handles, live_cat_tags=live_cat_tags, dup_handles=dup)
        if js.count(res["jan"]) > 1:
            res["errors"].append(("jan", "表里同一个 JAN 出现了多行"))
        out.append(res)
    return out
