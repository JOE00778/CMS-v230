"""模块 #21 Shopee 上架（v2.12 N8N 自动化版 · 2026-05-19 重构）

三个 Tab：

    🤖 全自动管线   →  输 SPU CSV → 一键调 N8N → 自动出图 + 自动上架 + 飞书通知
    🎨 参考图库     →  按品类一次性上传参考详情图（喂给 Nano Banana 2 复刻风格）
    📜 历史运行     →  automation_runs 实时进度 + 历史

历史：
- 原 page 22 + 23 已合并进本页
- 2026-05-19 Boss 拍板移除 Tab 2「手工模式」（v1 老脚本套件）· 改为纯 N8N 自动化

依赖：
- shared/n8n_client.py · 触发 + 状态查询（→ deploy/n8n/cms_api）
- shared/lark_notify.py · 兜底飞书
- automation_runs 表
- data/files/reference-images/<category>/*.png  · 参考图存储
"""
from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from shared.auth import is_admin, require_admin
from shared.db import get_connection, DATA_DIR
from shared.i18n import get_lang, lang_selector, t
from shared.i18n_columns import localize_df
from shared.n8n_client import (
    get_run_status,
    list_recent_runs,
    trigger_workflow,
)


# --------------------------------------------------------------------------- #
# 参考图库 · 文件系统位置
# --------------------------------------------------------------------------- #
REFERENCE_DIR = DATA_DIR / "files" / "reference-images"
REFERENCE_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Page setup
# --------------------------------------------------------------------------- #
st.set_page_config(page_title="Shopee 上架", page_icon="🚀", layout="wide")
require_admin()
from shared.theme import inject_theme
inject_theme()
lang_selector()

with st.sidebar:
    st.divider()
    en_on = st.checkbox("🌐 English (this page)", value=False, key="t309_en")
    if en_on:
        st.session_state["lang"] = "en"

st.title(t("🚀 Shopee 上架"))
st.caption(t("全自动管线 / 手工模式 / 参考图库 / 历史运行 一站式"))

conn = get_connection()


# --------------------------------------------------------------------------- #
# 三语 i18n 兜底
# --------------------------------------------------------------------------- #
_PAGE_STRINGS_EN: Dict[str, str] = {
    "Shopee 上架": "Shopee Listing",
    "全自动管线 / 手工模式 / 参考图库 / 历史运行 一站式":
        "Auto pipeline / Manual / Reference library / History — all in one",
}


def tt(text: str) -> str:
    lang = get_lang()
    if lang == "en":
        return _PAGE_STRINGS_EN.get(text, t(text))
    return t(text)


# --------------------------------------------------------------------------- #
# 公共 helpers
# --------------------------------------------------------------------------- #
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
        warnings.append(
            f"⚠️ SKU 重复，已自动去重（保留首次出现）：{', '.join(dup_skus[:10])}"
            + ("..." if len(dup_skus) > 10 else "")
        )
        df = df[~dup_mask].reset_index(drop=True)
    return df, warnings


def _df_to_clean_csv_path(df: pd.DataFrame) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
    )
    df[["SPU", "SKU"]].to_csv(tmp.name, index=False)
    tmp.close()
    return Path(tmp.name)


# --------------------------------------------------------------------------- #
# 3 Tab
# --------------------------------------------------------------------------- #
tab_auto, tab_refs, tab_history = st.tabs(
    ["🤖 全自动管线", "🎨 参考图库", "📜 历史运行"]
)


