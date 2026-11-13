"""NST 订货单（発注書）CSV の生成 — page10 発注書作成 と page48 新品订货申请 で共用（2026-11-13 page10 から抽出・出力不変）。"""
from __future__ import annotations

import re
from datetime import date

import numpy as np
import pandas as pd

EMPLOYEES = ["079 隋艶偉", "005 川崎里子", "037 米澤和敏", "043 徐越"]
DEPARTMENTS = ["輸出事業", "輸出事業 : 輸出（中国）"]
LOCATIONS = ["弁天倉庫"]  # Boss 2026-11-04 京东仓不再使用


def _norm_jan(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"^0{5,}", "", regex=True)


def load_item_master(conn) -> pd.DataFrame:
    """nst.item_master_raw（有効・JAN あり）→ jan/item_code/display_name/tax_schedule/order_lot（発注ロット = 起批量）。
    同 JAN が複数 active（旧レコード未停用・極少）なら last_modified 最新の 1 行。"""
    df = pd.DataFrame([dict(r) for r in conn.execute(
        "SELECT jan, item_code, display_name, tax_schedule, order_lot, last_modified "
        "FROM nst.item_master_raw "
        "WHERE is_inactive = FALSE AND jan IS NOT NULL AND jan <> ''"
    ).fetchall()])
    if df.empty:
        return df
    df.columns = df.columns.str.strip().str.lower()
    df["jan"] = _norm_jan(df["jan"])
    return (df.sort_values("last_modified", ascending=False, na_position="last")
            .drop_duplicates(subset=["jan"], keep="first")
            .drop(columns=["last_modified"]))


def tax_rate_from_schedule(ts) -> int | None:
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


def build_output(df_order: pd.DataFrame, df_item: pd.DataFrame, *, ext_id: str, sup: str, order_date: date,
                 employee: str, department: str, memo: str, location: str,
                 tax_pct: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """订货行 + item master → 标准订货单 df_out + missing JAN df。
    税率优先按 item.tax_schedule 行级匹配，DB 为空时回退 tax_pct。"""
    df_order = df_order.copy()
    df_order["jan"] = _norm_jan(df_order["jan"])
    df = df_order.merge(df_item, on="jan", how="left")
    name_col, code_col = "display_name", "item_code"
    missing = df[df[name_col].isna()]

    qty_col = "ロット×数量" if "ロット×数量" in df.columns else "数量"
    df["数量"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0).astype(int)
    df["単価"] = pd.to_numeric(df["単価"], errors="coerce").fillna(0).astype(int)
    df["金額"] = df["単価"] * df["数量"]
    if "tax_schedule" in df.columns:
        df["_tax_rate"] = df["tax_schedule"].map(tax_rate_from_schedule).fillna(tax_pct)
    else:
        df["_tax_rate"] = tax_pct
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
