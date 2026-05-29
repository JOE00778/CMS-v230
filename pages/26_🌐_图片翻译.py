"""模块 #26 图片文字翻译 · 中/日 → 东南亚.

把详情图 / 包装图 / 营销图里的中日文字翻译成东南亚目标市场语言
（人物 / 产品 / 排版 / 配色不变）。
封装自 ~/.claude/skills/image-translate/，核心在 modules/image_translate/。

适用市场：Shopee/Lazada 东南亚 6 站（SG/MY/ID/TH/VN/PH）。
- 源语言：auto / zh / ja
- 目标语言：en (SG/PH) · tl (PH) · id (ID) · ms (MY) · vi (VN) · th (TH)

⚠️ 安全性（一元管理「最高优」）
- 图片字节会被发送到 Google Gemini API。
- 上传前自动剥离 EXIF / GPS / 设备指纹（modules.image_translate.strip_metadata）
- 仅 admin 角色可用（require_admin）
- 用户须勾选「我确认未上传机密内容」才能跑
- 单批 ≤ 20 张、单图 ≤ 15 MB
- 每次调用写审计日志 data/audit/image_translate.jsonl

API key：Streamlit secrets 或环境变量 `GEMINI_API_KEY`（与其他 LLM page 共享）。
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import zipfile
from datetime import datetime
from pathlib import Path

import streamlit as st

from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("图片翻译"), page_icon="🌐", layout="wide")

from shared.auth import require_admin
require_admin()
from shared.theme import inject_theme
inject_theme()
lang_selector()

from modules.image_translate import (
    DEFAULT_MODEL,
    LANG_NAMES,
    MAX_IMAGE_BYTES,
    get_client,
    load_image_from_url,
    strip_metadata,
    translate_image_bytes,
)


# ============================================================
# 业务范围 + 安全策略常量
# ============================================================
# 源语言：仅支持中/日（业务范围内 SKU 原始素材语言）
SOURCE_LANG_OPTIONS: list[str] = ["auto", "zh", "ja"]

# 目标语言：东南亚 6 站当地语言（Shopee/Lazada 业务范围）
SEA_TARGET_LANGS: list[str] = ["en", "tl", "id", "ms", "vi", "th"]
SEA_MARKET_HINT: dict[str, str] = {
    "en": "SG / PH",
    "tl": "PH",
    "id": "ID",
    "ms": "MY",
    "vi": "VN",
    "th": "TH",
}

MAX_IMAGES_PER_RUN = 20  # 单批上限
AUDIT_LOG_PATH = Path("data/audit/image_translate.jsonl")


# ============================================================
# 工具函数
# ============================================================
def _read_api_key() -> str:
    """优先 streamlit secrets，其次环境变量（与 shared.auth 同源）。"""
    try:
        v = st.secrets.get("GEMINI_API_KEY", None)
        if v:
            return str(v)
    except (FileNotFoundError, KeyError):
        pass
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


@st.cache_resource(show_spinner=False)
def _cached_client(api_key: str):
    return get_client(api_key)


def _guess_mime(filename: str, fallback: str = "image/jpeg") -> str:
    return mimetypes.guess_type(filename)[0] or fallback


def _stem(name: str) -> str:
    return Path(name).stem or "image"


def _audit(event: dict) -> None:
    """追加一行审计日志（写失败不阻断主流程）。"""
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        event["ts"] = datetime.now().isoformat(timespec="seconds")
        event["user"] = st.session_state.get("__lark_user", {}).get("email") or "admin"
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ============================================================
# UI
# ============================================================
st.title(f"🌐 {t('图片翻译')}")
st.caption(
    t(
        "中/日素材 → 东南亚 6 站当地语言 · 保留人物/产品/排版/配色不变 · "
        "基于 Gemini 2.5 Flash Image"
    )
)

# === 安全提示横幅（高 visibility） ===
st.warning(
    "⚠️ **数据出境告知** · "
    "上传的图片会发送到 **Google Gemini** 处理（系统已自动剥离 EXIF / GPS / 设备元数据，"
    "但图片本体仍会出境）。\n\n"
    "**业务范围**：仅用于已上架东南亚 6 站的商品详情图 / 营销图本地化。\n\n"
    "**禁止上传**：未上市产品 mockup / 含成本价或客户信息的图 / 内部资料 / 含人脸的私密照。\n\n"
    "**建议**：长期使用前请将 Gemini API key 升级到付费层（绑卡），关闭 Google 训练用途。",
    icon="🛡️",
)

api_key = _read_api_key()
if not api_key:
    st.error(
        "未配置 `GEMINI_API_KEY`。请在 Streamlit Secrets 或环境变量中设置。\n\n"
        "获取 key：https://aistudio.google.com/apikey"
    )
    st.stop()


# 输入区
with st.container(border=True):
    src_mode = st.radio(
        t("图片来源"),
        options=[t("上传文件"), t("粘贴 URL")],
        horizontal=True,
        key="img_src_mode",
    )

    uploaded: list[tuple[str, bytes, str]] = []  # (name, bytes, mime) — 已剥离 EXIF

    if src_mode == t("上传文件"):
        files = st.file_uploader(
            t("选择图片（支持多选，单次 ≤ 20 张、单图 ≤ 15 MB）"),
            type=["jpg", "jpeg", "png", "webp"],
            accept_multiple_files=True,
            key="img_uploader",
        )
        for f in (files or [])[:MAX_IMAGES_PER_RUN]:
            try:
                clean, mime = strip_metadata(f.getvalue())
                uploaded.append((f.name, clean, mime))
            except ValueError as e:
                st.warning(f"❌ {f.name}: {e}")
        if files and len(files) > MAX_IMAGES_PER_RUN:
            st.info(
                f"📋 已截断到前 {MAX_IMAGES_PER_RUN} 张（避免超量出境），"
                f"其余 {len(files) - MAX_IMAGES_PER_RUN} 张被忽略"
            )
    else:
        urls_raw = st.text_area(
            t("图片 URL（每行一个，单次 ≤ 20 张）"),
            placeholder="https://cdn.shopify.com/.../detail.jpg",
            height=100,
            key="img_urls",
        )
        urls = [u.strip() for u in (urls_raw or "").splitlines() if u.strip()]
        for url in urls[:MAX_IMAGES_PER_RUN]:
            try:
                data, _ = load_image_from_url(url)
                clean, mime = strip_metadata(data)
                name = Path(url.split("?", 1)[0]).name or "image.jpg"
                uploaded.append((name, clean, mime))
            except ValueError as e:
                st.warning(f"❌ {url}: {e}")
            except Exception as e:
                st.warning(f"❌ {url} — {e}")
        if len(urls) > MAX_IMAGES_PER_RUN:
            st.info(f"📋 已截断到前 {MAX_IMAGES_PER_RUN} 张")

    if uploaded:
        st.caption(f"✅ {len(uploaded)} 张图片已加载并剥离 EXIF 元数据")

    col_a, col_b = st.columns([1, 2])
    with col_a:
        source_lang = st.selectbox(
            t("源语言（中/日素材）"),
            options=SOURCE_LANG_OPTIONS,
            format_func=lambda c: "auto · 自动识别中/日" if c == "auto" else f"{c} · {LANG_NAMES[c]}",
            index=0,
            key="src_lang",
        )
    with col_b:
        target_langs = st.multiselect(
            t("目标语言 · 东南亚 6 站（可多选）"),
            options=SEA_TARGET_LANGS,
            format_func=lambda c: f"{c} · {LANG_NAMES[c]} ({SEA_MARKET_HINT[c]})",
            default=["en", "tl"],
            key="tgt_langs",
        )

    with st.expander(t("高级选项"), expanded=False):
        note = st.text_area(
            t("自定义提示（可选）"),
            placeholder=t("例如：保留 UNNY logo 不翻译；锁定玫瑰粉配色 #F4B6C2"),
            height=80,
            key="extra_note",
        )
        col_m, col_r = st.columns(2)
        with col_m:
            model = st.text_input(t("模型"), value=DEFAULT_MODEL, key="model")
        with col_r:
            retries = st.number_input(
                t("失败重试"), min_value=1, max_value=6, value=2, step=1, key="retries"
            )

    # 显式合规勾选
    consent = st.checkbox(
        "✅ 我确认所选图片**不含**未上市产品 / 成本价 / 客户信息 / 私密内容，可上传到 Google Gemini",
        key="consent_to_upload",
    )

run = st.button(
    t("开始翻译"),
    type="primary",
    use_container_width=True,
    disabled=not (uploaded and target_langs and consent),
)
if uploaded and target_langs and not consent:
    st.caption(t("⛔ 需勾选合规确认后才能开始"))


# ============================================================
# 执行
# ============================================================
if run:
    client = _cached_client(api_key)
    total = len(uploaded) * len(target_langs)
    results: list[tuple[str, str, bytes | None, str]] = []

    _audit({
        "event": "translate_batch_start",
        "n_images": len(uploaded),
        "target_langs": target_langs,
        "source_lang": source_lang,
        "model": model or DEFAULT_MODEL,
    })

    progress = st.progress(0.0, text=t("准备中..."))
    done = 0
    for img_name, img_bytes, mime in uploaded:
        for lang in target_langs:
            progress.progress(
                done / total,
                text=f"{img_name} → {lang} ({done + 1}/{total})",
            )
            try:
                out_bytes = translate_image_bytes(
                    client,
                    img_bytes,
                    mime,
                    target_lang=lang,
                    source_lang=source_lang,
                    note=note or "",
                    model=model or DEFAULT_MODEL,
                    retries=int(retries),
                )
                results.append((img_name, lang, out_bytes, ""))
            except Exception as e:
                results.append((img_name, lang, None, str(e)))
            done += 1
    progress.progress(1.0, text=t("完成"))

    ok = sum(1 for _, _, b, _ in results if b is not None)
    fail = total - ok
    st.success(f"✅ {t('完成')} · ok={ok} · fail={fail}")

    _audit({
        "event": "translate_batch_end",
        "ok": ok,
        "fail": fail,
        "errors": [e[:200] for _, _, b, e in results if b is None][:10],
    })

    # 结果展示：按原图分组，原图 + 各语言版本并排
    for img_name, img_bytes, _mime in uploaded:
        st.markdown(f"### 📷 {img_name}")
        cols = st.columns(min(1 + len(target_langs), 4))
        cols[0].caption(f"{t('原图')} · {source_lang}")
        cols[0].image(img_bytes, use_container_width=True)

        for i, lang in enumerate(target_langs, start=1):
            col = cols[i % len(cols)]
            match = next(
                (r for r in results if r[0] == img_name and r[1] == lang), None
            )
            if not match:
                continue
            _, _, out_bytes, err = match
            if out_bytes is None:
                col.error(f"{lang} ❌ {err[:200]}")
                continue
            stem = _stem(img_name)
            out_name = f"{stem}.{lang}.png"
            col.caption(f"{lang} · {LANG_NAMES[lang]}")
            col.image(out_bytes, use_container_width=True)
            col.download_button(
                f"⬇️ {out_name}",
                data=out_bytes,
                file_name=out_name,
                mime="image/png",
                key=f"dl_{img_name}_{lang}",
                use_container_width=True,
            )

    success_items = [(n, l, b) for n, l, b, _ in results if b is not None]
    if success_items:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for n, l, b in success_items:
                zf.writestr(f"{_stem(n)}.{l}.png", b)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        st.download_button(
            f"📦 {t('整批下载 ZIP')}",
            data=buf.getvalue(),
            file_name=f"image-translate-{stamp}.zip",
            mime="application/zip",
            use_container_width=True,
        )


# ============================================================
# 帮助
# ============================================================
with st.expander("🛡️ " + t("数据安全说明"), expanded=False):
    st.markdown(
        f"""
