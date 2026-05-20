"""店铺 → 负责人 映射（shopee&lazada 团队 · 「负责人毛利情况」表 + Boss 补充）。

业务规则（Boss 2026-05-20 确认）：
- 负责人毛利情况表（1月）の店舗割当 + COUPANG=尹雪莉
- 日本店（Amazon / ヤフー）は担当者統計の対象外（忽略）
- 未登録の店舗は「未分配」（Boss に確認して _SHOP_OWNER に追記 → 再デプロイ）

负责人は期ごとに変動するため、更新は本 dict を編集して SSH 一括デプロイ。
"""
from __future__ import annotations

import pandas as pd

OWNER_NA = "未分配"
OWNER_EXCLUDED = "対象外"

# 负责人 → 担当店舗（「负责人毛利情况」表 2026/1 + COUPANG）
_SHOP_OWNER: dict[str, list[str]] = {
    "邓晓庆": ["Lazada MY", "Lazada PH", "Lazada SG", "Shopee TW", "Shopee toy TW"],
    "尹雪莉": ["Shopee Cosme VN", "Shopee VN", "Smikiejapan COUPANG", "Shopee TH"],
    "刘颖":   ["Shopee J-Beauty Hub PH", "Shopee Mall PH", "Shopee PH",
              "Shopee Kurashi-Mart.PH"],
    "裴晓晗": ["Shopee BR", "Shopee Cosme SG", "Shopee Kurashi-Mart.SG", "Shopee SG",
              "Shopee kurashi_mart.BR", "Shopee Cosme PH"],
}
# shop（strip 済）→ owner の逆引き
_OWNER_BY_SHOP = {
    shop.strip(): owner for owner, shops in _SHOP_OWNER.items() for shop in shops
}

ALL_OWNERS = list(_SHOP_OWNER.keys())


def classify_owner(shop: str | None) -> str:
    """店舗名 → 负责人。日本店（Amazon/ヤフー）は対象外、未登録は未分配。"""
    if not shop:
        return OWNER_NA
    s = str(shop).strip()
    if "Amazon" in s or "ヤフー" in s:   # 日本店は担当者統計の対象外
        return OWNER_EXCLUDED
    return _OWNER_BY_SHOP.get(s, OWNER_NA)


def add_owner_column(df: pd.DataFrame, shop_col: str = "shop") -> pd.DataFrame:
    """DataFrame に 'owner' 列を付与（shop 列ベース）。新しい DataFrame を返す。"""
    if df.empty or shop_col not in df.columns:
        out = df.copy()
        out["owner"] = OWNER_NA
        return out
    out = df.copy()
    out["owner"] = out[shop_col].apply(classify_owner)
    return out
