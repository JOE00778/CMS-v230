"""一元管理系统V2.7 · Streamlit 主入口（仪表盘）。

UI Phase 1 mockup 落地（docs/14-ui-redesign-mockup.html）：
- 顶部 inject_theme() 全局 CSS
- 业务大盘 4 KPI（商品 SKU / 在库金额 / 本月销售 / 毛利率）
- 风险预警 5 KPI（断货 / 正常 / 压库存 / 数据不足 / 入荷困難）
- 月度销售趋势 + 市场分布 charts
- TOP 10 当月销量表
- Sidebar 快捷栏 5 个 page_link
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from shared.db import get_connection
from shared.i18n import lang_selector, t
from shared.kpi_history import get_delta, get_history, take_snapshot
from shared.supabase_client import is_configured

# 页面配置
st.set_page_config(
    page_title=t("一元管理系统V2.7"),
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

from shared.auth import require_password, show_role_badge, APP_VERSION
from shared.theme import inject_theme

require_password()
inject_theme()
show_role_badge()

# 全局语言切换器（侧边栏顶部）
lang_selector()

conn = get_connection()

# 当月 KPI snapshot · 每次访问 home 自动 UPSERT
try:
    take_snapshot(conn)
except Exception:
    pass

# 当月 vs 上月 delta（不足两月时返回 {}）
try:
    _kpi_delta = get_delta(conn) or {}
except Exception:
    _kpi_delta = {}


# Sidebar 导航统一由 lang_selector() 渲染（_NAV_GROUPS 唯一来源），
# 不在主入口再追加快捷栏，避免与分组导航重复


# ============================================================
# 顶部标题（mockup 简化版）
# ============================================================
now = datetime.now()
month_label = f"{now.year}-{now.month:02d}"
st.title(t("📊 仪表盘"))
st.caption(
    f"{t('最近更新')} {now.strftime('%Y-%m-%d %H:%M')} · "
    f"{month_label} {t('月数据')} · "
    f"build `{APP_VERSION}`"
)

# Supabase 连接状态（保留, 但折叠到一行 caption）
if is_configured():
    st.caption(f"🟢 {t('Supabase 已连接')}")
else:
    st.caption(f"🟡 {t('Supabase 未配置 —— 部分模块降级为本地数据')}")


# ============================================================
# SQL helpers · 全部 try/except 兜底, 不挂 page
# ============================================================
def _safe_scalar(sql: str, default=0):
    try:
        row = conn.execute(sql).fetchone()
        if row is None:
            return default
        try:
            v = row[0]
        except Exception:
            try:
                v = list(dict(row).values())[0]
            except Exception:
                return default
        return v if v is not None else default
    except Exception:
        return default


def _safe_df(sql: str) -> pd.DataFrame:
    try:
        rs = conn.execute(sql).fetchall()
        return pd.DataFrame([dict(r) for r in rs])
    except Exception:
        return pd.DataFrame()


# ============================================================
# 业务大盘 4 KPI
# ============================================================
# PPVI P1 · 去 emoji 装饰前缀（结构主义：颜色/装饰仅用于编码信息）
st.markdown(f"##### {t('业务大盘')}")

# 1) 商品 SKU 数（nst.item_master_raw · 旧 item_v2 置換）
sku_total = _safe_scalar(
    "SELECT COUNT(*) FROM nst.item_master_raw WHERE jan IS NOT NULL", default=0)

# 2) 在库金额（nst.inventory_snapshot 手持数 × 定義原価 · JD主仓最新快照 · 对齐库存监控页06）
inv_amount = _safe_scalar(
    """
    SELECT COALESCE(SUM(inv.qty_on_hand * COALESCE(im.cost_estimate, 0)), 0)
    FROM nst.inventory_snapshot inv
    JOIN nst.item_master_raw im ON im.internal_id = inv.item_internal_id
    WHERE inv.snapshot_date = (SELECT MAX(snapshot_date) FROM nst.inventory_snapshot)
      AND inv.warehouse = 'JD-物流-千葉'
    """,
    default=0,
)

# 本月 YYYY-MM（_ym 给 KPI #3/#4 的 nst.sales_daily · period_start 保留给下方 TOP10/趋势区）
period_start = f"{now.year}-{now.month:02d}-01"
_ym = f"{now.year}-{now.month:02d}"

# 3) 本月营业额（nst.sales_daily 当月 SUM(revenue) · 对齐店铺毛利页05）
sales_amount = _safe_scalar(
    f"""
    SELECT COALESCE(SUM(revenue), 0) FROM nst.sales_daily
    WHERE to_char(sale_date, 'YYYY-MM') = '{_ym}'
    """,
    default=0,
)

# 4) 毛利率（nst.sales_daily 当月 gross_profit / revenue）
gp_row = _safe_df(
    f"""
    SELECT COALESCE(SUM(gross_profit), 0) AS gp,
           COALESCE(SUM(revenue), 0)      AS rev
    FROM nst.sales_daily
    WHERE to_char(sale_date, 'YYYY-MM') = '{_ym}'
    """
)
gp_rate = 0.0
try:
    if not gp_row.empty and float(gp_row.iloc[0]["rev"]) > 0:
        gp_rate = float(gp_row.iloc[0]["gp"]) / float(gp_row.iloc[0]["rev"])
except Exception:
    gp_rate = 0.0


def _fmt_jpy_short(amt) -> str:
    try:
        v = float(amt or 0)
    except (TypeError, ValueError):
        return "¥0"
    if v >= 1_000_000:
        return f"¥{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"¥{v / 1_000:.1f}K"
    return f"¥{v:,.0f}"


# delta 字符串构造（无历史→None；有历史→真实数值）
def _fmt_delta_int(v, suffix=""):
    if v is None:
        return None
    sign = "+" if v >= 0 else ""
    return f"{sign}{int(v):,}{suffix}"


def _fmt_delta_pct(v, suffix="%"):
    if v is None:
        return None
    return f"{v:+.1f}{suffix}"


_sku_delta_str = (
    f"{_fmt_delta_int(_kpi_delta['sku_delta'])} {t('vs 上月')}"
    if _kpi_delta.get("sku_delta") is not None else None
)
_stock_delta_str = (
    _fmt_delta_pct(_kpi_delta["stock_delta_pct"])
    if _kpi_delta.get("stock_delta_pct") is not None else None
)
_revenue_delta_str = (
    _fmt_delta_pct(_kpi_delta["revenue_delta_pct"])
    if _kpi_delta.get("revenue_delta_pct") is not None else None
)
_margin_delta_str = (
    _fmt_delta_pct(_kpi_delta["margin_delta_pp"], suffix="pp")
    if _kpi_delta.get("margin_delta_pp") is not None else None
)


def _spark(field: str, n: int = 6):
    """读取近 n 月历史并画 mini area chart. 数据不足→展示 fallback mock."""
    try:
        hist = get_history(field, n=n, conn=conn)
    except Exception:
        hist = pd.DataFrame()
    if hist.empty or len(hist) < 2:
        # fallback mock: 6 个点确保不挂, 区分平稳/微涨
        hist = pd.DataFrame({
            "ym": [f"M-{i}" for i in range(n, 0, -1)],
            "v": [1.0] * n,
        })
    try:
        st.area_chart(
            hist.set_index("ym")[["v"]],
            height=60,
            use_container_width=True,
        )
    except Exception:
        pass


# PPVI P2 · 不对称分栏（主 KPI 占 2 列，3 个次 KPI 各 1 列）
# 结构主义：打破完美等分，用栏宽传达主次信息层级
# 主 KPI 选「毛利率」——经营核心指标
k1, k2, k3, k4 = st.columns([2, 1, 1, 1])
with k1:
    st.metric(t("毛利率"), f"{gp_rate * 100:.1f}%", delta=_margin_delta_str)
    _spark("gross_margin")
with k2:
    st.metric(t("商品 SKU"), f"{int(sku_total):,}", delta=_sku_delta_str)
    _spark("sku_total")
with k3:
    st.metric(t("在库金额"), _fmt_jpy_short(inv_amount), delta=_stock_delta_str)
    _spark("stock_value_jpy")
with k4:
    st.metric(t("本月销售"), _fmt_jpy_short(sales_amount), delta=_revenue_delta_str)
    _spark("month_revenue_jpy")


# ============================================================
# 风险预警 5 KPI
# ============================================================
# PPVI P1 · 去 emoji 装饰前缀
st.markdown(f"##### {t('风险预警')}")

# 当月 latest year_month 优先，找不到就用全部
latest_ym_row = _safe_df(
    "SELECT MAX(year_month) AS ym FROM item_monthly_turnover"
)
latest_ym = None
if not latest_ym_row.empty:
    try:
        latest_ym = latest_ym_row.iloc[0]["ym"]
    except Exception:
        latest_ym = None

ym_filter = f"AND year_month = '{latest_ym}'" if latest_ym else ""

risk_df = _safe_df(
    f"""
    SELECT risk_label, COUNT(DISTINCT item_code) AS c
    FROM item_monthly_turnover
    WHERE 1=1 {ym_filter}
    GROUP BY risk_label
    """
)
risk_map = {}
if not risk_df.empty:
    for _, row in risk_df.iterrows():
        risk_map[str(row.get("risk_label") or "无数据")] = int(row.get("c") or 0)

# 入荷困難 (单独表)
hard_get_n = _safe_scalar(
    "SELECT COUNT(*) FROM difficult_items",
    default=0,
)

# PPVI P2 · 不对称分栏（断货风险作为主告警占 2 列，4 个次告警各 1 列）
r1, r2, r3, r4, r5 = st.columns([2, 1, 1, 1, 1])
# PPVI P1 · KPI 标签去 emoji（中文硬编码绕过 i18n.py 未提交改动）
r1.metric("断货风险", int(risk_map.get("断货风险", 0)))
r2.metric("正常", int(risk_map.get("正常", 0)))
r3.metric("压库存", int(risk_map.get("压库存", 0)))
r4.metric("数据不足", int(risk_map.get("数据不足", 0) + risk_map.get("无数据", 0)))
r5.metric("入荷困難", int(hard_get_n))

st.divider()


# ============================================================
# 当 v2 表为空 → 友好 empty state
# ============================================================
if int(sku_total) == 0:
    st.info(
        f"📭 {t('item_v2 表暂无数据。请先到')} **⚙️ 数据导入与设置** "
        f"{t('上传 6 份 NST 报表')}（NetSuite Item / Sales / Inventory / Cost / Status / Daily Sales）。"
    )

# ============================================================
# 2 列布局图表（mockup T7 视图 / T11 图表）
# ============================================================
chart_l, chart_r = st.columns(2)

with chart_l:
    st.markdown(f"##### 📈 {t('月度销售趋势 (近 6 个月)')}")
    trend_df = _safe_df(
        """
        SELECT period_start, COALESCE(SUM(revenue_jpy), 0) AS revenue
        FROM shop_sales
        WHERE granularity = 'monthly'
        GROUP BY period_start
        ORDER BY period_start DESC
        LIMIT 6
        """
    )
    if not trend_df.empty:
        trend_df = trend_df.sort_values("period_start")
        trend_df["month"] = pd.to_datetime(trend_df["period_start"]).dt.strftime("%Y-%m")
        chart_data = trend_df.set_index("month")[["revenue"]]
        chart_data.columns = [t("销售额 (JPY)")]
        st.line_chart(chart_data, height=260)
    else:
        st.info(t("暂无销售数据"))

with chart_r:
    st.markdown(f"##### 🏪 {t('市场分布 (按 shop_id)')}")
    market_df = _safe_df(
        f"""
        SELECT shop_id, COALESCE(SUM(revenue_jpy), 0) AS revenue
        FROM shop_sales
        WHERE granularity = 'monthly' AND period_start = '{period_start}'
        GROUP BY shop_id
        ORDER BY revenue DESC
        LIMIT 8
        """
    )
    if not market_df.empty:
        chart_data = market_df.set_index("shop_id")[["revenue"]]
        chart_data.columns = [t("销售额 (JPY)")]
        st.bar_chart(chart_data, height=260)
    else:
        st.info(t("暂无市场数据"))


st.divider()


# ============================================================
# TOP 10 当月销售（shop_sales JOIN item_v2 取 maker / rank）
# ============================================================
st.markdown(f"##### 🏆 {t('TOP 10 当月销售')}")

top10 = _safe_df(
    f"""
    SELECT
      ss.jan,
      COALESCE(iv.display_name, '') AS display_name,
      COALESCE(iv.maker, '')        AS maker,
      COALESCE(iv.rank, '')         AS rank,
      COALESCE(SUM(ss.qty_sold), 0)     AS qty_sold,
      COALESCE(SUM(ss.revenue_jpy), 0)  AS revenue
    FROM shop_sales ss
    LEFT JOIN item_v2 iv ON iv.jan = ss.jan
    WHERE ss.granularity = 'monthly' AND ss.period_start = '{period_start}'
    GROUP BY ss.jan
    ORDER BY revenue DESC
    LIMIT 10
    """
)

if not top10.empty:
    show = top10.copy()
    show["revenue"] = show["revenue"].apply(lambda v: f"¥{float(v or 0):,.0f}")
    show["qty_sold"] = show["qty_sold"].apply(lambda v: f"{int(float(v or 0)):,}")
    show.columns = [t("JAN"), t("商品名"), t("メーカー"), t("ランク"), t("销量"), t("销售额")]
    st.dataframe(show, use_container_width=True, hide_index=True, height=380)
else:
    st.info(t("📭 当月暂无销售数据 · 请到「⚙️ 数据导入与设置」上传 ASEAN 月度销售报表"))


st.divider()


# ============================================================
# v2 数据快查（保留原有 expander, 折叠到底部）
# ============================================================
with st.expander(f"📦 {t('v2 数据快查 (raw counts)')}"):
    def _count(table: str) -> int:
        try:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            return int(row["c"]) if row else 0
        except Exception:
            return 0

    cc1, cc2, cc3, cc4, cc5, cc6 = st.columns(6)
    cc1.metric(t("商品"), f"{_count('item'):,}")
    cc2.metric(t("供应商"), f"{_count('supplier'):,}")
    cc3.metric(t("销售记录"), f"{_count('sales'):,}")
    cc4.metric(t("库存快照"), f"{_count('inventory'):,}")
    cc5.metric(t("采购记录"), f"{_count('purchase'):,}")
    cc6.metric(t("批次"), f"{_count('lot'):,}")


# ============================================================
# 模块导航（保留, 给新用户当索引）
# ============================================================
with st.expander(f"🗂️ {t('功能模块速查')}"):
    modules = [
        ("🔍 商品情报检索", "Phase 1", "按多维度筛选 SKU，看完整商品信息"),
        ("💰 定義原価編集", "Phase 1", "NetSuite Standard Cost 统一编辑"),
        ("📊 销售数据查询", "Phase 2", "按时间/商品/店铺多维度查销售"),
        ("🏪 店铺毛利", "Phase 2", "整月与单月对比"),
        ("📦 库存健康监控", "Phase 3", "ratio_months 4 档健康度判定"),
        ("🏷️ 商品等级判定", "Phase 3", "A/B/C/停售 4 档·季度·Boss-only"),
        ("💡 运营调整建议", "Phase 3", "毛利×周转 5 档矩阵建议"),
        ("⚠️ 改廃確認", "Phase 4", "Boss 三按钮 · 联动停售"),
        ("💱 Shopee財務", "Phase 4", "拨款 + 订单级对账"),
        ("📝 商品登录", "Phase 5", "新品工作流 → NetSuite Item Create CSV"),
        ("📈 等级历史趋势", "Phase 3", "Sankey 图跨季度等级流向"),
        ("🚀 Shopee上架", "Phase 5", "SPU+SKU CSV → AI 文案 → 店小秘 / Shopee mass upload xlsx"),
        ("🛡️ 库存风控", "Phase 4", "月完売率 → 断货 / 压库存 风险监控（不出订货量）"),
    ]

    for i in range(0, len(modules), 2):
        cols = st.columns(2)
        for col, (name, phase, desc) in zip(cols, modules[i : i + 2]):
            with col:
                st.markdown(f"**{t(name)}**  ·  _{phase}_")
                st.caption(t(desc))


# ============================================================
# 固定汇率表（保留, 折叠）
# ============================================================
from shared.forex import FX_TO_JPY, FX_SYMBOLS, FX_NAMES_JA

with st.expander(t("💱 公司对日元固定汇率（仅 Shopee 财务模块使用）"), expanded=False):
    st.caption(t(
        "📌 用途: 仅「💱 Shopee 財務」模块把 PHP 等外币换算到 JPY · "
        "其他模块数据本身就是日元 · 数据源: NetSuite 為替レート (2026-04-30) · "
        "Boss 修正: PHP=2.4 / USD=145 · 修改在 shared/forex.py"
    ))
    fx_df = pd.DataFrame([
        {
            t("货币代码"): code,
            t("ソース通貨"): FX_NAMES_JA.get(code, ""),
            t("符号"): FX_SYMBOLS.get(code, ""),
            t("1 单位 → JPY"): rate,
        }
        for code, rate in FX_TO_JPY.items()
        if code != "JPY"
    ])
    st.dataframe(fx_df, use_container_width=True, hide_index=True)


# ============================================================
# 最近导入（保留, 折叠）
# ============================================================
with st.expander(f"📥 {t('最近数据导入')}"):
    try:
        runs = conn.execute(
            """
            SELECT ingestor, source_file, total_rows, inserted, errors, run_at
            FROM _ingest_runs
            ORDER BY run_id DESC
            LIMIT 10
            """
        ).fetchall()
        if runs:
            st.dataframe(
                [dict(r) for r in runs],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info(t("还没有任何导入记录。请到「⚙️ 数据导入与设置」上传 CSV"))
    except Exception as e:
        st.error(f"{t('读取导入记录失败')}: {e}")
