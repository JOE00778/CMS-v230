"""库存风控 · 周转率（库存月数）による在庫リスク分档。

リスク判定 = **当前库存 ÷ 月销量 = 库存月数**（何ヶ月で売り切れるか）を閾値と比較し補货要否を判断:
  库存月数 < 补货线   → 🔴 断货风险（要补货·在庫が薄い）
  库存月数 > 压库存线 → 🟡 压库存（卖得慢·在庫が厚い）
  中间               → 🟢 正常
  月销 = 0: 在庫あり → 压库存（売れ残り）/ 在庫 0 → 数据不足。

完売率（= sold/(opening+received)）は **発注結果の参考指標** であり分档には使わない。
Boss 2026-06-04 訂正: 風控は周転率（库存月数）で判断する。完売率は「今月の発注量が妥当
だったか」を見る結果指標にすぎない。

当前库存 = JD 当天手持（inventory_snapshot 最新 · 弁天 / 在途は含めない）· 月销量 = 直近月 sold。
発注量・仕入先選択は責務外 → 発注AI v2（page25·唯一の下单引擎）。
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

# 库存月数の閾値（Boss が随時調整·page18 の expander）
_DEFAULT_THRESHOLDS = {"reorder_months": 1.0, "overstock_months": 3.0}


def _thresholds_path() -> Path:
    return Path(os.environ.get("INVENTORY_RISK_THRESHOLDS",
                               "data/files/inventory_risk_thresholds.json"))


def load_risk_thresholds() -> dict:
    """{reorder_months, overstock_months} を返す。欠如/壊れは既定（1.0 / 3.0）。"""
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


def stock_months(stock, monthly_sold):
    """库存月数 = 当前库存 / 月销量。月销 ≤ 0 → None（無限·別扱い）。純関数。"""
    try:
        sold = float(monthly_sold)
    except (TypeError, ValueError):
        return None
    if sold <= 0:
        return None
    try:
        return max(float(stock), 0.0) / sold
    except (TypeError, ValueError):
        return None


def classify_risk(stock, monthly_sold, *,
                  reorder_months: float = 1.0, overstock_months: float = 3.0) -> str:
    """库存月数を閾値と比較してリスクラベル。純関数。

    月销 = 0: 在庫あり → 压库存（売れ残り）/ 在庫 0 → 数据不足。
    境界は「正常」側に含める（= reorder / = overstock は 正常）。
    """
    m = stock_months(stock, monthly_sold)
    if m is None:   # 月销 = 0
        try:
            return RISK_OVERSTOCK if float(stock) > 0 else RISK_NO_DATA
        except (TypeError, ValueError):
            return RISK_NO_DATA
    if m < reorder_months:
        return RISK_STOCKOUT
    if m > overstock_months:
        return RISK_OVERSTOCK
    return RISK_NORMAL


def inventory_turnover(monthly_sold, current_stock) -> float:
    """库存周转率 = 月销量 / 当前库存（库存月数の逆数·SKU 360 用）。純関数。

    库存 ≤ 0（在庫なし）→ 0.0。
    """
    try:
        stock = float(current_stock)
    except (TypeError, ValueError):
        return 0.0
    if stock <= 0:
        return 0.0
    sold = float(monthly_sold) if monthly_sold is not None else 0.0
    return sold / stock


def enrich(df, thresholds: dict | None = None):
    """DataFrame に派生列を付与して返す（page18 はこれだけ呼ぶ）。

    入力列: opening_qty / received_qty / qty_sold / current_stock / cost_estimate
    付与列: available_qty / sell_through_rate(参考) / stock_months / risk_label / capital_exposure
    """
    import pandas as pd

    th = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}
    ro, ov = th["reorder_months"], th["overstock_months"]
    out = df.copy()
    for col in ("opening_qty", "received_qty", "qty_sold", "current_stock",
                "cost_estimate", "close_qty"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0)
        else:
            out[col] = 0.0

    # 完売率（参考指标·分档には使わない）
    out["available_qty"] = out["opening_qty"] + out["received_qty"]
    denom = out["available_qty"].replace(0, pd.NA)
    out["sell_through_rate"] = (out["qty_sold"] / denom).fillna(0).astype(float)

    # 库存月数 + リスク分档（当前库存 vs 月销量）
    out["stock_months"] = [
        (lambda m: m if m is not None else 0.0)(stock_months(s, q))
        for s, q in zip(out["current_stock"], out["qty_sold"])
    ]
    out["risk_label"] = [
        classify_risk(s, q, reorder_months=ro, overstock_months=ov)
        for s, q in zip(out["current_stock"], out["qty_sold"])
    ]
    # 资金占用 = 当前库存 × 定義原価（压库存 = 圧迫資金）
    out["capital_exposure"] = out["current_stock"] * out["cost_estimate"]
    return out
