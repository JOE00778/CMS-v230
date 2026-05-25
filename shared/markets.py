"""店铺 → 市场（地域）分类。

业务规则（Boss 2026-05-02 确认）：
- Lazada * / Shopee * → 东南亚（含巴西 Shopee BR、台湾 Shopee TW 等所有 Shopee 子站）
- *COUPANG* → 韩国
- 其他 → 日本（含 Amazon、ヤフー 等）
"""
from __future__ import annotations

import pandas as pd

MARKET_SEA = "🌏 东南亚"
MARKET_KOREA = "🇰🇷 韩国"
MARKET_JAPAN = "🇯🇵 日本"
MARKET_UNKNOWN = "smikiejapan-自建站"

ALL_MARKETS = [MARKET_SEA, MARKET_KOREA, MARKET_JAPAN, MARKET_UNKNOWN]


def classify_market(store: str | None) -> str:
    """单个店铺名 → 市场分类。空值返 UNKNOWN。"""
    if not store:
        return MARKET_UNKNOWN
    s = str(store).strip()
    if not s:
        return MARKET_UNKNOWN
    s_lower = s.lower()
    if s.startswith("Shopee") or s.startswith("Lazada"):
        return MARKET_SEA
    if "coupang" in s_lower:
        return MARKET_KOREA
    return MARKET_JAPAN


def add_market_column(df: pd.DataFrame, store_col: str = "store") -> pd.DataFrame:
    """给 DataFrame 加 'market' 列（基于 store 列）。返回新 DataFrame。"""
    if df.empty or store_col not in df.columns:
        out = df.copy()
        out["market"] = MARKET_UNKNOWN
        return out
    out = df.copy()
    out["market"] = out[store_col].apply(classify_market)
    return out
