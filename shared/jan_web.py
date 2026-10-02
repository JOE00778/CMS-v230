"""JAN → 商品名/会社名/ジャンル(jancode.xyz)と 個装重量・個装サイズ(NETSEA → スーパーデリバリー)。

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
MAX_SHOPS = 4          # 1 サイトあたり詳細を見る店の上限(1 件 ≒ 検索 1 + 詳細 ≤4 リクエスト)
INTERVAL = 0.8         # 同一サイトへの連続リクエスト間隔(秒)。規約の「過度な負荷」対策

JANCODE_URL = "https://www.jancode.xyz/{jan}/"
NETSEA_SEARCH = "https://www.netsea.jp/search/?keyword={jan}"
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


@dataclass
class WeightHit:
    jan: str
    weight_g: float | None
    height_cm: float | None
    width_cm: float | None
    depth_cm: float | None
    source: str          # "netsea" | "superdelivery"
    url: str
    note: str = ""       # 軸の対応が推定のとき等


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
    return JanMeta(jan, name, maker, genre, status)


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


def parse_netsea_links(page: str) -> list[str]:
    urls = re.findall(r'href="(https://www\.netsea\.jp/shop/\d+/[^"?/#]+)"', page)
    return list(dict.fromkeys(urls))


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


def fetch_weight(jan: str, *, opener=None) -> WeightHit | None:
    """NETSEA → スーパーデリバリー の個装重量を **店舗横断で照合**して 1 つ返す。

    1 店だけを信じない。実例: 4971671192232(JD 実測 196g)は NETSEA 先頭店が「個装 19g」と
    桁を落として書いていて、先頭店採用だと 90% 外れた(2026-10-03)。
      · 重量を書いた店が 2 店以上あり中央値 ±30% で 2 店以上一致 → 一致した店のうち中央値に最も近い店
      · NETSEA だけで一致が取れなければ SD も集めて照合し直す
      · 1 店しか無い → その値(note に「1店舗のみ」)
      · 2 店以上あるのに一致しない → 重量は空(寸法は残す · note に各店の値)。推測で選ばない
    None は「調べ切って個装の記載なし」だけ。何も取れずに通信失敗が 1 つでもあれば FetchError
    (「取れなかった」を「無かった」にしない · fetch_many が error として残す)。
    """
    jan = str(jan).strip()
    hits: list[WeightHit] = []
    errors: list[str] = []
    for search, parse, source in ((NETSEA_SEARCH, parse_netsea_links, "netsea"),
                                  (SD_SEARCH, parse_sd_links, "superdelivery")):
        try:
            links = parse(_get(search.format(jan=urllib.parse.quote(jan)), opener))
        except FetchError as e:
            errors.append(str(e))
            continue
        hits += _kosou_hits(jan, links, source, opener, errors)
        weighed = [h for h in hits if h.weight_g is not None]
        if _agreeing(weighed):
            break                     # NETSEA だけで一致が取れたら SD は叩かない
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
                    ", ".join(f"{h.weight_g:g}g({h.source})" for h in dropped)
            return best
        base = next((h for h in hits if any((h.height_cm, h.width_cm, h.depth_cm))), hits[0])
        return WeightHit(jan, None, base.height_cm, base.width_cm, base.depth_cm,
                         base.source, base.url,
                         "店舗間で重量が一致せず空にした: " +
                         ", ".join(f"{h.weight_g:g}g({h.source})" for h in weighed))
    if weighed:
        h = weighed[0]
        h.note = (h.note + " " if h.note else "") + "1店舗のみ(未照合)"
        return h
    if hits:
        return hits[0]
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
