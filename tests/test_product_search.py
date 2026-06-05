"""商品检索增强模块测试（T-008）。

覆盖 DoD ≥6 测试：
- 单维筛选 × 3：brand / category / price_range
- 多维组合 × 1：brand + price + stock_status
- 全文搜索命中 × 1：keyword 命中 display_name / maker
- 导出 CSV 行数对得上 × 1
额外：stock_status / created_at 单维、filters 校验、hide_inactive。

数据用 tests/nstdb.py 的 ATTACH-SQLite 范式 seed（零依赖 · 不需 PG/Docker）。
"""
from __future__ import annotations

import io

import pandas as pd
import pytest

from modules.product_search import (
    STOCK_IN,
    STOCK_OUT,
    SearchFilters,
    search_items,
    to_csv_bytes,
)
from modules.product_search.filters import STOCK_ALL
from tests.nstdb import new_conn, seed_item, seed_inventory


@pytest.fixture
def conn():
    """4 件 SKU 的固定数据集 · 覆盖各筛选维。"""
    c = new_conn()
    # A: 花王 美白凝胶 · rank A · 原价 300 · 有库存 · 2026-05 更新
    seed_item(c, "4900001", display_name="パーフェクトホワイトジェル",
              maker="花王", item_rank="Aランク", cost_estimate=300.0,
              last_modified="2026-05-10")
    seed_inventory(c, "4900001", qty_on_hand=50)
    # B: 花王 洗顔 · rank B · 原价 120 · 无库存 · 2026-01 更新
    seed_item(c, "4900002", display_name="フェイスウォッシュ",
              maker="花王", item_rank="Bランク", cost_estimate=120.0,
              last_modified="2026-01-15")
    seed_inventory(c, "4900002", qty_on_hand=0)
    # C: 資生堂 化粧水 · rank A · 原价 800 · 有库存 · 2026-06 更新
    seed_item(c, "4900003", display_name="化粧水ローション",
              maker="資生堂", item_rank="Aランク", cost_estimate=800.0,
              last_modified="2026-06-01")
    seed_inventory(c, "4900003", qty_on_hand=10)
    # D: 資生堂 停用品 · rank C · 原价 50 · 无库存记录 · is_inactive
    seed_item(c, "4900004", display_name="廃番アイテム",
              maker="資生堂", item_rank="Cランク", cost_estimate=50.0,
              last_modified="2025-12-01", is_inactive=1)
    c.commit()
    return c


# ============================================================
# 单维筛选 × 3
# ============================================================
def test_filter_by_brand(conn):
    """brand 单维：maker = 花王 → 2 件（默认隐藏停用品不影响花王）。"""
    df = search_items(conn, SearchFilters(brands=["花王"]))
    assert set(df["item_code"]) == {"4900001", "4900002"}


def test_filter_by_category(conn):
    """category 单维：item_rank = Aランク → A/C 两件（D 是 C 且停用）。"""
    df = search_items(conn, SearchFilters(categories=["Aランク"]))
    assert set(df["item_code"]) == {"4900001", "4900003"}


def test_filter_by_price_range(conn):
    """price_range 单维：100 ≤ cost_estimate ≤ 400 → A(300)/B(120)。"""
    df = search_items(conn, SearchFilters(price_min=100, price_max=400))
    assert set(df["item_code"]) == {"4900001", "4900002"}


# ============================================================
# 额外单维（stock_status / created_at）
# ============================================================
def test_filter_by_stock_status_in(conn):
    """stock_status=IN → 有库存的 A/C。"""
    df = search_items(conn, SearchFilters(stock_status=STOCK_IN))
    assert set(df["item_code"]) == {"4900001", "4900003"}


def test_filter_by_stock_status_out(conn):
    """stock_status=OUT → B 无库存（D 停用被默认隐藏）。"""
    df = search_items(conn, SearchFilters(stock_status=STOCK_OUT))
    assert set(df["item_code"]) == {"4900002"}


