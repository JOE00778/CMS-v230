"""shared/inventory_risk の純ロジックテスト（凭証/DB 不要）。

風控の档分け = 库存月数（当前库存/月销量）を閾値と比較。境界 / 月销0 / 在庫0 を固定して
回帰を防ぐ。完売率は参考指标で分档に使わない。
"""
from __future__ import annotations

import pandas as pd
import pytest

from shared import inventory_risk as ir


# ---- classify_risk 境界（库存月数ベース·既定 补货线1.0 / 压库存线3.0）----

def test_stockout_below_reorder_line():
    assert ir.classify_risk(0, 10) == ir.RISK_STOCKOUT       # 库存0·月销10 → 0月 < 1
    assert ir.classify_risk(5, 10) == ir.RISK_STOCKOUT       # 0.5月 < 1
    assert ir.classify_risk(9, 10) == ir.RISK_STOCKOUT       # 0.9月 < 1


def test_normal_between_lines():
    assert ir.classify_risk(10, 10) == ir.RISK_NORMAL        # 1.0月 = 补货线（含む→正常）
    assert ir.classify_risk(20, 10) == ir.RISK_NORMAL        # 2.0月
    assert ir.classify_risk(30, 10) == ir.RISK_NORMAL        # 3.0月 = 压库存线（含む→正常）


def test_overstock_above_line():
    assert ir.classify_risk(31, 10) == ir.RISK_OVERSTOCK     # 3.1月 > 3
    assert ir.classify_risk(100, 10) == ir.RISK_OVERSTOCK    # 10月


def test_zero_sales():
    assert ir.classify_risk(50, 0) == ir.RISK_OVERSTOCK      # 売れ残り（在庫あり·月销0）
    assert ir.classify_risk(0, 0) == ir.RISK_NO_DATA         # 在庫0·月销0
    assert ir.classify_risk(50, None) == ir.RISK_OVERSTOCK


def test_custom_thresholds():
    # 补货线0.5 / 压库存线2.0
    assert ir.classify_risk(4, 10, reorder_months=0.5, overstock_months=2.0) == ir.RISK_STOCKOUT  # 0.4月
    assert ir.classify_risk(15, 10, reorder_months=0.5, overstock_months=2.0) == ir.RISK_NORMAL   # 1.5月
    assert ir.classify_risk(25, 10, reorder_months=0.5, overstock_months=2.0) == ir.RISK_OVERSTOCK  # 2.5月


# ---- stock_months ----

def test_stock_months_basic():
    assert ir.stock_months(20, 10) == 2.0
    assert ir.stock_months(5, 10) == 0.5


def test_stock_months_zero_sales_is_none():
    assert ir.stock_months(50, 0) is None
    assert ir.stock_months(50, None) is None


def test_stock_months_negative_stock_floored():
    assert ir.stock_months(-5, 10) == 0.0


# ---- 閾値 persist round-trip ----

def test_thresholds_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("INVENTORY_RISK_THRESHOLDS", str(tmp_path / "th.json"))
    assert ir.load_risk_thresholds() == {"reorder_months": 1.0, "overstock_months": 3.0}
    ir.save_risk_thresholds({"reorder_months": 0.5, "overstock_months": 2.0, "ignored": 9})
    assert ir.load_risk_thresholds() == {"reorder_months": 0.5, "overstock_months": 2.0}


def test_load_thresholds_broken_file_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "th.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("INVENTORY_RISK_THRESHOLDS", str(p))
    assert ir.load_risk_thresholds() == {"reorder_months": 1.0, "overstock_months": 3.0}


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
    assert out.loc[0, "stock_months"] == pytest.approx(0.5)
    assert out.loc[2, "stock_months"] == pytest.approx(9.0)
    assert out.loc[2, "capital_exposure"] == 900     # 当前库存 90 × 10
    # 完売率は参考列として残る（分档には未使用）
    assert out.loc[0, "sell_through_rate"] == pytest.approx(1.0)   # 100/(5+95)


def test_enrich_custom_thresholds_shift_bands():
    df = pd.DataFrame([{"opening_qty": 0, "received_qty": 0, "qty_sold": 10,
                        "current_stock": 25, "cost_estimate": 0}])   # 2.5月
    assert ir.enrich(df, {"reorder_months": 1.0, "overstock_months": 3.0}).loc[0, "risk_label"] == ir.RISK_NORMAL
    assert ir.enrich(df, {"reorder_months": 1.0, "overstock_months": 2.0}).loc[0, "risk_label"] == ir.RISK_OVERSTOCK


def test_enrich_missing_current_stock_safe():
    # current_stock 欠如 → 0 扱い·月销>0 なら 库存月数0 → 断货
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