# ============================================================
# Tab 1 · 🤖 全自动管线
# ============================================================
with tab_auto:
    st.subheader(t("一键完成：出图 + 上架 + 飞书通知"))
    st.caption(t(
        "上传 SPU CSV → 自动按品类调 Nano Banana 2 出详情图（用 Tab 3 的参考图复刻风格）"
        " → 自动跑 Shopee mass-upload pipeline → N8N 调 Shopee API 上架 → 飞书群通知"
    ))

    # --- Step 1: 选市场 + 上传 CSV ---
    col_left, col_right = st.columns([2, 1])
    with col_left:
        market = st.selectbox(
            "🌏 Shopee 站点",
            ["TW", "SG", "MY", "PH", "TH", "VN", "ID"],
            help="N8N 用对应市场的 Shopee Partner API 鉴权",
            key="auto_market",
        )
    with col_right:
        num_outputs = st.number_input(
            "🖼️ 每商品出图数",
            min_value=1, max_value=10, value=5,
            help="Nano Banana 2 一次出 N 张同商品不同角度",
            key="auto_num_imgs",
        )

    csv_file = st.file_uploader(
        "📄 上传 SPU+SKU CSV（A=SPU, B=SKU/JAN）",
        type=["csv"],
        key="auto_csv",
    )

    enable_image_gen = st.checkbox(
        "🎨 自动出图（用 Nano Banana 2 + Tab 3 参考图）",
        value=True,
        key="auto_enable_img_gen",
        help="关掉则跳过 AI 出图，仅做 listing 生成 + Shopee 上架（用 SKU 自带主图）",
    )

    # 环境检查
    has_gemini = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    if enable_image_gen and not has_gemini:
        st.warning("⚠️ 未配置 GEMINI_API_KEY — 出图功能将跳过（仍会跑 listing + 上架）")

    # --- Step 2: 触发 ---
    can_trigger = csv_file is not None
    if not can_trigger:
        st.info("先上传 SPU CSV 才能触发")

    if st.button(
        "🚀 触发全自动管线",
        type="primary",
        disabled=not can_trigger,
        use_container_width=True,
        key="auto_trigger",
    ):
        try:
            df = _read_uploaded_csv(csv_file)
            df, warns = _validate_and_dedup(df)
            for w in warns:
                st.warning(w)

            # 把 CSV 编码进 payload
            csv_bytes = df[["SPU", "SKU"]].to_csv(index=False).encode("utf-8")
            payload = {
                "market": market,
                "csv_b64": base64.b64encode(csv_bytes).decode("ascii"),
                "spu_count": int(df["SPU"].nunique()),
                "sku_count": int(len(df)),
                "enable_image_gen": enable_image_gen,
                "num_outputs_per_product": int(num_outputs),
                "triggered_at": datetime.utcnow().isoformat() + "Z",
            }

            user_email = st.session_state.get("user_email", "admin")
            run_id = trigger_workflow(
                module="shopee_mass_upload",
                webhook_path="shopee-mass-upload",
                payload=payload,
                conn=conn,
                triggered_by=user_email,
            )
            st.session_state["last_auto_run_id"] = run_id
            st.success(f"✅ 已触发，run_id = `{run_id}`")
        except Exception as e:
            st.error(f"❌ 触发失败：{e}")
            st.exception(e)

    # --- Step 3: 实时进度 ---
    last_id = st.session_state.get("last_auto_run_id")
    if last_id:
        st.divider()
        st.subheader(f"实时进度 · `{last_id[:8]}…`")
        placeholder = st.empty()
        for _ in range(20):
            row = get_run_status(conn, last_id)
            if not row:
                placeholder.warning("查不到该 run（DB 还没刷新）")
                break
            with placeholder.container():
                cols = st.columns(4)
                cols[0].metric("状态", row.get("status", "-"))
                cols[1].metric("模块", row.get("module", "-"))
                cols[2].metric("触发者", row.get("triggered_by", "-"))
                cols[3].metric("触发时间", str(row.get("triggered_at", "-"))[:19])
                if row.get("summary"):
                    st.json(row["summary"])
            if row.get("status") in ("completed", "failed"):
                break
            time.sleep(1.5)
        else:
            placeholder.info("仍在处理中 — 切到「📜 历史运行」Tab 或刷新页面继续观察")


# ============================================================
# Tab 3 · 🎨 参考图库
# ============================================================
with tab_refs:
    st.subheader(t("按品类一次性上传参考详情图（喂给 Nano Banana 2 复刻风格）"))
    st.caption(
        f"存储位置：`{REFERENCE_DIR}` · 每个品类放 3-14 张 · "
        "Tab 1 触发自动管线时按 SPU 类目自动加载对应文件夹"
    )

    # 列出现有品类
    existing_categories = sorted([
        p.name for p in REFERENCE_DIR.iterdir() if p.is_dir()
    ]) if REFERENCE_DIR.exists() else []

    col_a, col_b = st.columns([2, 3])

    with col_a:
        st.write("**已有品类**")
        if existing_categories:
            for cat in existing_categories:
                cat_path = REFERENCE_DIR / cat
                imgs = list(cat_path.glob("*.png")) + list(cat_path.glob("*.jpg")) + list(cat_path.glob("*.jpeg"))
                st.write(f"- `{cat}` · {len(imgs)} 张")
        else:
            st.info("还没有任何品类")

        st.divider()
        st.write("**新建 / 选择品类**")
        category_input = st.text_input(
            "品类名（中日英都可，建议跟 Shopee 类目一致）",
            placeholder="如：ボトル / ステッカー / スマホスタンド",
            key="ref_cat_new",
        )
        if existing_categories:
            category_select = st.selectbox(
                "或从已有品类选",
                options=["（不选）"] + existing_categories,
                key="ref_cat_sel",
            )
            if category_select != "（不选）":
                category_input = category_select

    with col_b:
        st.write("**上传参考图到该品类**")
        if not category_input:
            st.info("先填左边的品类名才能上传图")
        else:
            uploaded_imgs = st.file_uploader(
                f"拖拽图片到「{category_input}」品类（多选）",
                type=["png", "jpg", "jpeg"],
                accept_multiple_files=True,
                key=f"ref_upload_{category_input}",
            )
            if uploaded_imgs:
                cat_dir = REFERENCE_DIR / category_input
                cat_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                saved = 0
                for i, f in enumerate(uploaded_imgs, 1):
                    safe_name = f"{ts}_{i:02d}_{f.name}"
                    (cat_dir / safe_name).write_bytes(f.getvalue())
                    saved += 1
                st.success(f"✅ 上传成功 · {saved} 张图存到 `{cat_dir}`")
                st.rerun()

            # 列出该品类已有图
            cat_dir = REFERENCE_DIR / category_input
            if cat_dir.exists():
                imgs = sorted(
                    list(cat_dir.glob("*.png")) + list(cat_dir.glob("*.jpg")) + list(cat_dir.glob("*.jpeg"))
                )
                if imgs:
                    st.divider()
                    st.write(f"**`{category_input}` 已有 {len(imgs)} 张参考图**")
                    cols = st.columns(min(4, len(imgs)))
                    for i, img_path in enumerate(imgs[:12]):
                        with cols[i % 4]:
                            st.image(str(img_path), caption=img_path.name[:20], width=180)
                            if st.button("🗑 删", key=f"del_{img_path.name}"):
                                img_path.unlink()
                                st.rerun()


# ============================================================
# Tab 4 · 📜 历史运行
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
