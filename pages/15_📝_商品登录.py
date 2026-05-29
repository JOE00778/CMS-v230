"""模块 #15 商品登录 · 新品首次推送 NetSuite/JD/BM

定位：商品首次登录（NST/JD/BM 还都没有这个 SKU），不是已登录商品的修改。
修改场景请走 page 03（定義原価編集）/ page 07（商品等级判定）等。

两个 tab 并存：
  🆕 原生版 (MVP · 2026-05-29 v2)：上传 NSTマスタ xlsx → 预览编辑 → NetSuite CSV
  📜 旧 HTML 版：商品登録ツール iframe（JD/BM xlsx 输出仍走此处）

原生版流程：
  Step 1 上传 NSTマスタ.xlsx（sheet: NetSuiteマスタ登録，与旧 HTML 工具同格式）
  Step 2 自动解析：删 row1/3/4，row2=header，row5+ 数据；A 列空行剔除
  Step 3 预览编辑（st.data_editor），自动用 nst.item_image_cache 补画像URL（仅参考）
  Step 4 ⬇️ 生成 NetSuite【アイテム】マスタ登録-V260326EX CSV
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from data_warehouse.templates import nst_item_master as TPL
from data_warehouse.templates import jd_bm_item_master as JBM
from shared.auth import require_password
from shared.db import get_connection
from shared.i18n import t, lang_selector
from shared.theme import inject_theme

st.set_page_config(page_title=t("商品登录"), page_icon="📝", layout="wide")
require_password()
inject_theme()
lang_selector()

st.title(t("📝 商品登录"))

tab_native, tab_legacy = st.tabs([t("🆕 原生版 (MVP)"), t("📜 旧 HTML 版")])

# ───────────────────────── NSTマスタ xlsx 解析 ─────────────────────────

NST_SHEET_NAME = "NetSuiteマスタ登録"
JAN_COL_NAME = "JANコード"


def _parse_nst_xlsx(file) -> tuple[pd.DataFrame, list[str]]:
    """解析 NSTマスタ xlsx，删 row1/3/4，row2=header，A 列空行剔除。

    返回 (DataFrame, warnings)
    """
    warnings: list[str] = []
    raw = pd.read_excel(file, sheet_name=NST_SHEET_NAME, header=None, dtype=str)
    if len(raw) < 5:
        return pd.DataFrame(), [f"sheet 行数不足 5（{len(raw)} 行）· 期待 row2=header / row5+=data"]

    # 删除 row1(idx0)、row3(idx2)、row4(idx3)
    filtered = raw.drop([0, 2, 3]).reset_index(drop=True)
    header = filtered.iloc[0].fillna("").astype(str).str.strip().tolist()
    body = filtered.iloc[1:].copy()
    body.columns = header

    # A 列（第一列：通常是 型番）空行剔除
    first_col = body.iloc[:, 0]
    valid_mask = first_col.notna() & (first_col.astype(str).str.strip() != "")
    dropped = (~valid_mask).sum()
    if dropped:
        warnings.append(f"A 列空行剔除 {int(dropped)} 行")
    body = body[valid_mask].reset_index(drop=True)

    # 列名按模板白名单过滤（不在模板的列丢弃 + 警告）
    valid_cols = [c for c in body.columns if c in TPL.VALID_COLUMNS]
    unknown_cols = [c for c in body.columns if c and c not in TPL.VALID_COLUMNS]
    if unknown_cols:
        warnings.append(f"非模板列被忽略 ({len(unknown_cols)} 个): {', '.join(unknown_cols[:5])}{'...' if len(unknown_cols) > 5 else ''}")

    body = body[valid_cols].copy()
    return body, warnings


def _augment_image_url(conn, df: pd.DataFrame) -> pd.DataFrame:
    """从 nst.item_image_cache 按 JAN 取已有 image_url 作辅助参考列（不进 CSV）。"""
    if df.empty or JAN_COL_NAME not in df.columns:
        df["_画像URL（参考·来自 image cache）"] = ""
        return df
    jans = [str(j).strip() for j in df[JAN_COL_NAME] if pd.notna(j) and str(j).strip()]
    if not jans:
        df["_画像URL（参考·来自 image cache）"] = ""
        return df
    placeholder = ",".join(["%s"] * len(jans))
    rows = conn.execute(
        f"SELECT jan_cd, image_url FROM nst.item_image_cache "
        f"WHERE jan_cd IN ({placeholder}) AND status='ok'",
        jans,
    ).fetchall()
    url_map = {r["jan_cd"]: r["image_url"] for r in rows}
    df["_画像URL（参考·来自 image cache）"] = df[JAN_COL_NAME].astype(str).str.strip().map(url_map).fillna("")
    return df


# ───────────────────────── tab 🆕 原生版 ─────────────────────────

with tab_native:
    st.caption(t(
        "新品首次推送 · 上传 NSTマスタ xlsx → 预览编辑 → 生成 NetSuite【アイテム】マスタ登録-V260326EX CSV"
    ))

    with st.expander(t("📌 使用说明"), expanded=False):
        st.markdown(t(
            "- **场景**：新品首次推送到 NST/JD/BM（已登录商品的修改请走 page 03/07 等）\n"
            f"- **上传**：只上传 **NSTマスタ 一张 sheet**（`{NST_SHEET_NAME}`） · 自动删 row1/3/4, row2=header, A 列空行剔除\n"
            "- **非模板列**：被自动忽略并提示（防止脏列污染 CSV）\n"
            "- **画像 URL**：自动从 nst.item_image_cache 按 JAN 取，参考列展示 + 自动填进 BM 图片URL\n"
            "- **一键打包 ZIP**：NetSuite CSV + JD.xlsx + BM.xlsx 三件一起出\n"
            f"- **JD 默认值**：货主编码=`{JBM.DEFAULT_JD_CUSTOMER_CODE}` · 销售渠道=`{JBM.DEFAULT_JD_SALES_CHANNEL}` · 平台编码=`{JBM.DEFAULT_JD_PLATFORM_CODE}`（可在生成区域改）\n"
            "- **BM 规则**：SPU=JAN · ERP 类目留空 · 英文名称留空（用户后填）"
        ))

    uploaded = st.file_uploader(
        t(f"📤 上传 NSTマスタ xlsx（sheet 名 = {NST_SHEET_NAME}）"),
        type=["xlsx"],
        key="page15_upload",
    )

    if uploaded:
        try:
            df, warns = _parse_nst_xlsx(uploaded)
        except ValueError as e:
            st.error(t(f"❌ 解析失败：{e}（确认 sheet 名是 `{NST_SHEET_NAME}`）"))
            st.stop()
        except Exception as e:
            st.error(t(f"❌ 解析失败：{type(e).__name__}: {e}"))
            st.stop()

        if df.empty:
            st.warning(t("⚠️ 解析后无有效行（A 列全空？检查上传文件）"))
            st.stop()

        with get_connection() as conn:
            df = _augment_image_url(conn, df)

        st.session_state["page15_df"] = df
        st.session_state["page15_warns"] = warns

    df: pd.DataFrame | None = st.session_state.get("page15_df")
    warns: list[str] = st.session_state.get("page15_warns") or []

    if df is not None and not df.empty:
        c1, c2, c3 = st.columns(3)
        c1.metric(t("解析行数"), len(df))
        c2.metric(t("有效模板列"), sum(1 for c in df.columns if c in TPL.VALID_COLUMNS))
        n_with_img = (df["_画像URL（参考·来自 image cache）"] != "").sum() if "_画像URL（参考·来自 image cache）" in df.columns else 0
        c3.metric(t("画像已缓存"), int(n_with_img))

        if warns:
            with st.expander(t(f"⚠️ 解析警告 {len(warns)} 条"), expanded=False):
                for w in warns:
                    st.text(f"• {w}")

        st.subheader(t("📋 预览（可直接在表里补缺/修改）"))

        # 移到首列：型番、アイテム名、JANコード（如果存在），其余按原序
        priority = ["型番", "アイテム名", "JANコード"]
        cols_in_priority = [c for c in priority if c in df.columns]
        rest = [c for c in df.columns if c not in cols_in_priority]
        df_show = df[cols_in_priority + rest].copy()

        col_config = {
            "_画像URL（参考·来自 image cache）": st.column_config.ImageColumn(
                t("画像（参考）"), width="small"
            ),
            "JANコード": st.column_config.TextColumn("JANコード", width="small"),
        }

        edited = st.data_editor(
            df_show,
            use_container_width=True,
            height=480,
            num_rows="dynamic",
            disabled=["_画像URL（参考·来自 image cache）"],
            column_config=col_config,
            key="page15_editor",
        )

        st.divider()
        st.subheader(t("📦 一键生成 NST + JD + BM"))

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            jd_customer_code = st.text_input(
                t("JD *货主编码"),
                value=JBM.DEFAULT_JD_CUSTOMER_CODE,
                help=t("写入 JD xlsx 第 1 列，所有行同值"),
            )
        with col_b:
            sales_channel = st.text_input(
                t("JD *销售渠道编码"),
                value=JBM.DEFAULT_JD_SALES_CHANNEL,
            )
        with col_c:
            platform_code = st.text_input(
                t("JD 平台商品编码"),
                value=JBM.DEFAULT_JD_PLATFORM_CODE,
            )

        btn_gen = st.button(t("⬇️ 生成 ZIP（NST.csv + JD.xlsx + BM.xlsx）"), type="primary")
        if btn_gen:
            # 整理可见行（剔除整行空白），收集 nst_rows 同时构造 image_url_map
            nst_field_cols = [c for c in edited.columns if c in TPL.VALID_COLUMNS]
            nst_rows: list[dict] = []
            ref_col = "_画像URL（参考·来自 image cache）"
            image_url_map: dict[str, str] = {}
            for _, r in edited.iterrows():
                first_val = r.iloc[0] if len(r) else None
                if pd.isna(first_val) or str(first_val).strip() == "":
                    continue
                row: dict = {TPL.ID_LABEL: ""}
                for col in nst_field_cols:
                    v = r.get(col)
                    if pd.notna(v) and str(v).strip() != "":
                        row[col] = v
                nst_rows.append(row)
                jan = str(row.get(JAN_COL_NAME, "")).strip()
                if jan and ref_col in edited.columns:
                    img = r.get(ref_col)
                    if pd.notna(img) and str(img).strip():
                        image_url_map[jan] = str(img).strip()

            if not nst_rows:
                st.error(t("❌ 无有效行可生成"))
            else:
                nst_csv = TPL.build_nst_master_csv(nst_rows, nst_field_cols)
                jd_xlsx = JBM.build_jd_xlsx(
                    nst_rows,
                    image_url_map=image_url_map,
                    jd_customer_code=jd_customer_code.strip() or JBM.DEFAULT_JD_CUSTOMER_CODE,
                    sales_channel=sales_channel.strip() or JBM.DEFAULT_JD_SALES_CHANNEL,
                    platform_code=platform_code.strip() or JBM.DEFAULT_JD_PLATFORM_CODE,
                )
                bm_xlsx = JBM.build_bm_xlsx(nst_rows, image_url_map=image_url_map)

                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(TPL.dated_filename(), nst_csv)
                    zf.writestr(JBM.dated_filename_jd(), jd_xlsx)
                    zf.writestr(JBM.dated_filename_bm(), bm_xlsx)
                buf.seek(0)
                st.session_state["page15_zip_bytes"] = buf.getvalue()
                st.session_state["page15_zip_rows"] = len(nst_rows)

        zip_bytes = st.session_state.get("page15_zip_bytes")
        if zip_bytes:
            n = st.session_state.get("page15_zip_rows", 0)
            zip_name = f"商品登録_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            st.download_button(
                t(f"⬇️ 下载 ZIP（{n} 件商品 · NST + JD + BM 三件套）"),
                data=zip_bytes,
                file_name=zip_name,
                mime="application/zip",
                type="primary",
            )
            st.success(t(
                f"✅ ZIP 已生成 · 解压含 3 份文件：NST.csv 直传 NetSuite · "
                f"JD.xlsx 直传 JD（{n} 行）· BM.xlsx 直传 BM（{n} 行）"
            ))
    elif uploaded is None:
        st.info(t("📤 上传 NSTマスタ xlsx 开始（拖入或点击上方上传区）"))


# ───────────────────────── tab 📜 旧 HTML 版 ─────────────────────────

with tab_legacy:
    st.caption(t("现有商品登録ツール（HTML 版）· 输出 NetSuite/JD/BM CSV · 新品时使用原生版"))
    st.info(t(
        "📌 这是旧版商品登録ツール iframe 嵌入。仅 JD/BM CSV 输出时使用；"
        "NST CSV 推荐走「🆕 原生版 (MVP)」tab，可直接从数据库拉数据，不需要上传 NST マスタ Excel。"
    ))

    html_path = Path(__file__).resolve().parent.parent / "assets" / "商品登録ツール_0418.html"
    if html_path.exists():
        components.html(html_path.read_text(encoding="utf-8"), height=1500, scrolling=True)
    else:
        st.error(t(f"❌ 找不到 HTML 文件：{html_path}"))
