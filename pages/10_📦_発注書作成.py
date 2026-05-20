"""模块 #10 発注書作成 · 文本/CSV → NetSuite 订货单.

输入两种:
  A. 粘贴文本（自由格式 JAN+数量+单价）
  B. CSV/Excel 上传（含 jan, 数量, 単価 列）

输出: 標準 NetSuite 订货单 CSV，包含 外部ID/仕入先/日付/従業員/部門/メモ/場所/アイテム/数量/単価/金額/税額/総額
"""
from __future__ import annotations

import re
from datetime import date, datetime

import numpy as np
import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t
from shared.i18n_columns import localize_df

st.set_page_config(page_title=t("発注書作成"), page_icon="📦", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

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

option = st.radio(t("输入方式"), [t("文本粘贴"), t("CSV / Excel 上传")])
df_order = None

if option == t("文本粘贴"):
    input_text = st.text_area(t("粘贴订货文本"), height=300)
    if st.button(t("✏️ 文本转换")):
        if not input_text.strip():
            st.warning(t("⚠ 请先输入文本"))
        else:
            df_order = _parse_items_text(input_text)
            if df_order is None or df_order.empty:
                st.warning(t("⚠ 没能从文本中识别出 JAN+数量"))
                df_order = None
else:
    uploaded = st.file_uploader(t("上传订货 CSV / Excel"), type=["csv", "xlsx"])
    if uploaded is not None:
        try:
            if uploaded.name.endswith(".xlsx"):
                df_order = pd.read_excel(uploaded)
            else:
                df_order = pd.read_csv(uploaded, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df_order = pd.read_csv(uploaded, encoding="shift_jis")
        df_order.columns = df_order.columns.str.strip().str.lower()
        rename_map = {
            "janコード": "jan", "ｊａｎ": "jan", "jan": "jan",
            "数量": "数量", "数": "数量", "qty": "数量",
            "ロット×数量": "ロット×数量",
            "単価": "単価", "価格": "単価", "price": "単価",
        }
        df_order.rename(
            columns={k.lower(): v for k, v in rename_map.items() if k.lower() in df_order.columns},
            inplace=True,
        )
        if "jan" not in df_order.columns:
            st.error(t("❌ CSV / Excel 没有 'jan' 列"))
            df_order = None

# 订货单 meta（dropdown）
SUPPLIERS = [
    "0020 エンパイヤ自動車株式会社（KONNGU'S）", "0025 株式会社オンダ", "0029 K・BLUE株式会社",
    "0072 新富士バーナー株式会社", "0073 株式会社　エィチ・ケイ", "0077 大分共和株式会社",
    "0085 中央物産株式会社", "0106 西川株式会社", "0197 大木化粧品株式会社", "0201 現金仕入れ",
    "0202 トラスコ中山株式会社", "0256 株式会社 グランジェ", "0258 株式会社 ファイン",
    "0263 株式会社メディファイン", "0285 有限会社オーザイ首藤", "0343 株式会社森フォレスト",
    "0376 菅野株式会社", "0402 ハリマ共和物産株式会社", "0411 株式会社ラクーンコマース（スーパーデリバリー）",
    "0435 株式会社 流久商事", "0444 ハナモンワークス 合同会社", "0445 富森商事 株式会社",
    "0457 カネイシ株式会社", "0468 王子国際貿易株式会社", "0469 株式会社 新日配薬品",
    "0474 株式会社　五洲", "0475 株式会社シゲマツ", "0476 カード仕入れ",
    "0479 スケーター株式会社", "0482 風雲商事株式会社", "0484 ZSA商事株式会社",
    "0486 Maple International株式会社", "0490 NEW WIND株式会社", "0491 アプライド株式会社",
    "0504 京浜商事株式会社", "0510 株式会社タジマヤ", "C000510 太田物産 株式会社",
    "0042 ビクトリノックス・ジャパン株式会社",
]
EMPLOYEES = ["079 隋艶偉", "005 川崎里子", "037 米澤和敏", "043 徐越"]
DEPARTMENTS = ["輸出事業", "輸出事業 : 輸出（中国）"]
LOCATIONS = ["JD-物流-千葉", "弁天倉庫"]

col1, col2, col3 = st.columns(3)
with col1:
    external_id = datetime.now().strftime("%Y%m%d%H%M%S")
    st.text_input(t("外部 ID"), value=external_id, disabled=True)
    supplier = st.selectbox(t("仕入先"), SUPPLIERS)
with col2:
    order_date = st.date_input(t("日付"), value=date.today())
    employee = st.selectbox(t("従業員"), EMPLOYEES)
with col3:
    department = st.selectbox(t("部門"), DEPARTMENTS)
    location = st.selectbox(t("場所"), LOCATIONS)
memo = st.text_input(t("备注"), "")
_tax_pct = st.selectbox(t("税率"), [10, 8, 0], format_func=lambda x: f"{x}%",
                        help=t("NST 商品マスタは税区分を持たないため一括指定"))

if df_order is not None and not df_order.empty:
    df_item = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT jan, item_code, display_name FROM nst.item_master_raw"
    ).fetchall()])
    if df_item.empty:
        st.error(t("❌ nst.item_master_raw 表为空。请到 page 27「📥 NST 取得数据」执行 items 取得。"))
        st.stop()
    df_item.columns = df_item.columns.str.strip().str.lower()

    df_order["jan"] = df_order["jan"].astype(str).str.strip().str.replace(r"^0{5,}", "", regex=True)
    df_item["jan"] = df_item["jan"].astype(str).str.strip().str.replace(r"^0{5,}", "", regex=True)

    df = df_order.merge(df_item, on="jan", how="left")
    name_col = "display_name"
    code_col = "item_code"

    missing = df[df[name_col].isna()] if name_col in df.columns else pd.DataFrame()
    if len(missing) > 0:
        st.warning(t(f"⚠ {len(missing)} 件 JAN 在 nst.item_master_raw 找不到"))
        st.dataframe(localize_df(missing[["jan"]]))

    qty_col = "ロット×数量" if "ロット×数量" in df.columns else "数量"
    df["数量"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0).astype(int)
    df["単価"] = pd.to_numeric(df["単価"], errors="coerce").fillna(0).astype(int)
    df["金額"] = df["単価"] * df["数量"]
    df["税額"] = np.floor(df["金額"] * (_tax_pct / 100)).fillna(0).astype(int)
    df["総額"] = df["金額"] + df["税額"]

    df_out = pd.DataFrame({
        "外部ID": external_id,
        "仕入先": supplier,
        "日付": order_date.strftime("%Y/%m/%d"),
        "従業員": employee,
        "部門": department,
        "メモ": memo,
        "場所": location,
        "アイテム": (df.get(code_col, "").astype(str) + " " + df.get(name_col, "").astype(str)).str.strip(),
        "数量": df["数量"],
        "単価/率": df["単価"],
        "金額": df["金額"],
        "税額": df["税額"],
        "総額": df["総額"],
    })

    st.subheader(t("📑 訂货单预览"))
    st.dataframe(df_out, use_container_width=True)

    csv_out = df_out.to_csv(index=False, encoding="utf-8-sig")
    st.download_button(
        t("📥 订货单 CSV 下载"),
        data=csv_out,
        file_name=f"発注書_{external_id}.csv",
        mime="text/csv",
    )
