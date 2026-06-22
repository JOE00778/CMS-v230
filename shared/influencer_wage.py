"""網紅(PHライブ配信バイト)工资 計算 + 直播名称→人 マッチング（純関数·DB/Streamlit 不要）.

page 33「💸 网红工资」が使う。Shopee ライブ配信の総エクスポート(全網紅混在)を
直播名称で人ごとに振り分け、各人の労働時間・確定売上を集計して給与を算出する。

計算式は フィリピンバイト料-*.xlsx「計算表」を踏襲（Boss 2026-06-22 · 実ファイル逆算）:
  給与($)      = round(総時間h × 時給, 0)              時給は人ごと(名册)
  奖励(peso)   = min(売上,閾値)×r1 + max(売上-閾値,0)×r2   階梯抽成·既定 20万peso/1%/1.5%
  奖励($)      = 奖励peso / php_usd                      php_usd=rate(手动)
  Total($)     = 給与$ + 奖励$
  含手续费($)  = Total$ × fee(1.045)
  日本円       = ×usd_jpy(155)
売上 = Σ「销售金额(已确认订单)」 · 総時間 = Σ「时长」(HH:MM:SS)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class WageParams:
    """汇率(rate)は当時状況で手动入力。階梯抽成は既定値(必要なら可変)。"""
    php_usd: float = 61.49        # peso/$ レート（手动）
    usd_jpy: float = 155.0        # $/円（手动）
    fee: float = 1.045            # 含手续费倍率（+4.5%）
    tier_threshold: float = 200000.0  # 階梯閾值(peso)
    tier1_rate: float = 0.01      # 閾值以内 1%
    tier2_rate: float = 0.015     # 閾值超过部分 1.5%


def parse_duration_hours(s) -> float:
    """'08:14:00' / '1:25:23' / '00:00:11' → 小时(float)。空/异常 → 0.0。"""
    if s is None:
        return 0.0
    s = str(s).strip()
    if not s:
        return 0.0
    parts = s.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    while len(nums) < 3:
        nums.insert(0, 0.0)  # 不足分は時を補う（'MM:SS' → 'HH:MM:SS'）
    h, m, sec = nums[-3], nums[-2], nums[-1]
    return h + m / 60.0 + sec / 3600.0


def parse_peso(s) -> float:
    """'₱48,672' / '48,672' / 48672 / '' → float。記号·桁区切り·空白を除去。異常 → 0.0。"""
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    cleaned = re.sub(r"[^\d.\-]", "", str(s))
    if cleaned in ("", "-", ".", "-."):
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def match_influencer(live_name, roster_keywords) -> str | None:
    """直播名称 を roster_keywords({name: [kw,...]}) に **小文字部分一致** で照合。

    複数キーワードがヒットした場合は **最長キーワード優先**（'bel' より 'bella'）。
    どの人にも当たらなければ None（呼び出し側で手动指派）。
    """
    if not live_name:
        return None
    low = str(live_name).lower()
    best = None
    best_len = 0
    for name, kws in roster_keywords.items():
        for kw in kws or []:
            k = str(kw).lower().strip()
            if k and k in low and len(k) > best_len:
                best, best_len = name, len(k)
    return best


def incentive_peso(sales_peso: float, params: WageParams) -> float:
    """階梯抽成(peso)。min(売上,閾値)×r1 + max(売上-閾値,0)×r2。"""
    th = params.tier_threshold
    base = min(sales_peso, th) * params.tier1_rate
    extra = max(sales_peso - th, 0.0) * params.tier2_rate
    return base + extra


def compute_wage(*, total_hours: float, sales_peso: float, hourly_wage: float,
                 params: WageParams) -> dict:
    """1 人分の給与計算。戻り値は全項目($/peso/円)の dict。"""
    salary_usd = round(total_hours * hourly_wage, 0)
    inc_peso = incentive_peso(sales_peso, params)
    inc_usd = (inc_peso / params.php_usd) if params.php_usd else 0.0
    total_usd = salary_usd + inc_usd
    total_fee_usd = total_usd * params.fee
    return {
        "salary_usd": salary_usd,
        "incentive_peso": inc_peso,
        "incentive_usd": inc_usd,
        "total_usd": total_usd,
        "total_fee_usd": total_fee_usd,
        "total_jpy": total_usd * params.usd_jpy,
        "total_fee_jpy": total_fee_usd * params.usd_jpy,
    }
