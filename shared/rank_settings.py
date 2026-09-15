"""商品等级判定 可调参数（page07「⚙️ 判定原理与参数」tab · Boss 2026-09-15）。

存 JSON 于可写挂载 data/files，缺失/损坏回退默认（与 shared/order_settings.py 同一模式）。
默认値 = 2026-09-15 以前コードに直書きされていた値（Boss 重构 v3）。

  top_pct          売上累計占比の頭部ライン（≤ で頭部品）          0.80
  a_margin         A ランクの粗利率ライン（≥）                     0.59
  no_sales_months  直近 N ヶ月 販売数 0 → 取扱中止                 3
  stop_absorbing   現行が取扱中止なら A/B/C へ戻さない（吸収態）    True
  safety_a/b/c/stop 発注安全係数                                  1.5 / 1.0 / 0.5 / 0.0
  lead_days        進貨周期 既定（NST に無い）                     30
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_RANK_PARAMS: dict = {
    "top_pct": 0.80,
    "a_margin": 0.59,
    "no_sales_months": 3,
    "stop_absorbing": True,
    "safety_a": 1.5,
    "safety_b": 1.0,
    "safety_c": 0.5,
    "safety_stop": 0.0,
    "lead_days": 30,
}
_INT_KEYS = {"no_sales_months", "lead_days"}
_BOOL_KEYS = {"stop_absorbing"}
_PATH = Path(os.environ.get("RANK_PARAMS_PATH", "data/files/rank_params.json"))


def _coerce(d: dict) -> dict:
    out = dict(DEFAULT_RANK_PARAMS)
    for k, v in d.items():
        if k not in DEFAULT_RANK_PARAMS:
            continue
        try:
            if k in _BOOL_KEYS:
                out[k] = bool(v)
            elif k in _INT_KEYS:
                out[k] = max(1, int(v))
            else:
                out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def load_rank_params() -> dict:
    """現在の判定パラメータ。ファイル無し/壊れは既定に戻る（黙って既定は使うが上書きはしない）。"""
    try:
        with open(_PATH, encoding="utf-8") as f:
            return _coerce(json.load(f))
    except Exception:
        return dict(DEFAULT_RANK_PARAMS)


def save_rank_params(d: dict) -> dict:
    """保存して正規化後の値を返す。"""
    clean = _coerce(d)
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def safety_factor(rank: str, params: dict | None = None) -> float:
    p = params or load_rank_params()
    return {
        "Aランク": p["safety_a"], "Bランク": p["safety_b"],
        "Cランク": p["safety_c"], "取扱中止": p["safety_stop"],
    }.get(rank, p["safety_c"])
