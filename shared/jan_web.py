"""JAN → 商品名/会社名/ジャンル(jancode.xyz)と 個装重量・個装サイズ(スーパーデリバリー)。

2026-10-03 Boss: NETSEA は使わない(robots.txt が AI クローラ不可・規約 第7条2項三)。
下の NETSEA の書式メモは parse_kosou が読める書式の記録として残す(取得はしない)。

商品登録 v2 用。shared/jan_lookup.py(楽天/Yahoo・成分)とは別物:あちらは会社名も重量も取れない。

実測(2026-10-03 · 対照 JAN 6 件の実 HTML):
- jancode.xyz 詳細 `/<JAN>/` は `<th>商品名</th><td>…</td>` の表。ジャンルは `<a>` を `&gt`(セミコロン無し)で連結。
  Mac からは 403、元川からは 200。**403/429/通信失敗は status=error**(「該当なし」と混同しない)。
- NETSEA 検索 `/search/?keyword=<JAN>` → `https://www.netsea.jp/shop/<店>/<品>` が表示順に並ぶ。
  詳細の書式は店ごとに違う(全部「個装」の行だけ採る):
    `個装サイズ(cm)・重量(g):縦13.5奥行4.5横7.6重量195`
    `個装サイズ：W140×D58×H220（mm）／422（g）`
    `個装サイズ(約) 幅3.5×奥行32×高さ15cm`(同じ頁の「重量（約）70g」は本体正味 → 採らない)
  「商品サイズ：264×208×213(mm)＋ケース入数」だけの店はケース寸法 → 採らない。
- スーパーデリバリー 検索 `/p/do/psl/?word=<JAN>` → `/p/r/pd_p/<id>/`。
  詳細の `個装重量：200g` は採る。`ケースサイズ/ケース重量/ケース入数` だけの品は採らない(÷入数は過大)。
- 軸ラベル: 縦/高さ/高/H=高さ · 横/幅/W=幅 · 奥行/奥/D=奥行。ラベルが無い 3 辺は並び順のまま入れ note=軸推定。
"""
from __future__ import annotations

import html
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from shared.jan_lookup import UA, decode

TIMEOUT = 15
MAX_SHOPS = 4          # 詳細を見る店の上限(1 件 ≒ 検索 1 + 詳細 ≤4 リクエスト)
INTERVAL = 0.8         # 同一サイトへの連続リクエスト間隔(秒)。規約の「過度な負荷」対策

JANCODE_URL = "https://www.jancode.xyz/{jan}/"
SD_SEARCH = "https://www.superdelivery.com/p/do/psl/?word={jan}"
SD_BASE = "https://www.superdelivery.com"


@dataclass
class JanMeta:
    jan: str
    name: str
    maker: str
    genre: str
    status: str          # "ok" | "not_found" | "error"
    error: str = ""
    image_url: str = ""  # 詳細ページの商品画像（https 絶対 URL・無ければ空）
    source: str = "jancode"   # "jancode" | "rakuten"（楽天の商品名を jancode 形式に整えたもの・要確認）


@dataclass
class WeightHit:
    jan: str
    weight_g: float | None
    height_cm: float | None
    width_cm: float | None
    depth_cm: float | None
    source: str          # "superdelivery"
    url: str
    note: str = ""       # 軸の対応が推定のとき等
    field_urls: dict[str, str] | None = None


class FetchError(Exception):
    """HTTP エラー・タイムアウト。「無い」ではなく「取れなかった」。"""


def _default_open(req, timeout=TIMEOUT):
    return urllib.request.urlopen(req, timeout=timeout)


