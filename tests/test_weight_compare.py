"""JDL vs NST 重量对比模块测试（page 02 Tab2 恢复）。

覆盖：
- load_compare：ATTACH-SQLite（nst.item_master_raw + jdl.v_goods_dimensions）端到端取数
- compute_compare：diff_g / diff_pct 计算正确（含 NST=0 → diff_pct NaN 防除零）
- coverage_stats：覆盖率/一致性统计 + comparable 按 |diff_pct| 降序

零依赖（in-memory SQLite · 不需 PG/Docker）。
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest

from modules.weight_compare import (
    compute_compare,
    coverage_stats,
    load_compare,
)

# 本模块专用最小 schema（nstdb.py 不含重量列 / jdl view）
_DDL = """
CREATE TABLE nst.item_master_raw (
    jan             TEXT,
    display_name    TEXT,
    maker           TEXT,
    item_rank       TEXT,
    is_inactive     INTEGER DEFAULT 0,
    item_weight_g   REAL,
    package_weight_g REAL,
    carton_weight_g  REAL
);
CREATE TABLE jdl.v_goods_dimensions (
    jan               TEXT,
    wms_gross_weight_g REAL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("ATTACH DATABASE ':memory:' AS nst")
    c.execute("ATTACH DATABASE ':memory:' AS jdl")
    c.executescript(_DDL)
    return c


def _seed_item(c, jan, *, display_name="商品", maker="花王", item_rank="A",
               is_inactive=0, item_weight_g=None, package_weight_g=None,
               carton_weight_g=None):
    c.execute(
        "INSERT INTO nst.item_master_raw "
        "(jan, display_name, maker, item_rank, is_inactive, "
        " item_weight_g, package_weight_g, carton_weight_g) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (jan, display_name, maker, item_rank, is_inactive,
         item_weight_g, package_weight_g, carton_weight_g),
    )


def _seed_jdl(c, jan, wms_gross_weight_g):
    c.execute(
        "INSERT INTO jdl.v_goods_dimensions (jan, wms_gross_weight_g) VALUES (?,?)",
        (jan, wms_gross_weight_g),
    )


@pytest.fixture
def conn():
    c = _conn()
    # A: NST=100 / JDL=110 → diff +10 / +10%（可对比 · close）
    _seed_item(c, "4900001", package_weight_g=100.0)
    _seed_jdl(c, "4900001", 110.0)
    # B: NST=200 / JDL=300 → diff +100 / +50%（可对比 · big diff）
    _seed_item(c, "4900002", package_weight_g=200.0)
    _seed_jdl(c, "4900002", 300.0)
    # C: NST 有 / JDL 无 → 不可对比
    _seed_item(c, "4900003", package_weight_g=150.0)
    # D: 停用 → 应被 WHERE 过滤掉
    _seed_item(c, "4900004", is_inactive=1, package_weight_g=999.0)
    _seed_jdl(c, "4900004", 999.0)
    return c


def test_load_compare_excludes_inactive(conn):
    df = load_compare(conn)
    jans = set(df["jan"].tolist())
    assert "4900004" not in jans          # 停用被过滤
    assert {"4900001", "4900002", "4900003"} <= jans
    assert len(df) == 3


def test_compute_diff(conn):
    df = compute_compare(load_compare(conn)).set_index("jan")
    assert df.loc["4900001", "diff_g"] == pytest.approx(10.0)
    assert df.loc["4900001", "diff_pct"] == pytest.approx(10.0)
    assert df.loc["4900002", "diff_g"] == pytest.approx(100.0)
    assert df.loc["4900002", "diff_pct"] == pytest.approx(50.0)
    # C 无 JDL → diff_g 为 NaN
    assert pd.isna(df.loc["4900003", "diff_g"])


def test_compute_diff_no_divzero():
    df = pd.DataFrame({
        "jan": ["X"], "display_name": ["x"], "maker": ["m"], "item_rank": ["A"],
        "nst_item_g": [None], "nst_package_g": [0.0],
        "nst_carton_g": [None], "jdl_wms_g": [50.0],
    })
    out = compute_compare(df)
    # NST package=0 → diff_pct 不能是 inf，应为 NaN
    assert pd.isna(out.loc[0, "diff_pct"])
    assert not np.isinf(out.loc[0, "diff_pct"])


def test_coverage_stats(conn):
    s = coverage_stats(compute_compare(load_compare(conn)))
    assert s["n_total"] == 3        # 活跃 3 件
    assert s["n_nst"] == 3          # 三件都有 NST 重量
    assert s["n_jdl"] == 2          # 只有 A/B 有 JDL
    assert s["n_cmp"] == 2          # A/B 可对比
    assert s["n_close"] == 1        # 只有 A 差 ≤10%
    assert s["n_diff_big"] == 1     # B 差 50% > 30%
    # comparable 按 |diff_pct| 降序：B(50%) 在 A(10%) 前
    order = s["comparable"]["jan"].tolist()
    assert order == ["4900002", "4900001"]
