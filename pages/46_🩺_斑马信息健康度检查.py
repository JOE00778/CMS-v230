"""模块 · 斑马信息健康度检查（Boss 2026-10-29）。

范围: 斑马 Export 部门有效 SKU 中，NST 商品等级 A/B/C/NEW 的（组合品 = 组成单品全部有等级）。
检查: ① 三边/重量: 缺失的按 JDL 实测 > NST 包装重量补值，组合品按单品计算，
        生成可直接上传斑马「产品 → 批量修改SKU信息」的 Excel（≤1000 行/个・成本价留空=不改）；补不了的列问题。
      ② 报关信息: 有无、英文名（标准报关名・组合品 Nset）、HS 编码 → 问题清单（模板无报关栏，只出清单）。
勾选: 重量 / 三边 / 报关英文名 / HS编码（默认三边不勾 · Boss 2026-10-29）。下载 = 一个 Excel（数据・示例・问题清单・导入明细）。
判定规则见 shared/banma_dims.py。

数据源（PG・本页只读，「立即从斑马拉取」只置 run_now）:
  · banma.sku_dims / sku_customs / name_en_std  pull_sku_health（jdl_scheduler 每日 04:00 JST · 约 1 小时）
  · jdl.v_goods_dimensions                      JDL 仓库实测毛重/三边
  · nst.item_master_raw                         NST 包装重量 package_weight_g · 商品等级 item_rank
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import streamlit as st

from shared.banma_dims import CHUNK, build, customs_issues, in_scope, report_xlsx, select
from shared.db import get_connection, get_readonly_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("斑马信息健康度检查"), page_icon="🩺", layout="wide")
from shared.auth import require_password  # noqa: E402

require_password()
from shared.theme import inject_theme  # noqa: E402

inject_theme()
lang_selector()

st.title(t("🩺 斑马信息健康度检查"))
st.caption(t("斑马 Export 部门、NST 商品等级 A/B/C/NEW 的 SKU：检查三边、重量、报关信息（英文名、HS 编码） · "
             "三边/重量可下载 Excel 直接在斑马「批量修改SKU信息」上传"))

JOB = "banma_health_daily"
RANKS = ("Aランク", "Bランク", "Cランク", "NEW")
_SKU_SQL = ("SELECT sku_id, code, spu_id, spu_title, specification, category_top, sku_type, is_valid, "
            "weight, length, width, height, combine, pulled_at FROM banma.sku_dims")
_CUS_SQL = "SELECT sku_id, name_cn, name_en, hs_code FROM banma.sku_customs"
_STD_SQL = "SELECT hs, name_cn, name_en FROM banma.name_en_std"
_JDL_SQL = ("SELECT jan, wms_gross_weight_g, wms_length_mm, wms_width_mm, wms_height_mm "
            "FROM jdl.v_goods_dimensions")
_NST_SQL = ("SELECT jan, max(package_weight_g) AS w FROM nst.item_master_raw "
            "WHERE jan IS NOT NULL AND package_weight_g > 0 GROUP BY jan")
_RANK_SQL = "SELECT DISTINCT jan FROM nst.item_master_raw WHERE jan IS NOT NULL AND item_rank IN (%s, %s, %s, %s)"


@st.cache_data(ttl=300, show_spinner=False)
def _load():
    conn = get_readonly_connection()
    try:
        q = lambda sql, p=None: [dict(r) for r in (conn.execute(sql, p) if p else conn.execute(sql)).fetchall()]
        skus = q(_SKU_SQL)
        customs = {r["sku_id"]: r for r in q(_CUS_SQL)}
        std = [(r["hs"], r["name_cn"], r["name_en"]) for r in q(_STD_SQL)]
        jdl = {r["jan"]: (r["wms_gross_weight_g"], (r["wms_length_mm"], r["wms_width_mm"], r["wms_height_mm"]))
               for r in q(_JDL_SQL)}
        nst = {r["jan"]: r["w"] for r in q(_NST_SQL)}
        ranked = {r["jan"] for r in q(_RANK_SQL, RANKS)}
    finally:
        conn.close()
    for s in skus:
        if isinstance(s["combine"], str):
            s["combine"] = json.loads(s["combine"])
    return skus, customs, std, jdl, nst, ranked


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
        c1.info(t("⏳ 拉取中（约 1 小时），完成后刷新本页"))
    elif c1.button(t("▶️ 立即从斑马拉取"), type="primary"):
        try:
            wconn.execute("UPDATE jdl.pull_schedule SET run_now=TRUE, updated_at=now() WHERE job_key=%s", (JOB,))
            wconn.commit()
            st.cache_data.clear()
            st.success(t("已预约，1 分钟内开始，约 1 小时完成"))
            st.rerun()
        except Exception as e:
            wconn.rollback()
            st.error(str(e))

try:
    skus, customs, std, jdl, nst, ranked = _load()
except Exception as e:
    st.error(t("读取失败") + f": {e}")
    st.stop()
if not skus:
    st.warning(t("还没有斑马数据，请先点「立即从斑马拉取」"))
    st.stop()

pulled = max(s["pulled_at"] for s in skus)
c3.caption(t("斑马数据时点") + f": {str(pulled)[:16]}")
if pd.Timestamp.now(tz="UTC") - pd.Timestamp(pulled) > pd.Timedelta(hours=26):
    st.warning(t("斑马数据超过 1 天未更新，上传前请先点「立即从斑马拉取」"))

stamp = dt.datetime.now().strftime("%Y%m%d")
st.markdown("**" + t("检查项") + "**")
k1, k2, k3, k4 = st.columns(4)
checks = {name for col, name, default in ((k1, "重量", True), (k2, "三边", False),
                                          (k3, "英文名", True), (k4, "HS", True))
          if col.checkbox(t({"英文名": "报关英文名", "HS": "HS编码"}.get(name, name)), value=default, key=f"chk_{name}")}
if not checks:
    st.info(t("请至少勾选一项"))
    st.stop()

rows, dim_issues = build(skus, jdl, nst, ranked)
rows, issues = select(rows, dim_issues + customs_issues(skus, customs, std, ranked), checks)

m1, m2, m3 = st.columns(3)
m1.metric(t("检查对象 SKU"), f"{len(in_scope(skus, ranked)):,}")
m2.metric(t("可直接上传斑马"), f"{len(rows):,}")
m3.metric(t("问题 SKU"), f"{len({x['SKU编码'] for x in issues}):,}")

# 一个 Excel = 数据（斑马上传用）+ 示例 + 问题清单 + 导入明细；>1000 行时第 2 批起另出文件（斑马每次 ≤1000）
batches = [rows[k:k + CHUNK] for k in range(0, len(rows), CHUNK)] or [[]]
for n, part in enumerate(batches, 1):
    st.download_button(
        t("📥 下载 Excel（「数据」页可直接上传斑马 + 问题清单）") + (f" · {t('第')}{n}{t('批')}" if len(batches) > 1 else ""),
        report_xlsx(part, issues if n == 1 else []),
        file_name=f"斑马健康度检查_{stamp}" + (f"_{n}" if len(batches) > 1 else "") + ".xlsx",
        type="primary" if n == 1 else "secondary", key=f"dl_{n}")
st.caption(t("「数据」页 = 斑马「批量修改SKU信息」格式，空栏 = 不修改；报关信息斑马模板没有栏位，只在问题清单里 · "
             "上传后点「立即从斑马拉取」，已改好的会从表中消失"))

tab_up, tab_issue = st.tabs([t("✅ 可直接上传斑马"), t("⚠️ 问题清单")])
with tab_up:
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, height=520, hide_index=True)
    else:
        st.success(t("没有需要补的 SKU"))
with tab_issue:
    if issues:
        df = pd.DataFrame(issues)
        kinds = df["问题"].value_counts()
        pick = st.multiselect(t("问题类型"), list(kinds.index), format_func=lambda k: f"{k}（{kinds[k]}）")
        st.dataframe(df[df["问题"].isin(pick)] if pick else df, use_container_width=True, height=520, hide_index=True)
    else:
        st.success(t("没有问题 SKU"))
