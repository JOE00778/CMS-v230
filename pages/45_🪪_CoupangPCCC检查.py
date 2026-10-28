"""模块 #45 Coupang PCCC 检查：斑马「待审核」的 Coupang 订单 → 关税厅核对（经 GSI Express）→ 列出有误订单。

Boss 2026-10-28。逻辑在 shared.coupang_pccc_audit（照合本体は shared.pccc_check）。
个人信息只在本次会话内存中，不写数据库/文件；画面打码，CSV 只含订单号与原因。
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Coupang PCCC检查", page_icon="🪪", layout="wide")

from shared import banma_client  # noqa: E402
from shared import coupang_pccc_audit as A  # noqa: E402
from shared.auth import require_admin  # noqa: E402
from shared.db import get_connection  # noqa: E402
from shared.i18n import lang_selector, t  # noqa: E402
from shared.theme import inject_theme  # noqa: E402

require_admin()
inject_theme()
lang_selector()

st.title(t("🪪 Coupang PCCC 检查"))
st.caption(t("一键：拉取斑马中 Coupang「待审核」订单的收件人 PCCC → 按 GSI Express 格式提交关税厅核对 → 列出有误的订单"))
st.info(t("「有误」不等于 PCCC 无效：姓名、电话、邮编与关税厅登记不一致也会报错。请看原因后判断。"
          "个人信息只在本次页面内存中，不保存；表格已打码，下载的 CSV 只含订单号和原因。"))

if not banma_client.is_configured():
    st.error(t("斑马 API 未配置（BANMA_APP_ID / BANMA_APP_SECRET）"))
    st.stop()

if st.button(t("🔍 一键检查"), type="primary", use_container_width=True):
    try:
        client = banma_client.BanmaClient.from_env()
        with get_connection() as conn:
            banma_client.ensure_token(conn, client)
        bar = st.progress(0.0, text=t("拉取斑马订单…"))
        targets, missing = A.fetch_pending(
            client, on_progress=lambda n, tot: bar.progress(n / tot, text=f"{t('拉取斑马订单…')} {n}/{tot}"))
        bar.progress(1.0, text=t("提交关税厅核对…"))
        rows, err = A.audit(targets)
        bar.empty()
        st.session_state["p45_result"] = {"rows": rows, "missing": missing, "err": err}
    except Exception as e:  # noqa: BLE001
        st.error(t("检查失败：") + f"{type(e).__name__}: {e}")

res = st.session_state.get("p45_result")
if res:
    df = pd.DataFrame(res["rows"], columns=["display_id", "order_id", "verdict", "reason", "message",
                                            "name_masked", "pccc_masked"])
    n_ok = int((df["verdict"] == A.VERDICT_OK).sum())
    n_ng = int((df["verdict"] == A.VERDICT_NG).sum())
    n_un = int((df["verdict"] == A.VERDICT_UNCHECKED).sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(t("待审核订单"), len(df) + len(res["missing"]))
    c2.metric(t("正常"), n_ok)
    c3.metric(t("PCCC 有误"), n_ng)
    c4.metric(t("未能检查"), n_un + len(res["missing"]))
    if res["err"]:
        st.warning(t("核对工具返回异常：") + res["err"])
    if res["missing"]:
        st.warning(t("以下斑马订单取不到收件地址，未检查：") + "、".join(res["missing"]))

    bad = df[df["verdict"] != A.VERDICT_OK]
    if bad.empty:
        st.success(t("全部正常，没有 PCCC 有误的订单。"))
    else:
        show = bad.rename(columns={
            "display_id": t("Coupang 订单号"), "order_id": t("斑马订单ID"), "verdict": t("结果"),
            "reason": t("原因"), "message": t("关税厅原文"), "name_masked": t("收件人"),
            "pccc_masked": "PCCC"})
        st.subheader(t("需要处理的订单"))
        st.dataframe(show, use_container_width=True, hide_index=True)
        csv = bad[["display_id", "order_id", "verdict", "reason", "message"]].to_csv(index=False).encode("utf-8-sig")
        st.download_button(t("⬇️ 下载有误订单（CSV）"), data=csv, file_name="coupang_pccc_ng.csv", mime="text/csv")
