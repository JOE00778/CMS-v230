"""模块 #34 Shopee 财务 (API 版) · shopee_api ingester 直拉 escrow_order.

与 page 14（EXCEL 上传 v4）区分：
- page 14: 手动拖 Shopee 后台导出的 income / orders Excel，按 NST 6 列汇总
- page 34: shopee_api ingester (pull_escrow.py) 每天自动同步 → shopee.escrow_order

数据源:
- shopee.escrow_order  订单托管金额明细（GET /api/v2/payment/get_escrow_detail）
- shopee.pull_log      ingester 拉取审计（数据健康度）

当前阶段（2026-05-29）:
- Sandbox PH shop OAuth 已通 · escrow 表 schema 已建
- Live App 审核中（Boss 已提交 Go-Live · 153.187.107.190 已加白名单）
- 审核通过 → Live OAuth → 真实数据开始流入
- 在此之前 escrow_order 可能为空 → 页面给「等待数据」空状态
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t

st.set_page_config(page_title=t("Shopee 财务 (API)"), page_icon="💱", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme

require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("💱 Shopee 财务 (API)"))
st.caption(t(
    "shopee_api ingester 直拉 · escrow_order 订单托管金额明细 · "
    "按订单创建时间汇总（与 page 14 EXCEL 版互补）"
))


# -------------------------------------------------------------
# 数据健康度（顶部 1 行）
# -------------------------------------------------------------
def _load_pull_log() -> pd.DataFrame:
    sql = """
        SELECT domain, country, started_at, finished_at,
               fetched, upserted, errors, status, error_message
        FROM shopee.pull_log
        WHERE domain = 'escrow'
        ORDER BY started_at DESC
        LIMIT 20
    """
    try:
        return pd.read_sql(sql, conn)
    except Exception as e:
        st.warning(f"shopee.pull_log 读取失败: {e}")
        return pd.DataFrame()


def _load_escrow_window(since: str, until: str, countries: list[str]) -> pd.DataFrame:
    if not countries:
        return pd.DataFrame()
    placeholders = ",".join(["%s"] * len(countries))
    sql = f"""
        SELECT
            country, order_sn, shop_id, buyer_user_name,
            order_create_time, currency,
            order_selling_price, seller_discount, shopee_discount,
            voucher_from_seller, voucher_from_shopee, coins,
            buyer_paid_shipping_fee, actual_shipping_fee,
            shopee_shipping_rebate, final_shipping_fee,
            commission_fee, service_fee, seller_transaction_fee,
            transaction_fee, cross_border_tax, payment_promotion,
            escrow_amount, payout_amount,
            buyer_total_amount, return_order_sn_list,
            pulled_at
        FROM shopee.escrow_order
        WHERE country IN ({placeholders})
          AND order_create_time::date BETWEEN %s AND %s
        ORDER BY order_create_time DESC
        LIMIT 5000
    """
    try:
        return pd.read_sql(sql, conn, params=[*countries, since, until])
    except Exception as e:
        st.error(f"shopee.escrow_order 查询失败: {e}")
        return pd.DataFrame()


def _all_countries() -> list[str]:
    try:
        df = pd.read_sql(
            "SELECT DISTINCT country FROM shopee.escrow_order ORDER BY country",
            conn,
        )
        return df["country"].tolist()
    except Exception:
        return []


# -------------------------------------------------------------
# 顶栏：pull_log 健康度
# -------------------------------------------------------------
log_df = _load_pull_log()
with st.expander(t("📡 数据同步状态 (escrow 最近 20 次)"), expanded=False):
    if log_df.empty:
        st.info(t(
            "暂无 ingester 拉取日志。Go-Live 审核通过后由 cms_nst_scheduler 自动调度。"
        ))
    else:
        last = log_df.iloc[0]
        cols = st.columns(4)
        cols[0].metric(t("最近拉取"), str(last["started_at"])[:16])
        cols[1].metric(t("状态"), str(last["status"]))
        cols[2].metric(t("订单获取"), int(last.get("fetched") or 0))
        cols[3].metric(t("Upsert"), int(last.get("upserted") or 0))
        st.dataframe(log_df, use_container_width=True, hide_index=True)


# -------------------------------------------------------------
# 筛选栏
# -------------------------------------------------------------
available_countries = _all_countries()

if not available_countries:
    st.info(t(
        "🟡 escrow_order 暂无数据。原因可能是：\n"
        "1. Live App 审核中（Boss 已提交 · 等 Shopee 3–7 天）\n"
        "2. Live OAuth 未完成 · 缺 SHOPEE_REFRESH_TOKENS\n"
        "3. cms_nst_scheduler 未启用 escrow domain\n"
        "审核通过后会自动开始拉取并显示在此。"
    ))
    st.stop()

with st.container(border=True):
    c1, c2, c3 = st.columns([2, 2, 2])
    sel_countries = c1.multiselect(
        t("国家 (country)"),
        options=available_countries,
        default=available_countries,
    )
    today = pd.Timestamp.now(tz="Asia/Tokyo").date()
    sel_since = c2.date_input(t("订单创建 从"), value=today - pd.Timedelta(days=30))
    sel_until = c3.date_input(t("订单创建 至"), value=today)

df = _load_escrow_window(str(sel_since), str(sel_until), sel_countries)

if df.empty:
    st.warning(t("当前筛选区间无数据"))
    st.stop()


# -------------------------------------------------------------
# KPI（按原币种 · 不换汇 · 跨国累加仅作占位）
# -------------------------------------------------------------
n_orders = len(df)
gmv = df["order_selling_price"].fillna(0).sum()
discount_total = (
    df["seller_discount"].fillna(0)
    + df["shopee_discount"].fillna(0)
    + df["voucher_from_seller"].fillna(0)
    + df["voucher_from_shopee"].fillna(0)
).sum()
commission = df["commission_fee"].fillna(0).sum()
service = df["service_fee"].fillna(0).sum()
escrow_total = df["escrow_amount"].fillna(0).sum()

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric(t("订单数"), f"{n_orders:,}")
k2.metric(t("挂牌总额"), f"{gmv:,.0f}")
k3.metric(t("折扣/券合计"), f"-{discount_total:,.0f}")
k4.metric(t("平台费 (佣金+服务费)"), f"-{(commission + service):,.0f}")
k5.metric(t("卖家实收 (escrow)"), f"{escrow_total:,.0f}")

st.caption(t(
    "⚠️ 多国累加仅原币种 sum · 跨国对比需用 shared/forex 换算到 JPY（后续接入）"
))


# -------------------------------------------------------------
# 明细表
# -------------------------------------------------------------
st.subheader(t("订单明细"))

show_cols = [
    "country", "order_sn", "shop_id", "currency",
    "order_create_time",
    "order_selling_price", "seller_discount", "shopee_discount",
    "commission_fee", "service_fee",
    "escrow_amount", "payout_amount",
]
view = df[show_cols].copy()
st.dataframe(view, use_container_width=True, hide_index=True, height=520)

csv = df.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    label=t("下载 CSV"),
    data=csv,
    file_name=f"shopee_escrow_{sel_since}_{sel_until}.csv",
    mime="text/csv",
)
