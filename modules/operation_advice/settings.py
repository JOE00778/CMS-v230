"""运营调整建议 阈值持久化（密码保护设定）。

阈值存 JSON 于可写挂载 data/files（CMS bind mount 唯一可写目录），
容器重启保留。读不到时回退默认（毛利 30/50% · 周转 0.3/1.0）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_DEFAULT = {"margin_low": 30.0, "margin_high": 50.0, "turn_low": 0.3, "turn_high": 1.0}
_PATH = Path(os.environ.get("OP_ADVICE_THRESHOLDS", "data/files/op_advice_thresholds.json"))


def load_thresholds() -> dict:
    """读当前阈值（缺失/损坏回退默认）。"""
    try:
        with open(_PATH, encoding="utf-8") as f:
            return {**_DEFAULT, **json.load(f)}
    except Exception:
        return dict(_DEFAULT)


def save_thresholds(d: dict) -> None:
    """写阈值（仅保存已知 4 键·转 float）。"""
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump({k: float(v) for k, v in d.items() if k in _DEFAULT}, f)
