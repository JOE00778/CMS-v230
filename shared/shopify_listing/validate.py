"""上传的「商品」表逐行检查（纯函数 · 不碰 DB / Shopify / 网络）。

Boss 2026-11-20：模板全部由同事的 AI 按 README 填，CMS 上传时把关——
  规则检查（标题/handle/标签/字数/禁用词/GEO/待查）+ 对照内部数据（Shopify 查重 · NST · 厂商口径表 · 价格公式）。
error = 不能建草稿（回 Excel 改）；warn = 可以建，但要人勾选确认。
ctx[jan] = {"nst": {status, inactive, cost, name} | None, "brand": {vendor, brand, boss_confirmed} | None}
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
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _num(v) -> float | None:
    try:
        x = float(_s(v).replace(",", "").replace("$", ""))
        return x if x == x else None
    except ValueError:
        return None


def _split_tags(v: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\n|;，、]", v or "") if t.strip()]


def final_tags(row: dict) -> list[str]:
    """系统生成的 cat-/brand-/template- + AI 填的其他标签。"""
    l1, l2 = _s(row.get("l1")), _s(row.get("l2"))
    tags = []
    if l1 in S.CATEGORIES:
        t1, subs = S.CATEGORIES[l1]
        tags.append(t1)
        if l2 in subs:
            tags.append(subs[l2])
    tags += S.brand_tags(_s(row.get("vendor")), _s(row.get("brand")))
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


def _same(a: str, b: str) -> bool:
    return S.deacc(a).casefold() == S.deacc(b).casefold()


def check_row(row: dict, ctx: dict | None, *, existing_jans: set, existing_handles: set,
              live_cat_tags: set, dup_handles: set) -> dict:
    """1 行 → {'jan', 'errors': [(列, 原因)], 'warns': [(列, 原因)]}。"""
    E, W = [], []
    ctx = ctx or {}
    jan = _s(row.get("jan"))

    # ── JAN ──
    if not jan:
        E.append(("jan", "必填，没有填"))
    elif re.fullmatch(r"\d+(\.\d+)?[eE][+-]?\d+", jan):
        E.append(("jan", "JAN 被 Excel 变成了科学计数，JAN 列要设成文本"))
    elif not S.JAN_RE.match(jan):
        if S.CODE_RE.match(jan):
            W.append(("jan", "不是 8~14 位数字的 JAN（自编码？）请确认"))
        else:
            E.append(("jan", "JAN 格式不对（只能是数字，或字母数字和 -）"))
    if jan and jan in existing_jans:
        E.append(("jan", "Shopify 已有这个 JAN 的商品（查重）"))

    # ── 对照 NST ──
    nst = ctx.get("nst")
    if jan and not nst:
        W.append(("jan", "NST 里没有这个 JAN（未登录的新品？）请确认身份"))
    elif nst:
        if nst.get("inactive"):
            W.append(("jan", "NST 里这个 JAN 是【無効】"))
        if nst.get("status") and nst["status"] != "取扱中":
            W.append(("jan", f"NST 经营状态是「{nst['status']}」，不是取扱中（规则：默认不新上架）"))

    # ── 厂商 / 品牌（对照口径表；Boss 已确认的不可覆盖）──
    vendor, brand = _s(row.get("vendor")), _s(row.get("brand"))
    if vendor and S.deacc(vendor + brand) != vendor + brand:
        E.append(("vendor", "厂商/品牌名不能带重音符号"))
    bt = ctx.get("brand")
    if bt and vendor:
        diff = [f"{k}「{_s(row.get(k))}」≠ 口径表「{bt.get(k) or ''}」"
                for k in ("vendor", "brand") if bt.get(k) and not _same(_s(row.get(k)), bt[k])]
        if diff:
            if bt.get("boss_confirmed"):
                E.append(("vendor", "与 Boss 已确认的口径不一致（不可覆盖）：" + "；".join(diff)))
            else:
                W.append(("vendor", "与厂商口径表不一致：" + "；".join(diff)))
    elif vendor:
        W.append(("vendor", f"口径表里没有这个 JAN：厂商「{vendor}」/ 品牌「{brand}」请确认写法"))

    # ── 价格 ──
    price = _num(row.get("price_usd"))
    if price is None:
        E.append(("price_usd", "美国价为空（不知道原価时在本页表格里填）"))
    elif abs(round(price * 100) % 100 - 99) > 0:
        E.append(("price_usd", f"美国价要以 .99 结尾（现在 {price}）"))
    else:
        nst_cost = (nst or {}).get("cost")
        p_nst = S.price_usd(nst_cost)
        p_xls = S.price_usd(_num(row.get("cost_jpy")))
        if p_nst is not None:
            if abs(price - p_nst) > 0.005:
                W.append(("price_usd", f"与 NST 定義原価 ¥{nst_cost:,.0f} 的公式价 ${p_nst} 不同（表里 ${price}）"))
        elif p_xls is not None:
            if abs(price - p_xls) > 0.005:
                W.append(("price_usd", f"与表里原価的公式价 ${p_xls} 不同（表里 ${price}）"))
            W.append(("price_usd", "NST 没有定義原価，价格依据是表里填的原価，请确认"))
        else:
            W.append(("price_usd", "没有原価依据（NST 和表里都没有），请确认价格"))

    # ── 必填 / 字数 / 待查 ──
    for k in S.REQUIRED:
        if k in ("jan",):
            continue
        if not _s(row.get(k)):
            E.append((k, "必填，没有填"))
    for k, n in S.MAXLEN.items():
        v = _s(row.get(k))
        if len(v) > n:
            E.append((k, f"超过 {n} 字符（现在 {len(v)}）"))
    for k in S.COL_KEYS:
        if k != "notes" and S.PENDING in _s(row.get(k)):
            E.append((k, "含「待查」：查不到的商品先不上架（Boss 2026-09-04）"))

    # ── 标题 ──
    title = _s(row.get("title"))
    if title:
        if S.deacc(title) != title:
            E.append(("title", "标题里有重音符号（Biore 不是 Bioré）"))
        if S.KANA_KANJI.search(title):
            E.append(("title", "标题里有日文"))
        if re.search(r"\bmedicated\b", title, re.I) and re.search(r"\bwhiten", title, re.I):
            E.append(("title", "Medicated 和 Whitening 不能同时出现"))
        disp = S.display_name(vendor, brand) if vendor else ""
        if disp and not title.lower().startswith(disp.lower()):
            W.append(("title", f"标题没有以品牌展示名「{disp}」开头"))
        caps = list(_CAPACITY.finditer(title))
        tail = re.sub(r"\s*\(Pack of \d+\)\s*$", "", title)
        if caps and not tail.rstrip().endswith(caps[-1].group(0)):
            W.append(("title", "容量/规格看起来不在最后"))
        if re.search(r"[×xX]\s?\d+\s*$", title):
            E.append(("title", "件数要写 (Pack of N)，不写 ×N"))

    # ── handle ──
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

    # ── 分类 / 页面类型 ──
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

    # ── 标签 ──
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

    # ── 文案 ──
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
    if _s(row.get("sources")) and not re.search(r"https?://", _s(row.get("sources"))):
        E.append(("sources", "查证出处要写网页 URL"))
    return {"jan": jan, "errors": E, "warns": W}


def check_all(rows: list[dict], ctx_by_jan: dict, *, existing_jans: set, existing_handles: set,
              live_cat_tags: set) -> list[dict]:
    hs = [_s(r.get("handle")) for r in rows]
    dup = {h for h in hs if h and hs.count(h) > 1}
    js = [_s(r.get("jan")) for r in rows]
    out = []
    for r in rows:
        res = check_row(r, ctx_by_jan.get(_s(r.get("jan"))), existing_jans=existing_jans,
                        existing_handles=existing_handles, live_cat_tags=live_cat_tags, dup_handles=dup)
        if res["jan"] and js.count(res["jan"]) > 1:
            res["errors"].append(("jan", "表里同一个 JAN 出现了多行"))
        out.append(res)
    return out
