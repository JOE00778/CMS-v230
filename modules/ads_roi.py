"""page38 广告 ROI 判定的纯计算层（doc 23）。

抽出为独立模块的唯一理由：page 文件名 `38_📣_广告状态看板.py` 以数字开头且含 emoji，
无法 import，测试够不着。这里只放不依赖 streamlit 的纯函数。

核心口径（doc 23 §3.5）：**比率类指标做周/月聚合时必须先聚合分子分母再相除**，
不能对日值取平均——低消耗日会被赋予同等权重，结果静默出错。
"""
from __future__ import annotations

import pandas as pd

# 转化样本下限：低于此值任何 ROAS/CPA 判定都是噪音。
# 出处 doc 12：Google 官方建议 tCPA 类智能出价需 ≥15 转化/月。
SAMPLE_MIN_CONVERSIONS = 15

GRAINS = ("day", "week", "month")

# 直接求和的金额/计数列；比率列一律由这些重新算出，不参与 sum。
_ADDITIVE = ["cost_usd", "impressions", "clicks", "conversions", "conversions_value"]


def safe_ratio(numerator: float, denominator: float) -> float | None:
    """除零/缺失返回 None（页面显示 `—`），绝不返回 inf/NaN。"""
    if denominator is None or numerator is None:
        return None
    try:
        if float(denominator) == 0.0:
            return None
        val = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return val if pd.notna(val) else None


def breakeven_roas(gross_margin: float) -> float | None:
    """盈亏平衡 ROAS = 1 ÷ 毛利率。毛利率非法（≤0 或 >1）时返回 None。"""
    if gross_margin is None:
        return None
    try:
        m = float(gross_margin)
    except (TypeError, ValueError):
        return None
    if m <= 0 or m > 1:
        return None
    return 1.0 / m


def add_ratio_columns(df: pd.DataFrame) -> pd.DataFrame:
    """按已聚合的分子分母算出 roas / cpa / ctr / cpc。就地新增列并返回。"""
    out = df.copy()
    pairs = {
        "roas": ("conversions_value", "cost_usd"),
        "cpa": ("cost_usd", "conversions"),
        "ctr": ("clicks", "impressions"),
        "cpc": ("cost_usd", "clicks"),
    }
    for col, (num, den) in pairs.items():
        if num in out.columns and den in out.columns:
            out[col] = [safe_ratio(n, d) for n, d in zip(out[num], out[den])]
    return out


def period_key(dates: pd.Series, grain: str) -> pd.Series:
    """把日期映射到所属期的**起始日**（周=ISO 周一，月=1 号）。"""
    d = pd.to_datetime(dates)
    if grain == "day":
        return d.dt.normalize()
    if grain == "week":
        # pandas 的 W-X 中 X 是周**结束**日；ISO 周(周一起算)= W-SUN，不是 W-MON。
        return d.dt.to_period("W-SUN").dt.start_time
    if grain == "month":
        return d.dt.to_period("M").dt.start_time
    raise ValueError(f"unknown grain: {grain!r}")


def resample(
    df: pd.DataFrame,
    grain: str,
    *,
    today: pd.Timestamp | None = None,
    drop_partial: bool = True,
) -> pd.DataFrame:
    """按粒度聚合 marketing 日次数据。

    金额/计数列求和；比率列由聚合后的分子分母重算（**不是**对日比率取平均）。

    drop_partial=True 时剔除尚未走完的当期（本周/本月）——否则末点永远向下掉，
    看起来像断崖下滑，实际只是期还没过完（doc 23 §3.5）。
    """
    if grain not in GRAINS:
        raise ValueError(f"unknown grain: {grain!r}")
    if df.empty:
        return pd.DataFrame(columns=["period", *_ADDITIVE, "roas", "cpa", "ctr", "cpc"])

    work = df.copy()
    work["period"] = period_key(work["report_date"], grain)

    cols = [c for c in _ADDITIVE if c in work.columns]
    grouped = work.groupby("period", as_index=False)[cols].sum(min_count=1)

    if drop_partial and grain != "day":
        now = pd.Timestamp(today) if today is not None else pd.Timestamp.today()
        current = period_key(pd.Series([now]), grain).iloc[0]
        grouped = grouped[grouped["period"] < current]

    return add_ratio_columns(grouped).sort_values("period").reset_index(drop=True)


def verdict(
    roas: float | None,
    breakeven: float | None,
    conversions: float | None,
    *,
    sample_min: int = SAMPLE_MIN_CONVERSIONS,
) -> tuple[str, str]:
    """返回 (level, 中文标签)。level ∈ insufficient/profit/marginal/loss/unknown。

    **样本不足优先于盈亏三档**：转化数不够时任何判定都是噪音，
    页面必须说「别看这个数」而不是显示一个误导性的红灯（doc 23 §3.2）。
    """
    if conversions is None or float(conversions or 0) < sample_min:
        return "insufficient", "样本不足·判定不可信"
    if roas is None or breakeven is None:
        return "unknown", "数据不足"
    if roas >= breakeven * 1.2:
        return "profit", "盈利"
    if roas >= breakeven:
        return "marginal", "打平·边际"
    return "loss", "亏损"


def _demo() -> None:
    """自检：聚合陷阱是本模块唯一会静默出错的地方（doc 23 §5.5）。"""
    # 高消耗低回报日 + 低消耗高回报日：日 ROAS 均值 = 2.55，加权真值 = 0.60
    df = pd.DataFrame({
        "report_date": pd.to_datetime(["2026-07-06", "2026-07-07"]),
        "cost_usd": [1000.0, 10.0],
        "impressions": [10000, 100],
        "clicks": [500, 10],
        "conversions": [10.0, 8.0],
        "conversions_value": [500.0, 50.0],
    })
    wk = resample(df, "week", today=pd.Timestamp("2026-07-20"))
    assert len(wk) == 1, wk
    got = wk["roas"].iloc[0]
    assert abs(got - 550.0 / 1010.0) < 1e-9, f"周 ROAS 应为 Σvalue/Σcost，得到 {got}"
    assert abs(got - 2.55) > 0.1, "落入了日 ROAS 取均值的陷阱"

    # 除零不产生 inf/NaN
    assert safe_ratio(1.0, 0) is None
    assert safe_ratio(None, 5) is None

    # 保本线与判定
    assert abs(breakeven_roas(0.5) - 2.0) < 1e-9
    assert breakeven_roas(0) is None
    assert verdict(9.9, 2.0, 3)[0] == "insufficient", "样本不足必须优先于盈亏档"
    assert verdict(2.5, 2.0, 20)[0] == "profit"
    assert verdict(2.1, 2.0, 20)[0] == "marginal"
    assert verdict(1.0, 2.0, 20)[0] == "loss"

    # 未走完的当期被剔除
    df2 = pd.DataFrame({
        "report_date": pd.to_datetime(["2026-06-15", "2026-07-15"]),
        "cost_usd": [100.0, 100.0],
        "impressions": [1, 1], "clicks": [1, 1],
        "conversions": [1.0, 1.0], "conversions_value": [1.0, 1.0],
    })
    m = resample(df2, "month", today=pd.Timestamp("2026-07-27"))
    assert list(m["period"].dt.month) == [6], f"7 月未结束应剔除，得到 {list(m['period'])}"
    print("ads_roi self-check ok")


if __name__ == "__main__":
    _demo()
