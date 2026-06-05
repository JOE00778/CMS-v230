"""模块 #17 价格改善 · 当前进价 vs 最低进价 找改善空间.

业务:
- 对每个 SKU 算 need_qty = max(sold - stock + ⌈sold×0.5⌉ - ordered, 0)
- 在 purchase_data 中按 lot 接近度选「当前会用的进价」
- 跟同 JAN 全行的 min(price) 对比,差额>0 → 改善对象
"""
from __future__ import annotations

import math
import re

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n_columns import localize_df
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("价格改善"), page_icon="💰", layout="wide")
from shared.auth import require_password
require_password()
from shared.theme import inject_theme
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("💰 价格改善"))
st.caption(t("当前进价 vs 同 JAN 最低进价 · 找改善空间"))


def _normalize_jan(x):
    s = str(x).strip() if x is not None else ""
    if re.fullmatch(r"\d+(\.0+)?", s):
        return str(int(float(s)))
    return s


def _df(sql: str, params: tuple = ()) -> pd.DataFrame:
    from shared.cache import cached_df, data_version
    return cached_df(conn, sql, params, ver=data_version())


with st.spinner(t("📊 数据加载中...")):
    # 商品主档（internal_id ↔ jan / item_code / maker）
    df_item = _df(
        "SELECT internal_id, jan, item_code, display_name, maker "
        "FROM nst.item_master_raw"
    )
    # 进价 = 供应商报价（同 JAN 多供应商多档·价格改善对比源）
    df_purchase = _df(
        "SELECT jan, unit_price AS price, lot_size AS order_lot FROM supplier_quote"
    )
    # 最近完整月の全店販売数（by internal_id）
    _ym_row = conn.execute("SELECT max(year_month) AS m FROM nst.sales_monthly").fetchone()
    _latest_ym = _ym_row["m"] if _ym_row else None
    df_sales = _df(
        "SELECT item_internal_id, SUM(qty_sold) AS quantity_sold "
        "FROM nst.sales_monthly WHERE year_month = ? GROUP BY item_internal_id",
        (_latest_ym,),
    ) if _latest_ym else pd.DataFrame()
    # 最新在庫スナップショット（JD-物流-千葉）：可用量 / 在途
    df_inv = _df(
        "SELECT item_internal_id, qty_available, qty_on_hand, qty_on_order "
        "FROM nst.inventory_snapshot "
        "WHERE snapshot_date = (SELECT max(snapshot_date) FROM nst.inventory_snapshot) "
        "  AND warehouse = 'JD-物流-千葉'"
    )

if df_item.empty or df_purchase.empty:
    st.warning(t("必要数据不足（需要 nst.item_master_raw + supplier_quote 供应商报价）"))
    st.stop()
if df_sales.empty:
    st.warning(t("最近月の販売データがありません（nst.sales_monthly 未取得）。"))
    st.stop()

# internal_id 级合并：销量 + 库存 + 主档(jan)
m = df_sales.merge(df_inv, on="item_internal_id", how="left")
m = m.merge(df_item, left_on="item_internal_id", right_on="internal_id", how="left")
m["jan"] = m["jan"].apply(_normalize_jan)
m["quantity_sold"] = pd.to_numeric(m["quantity_sold"], errors="coerce").fillna(0).astype(int)
# 可用库存：优先 qty_available（利用可能），缺失回退 qty_on_hand
m["stock_available"] = pd.to_numeric(
    m["qty_available"].fillna(m["qty_on_hand"]), errors="coerce"
).fillna(0).astype(int)
m["stock_ordered"] = pd.to_numeric(m["qty_on_order"], errors="coerce").fillna(0).astype(int)

df_purchase["jan"] = df_purchase["jan"].apply(_normalize_jan)
df_purchase["price"] = pd.to_numeric(df_purchase["price"], errors="coerce").fillna(0)
df_purchase["order_lot"] = pd.to_numeric(df_purchase["order_lot"], errors="coerce").fillna(0).astype(int)

# 当前会用的进价（按需补货量 need_qty 选 lot 档）
current_prices: dict[str, float] = {}
for _, row in m.iterrows():
    jan = row["jan"]
    if not jan:
        continue
    sold = int(row["quantity_sold"])
    stock = int(row["stock_available"])
    ordered = int(row["stock_ordered"])
    options = df_purchase[df_purchase["jan"] == jan].copy()
    if options.empty:
        continue

    if stock >= sold:
        need_qty = 0
    else:
        need_qty = sold - stock + math.ceil(sold * 0.5) - ordered
        need_qty = max(need_qty, 0)
    if need_qty <= 0:
        continue

    options = options[options["order_lot"] > 0]
    if options.empty:
        continue
    options["diff"] = (options["order_lot"] - need_qty).abs()

    smaller = options[options["order_lot"] <= need_qty]
    if not smaller.empty:
        best = smaller.loc[smaller["diff"].idxmin()]
    else:
        near = options[
            (options["order_lot"] > need_qty)
            & (options["order_lot"] <= need_qty * 1.5)
            & (options["order_lot"] != 1)
        ]
        if not near.empty:
            best = near.loc[near["diff"].idxmin()]
        else:
            one = options[options["order_lot"] == 1]
            best = one.iloc[0] if not one.empty else options.sort_values("order_lot").iloc[0]

    current_prices[jan] = float(best["price"])

min_prices = df_purchase.groupby("jan")["price"].min().to_dict()

# 主档 by jan（结果展示用）
_item_by_jan = df_item.copy()
_item_by_jan["jan"] = _item_by_jan["jan"].apply(_normalize_jan)

rows = []
for jan, cur_price in current_prices.items():
    if jan in min_prices and min_prices[jan] < cur_price:
        item = _item_by_jan[_item_by_jan["jan"] == jan].head(1)
        rows.append({
            "商品コード": (item.iloc[0].get("item_code", "") if not item.empty else ""),
            "JAN": jan,
            "メーカー名": (item.iloc[0].get("maker", "") if not item.empty else ""),
            "現在の仕入価格": cur_price,
            "最安値の仕入価格": min_prices[jan],
            "差分": round(min_prices[jan] - cur_price, 2),
        })

if not rows:
    st.info(t("没找到可改善的商品。"))
    st.stop()

df_result = pd.DataFrame(rows).sort_values("差分")

st.success(t(f"✅ 改善对象: {len(df_result)} 件"))
st.dataframe(localize_df(df_result), use_container_width=True)

csv = df_result.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    t("📥 改善清单 CSV 下载"),
    data=csv,
    file_name="price_improvement_list.csv",
    mime="text/csv",
    key="price_improve_dl",
)
