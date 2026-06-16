"""页面 #04 销售数据查询 · 库存 fan-out 回归测试。

障害（2026-06-16 修复）：当月 KPI「販売数量計 / 総収益計 / 粗利計」从主表 df SUM 出来，
而主表查询 `LEFT JOIN _INV inv ON inv.item_internal_id = s.item_internal_id` 里的 _INV 子查询
**没有按 item 聚合**——inventory_snapshot 按仓库分行（JD-物流-千葉 + 弁天倉庫），同一 SKU
有多行 → 每条销售行被 join 成多行 → `SUM(s.qty_sold/revenue/gross_profit)` 被乘仓库数放大。
比率类（粗利率）因分子分母同倍放大而免疫，绝对值类（数量/收益/毛利）全被夸大。
同页「上月」用无 inv-join 的查询算对了，是对照参照。

修复 = _INV 改为 `SUM(qty_on_hand) ... WHERE warehouse IN (JD,弁天) GROUP BY item_internal_id`
（每品一行，fan-out 消失；on_hand 口径对齐本页 库存总金额 KPI 的 JD+弁天 范围）。

数据用 tests/nstdb.py 的 ATTACH-SQLite 范式 seed（零依赖 · 不需 PG/Docker）。
本测试直接对 seeded SQLite 跑主表查询的销售聚合骨架，锁定 de-fan-out 语义。
"""
from __future__ import annotations

import pytest

from tests.nstdb import new_conn, seed_item, seed_inventory, seed_sales

_YM = "2026-05"
_LATEST = "2026-05-31"

# ── 主表 df 查询的销售聚合骨架（与 pages/04 第 524-534 行同形）──
# 只保留触发/暴露 fan-out 的最小列：jan + 3 个销售 SUM + on_hand。
_SELECT = (
    "SELECT im.jan AS jan, "
    "SUM(s.qty_sold) AS qty_sold, SUM(s.revenue) AS revenue, "
    "SUM(s.gross_profit) AS gross_profit, MAX(inv.qty_on_hand) AS qty_on_hand "
    "FROM nst.sales_monthly s "
    "LEFT JOIN nst.item_master_raw im ON im.internal_id = s.item_internal_id "
    "LEFT JOIN {INV} inv ON inv.item_internal_id = s.item_internal_id "
    "WHERE s.year_month = ? "
    "GROUP BY im.jan"
)

# 旧（bug）：_INV 不聚合 → 多仓品 fan-out
_INV_OLD = (
    "(SELECT item_internal_id, qty_on_hand FROM nst.inventory_snapshot "
    "WHERE snapshot_date=(SELECT max(snapshot_date) FROM nst.inventory_snapshot))"
)
# 新（修复）：每品一行 + JD+弁天 范围（与 pages/04 _INV 修复后一致）
_INV_NEW = (
    "(SELECT item_internal_id, SUM(qty_on_hand) AS qty_on_hand "
    "FROM nst.inventory_snapshot "
    "WHERE snapshot_date=(SELECT max(snapshot_date) FROM nst.inventory_snapshot) "
    "  AND warehouse IN ('JD-物流-千葉', '弁天倉庫') "
    "GROUP BY item_internal_id)"
)


@pytest.fixture
def conn():
    """3 个 SKU：P=多仓(JD+弁天)、Q=单仓(JD)、R=无库存。销售口径一致。"""
    c = new_conn()
    # P：2 仓 → 旧查询会把销售翻倍。另埋 1 条旧快照验证 latest 过滤仍生效。
    seed_item(c, "P0001", display_name="多仓品")
    seed_sales(c, "P0001", _YM, 100, revenue=1000.0, gross_profit=500.0)
    seed_inventory(c, "P0001", qty_on_hand=30, warehouse="JD-物流-千葉", snapshot_date=_LATEST)
    seed_inventory(c, "P0001", qty_on_hand=20, warehouse="弁天倉庫", snapshot_date=_LATEST)
    seed_inventory(c, "P0001", qty_on_hand=999, warehouse="JD-物流-千葉", snapshot_date="2026-04-30")
    # Q：1 仓 → 不受 fan-out 影响
    seed_item(c, "Q0002", display_name="单仓品")
    seed_sales(c, "Q0002", _YM, 50, revenue=500.0, gross_profit=250.0)
    seed_inventory(c, "Q0002", qty_on_hand=40, warehouse="JD-物流-千葉", snapshot_date=_LATEST)
    # R：无库存记录 → LEFT JOIN 必须保留该行
    seed_item(c, "R0003", display_name="无库存品")
    seed_sales(c, "R0003", _YM, 10, revenue=100.0, gross_profit=60.0)
    c.commit()
    return c


def _totals(c, inv):
    rows = c.execute(_SELECT.format(INV=inv), (_YM,)).fetchall()
    by_jan = {r[0]: r for r in rows}
    tot_q = sum(r[1] for r in rows)
    tot_r = sum(r[2] for r in rows)
    tot_g = sum(r[3] for r in rows)
    return by_jan, tot_q, tot_r, tot_g


def test_old_query_inflates_multiwarehouse_sales(conn):
    """旧 _INV：多仓品 P 的销售被翻倍，合计被夸大（复现障害）。"""
    by_jan, tot_q, tot_r, tot_g = _totals(conn, _INV_OLD)
    assert by_jan["P0001"][1] == 200   # 100 × 2 仓 → fan-out
    assert by_jan["Q0002"][1] == 50    # 单仓不受影响
    assert tot_q == 260                # 期望 160，被夸大到 260
    assert tot_r == 2600.0             # 期望 1600
    assert tot_g == 1310.0             # 期望 810


def test_new_query_no_fanout(conn):
    """修复 _INV：每品一行，销售合计回到真值。"""
    by_jan, tot_q, tot_r, tot_g = _totals(conn, _INV_NEW)
    assert by_jan["P0001"][1] == 100   # 不再翻倍
    assert by_jan["Q0002"][1] == 50
    assert by_jan["R0003"][1] == 10    # LEFT JOIN：无库存品仍在
    assert tot_q == 160
    assert tot_r == 1600.0
    assert tot_g == 810.0


def test_new_query_onhand_is_warehouse_sum(conn):
    """修复后 on_hand = JD+弁天 之和（30+20），且只取 latest 快照（排除 999 旧行）。"""
    by_jan, *_ = _totals(conn, _INV_NEW)
    assert by_jan["P0001"][4] == 50    # 30 + 20，不是 MAX 也不含 999
    assert by_jan["Q0002"][4] == 40
    assert by_jan["R0003"][4] is None  # 无库存 → NULL
