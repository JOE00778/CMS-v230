"""NST 开票日 → 下单日 的日次重新分摊（Boss 2026-11-20）。

NST 按开票(出货)日记销售，周日不开票 → 周日的订单被记到周一。
店铺毛利「月内每日推移」改按下单日看：
  每个 (店铺, 开票日) 的数值按该日发票里各订单的下单日金额比例拆开。
  原价/毛利没有逐单数据 → 跟收入同比例拆（= 沿用该店该日毛利率，估算）。
  拿不到下单日（Lazada/自建站等）→ 留在开票日。
  上月下单的部分不计入本月日次，金额另行返回（页面注明）。
"""
from __future__ import annotations

import pandas as pd

VALUE_COLS = ("qty", "revenue", "defined_cost", "gross_profit")

# 发票金额按订单均摊，再按下单日聚合。仅正票（负票=取消冲销，留在开票日）。
SHARES_SQL = (
    "WITH inv AS ("
    "  SELECT trim(si.shop) AS shop, si.tran_date AS inv_date, si.total_jpy, "
    "         count(*) OVER (PARTITION BY si.invoice_id) AS n, io.order_no "
    "  FROM nst.sales_invoice si "
    "  JOIN nst.invoice_order io ON io.invoice_id = si.invoice_id "
    "  WHERE to_char(si.tran_date, 'YYYY-MM') = ? AND si.total_jpy > 0) "
    "SELECT i.shop, i.inv_date, "
    "       coalesce((e.order_create_time AT TIME ZONE 'Asia/Tokyo')::date, "
    "                cr.sale_date) AS order_date, "
    "       sum(i.total_jpy / i.n) AS amt "
    "FROM inv i "
    "LEFT JOIN shopee.escrow_order e ON e.order_sn = i.order_no "
    "LEFT JOIN coupang.v_revenue_order cr ON cr.order_no = i.order_no "
    "GROUP BY 1, 2, 3"
)


def redistribute(daily_shop: pd.DataFrame, shares: pd.DataFrame, ym: str):
    """daily_shop: shop, sale_date(开票日) + VALUE_COLS
    shares: shop, inv_date, order_date(None=未知), amt
    → (按下单日的日次 DataFrame[sale_date, *VALUE_COLS, n_shop], 上月下单收入)"""
    d = daily_shop.copy()
    d["sale_date"] = pd.to_datetime(d["sale_date"]).dt.date
    s = shares.copy()
    if not s.empty:
        s["inv_date"] = pd.to_datetime(s["inv_date"]).dt.date
        s["order_date"] = [o if pd.notna(o) else i for o, i in
                           zip(pd.to_datetime(s["order_date"]).dt.date, s["inv_date"])]
        s["amt"] = pd.to_numeric(s["amt"], errors="coerce").fillna(0.0)
        s = s[s["amt"] > 0]
        s["w"] = s["amt"] / s.groupby(["shop", "inv_date"])["amt"].transform("sum")
        s = s[["shop", "inv_date", "order_date", "w"]]
    else:
        s = pd.DataFrame(columns=["shop", "inv_date", "order_date", "w"])

    m = d.merge(s, left_on=["shop", "sale_date"], right_on=["shop", "inv_date"], how="left")
    m["order_date"] = m["order_date"].where(m["order_date"].notna(), m["sale_date"])
    m["w"] = pd.to_numeric(m["w"], errors="coerce").fillna(1.0)
    for c in VALUE_COLS:
        m[c] = pd.to_numeric(m[c], errors="coerce").fillna(0.0) * m["w"]

    in_month = pd.to_datetime(m["order_date"]).dt.strftime("%Y-%m") == ym
    prior = float(m.loc[~in_month, "revenue"].sum())
    m = m[in_month]
    out = (m.groupby("order_date", as_index=False)
           .agg(**{c: (c, "sum") for c in VALUE_COLS}, n_shop=("shop", "nunique"))
           .rename(columns={"order_date": "sale_date"})
           .sort_values("sale_date").reset_index(drop=True))
    return out, prior


# 订单数（Boss 2026-11-20「销售数量保留，另外增加订单数」= 订单ID 的个数）
# 1 注文 1 行: 当月正票に載った注文番号 · 初回開票日 · 下単日（Shopee/Coupang のみ）。
ORDERS_SQL = (
    "SELECT trim(si.shop) AS shop, io.order_no, min(si.tran_date) AS inv_date, "
    "       min(coalesce((e.order_create_time AT TIME ZONE 'Asia/Tokyo')::date, "
    "                    cr.sale_date)) AS order_date "
    "FROM nst.sales_invoice si "
    "JOIN nst.invoice_order io ON io.invoice_id = si.invoice_id "
    "LEFT JOIN shopee.escrow_order e ON e.order_sn = io.order_no "
    "LEFT JOIN coupang.v_revenue_order cr ON cr.order_no = io.order_no "
    "WHERE to_char(si.tran_date, 'YYYY-MM') = ? AND si.total_jpy > 0 "
    "GROUP BY 1, 2"
)


def daily_order_counts(orders: pd.DataFrame, ym: str) -> pd.DataFrame:
    """orders: shop, order_no, inv_date, order_date(None=未知)
    → 下单日别 订单数 DataFrame[sale_date, n_orders]。下单日不明は開票日、上月下单は除外。"""
    if orders.empty:
        return pd.DataFrame(columns=["sale_date", "n_orders"])
    o = orders.copy()
    inv = pd.to_datetime(o["inv_date"]).dt.date
    od = pd.to_datetime(o["order_date"]).dt.date
    o["sale_date"] = [d if pd.notna(d) else i for d, i in zip(od, inv)]
    o = o[pd.to_datetime(o["sale_date"]).dt.strftime("%Y-%m") == ym]
    return (o.groupby("sale_date", as_index=False)
            .agg(n_orders=("order_no", "nunique"))
            .sort_values("sale_date").reset_index(drop=True))
