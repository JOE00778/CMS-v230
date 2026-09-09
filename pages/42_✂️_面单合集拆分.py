"""模块 #42 ECMS 面单合集拆分工具。"""
from __future__ import annotations

import hashlib
from datetime import datetime

import pandas as pd
import streamlit as st

st.set_page_config(page_title="面单合集拆分", page_icon="✂️", layout="wide")

from shared.auth import require_admin  # noqa: E402
from shared.ecms_label_splitter import LabelPdfError, split_label_pdf  # noqa: E402
from shared.i18n import lang_selector, t  # noqa: E402
from shared.theme import inject_theme  # noqa: E402

require_admin()
inject_theme()
lang_selector()

st.title(t("✂️ 面单合集拆分"))
st.caption(t("上传一页一张的 ECMS 面单 PDF，按每页 13 位单号拆分并打包下载"))
st.info(t("面单只在 CMS 内存中处理，不上传外部服务，也不写入数据库或服务器文件。"))

uploaded = st.file_uploader(
    t("选择 ECMS 面单合集 PDF"),
    type=["pdf"],
    accept_multiple_files=False,
    key="ecms_label_pdf",
)

upload_bytes = uploaded.getvalue() if uploaded is not None else b""
source_fingerprint = (
    hashlib.sha256(upload_bytes).hexdigest() if upload_bytes else ""
)
if uploaded is not None:
    st.caption(f"{uploaded.name} · {len(upload_bytes):,} bytes")

if st.button(
    t("开始拆分"),
    type="primary",
    disabled=uploaded is None,
    use_container_width=True,
):
    try:
        with st.spinner(t("正在逐页识别并生成 ZIP…")):
            st.session_state["ecms_label_split_result"] = split_label_pdf(upload_bytes)
            st.session_state["ecms_label_split_source"] = source_fingerprint
    except LabelPdfError as exc:
        st.session_state.pop("ecms_label_split_result", None)
        st.session_state.pop("ecms_label_split_source", None)
        st.error(str(exc))
    except Exception:
        st.session_state.pop("ecms_label_split_result", None)
        st.session_state.pop("ecms_label_split_source", None)
        st.error(t("处理失败，请确认 PDF 未损坏后重试。"))

result = st.session_state.get("ecms_label_split_result")
result_source = st.session_state.get("ecms_label_split_source")
if result is not None and source_fingerprint and result_source == source_fingerprint:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(t("总页数"), result.total_pages)
    c2.metric(t("成功"), result.success_count)
    c3.metric(t("识别失败"), result.failed_count)
    c4.metric(t("重复单号"), result.duplicate_count)

    exceptions = [row for row in result.results if row.status != "success"]
    if exceptions:
        st.warning(t("部分页面需要人工检查；异常页面已保留在 ZIP 的“识别失败”目录。"))
        status_labels = {
            "unrecognized": t("未识别"),
            "ambiguous": t("多个候选"),
            "duplicate": t("重复单号"),
        }
        st.dataframe(
            pd.DataFrame(
                {
                    t("页码"): [row.page_number for row in exceptions],
                    t("状态"): [status_labels[row.status] for row in exceptions],
                    t("单号"): [row.tracking_number for row in exceptions],
                    t("原因"): [row.reason for row in exceptions],
                }
            ),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.success(t("全部页面识别成功。"))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    st.download_button(
        t("📦 整批下载 ZIP"),
        data=result.zip_bytes,
        file_name=f"面单拆分-{stamp}.zip",
        mime="application/zip",
        type="primary",
        use_container_width=True,
    )
