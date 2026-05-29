"""模块 #22 画像検索 · JAN → 商品主图 ZIP 下载

由原 商品登録ツール HTML 中 tab3「JAN 画像検索・ダウンロード」原生化迁移而来。

抓取策略（数据安全方案 A · 2026-05-29 Boss 拍板）：
  1. PG cache hit (nst.item_image_cache) → 直接返回
  2. urllib HEAD jancode.xyz/images/<JAN>.jpg → 200 + size > 8KB → ok
  3. 都没 → status=not_found，用户后续可手动补 URL
  ※ 容器内 urllib 直出，无 127.0.0.1 代理；公网仅访问公开图片站；
     外发数据仅 JAN 13 位条码（公开非敏感）。

输出：进度表 + ZIP 下载（仅成功项）。
"""
from __future__ import annotations

import io
import re
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime

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
st.caption(t("JAN → 主图自动抓取（jancode.xyz）· 结果缓存到 PG · ZIP 一键下载"))

JANCODE_URL_FMT = "https://www.jancode.xyz/images/{jan}.jpg"
HTTP_TIMEOUT = 10
MIN_IMAGE_BYTES = 8000
USER_AGENT = "Mozilla/5.0 (compatible; CMS-image-fetch/1.0)"


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


def _fetch_jancode(jan: str) -> tuple[str, bytes | None, int, str | None]:
    """返回 (status, bytes_or_None, size, error_msg)."""
    url = JANCODE_URL_FMT.format(jan=jan)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = resp.read()
            if len(data) < MIN_IMAGE_BYTES:
                return ("not_found", None, len(data), f"size<{MIN_IMAGE_BYTES}")
            return ("ok", data, len(data), None)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ("not_found", None, 0, "404")
        return ("error", None, 0, f"HTTP {e.code}")
    except Exception as e:
        return ("error", None, 0, str(e)[:200])


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
        "- **抓取源**：jancode.xyz（公开商品图直链）\n"
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
                status, data, size, err = _fetch_jancode(jan)
                if status == "ok":
                    url = JANCODE_URL_FMT.format(jan=jan)
                    bytes_map[jan] = data
                    _upsert_cache(conn, jan, url, "jancode", "ok", size, None)
                    results.append({
                        "jan": jan, "url": url, "source": "jancode",
                        "status": "ok", "size": size, "captured_at": datetime.now(),
                    })
                    n_new_ok += 1
                else:
                    _upsert_cache(conn, jan, None, None, status, 0, err)
                    results.append({
                        "jan": jan, "url": None, "source": None,
                        "status": status, "size": 0, "captured_at": datetime.now(),
                    })
                    n_new_fail += 1
                time.sleep(0.15)  # 礼貌限速

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
    st.dataframe(
        df[["jan", "status", "source", "size", "captured_at", "url"]],
        use_container_width=True,
        height=400,
        column_config={
            "jan": st.column_config.TextColumn("JAN", width="small"),
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
    cache_only_jans = [r["jan"] for r in results
                        if r["status"] == "ok (cache)" and r["jan"] not in bytes_map]

    st.divider()
    st.subheader(t("📦 打包下载"))

    if st.button(t("⬇️ 重新下载缓存命中的图片并打包 ZIP"),
                 disabled=not (bytes_map or cache_only_jans)):
        with st.spinner(t("下载并打包中...")):
            for jan in cache_only_jans:
                url = JANCODE_URL_FMT.format(jan=jan)
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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
