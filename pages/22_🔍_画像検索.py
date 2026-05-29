"""模块 #22 画像検索 · JAN → 商品主图 ZIP 下载

由原 商品登録ツール HTML 中 tab3「JAN 画像検索・ダウンロード」原生化迁移而来。

抓取策略 v2 (2026-05-29 切换源):
  jancode.xyz 整站升级风控,所有 server-side 访问 403 → 切 kakaku.com
  1. PG cache hit (nst.item_image_cache) → 直接返回
  2. GET https://search.kakaku.com/<JAN>/  (shift_jis HTML)
     → 解析 <img class="p-item_visual_entity"> 的 src/data-src
     → 取第一张作主图(实际是楽天/Amazon CDN 直链, 不被 ban)
  3. 抓图 → 入 cache + bytes_map
  4. 都没 → status=not_found
  ※ 容器内 urllib 直出, 无 127.0.0.1 代理; 仅访问公开图片站;
     外发数据仅 JAN 13 位条码(公开非敏感)。

输出: 进度表 + ZIP 下载(仅成功项)。
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile
import zlib
from datetime import datetime
from functools import lru_cache

import pandas as pd
import streamlit as st

from shared.auth import require_password
from shared.db import get_connection
from shared.i18n import t, lang_selector
from shared.theme import inject_theme

st.set_page_config(page_title=t("画像検索"), page_icon="🔍", layout="wide")
require_password()
inject_theme()
lang_selector()

st.title(t("🔍 画像検索"))
st.caption(t("JAN → 主图自动抓取（kakaku.com 搜索结果首图，取自楽天/Amazon CDN）· 结果缓存到 PG · ZIP 一键下载"))

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


# ───────────────────────── UI ─────────────────────────

with st.expander(t("📌 抓取规则与数据安全说明"), expanded=False):
    st.markdown(t(
        "- **抓取源**：kakaku.com 搜索首图（实际图来自楽天/Amazon CDN）→ 失败时 fallback 楽天 ItemSearch API（若已配 ENV `RAKUTEN_APPLICATION_ID`）\n"
        "- **旧源 jancode.xyz**：整站升级风控对 server-side 全 403，已弃\n"
        "- **来源标记**：`kakaku` / `rakuten_api` · cache 区分两路径命中\n"
        "- **缓存**：命中过的 JAN 直接返回，不重复抓\n"
        "- **外发数据**：仅 JAN 13 位条码（公开非敏感）· 无 127.0.0.1 代理\n"
        "- **失败标记**：抓不到的标 `not_found`，后续可手动补 URL\n"
        "- **下载产物**：ZIP 仅包含成功抓取的 JPG"
    ))

col_input, col_opt = st.columns([3, 1])
with col_input:
    jan_text = st.text_area(
        t("JAN 列表（每行一个，支持空格/逗号/分号分隔 · ≤ 1000 件）"),
        height=180,
        placeholder="4901234567890\n4905678901234\n...",
    )
with col_opt:
    force_refetch = st.checkbox(t("强制重新抓取（忽略缓存）"), value=False)
    max_items = st.number_input(t("单次最多"), min_value=10, max_value=1000, value=200, step=50)
    btn_run = st.button(t("🚀 开始抓取"), type="primary", use_container_width=True)

jans_all = _parse_jans(jan_text)
if jans_all:
    st.caption(t(f"解析到 {len(jans_all)} 个有效 JAN（去重后）"))

if btn_run:
    if not jans_all:
        st.warning(t("请先贴 JAN 列表"))
        st.stop()

    jans = jans_all[: int(max_items)]
    if len(jans_all) > len(jans):
        st.info(t(f"超过单次上限，仅处理前 {len(jans)} 件，剩余 {len(jans_all) - len(jans)} 件请分批"))

    with get_connection() as conn:
        cache = {} if force_refetch else _load_cache(conn, jans)

        results: list[dict] = []
        bytes_map: dict[str, bytes] = {}

        progress = st.progress(0.0, text=t("抓取中..."))
        status_box = st.empty()
        n_cache = n_new_ok = n_new_fail = 0

        for i, jan in enumerate(jans, 1):
            hit = cache.get(jan)
            if hit and hit["status"] == "ok":
                results.append({
                    "jan": jan,
                    "url": hit["image_url"],
                    "source": hit["source"],
                    "status": "ok (cache)",
                    "size": hit["bytes_size"] or 0,
                    "captured_at": hit["captured_at"],
                })
                n_cache += 1
            else:
                status, data, size, err, img_url, source = _fetch_image_for_jan(jan)
                if status == "ok":
                    bytes_map[jan] = data
                    _upsert_cache(conn, jan, img_url, source, "ok", size, None)
                    results.append({
                        "jan": jan, "url": img_url, "source": source,
                        "status": "ok", "size": size, "captured_at": datetime.now(),
                    })
                    n_new_ok += 1
                else:
                    _upsert_cache(conn, jan, img_url, None, status, 0, err)
                    results.append({
                        "jan": jan, "url": img_url, "source": None,
                        "status": status, "size": 0, "captured_at": datetime.now(),
                    })
                    n_new_fail += 1
                # kakaku 走 2 个 HTTP, rakuten fallback 走 +2 个，礼貌限速
                time.sleep(0.4)

            progress.progress(i / len(jans), text=t(f"抓取中 {i}/{len(jans)} · cache={n_cache} ok={n_new_ok} fail={n_new_fail}"))
            if i % 10 == 0:
                conn.commit()

        conn.commit()
        progress.empty()
        status_box.success(t(
            f"完成 · 总 {len(jans)} 件：cache={n_cache} · 新抓成功={n_new_ok} · 失败={n_new_fail}"
        ))

    st.session_state["_image_search_results"] = results
    st.session_state["_image_search_bytes"] = bytes_map

# ───────────────────────── 结果展示 ─────────────────────────

results = st.session_state.get("_image_search_results") or []
if results:
    df = pd.DataFrame(results)
    df_ok = df[df["status"].str.startswith("ok")].copy()
    df_fail = df[~df["status"].str.startswith("ok")].copy()

    c1, c2, c3 = st.columns(3)
    c1.metric(t("总数"), len(df))
    c2.metric(t("成功"), len(df_ok))
    c3.metric(t("失败"), len(df_fail))

    st.subheader(t("📋 抓取结果"))
    # 缩略图列复用 url（streamlit 用 column_config.ImageColumn 直接 render 远程图）
    df_show = df[["jan", "status", "source", "size", "captured_at", "url"]].copy()
    df_show["缩略图"] = df_show["url"]
    df_show = df_show[["jan", "缩略图", "status", "source", "size", "captured_at", "url"]]
    st.dataframe(
        df_show,
        use_container_width=True,
        height=480,
        column_config={
            "jan": st.column_config.TextColumn("JAN", width="small"),
            "缩略图": st.column_config.ImageColumn(t("缩略图"), width="small"),
            "status": t("状态"),
            "source": t("来源"),
            "size": st.column_config.NumberColumn(t("字节"), format="%d"),
            "captured_at": t("抓取时间"),
            "url": st.column_config.LinkColumn("URL", width="large"),
        },
    )

    if df_fail.empty is False:
        with st.expander(t(f"❌ 失败 {len(df_fail)} 件 · 后续可手动补 URL"), expanded=False):
            st.dataframe(df_fail[["jan", "status"]], use_container_width=True, height=200)

    bytes_map = st.session_state.get("_image_search_bytes") or {}
    cache_only = [r for r in results
                  if r["status"] == "ok (cache)" and r["url"] and r["jan"] not in bytes_map]

    st.divider()
    st.subheader(t("📦 打包下载"))

    if st.button(t("⬇️ 重新下载缓存命中的图片并打包 ZIP"),
                 disabled=not (bytes_map or cache_only)):
        with st.spinner(t("下载并打包中...")):
            for r in cache_only:
                jan, url = r["jan"], r["url"]
                try:
                    req = urllib.request.Request(url, headers={
                        "User-Agent": USER_AGENT,
                        "Referer": KAKAKU_SEARCH_FMT.format(jan=jan),
                        "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
                    })
                    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                        data = resp.read()
                        if len(data) >= MIN_IMAGE_BYTES:
                            bytes_map[jan] = data
                except Exception:
                    pass

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for jan, data in bytes_map.items():
                    zf.writestr(f"{jan}.jpg", data)
            buf.seek(0)
            st.session_state["_image_search_zip"] = buf.getvalue()
            st.session_state["_image_search_zip_count"] = len(bytes_map)

    zip_bytes = st.session_state.get("_image_search_zip")
    if zip_bytes:
        st.download_button(
            t(f"⬇️ 下载 ZIP（{st.session_state.get('_image_search_zip_count', 0)} 张）"),
            data=zip_bytes,
            file_name=f"JAN画像_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            type="primary",
        )
else:
    st.info(t("贴 JAN 列表后点「开始抓取」"))
