"""模块 · 斑马体积重量检查（Boss 2026-10-29）。

斑马 Export 部门有效 SKU 的三边/重量：缺失的按 JDL 实测 > NST 包装重量补值，组合品按单品计算，
生成可直接上传斑马「产品 → 批量修改SKU信息」的 Excel（≤1000 行/个・成本价留空=不改）。
补不了的列为问题清单。判定规则见 shared/banma_dims.py 文件头。

数据源（PG・本页只读，「立即刷新」只置 run_now）:
  · banma.sku_dims         斑马 SKU 快照（pull_sku_dims · jdl_scheduler 每日 04:00 JST）
  · jdl.v_goods_dimensions JDL 仓库实测毛重/三边
  · nst.item_master_raw    NST 包装重量 package_weight_g
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import streamlit as st

from shared.banma_dims import build, table_xlsx, upload_zip
from shared.db import get_connection, get_readonly_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("斑马体积重量检查"), page_icon="📐", layout="wide")
from shared.auth import require_password  # noqa: E402

require_password()
from shared.theme import inject_theme  # noqa: E402

inject_theme()
lang_selector()

st.title(t("📐 斑马体积重量检查"))
st.caption(t("斑马 Export 部门 SKU 的三边/重量：缺失的按 JDL 实测、NST 包装重量补，组合品按单品计算 · "
             "下载的 Excel 可直接在斑马「批量修改SKU信息」上传"))

JOB = "banma_sku_dims_daily"
_SKU_SQL = ("SELECT sku_id, code, spu_id, spu_title, specification, category_top, sku_type, is_valid, "
            "weight, length, width, height, combine, pulled_at FROM banma.sku_dims")
_JDL_SQL = ("SELECT jan, wms_gross_weight_g, wms_length_mm, wms_width_mm, wms_height_mm "
            "FROM jdl.v_goods_dimensions")
_NST_SQL = ("SELECT jan, max(package_weight_g) AS w FROM nst.item_master_raw "
            "WHERE jan IS NOT NULL AND package_weight_g > 0 GROUP BY jan")


@st.cache_data(ttl=300, show_spinner=False)
def _load():
    conn = get_readonly_connection()
    try:
        skus = [dict(r) for r in conn.execute(_SKU_SQL).fetchall()]
        jdl = {r["jan"]: (r["wms_gross_weight_g"], (r["wms_length_mm"], r["wms_width_mm"], r["wms_height_mm"]))
               for r in conn.execute(_JDL_SQL).fetchall()}
        nst = {r["jan"]: r["w"] for r in conn.execute(_NST_SQL).fetchall()}
    finally:
        conn.close()
    for s in skus:
        if isinstance(s["combine"], str):
            s["combine"] = json.loads(s["combine"])
    return skus, jdl, nst


# ── 刷新 ──
c1, c2, c3 = st.columns([2, 2, 3])
wconn = get_connection()
try:
    job = wconn.execute("SELECT run_now, last_run_at, last_status FROM jdl.pull_schedule WHERE job_key=%s",
                        (JOB,)).fetchone()
except Exception:
    wconn.rollback()
    job = None
if job is None:
    c1.warning(t("排程未登记（banma 002 迁移未应用？）"))
else:
    c2.caption(t("上次拉取") + ": " + (str(job["last_run_at"])[:16] if job["last_run_at"] else "-")
               + " · " + str(job["last_status"] or "-"))
    if job["run_now"]:
        c1.info(t("⏳ 拉取中（约 3 分钟），完成后刷新本页"))
    elif c1.button(t("▶️ 立即从斑马拉取"), type="primary"):
        try:
            wconn.execute("UPDATE jdl.pull_schedule SET run_now=TRUE, updated_at=now() WHERE job_key=%s", (JOB,))
            wconn.commit()
            st.cache_data.clear()
            st.success(t("已预约，1 分钟内开始，约 3 分钟完成"))
            st.rerun()
        except Exception as e:
            wconn.rollback()
            st.error(str(e))

try:
    skus, jdl, nst = _load()
except Exception as e:
    st.error(t("读取失败") + f": {e}")
    st.stop()
if not skus:
    st.warning(t("banma.sku_dims 为空，请先点「立即从斑马拉取」"))
    st.stop()

pulled = max(s["pulled_at"] for s in skus)
c3.caption(t("斑马数据时点") + f": {str(pulled)[:16]}")
if pd.Timestamp.now(tz="UTC") - pd.Timestamp(pulled) > pd.Timedelta(hours=26):
    st.warning(t("斑马数据超过 1 天未更新，上传前请先点「立即从斑马拉取」"))

rows, issues = build(skus, jdl, nst)
scope = [s for s in skus if s.get("is_valid") and s.get("category_top") == "Export"]
stamp = dt.datetime.now().strftime("%Y%m%d")

m1, m2, m3, m4 = st.columns(4)
m1.metric(t("Export 有效 SKU"), f"{len(scope):,}")
m2.metric(t("可上传补值"), f"{len(rows):,}")
m3.metric(t("其中补重量"), f"{sum(r['重量(g)'] is not None for r in rows):,}")
m4.metric(t("待人工 / 无数据源"), f"{len(issues):,}")

tab_up, tab_issue = st.tabs([t("✅ 可上传斑马"), t("⚠️ 问题清单")])
with tab_up:
    if rows:
        n_files = (len(rows) - 1) // 1000 + 1
        st.download_button(
            t("📥 下载斑马导入文件") + f"（{n_files} " + t("个") + " + " + t("核对明细") + "，zip）",
            upload_zip(rows, stamp), file_name=f"斑马批量修改SKU_体积重量_{stamp}.zip",
            mime="application/zip", type="primary")
        st.caption(t("每个文件 ≤1000 行；空栏 = 斑马不修改；上传后点「立即从斑马拉取」，已上传的会从本表消失"))
        st.dataframe(pd.DataFrame(rows), use_container_width=True, height=520, hide_index=True)
    else:
        st.success(t("没有需要补的 SKU"))
with tab_issue:
    if issues:
        df = pd.DataFrame(issues)
        kinds = df["问题"].value_counts()
        pick = st.multiselect(t("问题类型"), list(kinds.index),
                              format_func=lambda k: f"{k}（{kinds[k]}）")
        show = df[df["问题"].isin(pick)] if pick else df
        st.download_button(t("📥 下载问题清单"), table_xlsx(show.to_dict("records"), "问题清单"),
                           file_name=f"斑马体积重量_问题清单_{stamp}.xlsx")
        st.dataframe(show, use_container_width=True, height=520, hide_index=True)
    else:
        st.success(t("没有问题 SKU"))