**当前已生效的防护**
- 仅 admin 角色（`require_admin`）
- 上传前自动剥离 EXIF / GPS / 设备指纹 / ICC profile（重编码为干净 PNG）
- 单批 ≤ {MAX_IMAGES_PER_RUN} 张、单图 ≤ {MAX_IMAGE_BYTES // 1024 // 1024} MB
- 服务端不落盘（in-memory 处理）
- 显式合规勾选才能开跑
- 每次批处理写审计日志 `{AUDIT_LOG_PATH}`

**尚未覆盖（需 Boss 决策）**
- 图片本身仍会上传到 Google Gemini API — 这是 Gemini 服务的本质，无法绕过
- 当前 API key 类型未知；**强烈建议**升级到付费层关闭训练用途：
  1. 打开 https://aistudio.google.com/apikey
  2. 给项目绑信用卡 → 自动进入 paid tier
  3. paid tier 下 Google 不会用数据训练模型，且数据保留更短
- 如需更强企业级合同，可迁到 Vertex AI（工程量较大，先记账）
        """
    )

with st.expander("ℹ️ " + t("使用说明 / 已知局限"), expanded=False):
    st.markdown(
        """
**典型场景（东南亚 6 站）**
- Shopee/Lazada 详情图本地化：中/日 → en (SG/PH) · tl (PH) · id (ID) · ms (MY) · vi (VN) · th (TH)
- 站内营销 banner 换语言版（每站独立海报）
- 包装 mockup 多市场版本

**已知局限**
- 极小字号 CJK（< 12px）可能模糊 → 先 2× 放大原图再传
- 渐变 / 透出底图的文字偶有残影 → 用「自定义提示」加 *Re-render text on a clean background*
- 含 CJK 的品牌 logo 可能被一并翻译 → 用「自定义提示」加 *Do NOT translate the brand logo, keep `<BRAND>` as-is*
- 字体只能找近似的，做不到像素级一致

**计费**
- Gemini 2.5 Flash Image ≈ $0.039 / 张输出（2026-01 报价）
- 每多一个目标语言 = 多一次 API 调用
        """
    )
