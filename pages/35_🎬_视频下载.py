"""模块 #35 视频下载 · 工具页.

贴视频链接下载最高画质 mp4 到本机。主流平台(YouTube 等)走 yt-dlp、
其它(LIPS 类)走真 Chrome 嗅探。后端 = workflow-automation/video-downloader 独立服务
(docker 内网 http://video-downloader:8000)。
当前 CMS 全开放模型下 require_admin = 仅登录(靠飞书门禁挡外人)。Boss 2026-07-01。
"""
from __future__ import annotations

import streamlit as st

st.set_page_config(page_title="视频下载", page_icon="🎬", layout="wide")
from shared.auth import require_admin  # noqa: E402
require_admin()
from shared.theme import inject_theme  # noqa: E402
inject_theme()
from shared.i18n import lang_selector, t  # noqa: E402
lang_selector()

from shared import video_dl_client as vdl  # noqa: E402

st.title(t("🎬 视频下载"))
st.caption(t("贴视频链接，下载最高画质 mp4 到本机 · 支持 YouTube 等主流平台 + LIPS 类小站"))

url = st.text_input(t("视频链接"), placeholder="https://...")

if st.button(t("解析"), type="primary") and url:
    try:
        with st.spinner(t("解析中…")):
            st.session_state["vdl_info"] = vdl.probe(url)
            st.session_state["vdl_url"] = url
    except Exception as e:
        st.error(t("解析失败：") + str(e))

info = st.session_state.get("vdl_info")
if info and st.session_state.get("vdl_url") == url:
    c1, c2, c3 = st.columns(3)
    c1.metric(t("标题"), (info.get("title") or "-")[:40])
    c2.metric(t("引擎"), info.get("engine"))
    dur = info.get("duration")
    c3.metric(t("时长(秒)"), dur if dur else "-")

    if st.button(t("下载最高画质"), type="primary"):
        bar = st.progress(0, text=t("下载中…"))

        def _p(n):
            bar.progress(min(n / (300 * 1024 * 1024), 1.0),
                         text=t("已接收 ") + f"{n // 1024 // 1024} MB")

        try:
            with st.spinner(t("正在从源站下载并传输，请稍候…")):
                data, fname = vdl.download(url, progress=_p)
            bar.progress(1.0, text=t("完成"))
            st.download_button(t("保存到我的电脑"), data=data, file_name=fname,
                               mime="video/mp4", type="primary")
        except Exception as e:
            st.error(t("下载失败：") + str(e))
