"""模块 #15 商品登录

两个 tab 并存：
  🆕 原生版 (MVP · 2026-05-29)：贴 JAN → PG (nst.item_master_raw) 直拉 → NetSuite CSV
  📜 旧 HTML 版：商品登録ツール iframe（保留过渡期使用）

原生版只生成 NetSuite【アイテム】マスタ登録-V260326EX 格式 CSV
（JD/BM CSV 暂留旧 HTML 版做）。
未命中 JAN 在顶部名单提示，仅命中 JAN 进 CSV。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from data_warehouse.templates import nst_item_master as TPL
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

# ───────────────────────── PG → NST 模板列映射 ─────────────────────────

# PG 字段 → NST 模板列名（出现在最终 CSV header 中的列）
PG_TO_NST_COL: dict[str, str] = {
    "item_code":        "型番",
    "display_name":     "アイテム名",
    "jan":              "JANコード",
    "maker":            "メーカー名",
    "item_rank":        "商品ランク",
    "handling_cd":      "取扱区分",
    "department":       "部門",
    "cost":             "商品原価",
    "carton_qty":       "カートン入数",
    "order_lot":        "発注ロット",
    "tax_schedule":     "納税スケジュール",
    "item_weight_g":    "商品重量(g)",
    "package_weight_g": "パッケージ重量(g)",
    "carton_weight_g":  "カートン重量(g)",
}

# 选择查询的 PG 列（含 internal_id 作为 ID）
PG_SELECT_COLS = ["internal_id"] + list(PG_TO_NST_COL.keys())


def _parse_jans(raw: str) -> list[str]:
    seen, out = set(), []
    import re
    for line in re.split(r"[\s,;]+", raw or ""):
        j = line.strip()
        if not j or not j.isdigit() or not (8 <= len(j) <= 14):
            continue
        if j not in seen:
            seen.add(j)
            out.append(j)
    return out


def _fetch_items(conn, jans: list[str]) -> pd.DataFrame:
    if not jans:
        return pd.DataFrame()
    placeholder = ",".join(["%s"] * len(jans))
    sql = f"""
        SELECT {", ".join(PG_SELECT_COLS)},
               img.image_url AS image_url,
               img.status    AS image_status
        FROM nst.item_master_raw m
        LEFT JOIN nst.item_image_cache img ON img.jan_cd = m.jan
        WHERE m.jan IN ({placeholder})
    """
    rows = conn.execute(sql, jans).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


# ───────────────────────── tab 🆕 原生版 ─────────────────────────

with tab_native:
    st.caption(t(
        "贴 JAN → 从 nst.item_master_raw 直拉 Internal ID 等 15 个字段 → "
        "生成 NetSuite【アイテム】マスタ登録-V260326EX CSV"
    ))

    with st.expander(t("📌 字段来源与限制"), expanded=False):
        st.markdown(t(
            f"- **PG 直拉 {len(PG_TO_NST_COL)} 列**：Internal ID / 型番 / アイテム名 / JANコード / "
            "メーカー名 / 商品ランク / 取扱区分 / 部門 / 商品原価 / カートン入数 / 発注ロット / "
            "納税スケジュール / 商品重量(g) / パッケージ重量(g) / カートン重量(g)\n"
            "- **预览表里可编辑**：补缺失值（必須列空会标红）· 直接生成 CSV\n"
            "- **未命中 JAN**：顶部提示，仅命中行进 CSV\n"
            "- **JD/BM CSV**：暂走「📜 旧 HTML 版」tab"
        ))

    jan_text = st.text_area(
        t("JAN 列表（每行一个，支持空格/逗号/分号分隔）"),
        height=160,
        placeholder="4901234567890\n4905678901234\n...",
        key="page15_jan_text",
    )

    jans = _parse_jans(jan_text)
    if jans:
        st.caption(t(f"解析到 {len(jans)} 个有效 JAN（去重后）"))

    col_btn1, col_btn2 = st.columns([1, 4])
    btn_query = col_btn1.button(t("🔍 查询 PG"), type="primary", disabled=not jans)

    if btn_query:
        with get_connection() as conn:
            df = _fetch_items(conn, jans)
        st.session_state["page15_df"] = df
        st.session_state["page15_jans"] = jans

    df: pd.DataFrame | None = st.session_state.get("page15_df")
    queried_jans: list[str] = st.session_state.get("page15_jans") or []

    if df is not None and queried_jans:
        hit_jans = set(df["jan"].astype(str).tolist()) if not df.empty else set()
        miss_jans = [j for j in queried_jans if j not in hit_jans]

        c1, c2, c3 = st.columns(3)
        c1.metric(t("贴入"), len(queried_jans))
        c2.metric(t("命中"), len(hit_jans))
        c3.metric(t("未命中"), len(miss_jans), delta_color="inverse")

        if miss_jans:
            with st.expander(t(f"⚠️ {len(miss_jans)} 个 JAN 在 PG 未命中（新品？）· 不进 CSV"),
                             expanded=True):
                st.code("\n".join(miss_jans), language="text")

        if df.empty:
            st.warning(t("命中 0 件 · 请确认 JAN 已在 nst.item_master_raw（pull_items.py 拉过）"))
            st.stop()

        # 重新排成 [internal_id, image_url, ...NST 模板列]
        rename_map = {pg: nst for pg, nst in PG_TO_NST_COL.items()}
        df_view = df.rename(columns=rename_map).copy()
        # Internal ID 改名
        df_view = df_view.rename(columns={"internal_id": "Internal ID"})

        st.subheader(t("📋 预览（可直接在表里补缺）"))

        display_cols = ["Internal ID", "image_url"] + list(PG_TO_NST_COL.values())
        df_show = df_view[display_cols].copy()

        edited = st.data_editor(
            df_show,
            use_container_width=True,
            height=480,
            num_rows="fixed",
            disabled=["Internal ID", "image_url"],
            column_config={
                "Internal ID": st.column_config.TextColumn("Internal ID", width="small", disabled=True),
                "image_url": st.column_config.ImageColumn(t("缩略图"), width="small"),
                "JANコード": st.column_config.TextColumn("JANコード", width="small", disabled=True),
                "商品原価": st.column_config.NumberColumn("商品原価", format="%.2f"),
                "カートン入数": st.column_config.NumberColumn("カートン入数", format="%d"),
                "発注ロット": st.column_config.NumberColumn("発注ロット", format="%d"),
                "商品重量(g)": st.column_config.NumberColumn("商品重量(g)", format="%.1f"),
                "パッケージ重量(g)": st.column_config.NumberColumn("パッケージ重量(g)", format="%.1f"),
                "カートン重量(g)": st.column_config.NumberColumn("カートン重量(g)", format="%.1f"),
            },
            key="page15_editor",
        )

        st.divider()
        st.subheader(t("📦 生成 NetSuite CSV"))

        col_g1, col_g2 = st.columns([1, 3])
        with col_g1:
            btn_gen = st.button(t("⬇️ 生成 CSV"), type="primary")
        if btn_gen:
            field_cols = list(PG_TO_NST_COL.values())
            rows = []
            for _, r in edited.iterrows():
                row = {"Internal ID": r["Internal ID"]}
                for col in field_cols:
                    v = r.get(col)
                    if pd.notna(v) and v != "":
                        row[col] = v
                rows.append(row)

            csv_bytes = TPL.build_nst_master_csv(rows, field_cols)
            st.session_state["page15_csv_bytes"] = csv_bytes
            st.session_state["page15_csv_rows"] = len(rows)

        csv_bytes = st.session_state.get("page15_csv_bytes")
        if csv_bytes:
            st.download_button(
                t(f"⬇️ 下载 {TPL.dated_filename()}（{st.session_state.get('page15_csv_rows', 0)} 行）"),
                data=csv_bytes,
                file_name=TPL.dated_filename(),
                mime="text/csv",
                type="primary",
            )
            st.success(t("✅ CSV 已生成 · 上传到 NetSuite 即可"))


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
