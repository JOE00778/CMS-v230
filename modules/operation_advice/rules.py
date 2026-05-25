"""运营调整建议（B/C 档 · 基于「毛利 × 周转」双维度矩阵）

业务逻辑：
- 库存视角（健康度）已判定死钱 → 看资金占用
- 运营视角（毛利 × 周转）→ 给调价 / 降级建议
- 等级是渐变流程：A → B → C → 停售（不是一步改廃）
- 「改廃」专指品牌方产品迭代（外部信号 · page 13 处理），跟自主降级是两回事

3×3 矩阵 → 5 档建议：
              毛利低<30%      毛利中30-50%    毛利高>50%
周转低<0.3   ⬇️ 降级候选     ⚠️ 降价候选     🔥 重点降价
周转中       ⬆️ 提价候选    ✅ 维持          ⚠️ 降价候选
周转高>1.0   🔥 重点提价    ⬆️ 提价候选     ✅ 优秀

仅对 B/C 档 SKU 出建议（A 档已健康、停售已决策）

降级流程：
- B 档 + 双低 → 建议下季度降到 C
- C 档 + 双低 + 持续无销 → 建议改为停售（取扱中止）
"""
from __future__ import annotations
from typing import Literal

# 阈值（v3 默认 · Boss 可后续调）
MARGIN_LOW = 30.0       # 毛利率 % 低界
MARGIN_HIGH = 50.0      # 毛利率 % 高界
TURNOVER_LOW = 0.3      # 月周转 低界
TURNOVER_HIGH = 1.0     # 月周转 高界

AdviceType = Literal[
    "🔥 重点提价",  # 周转高 × 毛利低 — 卖得快但赚少
    "🔥 重点降价",  # 周转低 × 毛利高 — 赚得多但卖太慢
    "⬆️ 提价候选",  # 周转中高 + 毛利低中
    "⚠️ 降价候选",  # 周转中低 + 毛利中高
    "⬇️ 降级候选",  # 周转低 × 毛利低 — 该往下一档掉（B→C / C→停售）
    "✅ 维持",      # 中段平衡 / 优秀
    "—",            # A 档 / 停售 / 数据不足
]


def margin_level(gross_margin_pct: float) -> str:
    if gross_margin_pct < MARGIN_LOW: return "低"
    if gross_margin_pct < MARGIN_HIGH: return "中"
    return "高"


def turnover_level(monthly_turnover: float) -> str:
    if monthly_turnover < TURNOVER_LOW: return "低"
    if monthly_turnover < TURNOVER_HIGH: return "中"
    return "高"


def advise(rank: str, gross_margin_pct: float, monthly_turnover: float, *,
           margin_low: float = MARGIN_LOW, margin_high: float = MARGIN_HIGH,
           turn_low: float = TURNOVER_LOW, turn_high: float = TURNOVER_HIGH) -> dict:
    """给单个 SKU 出运营调整建议（阈值可由系统设置覆盖·默认 v3）"""
    if rank in ("A", "停售"):
        return {"advice": "—", "margin_lv": "-", "turnover_lv": "-",
                "reason": "A档已健康 / 停售已决策"}

    # 数据不全
    if monthly_turnover <= 0 or gross_margin_pct <= 0:
        return {"advice": "—", "margin_lv": "-", "turnover_lv": "-",
                "reason": "无销售或毛利数据"}

    m = "低" if gross_margin_pct < margin_low else ("中" if gross_margin_pct < margin_high else "高")
    t = "低" if monthly_turnover < turn_low else ("中" if monthly_turnover < turn_high else "高")

    # 5 档判定（3×3 矩阵）
    if t == "低" and m == "低":
        advice = "⬇️ 降级候选"
        # B 档 → C / C 档 → 停售
        next_rank = "C" if rank == "B" else "停售"
        reason = f"周转<{turn_low} 且 毛利<{margin_low}% — 双低，建议降到 {next_rank}"
    elif t == "低" and m == "高":
        advice = "🔥 重点降价"
        reason = f"周转<{turn_low} 但 毛利>{margin_high}% — 降价加速周转"
    elif t == "高" and m == "低":
        advice = "🔥 重点提价"
        reason = f"周转>{turn_high} 但 毛利<{margin_low}% — 提价不影响销量"
    elif t == "低" and m == "中":
        advice = "⚠️ 降价候选"
        reason = "周转低 · 毛利中 — 适度降价试水"
    elif t == "中" and m == "高":
        advice = "⚠️ 降价候选"
        reason = "周转中 · 毛利高 — 微降可冲量"
    elif t == "中" and m == "低":
        advice = "⬆️ 提价候选"
        reason = "周转中 · 毛利低 — 提价试水"
    elif t == "高" and m == "中":
        advice = "⬆️ 提价候选"
        reason = "周转高 · 毛利中 — 可适度提价"
    else:
        advice = "✅ 维持"
        reason = f"毛利{m} · 周转{t} — 平衡区"

    return {"advice": advice, "margin_lv": m, "turnover_lv": t, "reason": reason}
