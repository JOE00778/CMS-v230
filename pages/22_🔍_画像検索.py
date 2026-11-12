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

import io
import time
import urllib.parse
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
st.caption(t("JAN → 主图自动抓取（kakaku.com 搜索结果首图，取自楽天/Amazon CDN）· 结果缓存到 PG · ZIP 一键下载"))

from shared.image_search import (  # noqa: E402 — 抓図ロジックは shared に移した
    HTTP_TIMEOUT, KAKAKU_SEARCH_FMT, MIN_IMAGE_BYTES, USER_AGENT,
    _fetch_image_for_jan, _load_cache, _parse_jans, _upsert_cache,
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

_PAGE22_RESULT_KEYS = [
    "_image_search_results", "_image_search_bytes",
    "_image_search_zip", "_image_search_zip_count",
]


def _clear_page22_state():
    for k in _PAGE22_RESULT_KEYS:
        st.session_state.pop(k, None)
    st.session_state.pop("page22_jan_text", None)


col_input, col_opt = st.columns([3, 1])
with col_input:
    jan_text = st.text_area(
        t("JAN 列表（每行一个，支持空格/逗号/分号分隔 · ≤ 1000 件）"),
        height=180,
        placeholder="4901234567890\n4905678901234\n...",
        key="page22_jan_text",
    )
with col_opt:
    force_refetch = st.checkbox(t("强制重新抓取（忽略缓存）"), value=False)
    max_items = st.number_input(t("单次最多"), min_value=10, max_value=1000, value=200, step=50)
    btn_run = st.button(t("🚀 开始抓取"), type="primary", use_container_width=True)
    if st.button(t("🗑️ 清除结果"), use_container_width=True,
                 help=t("清除上次抓取结果和 JAN 输入，准备下一批")):
        _clear_page22_state()
        st.rerun()

jans_all = _parse_jans(jan_text)
if jans_all:
    st.caption(t("解析到 {n} 个有效 JAN（去重后）").format(n=len(jans_all)))

if btn_run:
    if not jans_all:
        st.warning(t("请先贴 JAN 列表"))
        st.stop()

    # 修「第二次没法操作」：开新抓取前清掉旧结果残留
    for k in _PAGE22_RESULT_KEYS:
        st.session_state.pop(k, None)

    jans = jans_all[: int(max_items)]
    if len(jans_all) > len(jans):
        st.info(t("超过单次上限，仅处理前 {n} 件，剩余 {rest} 件请分批").format(n=len(jans), rest=len(jans_all) - len(jans)))

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

            progress.progress(i / len(jans), text=t("抓取中 {i}/{n} · cache={c} ok={ok} fail={ng}").format(
                i=i, n=len(jans), c=n_cache, ok=n_new_ok, ng=n_new_fail))
            if i % 10 == 0:
                conn.commit()

        conn.commit()
        progress.empty()
        status_box.success(t(
            "完成 · 总 {n} 件：cache={c} · 新抓成功={ok} · 失败={ng}"
        ).format(n=len(jans), c=n_cache, ok=n_new_ok, ng=n_new_fail))

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
        with st.expander(t("❌ 失败 {n} 件 · 后续可手动补 URL").format(n=len(df_fail)), expanded=False):
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
            t("⬇️ 下载 ZIP（{n} 张）").format(n=st.session_state.get('_image_search_zip_count', 0)),
            data=zip_bytes,
            file_name=f"JAN画像_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            type="primary",
        )
else:
    st.info(t("贴 JAN 列表后点「开始抓取」"))
