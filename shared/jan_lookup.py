"""JAN → 商品名/全成分 外部查询(合规检测用 · page39)。

背景:上架前要判定的品**天然不在**自建站/NST 库里,所以必须能按 JAN 从外部取回
商品名与全成分,否则页面只能让人手动粘贴,等于没做。

源(按可靠度顺序,全部免密钥):
1. 楽天24(rakuten24):商品页 URL 直接用 JAN,含「【全成分】」段 → 名称+成分
   ※ 页面编码 EUC-JP,必须显式解码,否则成分抽取全落空
2. 楽天全国スーパー(西友):同样 JAN 可寻址,食品类覆盖较好 → 名称(成分多为空)
3. Yahoo!ショッピング 検索:兜底只取商品名(店铺页普遍不含全成分)

取不到就诚实返回空(调用方显示「外部も未収録」并允许粘贴),不猜、不编。
网络访问在元川容器内进行(日本住宅线路,已实测可达)。
"""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.request

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept-Language": "ja,en;q=0.8",
}
TIMEOUT = 12
MAX_INGREDIENT = 2000

# JAN をそのまま商品コードに使う店(実測で確認)。成分の載る率が高い順。
JAN_ADDRESSABLE = (
    ("https://item.rakuten.co.jp/rakuten24/{jan}/", "楽天24"),
    ("https://item.rakuten.co.jp/matsukiyo/{jan}/", "マツキヨ楽天店"),
    ("https://item.rakuten.co.jp/sundrug/{jan}/", "サンドラッグ楽天店"),
    ("https://netsuper.rakuten.co.jp/seiyu/item/{jan}/", "楽天全国スーパー"),
)
YAHOO_SEARCH = "https://shopping.yahoo.co.jp/search?p={jan}"

# 成分の見出しは店ごとにバラバラ:【全成分】/ 全成分: / 成分／分量 / 原料・成分等【成分】
_ING_RE = re.compile(
    r"(?:【\s*)?(?:全成分|成分\s*[／/・]\s*分量|原料\s*・\s*成分等|配合成分|成分)"
    # 下限 15:成分 5 品目程度の短い表もあるため。散文の誤検出は区切り記号チェックで弾く。
    r"\s*(?:】|[::]|\s)\s*([^【]{15,%d})" % MAX_INGREDIENT)
_TAG_RE = re.compile(r"<script.*?</script>|<style.*?</style>", re.S | re.I)
_YAHOO_CHROME = ("Yahoo!ショッピング", "ふるさと納税", "PayPay", "ログイン", "カート")
# 商品が無くても 200 でサイト共通ページを返す店がある(楽天全国スーパー等)→
# 「商品名が取れた」と誤認しないためのサイト共通タイトル排除。
_GENERIC_TITLE = ("ネットスーパー", "エラー", "見つかりません", "ページが存在",
                  "楽天市場", "検索結果", "お探しの商品")


def _get(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as r:
            return r.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None


def decode(raw: bytes) -> str:
    """楽天は EUC-JP、他は UTF-8。誤判定を避けるため実際に日本語が出る方を採る。"""
    for enc in ("utf-8", "euc-jp", "shift_jis"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "�" not in text[:20000]:
            return text
    return raw.decode("utf-8", errors="replace")


def _plain(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_ingredient(page_text: str) -> str:
    """成分表らしさを検証してから返す(「有効成分について」等の散文を拾わない)。"""
    text = _plain(page_text)
    for m in _ING_RE.finditer(text):
        body = re.split(r"※|広告文責|内容量|原産国|メーカー|区分\s*[:：]|お問い合わせ", m.group(1))[0]
        body = body.strip(" 、,")
        # 成分表は区切り記号が多い。3 個未満は説明文とみなし採用しない。
        if body.count("、") + body.count(",") >= 3:
            return body[:MAX_INGREDIENT]
    return ""


def parse_rakuten_title(page_text: str) -> str:
    """『【楽天市場】<商品名>：<店舗名>』から商品名だけ取り出す。"""
    m = re.search(r"<title>(.*?)</title>", page_text, re.S)
    if not m:
        return ""
    title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    title = re.sub(r"^【楽天市場】", "", title)
    title = re.split(r"[:：]", title)[0]
    return title.strip()


def parse_yahoo_name(page_text: str) -> str:
    for cand in re.findall(r'alt="([^"]{12,100})"', page_text):
        name = html.unescape(cand).strip()
        if not any(x in name for x in _YAHOO_CHROME):
            return name
    return ""


def lookup(jan: str) -> dict:
    """JAN → {name, ingredient, source, url}。何も取れなければ空 dict。"""
    jan = (jan or "").strip()
    if not (jan.isdigit() and len(jan) >= 8):
        return {}

    best: dict = {}
    for url_tpl, label in JAN_ADDRESSABLE:
        url = url_tpl.format(jan=jan)
        raw = _get(url)
        if not raw:
            continue
        text = decode(raw)
        name = parse_rakuten_title(text)
        # 商品ページである確証:JAN がページ内にあり、かつサイト共通タイトルでない
        if not name or jan not in text or any(g in name for g in _GENERIC_TITLE):
            continue
        hit = {"name": name, "ingredient": parse_ingredient(text), "source": label, "url": url}
        if hit["ingredient"]:
            return hit          # 成分まで取れたら即採用
        best = best or hit      # 名称だけの店は保持し、成分のある店を探し続ける

    if not best:
        raw = _get(YAHOO_SEARCH.format(jan=jan))
        if raw:
            name = parse_yahoo_name(decode(raw))
            if name:
                best = {"name": name, "ingredient": "", "source": "Yahoo!ショッピング",
                        "url": YAHOO_SEARCH.format(jan=jan)}
    return best
