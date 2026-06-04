"""shared/inventory_risk の純ロジックテスト（凭証/DB 不要）。

風控の档分け = 库存月数（当前库存/月销量）を閾値と比較。境界 / 月销0 / 在庫0 を固定して
回帰を防ぐ。完売率は参考指标で分档に使わない。
"""
from __future__ import annotations

import pandas as pd
import pytest

from shared import inventory_risk as ir


# ---- classify_risk 境界（可售天数ベース·既定 断货线30天 / 压库存线90天）----
# 可售天数 = 当前库存 × 30 / 月销量

def test_stockout_below_reorder_line():
    assert ir.classify_risk(0, 10) == ir.RISK_STOCKOUT       # 0天 < 30
    assert ir.classify_risk(5, 10) == ir.RISK_STOCKOUT       # 15天 < 30
    assert ir.classify_risk(9, 10) == ir.RISK_STOCKOUT       # 27天 < 30


def test_normal_between_lines():
    assert ir.classify_risk(10, 10) == ir.RISK_NORMAL        # 30天 = 断货线（含む→正常）
    assert ir.classify_risk(20, 10) == ir.RISK_NORMAL        # 60天
    assert ir.classify_risk(30, 10) == ir.RISK_NORMAL        # 90天 = 压库存线（含む→正常）


def test_overstock_above_line():
    assert ir.classify_risk(31, 10) == ir.RISK_OVERSTOCK     # 93天 > 90
    assert ir.classify_risk(100, 10) == ir.RISK_OVERSTOCK    # 300天


def test_zero_sales():
    assert ir.classify_risk(50, 0) == ir.RISK_OVERSTOCK      # 売れ残り（在庫あり·月销0）
    assert ir.classify_risk(0, 0) == ir.RISK_NO_DATA         # 在庫0·月销0
    assert ir.classify_risk(50, None) == ir.RISK_OVERSTOCK


def test_custom_thresholds():
    # 断货线15天 / 压库存线60天
    assert ir.classify_risk(4, 10, reorder_days=15, overstock_days=60) == ir.RISK_STOCKOUT   # 12天
    assert ir.classify_risk(15, 10, reorder_days=15, overstock_days=60) == ir.RISK_NORMAL    # 45天
    assert ir.classify_risk(25, 10, reorder_days=15, overstock_days=60) == ir.RISK_OVERSTOCK  # 75天


# ---- days_of_supply / stock_months ----

def test_days_of_supply_basic():
    assert ir.days_of_supply(20, 10) == 60.0     # 20×30/10
    assert ir.days_of_supply(5, 10) == 15.0


def test_days_of_supply_zero_sales_is_none():
    assert ir.days_of_supply(50, 0) is None
    assert ir.days_of_supply(50, None) is None


def test_stock_months_basic():
    assert ir.stock_months(20, 10) == 2.0
    assert ir.stock_months(5, 10) == 0.5


def test_stock_months_negative_stock_floored():
    assert ir.stock_months(-5, 10) == 0.0


# ---- 閾値 persist round-trip ----

def test_thresholds_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("INVENTORY_RISK_THRESHOLDS", str(tmp_path / "th.json"))
    assert ir.load_risk_thresholds() == {"reorder_days": 30.0, "overstock_days": 90.0}
    ir.save_risk_thresholds({"reorder_days": 15.0, "overstock_days": 60.0, "ignored": 9})
    assert ir.load_risk_thresholds() == {"reorder_days": 15.0, "overstock_days": 60.0}


def test_load_thresholds_broken_file_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "th.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("INVENTORY_RISK_THRESHOLDS", str(p))
    assert ir.load_risk_thresholds() == {"reorder_days": 30.0, "overstock_days": 90.0}


# ---- enrich ----

