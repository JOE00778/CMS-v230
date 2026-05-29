"""NST vs JDL 账实对账 · 差异档位阈值（系统参数设定·密码保护）。

存 JSON 于可写挂载 data/files，缺失回退默认（Boss 2026-05-27）：
  MINOR_DIFF = |diff| <= minor_abs_threshold OR (diff/nst_qty) <= minor_pct_threshold%
  MAJOR_DIFF = 其他超过 MINOR 的差异
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT = {
    "minor_abs_threshold": 1,        # 差 N 件以内算 MINOR（默认 1）
    "minor_pct_threshold": 0.0,      # 差占 NST 账面 X% 以内算 MINOR（默认 0 = 不启用比例判定）
}
_PATH = Path(os.environ.get("JDL_RECON_PARAMS",
                            "data/files/jdl_recon_params.json"))


def load_recon_params() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return {**_DEFAULT, **json.load(f)}
    except Exception:
        return dict(_DEFAULT)


def save_recon_params(d: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def classify_status(nst_qty: float | None, jdl_qty: float | None,
                    params: dict | None = None) -> str:
    """根据当前阈值判定一行的 status。

    输入 None 表示该侧无记录：
      - nst_qty=None  → JDL_ONLY
      - jdl_qty=None  → NST_ONLY
    """
    if nst_qty is None:
        return "JDL_ONLY"
    if jdl_qty is None:
        return "NST_ONLY"
    diff = abs((nst_qty or 0) - (jdl_qty or 0))
    if diff == 0:
        return "OK"
    p = params if params is not None else load_recon_params()
    minor_abs = float(p.get("minor_abs_threshold", 1))
    minor_pct = float(p.get("minor_pct_threshold", 0))
    if diff <= minor_abs:
        return "MINOR_DIFF"
    if minor_pct > 0 and nst_qty and (diff / nst_qty) * 100 <= minor_pct:
        return "MINOR_DIFF"
    return "MAJOR_DIFF"
