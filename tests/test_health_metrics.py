"""库存健康度计算模块的测试（modules/inventory_health/metrics.py）。

2026-05 业务规则迁移后版本：
- 健康度主判定改为 **ratio_months = qty_on_hand / qty_sold**（不再按 short/normal/long
  进货周期桶分 GMROI 阈值）。阈值 0.7 / 2.0 / 6.0 月 → 🟢优秀 / 🟡健康 / 🟠注意 / 🔴死钱。
- 库存硬过滤 location = 'JD-物流-千葉'（v2 仓库决策）：seed 必须带 location，否则查不到库存。
- CrossRatio 字段为 gross_margin_pct / monthly_turnover（月周转 = qty_sold / qty_on_hand）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import shared.db as shared_db
from modules.inventory_health.metrics import (
    calc_cross_ratio,
    calc_health_grade,
    calc_stock_sales_ratio,
    batch_calc,
    get_bucket,
    WAREHOUSE_FILTER,
)

WH = WAREHOUSE_FILTER  # 'JD-物流-千葉'


def _ins_inv(conn, sku, qty, cost, *, location=WH, handling="取扱中", iid_suffix="1"):
    conn.execute(
        "INSERT INTO nst_inventory_snapshot "
        "(internal_id, item_code, qty_on_hand, std_cost, location, handling_status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"ID-{sku}-{iid_suffix}", sku, qty, cost, location, handling),
    )


def _ins_sales(conn, sku, qty_sold, *, margin=0.5, revenue=0.0):
    conn.execute(
        "INSERT INTO nst_store_sales (item_code, qty_sold, gross_margin, revenue) "
        "VALUES (?, ?, ?, ?)",
        (sku, qty_sold, margin, revenue),
    )


@pytest.fixture
def test_db():
    """创建临时测试数据库（文件型，便于 batch_calc 另开连接）。"""
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"

        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS supply_cycle (
                jan TEXT PRIMARY KEY, lead_time_days INTEGER, bucket TEXT,
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS nst_turnover (
                id INTEGER PRIMARY KEY AUTOINCREMENT, department TEXT,
                item_code TEXT NOT NULL, handling_status TEXT, cost REAL, avg_value REAL,
                turnover_rate REAL, avg_days_on_hand REAL,
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(item_code, department)
            );
            CREATE TABLE IF NOT EXISTS nst_store_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT, fb_store TEXT, item_code TEXT NOT NULL,
                upc TEXT, handling_status TEXT, display_name TEXT, qty_sold REAL, unit_price REAL,
                revenue REAL, defined_cost REAL, gross_profit REAL, gross_margin REAL, rank TEXT,
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS nst_inventory_snapshot (
                id INTEGER PRIMARY KEY AUTOINCREMENT, internal_id TEXT NOT NULL, item_code TEXT NOT NULL,
                upc TEXT, display_name TEXT, status TEXT, bin_number TEXT, location TEXT,
                handling_status TEXT, qty_on_hand REAL, qty_committed REAL, qty_backorder REAL,
                std_cost REAL, total_amount REAL, avg_cost REAL, owner TEXT, department TEXT,
                ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(internal_id, location, bin_number)
            );
            CREATE TABLE IF NOT EXISTS stock_sales_ratio_monthly (
                sku TEXT NOT NULL, year_month TEXT NOT NULL, end_inventory REAL, monthly_sales REAL,
                ratio_months REAL, calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sku, year_month)
            );
            CREATE TABLE IF NOT EXISTS cross_ratio_monthly (
                sku TEXT NOT NULL, year_month TEXT NOT NULL, gross_margin REAL, turnover REAL,
                cross_ratio REAL, calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (sku, year_month)
            );
            CREATE TABLE IF NOT EXISTS health_grade_monthly (
                sku TEXT NOT NULL, year_month TEXT NOT NULL, bucket TEXT, threshold REAL,
                cross_ratio REAL, grade TEXT, dead_money_jpy REAL,
                calculated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (sku, year_month)
            );
            """
        )
        conn.commit()
        yield (conn, str(db_path))
        conn.close()


# ============================================================
# get_bucket（2）
# ============================================================