def test_enrich_adds_derived_columns():
    df = pd.DataFrame([
        # current_stock / qty_sold = 库存月数
        {"opening_qty": 5, "received_qty": 95, "qty_sold": 100, "current_stock": 50, "cost_estimate": 100},   # 0.5月 → 断货
        {"opening_qty": 50, "received_qty": 50, "qty_sold": 50, "current_stock": 100, "cost_estimate": 200},  # 2.0月 → 正常
        {"opening_qty": 80, "received_qty": 20, "qty_sold": 10, "current_stock": 90, "cost_estimate": 10},    # 9.0月 → 压库存
        {"opening_qty": 0, "received_qty": 0, "qty_sold": 0, "current_stock": 0, "cost_estimate": 50},        # 月销0·在庫0 → 数据不足
    ])
    out = ir.enrich(df)
    assert list(out["risk_label"]) == [
        ir.RISK_STOCKOUT, ir.RISK_NORMAL, ir.RISK_OVERSTOCK, ir.RISK_NO_DATA]
    assert out.loc[0, "days_of_supply"] == pytest.approx(15.0)   # 50×30/100
    assert out.loc[2, "days_of_supply"] == pytest.approx(270.0)  # 90×30/10
    assert out.loc[2, "capital_exposure"] == 900     # 当前库存 90 × 10
    assert pd.isna(out.loc[3, "days_of_supply"])     # 月销0 → NaN
    # 完売率は参考列として残る（分档には未使用）
    assert out.loc[0, "sell_through_rate"] == pytest.approx(1.0)   # 100/(5+95)


def test_enrich_custom_thresholds_shift_bands():
    df = pd.DataFrame([{"opening_qty": 0, "received_qty": 0, "qty_sold": 10,
                        "current_stock": 25, "cost_estimate": 0}])   # 75天
    assert ir.enrich(df, {"reorder_days": 30, "overstock_days": 90}).loc[0, "risk_label"] == ir.RISK_NORMAL
    assert ir.enrich(df, {"reorder_days": 30, "overstock_days": 60}).loc[0, "risk_label"] == ir.RISK_OVERSTOCK


def test_enrich_missing_current_stock_safe():
    # current_stock 欠如 → 0 扱い·月销>0 なら 可售天数0 → 断货
    df = pd.DataFrame([{"opening_qty": 10, "received_qty": 0, "qty_sold": 9, "cost_estimate": 5}])
    out = ir.enrich(df)
    assert out.loc[0, "risk_label"] == ir.RISK_STOCKOUT
    assert out.loc[0, "capital_exposure"] == 0


# ---- inventory_turnover（SKU 360）----

def test_inventory_turnover_normal():
    assert ir.inventory_turnover(50, 100) == 0.5
    assert ir.inventory_turnover(120, 60) == 2.0


def test_inventory_turnover_zero_or_negative_stock():
    assert ir.inventory_turnover(50, 0) == 0.0
    assert ir.inventory_turnover(50, -5) == 0.0
    assert ir.inventory_turnover(50, None) == 0.0


def test_inventory_turnover_no_sales():
    assert ir.inventory_turnover(0, 100) == 0.0
    assert ir.inventory_turnover(None, 100) == 0.0


# ---- 断货判定 is_stockout ----

def test_is_stockout_true_when_prev_sold_and_zero_stock():
    assert ir.is_stockout(89, 0) is True
    assert ir.is_stockout(1, 0) is True


def test_is_stockout_false():
    assert ir.is_stockout(89, 5) is False     # 有库存
    assert ir.is_stockout(0, 0) is False       # 上月无销量
    assert ir.is_stockout(None, 0) is False
    assert ir.is_stockout(10, None) is True    # 库存 None → 0 → 断货


# ---- 断货率 stockout_rate_by_rank ----

def test_stockout_rate_by_rank():
    df = pd.DataFrame([
        {"rank": "Aランク", "is_stockout": True},
        {"rank": "Aランク", "is_stockout": True},
        *[{"rank": "Aランク", "is_stockout": False} for _ in range(8)],   # A: 10 个, 2 断货 → 20%
        {"rank": "Bランク", "is_stockout": True},
        {"rank": "Bランク", "is_stockout": False},                         # B: 2 个, 1 断货 → 50%
        {"rank": "", "is_stockout": True},                                # 无等级 → 不计
        {"rank": None, "is_stockout": True},
    ])
    g = ir.stockout_rate_by_rank(df)
    a = g[g["rank"] == "Aランク"].iloc[0]
    assert int(a["total"]) == 10 and int(a["stockout"]) == 2
    assert a["rate"] == pytest.approx(0.2)
    b = g[g["rank"] == "Bランク"].iloc[0]
    assert b["rate"] == pytest.approx(0.5)
    assert set(g["rank"]) == {"Aランク", "Bランク"}   # 无等级被排除


def test_stockout_rate_empty_or_missing_cols():
    assert ir.stockout_rate_by_rank(pd.DataFrame({"x": [1]})).empty
