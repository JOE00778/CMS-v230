"""模块 ④ 財務 v4 · 与 NST 上传颗粒度对齐.

业务约定 (NST 上传规则):
- 按【订单成立时间】月份切分文件 (不是拨款日, 不是按周)
- 上传文件统一 6 列: 编号 / 订单编号 / 拨款完成日期 / 付款金额 / 退款金额 / 账单金额
- 单文件 ≤899 行, 超限按 (1)(2)... 拆分

NST 列计算:
- 付款金额 = 商品原价 + 商品折扣 + 退款金额  (gross_price + product_discount + refund_amount)
- 退款金额 = refund_amount
- 账单金额 = sum(refund_amount 右侧到 payout_amount 左侧的费用列)
            = shopee_rebate + seller_voucher + ... + service_fee + transaction_fee + fbs_fee 等

数据源:
- shopee_orders_raw   ← 订单导出.xlsx (订单 ID + SKU + 店铺)
- shopee_income_lines ← *.income.已拨款.*.xlsx (拨款扣费, 含 seller_account / order_created_at)
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.forex import FX_TO_JPY
from shared.i18n import lang_selector, t
from shared.i18n_columns import localize_df

st.set_page_config(page_title=t("財務"), page_icon="💱", layout="wide")
from shared.auth import require_password
from shared.theme import inject_theme
require_password()
inject_theme()
lang_selector()
conn = get_connection()

st.title(t("💱 財務"))

# ============================================================
# 板块切换 (Shopee / Lazada · Shopify 自建站)
# ============================================================
SECTION_SHOPEE = t("🛒 Shopee / Lazada (东南亚)")
SECTION_SHOPEE_API = t("🛒 Shopee API 直拉 (NEW)")
SECTION_SHOPIFY = t("🛍 Shopify 自建站")

section = st.radio(
    t("📊 财务板块"),
    [SECTION_SHOPEE, SECTION_SHOPEE_API, SECTION_SHOPIFY],
    horizontal=True,
    key="finance_section",
)
st.divider()


# ============================================================
# 板块 2: Shopee API 直拉 (shopee_api ingester · escrow_order)
# ============================================================
if section == SECTION_SHOPEE_API:
    st.subheader(t("🛒 Shopee API 直拉 · escrow_order 订单托管金额"))
    st.caption(t(
        "shopee_api ingester 自动同步 · 与 EXCEL 上传版互补 · "
        "Go-Live 审核通过后开始自动有数据"
    ))

    def _api_load_pull_log() -> pd.DataFrame:
        try:
            return pd.read_sql(
                "SELECT domain, country, started_at, finished_at, "
                "fetched, upserted, errors, status, error_message "
                "FROM shopee.pull_log WHERE domain = 'escrow' "
                "ORDER BY started_at DESC LIMIT 20",
                conn,
            )
        except Exception as e:
            st.warning(f"shopee.pull_log 读取失败: {e}")
            return pd.DataFrame()

    def _api_load_escrow(since: str, until: str, countries: list[str]) -> pd.DataFrame:
        if not countries:
            return pd.DataFrame()
        placeholders = ",".join(["%s"] * len(countries))
        sql = f"""
            SELECT country, order_sn, shop_id, buyer_user_name,
                   order_create_time, currency,
                   order_selling_price, seller_discount, shopee_discount,
                   voucher_from_seller, voucher_from_shopee, coins,
                   buyer_paid_shipping_fee, actual_shipping_fee,
                   shopee_shipping_rebate, final_shipping_fee,
                   commission_fee, service_fee, seller_transaction_fee,
                   transaction_fee, cross_border_tax, payment_promotion,
                   escrow_amount, payout_amount,
                   buyer_total_amount, return_order_sn_list, pulled_at
            FROM shopee.escrow_order
            WHERE country IN ({placeholders})
              AND order_create_time::date BETWEEN %s AND %s
            ORDER BY order_create_time DESC LIMIT 5000
        """
        try:
            return pd.read_sql(sql, conn, params=[*countries, since, until])
        except Exception as e:
            st.error(f"shopee.escrow_order 查询失败: {e}")
            return pd.DataFrame()

    def _api_countries() -> list[str]:
        try:
            return pd.read_sql(
                "SELECT DISTINCT country FROM shopee.escrow_order ORDER BY country",
                conn,
            )["country"].tolist()
        except Exception:
            return []

    log_df = _api_load_pull_log()
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

    available_countries = _api_countries()
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
            t("国家 (country)"), options=available_countries,
            default=available_countries,
        )
        today = pd.Timestamp.now(tz="Asia/Tokyo").date()
        sel_since = c2.date_input(t("订单创建 从"), value=today - pd.Timedelta(days=30))
        sel_until = c3.date_input(t("订单创建 至"), value=today)

    df_api = _api_load_escrow(str(sel_since), str(sel_until), sel_countries)
    if df_api.empty:
        st.warning(t("当前筛选区间无数据"))
        st.stop()

    n_orders = len(df_api)
    gmv = df_api["order_selling_price"].fillna(0).sum()
    discount_total = (
        df_api["seller_discount"].fillna(0)
        + df_api["shopee_discount"].fillna(0)
        + df_api["voucher_from_seller"].fillna(0)
        + df_api["voucher_from_shopee"].fillna(0)
    ).sum()
    commission = df_api["commission_fee"].fillna(0).sum()
    service = df_api["service_fee"].fillna(0).sum()
    escrow_total = df_api["escrow_amount"].fillna(0).sum()

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric(t("订单数"), f"{n_orders:,}")
    k2.metric(t("挂牌总额"), f"{gmv:,.0f}")
    k3.metric(t("折扣/券合计"), f"-{discount_total:,.0f}")
    k4.metric(t("平台费 (佣金+服务费)"), f"-{(commission + service):,.0f}")
    k5.metric(t("卖家实收 (escrow)"), f"{escrow_total:,.0f}")
    st.caption(t(
        "⚠️ 多国累加仅原币种 sum · 跨国对比需用 shared/forex 换算到 JPY（后续接入）"
    ))

    st.subheader(t("订单明细"))
    show_cols = [
        "country", "order_sn", "shop_id", "currency",
        "order_create_time",
        "order_selling_price", "seller_discount", "shopee_discount",
        "commission_fee", "service_fee",
        "escrow_amount", "payout_amount",
    ]
    st.dataframe(
        df_api[show_cols], use_container_width=True, hide_index=True, height=520,
    )
    st.download_button(
        label=t("下载 CSV"),
        data=df_api.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"shopee_escrow_{sel_since}_{sel_until}.csv",
        mime="text/csv",
    )
    st.stop()


# ============================================================
# 板块 3: Shopify 自建站 (规划中)
# ============================================================
if section == SECTION_SHOPIFY:
    st.subheader(t("🛍 Shopify 自建站财务"))
    st.warning(t("🚧 数据接入计划中 (SmikieJapan · PH/KR/US)"))

    with st.expander(t("📋 详细接入计划与下一步"), expanded=False):
        st.markdown(t("""
