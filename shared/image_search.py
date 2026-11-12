"""JAN → 商品主图（page22 画像検索 と page15 商品登録 が共用）.

page22 から関数を原様で移した（2026-11-09 · page15 の斑马登録で同じ抓図を使うため）。
楽天 API 主 → kakaku 兜底。結果は PG nst.item_image_cache にキャッシュ。
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from functools import lru_cache

KAKAKU_SEARCH_FMT = "https://search.kakaku.com/{jan}/"
# kakaku 商品卡主图: <img ... class="p-item_visual_entity" ...>
# 两步匹配 — src/data-src 与 class 顺序在实际 HTML 中是不固定的
KAKAKU_IMG_TAG_RE = re.compile(
    r'<img[^>]*p-item_visual_entity[^>]*>',
    re.IGNORECASE,
)
KAKAKU_SRC_IN_TAG_RE = re.compile(
    r'(?:src|data-src)="(https?://[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
    re.IGNORECASE,
)
# 兜底：第一张 data-src 楽天/Amazon 直链
KAKAKU_FALLBACK_RE = re.compile(
    r'data-src="(https?://(?:tshop\.r10s\.jp|m\.media-amazon\.com|img\.kakaku\.k-img\.com)[^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"',
    re.IGNORECASE,
)
HTTP_TIMEOUT = 10
MIN_IMAGE_BYTES = 2000
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _parse_jans(raw: str) -> list[str]:
    """去重 + 仅保留 8-13 位数字 JAN。"""
    seen, out = set(), []
    for line in re.split(r"[\s,;]+", raw or ""):
        j = line.strip()
        if not j or not j.isdigit() or not (8 <= len(j) <= 14):
            continue
        if j not in seen:
            seen.add(j)
            out.append(j)
    return out


def _load_cache(conn, jans: list[str]) -> dict[str, dict]:
    if not jans:
        return {}
    placeholder = ",".join(["%s"] * len(jans))
    rows = conn.execute(
        f"SELECT jan_cd, image_url, source, status, bytes_size, captured_at "
        f"FROM nst.item_image_cache WHERE jan_cd IN ({placeholder})",
        jans,
    ).fetchall()
    return {r["jan_cd"]: dict(r) for r in rows}


def _fetch_kakaku(jan: str) -> tuple[str, bytes | None, int, str | None, str | None]:
    """两步抓 kakaku.com：搜索页解析商品图直链(楽天/Amazon CDN) → 抓图。

    返回 (status, bytes_or_None, size, error_msg, image_url)
    """
    search_url = KAKAKU_SEARCH_FMT.format(jan=jan)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw)
            # kakaku 是 shift_jis
            html = raw.decode("shift_jis", errors="ignore")
    except urllib.error.HTTPError as e:
        return ("error", None, 0, f"search HTTP {e.code}", None)
    except Exception as e:
        return ("error", None, 0, f"search {str(e)[:160]}", None)

    # 优先 p-item_visual_entity 主图（kakaku 商品卡）— 两步：找含 class 的 img tag，再从中提 src
    img_url = None
    tag_m = KAKAKU_IMG_TAG_RE.search(html)
    if tag_m:
        src_m = KAKAKU_SRC_IN_TAG_RE.search(tag_m.group(0))
        if src_m:
            img_url = src_m.group(1)
    if not img_url:
        # 兜底任意楽天/Amazon CDN data-src
        fb_m = KAKAKU_FALLBACK_RE.search(html)
        if fb_m:
            img_url = fb_m.group(1)
    if not img_url:
        return ("not_found", None, 0, "no item img in kakaku search", None)

    # 升级分辨率: kakaku 给的 ?fitin=300:300 太小, 改 800:800
    # (楽天 tshop.r10s.jp CDN 支持任意 fitin 参数, 自动放大压缩)
    if "tshop.r10s.jp" in img_url and "fitin=" in img_url:
        img_url = re.sub(r"fitin=\d+:\d+", "fitin=800:800", img_url)

    img_headers = {
        "User-Agent": USER_AGENT,
        "Referer": search_url,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    }
    try:
        req2 = urllib.request.Request(img_url, headers=img_headers)
        with urllib.request.urlopen(req2, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read()
            if len(data) < MIN_IMAGE_BYTES:
                return ("not_found", None, len(data), f"size<{MIN_IMAGE_BYTES}", img_url)
            return ("ok", data, len(data), None, img_url)
    except urllib.error.HTTPError as e:
        return ("error", None, 0, f"img HTTP {e.code}", img_url)
    except Exception as e:
        return ("error", None, 0, f"img {str(e)[:160]}", img_url)


# ───────────────────────── Rakuten ItemSearch API (fallback) ─────────────────────────

# 2026-04-01 新版 endpoint · 新 UUID app 必须走这个 + accessKey 双参数
RAKUTEN_API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401"
RAKUTEN_APP_ID_ENV = "RAKUTEN_APPLICATION_ID"
RAKUTEN_ACCESS_KEY_ENV = "RAKUTEN_ACCESS_KEY"


# 已知带水印 / 第三方加印的店铺关键词（shopCode 或 shopName 含这些字符串则跳过）
# 命名规律: 楽天直营大店 + @cosme 系列 + 其他常见 logo 店
_WATERMARK_SHOP_KEYWORDS = [
    "cosme", "luminous", "atcosme", "アットコスメ", "@cosme",
    "rakuten24", "rakuten-24", "rakuten 24", "楽天24",
    "soukai", "爽快",            # 爽快ドラッグ（有 logo）
    "e-zaiko",                   # e-在庫
    "kenkocom", "ケンコーコム",   # ケンコーコム
    "rakuten-direct",            # 楽天 Direct
    "lohaco",                    # Yahoo LOHACO 入楽天
]

# 极高文字密度阈值（兜底，超过此值即使无关键词也视为带水印）
OCR_TEXT_RATIO_HARD_LIMIT = 0.20

# OCR 文本中含这些关键词 → 判定为水印图（不区分大小写）
# 注: OCR 可能简繁字符混杂识别, 关键词需 cover 多种变体
_OCR_WATERMARK_TEXTS = [
    # 楽天直营 / @cosme 系列
    "rakuten 24", "rakuten24", "楽天24", "rakuten",
    "@cosme", "cosme shopping", "コスメ", "アットコスメ", "@COSME",
    # 其他常见水印店
    "soukai", "爽快", "ケンコーコム", "kenko.com",
    "lohaco", "ヨドバシ", "yodobashi",
    # 正規品 simp+trad 双形
    "正規品", "正规品", "official", "公式",
    # 楽天/Shop 常见促销文案 (店家额外加上的字样, 非品牌包装)
    "最大", "ポイント", "倍",
    "セール", "off", "%off", "％off", "クーポン",
    "送料無料", "送料无料",
]


@lru_cache(maxsize=1)
def _get_ocr():
    """懒加载 RapidOCR (rapidocr-onnxruntime, ~50MB 含 ONNX 模型)。

    未安装时返回 None, 调用方应 fallback 到无 OCR 模式（仅靠 shop 黑名单过滤）。
    """
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()
    except ImportError:
        return None
    except Exception:
        return None


def _image_watermark_score(image_bytes: bytes) -> tuple[bool, float, str]:
    """评估图水印程度。返回 (is_watermark, text_ratio, reason)。

    is_watermark=True 表示**确定**有水印（命中关键词或文字超阈值）
    text_ratio 用于在 is_watermark=False 的候选中比较 (越小越干净)
    """
    ocr = _get_ocr()
    if ocr is None:
        return (False, 0.0, "ocr_disabled")
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        if w * h == 0:
            return (False, 0.0, "invalid_image")
        result, _ = ocr(image_bytes)
        if not result:
            return (False, 0.0, "no_text")

        total_area = 0.0
        all_texts = []
        for entry in result:
            box = entry[0] if isinstance(entry, (list, tuple)) else None
            text = entry[1] if len(entry) > 1 else ""
            if not box or len(box) < 4:
                continue
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            total_area += (max(xs) - min(xs)) * (max(ys) - min(ys))
            all_texts.append(str(text))

        ratio = total_area / (w * h)
        joined = " | ".join(all_texts).lower()
        for kw in _OCR_WATERMARK_TEXTS:
            if kw.lower() in joined:
                return (True, ratio, f"keyword:{kw}")
        if ratio > OCR_TEXT_RATIO_HARD_LIMIT:
            return (True, ratio, f"high_density:{ratio:.2f}")
        return (False, ratio, f"clean ratio={ratio:.2f}")
    except Exception as e:
        return (False, 1.0, f"ocr_error:{type(e).__name__}")


# 向后兼容旧名（如果别处引用）
def _image_has_watermark(image_bytes: bytes) -> tuple[bool, str]:
    is_wm, _, reason = _image_watermark_score(image_bytes)
    return (is_wm, reason)


def _fetch_rakuten_api(jan: str) -> tuple[str, bytes | None, int, str | None, str | None]:
    """楽天 ItemSearch API · v2026-04 新 endpoint, 需 applicationId + accessKey + Origin。

    hits=10 多候选, 跳过已知带水印的 shop（@cosme 等）, 选第一个干净的。
    mediumImageUrls strip ?_ex 后拿原图（5-10x 缩略图大小）。

    返回 (status, bytes_or_None, size, error_msg, image_url)
    status='disabled' 表示 ENV 未配齐。
    """
    app_id = os.environ.get(RAKUTEN_APP_ID_ENV, "").strip()
    access_key = os.environ.get(RAKUTEN_ACCESS_KEY_ENV, "").strip()
    if not app_id or not access_key:
        return ("disabled", None, 0,
                "RAKUTEN_APPLICATION_ID / RAKUTEN_ACCESS_KEY 未配齐", None)

    params = {
        "applicationId": app_id,
        "accessKey": access_key,
        "keyword": jan,
        "hits": "30",  # 多候选用于跳过水印店（楽天 API 上限 30）
        "format": "json",
        "imageFlag": "1",
    }
    api_url = f"{RAKUTEN_API_URL}?{urllib.parse.urlencode(params)}"
    # 楽天 v2026-04 Web Application 类 app 走 CORS 标准: Origin 必填且在 allowed_websites 白名单内
    # 实测 Referer 不被识别, Origin 才被识别
    api_headers = {
        "User-Agent": USER_AGENT,
        "Origin": "https://smikie-cms.cc",
    }
    try:
        req = urllib.request.Request(api_url, headers=api_headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read()
            j = json.loads(data.decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")[:120]
        except Exception:
            body = ""
        return ("error", None, 0, f"rakuten API HTTP {e.code} {body}", None)
    except Exception as e:
        return ("error", None, 0, f"rakuten API {str(e)[:160]}", None)

    items = j.get("Items") or []
    if not items:
        return ("not_found", None, 0, "rakuten API no items", None)

    # 第一步: shop 黑名单过滤
    candidates = []  # list of (item_dict, raw_url)
    for entry in items:
        item = entry.get("Item") or {}
        shop_code = (item.get("shopCode") or "").lower()
        shop_name = (item.get("shopName") or "").lower()
        if any(kw in shop_code or kw in shop_name for kw in _WATERMARK_SHOP_KEYWORDS):
            continue
        for k in ("mediumImageUrls", "smallImageUrls"):
            imgs = item.get(k) or []
            if imgs:
                cand = imgs[0]
                cand_url = cand.get("imageUrl") if isinstance(cand, dict) else cand
                if cand_url:
                    candidates.append((item, cand_url))
                    break

    # 第二步: 对前 8 个候选跑 OCR 评分 → 选 ratio 最低的非水印图
    img_url = None
    ocr_available = _get_ocr() is not None
    if ocr_available and candidates:
        scored = []
        for item, cand_url in candidates[:8]:
            try:
                probe_url = cand_url.split("?")[0] + "?_ex=400x400"
                req_p = urllib.request.Request(probe_url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req_p, timeout=8) as r:
                    probe_bytes = r.read()
                is_wm, ratio, _reason = _image_watermark_score(probe_bytes)
                if not is_wm:
                    scored.append((ratio, cand_url))
            except Exception:
                continue
        if scored:
            # 文字密度最低 = 最干净（接近 0 = 纯产品图）
            scored.sort(key=lambda x: x[0])
            img_url = scored[0][1]

    # 第三步: OCR 没找到干净的 → fallback 用第一个 shop 过滤后的候选
    if not img_url and candidates:
        img_url = candidates[0][1]
    # 第四步: shop 全被黑名单过滤 → 退回第一个原始 item
    if not img_url and items:
        first = items[0].get("Item") or {}
        for k in ("mediumImageUrls", "smallImageUrls"):
            imgs = first.get(k) or []
            if imgs:
                cand = imgs[0]
                img_url = cand.get("imageUrl") if isinstance(cand, dict) else cand
                if img_url:
                    break
    if not img_url:
        return ("not_found", None, 0, "rakuten API item w/o image", None)

    # mediumImageUrls 带 ?_ex=128x128, 去掉拿原图 (5-10x 大小)
    img_url_clean = img_url.split("?")[0]
    img_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
    }
    try:
        req2 = urllib.request.Request(img_url_clean, headers=img_headers)
        with urllib.request.urlopen(req2, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read()
            if len(data) < MIN_IMAGE_BYTES:
                return ("not_found", None, len(data), f"size<{MIN_IMAGE_BYTES}", img_url_clean)
            return ("ok", data, len(data), None, img_url_clean)
    except urllib.error.HTTPError as e:
        return ("error", None, 0, f"img HTTP {e.code}", img_url_clean)
    except Exception as e:
        return ("error", None, 0, f"img {str(e)[:160]}", img_url_clean)


def _fetch_image_for_jan(jan: str) -> tuple[str, bytes | None, int, str | None, str | None, str]:
    """双轨抓图：楽天 API 主（原图质量高 + 跳水印店）→ kakaku 兜底。

    返回 (status, bytes_or_None, size, error_msg, image_url, source)
    """
    # 楽天 API 优先（已配 ENV 时）
    st1, data1, size1, err1, url1 = _fetch_rakuten_api(jan)
    if st1 == "ok":
        return (st1, data1, size1, err1, url1, "rakuten_api")
    # 楽天未配置 or 失败 → kakaku 兜底
    st2, data2, size2, err2, url2 = _fetch_kakaku(jan)
    if st2 == "ok":
        return (st2, data2, size2, err2, url2, "kakaku")
    if st1 == "disabled":
        return (st2, data2, size2, err2, url2, "kakaku")
    merged_err = f"rakuten:{err1 or st1} | kakaku:{err2 or st2}"
    return (st2, None, 0, merged_err, url1 or url2, "rakuten+kakaku")


# Backward aliases
_fetch_jancode = _fetch_kakaku


def _upsert_cache(conn, jan: str, url: str | None, source: str | None,
                  status: str, bytes_size: int, error_msg: str | None) -> None:
    conn.execute(
        """
        INSERT INTO nst.item_image_cache
            (jan_cd, image_url, source, status, bytes_size, error_msg, captured_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (jan_cd) DO UPDATE SET
            image_url   = EXCLUDED.image_url,
            source      = EXCLUDED.source,
            status      = EXCLUDED.status,
            bytes_size  = EXCLUDED.bytes_size,
            error_msg   = EXCLUDED.error_msg,
            updated_at  = now()
        """,
        (jan, url, source, status, bytes_size, error_msg),
    )


def search_jan(jan: str) -> str:
    """JAN 1 件の画像 URL（page15 の斑马登録・page47 の斑马登録内容で共用）: PG キャッシュ → 楽天 API → kakaku。
    取れたらキャッシュへ。取れなければ空文字。"""
    from shared.db import get_connection
    with get_connection() as conn:
        hit = _load_cache(conn, [jan]).get(jan)
        if hit and hit.get("status") == "ok" and hit.get("image_url"):
            return hit["image_url"]
        status, _, size, err, url, source = _fetch_image_for_jan(jan)
        _upsert_cache(conn, jan, url, source if status == "ok" else None,
                      status, size if status == "ok" else 0, None if status == "ok" else err)
        conn.commit()
    return url if status == "ok" else ""
