"""模块 #21 Shopee 上架（v3 草稿审核线 · 2026-09-10 重构）

四个 Tab（Boss 2026-09-10 拍板的流程：英文母版 → 勾店铺 → 每店本地化 → 再定是否上传）：

    🤖 生成母版     →  上传 SPU CSV → 容器内跑 run_pipeline.py（AI 文案 · 38% 原価率 7 国价 · 模板图）→ 进待确认
    ✅ 待确认       →  列表层批量批准/拒绝 + 详情层改文案/换图 + 店铺本地化（语言 · 不重复标题 · 店铺模板主图）
    🎨 主图模板     →  每店一张 1500×1500 透明 PNG（方框 + 店铺 logo），CMS 上传即生效，不进镜像
    📜 历史运行     →  automation_runs（旧 N8N 线的记录，只读）

流水线代码不在本仓：workflow-automation/shopee-listing（compose 只读 mount 到 /opt/shopee-listing，
SHOPEE_LISTING_DIR 可改；本机开发回落 ../workflow-automation/shopee-listing）。本页只 import + 调用，不复制逻辑。
图片读写全部经 image-processor sidecar（IMAGE_PROCESSOR_URL），CMS 对 /data/whitebg 只读。

历史：
- 原 page 22 + 23 已合并进本页
- 2026-05-19 Boss 拍板移除 Tab 2「手工模式」· 改为纯 N8N 自动化
- 2026-08-21 N8N 线 B1 调的 /api/sku/master 删除 → 旧「全自动管线」链路断（Tab 1 于 2026-09-10 改为新流水线入口）
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import streamlit as st

from shared.auth import require_admin
from shared.db import get_readonly_connection, get_connection, DATA_DIR
from shared.i18n import get_lang, lang_selector, t
from shared.i18n_columns import localize_df, label
from shared.n8n_client import list_recent_runs

# --------------------------------------------------------------------------- #
# 流水线代码（workflow-automation/shopee-listing）· 只 import，不复制
# --------------------------------------------------------------------------- #
_SL_DIR = Path(os.environ.get("SHOPEE_LISTING_DIR") or "/opt/shopee-listing")
if not _SL_DIR.exists():
    _SL_DIR = Path(__file__).resolve().parents[2] / "workflow-automation" / "shopee-listing"
if str(_SL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_SL_DIR / "scripts"))
try:
    from draft_store import (list_drafts, list_batches, get_draft, set_status, update_text, counts_by_status,  # noqa: E402
                             bulk_set_status, set_sku_image, set_spu_image, list_shops, list_shop_drafts,
                             get_shop_draft, set_shop_status, update_shop_text, shop_counts)
    from image_pipeline import ImageProcessorClient, build_shop_images  # noqa: E402
    from localize import localize_spu, lang_for_shop  # noqa: E402
    PIPE_OK, PIPE_ERR = True, ""
except Exception as _e:  # noqa: BLE001
    PIPE_OK, PIPE_ERR = False, f"{type(_e).__name__}: {_e}"

JOB_DIR = DATA_DIR / "files" / "shopee-listing"          # CSV + 运行日志（rw mount）
IMAGE_PROCESSOR_URL = os.environ.get("IMAGE_PROCESSOR_URL", "").strip()

# --------------------------------------------------------------------------- #
# Page setup
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Shopee 上架", page_icon="🚀", layout="wide")
require_admin()
from shared.theme import inject_theme  # noqa: E402
inject_theme()
lang_selector()

with st.sidebar:
    st.divider()
    en_on = st.checkbox("🌐 English (this page)", value=False, key="t309_en")
    if en_on:
        st.session_state["lang"] = "en"

st.title(t("🚀 Shopee 上架"))
st.caption(t("生成母版 / 待确认 / 主图模板 / 历史运行 一站式"))

conn = get_readonly_connection()

# --------------------------------------------------------------------------- #
# 三语 i18n 兜底（中文 key；JA 在 shared/i18n.py；EN 在这里）
# --------------------------------------------------------------------------- #
_PAGE_STRINGS_EN: Dict[str, str] = {
    "🤖 生成母版": "🤖 Generate master", "✅ 待确认": "✅ Review queue", "🎨 主图模板": "🎨 Image templates", "📜 历史运行": "📜 Run history",
    "草稿待确认": "Pending review", "已批准": "Approved", "已拒绝": "Rejected", "已发布": "Published", "状态": "Status", "全部": "All",
    "Shopee 上架": "Shopee Listing",
    "生成母版 / 待确认 / 主图模板 / 历史运行 一站式": "Generate / Review / Templates / History — all in one",
    "生成英文母版：上传 SPU CSV → 流水线出文案 · 7 国价 · 图 → 进「待确认」": "Generate the English master: upload SPU CSV → pipeline writes copy · 7-country prices · images → Review queue",
    "流水线代码不可用：": "Pipeline code unavailable: ",
    "SPU+JAN 两列 CSV（A=SPU, B=JAN）": "Two-column CSV (A=SPU, B=JAN)",
    "主图": "Main images", "自动出图": "Auto images", "先不出图（之后在详情页手传）": "Skip images (upload later in detail view)",
    "批次号": "Batch ID", "🚀 生成母版": "🚀 Generate master", "先上传 CSV": "Upload a CSV first",
    "已启动，批次 {b}。生成需要几分钟，下面看日志；完成后到「待确认」按批次筛。": "Started batch {b}. Takes a few minutes — see log below; then filter by batch in Review queue.",
    "启动失败：": "Failed to start: ", "最近运行日志": "Recent run logs", "🔄 刷新": "🔄 Refresh",
    "LLM key：": "LLM keys: ", "未配置任何 LLM key，只能出 <MOCK> 占位文案": "No LLM key configured — only <MOCK> placeholders",
    "上架草稿确认：列表批量批 → 详情改文案换图 → 母版批准后勾店铺出本地化版 → 每店再批一次": "Review: batch-approve in the list → fix copy / images in detail → after master approval pick shops for localized versions → approve per shop",
    "批次": "Batch", "命中 {n} 个 SPU": "{n} SPUs", "这个状态下没有草稿": "No drafts in this status",
    "草稿表还没就位：先在元川 PG 跑 sql/001 + 002，再从「生成母版」出草稿。": "Draft tables missing: run sql/001 + 002 on the PG first, then generate from the first tab.",
    "勾选后批量操作（{n} 个）": "Batch actions on {n} selected",
    "✅ 批准勾选": "✅ Approve selected", "❌ 拒绝勾选": "❌ Reject selected", "💾 保存标题修改": "💾 Save title edits",
    "批量批准 ok={ok} fail={fail}": "Batch approve ok={ok} fail={fail}", "批量拒绝 ok={ok} fail={fail}": "Batch reject ok={ok} fail={fail}",
    "失败/跳过：": "Failed/skipped: ", "拒绝要写原因": "Rejection needs a reason", "备注（拒绝原因 / 修改说明）": "Note (reason / edit memo)",
    "标题改了 {n} 条（回到待确认）": "Updated {n} titles (back to pending)",
    "打开一个 SPU": "Open an SPU", "这条是 <MOCK> 占位文案（流水线没配 LLM key 时的产物），不能批准上架。": "This is a <MOCK> placeholder (no LLM key) — cannot be approved.",
    "标题（80–120 字符）": "Title (80–120 chars)", "{n} 字符": "{n} chars", "描述": "Description", "{n} 字符 · Shopee 上限 3000": "{n} chars · Shopee max 3000",
    "Shopee 类目 ID": "Shopee category ID", "品牌": "Brand", "模型": "Model", "卖点": "Key features", "输入里被剔掉的 JAN：": "JANs dropped from input:",
    "Shopee 属性": "Shopee attributes", "SPU 拼图": "SPU composite", "SPU 拼图路径（此机不可见）：": "SPU composite path (not visible here): ", "无 SPU 拼图": "No SPU composite",
    "**SKU 与 7 国价格**（原価率 38% · 汇率取生成当时 NST 值）": "**SKUs & 7-country prices** (38% cost ratio · FX at generation time)",
    "🖼 换主图（按 JAN）": "🖼 Replace main image (per JAN)", "图片处理服务未配置（IMAGE_PROCESSOR_URL）": "Image processor not configured (IMAGE_PROCESSOR_URL)",
    "原图 → 也走抠图套模板": "Raw photo → also cut out + apply template", "上传": "Upload", "已换图 {j}（{s}）": "Replaced image {j} ({s})", "换图失败：": "Image upload failed: ",
    "💾 保存修改（回到待确认，需再次确认）": "💾 Save (back to pending, re-confirm)", "已保存": "Saved", "没有行被更新": "No rows updated", "保存失败：": "Save failed: ",
    "✅ 批准母版（进入店铺本地化）": "✅ Approve master (unlock shop localization)", "已批准 {k}": "Approved {k}", "批准失败：": "Approve failed: ",
    "❌ 拒绝": "❌ Reject", "已拒绝 {k}": "Rejected {k}", "拒绝失败：": "Reject failed: ",
    "🌏 店铺本地化": "🌏 Shop localization", "母版批准后才能出店铺版": "Approve the master first",
    "勾选店铺": "Pick shops", "覆盖已批准的店铺版": "Overwrite approved shop versions", "同时出该店模板主图": "Also render shop-template images",
    "🌏 生成本地化版": "🌏 Generate localized versions", "本地化：{s}": "Localization: {s}", "用了默认红模板的店（先到「主图模板」传该店模板）：": "Shops on default red template (upload their template first): ",
    "本地化失败：": "Localization failed: ", "还没有店铺版": "No shop versions yet",
    "✅ 批准勾选（=要上传）": "✅ Approve selected (= to publish)", "店铺版 {s}": "Shop versions {s}",
    "打开一个店铺版": "Open a shop version", "🔁 按当前模板重出该店图": "🔁 Re-render this shop's images with current template", "已重出 {k} 的图（{n} 张）": "Re-rendered {k} images ({n})",
    "每店一张主图模板：1500×1500 PNG，中间透明，四周方框 + 店铺 logo。上传即替换，之后出图自动用新模板；已出的图不自动重出（详情页有「重出」按钮）。": "One template per shop: 1500×1500 PNG, transparent middle, frame + shop logo. Uploading replaces it; new renders use it immediately; existing images are not re-rendered automatically (use the button in detail view).",
    "选店铺": "Shop", "模板 PNG（1500×1500 · 透明通道）": "Template PNG (1500×1500 · alpha)", "⬆️ 上传 / 替换模板": "⬆️ Upload / replace template", "已上传 {k} 模板": "Template {k} uploaded", "上传失败：": "Upload failed: ",
    "🗑 删除该店模板（回落默认红模板）": "🗑 Delete this shop's template (fall back to default)", "当前模板": "Current template", "默认红模板（未上传）": "Default red template (none uploaded)",
    "⚠ 默认": "⚠ default",
}


def tt(text: str) -> str:
    lang = get_lang()
    if lang == "en":
        return _PAGE_STRINGS_EN.get(text, t(text))
    return t(text)


# --------------------------------------------------------------------------- #
# 公共 helpers
# --------------------------------------------------------------------------- #
_DRAFT_T = "shopee.listing_draft"
_DRAFT_SKU_T = "shopee.listing_draft_sku"
_DRAFT_SHOP_T = "shopee.listing_draft_shop"
_MARKETS_ORDER = ["PH", "MY", "SG", "TH", "VN", "TW", "BR"]
# 状态值在库里固定为英文（发布器/流水线按它判断），**显示层**按 UI 语言翻译（Boss 2026-09-09「匹配为中文和日文UI」）。
_STATUS_LABELS = {"draft": "草稿待确认", "approved": "已批准", "rejected": "已拒绝", "published": "已发布"}
_STATUS_ALL = "__all__"
_BATCH_ALL = "__all__"


def _st_label(v: str) -> str:
    return tt("全部") if v == _STATUS_ALL else tt(_STATUS_LABELS.get(v, v))


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _ip_client():
    return ImageProcessorClient(IMAGE_PROCESSOR_URL) if (PIPE_OK and IMAGE_PROCESSOR_URL) else None


@st.cache_data(show_spinner=False, max_entries=512)
def _thumb(path: Optional[str], mtime: float) -> Optional[str]:
    """容器内路径 → 120px 缩略图 data URL（data_editor 的 ImageColumn 只吃 URL）。"""
    if not path or not Path(path).exists():
        return None
    try:
        from PIL import Image
        im = Image.open(path)
        im.thumbnail((120, 120))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001
        return None


def _thumb_of(path: Optional[str]) -> Optional[str]:
    try:
        return _thumb(path, Path(path).stat().st_mtime) if path and Path(path).exists() else None
    except Exception:  # noqa: BLE001
        return None


def _b64_of_upload(f) -> str:
    return base64.b64encode(f.getvalue()).decode("ascii")


def _read_uploaded_csv(uploaded) -> pd.DataFrame:
    raw = uploaded.read()
    try:
        return pd.read_csv(io.BytesIO(raw), dtype=str, encoding="utf-8-sig", keep_default_na=False)
    except UnicodeDecodeError:
        return pd.read_csv(io.BytesIO(raw), dtype=str, encoding="cp932", keep_default_na=False)


def _validate_and_dedup(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    warnings: list[str] = []
    if df.shape[1] < 2:
        raise ValueError("❌ CSV 至少需要 A 列 (SPU) + B 列 (SKU)")
    df = df.copy()
    cols = list(df.columns)
    df = df.rename(columns={cols[0]: "SPU", cols[1]: "SKU"})
    df["SPU"] = df["SPU"].astype(str).str.strip()
    df["SKU"] = df["SKU"].astype(str).str.strip()
    df = df[(df["SPU"] != "") & (df["SKU"] != "")].reset_index(drop=True)
    dup_mask = df["SKU"].duplicated(keep="first")
    if dup_mask.any():
        dup_skus = df.loc[dup_mask, "SKU"].tolist()
        warnings.append("⚠️ SKU 重复，已自动去重（保留首次出现）：" + ", ".join(dup_skus[:10]) + ("..." if len(dup_skus) > 10 else ""))
        df = df[~dup_mask].reset_index(drop=True)
    return df, warnings


def _sku_summary(c, keys: list[str]) -> dict:
    """{spu_key: (sku_n, ph_price, all_have_image)} — 列表层要显示的 SKU 汇总，一次查完。"""
    if not keys:
        return {}
    ph = ",".join("?" * len(keys))
    rows = c.execute(f"SELECT spu_key, prices_json, image_path, image_error FROM {_DRAFT_SKU_T} WHERE spu_key IN ({ph})",
                     tuple(keys)).fetchall()
    out: dict = {}
    for r in rows:
        n, price, img_ok = out.get(r["spu_key"], (0, None, True))
        try:
            p = (json.loads(r["prices_json"] or "{}").get("PH") or {}).get("price")
        except Exception:  # noqa: BLE001
            p = None
        price = price if price is not None else p
        img_ok = img_ok and bool(r["image_path"]) and not r["image_error"]
        out[r["spu_key"]] = (n + 1, price, img_ok)
    return out


def _shop_summary(c, keys: list[str]) -> dict:
    """{spu_key: (店铺版数, 已批准数)}"""
    if not keys:
        return {}
    ph = ",".join("?" * len(keys))
    try:
        rows = c.execute(f"SELECT spu_key, COUNT(*) AS n, SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS a "
                         f"FROM {_DRAFT_SHOP_T} WHERE spu_key IN ({ph}) GROUP BY spu_key", tuple(keys)).fetchall()
    except Exception:  # noqa: BLE001 — 002 未跑
        return {}
    return {r["spu_key"]: (int(r["n"]), int(r["a"] or 0)) for r in rows}


# --------------------------------------------------------------------------- #
# 4 Tab
# --------------------------------------------------------------------------- #
tab_auto, tab_review, tab_refs, tab_history = st.tabs(
    [tt("🤖 生成母版"), tt("✅ 待确认"), tt("🎨 主图模板"), tt("📜 历史运行")]
)


# ============================================================
# Tab 1 · 🤖 生成母版（run_pipeline.py 在本容器后台跑）
# ============================================================
with tab_auto:
    st.subheader(tt("生成英文母版：上传 SPU CSV → 流水线出文案 · 7 国价 · 图 → 进「待确认」"))
    if not PIPE_OK:
        st.error(tt("流水线代码不可用：") + PIPE_ERR + f"（SHOPEE_LISTING_DIR={_SL_DIR}）")
    keys_on = [k for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY") if os.environ.get(k, "").strip()]
    st.caption(tt("LLM key：") + (" → ".join(k.replace("_API_KEY", "") for k in keys_on) if keys_on else tt("未配置任何 LLM key，只能出 <MOCK> 占位文案")))

    csv_file = st.file_uploader(tt("SPU+JAN 两列 CSV（A=SPU, B=JAN）"), type=["csv"], key="gen_csv")
    c1, c2 = st.columns([1, 1])
    img_mode = c1.radio(tt("主图"), [tt("自动出图"), tt("先不出图（之后在详情页手传）")], key="gen_img_mode", horizontal=True)
    default_batch = f"{Path(csv_file.name).stem}-{datetime.now():%Y%m%d-%H%M}" if csv_file else ""
    batch_id = c2.text_input(tt("批次号"), value=default_batch, key="gen_batch")

    if st.button(tt("🚀 生成母版"), type="primary", disabled=not (csv_file and PIPE_OK), use_container_width=True, key="gen_go"):
        try:
            df = _read_uploaded_csv(csv_file)
            df, warns = _validate_and_dedup(df)
            for w in warns:
                st.warning(w)
            JOB_DIR.mkdir(parents=True, exist_ok=True)
            safe_batch = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (batch_id or default_batch or "batch"))
            csv_path = JOB_DIR / f"{safe_batch}.csv"
            df[["SPU", "SKU"]].to_csv(csv_path, index=False)
            log_path = JOB_DIR / f"{safe_batch}.log"
            cmd = [sys.executable, str(_SL_DIR / "scripts" / "run_pipeline.py"), "--csv", str(csv_path),
                   "--db-url", os.environ.get("DATABASE_URL", ""), "--batch-id", safe_batch]
            if IMAGE_PROCESSOR_URL and img_mode == tt("自动出图"):
                cmd += ["--image-processor-url", IMAGE_PROCESSOR_URL]
            else:
                cmd += ["--no-images"]
            if not keys_on:
                cmd += ["--mock-llm"]
            with open(log_path, "ab") as lf:
                lf.write(f"# {_now()} {' '.join(c if 'postgresql://' not in c else 'postgresql://***' for c in cmd)}\n".encode())
                subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=os.environ.copy(), cwd=str(_SL_DIR))
            st.session_state["gen_last_batch"] = safe_batch
            st.success(tt("已启动，批次 {b}。生成需要几分钟，下面看日志；完成后到「待确认」按批次筛。").format(b=safe_batch))
        except Exception as e:  # noqa: BLE001
            st.error(tt("启动失败：") + str(e))
    elif not csv_file:
        st.info(tt("先上传 CSV"))

    st.divider()
    st.markdown(f"**{tt('最近运行日志')}**")
    if st.button(tt("🔄 刷新"), key="gen_refresh"):
        st.rerun()
    logs = sorted(JOB_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5] if JOB_DIR.exists() else []
    for lp in logs:
        tail = lp.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        done = any(l.startswith("ok=") for l in tail[-3:])
        with st.expander(f"{'✅' if done else '⏳'} {lp.stem} · {datetime.fromtimestamp(lp.stat().st_mtime):%m-%d %H:%M}", expanded=(lp.stem == st.session_state.get("gen_last_batch"))):
            st.code("\n".join(tail) or "(empty)")


# ============================================================
# Tab 2 · ✅ 待确认（列表批量 → 详情 → 店铺本地化）
# ============================================================
with tab_review:
    st.subheader(tt("上架草稿确认：列表批量批 → 详情改文案换图 → 母版批准后勾店铺出本地化版 → 每店再批一次"))
    counts = None
    if PIPE_OK:
        try:
            counts = counts_by_status(conn)
        except Exception as e:  # noqa: BLE001 — 表不存在 / 无权限
            st.info(tt("草稿表还没就位：先在元川 PG 跑 sql/001 + 002，再从「生成母版」出草稿。") + f"（{type(e).__name__}: {e}）"[:200])
    else:
        st.error(tt("流水线代码不可用：") + PIPE_ERR)

    if counts is not None:
        user_email = st.session_state.get("user_email", "admin")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric(_st_label("draft"), counts.get("draft", 0))
        m2.metric(_st_label("approved"), counts.get("approved", 0))
        m3.metric(_st_label("rejected"), counts.get("rejected", 0))
        m4.metric(_st_label("published"), counts.get("published", 0))

        f1, f2 = st.columns([1, 2])
        status_pick = f1.selectbox(tt("状态"), ["draft", "approved", "rejected", "published", _STATUS_ALL],
                                   format_func=_st_label, key="rv_status")
        batches = list_batches(conn)
        batch_pick = f2.selectbox(tt("批次"), [_BATCH_ALL] + [b for b, _ in batches],
                                  format_func=lambda b: tt("全部") if b == _BATCH_ALL else f"{b} ({dict(batches).get(b, 0)})",
                                  key="rv_batch")
        drafts = list_drafts(conn, None if status_pick == _STATUS_ALL else status_pick,
                             batch_id=None if batch_pick == _BATCH_ALL else batch_pick)
        if not drafts:
            st.info(tt("这个状态下没有草稿"))
        else:
            keys = [d["spu_key"] for d in drafts]
            sku_sum = _sku_summary(conn, keys)
            shop_sum = _shop_summary(conn, keys)
            tbl = pd.DataFrame([{
                "select": False,
                "spu_key": d["spu_key"],
                "thumb": _thumb_of(d.get("spu_image_path")),
                "title": d["title"] or "",
                "title_len": len(d["title"] or ""),
                "sku_count": sku_sum.get(d["spu_key"], (0, None, False))[0],
                "ph_price": sku_sum.get(d["spu_key"], (0, None, False))[1],
                "image": "✅" if sku_sum.get(d["spu_key"], (0, None, False))[2] else "⚠",
                "status": _st_label(d["status"]),
                "shops_done": f"{shop_sum.get(d['spu_key'], (0, 0))[1]}/{shop_sum.get(d['spu_key'], (0, 0))[0]}",
                "model": d.get("model") or "",
                "batch_id": d.get("batch_id") or "",
                "updated_at": (d.get("updated_at") or "")[:16],
            } for d in drafts])
            edited = st.data_editor(
                tbl, hide_index=True, use_container_width=True, num_rows="fixed",
                key=f"rv_editor_{status_pick}_{batch_pick}",
                column_config={
                    "select": st.column_config.CheckboxColumn(label("select"), default=False),
                    "spu_key": st.column_config.TextColumn(label("spu_key"), disabled=True),
                    "thumb": st.column_config.ImageColumn(label("thumb")),
                    "title": st.column_config.TextColumn(label("title"), width="large"),
                    "title_len": st.column_config.NumberColumn(label("title_len"), disabled=True),
                    "sku_count": st.column_config.NumberColumn(label("sku_count"), disabled=True),
                    "ph_price": st.column_config.TextColumn(label("ph_price"), disabled=True),
                    "image": st.column_config.TextColumn(label("image"), disabled=True),
                    "status": st.column_config.TextColumn(label("status"), disabled=True),
                    "shops_done": st.column_config.TextColumn(label("shops_done"), disabled=True),
                    "model": st.column_config.TextColumn(label("model"), disabled=True),
                    "batch_id": st.column_config.TextColumn(label("batch_id"), disabled=True),
                    "updated_at": st.column_config.TextColumn(label("updated_at"), disabled=True),
                },
            )
            st.caption(tt("命中 {n} 个 SPU").format(n=len(drafts)))
            picked = edited[edited["select"] == True]["spu_key"].tolist()  # noqa: E712
            changed_titles = {r.spu_key: r.title for r in edited.itertuples()
                              if (r.title or "") != (next(d["title"] for d in drafts if d["spu_key"] == r.spu_key) or "")}
            st.markdown(f"**{tt('勾选后批量操作（{n} 个）').format(n=len(picked))}**")
            bnote = st.text_input(tt("备注（拒绝原因 / 修改说明）"), key="rv_bulk_note")
            b1, b2, b3 = st.columns(3)
            if b1.button(tt("✅ 批准勾选"), type="primary", disabled=not picked, key="rv_bulk_ok", use_container_width=True):
                wc = get_connection()
                ok, failed = bulk_set_status(wc, picked, "approved", by=user_email, note=bnote or None)
                st.success(tt("批量批准 ok={ok} fail={fail}").format(ok=ok, fail=len(failed)))
                if failed:
                    st.warning(tt("失败/跳过：") + ", ".join(failed))
            if b2.button(tt("❌ 拒绝勾选"), disabled=not picked, key="rv_bulk_no", use_container_width=True):
                if not bnote.strip():
                    st.warning(tt("拒绝要写原因"))
                else:
                    wc = get_connection()
                    ok, failed = bulk_set_status(wc, picked, "rejected", note=bnote)
                    st.success(tt("批量拒绝 ok={ok} fail={fail}").format(ok=ok, fail=len(failed)))
                    if failed:
                        st.warning(tt("失败/跳过：") + ", ".join(failed))
            if b3.button(tt("💾 保存标题修改"), disabled=not changed_titles, key="rv_bulk_title", use_container_width=True):
                wc = get_connection()
                n = 0
                for k, new_title in changed_titles.items():
                    try:
                        if update_text(wc, k, title=new_title) and set_status(wc, k, "draft"):
                            n += 1
                    except Exception as e:  # noqa: BLE001
                        wc.rollback()
                        st.error(f"{k}: {e}")
                st.success(tt("标题改了 {n} 条（回到待确认）").format(n=n))

            # ---------------- 详情层 ----------------
            st.divider()
            sel = st.selectbox(tt("打开一个 SPU"), keys, key="rv_sel")
            d = get_draft(conn, sel) if sel else None
            if d:
                is_mock = str(d.get("title", "")).startswith("<MOCK>")
                if is_mock:
                    st.warning(tt("这条是 <MOCK> 占位文案（流水线没配 LLM key 时的产物），不能批准上架。"))
                left, right = st.columns([3, 2])
                with left:
                    new_title = st.text_input(tt("标题（80–120 字符）"), value=d.get("title") or "", key=f"rv_title_{sel}")
                    st.caption(tt("{n} 字符").format(n=len(new_title)))
                    new_desc = st.text_area(tt("描述"), value=d.get("description") or "", height=320, key=f"rv_desc_{sel}")
                    st.caption(tt("{n} 字符 · Shopee 上限 3000").format(n=len(new_desc)))
                    new_cat = st.text_input(tt("Shopee 类目 ID"), value=d.get("category_id") or "", key=f"rv_cat_{sel}")
                with right:
                    st.markdown(f"**{tt('品牌')}** {d.get('brand') or '-'} · **Hook** {d.get('hook') or '-'} · **{tt('模型')}** {d.get('model') or '-'}")
                    if d.get("key_features"):
                        st.markdown(f"**{tt('卖点')}**\n" + "\n".join(f"- {x}" for x in d["key_features"]))
                    if d.get("dropped"):
                        st.warning(tt("输入里被剔掉的 JAN：") + "\n" + "\n".join(f"- {x}" for x in d["dropped"]))
                    if d.get("attributes"):
                        with st.expander(tt("Shopee 属性"), expanded=False):
                            st.json(d["attributes"])
                    spu_img = d.get("spu_image_path")
                    if spu_img and Path(spu_img).exists():
                        st.image(spu_img, caption=tt("SPU 拼图"), use_container_width=True)
                    elif spu_img:
                        st.caption(tt("SPU 拼图路径（此机不可见）：") + f"`{spu_img}`")
                    else:
                        st.caption(tt("无 SPU 拼图"))

                st.markdown(tt("**SKU 与 7 国价格**（原価率 38% · 汇率取生成当时 NST 值）"))
                rows = []
                for s_ in d["skus"]:
                    row = {"jan": s_["jan"], "name_jp": (s_.get("name_jp") or "")[:40], "maker": s_.get("maker"),
                           "cost_jpy": s_.get("cost_jpy"), "weight_g": s_.get("weight_g")}
                    for mk in _MARKETS_ORDER:
                        p = (s_.get("prices") or {}).get(mk) or {}
                        row[f"{mk} {p.get('currency', '')}".strip()] = p.get("price") or (p.get("error") and "⚠")
                    row["image"] = "✅" if s_.get("image_path") and not s_.get("image_error") else (s_.get("image_error") or "-")[:30]
                    row["image_source"] = s_.get("image_source") or ""
                    rows.append(row)
                st.dataframe(localize_df(pd.DataFrame(rows)), use_container_width=True, hide_index=True)

                # 换主图：成品 / 原图☑套模板（Boss 2026-09-10）
                with st.expander(tt("🖼 换主图（按 JAN）"), expanded=False):
                    ipc = _ip_client()
                    if ipc is None:
                        st.info(tt("图片处理服务未配置（IMAGE_PROCESSOR_URL）"))
                    else:
                        jans = [s_["jan"] for s_ in d["skus"]]
                        up_jan = st.selectbox("JAN", jans, key=f"rv_upjan_{sel}")
                        up_file = st.file_uploader("", type=["png", "jpg", "jpeg", "webp"], key=f"rv_upfile_{sel}_{up_jan}")
                        templated = st.checkbox(tt("原图 → 也走抠图套模板"), value=False, key=f"rv_uptpl_{sel}_{up_jan}")
                        cur = next((s_ for s_ in d["skus"] if s_["jan"] == up_jan), None)
                        if cur and cur.get("image_path") and Path(cur["image_path"]).exists():
                            st.image(cur["image_path"], width=180, caption=f"{up_jan} · {cur.get('image_source') or ''}")
                        if st.button(tt("上传"), disabled=up_file is None, key=f"rv_upgo_{sel}_{up_jan}"):
                            try:
                                r = ipc.manual_image(up_jan, _b64_of_upload(up_file), templated=templated)
                                wc = get_connection()
                                set_sku_image(wc, sel, up_jan, r["saved_path"], r.get("source") or ("manual_templated" if templated else "manual"))
                                good = [s_["jan"] for s_ in d["skus"] if (s_["jan"] == up_jan) or (s_.get("image_path") and not s_.get("image_error"))]
                                comp = ipc.compose_spu(sel, good, overwrite=True)
                                set_spu_image(wc, sel, comp.get("saved_path"))
                                _thumb.clear()
                                st.success(tt("已换图 {j}（{s}）").format(j=up_jan, s=r.get("source")))
                            except Exception as e:  # noqa: BLE001
                                st.error(tt("换图失败：") + str(e))

                note = st.text_input(tt("备注（拒绝原因 / 修改说明）"), key=f"rv_note_{sel}")
                c1, c2, c3 = st.columns(3)
                if c1.button(tt("💾 保存修改（回到待确认，需再次确认）"), key=f"rv_save_{sel}", use_container_width=True):
                    try:
                        wc = get_connection()
                        n = update_text(wc, sel, title=new_title, description=new_desc, category_id=new_cat or None, review_note=note or None)
                        set_status(wc, sel, "draft")
                        st.success(tt("已保存")) if n else st.warning(tt("没有行被更新"))
                    except Exception as e:  # noqa: BLE001
                        st.error(tt("保存失败：") + str(e))
                if c2.button(tt("✅ 批准母版（进入店铺本地化）"), type="primary", key=f"rv_ok_{sel}",
                             disabled=is_mock or new_title.startswith("<MOCK>") or d.get("status") == "published", use_container_width=True):
                    try:
                        wc = get_connection()
                        n = set_status(wc, sel, "approved", by=user_email, note=note or None)
                        st.success(tt("已批准 {k}").format(k=sel)) if n else st.warning(tt("没有行被更新"))
                    except Exception as e:  # noqa: BLE001
                        st.error(tt("批准失败：") + str(e))
                if c3.button(tt("❌ 拒绝"), key=f"rv_no_{sel}", disabled=d.get("status") == "published", use_container_width=True):
                    if not note.strip():
                        st.warning(tt("拒绝要写原因"))
                    else:
                        try:
                            wc = get_connection()
                            n = set_status(wc, sel, "rejected", note=note)
                            st.success(tt("已拒绝 {k}").format(k=sel)) if n else st.warning(tt("没有行被更新"))
                        except Exception as e:  # noqa: BLE001
                            st.error(tt("拒绝失败：") + str(e))

                # ---------------- 店铺本地化 ----------------
                st.divider()
                st.subheader(tt("🌏 店铺本地化"))
                if d.get("status") not in ("approved", "published"):
                    st.info(tt("母版批准后才能出店铺版"))
                else:
                    try:
                        shops = list_shops(conn)
                    except Exception as e:  # noqa: BLE001
                        shops = []
                        st.error(str(e))
                    shop_by_key = {s["shop_key"]: s for s in shops}
                    existing = {r["shop_key"]: r for r in list_shop_drafts(conn, spu_key=sel)}
                    fmt = lambda k: f"{k} · {shop_by_key[k]['shop_name']} · {lang_for_shop(shop_by_key[k])}" + ("  ✔" if k in existing else "")  # noqa: E731
                    pick = st.multiselect(tt("勾选店铺"), [s["shop_key"] for s in shops], default=[s["shop_key"] for s in shops],
                                          format_func=fmt, key=f"lc_pick_{sel}")
                    o1, o2 = st.columns(2)
                    force = o1.checkbox(tt("覆盖已批准的店铺版"), value=False, key=f"lc_force_{sel}")
                    with_img = o2.checkbox(tt("同时出该店模板主图"), value=bool(_ip_client()), key=f"lc_img_{sel}", disabled=_ip_client() is None)
                    if st.button(tt("🌏 生成本地化版"), type="primary", disabled=not pick, key=f"lc_go_{sel}"):
                        try:
                            wc = get_connection()
                            with st.spinner("LLM…"):
                                rep = localize_spu(wc, sel, pick, image_client=_ip_client() if with_img else None, force=force)
                            st.success(tt("本地化：{s}").format(s=rep.summary()))
                            if rep.fallback_template:
                                st.warning(tt("用了默认红模板的店（先到「主图模板」传该店模板）：") + ", ".join(rep.fallback_template))
                            for k, m in rep.fail:
                                st.error(f"{k}: {m}")
                            for k, m in rep.skipped:
                                st.caption(f"SKIP {k}: {m}")
                            _thumb.clear()
                            existing = {r["shop_key"]: r for r in list_shop_drafts(conn, spu_key=sel)}
                        except Exception as e:  # noqa: BLE001
                            st.error(tt("本地化失败：") + str(e))

                    if not existing:
                        st.info(tt("还没有店铺版"))
                    else:
                        sc = shop_counts(conn, sel)
                        st.caption(tt("店铺版 {s}").format(s=" · ".join(f"{_st_label(k)} {v}" for k, v in sc.items())))
                        stbl = pd.DataFrame([{
                            "select": False, "shop_key": r["shop_key"],
                            "shop_name": (shop_by_key.get(r["shop_key"]) or {}).get("shop_name", ""),
                            "lang": r["lang"], "thumb": _thumb_of(r.get("spu_image_path")),
                            "title": r["title"] or "", "title_len": len(r["title"] or ""),
                            "template": (r.get("template_key") if r.get("template_key") and r.get("template_key") != "default" else tt("⚠ 默认")),
                            "status": _st_label(r["status"]), "model": r.get("model") or "",
                        } for r in existing.values()])
                        sed = st.data_editor(
                            stbl, hide_index=True, use_container_width=True, num_rows="fixed", key=f"lc_editor_{sel}",
                            column_config={
                                "select": st.column_config.CheckboxColumn(label("select"), default=False),
                                "shop_key": st.column_config.TextColumn(label("shop_key"), disabled=True),
                                "shop_name": st.column_config.TextColumn(label("shop_name"), disabled=True),
                                "lang": st.column_config.TextColumn(label("lang"), disabled=True),
                                "thumb": st.column_config.ImageColumn(label("thumb")),
                                "title": st.column_config.TextColumn(label("title"), width="large"),
                                "title_len": st.column_config.NumberColumn(label("title_len"), disabled=True),
                                "template": st.column_config.TextColumn(label("template"), disabled=True),
                                "status": st.column_config.TextColumn(label("status"), disabled=True),
                                "model": st.column_config.TextColumn(label("model"), disabled=True),
                            })
                        spicked = sed[sed["select"] == True]["shop_key"].tolist()  # noqa: E712
                        schanged = {r.shop_key: r.title for r in sed.itertuples() if (r.title or "") != (existing[r.shop_key]["title"] or "")}
                        snote = st.text_input(tt("备注（拒绝原因 / 修改说明）"), key=f"lc_note_{sel}")
                        s1, s2, s3 = st.columns(3)
                        if s1.button(tt("✅ 批准勾选（=要上传）"), type="primary", disabled=not spicked, key=f"lc_ok_{sel}", use_container_width=True):
                            wc = get_connection()
                            ok = sum(1 for k in spicked if set_shop_status(wc, sel, k, "approved", by=user_email, note=snote or None))
                            st.success(tt("批量批准 ok={ok} fail={fail}").format(ok=ok, fail=len(spicked) - ok))
                        if s2.button(tt("❌ 拒绝勾选"), disabled=not spicked, key=f"lc_no_{sel}", use_container_width=True):
                            if not snote.strip():
                                st.warning(tt("拒绝要写原因"))
                            else:
                                wc = get_connection()
                                ok = sum(1 for k in spicked if set_shop_status(wc, sel, k, "rejected", note=snote))
                                st.success(tt("批量拒绝 ok={ok} fail={fail}").format(ok=ok, fail=len(spicked) - ok))
                        if s3.button(tt("💾 保存标题修改"), disabled=not schanged, key=f"lc_title_{sel}", use_container_width=True):
                            wc = get_connection()
                            n = sum(1 for k, v in schanged.items() if update_shop_text(wc, sel, k, title=v))
                            st.success(tt("标题改了 {n} 条（回到待确认）").format(n=n))

                        ssel = st.selectbox(tt("打开一个店铺版"), list(existing), format_func=fmt, key=f"lc_sel_{sel}")
                        sd = get_shop_draft(conn, sel, ssel) if ssel else None
                        if sd:
                            l2, r2 = st.columns([3, 2])
                            with l2:
                                s_desc = st.text_area(tt("描述"), value=sd.get("description") or "", height=280, key=f"lc_desc_{sel}_{ssel}")
                                st.caption(tt("{n} 字符 · Shopee 上限 3000").format(n=len(s_desc)))
                                if st.button(tt("💾 保存修改（回到待确认，需再次确认）"), key=f"lc_save_{sel}_{ssel}"):
                                    wc = get_connection()
                                    st.success(tt("已保存")) if update_shop_text(wc, sel, ssel, description=s_desc) else st.warning(tt("没有行被更新"))
                            with r2:
                                simg = sd.get("spu_image_path")
                                if simg and Path(simg).exists():
                                    st.image(simg, caption=f"{ssel} · {sd.get('template_key') or ''}", use_container_width=True)
                                else:
                                    st.caption(tt("无 SPU 拼图"))
                                if _ip_client() and st.button(tt("🔁 按当前模板重出该店图"), key=f"lc_reimg_{sel}_{ssel}"):
                                    try:
                                        wc = get_connection()
                                        res = build_shop_images(sel, [s_["jan"] for s_ in d["skus"]], [ssel], client=_ip_client())[ssel]
                                        n = 0
                                        for j, si in res.skus.items():
                                            if si.branded_path:
                                                set_sku_image(wc, sel, j, si.branded_path, si.source or "auto", shop_key=ssel, error=si.error)
                                                n += 1
                                        set_spu_image(wc, sel, res.spu_path, shop_key=ssel)
                                        _thumb.clear()
                                        st.success(tt("已重出 {k} 的图（{n} 张）").format(k=ssel, n=n) + (f" · template={res.template_key}" if res.template_key else ""))
                                    except Exception as e:  # noqa: BLE001
                                        st.error(str(e))
                                sk = sd.get("skus") or []
                                if sk:
                                    st.dataframe(localize_df(pd.DataFrame([{"jan": x["jan"], "image": "✅" if x.get("image_path") and not x.get("image_error") else (x.get("image_error") or "-")[:30],
                                                                            "image_source": x.get("image_source") or ""} for x in sk])),
                                                 use_container_width=True, hide_index=True)


# ============================================================
# Tab 3 · 🎨 主图模板（每店一张 · CMS 上传 · sidecar 读）
# ============================================================
with tab_refs:
    st.subheader(tt("🎨 主图模板"))
    st.caption(tt("每店一张主图模板：1500×1500 PNG，中间透明，四周方框 + 店铺 logo。上传即替换，之后出图自动用新模板；已出的图不自动重出（详情页有「重出」按钮）。"))
    ipc = _ip_client()
    if not PIPE_OK:
        st.error(tt("流水线代码不可用：") + PIPE_ERR)
    elif ipc is None:
        st.info(tt("图片处理服务未配置（IMAGE_PROCESSOR_URL）"))
    else:
        try:
            shops = list_shops(conn)
            tpls = {x["key"]: x for x in ipc.list_templates()}
        except Exception as e:  # noqa: BLE001
            shops, tpls = [], {}
            st.error(str(e))
        if shops:
            tdf = pd.DataFrame([{
                "shop_key": s["shop_key"], "shop_name": s["shop_name"], "country_code": s["country_code"],
                "template": s["shop_key"] if s["shop_key"] in tpls else tt("⚠ 默认"),
                "template_updated": datetime.fromtimestamp(tpls[s["shop_key"]]["updated_at"]).strftime("%Y-%m-%d %H:%M") if s["shop_key"] in tpls else "",
                "thumb": _thumb_of(tpls[s["shop_key"]]["saved_path"]) if s["shop_key"] in tpls else None,
            } for s in shops])
            st.dataframe(tdf, hide_index=True, use_container_width=True,
                         column_config={c: st.column_config.ImageColumn(label(c)) if c == "thumb" else st.column_config.TextColumn(label(c)) for c in tdf.columns})
            st.caption(f"{sum(1 for s in shops if s['shop_key'] in tpls)}/{len(shops)}")

            u1, u2 = st.columns([1, 2])
            tshop = u1.selectbox(tt("选店铺"), [s["shop_key"] for s in shops],
                                 format_func=lambda k: f"{k} · {next(s['shop_name'] for s in shops if s['shop_key'] == k)}", key="tpl_shop")
            tfile = u2.file_uploader(tt("模板 PNG（1500×1500 · 透明通道）"), type=["png"], key=f"tpl_file_{tshop}")
            p1, p2 = st.columns(2)
            with p1:
                if tshop in tpls and Path(tpls[tshop]["saved_path"]).exists():
                    st.image(tpls[tshop]["saved_path"], caption=tt("当前模板") + f" · {tshop}", width=260)
                else:
                    st.caption(tt("默认红模板（未上传）"))
            with p2:
                if st.button(tt("⬆️ 上传 / 替换模板"), type="primary", disabled=tfile is None, key=f"tpl_go_{tshop}"):
                    try:
                        ipc.put_template(tshop, _b64_of_upload(tfile))
                        _thumb.clear()
                        st.success(tt("已上传 {k} 模板").format(k=tshop))
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(tt("上传失败：") + str(e))
                if tshop in tpls and st.button(tt("🗑 删除该店模板（回落默认红模板）"), key=f"tpl_del_{tshop}"):
                    try:
                        ipc.delete_template(tshop)
                        _thumb.clear()
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(str(e))


# ============================================================
# Tab 4 · 📜 历史运行（旧 N8N 线 automation_runs · 只读）
# ============================================================
with tab_history:
    st.subheader(t("最近 50 次自动化运行（含 Shopee 上架 + 出图 + 改廃监控等）"))

    module_filter = st.selectbox(
        "按模块过滤",
        ["全部", "shopee_mass_upload", "image_gen", "discontinue_confirm", "nst_order"],
        key="history_module",
    )

    runs = list_recent_runs(
        conn,
        module=None if module_filter == "全部" else module_filter,
        limit=50,
    )
    if not runs:
        st.info("还没有任何运行记录")
    else:
        rows = []
        for r in runs:
            rows.append({
                "run_id": r["run_id"][:8] + "...",
                "module": r["module"],
                "status": r["status"],
                "triggered_by": r["triggered_by"],
                "triggered_at": (r.get("triggered_at") or "")[:19],
                "completed_at": (r.get("completed_at") or "")[:19],
            })
        st.dataframe(localize_df(pd.DataFrame(rows)), use_container_width=True, hide_index=True)

        ids = [r["run_id"] for r in runs]
        sel = st.selectbox(
            "查看 payload + summary",
            ids,
            format_func=lambda x: f"{x[:8]}... · "
            + next(r['module'] for r in runs if r['run_id'] == x)
            + " · "
            + next(r['status'] for r in runs if r['run_id'] == x),
            key="history_sel",
        )
        if sel:
            row = next(r for r in runs if r["run_id"] == sel)
            with st.expander("payload", expanded=False):
                p = row.get("payload")
                try:
                    st.json(json.loads(p) if isinstance(p, str) else p)
                except Exception:
                    st.code(p or "(empty)")
            with st.expander("summary", expanded=True):
                s = row.get("summary")
                try:
                    st.json(json.loads(s) if isinstance(s, str) else s)
                except Exception:
                    st.code(s or "(empty)")