**站点结构**: SmikieJapan (smikiejapan.com, 单 store) → 3 markets

| 市场 | 货币 | 状态 |
|---|---|---|
| PH | PHP | 已部署 |
| KR | KRW | 新增 |
| US | USD | 已部署 |

**待接入数据源**:
1. Shopify Payouts → `shopify_payouts (payout_id, market, payout_date, gross, fees, net)`
2. Shopify Orders → `shopify_orders (order_no, market, order_created_at, total_price, refunds)`
3. Adjustments / Refunds (随 orders)

**颗粒度** (与 Shopee 板块一致): 按【订单成立时间】月份切分 / NST 6 列 / 周月切换 / 3 市场可独立可汇总

**下一步**: 1) 接入方式 (CSV vs API) 2) schema 设计 3) ingester 4) 本页加 Shopify Tab
"""))
    st.stop()


# ============================================================
# 常量
# ============================================================
NST_MAX_ROWS = 899  # NST 单文件上限

# 账单金额 = 新版 Shopee Income 表【退款金额】右侧到【拨款金额】左侧的费用列
# (新版 Excel 把 买家支付运费/第三方物流费/Shopee运费回扣 移到了退款金额左侧, 不再算入账单金额)
BILL_FEE_COLS = [
    # 退款金额右侧 (= 账单金额扣款项目, 与 Boss 截图 K 列起对齐):
    "shopee_rebate",                                       # Shopee 回扣金额
    "seller_voucher", "seller_voucher_jv",                 # 卖家优惠券折扣 (含合资)
    "seller_shopee_coin", "seller_shopee_coin_jv",         # 卖家 Shopee 币回扣 (含合资)
    "return_shipping", "return_to_seller_ship",            # 退货运费
    "shipping_insurance_save",                             # 运费险节省
    "affiliate_commission",                                # 联盟营销佣金
    "commission",                                          # 佣金
    "fbs_overseas_fail", "fbs_overseas_return",            # 海外免退服务费
    "service_fee",                                         # 服务费
    "shipping_insurance_fee",                              # 运费险服务费
    "transaction_fee",                                     # 交易费 (旧名: 交易手续费)
    "fbs_fee",                                             # FBS Fee
]

# 拨款 Summary 项目: 与 Shopee Income 表列顺序对齐 (Boss 截图 G-P 等)
# 顺序: 商品原价 → 商品折扣 → 运费类 (J 左侧) → 退款金额 → 退款右侧扣款 → 拨款金额
SUMMARY_FEE_COLS = [
    # 退款金额左侧
    ("商品原价",          ["gross_price"],                 "income"),
    ("商品折扣",          ["product_discount"],            "income"),
    ("买家支付运费",      ["buyer_shipping"],              "income"),
    ("第三方物流费",      ["seller_shipping"],             "income"),
    ("Shopee 运费回扣",   ["shopee_shipping_subsidy"],     "income"),
    ("退款金额",          ["refund_amount"],               "refund"),

    # 退款金额右侧 (账单金额组成项)
    ("卖家优惠券折扣",    ["seller_voucher", "seller_voucher_jv"],            "bill"),
    ("卖家 Shopee 币回扣", ["seller_shopee_coin", "seller_shopee_coin_jv"],   "bill"),
    ("Shopee 回扣金额",   ["shopee_rebate"],                                  "bill"),
    ("佣金",             ["commission", "affiliate_commission"],             "bill"),
    ("服务费",           ["service_fee"],                                    "bill"),
    ("交易费",           ["transaction_fee"],                                "bill"),
    ("退货运费",         ["return_shipping", "return_to_seller_ship"],       "bill"),
    ("运费险",           ["shipping_insurance_save", "shipping_insurance_fee"], "bill"),
    ("FBS / 海外免退",    ["fbs_fee", "fbs_overseas_fail", "fbs_overseas_return"], "bill"),

    # 截图中存在但 ingester 未捕获 (待补)
    ("AMS 佣金",         [],  "missing"),
    ("线下调整金额",      [],  "missing"),

    # 拨款金额 (右侧终点)
    ("拨款金额",         ["payout_amount"],                                  "payout"),
]


# ============================================================
# 工具
# ============================================================
def _df(sql: str, params=None) -> pd.DataFrame:
    from shared.db_helpers import df
    return df(conn, sql, params)


def _country_from_shop(shop: str | None) -> str:
    if not shop:
        return "?"
    s = str(shop).lower()
    m = re.search(r"\.([a-z]{2,3})\b", s)
    if m:
        return m.group(1).upper()
    s_orig = str(shop)
    if "日本直" in s_orig or "直郵" in s_orig or "旗艦" in s_orig or "台灣" in s_orig:
        return "TW"
    return "OTHER"


def _country_from_seller(seller: str | None) -> str:
    if not seller:
        return "?"
    s = str(seller).lower()
    m = re.search(r"\.([a-z]{2,3})$", s)
    return m.group(1).upper() if m else s.upper()


def _market_from_country(country: str | None) -> str:
    """市场维度: country → 高级地区
    - TW/SG/MY/PH/ID/VN/TH → 东南亚
    - KR → 韩国 (Coupang 接入后启用)
    - 其他 → 其他
    """
    c = (country or "").upper()
    if c in {"TW", "SG", "MY", "PH", "ID", "VN", "TH", "BR"}:
        return t("东南亚")
    if c == "KR":
        return t("韩国")
    return t("其他")


def _to_yyyy_mm(dt) -> str | None:
    if dt is None or pd.isna(dt):
        return None
    try:
        return pd.to_datetime(dt).strftime("%Y-%m")
    except Exception:
        return None


def _to_iso_week(dt) -> str | None:
    """ISO 周表示 'YYYY-Www' (周一为周首日)."""
    if dt is None or pd.isna(dt):
        return None
    try:
        d = pd.to_datetime(dt)
        iso = d.isocalendar()
        return f"{int(iso.year)}-W{int(iso.week):02d}"
    except Exception:
        return None


def _to_yyyy_mm_dd_slash(dt) -> str:
    """NST 拨款完成日期格式: YYYY/MM/DD."""
    if dt is None or pd.isna(dt):
        return ""
    try:
        return pd.to_datetime(dt).strftime("%Y/%m/%d")
    except Exception:
        return ""


def _safe_order_no(x) -> str:
    """文本保护订单号 (避免科学计数法)."""
    if pd.isna(x):
        return ""
    s = str(x).strip()
    if not s:
        return ""
    if re.search(r"e[+-]?\d+", s, flags=re.I):
        try:
            from decimal import Decimal
            s = format(Decimal(s), "f")
        except Exception:
            pass
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _fmt_money(x):
    try:
        return f"{int(round(float(x))):,}"
    except Exception:
        return x


# ============================================================
# 懒加载：先只查筛选选项的 distinct，不读全表数据
# 用户在筛选 expander 内点【🔍 查询】后，才 SELECT 全表 + 渲染
# ============================================================
@st.cache_data(ttl=120, show_spinner=False)
def _load_filter_options() -> dict:
    """轻量 distinct 查询，给筛选下拉填充选项。"""
    def _list(sql: str) -> list:
        return [r[0] for r in conn.execute(sql).fetchall() if r[0] is not None]
    # income 表派生列在 ingest 时不一定存在，用 strftime/原始字段抽取
    months = _list(
        "SELECT DISTINCT substr(order_created_at, 1, 7) AS m "
        "FROM shopee_income_lines "
        "WHERE order_created_at IS NOT NULL ORDER BY m DESC"
    )
    countries = _list(
        "SELECT DISTINCT seller_account FROM shopee_income_lines "
        "WHERE seller_account IS NOT NULL"
    )
    sellers = list(countries)
    return {
        "months": months, "countries": countries, "sellers": sellers,
    }

# 检查是否有数据（轻量 COUNT，不读全表）
_has_income = conn.execute("SELECT COUNT(*) AS c FROM shopee_income_lines").fetchone()["c"] > 0
_has_orders = conn.execute("SELECT COUNT(*) AS c FROM shopee_orders_raw").fetchone()["c"] > 0

if not _has_income and not _has_orders:
    st.warning(t(
        "⚠️ 数据为空。请到「⚙️ 数据导入与设置」上传:\n"
        "1) 订单导出-*.xlsx\n"
        "2) *.income.已拨款.*.xlsx"
    ))
    st.stop()

filter_opts = _load_filter_options()

# 数值化用列名（数据加载后才使用）
fee_cols_all = [
    "gross_price", "product_discount", "refund_amount",
    *BILL_FEE_COLS, "payout_amount",
]


def _process_income(df_income):
    """数值化 + 派生列（NST 切分 / country / market / nst 三金额 / JPY 系数）。"""
    if df_income.empty:
        return df_income
    for c in fee_cols_all:
        if c in df_income.columns:
            df_income[c] = pd.to_numeric(df_income[c], errors="coerce").fillna(0.0)

    # NST 颗粒度: 按【订单成立时间】月份切分
    df_income["order_create_month"] = df_income["order_created_at"].apply(_to_yyyy_mm)
    df_income["order_create_week"] = df_income["order_created_at"].apply(_to_iso_week)
    df_income["payout_month"] = df_income["payout_date"].apply(_to_yyyy_mm)
    df_income["payout_week"] = df_income["payout_date"].apply(_to_iso_week)

    df_income["country"] = df_income["seller_account"].apply(_country_from_seller)
    df_income["market"] = df_income["country"].apply(_market_from_country)
    if "platform" not in df_income.columns:
        df_income["platform"] = "Shopee"

    # NST 三个金额
    df_income["nst_payment"] = (
        df_income.get("gross_price", 0.0)
        + df_income.get("product_discount", 0.0)
        + df_income.get("refund_amount", 0.0)
    ).round(2)
    df_income["nst_refund"] = df_income.get("refund_amount", 0.0).round(2)
    bill_present = [c for c in BILL_FEE_COLS if c in df_income.columns]
    if bill_present:
        df_income["nst_bill"] = df_income[bill_present].sum(axis=1).round(2)
    else:
        df_income["nst_bill"] = 0.0

    # JPY 换算系数
    df_income["_jpy_rate"] = df_income["country"].map(FX_TO_JPY).fillna(1.0)
    return df_income


def _process_orders(df_orders, df_income):
    """加 country/market/platform，必要时与 income 合并 month/week。"""
    if df_orders.empty:
        return df_orders
    df_orders["country"] = df_orders["shop_name"].apply(_country_from_shop)
    df_orders["market"] = df_orders["country"].apply(_market_from_country)
    if "platform" not in df_orders.columns:
        df_orders["platform"] = "Shopee"
    if not df_income.empty:
        df_orders = df_orders.merge(
            df_income[[
                "order_no",
                "order_create_month", "order_create_week",
                "payout_month", "payout_week",
                "market",
            ]].drop_duplicates("order_no"),
            on="order_no", how="left",
            suffixes=("", "_income"),
        )
        if "market_income" in df_orders.columns:
            df_orders["market"] = df_orders["market_income"].fillna(df_orders["market"])
            df_orders.drop(columns=["market_income"], inplace=True)
    return df_orders


# ============================================================
# 期间筛选 (粒度切换: 按周 / 按月, 时间锚点 = 订单成立时间)
# ============================================================
GRAN_LABEL_TO_COL = {
    t("按周"): "order_create_week",
    t("按月"): "order_create_month",
}

# 筛选 expander：选项来自轻量 distinct（filter_opts），不读全表
# 用户调整后必须点【🔍 查询】才会真正 SELECT 全表 + 渲染
with st.expander(t("🔍 筛选条件"), expanded=True):
    c_gran, c_period, c_market, c_country, c_seller = st.columns([1, 1.5, 1, 1, 1.2])
    with c_gran:
        gran_label = st.radio(
            t("粒度"), list(GRAN_LABEL_TO_COL.keys()), horizontal=False,
            key="fin_gran",
        )
        gran_col = GRAN_LABEL_TO_COL[gran_label]
    with c_period:
        sel_period = st.selectbox(
            t("订单成立月份"), [t("全部")] + filter_opts["months"],
            key="fin_period",
        )
    with c_market:
        sel_market = st.selectbox(
            t("市场"),
            [t("全部"), t("东南亚"), t("韩国"), t("其他")],
            key="fin_market",
        )
    with c_country:
        _countries = sorted({_country_from_seller(s) for s in filter_opts["sellers"]})
        sel_country = st.selectbox(
            t("国家"), [t("全部")] + _countries, key="fin_country",
        )
    with c_seller:
        sel_seller = st.selectbox(
            t("店铺账号"), [t("全部")] + filter_opts["sellers"],
            key="fin_seller",
        )

    bcol1, bcol2, _ = st.columns([1, 1, 4])
    with bcol1:
        if st.button(t("🔍 查询"), type="primary", use_container_width=True):
            st.session_state["fin_run"] = True
            st.session_state["fin_filter_snapshot"] = (
                gran_label, sel_period, sel_market, sel_country, sel_seller,
            )
    with bcol2:
        if st.button(t("🔄 重置"), use_container_width=True):
            for k in ["fin_run", "fin_filter_snapshot"]:
                st.session_state.pop(k, None)
            st.rerun()

# 未点查询 → 不读全表，直接 stop
if not st.session_state.get("fin_run"):
    st.info(t("👆 选好筛选条件后点【🔍 查询】加载数据"))
    st.stop()

# 筛选条件已变更但没重新查询 → 提醒
_curr_snap = (gran_label, sel_period, sel_market, sel_country, sel_seller)
if st.session_state.get("fin_filter_snapshot") != _curr_snap:
    st.warning(t("⚠️ 筛选条件已变更，请重新点【🔍 查询】"))
    st.stop()

# ============================================================
# 数据加载（此处才 SELECT 全表 + 内存处理 + 应用筛选）
# ============================================================
with st.spinner(t("📊 正在加载数据...")):
    df_income = _df("SELECT * FROM shopee_income_lines")
    df_orders = _df("SELECT * FROM shopee_orders_raw")
    df_income = _process_income(df_income)
    df_orders = _process_orders(df_orders, df_income)

# 应用筛选 (income 表)
if not df_income.empty:
    if sel_period != t("全部"):
        df_income = df_income[df_income["order_create_month"] == sel_period]
    if sel_market != t("全部") and "market" in df_income.columns:
        df_income = df_income[df_income["market"] == sel_market]
    if sel_country != t("全部"):
        df_income = df_income[df_income["country"] == sel_country]
    if sel_seller != t("全部") and "seller_account" in df_income.columns:
        df_income = df_income[df_income["seller_account"] == sel_seller]

# 应用筛选 (orders 表)
if not df_orders.empty:
    if sel_period != t("全部") and "order_create_month" in df_orders.columns:
        df_orders = df_orders[
            (df_orders["order_create_month"] == sel_period)
            | df_orders["order_create_month"].isna()
        ]
    if sel_market != t("全部") and "market" in df_orders.columns:
        df_orders = df_orders[df_orders["market"] == sel_market]
    if sel_country != t("全部") and "country" in df_orders.columns:
        df_orders = df_orders[df_orders["country"] == sel_country]


# ============================================================
# JPY 副本 (用于跨国汇总)
# ============================================================
if not df_income.empty:
    df_income_jpy = df_income.copy()
    for c in fee_cols_all + ["nst_payment", "nst_refund", "nst_bill"]:
        if c in df_income_jpy.columns:
            df_income_jpy[c] = df_income_jpy[c] * df_income_jpy["_jpy_rate"]
else:
    df_income_jpy = df_income


# ============================================================
# KPI (NST 4 指标 + 实拨)
# ============================================================
n_orders = int(df_income["order_no"].nunique()) if not df_income.empty else 0
n_periods = int(df_income[gran_col].nunique()) if not df_income.empty else 0

if not df_income_jpy.empty:
    sum_payment = float(df_income_jpy["nst_payment"].sum())
    sum_refund = float(df_income_jpy["nst_refund"].sum())
    sum_bill = float(df_income_jpy["nst_bill"].sum())
    sum_payout = float(df_income_jpy.get("payout_amount", pd.Series([0.0])).sum())
else:
    sum_payment = sum_refund = sum_bill = sum_payout = 0.0

period_unit_label = t("覆盖周数") if gran_col == "order_create_week" else t("覆盖月份")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric(period_unit_label, f"{n_periods:,}")
c2.metric(t("订单数"), f"{n_orders:,}")
c3.metric(t("付款金额合计 (¥)"), f"¥{sum_payment:,.0f}")
c4.metric(t("账单金额合计 (¥)"), f"¥{sum_bill:,.0f}")
c5.metric(t("拨款金额合计 (¥)"), f"¥{sum_payout:,.0f}")

period_axis_label = t("订单成立周") if gran_col == "order_create_week" else t("订单成立月份")

with st.expander(t("💴 颗粒度 / 汇率 说明"), expanded=False):
    st.markdown(t(
        f"- 颗粒度: **{period_axis_label} × 店铺账号** (NST 上传切分仍以月份为基准)\n"
        f"- 跨国汇总按 country × 公司固定汇率换算为日元:\n"
        f"  PHP=2.4 · TWD=4.57 · MYR=36.48 · SGD=113.44 · USD=145"
    ))
st.divider()


# ============================================================
# Tabs
# ============================================================
_period_tab_unit = t("周") if gran_col == "order_create_week" else t("月")

(
    tab_nst, tab_market_period, tab_country, tab_shop, tab_platform,
    tab_raw_i, tab_raw_o,
) = st.tabs([
    t("📤 NST 上传明细 (6 列)"),
    f"🌍 {_period_tab_unit} × {t('市场')} {t('汇总')}",
    f"📅 {_period_tab_unit} × {t('国家')} {t('汇总')}",
    f"🏪 {_period_tab_unit} × {t('店铺')} {t('汇总')}",
    f"📱 {_period_tab_unit} × {t('平台')} {t('汇总')}",
    t("💰 拨款明细 (原始)"),
    t("📦 订单导出 (原始)"),
])


# ----- Tab 1: NST 6 列上传明细 -----
with tab_nst:
    if df_income.empty:
        st.info(t("无数据。"))
    else:
        st.caption(t(
            "🎯 与 NST 上传文件 1:1 对齐 · 单文件 ≤899 行 · "
            "金额保留 2 位小数 · 原币种 (未换汇)"
        ))

        # 按 seller_account + order_create_month 分组, 模拟 NST 文件切分
        nst_df = df_income.copy()
        nst_df = nst_df[nst_df["order_create_month"].notna()]
        nst_df["编号"] = pd.to_numeric(nst_df.get("seq"), errors="coerce").fillna(0).astype(int)
        nst_df["订单编号"] = nst_df["order_no"].apply(_safe_order_no)
        nst_df["拨款完成日期"] = nst_df["payout_date"].apply(_to_yyyy_mm_dd_slash)
        nst_df["付款金额"] = nst_df["nst_payment"]
        nst_df["退款金额"] = nst_df["nst_refund"]
        nst_df["账单金额"] = nst_df["nst_bill"]

        # 按 seller × month 分组, 显示文件清单
        groups = nst_df.groupby(
            ["seller_account", "order_create_month"], dropna=False, as_index=False,
        )
        file_rows = []
        for (seller, month), g in groups:
            n_rows = len(g)
            n_files = (n_rows + NST_MAX_ROWS - 1) // NST_MAX_ROWS if n_rows else 0
            month_label_n = int(month.split("-")[1]) if month else 0
            file_rows.append({
                t("店铺账号"): seller or "?",
                t("订单成立月份"): month or "?",
                t("月标签"): f"{month_label_n}月" if month_label_n else "",
                t("行数"): n_rows,
                t("文件数 (899/份)"): n_files,
                t("付款合计"): round(float(g["付款金额"].sum()), 2),
                t("退款合计"): round(float(g["退款金额"].sum()), 2),
                t("账单合计"): round(float(g["账单金额"].sum()), 2),
            })
        files_df = pd.DataFrame(file_rows).sort_values(
            [t("店铺账号"), t("订单成立月份")],
            ascending=[True, False],
        )

        st.subheader(t("📂 NST 文件清单 (按店铺 × 月份)"))
        st.dataframe(files_df, use_container_width=True, hide_index=True, height=280)

        # 拨款 Summary (与 Shopee Income 表 Summary 对齐 · 列顺序 = G→P→拨款金额)
        st.subheader(t("📊 拨款 Summary (按订单成立月份)"))
        st.caption(t(
            "🎯 列顺序与 Shopee Income 表 Summary 一致 (商品原价 → 运费类 → 退款金额 → 扣款项 → 拨款金额) · "
            "原币种 (未换汇) · ⚠️ AMS 佣金 / 线下调整金额 当前 ingester 未捕获 (列值显示 N/A)"
        ))

        # 选择展示视角: 月份 × 国家 / 月份 × 店铺账号 / 仅按月份
        view_mode = st.radio(
            t("Summary 维度"),
            [t("订单成立月份"), t("月份 × 国家"), t("月份 × 店铺账号")],
            horizontal=True,
            key="summary_view_mode",
        )
        if view_mode == t("月份 × 国家"):
            group_keys = ["order_create_month", "country"]
            label_keys = [t("订单成立月份"), t("国家")]
        elif view_mode == t("月份 × 店铺账号"):
            group_keys = ["order_create_month", "seller_account"]
            label_keys = [t("订单成立月份"), t("店铺账号")]
        else:
            group_keys = ["order_create_month"]
            label_keys = [t("订单成立月份")]

        summary_rows = []
        for keys, g in nst_df.groupby(group_keys, sort=True, dropna=False):
            if not isinstance(keys, tuple):
                keys = (keys,)
            row = dict(zip(label_keys, keys))
            row[t("订单数")] = int(g["order_no"].nunique())
            for label_zh, src_cols, kind in SUMMARY_FEE_COLS:
                if kind == "missing":
                    row[t(label_zh)] = "N/A"
                    continue
                present = [c for c in src_cols if c in g.columns]
                if not present:
                    row[t(label_zh)] = 0.0
                else:
                    row[t(label_zh)] = round(float(g[present].sum().sum()), 2)
            row[t("账单金额合计")] = round(float(g["账单金额"].sum()), 2)
            summary_rows.append(row)
        summary_df = pd.DataFrame(summary_rows)
        if t("订单成立月份") in summary_df.columns:
            summary_df = summary_df.sort_values(label_keys, ascending=[False] * len(label_keys))

        money_cols_s = [
            t(label_zh) for label_zh, _, kind in SUMMARY_FEE_COLS if kind != "missing"
        ] + [t("账单金额合计")]
        for c in money_cols_s:
            if c in summary_df.columns:
                summary_df[c] = summary_df[c].map(_fmt_money)
        st.dataframe(summary_df, use_container_width=True, hide_index=True, height=320)
        st.download_button(
            t("📥 拨款 Summary CSV"),
            data=summary_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="shopee_payout_summary.csv",
            mime="text/csv",
            key="dl_payout_summary",
        )

        st.subheader(t("📋 NST 6 列明细"))
        nst_6cols = nst_df[[
            "seller_account", "order_create_month",
            "编号", "订单编号", "拨款完成日期",
            "付款金额", "退款金额", "账单金额",
        ]].rename(columns={
            "seller_account": t("店铺账号"),
            "order_create_month": t("订单成立月份"),
        }).sort_values(
            [t("店铺账号"), t("订单成立月份"), "编号"],
            ascending=[True, False, True],
        )

        # 密度 + 列显示控件 (Phase 2A)
        _dctl1, _dctl2 = st.columns([1, 3])
        with _dctl1:
            _density = st.radio(
                t("密度"),
                [t("紧凑"), t("标准"), t("宽松")],
                horizontal=True,
                index=1,
                key=f"density_{__file__}",
                label_visibility="collapsed",
            )
        _density_class = {
            t("紧凑"): "density-compact",
            t("标准"): "",
            t("宽松"): "density-comfy",
        }.get(_density, "")

        with st.expander(t("⚙️ 显示列设置")):
            _all_cols = nst_6cols.columns.tolist()
            _picked_cols = st.multiselect(
                t("选择展示列"), _all_cols, default=_all_cols,
                key=f"colpick_{__file__}",
            )
        nst_6cols_render = nst_6cols[_picked_cols] if _picked_cols else nst_6cols

        st.markdown(f'<div class="{_density_class}">', unsafe_allow_html=True)
        st.dataframe(nst_6cols_render, use_container_width=True, hide_index=True, height=420)
        st.markdown('</div>', unsafe_allow_html=True)
        st.caption(t(f"共 {len(nst_6cols):,} 行"))

        # 单文件 CSV 下载
        single_csv_cols = ["编号", "订单编号", "拨款完成日期", "付款金额", "退款金额", "账单金额"]
        st.download_button(
            t("📥 全部明细 CSV (NST 6 列, 未拆分)"),
            data=nst_6cols[single_csv_cols].to_csv(index=False).encode("utf-8-sig"),
            file_name="nst_shopee_all.csv",
            mime="text/csv",
            key="dl_nst_all",
        )

        # ZIP 打包: 模拟 NST 文件切分
        st.markdown(t("##### 📦 ZIP 打包下载 (按 NST 规则切分)"))
        if st.button(t("生成 ZIP (店铺 × 月 × 899行)"), key="btn_nst_zip"):
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for (seller, month), g in groups:
                    if not seller or not month:
                        continue
                    g = g.sort_values("编号")
                    month_label_n = int(month.split("-")[1])
                    base_name = f"shopee-{seller}-{month}-{month_label_n}月"

                    rows = g[single_csv_cols].copy()
                    if len(rows) <= NST_MAX_ROWS:
                        zf.writestr(
                            f"{base_name}.csv",
                            rows.to_csv(index=False).encode("utf-8-sig"),
                        )
                    else:
                        n_parts = (len(rows) + NST_MAX_ROWS - 1) // NST_MAX_ROWS
                        for i in range(n_parts):
                            part = rows.iloc[i * NST_MAX_ROWS:(i + 1) * NST_MAX_ROWS]
                            zf.writestr(
                                f"{base_name}({i + 1}).csv",
                                part.to_csv(index=False).encode("utf-8-sig"),
                            )
            zip_buf.seek(0)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.download_button(
                t("⬇️ 下载 ZIP"),
                data=zip_buf.getvalue(),
                file_name=f"nst_shopee_{ts}.zip",
                mime="application/zip",
                key="dl_nst_zip",
            )


# ----- 通用聚合 -----
def _agg_by(df, *dims):
    return df.groupby(list(dims), as_index=False).agg(
        n_orders=("order_no", "nunique"),
        gross_price=("gross_price", "sum"),
        product_discount=("product_discount", "sum"),
        refund_amount=("refund_amount", "sum"),
        nst_payment=("nst_payment", "sum"),
        nst_refund=("nst_refund", "sum"),
        nst_bill=("nst_bill", "sum"),
        payout_amount=("payout_amount", "sum"),
    )


def _format_agg(agg, dim_cols, dim_labels):
    show = agg.copy()
    rename_map = dict(zip(dim_cols, dim_labels))
    rename_map.update({
        "n_orders": t("订单数"),
        "gross_price": t("商品原价"),
        "product_discount": t("商品折扣"),
        "refund_amount": t("退款"),
        "nst_payment": t("付款金额"),
        "nst_refund": t("退款金额"),
        "nst_bill": t("账单金额"),
        "payout_amount": t("拨款金额"),
    })
    show = show.rename(columns=rename_map)
    money_cols = [
        t("商品原价"), t("商品折扣"), t("退款"),
        t("付款金额"), t("退款金额"), t("账单金额"), t("拨款金额"),
    ]
    for col in money_cols:
        if col in show.columns:
            show[col] = show[col].map(_fmt_money)
    return show


# 选定粒度的 axis 标签 (用于 Tab 2-4 的列名)
period_axis_short = t("周") if gran_col == "order_create_week" else t("月份")


# ----- Tab 2: 期间 × 市场 -----
with tab_market_period:
    if df_income_jpy.empty:
        st.info(t("拨款明细未上传, 无法汇总。"))
    else:
        agg = _agg_by(df_income_jpy, gran_col, "market")
        show = _format_agg(
            agg,
            [gran_col, "market"],
            [period_axis_label, t("市场")],
        )
        show = show.sort_values(
            [period_axis_label, t("市场")],
            ascending=[False, True],
        )
        st.dataframe(show, use_container_width=True, hide_index=True, height=460)
        st.caption(t(f"共 {len(agg):,} 行"))
        st.download_button(
            f"📥 {period_axis_short}×{t('市场')} CSV",
            data=agg.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"nst_shopee_{gran_col}_market.csv",
            mime="text/csv",
            key="dl_period_market",
        )


# ----- Tab 3: 期间 × 国家 -----
with tab_country:
    if df_income_jpy.empty:
        st.info(t("拨款明细未上传, 无法汇总。"))
    else:
        agg = _agg_by(df_income_jpy, gran_col, "country")
        show = _format_agg(
            agg,
            [gran_col, "country"],
            [period_axis_label, t("国家")],
        )
        show = show.sort_values(
            [period_axis_label, t("国家")],
            ascending=[False, True],
        )
        st.dataframe(show, use_container_width=True, hide_index=True, height=460)
        st.caption(t(f"共 {len(agg):,} 行"))
        st.download_button(
            f"📥 {period_axis_short}×{t('国家')} CSV",
            data=agg.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"nst_shopee_{gran_col}_country.csv",
            mime="text/csv",
            key="dl_period_country",
        )


# ----- Tab 3: 期间 × 店铺 -----
with tab_shop:
    if df_income_jpy.empty or df_orders.empty:
        st.info(t("订单导出 + 拨款明细 都需上传 (店铺信息来自订单导出)。"))
    else:
        joined = df_income_jpy.merge(
            df_orders[["order_no", "shop_name"]].drop_duplicates("order_no"),
            on="order_no", how="left",
        )
        joined["shop_name"] = joined["shop_name"].fillna(t("(无店铺信息)"))
        agg = _agg_by(joined, gran_col, "shop_name")
        show = _format_agg(
            agg,
            [gran_col, "shop_name"],
            [period_axis_label, t("店铺")],
        )
        show = show.sort_values(
            [period_axis_label, t("拨款金额")],
            ascending=[False, False],
        )
        st.dataframe(show, use_container_width=True, hide_index=True, height=460)
        st.caption(t(f"共 {len(agg):,} 行"))
        st.download_button(
            f"📥 {period_axis_short}×{t('店铺')} CSV",
            data=agg.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"nst_shopee_{gran_col}_shop.csv",
            mime="text/csv",
            key="dl_period_shop",
        )


# ----- Tab 5: 期间 × 平台 -----
with tab_platform:
    if df_income_jpy.empty:
        st.info(t("拨款明细未上传, 无法汇总。"))
    else:
        agg = _agg_by(df_income_jpy, gran_col, "platform")
        show = _format_agg(
            agg,
            [gran_col, "platform"],
            [period_axis_label, t("平台")],
        )
        show = show.sort_values(
            [period_axis_label, t("平台")],
            ascending=[False, True],
        )
        st.dataframe(show, use_container_width=True, hide_index=True, height=460)
        st.caption(t(f"共 {len(agg):,} 行"))
        st.download_button(
            f"📥 {period_axis_short}×{t('平台')} CSV",
            data=agg.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"nst_shopee_{gran_col}_platform.csv",
            mime="text/csv",
            key="dl_period_platform",
        )


# ----- Tab 5: 拨款原始 -----
with tab_raw_i:
    if df_income_jpy.empty:
        st.info(t("拨款明细未上传。"))
    else:
        st.caption(t(
            "💴 金额已按 country × 公司固定汇率换算为日元。"
        ))
        cols = [
            "order_create_week", "order_create_month",
            "payout_week", "payout_month",
            "market", "country", "_jpy_rate",
            "seller_account", "order_no", "buyer_account",
            "order_created_at", "payout_date",
            "gross_price", "product_discount", "refund_amount",
            "nst_payment", "nst_refund", "nst_bill",
            "commission", "service_fee", "transaction_fee",
            "buyer_shipping", "seller_shipping",
            "payout_amount",
        ]
        cols = [c for c in cols if c in df_income_jpy.columns]
        st.dataframe(
            localize_df(df_income_jpy[cols]),
            use_container_width=True, hide_index=True, height=460,
        )
        st.caption(t(f"共 {len(df_income_jpy):,} 行 (JPY)"))
        st.download_button(
            t("📥 拨款明细 CSV (JPY)"),
            data=df_income_jpy[cols].to_csv(index=False).encode("utf-8-sig"),
            file_name="shopee_income_lines_jpy.csv",
            mime="text/csv",
            key="dl_i",
        )


# ----- Tab 6: 订单原始 -----
with tab_raw_o:
    if df_orders.empty:
        st.info(t("订单导出.xlsx 未上传。"))
    else:
        cols = [
            "order_no", "platform", "market", "shop_name", "country",
            "order_create_week", "order_create_month",
            "currency", "local_sku", "unit_price", "ship_qty", "payment_amount",
        ]
        cols = [c for c in cols if c in df_orders.columns]
        st.dataframe(
            localize_df(df_orders[cols]),
            use_container_width=True, hide_index=True, height=460,
        )
        st.caption(t(f"共 {len(df_orders):,} 条订单"))
        st.download_button(
            t("📥 订单导出 CSV"),
            data=df_orders[cols].to_csv(index=False).encode("utf-8-sig"),
            file_name="shopee_orders_raw.csv",
            mime="text/csv",
            key="dl_o",
        )
