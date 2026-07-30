"""加拿大の登録状況照会(DIN=薬品 / NPN=天然健康産品)。

Boss 2026-07-30 承認。**登録の有無で判定色は変わらない**——加国のライセンスは
現地の保有者と当該登録品に紐づき、日本市場版 SKU の越境販売を許すものではない。
価値は ①規則を「実査した事実」に変える ②大手が加国で持っている(=正規輸入ルート
が存在する)という例外を見つける、の 2 点。

- DIN(Drug Product Database):公式 API が brandname 検索に対応 → その場で照会。
- NPN(LNHPD):API は licence_number 検索のみ・全件 146MB。月次で取り込んだ
  `compliance.ca_npn_product` をローカルで引く(本モジュールは SQL を持たず、
  呼び出し側が渡した rows を整形するだけ)。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

DPD_API = "https://health-products.canada.ca/api/drug/drugproduct/"
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120 Safari/537.36")}
TIMEOUT = 15
# 商品名から検索語を作る:英字ブランド語を優先(DPD は英語 DB。日本語では当たらない)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z&'\-]{2,}")
_STOPWORDS = {"the", "and", "for", "with", "new", "set", "pack", "size", "ltd", "inc",
              "japan", "made", "type", "color", "clear", "mini", "plus", "gift"}


def brand_query(name: str, maker: str = "") -> str:
    """照会に使う英字語を 1 つ選ぶ。無ければ空(=照会不能を呼び出し側で表示)。"""
    for src in (maker, name):
        for w in _WORD_RE.findall(src or ""):
            if w.lower() not in _STOPWORDS:
                return w
    return ""


def dpd_by_brand(brand: str) -> list[dict] | None:
    """DPD をブランド名で照会。→ ヒット行(最大 20)/ 空リスト=未登録 / None=照会失敗。

    None と空リストは意味が違う(失敗を「未登録」と読ませない)。
    """
    brand = (brand or "").strip()
    if len(brand) < 3:
        return None
    url = DPD_API + "?" + urllib.parse.urlencode(
        {"lang": "en", "type": "json", "brandname": brand})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                    timeout=TIMEOUT) as r:
            rows = json.loads(r.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not isinstance(rows, list):
        return None
    out = []
    for x in rows[:20]:
        out.append({
            "din": str(x.get("drug_identification_number") or ""),
            "brand": str(x.get("brand_name") or ""),
            "class": str(x.get("class_name") or ""),
            "status": str(x.get("status") or x.get("drug_code") or ""),
        })
    return out
