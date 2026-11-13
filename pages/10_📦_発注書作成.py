"""模块 #10 発注書作成 · 文本/CSV → NetSuite 订货单.

输入两种:
  A. 粘贴文本（自由格式 JAN+数量+单价）
  B. CSV/Excel 上传（多文件 · 每文件单独选仕入先 · 2026-05-27 起）

输出: 標準 NetSuite 订货单 CSV，包含 外部ID/仕入先/日付/従業員/部門/メモ/場所/アイテム/数量/単価/金額/税額/総額
多文件时合并为单 CSV（各文件 外部 ID 默认 timestamp 接编号 1/2/3，可单独编辑 ·
NetSuite 按 外部 ID 自动分单）。
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd
import streamlit as st

from shared.db import get_readonly_connection
from shared.i18n import lang_selector, t
from shared.i18n_columns import localize_df
from shared.nst_choices import load_suppliers
from shared import po_csv

st.set_page_config(page_title=t("発注書作成"), page_icon="📦", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_readonly_connection()

st.title(t("📦 発注書作成"))
st.caption(t("文本 / CSV / Excel → NetSuite 订货单 CSV"))


def _parse_items_text(text: str) -> pd.DataFrame:
    """从粘贴文本里提取 JAN + 数量 + 単価。
    每行尝试匹配 13 位 JAN 起头, 再抽 1-2 个整数作数量/単価。"""
    rows = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.search(r"\b(\d{8,14})\b", ln)
        if not m:
            continue
        jan = m.group(1)
        nums = [int(x) for x in re.findall(r"\b(\d+)\b", ln) if x != jan]
        qty = nums[0] if len(nums) >= 1 else 0
        price = nums[1] if len(nums) >= 2 else 0
        rows.append({"jan": jan, "数量": qty, "単価": price})
    return pd.DataFrame(rows)


def _provide_template():
    template = pd.DataFrame({"jan": [], "数量": [], "単価": []})
    csv_temp = template.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        t("📝 入力 CSV テンプレ下载"),
        data=csv_temp,
        mime="text/csv",
        file_name="order_template.csv",
        help=t("必須列: jan, 数量, 単価"),
    )


_provide_template()


def _parse_uploaded(file) -> pd.DataFrame | None:
    """单个 CSV/Excel → 规范列名（jan/数量/単価/...）。失败返回 None。"""
    try:
        if file.name.endswith(".xlsx"):
            df = pd.read_excel(file)
        else:
            df = pd.read_csv(file, encoding="utf-8-sig")
    except UnicodeDecodeError:
        file.seek(0)
        df = pd.read_csv(file, encoding="shift_jis")
    df.columns = df.columns.str.strip().str.lower()
    rename_map = {
        "janコード": "jan", "ｊａｎ": "jan", "jan": "jan",
        "数量": "数量", "数": "数量", "qty": "数量",
        "ロット×数量": "ロット×数量",
        "単価": "単価", "価格": "単価", "price": "単価",
    }
    df.rename(columns={k.lower(): v for k, v in rename_map.items() if k.lower() in df.columns},
              inplace=True)
    if "jan" not in df.columns:
        return None
    return df


# 订货单 meta（dropdown）— SUPPLIERS 定义提前供文件名预选用
try:
    SUPPLIERS = load_suppliers(conn)
except Exception as _e_sup:  # noqa: BLE001
    SUPPLIERS = []
    st.error(t("仕入先マスタ取得失敗: ") + str(_e_sup))
if not SUPPLIERS:
    # 空のまま進むと 仕入先 が空欄の CSV を作ってしまい NetSuite 取込で落ちる
    st.error(t("⚠️ 仕入先マスタが空です。page 27「📥 NST 取得データ」→ "
               "「🏭 仕入先マスタ」で更新してください。"))
    st.stop()
from shared.po_csv import DEPARTMENTS, EMPLOYEES, LOCATIONS  # noqa: E402 — page48 と共用


def _guess_supplier(filename: str) -> int:
    """文件名 0XXX 前缀 → SUPPLIERS index（命中返回索引·没命中返回 0）。"""
    m = re.search(r"(\d{4})", filename or "")
    if not m:
        return 0
    prefix = m.group(1)
    for i, s in enumerate(SUPPLIERS):
        if s.startswith(prefix + " "):
            return i
    return 0


# 全局唯一时间戳：用作文本模式外部 ID / 多文件外部 ID 前缀 / 下载文件名
external_id = datetime.now().strftime("%Y%m%d%H%M%S")

option = st.radio(t("输入方式"), ["文本粘贴", "CSV / Excel 上传（多文件）"], format_func=t)
# 多文件模式: list of {filename, df_order, supplier}; 文本模式: 单 df_order
files_data: list[dict] = []
df_order_text = None

if option == "文本粘贴":
    input_text = st.text_area(t("粘贴订货文本"), height=300)
    if st.button(t("✏️ 文本转换")):
        if not input_text.strip():
            st.warning(t("⚠ 请先输入文本"))
        else:
            df_order_text = _parse_items_text(input_text)
            if df_order_text is None or df_order_text.empty:
                st.warning(t("⚠ 没能从文本中识别出 JAN+数量"))
                df_order_text = None
else:
    uploaded_list = st.file_uploader(
        t("上传订货 CSV / Excel（可多选）"), type=["csv", "xlsx"],
        accept_multiple_files=True,
    )
    if uploaded_list:
        for idx, f in enumerate(sorted(uploaded_list, key=lambda x: x.name)):
            with st.expander(f"📄 {f.name}", expanded=True):
                df_one = _parse_uploaded(f)
                if df_one is None:
                    st.error(t("❌ CSV / Excel 没有 'jan' 列"))
                    continue
                c1, c2 = st.columns([2, 3])
                with c1:
                    ext_one = st.text_input(
                        t("外部 ID"), value=f"{external_id}{idx + 1}",
                        key=f"ext_{idx}_{f.name}",
                    )
                with c2:
                    default_idx = _guess_supplier(f.name)
                    sup_one = st.selectbox(
                        t("仕入先"), SUPPLIERS, index=default_idx,
                        key=f"sup_{idx}_{f.name}",
                    )
                st.caption(t("{n} 行").format(n=len(df_one)))
                files_data.append({"filename": f.name, "df_order": df_one,
                                   "supplier": sup_one, "external_id": ext_one})

# 共通 meta
col1, col2, col3 = st.columns(3)
with col1:
    if option == "文本粘贴":
        st.text_input(t("外部 ID"), value=external_id, disabled=True)
        supplier_text = st.selectbox(t("仕入先"), SUPPLIERS)
    else:
        st.caption(t("外部 ID / 仕入先 由各文件单独指定 ↑"))
with col2:
    order_date = st.date_input(t("日付"), value=date.today())
    employee = st.selectbox(t("従業員"), EMPLOYEES)
with col3:
    department = st.selectbox(t("部門"), DEPARTMENTS)
    location = st.selectbox(t("場所"), LOCATIONS)
memo = st.text_input(t("备注"), "")
_tax_pct = st.selectbox(t("税率 fallback（DB 缺失时使用）"), [10, 8, 0],
                        format_func=lambda x: f"{x}%",
                        help=t("行级税率优先取 item_master_raw.tax_schedule 内的数字 "
                               "（例 '標準税率 10%' → 10）；DB 为空时回退到此 fallback。"
                               "一元管理 1.0 CSV 本身不带税率列，由 NetSuite 端导入算税。"))


def _build_output(df_order: pd.DataFrame, df_item: pd.DataFrame, *,
                  ext_id: str, sup: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """订货行 + item master → 标准订货单 df_out + missing JAN df（本体は shared/po_csv・page48 と共用）。"""
    return po_csv.build_output(df_order, df_item, ext_id=ext_id, sup=sup, order_date=order_date,
                               employee=employee, department=department, memo=memo,
                               location=location, tax_pct=_tax_pct)


# 输出阶段（item_master_raw 只取一次复用）
if files_data or (df_order_text is not None and not df_order_text.empty):
    # inactive 除外・同 JAN は last_modified 最新（shared/po_csv.load_item_master）
    df_item = po_csv.load_item_master(conn)
    if df_item.empty:
        st.error(t("❌ nst.item_master_raw 表为空。请到 page 27「📥 NST 取得数据」执行 items 取得。"))
        st.stop()

    st.subheader(t("📑 訂货单预览"))

    # 文本模式 → 单 df_out（沿用旧逻辑）
    if df_order_text is not None and not df_order_text.empty:
        df_out, missing = _build_output(df_order_text, df_item,
                                        ext_id=external_id, sup=supplier_text)
        if len(missing) > 0:
            st.warning(t("⚠ {n} 件 JAN 在 nst.item_master_raw 找不到").format(n=len(missing)))
            st.dataframe(localize_df(missing[["jan"]]))
        st.dataframe(df_out.rename(columns=t), use_container_width=True)
        st.download_button(
            t("📥 订货单 CSV 下载"),
            data=df_out.to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"発注書_{external_id}.csv", mime="text/csv",
        )

    # 多文件模式 → 每文件一段预览 + 合并为单 CSV 下载
    if files_data:
        outputs: list[pd.DataFrame] = []
        for fd in files_data:
            df_out, missing = _build_output(fd["df_order"], df_item,
                                            ext_id=fd["external_id"],
                                            sup=fd["supplier"])
            with st.expander(
                t("📄 {f} → {s} ({n} 行 · ¥{a:,})").format(
                    f=fd["filename"], s=fd["supplier"], n=len(df_out), a=int(df_out["総額"].sum())),
                expanded=(len(files_data) == 1),
            ):
                if len(missing) > 0:
                    st.warning(t("⚠ {n} 件 JAN 在 nst.item_master_raw 找不到").format(n=len(missing)))
                    st.dataframe(localize_df(missing[["jan"]]))
                st.dataframe(df_out.rename(columns=t), use_container_width=True)  # 表示のみ訳す（CSV は NetSuite 形式のまま）
            outputs.append(df_out)

        # 合并所有文件为单 CSV（各文件用 外部ID _NN 区分 → NetSuite 自动分单）
        df_merged = pd.concat(outputs, ignore_index=True)
        st.download_button(
            t("📥 订货单 CSV 下载（合并 {n} 个文件 · {rows} 行）").format(
                n=len(outputs), rows=len(df_merged)),
            data=df_merged.to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"発注書_{external_id}.csv", mime="text/csv",
        )