def test_multi_warehouse_no_duplicate_rows():
    """同一 SKU 多仓（JD-千葉 + 弁天）同日快照 → 检索仍 1 行·qty 合计。

    回归：inventory_snapshot 主键含 warehouse，inv CTE 不聚合会按仓库数重复行。
    """
    c = new_conn()
    seed_item(c, "4900001", display_name="多仓商品", maker="花王", item_rank="Aランク")
    seed_inventory(c, "4900001", qty_on_hand=10, warehouse="JD-物流-千葉", snapshot_date="2026-06-05")
    seed_inventory(c, "4900001", qty_on_hand=5, warehouse="弁天倉庫", snapshot_date="2026-06-05")
    c.commit()
    df = search_items(c, SearchFilters(stock_status=STOCK_ALL))
    assert len(df) == 1                          # 不按仓库重复
    assert int(df.iloc[0]["qty_on_hand"]) == 15  # 两仓合计 10+5


def test_filter_by_created_at(conn):
    """created_at 区间 2026-02-01..2026-12-31 → A(05)/C(06)（B 是 01 排除）。"""
    df = search_items(conn, SearchFilters(
        created_from="2026-02-01", created_to="2026-12-31"))
    assert set(df["item_code"]) == {"4900001", "4900003"}


# ============================================================
# 多维组合 × 1
# ============================================================
def test_multi_dim_combination(conn):
    """brand=花王 + price≥200 + stock=IN → 仅 A(花王/300/有库存)。"""
    df = search_items(conn, SearchFilters(
        brands=["花王"], price_min=200, stock_status=STOCK_IN))
    assert list(df["item_code"]) == ["4900001"]


# ============================================================
# 全文搜索命中 × 1
# ============================================================
def test_fulltext_hits_display_name(conn):
    """keyword 命中 display_name（部分匹配 · 大小写不敏感）。"""
    df = search_items(conn, SearchFilters(keyword="ローション"))
    assert list(df["item_code"]) == ["4900003"]


def test_fulltext_hits_maker(conn):
    """keyword 命中 maker（display_name 无该词 · 走 maker 分支）。"""
    df = search_items(conn, SearchFilters(keyword="資生堂"))
    assert set(df["item_code"]) == {"4900003"}  # 4900004 停用被隐藏


# ============================================================
# 导出 CSV 行数对得上 × 1
# ============================================================
def test_export_csv_row_count(conn):
    """导出 CSV 的数据行数 == 检索结果行数（不含表头）。"""
    df = search_items(conn, SearchFilters(brands=["花王"]))
    raw = to_csv_bytes(df)
    text = raw.decode("utf-8-sig")
    parsed = pd.read_csv(io.StringIO(text))
    assert len(parsed) == len(df) == 2
    # 表头存在
    assert "item_code" in parsed.columns


def test_export_csv_with_labels(conn):
    """带三语表头时行数不变、表头被重命名。"""
    df = search_items(conn, SearchFilters(stock_status=STOCK_ALL,
                                          hide_inactive=False))
    labels = {"item_code": "商品编码", "display_name": "显示名"}
    parsed = pd.read_csv(io.StringIO(to_csv_bytes(df, labels).decode("utf-8-sig")))
    assert len(parsed) == 4  # 不隐藏停用品 → 全 4 件
    assert "商品编码" in parsed.columns


# ============================================================
# filters 校验
# ============================================================
def test_filters_reject_inverted_price():
    with pytest.raises(ValueError):
        SearchFilters(price_min=500, price_max=100).validate()


def test_filters_reject_bad_date():
    with pytest.raises(ValueError):
        SearchFilters(created_from="2026/01/01").validate()


def test_filters_reject_bad_stock_status():
    with pytest.raises(ValueError):
        SearchFilters(stock_status="MAYBE").validate()


def test_active_dims_tracks_set_dims():
    f = SearchFilters(keyword="x", brands=["花王"], price_min=10)
    assert set(f.active_dims()) == {"keyword", "brand", "price_range"}
