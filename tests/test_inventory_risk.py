"""shared/inventory_risk の純ロジックテスト（凭証/DB 不要）。

風控の档分けは classify_risk に集約されているので、境界（high/low·available=0·
sold=0）を固定して回帰を防ぐ。閾値の persist も round-trip 検証。
"""
from __future__ import annotations

import pandas as pd
import pytest

from shared import inventory_risk as ir


# ---- classify_risk 境界 ----

def test_stockout_at_and_above_high():
    assert ir.classify_risk(9, 10) == ir.RISK_STOCKOUT       # 0.9 = high 境界（含む）
    assert ir.classify_risk(10, 10) == ir.RISK_STOCKOUT      # 1.0
    assert ir.classify_risk(95, 100) == ir.RISK_STOCKOUT     # 0.95


def test_normal_between_low_and_high():
    assert ir.classify_risk(5, 10) == ir.RISK_NORMAL         # 0.5 = low 境界（含む）
    assert ir.classify_risk(7, 10) == ir.RISK_NORMAL         # 0.7
    assert ir.classify_risk(89, 100) == ir.RISK_NORMAL       # 0.89 < high


def test_overstock_below_low():
    assert ir.classify_risk(4, 10) == ir.RISK_OVERSTOCK      # 0.4
    assert ir.classify_risk(0, 10) == ir.RISK_OVERSTOCK      # 売上ゼロ・在庫あり = 压库存


def test_no_data_when_no_available():
    assert ir.classify_risk(5, 0) == ir.RISK_NO_DATA
    assert ir.classify_risk(0, 0) == ir.RISK_NO_DATA
    assert ir.classify_risk(5, None) == ir.RISK_NO_DATA


def test_custom_thresholds():
    # high=0.8 / low=0.3 に変えると档が動く
    assert ir.classify_risk(85, 100, high=0.8, low=0.3) == ir.RISK_STOCKOUT
    assert ir.classify_risk(40, 100, high=0.8, low=0.3) == ir.RISK_NORMAL
    assert ir.classify_risk(20, 100, high=0.8, low=0.3) == ir.RISK_OVERSTOCK


# ---- 閾値 persist round-trip ----

def test_thresholds_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("INVENTORY_RISK_THRESHOLDS", str(tmp_path / "th.json"))
    assert ir.load_risk_thresholds() == {"high": 0.9, "low": 0.5}   # 欠如 → 既定
    ir.save_risk_thresholds({"high": 0.85, "low": 0.4, "ignored": 9})
    got = ir.load_risk_thresholds()
    assert got == {"high": 0.85, "low": 0.4}                         # 既知キーのみ


def test_load_thresholds_broken_file_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "th.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("INVENTORY_RISK_THRESHOLDS", str(p))
    assert ir.load_risk_thresholds() == {"high": 0.9, "low": 0.5}


# ---- enrich ----

def test_enrich_adds_derived_columns():
    df = pd.DataFrame([
        {"opening_qty": 5, "received_qty": 95, "qty_sold": 95, "close_qty": 5, "cost_estimate": 100},   # rate .95 → 断货
        {"opening_qty": 50, "received_qty": 50, "qty_sold": 70, "close_qty": 30, "cost_estimate": 200},  # rate .70 → 正常
        {"opening_qty": 80, "received_qty": 20, "qty_sold": 10, "close_qty": 90, "cost_estimate": 10},   # rate .10 → 压库存
        {"opening_qty": 0, "received_qty": 0, "qty_sold": 0, "close_qty": 0, "cost_estimate": 50},       # available 0 → 数据不足
    ])
    out = ir.enrich(df)
    assert list(out["risk_label"]) == [
        ir.RISK_STOCKOUT, ir.RISK_NORMAL, ir.RISK_OVERSTOCK, ir.RISK_NO_DATA]
    assert out.loc[0, "available_qty"] == 100
    assert out.loc[0, "sell_through_rate"] == pytest.approx(0.95)
    assert out.loc[2, "capital_exposure"] == 900     # 90 × 10
    assert out.loc[3, "sell_through_rate"] == 0.0     # ゼロ割回避


def test_enrich_missing_columns_safe():
    # cost_estimate 欠如でも capital_exposure=0 で落ちない
    df = pd.DataFrame([{"opening_qty": 10, "received_qty": 0, "qty_sold": 9, "close_qty": 1}])
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
