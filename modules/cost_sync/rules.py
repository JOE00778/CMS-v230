"""模块 #1 成本同步 · 业务规则引擎。

参照设计 spec：
  /Users/joe/CC/docs/superpowers/specs/2026-05-02-netsuite-cost-sync-design.md

核心：根据 NetSuite 的 Average Cost（平均原价）批量更新 Standard Cost（定义原价）。

业务规则：
- 触发条件：|diff| ≥ 3 yen OR |diff%| ≥ 2%
- 新值：std_cost_new = ceil(avg_cost)（向上取整到整日元，永远不低估成本）
- 不加 markup
- 5 类 SKIP（按优先级顺序判定）：
    1. SKIP_INACTIVE       —— 商品停产
    2. SKIP_NO_MASTER      —— 输入有该 SKU 但 item_master 查不到
    3. SKIP_AVG_ZERO       —— Average Cost 为空 / 0
    4. SKIP_STD_ZERO       —— Standard Cost 为空 / 0
    5. SKIP_BELOW_THRESHOLD —— 通过上面所有检查后差异不达阈值
- 异常告警分级（基于 |diff_pct|）：
    RED   ≥ 20%
    YELLOW 10%~20%
    NORMAL < 10%
"""
from __future__ import annotations

import math
from typing import Literal

# ============================================================
# 常量（spec § 4）
# ============================================================
THRESHOLD_YEN: float = 3.0
THRESHOLD_PCT: float = 0.02
SEVERITY_RED_PCT: float = 0.20
SEVERITY_YELLOW_PCT: float = 0.10

Action = Literal[
    "UPDATE",
    "SKIP_INACTIVE",
    "SKIP_NO_MASTER",
    "SKIP_AVG_ZERO",
    "SKIP_STD_ZERO",
    "SKIP_BELOW_THRESHOLD",
]
Severity = Literal["RED", "YELLOW", "NORMAL"]


# ============================================================
# 工具函数
# ============================================================
def ceil_yen(value: float) -> int:
    """向上取整到整日元。负数也按数学定义向上（朝 +∞）。"""
    return math.ceil(value)


def classify_severity(diff_pct: float) -> Severity:
    """按 |diff_pct| 分级。"""
    abs_pct = abs(diff_pct)
    if abs_pct >= SEVERITY_RED_PCT:
        return "RED"
    if abs_pct >= SEVERITY_YELLOW_PCT:
        return "YELLOW"
    return "NORMAL"


def _is_missing(v) -> bool:
    """空 / NaN / 非数字 / ≤ 0 都视为"缺失"。"""
    if v is None:
        return True
    try:
        f = float(v)
    except (TypeError, ValueError):
        return True
    if f != f:  # NaN
        return True
    return f <= 0


# 停产・取扱中止と見なす語（NST の handling_cd / item_rank の実際の値は 取扱中止・メーカー取扱中止 など。
# 「廃番」だけ見ていて実データでは 1 件も当たらなかった・監査 M14 2026-11-17。shared/purchase_v3.DISCONTINUED_TOKENS と同じ）
from shared.purchase_v3 import DISCONTINUED_TOKENS as INACTIVE_TOKENS  # noqa: E402 — 停産判定は 1 か所（再審査 M14）


def _is_inactive(row: dict, master: dict | None) -> bool:
    """先看 master.handling_status / item_rank，再看 row.inactive_flag。"""
    if master and any(tok in f"{master.get('handling_status') or ''} {master.get('item_rank') or ''}"
                      for tok in INACTIVE_TOKENS):
        return True
    flag = row.get("inactive_flag")
    if flag is None:
        return False
    s = str(flag).strip().lower()
    return s in ("t", "true", "1", "yes", "廃番")


# ============================================================
# 主决策函数
# ============================================================
def decide_action(row: dict, master: dict | None) -> dict:
    """对单个 SKU 行决定 action 与计算字段。

    Args:
        row: 输入 CSV 一行，期待 keys：
             internal_id, item_code, avg_cost, std_cost_old,
             inactive_flag (optional), display_name (optional)
        master: item_master 中对应的元数据；找不到则 None

    Returns:
        {
            internal_id, item_code, display_name,
            avg_cost, std_cost_old, std_cost_new (None for SKIP),
            diff (None for SKIP), diff_pct (None for SKIP),
            action, skip_reason (None for UPDATE),
            severity (None for SKIP)
        }
    """
    internal_id = str(row.get("internal_id", "")).strip()
    item_code = str(row.get("item_code", "")).strip()
    display_name = (
        row.get("display_name")
        or (master.get("display_name") if master else "")
        or ""
    )

    base = {
        "internal_id": internal_id,
        "item_code": item_code,
        "display_name": str(display_name),
        "avg_cost": _safe_float(row.get("avg_cost")),
        "std_cost_old": _safe_float(row.get("std_cost_old")),
        "std_cost_new": None,
        "diff": None,
        "diff_pct": None,
        "action": "",
        "skip_reason": None,
        "severity": None,
    }

    # ---- SKIP 优先级判定（必须按 spec § 4.3 顺序）----

    # 1. SKIP_INACTIVE
    if _is_inactive(row, master):
        base["action"] = "SKIP_INACTIVE"
        base["skip_reason"] = "商品停产（取扱中止・廃番等 或 Inactive=T）"
        return base

    # 2. SKIP_NO_MASTER
    if master is None:
        base["action"] = "SKIP_NO_MASTER"
        base["skip_reason"] = f"item_master 中找不到 item_code={item_code}"
        return base

    # 3. SKIP_AVG_ZERO
    if _is_missing(row.get("avg_cost")):
        base["action"] = "SKIP_AVG_ZERO"
        base["skip_reason"] = "Average Cost 为空 / 0 / 非数字"
        return base

    # 4. SKIP_STD_ZERO
    if _is_missing(row.get("std_cost_old")):
        base["action"] = "SKIP_STD_ZERO"
        base["skip_reason"] = "Standard Cost 为空 / 0 / 非数字"
        return base

    # ---- 走到这里：avg/std 都有效，可以算 diff ----
    avg = float(row["avg_cost"])
    std_old = float(row["std_cost_old"])
    std_new = ceil_yen(avg)
    diff = std_new - std_old
    diff_pct = diff / std_old  # std_old > 0 已通过 _is_missing 检查

    base["std_cost_new"] = std_new
    base["diff"] = diff
    base["diff_pct"] = diff_pct
    base["severity"] = classify_severity(diff_pct)

    # 5. SKIP_BELOW_THRESHOLD
    if abs(diff) < THRESHOLD_YEN and abs(diff_pct) < THRESHOLD_PCT:
        base["action"] = "SKIP_BELOW_THRESHOLD"
        base["skip_reason"] = (
            f"差异 {diff:+.2f}¥ ({diff_pct:+.2%}) 未达阈值 "
            f"(≥{THRESHOLD_YEN}¥ 或 ≥{THRESHOLD_PCT:.0%})"
        )
        # 即使 SKIP，diff/diff_pct/severity 也保留以便分析
        return base

    # 通过所有检查 → UPDATE
    base["action"] = "UPDATE"
    return base


def _safe_float(v) -> float | None:
    """把任意值转 float，失败返 None。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN 也返回 None
    except (TypeError, ValueError):
        return None
