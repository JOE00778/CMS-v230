"""模块 #10 発注書作成 · 文本/CSV → NetSuite 订货单.

输入两种:
  A. 粘贴文本（自由格式 JAN+数量+单价）
  B. CSV/Excel 上传（多文件 · 每文件单独选仕入先 · 2026-05-27 起）

输出: 標準 NetSuite 订货单 CSV，包含 外部ID/仕入先/日付/従業員/部門/メモ/場所/アイテム/数量/単価/金額/税額/総額
多文件时合并为单 CSV（各文件 外部ID 加 _NN 后缀 · NetSuite 按 外部ID 自动分单）。
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


option = st.radio(t("输入方式"), [t("文本粘贴"), t("CSV / Excel 上传（多文件）")])
# 多文件模式: list of {filename, df_order, supplier}; 文本模式: 单 df_order
files_data: list[dict] = []
df_order_text = None

if option == t("文本粘贴"):
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
        _now_ts = datetime.now().strftime("%Y%m%d%H%M%S")
        for idx, f in enumerate(sorted(uploaded_list, key=lambda x: x.name)):
            with st.expander(f"📄 {f.name}", expanded=True):
                df_one = _parse_uploaded(f)
                if df_one is None:
                    st.error(t("❌ CSV / Excel 没有 'jan' 列"))
                    continue
                c1, c2 = st.columns([2, 3])
                with c1:
                    ext_one = st.text_input(
                        t("外部 ID"), value=f"{_now_ts}{idx + 1}",
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
    external_id = datetime.now().strftime("%Y%m%d%H%M%S")
    if option == t("文本粘贴"):
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


def _tax_rate_from_schedule(ts) -> int | None:
    """tax_schedule 文本 → 税率整数。
    例: '仕入10%＆販売免税' / '消費税10%' → 10
        '仕入8%＆販売免税' / '消費税8%' → 8
        '免税' / '海外仕入' → 0
        无法识别返回 None。"""
    if not ts or pd.isna(ts):
        return None
    s = str(ts)
    m = re.search(r"(\d+)\s*%", s)
    if m:
        return int(m.group(1))
    if "免税" in s or "海外" in s:
        return 0
    return None


def _build_output(df_order: pd.DataFrame, df_item: pd.DataFrame, *,
                  ext_id: str, sup: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """订货行 + item master → 标准订货单 df_out + missing JAN df。
    税率优先按 item.tax_schedule 行级匹配，DB 为空时回退 fallback。"""
    df_order = df_order.copy()
    df_order["jan"] = (df_order["jan"].astype(str).str.strip()
                       .str.replace(r"^0{5,}", "", regex=True))
    df = df_order.merge(df_item, on="jan", how="left")
    name_col, code_col = "display_name", "item_code"
    missing = df[df[name_col].isna()] if name_col in df.columns else pd.DataFrame()

    qty_col = "ロット×数量" if "ロット×数量" in df.columns else "数量"
    df["数量"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0).astype(int)
    df["単価"] = pd.to_numeric(df["単価"], errors="coerce").fillna(0).astype(int)
    df["金額"] = df["単価"] * df["数量"]
    # 行级税率：优先 DB tax_schedule，缺失走 fallback
    if "tax_schedule" in df.columns:
        df["_tax_rate"] = df["tax_schedule"].map(_tax_rate_from_schedule).fillna(_tax_pct)
    else:
        df["_tax_rate"] = _tax_pct
    df["税額"] = np.floor(df["金額"] * df["_tax_rate"].astype(float) / 100).fillna(0).astype(int)
    df["総額"] = df["金額"] + df["税額"]

    df_out = pd.DataFrame({
        "外部ID": ext_id,
        "仕入先": sup,
        "日付": order_date.strftime("%Y/%m/%d"),
        "従業員": employee,
        "部門": department,
        "メモ": memo,
        "場所": location,
        "アイテム": (df.get(code_col, "").astype(str) + " "
                  + df.get(name_col, "").astype(str)).str.strip(),
        "数量": df["数量"],
        "単価/率": df["単価"],
        "金額": df["金額"],
        "税額": df["税額"],
        "総額": df["総額"],
    })
    return df_out, missing


# 输出阶段（item_master_raw 只取一次复用）
if files_data or (df_order_text is not None and not df_order_text.empty):
    # 过滤掉 inactive items（防止 JAN 重复 → 同一 JAN 双 item record 都被 merge 带出）
    df_item = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT jan, item_code, display_name, tax_schedule, last_modified "
        "FROM nst.item_master_raw "
        "WHERE is_inactive = FALSE AND jan IS NOT NULL AND jan <> ''"
    ).fetchall()])
    if df_item.empty:
        st.error(t("❌ nst.item_master_raw 表为空。请到 page 27「📥 NST 取得数据」执行 items 取得。"))
        st.stop()
    df_item.columns = df_item.columns.str.strip().str.lower()
    df_item["jan"] = (df_item["jan"].astype(str).str.strip()
                      .str.replace(r"^0{5,}", "", regex=True))
    # 同 JAN 仍可能多 active 行（极少 · 旧记录未及时停用）→ 保留 last_modified 最新一条
    df_item = (df_item.sort_values("last_modified", ascending=False, na_position="last")
               .drop_duplicates(subset=["jan"], keep="first")
               .drop(columns=["last_modified"]))

    st.subheader(t("📑 訂货单预览"))

    # 文本模式 → 单 df_out（沿用旧逻辑）
    if df_order_text is not None and not df_order_text.empty:
        df_out, missing = _build_output(df_order_text, df_item,
                                        ext_id=external_id, sup=supplier_text)
        if len(missing) > 0:
            st.warning(t("⚠ {n} 件 JAN 在 nst.item_master_raw 找不到").format(n=len(missing)))
            st.dataframe(localize_df(missing[["jan"]]))
        st.dataframe(df_out, use_container_width=True)
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
                f"📄 {fd['filename']} → {fd['supplier']} "
                f"({len(df_out)} {t('行')} · ¥{int(df_out['総額'].sum()):,})",
                expanded=(len(files_data) == 1),
            ):
                if len(missing) > 0:
                    st.warning(t("⚠ {n} 件 JAN 在 nst.item_master_raw 找不到").format(n=len(missing)))
                    st.dataframe(localize_df(missing[["jan"]]))
                st.dataframe(df_out, use_container_width=True)
            outputs.append(df_out)

        # 合并所有文件为单 CSV（各文件用 外部ID _NN 区分 → NetSuite 自动分单）
        df_merged = pd.concat(outputs, ignore_index=True)
        st.download_button(
            t("📥 订货单 CSV 下载（合并 {n} 个文件 · {rows} 行）").format(
                n=len(outputs), rows=len(df_merged)),
            data=df_merged.to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"発注書_{external_id}.csv", mime="text/csv",
        )