def _get(url: str, opener=None) -> str:
    """404 は "" を返す(本当に無い)。それ以外の失敗は FetchError。"""
    req = urllib.request.Request(url, headers=UA)
    try:
        with (opener or _default_open)(req, timeout=TIMEOUT) as r:
            return decode(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        raise FetchError(f"http={e.code} {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"{type(e).__name__} {url}") from e


def _cell_text(fragment: str) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return re.sub(r"\s+", " ", t).strip()


# ---------- jancode.xyz ----------

def _th_td(page: str, label: str) -> str:
    m = re.search(rf"<th[^>]*>\s*{label}\s*</th>\s*<td[^>]*>(.*?)</td>", page, re.S)
    return _cell_text(m.group(1)) if m else ""


def parse_jancode(jan: str, page: str) -> JanMeta:
    name, maker = _th_td(page, "商品名"), _th_td(page, "会社名")
    genre = re.sub(r"\s*>\s*", " > ", _th_td(page, "商品ジャンル"))
    status = "ok" if (name or maker) else "not_found"
    img = re.search(rf'<img[^>]+src="(/images/[^"]*{re.escape(jan)}[^"]*)"', page)
    return JanMeta(jan, name, maker, genre, status,
                   image_url="https://www.jancode.xyz" + img.group(1) if img else "")


def fetch_meta(jan: str, *, opener=None) -> JanMeta:
    jan = str(jan).strip()
    try:
        page = _get(JANCODE_URL.format(jan=jan), opener)
    except FetchError as e:
        return JanMeta(jan, "", "", "", "error", str(e))
    return parse_jancode(jan, page)


# ---------- 個装 重量・サイズ ----------

_AXIS = {"縦": "height", "高さ": "height", "高": "height", "H": "height",
         "横": "width", "幅": "width", "W": "width",
         "奥行き": "depth", "奥行": "depth", "奥": "depth", "D": "depth"}
_AXIS_RE = re.compile(r"(?<![A-Za-z])(奥行き|奥行|奥|高さ|高|縦|横|幅|W|H|D)\s*:?\s*(\d+(?:\.\d+)?)")
_TRIPLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)")
_W_LABEL_RE = re.compile(r"重量\s*:?\s*(\d+(?:\.\d+)?)\s*(kg|g)?", re.I)
_W_UNIT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*\(?\s*(kg|g)(?![a-z])", re.I)


def _lines(page: str) -> list[str]:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S | re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", t))
    t = unicodedata.normalize("NFKC", t)
    return [x.strip() for x in t.split("\n") if x.strip()]


def _kosou_segments(lines: list[str]) -> list[str]:
    """「個装」以降の文字列。個装入数は数量なので除外。本体/外装/ケース以降は別物なので切る。"""
    out = []
    for ln in lines:
        for m in re.finditer(r"個装", ln):
            seg = ln[m.start():]
            if seg.startswith("個装入数"):
                continue
            out.append(re.split(r"本体|外装|ケース", seg)[0])
    return out


def _parse_segment(seg: str) -> tuple[float | None, dict, str]:
    weight = None
    m = _W_LABEL_RE.search(seg) or _W_UNIT_RE.search(seg)
    if m:
        weight = float(m.group(1)) * (1000 if (m.group(2) or "").lower() == "kg" else 1)
    size_part = _W_LABEL_RE.sub("", seg)
    dims, note = {}, ""
    for lab, val in _AXIS_RE.findall(size_part):
        dims.setdefault(_AXIS[lab], float(val))
    if len(dims) != 3:
        dims = {}
        t = _TRIPLE_RE.search(size_part)
        if t:
            dims = dict(zip(("height", "width", "depth"), map(float, t.groups())))
            note = "軸推定(並び順のまま)"
    if dims:
        if "mm" in size_part.lower():
            dims = {k: round(v / 10, 2) for k, v in dims.items()}
        elif "cm" not in size_part.lower():
            note = (note + " 単位推定(cm)").strip()
    return weight, dims, note


def parse_kosou(page: str) -> tuple[float | None, dict, str]:
    """詳細ページ → (個装重量g, {height,width,depth} cm, note)。個装表記が無ければ (None, {}, "")。"""
    weight, dims, notes = None, {}, []
    for seg in _kosou_segments(_lines(page)):
        w, d, n = _parse_segment(seg)
        if weight is None and w is not None:
            weight = w
        if not dims and d:
            dims = d
            if n:
                notes.append(n)
    return weight, dims, " ".join(notes)


def parse_sd_links(page: str) -> list[str]:
    paths = re.findall(r'href="(/p/r/pd_p/\d+/)"', page)
    return [SD_BASE + p for p in dict.fromkeys(paths)]


AGREE = 0.30           # 店舗間で「同じ値」とみなす幅(中央値 ±30%)


def _kosou_hits(jan: str, links: list[str], source: str, opener,
                errors: list[str]) -> list[WeightHit]:
    """個装の記載がある店を最大 MAX_SHOPS 件ぶん全部集める。
    詳細 1 件の取得失敗は errors に記録して次の店へ(1 件の 403 で残りの店を捨てない)。"""
    hits: list[WeightHit] = []
    for url in links[:MAX_SHOPS]:
        time.sleep(INTERVAL)
        try:
            page = _get(url, opener)
        except FetchError as e:
            errors.append(str(e))
            continue
        if jan not in page:          # おすすめ枠など別商品のリンクを拾わない
            continue
        w, d, note = parse_kosou(page)
        if w is not None or d:
            hits.append(WeightHit(jan, w, d.get("height"), d.get("width"), d.get("depth"),
                                  source, url, note))
    return hits


def _agreeing(hits: list[WeightHit]) -> list[WeightHit]:
    """重量が中央値 ±AGREE に入る店だけ。2 店以上そろわなければ空。"""
    if len(hits) < 2:
        return []
    ws = sorted(h.weight_g for h in hits)
    mid = (ws[(len(ws) - 1) // 2] + ws[len(ws) // 2]) / 2
    ok = [h for h in hits if abs(h.weight_g - mid) <= mid * AGREE]
    return ok if len(ok) >= 2 else []


def fetch_superdelivery_weight(jan: str, *, opener=None) -> WeightHit | None:
    """スーパーデリバリーの個装重量を **店舗横断で照合**して 1 つ返す。

    1 店だけを信じない。実例: 4971671192232(JD 実測 196g)は先頭店が「個装 19g」と
    桁を落として書いていて、先頭店採用だと 90% 外れた(2026-10-03)。
      · 重量を書いた店が 2 店以上あり中央値 ±30% で 2 店以上一致 → 一致した店のうち中央値に最も近い店
      · 1 店しか無い → その値(note に「1店舗のみ」)
      · 2 店以上あるのに一致しない → 重量は空(寸法は残す · note に各店の値)。推測で選ばない
    None は「調べ切って個装の記載なし」だけ。何も取れずに通信失敗が 1 つでもあれば FetchError
    (「取れなかった」を「無かった」にしない · fetch_many が error として残す)。
    """
    jan = str(jan).strip()
    errors: list[str] = []
    links = parse_sd_links(_get(SD_SEARCH.format(jan=urllib.parse.quote(jan)), opener))
    hits = _kosou_hits(jan, links, "superdelivery", opener, errors)
    weighed = [h for h in hits if h.weight_g is not None]
    if len(weighed) >= 2:
        ok = _agreeing(weighed)
        if ok:
            ws = sorted(h.weight_g for h in ok)
            mid = ws[len(ws) // 2]
            best = min(ok, key=lambda h: abs(h.weight_g - mid))
            dropped = [h for h in weighed if h not in ok]
            if dropped:
                best.note = (best.note + " " if best.note else "") + "外れ値を除外: " + \
                    ", ".join(f"{h.weight_g:g}g" for h in dropped)
            return best
        base = next((h for h in hits if any((h.height_cm, h.width_cm, h.depth_cm))), hits[0])
        return WeightHit(jan, None, base.height_cm, base.width_cm, base.depth_cm,
                         base.source, base.url,
                         "店舗間で重量が一致せず空にした: " +
                         ", ".join(f"{h.weight_g:g}g" for h in weighed))
    if weighed:
        h = weighed[0]
        h.note = (h.note + " " if h.note else "") + "1店舗のみ(未照合)"
        return h
    if hits:
        return hits[0]
    if errors:
        raise FetchError("; ".join(errors))
    return None


def parse_netde_package(jan: str, page: str, url: str) -> WeightHit | None:
    """卸の管理番号を照合し、単品出荷単位だけ採る。本体・ケース・内容量は採らない。"""
    if _th_td(page, "商品管理番号") != jan:
        return None
    text = unicodedata.normalize("NFKC", _th_td(page, "サイズ"))
    size = re.search(r"【単品サイズ】(.*?)(?=【|$)", text)
    weight = re.search(r"【単品重量】\s*(\d+(?:\.\d+)?)\s*(kg|g)\b", text, re.I)
    dims = _parse_segment("個装サイズ " + size.group(1))[1] if size and re.search(r"mm|cm", size.group(1), re.I) else {}
    if dims and any(v <= 0 for v in dims.values()):
        dims = {}
    w = float(weight.group(1)) * (1000 if weight.group(2).lower() == "kg" else 1) if weight else None
    if w is not None and w <= 0:
        w = None
    if not dims and w is None:
        return None
    return WeightHit(jan, w, dims.get("height"), dims.get("width"), dims.get("depth"),
                     "netde", url, "卸の単品出荷単位（本体・ケース・内容量は不採用）")


def fetch_netde_weight(jan: str, *, opener=None) -> WeightHit | None:
    page = _get("https://netdeoroshi.com/list.php?keyword=" + urllib.parse.quote(jan), opener)
    page = page.split('class="__block __block--recommend"')[0]
    ids = list(dict.fromkeys(re.findall(r"product\.php\?id=(\d+)", page)))[:3]
    errors = []
    for pid in ids:
        time.sleep(INTERVAL)
        url = "https://netdeoroshi.com/product.php?id=" + pid
        try:
            hit = parse_netde_package(jan, _get(url, opener), url)
            if hit:
                return hit
        except FetchError as e:
            errors.append(str(e))
    if errors:
        raise FetchError("; ".join(errors))
    return None


def fetch_weight(jan: str, *, opener=None) -> WeightHit | None:
    """SuperDelivery → 卸。欠けた値だけ補完し、競合・取得失敗も記録する。"""
    fields = ("weight_g", "height_cm", "width_cm", "depth_cm")
    result, errors = None, []
    for fn in (fetch_superdelivery_weight, fetch_netde_weight):
        try:
            hit = fn(jan, opener=opener)
        except FetchError as e:
            errors.append(str(e))
            continue
        if hit is None:
            continue
        if result is None:
            result = hit
            result.field_urls = {f: hit.url for f in fields if getattr(hit, f) is not None}
        else:
            filled, conflicts = [], []
            for field in fields:
                old, new = getattr(result, field), getattr(hit, field)
                if old is None and new is not None:
                    setattr(result, field, new)
                    result.field_urls[field] = hit.url
                    filled.append(field)
                elif old is not None and new is not None and old != new:
                    conflicts.append(f"{field}: {old:g} / {new:g}")
            if filled:
                result.source += " + " + hit.source
                result.note += " · 補完=" + "/".join(filled) + " " + hit.url
            if conflicts:
                result.note += " · 重量・寸法の源間不一致（先の値を保持・確認してください）: " + ", ".join(conflicts)
        if all(getattr(result, f) is not None for f in fields):
            break
    if result:
        if errors:
            result.note += " · 別の源の取得失敗: " + "; ".join(errors)
        return result
    if errors:
        raise FetchError("; ".join(errors))
    return None


def fetch_many(jans, fn, *, interval: float = INTERVAL, on_progress=None) -> dict:
    """fn(jan) を順に呼ぶ。例外は {"error": "..."} として結果に残す(黙って落とさない)。"""
    jans = list(jans)
    out: dict = {}
    for i, jan in enumerate(jans):
        if i:
            time.sleep(interval)
        try:
            out[jan] = fn(jan)
        except Exception as e:  # noqa: BLE001 — 1 件の失敗で全体を止めない
            out[jan] = {"error": f"{type(e).__name__}: {e}"}
        if on_progress:
            on_progress(i + 1, len(jans), jan)
    return out


# ---------- 楽天フォールバック（Boss 2026-11-10: jancode が制限されたら楽天・名前は jancode の形式で） ----------
# jancode の商品名 = メーカー登録名「[メーカー/ブランド] [商品名] [規格・色] [容量]」（空白区切り・容量が末尾・販促語なし）。
# 楽天は店ごとの出品名（【販促】[JAN] ■投函■・×24個のセット・容量の後ろに検索語の羅列）→ 下の規則で jancode 形式に寄せ、
# 店をまたいだ多数決で 1 つに決める。2026-11-10 実測（片岡物産 辻利 / ロッシ / hiritu / ギャツビー / タミヤ）で調整。
# 「×N」はセット（30g×24個・x3)）。ただし 8×4 / 8x4（エイトフォー）のように × の直前が数字・英字なら商品名とみなす
# 単位の直後の ×N（30g×24・(12g×8本)×24）は別の枝で拾う
_SET_RE = re.compile(r"(kg|g|ml|mL|L|個|本|袋|枚|入|\))\s*[×xX]\s*\d|(?<![\dA-Za-z])[×xX]\s*\d+\s*(個|袋|本|箱|缶|パック|セット|枚|入|\)|\s|$)|\d+\s*(個|本|袋|箱|パック|点|缶)\s*(セット|組|入り?\]?$)|\d+\s*セット|まとめ買い|"
                     r"\[セット\]|【[^】]*\d+[^】]*個[^】]*】|\d+\s*個\s*】")
_DROP_RE = re.compile(r"【[^】]*】|\[[^\]]*\]|［[^］]*］|■[^■]*■|◆[^◆]*◆|JAN[：:]?\s*\d+|\(?\b\d{8}(\d{5})?\b\)?|"
                      r"送料無料|お一人様\d+点まで|取り寄せ商品|キャンセル不可|メール便[^\s]*|ネコポス[^\s]*|"
                      r"\(\s*JAN[^)]*\)")
_PROMO_HEAD_RE = re.compile(r"^.*?(ポイント\d+倍|クーポン|エントリー|まで)[^☆★！!]*[☆★！!]+\s*")
_VOL_RE = re.compile(r"\(?\s*(\d+(?:\.\d+)?)\s*(kg|mg|g|ml|mL|ML|L|l|個入り?|枚入り?|本入り?|包入り?|錠|粒)\s*(入)?\s*\)?",)


def jancode_style_name(title: str) -> str:
    """楽天の出品名 1 件を jancode 形式に。セット品・空は ""。"""
    t = unicodedata.normalize("NFKC", str(title or "")).replace("\u3000", " ")
    if not t.strip() or _SET_RE.search(t):
        return ""
    t = _PROMO_HEAD_RE.sub("", t)
    t = _DROP_RE.sub(" ", t)
    t = re.sub(r"^\s*(公式|新品)\s*", "", t)
    m = _VOL_RE.search(t)
    if m:   # 容量の後ろは検索語の羅列 → 切る。「(80g)」は「 80g」に
        t = t[:m.start()].rstrip() + " " + m.group(1) + m.group(2).replace("mL", "ml").replace("ML", "ml").rstrip("入り")
    return re.sub(r"\s+", " ", t).strip(" -/・")


def pick_jancode_style(titles) -> str:
    """店をまたいだ合議。語（空白区切り）が半数以上の店に出る＝核、少数の店だけ＝検索語の羅列。
    各候補を「核の語数 − 少数語の数」で採点し最高点（同点は同じ書き方の店が多い → 短い）を採る。"""
    from collections import Counter
    names = [n for n in (jancode_style_name(x) for x in titles) if n]
    if not names:
        return ""
    freq = Counter(tok for n in names for tok in set(n.split()))
    half = len(names) / 2
    same = Counter(names)

    def score(n: str):
        toks = n.split()
        return (sum(freq[x] >= half for x in toks) - sum(freq[x] < half for x in toks), same[n], -len(n))
    return max(same, key=score)


def rakuten_meta(jan: str) -> JanMeta:
    """楽天 ItemSearch API（公式・page22 / page39 と同じ資格情報）で JAN を引き、jancode 形式の名前を返す。"""
    import json as _json
    import os
    import urllib.parse
    from shared.jan_lookup import RAKUTEN_API, RAKUTEN_ORIGIN, UA as _UA
    app_id = (os.environ.get("RAKUTEN_APPLICATION_ID") or "").strip()
    key = (os.environ.get("RAKUTEN_ACCESS_KEY") or "").strip()
    if not (app_id and key):
        return JanMeta(jan, "", "", "", "error", "楽天 API の資格情報なし", source="rakuten")
    q = urllib.parse.urlencode({"applicationId": app_id, "accessKey": key, "keyword": jan, "hits": "30",
                                "format": "json"})
    req = urllib.request.Request(f"{RAKUTEN_API}?{q}", headers={**_UA, "Origin": RAKUTEN_ORIGIN})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                items = [x.get("Item", {}) for x in _json.loads(r.read().decode("utf-8")).get("Items", [])]
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:   # 楽天は 1 req/秒 程度
                time.sleep(1.5)
                continue
            return JanMeta(jan, "", "", "", "error", f"楽天 http={e.code}", source="rakuten")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            return JanMeta(jan, "", "", "", "error", f"楽天 {type(e).__name__}: {e}", source="rakuten")
    name = pick_jancode_style(it.get("itemName", "") for it in items)
    img = next((u.get("imageUrl", "").split("?")[0] for it in items for u in (it.get("mediumImageUrls") or [])
                if str(u.get("imageUrl", "")).startswith("https://")), "")
    return JanMeta(jan, name, "", "", "ok" if name else "not_found", image_url=img, source="rakuten")


def yahoo_meta(jan: str) -> JanMeta:
    """Yahoo!ショッピング の検索結果（page39 と同じ取り方・1 リクエスト）から、楽天と同じ規則で jancode 形式の名前。"""
    from shared.jan_lookup import _YAHOO_CHROME, YAHOO_SEARCH, _get, decode as _decode
    raw = _get(YAHOO_SEARCH.format(jan=jan))
    if raw is None:
        return JanMeta(jan, "", "", "", "error", "Yahoo 取得失敗", source="yahoo")
    page = _decode(raw)
    alts = []
    for a in re.findall(r'alt="([^"]{12,140})"', page):
        a = html.unescape(a).strip()
        if any(x in a for x in _YAHOO_CHROME) or re.search(r"スタンプ|対象|ポイント|クーポン|ストア|バナー|ランキング", a):
            continue
        if a.endswith(("...", "…")):     # 省略された alt は最後の切れた語ごと落とす
            a = a.rstrip(".…").rsplit(" ", 1)[0]
        alts.append(a)
    name = pick_jancode_style(alts)
    return JanMeta(jan, name, "", "", "ok" if name else "not_found", source="yahoo")


JANCODE_LIMIT = 40   # 1 回の処理で jancode を叩く上限（約 50 件連続で 403 になる・2026-11-10 実測 41 件目で遮断）


def make_meta_fetcher(limit: int = JANCODE_LIMIT):
    """jancode → 楽天。jancode は 403/429 が 1 回出たらこの処理中は二度と叩かない（制限の延長を防ぐ）・
    上限 limit 件。jancode に名前が無い品も楽天で引く。"""
    state = {"n": 0, "blocked": False}

    def fetch(jan: str) -> JanMeta:
        meta = None
        if not state["blocked"] and state["n"] < limit:
            state["n"] += 1
            meta = fetch_meta(jan)
            if meta.status == "error" and re.search(r"http=(403|429)", meta.error or ""):
                state["blocked"] = True
            elif meta.status == "ok" and (meta.name or "").strip():
                return meta
        rk = rakuten_meta(jan)
        if rk.status != "ok":
            time.sleep(INTERVAL)
            yh = yahoo_meta(jan)                 # 楽天にも無ければ Yahoo!ショッピング
            if yh.status == "ok":
                rk = yh
        if rk.status == "ok":
            if meta is not None and meta.maker:      # jancode に会社名だけある場合は残す
                rk.maker = meta.maker
            return rk
        return meta or JanMeta(jan, "", "", "", "error",
                               ("jancode 制限中" if state["blocked"] else "jancode 上限") + f" · {rk.error or '楽天・Yahoo にも無し'}",
                               source="rakuten")

    fetch.state = state
    return fetch

