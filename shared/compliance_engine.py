"""合规判定引擎(page39 用)· 纯函数,规则行进、命中明细出,不碰 DB。

判定顺序(Boss 2026-07-29 拍板):**品类 → 名称/宣称 → 成分**。
品类先判,`blocking` 规则命中即**停止后续判定**——例:ピジョン(婴儿用品)在 US 需
CPSC 儿童产品证书(CPC),我方没有,则不必再看成分,结论已定。这既更准也更省
(批量筛查时可跳过外部成分取数)。

规则来源(PG compliance schema,只读):
- category_rule:品类闸门(match_terms 小写包含匹配:品牌名/L1・L2/商品名词)
- keyword_rule:名称/宣称正则词表(country ∈ 该国 | 'ALL')
- ingredient_rule:该国禁用/限用成分(match_terms 小写包含匹配)

口径:
- 「医薬部外品」在匹配前统一剔除(Boss 2026-07-22:部外品不报警,
  沿 shopify/scripts/audit_compliance.py 既定处理)。
- 成分为自由文本匹配:未命中≠合规;无成分数据时降级为仅名称判定(结果里显式标注)。
"""
from __future__ import annotations

import re

SEV_ORDER = {"red": 0, "yellow": 1, "info": 2}


def _clean(text: str | None) -> str:
    return (text or "").replace("医薬部外品", "")


def _excerpt(text: str, start: int, end: int, width: int = 30) -> str:
    return text[max(0, start - width):min(len(text), end + width)].replace("\n", " ").strip()


def check_keywords(rules, country: str, fields: dict[str, str]) -> list[dict]:
    """fields: {'商品名(日)': ..., '商品名(EN)': ...} → 命中明细。

    rules 行:(country, category, pattern, severity, note)(dict 或同名属性均可)。
    """
    hits = []
    for r in rules:
        r = dict(r) if not isinstance(r, dict) else r
        if r["country"] not in (country, "ALL"):
            continue
        try:
            rx = re.compile(r["pattern"], re.I)
        except re.error:
            continue  # 坏正则跳过,规则表经迁移审查,理论不该发生
        for fname, raw in fields.items():
            target = _clean(raw)
            m = rx.search(target)
            if m:
                hits.append({
                    "kind": "名称/宣称", "field": fname, "category": r["category"],
                    "severity": r["severity"], "note": r["note"],
                    "matched": _excerpt(target, m.start(), m.end()),
                })
                break  # 同一规则命中一个字段即可
    return hits


def check_ingredients(rules, country: str, ingredient_text: str | None,
                      title: str | None = None) -> list[dict]:
    """成分文本(+标题兜底,如「ハイドロキノン配合」直接写在品名)对禁限用清单。"""
    haystack = " ".join(x for x in (ingredient_text, title) if x).lower()
    if not haystack.strip():
        return []
    hits = []
    for r in rules:
        r = dict(r) if not isinstance(r, dict) else r
        if r["country"] != country:
            continue
        terms = r["match_terms"] or []
        term = next((t for t in terms if t and t.lower() in haystack), None)
        if term is None:
            continue
        sev = "red" if r["rule_type"] == "prohibited" else "yellow"
        idx = haystack.find(term.lower())
        hits.append({
            "kind": "成分", "field": "成分表" if ingredient_text else "商品名",
            "category": r["rule_type"], "severity": sev,
            "note": f"{r['ingredient']}({r.get('source_ref') or r.get('source', '')})"
                    + (f":{r['condition_note']}" if r.get("condition_note") else ""),
            "matched": _excerpt(haystack, idx, idx + len(term)),
        })
    return hits


def check_category(rules, country: str, signals: dict[str, str]) -> list[dict]:
    """品类闸门。signals: {'品牌': 'Pigeon', 'L1': 'Baby & Family', '商品名': ...}。

    品牌名で品類が判る品が多い(ピジョン/コンビ=ベビー)ため、ブランドも突き合わせる。
    """
    haystack = " ".join(_clean(v) for v in signals.values() if v).lower()
    if not haystack.strip():
        return []
    hits = []
    for r in rules:
        r = dict(r) if not isinstance(r, dict) else r
        if r["country"] not in (country, "ALL") or not r.get("enabled", True):
            continue
        term = next((t for t in (r["match_terms"] or []) if t and t.lower() in haystack), None)
        if term is None:
            continue
        idx = haystack.find(term.lower())
        hits.append({
            "kind": "品类", "field": "品类/品牌", "category": r["category"],
            "severity": r["severity"], "note": r["note"],
            "matched": _excerpt(haystack, idx, idx + len(term)),
            "blocking": bool(r.get("blocking")),
        })
    return hits


def judge(keyword_rules, ingredient_rules, country: str,
          fields: dict[str, str], ingredient_text: str | None,
          category_rules=None, category_signals: dict[str, str] | None = None) -> dict:
    """综合判定(品类 → 名称/宣称 → 成分)。

    品类の blocking ルールに当たったら**そこで確定**し、以降(名称・成分)は評価しない。
    返り値の stopped_at で「どこで確定したか」を呼び出し側に伝える。
    """
    hits: list[dict] = []
    if category_rules:
        signals = dict(category_signals or {})
        signals.setdefault("商品名", " ".join(fields.values()))
        hits = check_category(category_rules, country, signals)
        blocking = [h for h in hits if h.get("blocking") and h["severity"] == "red"]
        if blocking:
            return {
                "verdict": "red",
                "hits": sorted(hits, key=lambda h: SEV_ORDER.get(h["severity"], 9)),
                "ingredient_checked": False,
                "stopped_at": "category",
            }

    hits += check_keywords(keyword_rules, country, fields)
    hits += check_ingredients(ingredient_rules, country, ingredient_text,
                              " ".join(fields.values()))
    hits.sort(key=lambda h: SEV_ORDER.get(h["severity"], 9))
    verdict = "green"
    if any(h["severity"] == "red" for h in hits):
        verdict = "red"
    elif any(h["severity"] == "yellow" for h in hits):
        verdict = "yellow"
    return {
        "verdict": verdict,
        "hits": hits,
        "ingredient_checked": bool(ingredient_text and ingredient_text.strip()),
        "stopped_at": "full",
    }
