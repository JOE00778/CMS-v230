"""发注引擎 可调参数 · 月完売率发注策略阈值（系统参数设定·密码保护）。

存 JSON 于可写挂载 data/files，缺失回退默认（Boss 2026-05-26）：
  完売率 ≥high → factor_high（加大订货）/ ≥low → factor_mid（正常补）/ <low → factor_low（减少）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT = {
    "high": 0.9, "low": 0.5,
    "factor_high": 1.5, "factor_mid": 1.0, "factor_low": 0.5,
}
_PATH = Path(os.environ.get("ORDER_SELLTHROUGH_PARAMS",
                            "data/files/order_sellthrough_params.json"))


def load_sellthrough_params() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return {**_DEFAULT, **json.load(f)}
    except Exception:
        return dict(_DEFAULT)


def save_sellthrough_params(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump({k: float(v) for k, v in d.items() if k in _DEFAULT}, f)
