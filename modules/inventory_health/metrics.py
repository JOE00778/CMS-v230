"""库存健康度计算 v2 · 双指标并存（Cross Ratio 运营视角 + GMROI 库存视角）。

公式（月度·真实单位）:
  gross_margin_pct = round(粗利率 × 100, 1)         # 百分比·1 位小数（例 60.5）
  monthly_turnover = qty_sold / qty_on_hand          # 月周转率（4 月份销量 / 月末库存快照）
  monthly_gross_profit = revenue × 粗利率            # 月毛利
  avg_inventory_value = qty_on_hand × std_cost      # 月平均库存价值（用月末快照近似）

  cross_ratio = gross_margin_pct × monthly_turnover  # 运营视角：产品状态指数
  gmroi = monthly_gross_profit / avg_inventory_value # 库存视角：每元库存月毛利回报

健康度判定（主指标 GMROI · 按进货周期 3 桶分阈值）:
  short(≤7 天)   优秀≥0.50  健康 0.25-0.50  注意 0.08-0.25  死钱<0.08
  normal(8-30 天) 优秀≥0.25  健康 0.125-0.25 注意 0.04-0.125 死钱<0.04
  long(31-60 天)  优秀≥0.16  健康 0.08-0.16  注意 0.025-0.08 死钱<0.025

死钱金额 = qty_on_hand × std_cost（仅 🔴 时填）
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional
from dataclasses import dataclass


# 库存月数阈值（v3 · 不分进货周期桶 · ABC 等级避免依赖 7 天以下紧急渠道）
# ratio_months = qty_on_hand / monthly_sales（多少个月卖完）
RATIO_THRESHOLDS = {
    "excellent_max": 0.7,   # ≤ 0.7 月卖完 → 🟢 优秀（畅销）
    "healthy_max":   2.0,   # 0.7-2.0 月  → 🟡 健康（黄金区）
    "attention_max": 6.0,   # 2.0-6.0 月  → 🟠 注意
    # > 6.0 月 → 🔴 死钱
}

# 兼容旧参数（保留以免别处引用报错；分桶机制已废弃）
GMROI_THRESHOLD = {"short": 0.0, "normal": 0.0, "long": 0.0}

# 仓库硬过滤（v2 决策 · 只看 JD-千叶仓库的库存）
WAREHOUSE_FILTER = "弁天倉庫"  # Boss 2026-11-04 京东库存不再使用


@dataclass
class StockSalesRatio:
    sku: str
    year_month: str
    end_inventory: float
    monthly_sales: float
    ratio_months: float


@dataclass
class CrossRatio:
    sku: str
    year_month: str
    gross_margin_pct: float    # 百分比 1 位小数（60.5）
    monthly_turnover: float    # 月周转率
    cross_ratio: float         # 运营产品状态指数


@dataclass
class GMROI:
    sku: str
    year_month: str
    monthly_gross_profit: float
    avg_inventory_value: float
    gmroi: float               # 每元库存月毛利回报


@dataclass
class HealthGrade:
    sku: str
    year_month: str
    bucket: str
    threshold: float           # GMROI 阈值
    gmroi: float               # 主判定指标
    cross_ratio: float         # 辅助指标
    grade: str
    dead_money_jpy: Optional[float] = None


@dataclass
class HealthRecord(HealthGrade):
    pass


def get_db_connection(db_path: str = "data_warehouse/warehouse.db"):
    """统一走 shared.db.get_connection，自动支持 Postgres / SQLite。"""
    from shared.db import get_connection
    return get_connection()


def get_bucket(sku: str, conn: sqlite3.Connection) -> str:
    """从 supply_cycle 取 bucket，无则默认 normal"""
    row = conn.execute("SELECT bucket FROM supply_cycle WHERE jan = ?", (sku,)).fetchone()
    return row["bucket"] if row and row["bucket"] else "normal"


def get_sku_aggregates(sku: str, conn: sqlite3.Connection) -> dict:
    """单次查询取该 SKU 所有需要的字段，避免重复 SQL"""
    sales = conn.execute("""
        SELECT
            SUM(qty_sold) AS qty_sold,
            SUM(revenue) AS revenue,
            AVG(gross_margin) AS margin,
            SUM(gross_profit) AS gross_profit
        FROM nst_store_sales WHERE item_code = ?
    """, (sku,)).fetchone()

    # 库存 + handling_status：仅 JD-千叶仓库 + 按 (internal_id, location) 去重再聚合
    inv = conn.execute("""
        SELECT
            SUM(qty_on_hand) AS qty,
            AVG(std_cost) AS std_cost,
            MIN(handling_status) AS handling_status
        FROM (
            SELECT DISTINCT internal_id, location, qty_on_hand, std_cost, handling_status
            FROM nst_inventory_snapshot
            WHERE item_code = ? AND location = ?
        )
    """, (sku, WAREHOUSE_FILTER)).fetchone()

    return {
        "qty_sold": (sales["qty_sold"] or 0.0) if sales else 0.0,
        "revenue": (sales["revenue"] or 0.0) if sales else 0.0,
        "margin": (sales["margin"] or 0.0) if sales else 0.0,
        "gross_profit": (sales["gross_profit"] or 0.0) if sales else 0.0,
        "qty_on_hand": (inv["qty"] or 0.0) if inv else 0.0,
        "std_cost": (inv["std_cost"] or 0.0) if inv else 0.0,
        "handling_status": (inv["handling_status"] or "") if inv else "",
    }


def calc_stock_sales_ratio(sku: str, year_month: str, conn: sqlite3.Connection) -> StockSalesRatio:
    a = get_sku_aggregates(sku, conn)
    ratio_months = (a["qty_on_hand"] / a["qty_sold"]) if a["qty_sold"] > 0 else 0.0
    return StockSalesRatio(sku, year_month, a["qty_on_hand"], a["qty_sold"], ratio_months)


def calc_cross_ratio(sku: str, year_month: str, conn: sqlite3.Connection) -> CrossRatio:
    """运营视角 · 月周转 × 毛利率%"""
    a = get_sku_aggregates(sku, conn)
    gross_margin_pct = round(a["margin"] * 100, 1)
    monthly_turnover = (a["qty_sold"] / a["qty_on_hand"]) if a["qty_on_hand"] > 0 else 0.0
    cross_ratio = gross_margin_pct * monthly_turnover
    return CrossRatio(sku, year_month, gross_margin_pct, monthly_turnover, cross_ratio)


def calc_gmroi(sku: str, year_month: str, conn: sqlite3.Connection) -> GMROI:
    """库存视角 · 月毛利 / 平均库存价值"""
    a = get_sku_aggregates(sku, conn)
    monthly_gross_profit = a["revenue"] * a["margin"]
    avg_inventory_value = a["qty_on_hand"] * a["std_cost"]
    gmroi = (monthly_gross_profit / avg_inventory_value) if avg_inventory_value > 0 else 0.0
    return GMROI(sku, year_month, monthly_gross_profit, avg_inventory_value, gmroi)


def calc_health_grade(sku: str, year_month: str, conn: sqlite3.Connection,
                       sales_rank_pct: Optional[float] = None,
                       consecutive_zero_months: int = 0) -> HealthGrade:
    """健康度主判定：GMROI 按进货周期分桶比阈值

    业务规则（v3）：
    - 取扱中止 / メーカー取扱中止 → 强制 🔴 死钱（已停售，库存=待清资金）
    - 负库存 → 当作 0
    - **C 档（销售后 20%）特殊逻辑**：
        - 月销>0 → 走 GMROI 正常判定
        - 月销=0 + 1 月未动销 → 🟠 注意
        - 月销=0 + 连续 2 月未动销 → 🔴 死钱（处理候选）
    - A/B 档（销售前 80%）：
        - qty=0 + 销>0 → 🟢 优秀（畅销断货，急补货）
        - qty=0 + 销=0 → 🔴 死钱 金额=0（异常·金牛突然不卖）
        - qty>0 → 正常 GMROI 4 档判定

    参数：
    - sales_rank_pct: 销售排名百分位（0-1），>0.80 即为 C 档
    - consecutive_zero_months: 连续 0 销月数（无历史数据时为 0，仅算当月）
    """
    a = get_sku_aggregates(sku, conn)
    bucket = get_bucket(sku, conn)
    # threshold 字段保留作占位（健康度判定不再按桶分阈值，统一 ratio_months）
    threshold = RATIO_THRESHOLDS["healthy_max"]

    # 负库存 → 当 0
    qty = max(a["qty_on_hand"], 0)
    cost = max(a["std_cost"], 0)
    qty_sold = a["qty_sold"]
    revenue = a["revenue"]
    margin = a["margin"]
    status = a.get("handling_status", "")

    gross_margin_pct = round(margin * 100, 1)

    # 停售 SKU → 强制 🔴 死钱（库存全是待清资金）
    if status in ("取扱中止", "メーカー取扱中止"):
        dead_money = qty * cost
        return HealthGrade(sku, year_month, bucket, threshold,
                           0.0, 0.0, "🔴 死钱", dead_money)

    # C 档（销售后 20%）特殊逻辑：动销时长判定
    is_c_rank = sales_rank_pct is not None and sales_rank_pct > 0.80
    if is_c_rank and qty_sold == 0:
        # 0 销 → 按未动销月数判定
        if consecutive_zero_months >= 2:
            # 连续 2 月 0 销 → 🔴 处理（dead money = 库存价值）
            return HealthGrade(sku, year_month, bucket, threshold,
                               0.0, 0.0, "🔴 死钱", qty * cost)
        else:
            # 仅 1 月 0 销 → 🟠 注意（暂判，等下月）
            return HealthGrade(sku, year_month, bucket, threshold,
                               0.0, 0.0, "🟠 注意", None)

    # qty = 0 处理（仅 active SKU · 多数是 A/B 档）
    if qty == 0:
        if qty_sold > 0:
            # 已断货畅销 → 优秀（无库存 = 库存效率最高）
            return HealthGrade(sku, year_month, bucket, threshold,
                               float('inf'), 0.0, "🟢 优秀", None)
        else:
            # 0 库存 0 销售 → 死钱（金额=0，无资金占用）
            return HealthGrade(sku, year_month, bucket, threshold,
                               0.0, 0.0, "🔴 死钱", 0.0)

    # 库存价值 = 0（成本未定义）
    avg_inventory_value = qty * cost
    if avg_inventory_value <= 0:
        # 成本未填，按销量定健康度
        if qty_sold > 0:
            return HealthGrade(sku, year_month, bucket, threshold,
                               0.0, 0.0, "🟡 健康", None)
        return HealthGrade(sku, year_month, bucket, threshold,
                           0.0, 0.0, "🔴 死钱", 0.0)

    # 正常计算（GMROI 保留作辅助 · 主判定改为 ratio_months）
    monthly_gross_profit = revenue * margin
    gmroi = monthly_gross_profit / avg_inventory_value
    monthly_turnover = qty_sold / qty
    cross_ratio = gross_margin_pct * monthly_turnover
    ratio_months = qty / qty_sold  # 库存月数（多少个月卖完）

    # 4 档判定（基于 ratio_months · 健康黄金区 0.7-2.0 月）
    if ratio_months <= RATIO_THRESHOLDS["excellent_max"]:
        grade = "🟢 优秀"      # ≤ 0.7 月卖完 · 畅销
    elif ratio_months <= RATIO_THRESHOLDS["healthy_max"]:
        grade = "🟡 健康"      # 0.7-2.0 月 · 黄金区
    elif ratio_months <= RATIO_THRESHOLDS["attention_max"]:
        grade = "🟠 注意"      # 2-6 月 · 偏滞
    else:
        grade = "🔴 死钱"      # > 6 月 · 严重滞销

    # C 档 cap：销>0 但极慢，不进 🔴（C 档死钱判定看动销时长）
    if is_c_rank and grade == "🔴 死钱":
        grade = "🟠 注意"

    dead_money = (qty * cost) if grade == "🔴 死钱" else None

    return HealthGrade(sku, year_month, bucket, threshold, gmroi, cross_ratio, grade, dead_money)


def batch_calc(year_month: str = "2026-04", db_path: str = "data_warehouse/warehouse.db") -> list[HealthRecord]:
    conn = get_db_connection(db_path)
    results = []
    now = datetime.now(timezone.utc).isoformat()
    try:
        # 只跑 JD-千叶仓库 有库存的 SKU
        skus = conn.execute(
            "SELECT DISTINCT item_code FROM nst_inventory_snapshot WHERE location = ? ORDER BY item_code",
            (WAREHOUSE_FILTER,),
        ).fetchall()

        # 预算所有 SKU 的销售排名百分位（用于 C 档判定）
        from modules.rank_classifier.rules import calc_sales_rank
        sku_to_sales = {}
        for s_row in conn.execute(f"""
            SELECT item_code, COALESCE(SUM(revenue), 0) AS revenue
            FROM nst_store_sales WHERE item_code IN (SELECT DISTINCT item_code FROM nst_inventory_snapshot WHERE location = ?)
            GROUP BY item_code
        """, (WAREHOUSE_FILTER,)).fetchall():
            sku_to_sales[s_row["item_code"]] = s_row["revenue"] or 0
        rank_pcts = calc_sales_rank(sku_to_sales) if sku_to_sales else {}

        for row in skus:
            sku = row["item_code"]
            sales_rank_pct = rank_pcts.get(sku, 1.0)  # 没销售 → 默认 100%（C 档）

            ssr = calc_stock_sales_ratio(sku, year_month, conn)
            conn.execute(
                "INSERT OR REPLACE INTO stock_sales_ratio_monthly (sku, year_month, end_inventory, monthly_sales, ratio_months, calculated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ssr.sku, ssr.year_month, ssr.end_inventory, ssr.monthly_sales, ssr.ratio_months, now),
            )

            cr = calc_cross_ratio(sku, year_month, conn)
            conn.execute(
                "INSERT OR REPLACE INTO cross_ratio_monthly (sku, year_month, gross_margin, turnover, cross_ratio, calculated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (cr.sku, cr.year_month, cr.gross_margin_pct, cr.monthly_turnover, cr.cross_ratio, now),
            )

            hg = calc_health_grade(sku, year_month, conn, sales_rank_pct=sales_rank_pct)
            conn.execute(
                "INSERT OR REPLACE INTO health_grade_monthly (sku, year_month, bucket, threshold, cross_ratio, grade, dead_money_jpy, calculated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hg.sku, hg.year_month, hg.bucket, hg.threshold, hg.gmroi, hg.grade, hg.dead_money_jpy, now),
            )
            results.append(HealthRecord(hg.sku, hg.year_month, hg.bucket, hg.threshold, hg.gmroi, hg.cross_ratio, hg.grade, hg.dead_money_jpy))
        conn.commit()
    finally:
        conn.close()
    return results


# 兼容旧名（v1 模块⑦页面可能引用）
THRESHOLD = GMROI_THRESHOLD