def test_get_bucket_exists(test_db):
    conn, _ = test_db
    conn.execute("INSERT INTO supply_cycle (jan, bucket) VALUES (?, ?)", ("SKU-001", "short"))
    conn.commit()
    assert get_bucket("SKU-001", conn) == "short"


def test_get_bucket_missing(test_db):
    conn, _ = test_db
    assert get_bucket("SKU-999", conn) == "normal"


# ============================================================
# calc_stock_sales_ratio（3）— 库存需带 location 才被统计
# ============================================================


def test_calc_stock_sales_ratio_normal(test_db):
    conn, _ = test_db
    sku = "SKU-100"
    _ins_inv(conn, sku, 100.0, 1000.0, iid_suffix="1")
    _ins_inv(conn, sku, 50.0, 1000.0, iid_suffix="2")
    _ins_sales(conn, sku, 30.0)
    conn.commit()

    result = calc_stock_sales_ratio(sku, "2026-04", conn)
    assert result.end_inventory == 150.0
    assert result.monthly_sales == 30.0
    assert result.ratio_months == 5.0  # 150 / 30


def test_calc_stock_sales_ratio_zero_sales(test_db):
    conn, _ = test_db
    sku = "SKU-101"
    _ins_inv(conn, sku, 100.0, 1000.0)
    conn.commit()

    result = calc_stock_sales_ratio(sku, "2026-04", conn)
    assert result.end_inventory == 100.0
    assert result.monthly_sales == 0.0
    assert result.ratio_months == 0.0


def test_calc_stock_sales_ratio_zero_inventory(test_db):
    conn, _ = test_db
    sku = "SKU-102"
    _ins_sales(conn, sku, 50.0)
    conn.commit()

    result = calc_stock_sales_ratio(sku, "2026-04", conn)
    assert result.end_inventory == 0.0
    assert result.monthly_sales == 50.0
    assert result.ratio_months == 0.0


# ============================================================
# calc_cross_ratio（2）— 月周转 = qty_sold / qty_on_hand
# ============================================================


def test_calc_cross_ratio_normal(test_db):
    conn, _ = test_db
    sku = "SKU-200"
    _ins_sales(conn, sku, 10.0, margin=0.4)
    _ins_inv(conn, sku, 5.0, 1000.0)          # 月周转 = 10 / 5 = 2.0
    conn.commit()

    result = calc_cross_ratio(sku, "2026-04", conn)
    assert result.gross_margin_pct == 40.0    # round(0.4 * 100, 1)
    assert result.monthly_turnover == 2.0
    assert abs(result.cross_ratio - 80.0) < 1e-9  # 40.0 * 2.0


def test_calc_cross_ratio_missing_data(test_db):
    conn, _ = test_db
    result = calc_cross_ratio("SKU-201", "2026-04", conn)
    assert result.gross_margin_pct == 0.0
    assert result.monthly_turnover == 0.0
    assert result.cross_ratio == 0.0


# ============================================================
# calc_health_grade（4 档 ratio_months + 特殊情形）
# ============================================================


def test_health_grade_excellent_fast_mover(test_db):
    """ratio_months ≤ 0.7 → 🟢 优秀（畅销）。qty 5 / sold 10 = 0.5。"""
    conn, _ = test_db
    sku = "SKU-300"
    _ins_sales(conn, sku, 10.0, margin=0.6)
    _ins_inv(conn, sku, 5.0, 1000.0)
    conn.commit()
    result = calc_health_grade(sku, "2026-04", conn)
    assert result.grade == "🟢 优秀"


def test_health_grade_healthy_golden_zone(test_db):
    """0.7 < ratio_months ≤ 2.0 → 🟡 健康。qty 15 / sold 10 = 1.5。"""
    conn, _ = test_db
    sku = "SKU-301"
    _ins_sales(conn, sku, 10.0, margin=0.4)
    _ins_inv(conn, sku, 15.0, 1000.0)
    conn.commit()
    assert calc_health_grade(sku, "2026-04", conn).grade == "🟡 健康"


