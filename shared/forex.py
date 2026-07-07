"""公司对日元固定汇率表 · 严格对齐 NetSuite 通貨マスタ.

来源: NetSuite「為替レート」管理画面 截图 (発効日: 2026-04-30)
Boss 修正:
  - PHP: 2.36 → 2.4
  - USD: 出口汇率 150（2026-04 由 145 调整 · NetSuite の 160 は進口側レート）

汇率口径: 1 单位外币 = X 日元
基準通貨: 日本円 (JPY)
"""
from __future__ import annotations

# 1 单位外币 = X 日元
# 数据源: NetSuite 為替レート (2026-04-30) + Boss 修正
FX_TO_JPY: dict[str, float] = {
    "JPY": 1.0,        # 日本円 (基準)
    "PHP": 2.4,        # フィリピン (Boss 修正,NetSuite 默认 2.36)
    "USD": 150.0,      # 米ドル 出口汇率 (2026-04 由145调整 · NetSuite 160=进口)
    "TWD": 4.57,       # 台湾ドル
    "MYR": 36.48,      # マレーシア
    "SGD": 113.44,     # シンガポール
    "VND": 0.0055,     # ベトナム
    "THB": 4.44,       # 泰銖
    "CNY": 23.28,      # 人民元
    "KRW": 0.1,        # 大韓民国ウォン
    "BRL": 29.03,      # ブラジル
}

# 货币显示符号 + 日文名(用于首页展示对照 NetSuite)
FX_SYMBOLS: dict[str, str] = {
    "JPY": "¥", "PHP": "₱", "TWD": "NT$", "MYR": "RM", "SGD": "S$",
    "USD": "$", "VND": "₫", "THB": "฿", "CNY": "¥", "KRW": "₩", "BRL": "R$",
}

FX_NAMES_JA: dict[str, str] = {
    "JPY": "日本円", "PHP": "フィリピン (PHP)", "TWD": "台湾ドル",
    "MYR": "マレーシア (MYR)", "SGD": "シンガポール", "USD": "米ドル",
    "VND": "ベトナム (VND)", "THB": "泰銖", "CNY": "人民元",
    "KRW": "大韓民国ウォン", "BRL": "ブラジル",
}


def usd_export_rate(ym: str) -> float:
    """USD 出口レート（引数 'YYYY-MM'）· Boss 2026-07-08 口径:
    2026-04 に 145 → 150 調整。NST currencyrate の 155→160 は進口側で別物。"""
    return 150.0 if ym >= "2026-04" else 145.0


def to_jpy(amount: float, currency: str) -> float:
    """外币金额 → JPY."""
    rate = FX_TO_JPY.get(currency.upper(), 0.0)
    return amount * rate


def fmt(amount: float, currency: str) -> str:
    """格式化: ₱1,234 / ¥56,789"""
    sym = FX_SYMBOLS.get(currency.upper(), currency.upper() + " ")
    return f"{sym}{amount:,.0f}"
