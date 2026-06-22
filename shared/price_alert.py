"""店铺日次 价格/毛利 预警ロジック（純関数 · DB/Streamlit 不要）.

page 05「🏪 店铺毛利 → ⚠️ 价格预警」が使う。各店舗の「当日」を「近7日平均」と比べて判定。
閾値は呼び出し側で可変（page 上の number_input）。色は 赤 > 黄 > OK の優先度。

規則（Boss 2026-06-22）:
  R1 当日毛利率 < 近7日均毛利率 − dev_pp                                   → 赤
  R2 当日销量 > 近7日均销量 ×(1+qty_up%) かつ 当日毛利率 < 近7日均 − mgn_drop_pp → 赤（掩盖式降价冲量）
  R3 当日毛利率 < red_line%                                                 → 赤（绝对红线·baseline 不要）
  R4 当日总收益 > 近7日均收益 だが 当日毛利率 < 近7日均毛利率                → 黄（收益涨·毛利率没跟上＝毛利没同步涨）
近7日データが無い店舗（新規等）は R1/R2/R4 判定不可 → R3 のみ評価。
"""
from __future__ import annotations

from dataclasses import dataclass

ALERT_RED = "red"
ALERT_YELLOW = "yellow"
ALERT_OK = "ok"

# 規則 ID（表示ラベルは page 側）
R1 = "R1"  # 跌破近7日均
R2 = "R2"  # 降价冲量
R3 = "R3"  # 毛利率破红线
R4 = "R4"  # 收益涨·毛利未同步

# 色優先度（数字大ほど深刻）
_SEVERITY = {ALERT_OK: 0, ALERT_YELLOW: 1, ALERT_RED: 2}


@dataclass(frozen=True)
class AlertParams:
    """全て百分率値（45.0 = 45%、100.0 = 100%、pp も百分点の数値）。"""
    dev_pp: float = 5.0          # R1 偏離（pp）
    qty_up_pct: float = 100.0    # R2 销量上涨（%）
    mgn_drop_pp: float = 5.0     # R2 毛利率下降（pp）
    red_line_pct: float = 45.0   # R3 红线毛利率（%）


def severity(status: str) -> int:
    return _SEVERITY.get(status, 0)


def evaluate(*, cur_margin, avg7_margin, cur_qty, avg7_qty, cur_rev, avg7_rev,
             params: AlertParams, has_baseline: bool) -> tuple[str, list[str]]:
    """1 店舗・当日の判定。margin は % 値（59.4 = 59.4%）。

    has_baseline=False（近7日に売上データ無し）なら baseline 比較系（R1/R2/R4）は
    スキップし、絶対红线 R3 のみ評価する。
    戻り値: (status, fired)  fired は発火した規則 ID リスト。
    """
    fired: list[str] = []

    # R3 — 绝对红线（baseline 不要）
    if cur_margin is not None and cur_margin < params.red_line_pct:
        fired.append(R3)

    if has_baseline and avg7_margin is not None:
        # R1 — 跌破近7日均 dev_pp 以上
        if cur_margin is not None and cur_margin < avg7_margin - params.dev_pp:
            fired.append(R1)
        # R2 — 销量翻倍 かつ 毛利率跌 mgn_drop_pp 以上（降价冲量）
        qty_up = (avg7_qty is not None and avg7_qty > 0 and cur_qty is not None
                  and cur_qty > avg7_qty * (1 + params.qty_up_pct / 100.0))
        mgn_drop = (cur_margin is not None
                    and cur_margin < avg7_margin - params.mgn_drop_pp)
        if qty_up and mgn_drop:
            fired.append(R2)
        # R4 — 收益涨但毛利率没跟上（毛利没同步涨）
        rev_up = (avg7_rev is not None and cur_rev is not None and cur_rev > avg7_rev)
        mgn_down = (cur_margin is not None and cur_margin < avg7_margin)
        if rev_up and mgn_down:
            fired.append(R4)

    # 色: R1/R2/R3 のいずれか → 赤、R4 のみ → 黄、無し → OK
    if any(r in fired for r in (R1, R2, R3)):
        status = ALERT_RED
    elif R4 in fired:
        status = ALERT_YELLOW
    else:
        status = ALERT_OK
    return status, fired
