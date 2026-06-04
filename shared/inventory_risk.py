"""库存风控 · 月完売率による在庫リスク分档（page18 库存风控の純ロジック）。

完売率 = sold / (opening + received)（全て数量）。閾値で 3 档に分ける：
  ≥high → 🔴 断货风险（売れ切れ気味·補充対象）/ ≥low → 🟢 正常 / <low → 🟡 压库存。
available=0（基準なし）→ 数据不足。

閾値は Boss が随時調整（page18 の「⚙️ 阈值设定」tab）。発注AI の系数閾値
（shared/order_settings.py）とは**独立**に持久化する（風控視点の調整が発注量に波及しないため）。

注: 発注量・仕入先選択は本モジュールの責務外（→ shared/purchase_engine.py / page25）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# リスクラベル（KPI / フィルタ / tab で共通参照）
RISK_STOCKOUT = "断货风险"
RISK_NORMAL = "正常"
RISK_OVERSTOCK = "压库存"
RISK_NO_DATA = "数据不足"
RISK_LABELS = (RISK_STOCKOUT, RISK_NORMAL, RISK_OVERSTOCK, RISK_NO_DATA)

_DEFAULT_THRESHOLDS = {"high": 0.9, "low": 0.5}


def _thresholds_path() -> Path:
    return Path(os.environ.get("INVENTORY_RISK_THRESHOLDS",
                               "data/files/inventory_risk_thresholds.json"))


def load_risk_thresholds() -> dict:
    """{high, low} を返す。ファイル欠如/壊れは既定（0.9 / 0.5）にフォールバック。"""
    try:
        with open(_thresholds_path(), encoding="utf-8") as f:
            return {**_DEFAULT_THRESHOLDS, **json.load(f)}
    except Exception:
        return dict(_DEFAULT_THRESHOLDS)


def save_risk_thresholds(d: dict) -> None:
    p = _thresholds_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({k: float(d[k]) for k in _DEFAULT_THRESHOLDS if k in d}, f)


def classify_risk(sold, available, *, high: float = 0.9, low: float = 0.5) -> str:
    """1 SKU・1 月のリスクラベル。純関数。

    available（= opening + received）が無ければ判定基準が無い → 数据不足。
    """
    try:
        avail = float(available)
    except (TypeError, ValueError):
        return RISK_NO_DATA
    if avail <= 0:
        return RISK_NO_DATA
    rate = (float(sold) if sold is not None else 0.0) / avail
    if rate >= high:
        return RISK_STOCKOUT
    if rate >= low:
        return RISK_NORMAL
    return RISK_OVERSTOCK


def enrich(df, thresholds: dict | None = None):
    """DataFrame に派生列を付与して返す（page18 はこれだけ呼ぶ）。

    入力列: opening_qty / received_qty / qty_sold / close_qty / cost_estimate
    付与列: available_qty / sell_through_rate / risk_label / capital_exposure
    """
    import pandas as pd

    th = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}
    high, low = th["high"], th["low"]
    out = df.copy()
    for col in ("opening_qty", "received_qty", "qty_sold", "close_qty", "cost_estimate"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
        else:
            out[col] = 0.0

    out["available_qty"] = out["opening_qty"] + out["received_qty"]
    denom = out["available_qty"].replace(0, pd.NA)
    out["sell_through_rate"] = (out["qty_sold"] / denom).fillna(0).astype(float)
    out["risk_label"] = [
        classify_risk(s, a, high=high, low=low)
        for s, a in zip(out["qty_sold"], out["available_qty"])
    ]
    out["capital_exposure"] = out["close_qty"] * out["cost_estimate"]
    return out