def test_health_grade_attention(test_db):
    """2.0 < ratio_months ≤ 6.0 → 🟠 注意。qty 40 / sold 10 = 4.0。"""
    conn, _ = test_db
    sku = "SKU-302"
    _ins_sales(conn, sku, 10.0, margin=0.3)
    _ins_inv(conn, sku, 40.0, 1000.0)
    conn.commit()
    assert calc_health_grade(sku, "2026-04", conn).grade == "🟠 注意"


def test_health_grade_deadmoney_overstock(test_db):
    """ratio_months > 6.0 → 🔴 死钱，金额 = qty × std_cost。qty 100 / sold 10 = 10。"""
    conn, _ = test_db
    sku = "SKU-303"
    _ins_sales(conn, sku, 10.0, margin=0.1)
    _ins_inv(conn, sku, 100.0, 100.0)
    conn.commit()
    result = calc_health_grade(sku, "2026-04", conn)
    assert result.grade == "🔴 死钱"
    assert result.dead_money_jpy == 10000.0  # 100 × 100


def test_health_grade_discontinued_forces_deadmoney(test_db):
    """停售 SKU → 强制 🔴 死钱（库存=待清资金），不看 ratio_months。"""
    conn, _ = test_db
    sku = "SKU-304"
    _ins_sales(conn, sku, 50.0, margin=0.5)   # 即使热销
    _ins_inv(conn, sku, 50.0, 100.0, handling="取扱中止")
    conn.commit()
    result = calc_health_grade(sku, "2026-04", conn)
    assert result.grade == "🔴 死钱"
    assert result.dead_money_jpy == 5000.0  # 50 × 100


def test_health_grade_zero_stock_with_sales_is_excellent(test_db):
    """库存 0 + 有销售 → 🟢 优秀（断货畅销，库存效率最高）。"""
    conn, _ = test_db
    sku = "SKU-305"
    _ins_sales(conn, sku, 30.0, margin=0.5)
    conn.commit()
    result = calc_health_grade(sku, "2026-04", conn)
    assert result.grade == "🟢 优秀"
    assert result.dead_money_jpy is None


def test_health_grade_zero_stock_zero_sales_is_deadmoney(test_db):
    """库存 0 + 销售 0 → 🔴 死钱（金额 0，无资金占用）。"""
    conn, _ = test_db
    sku = "SKU-306"
    _ins_inv(conn, sku, 0.0, 1000.0)
    conn.commit()
    result = calc_health_grade(sku, "2026-04", conn)
    assert result.grade == "🔴 死钱"
    assert result.dead_money_jpy == 0.0


def test_deadmoney_calculation_precise(test_db):
    """死钱金额精确：库存 75 件 × 单价 200 = 15000。ratio 75/5 = 15 > 6。"""
    conn, _ = test_db
    sku = "SKU-500"
    _ins_sales(conn, sku, 5.0, margin=0.05)
    _ins_inv(conn, sku, 75.0, 200.0)
    conn.commit()
    result = calc_health_grade(sku, "2026-04", conn)
    assert result.grade == "🔴 死钱"
    assert result.dead_money_jpy == 15000.0  # 75 × 200


# ============================================================
# batch_calc（1）— get_connection を test db に差し替え
# ============================================================


def test_batch_calc_multi_skus(test_db, monkeypatch):
    conn, db_path = test_db

    # batch_calc は内部で shared.db.get_connection() を呼ぶ（渡した db_path は無視）ので差し替える
    def _fake_conn():
        c = sqlite3.connect(db_path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(shared_db, "get_connection", _fake_conn)

    for i in range(1, 4):
        sku = f"SKU-400-{i}"
        _ins_sales(conn, sku, 10.0 + i, margin=0.1 + i * 0.1, revenue=1000.0)
        _ins_inv(conn, sku, 100.0, 1000.0)
    conn.commit()

    results = batch_calc("2026-04", db_path)

    assert len(results) == 3
    assert all(r.year_month == "2026-04" for r in results)
    assert all(r.grade in ["🟢 优秀", "🟡 健康", "🟠 注意", "🔴 死钱"] for r in results)

    health_rows = conn.execute(
        "SELECT COUNT(*) as cnt FROM health_grade_monthly WHERE year_month = '2026-04'"
    ).fetchone()
    assert health_rows["cnt"] == 3
