"""公司对日元固定汇率表 · 严格对齐 NetSuite 通貨マスタ.

来源: NetSuite「為替レート」管理画面 截图 (発効日: 2026-04-30)
Boss 修正:
  - PHP: 2.36 → 2.4
  - USD: 出口汇率 150（2026-04 由 145 调整 · NetSuite の 160 は進口側レート）
  - KRW: 0.1 → 0.095（2026-07-08 · NST=公司 統一 · nst.currency_rate ミラー値に合わせる）

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
    "KRW": 0.095,      # 大韓民国ウォン (2026-07-08 Boss: NST=公司 統一 · nst.currency_rate ミラー値に合わせ 0.1→0.095)
    "BRL": 29.03,      # ブラジル
}

# 国コード → 通貨コード。
# ⚠️ FX_TO_JPY のキーは **通貨コード**（PHP/VND…）。国コード（PH/VN…）で直接
# 引くと全件 miss して fillna(1.0) に落ち、「現地通貨 1 = 1 円」という壊れた
# 換算になる（2026-07-29 に page14 で実際に発生していた · VND は 180 倍過大）。
# 国から円レートを得る時は必ず country_to_jpy() を使うこと。
COUNTRY_TO_CURRENCY: dict[str, str] = {
    "JP": "JPY", "PH": "PHP", "TW": "TWD", "MY": "MYR", "SG": "SGD",
    "VN": "VND", "TH": "THB", "ID": "IDR", "KR": "KRW", "BR": "BRL",
    "CN": "CNY", "US": "USD",
}


def country_to_jpy(country: str | None) -> float | None:
    """国コード → 1 現地通貨あたりの円。未知なら **None**（1.0 に落とさない）。

    None を返すのは意図的。呼び出し側で「換算できなかった件数」を可視化させ、
    黙って等価換算するのを防ぐ。
    """
    cur = COUNTRY_TO_CURRENCY.get((country or "").upper())
    return FX_TO_JPY.get(cur) if cur else None


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


def nst_monthly_rates(conn, currency: str, months) -> dict[str, float]:
    """各月「月末時点で適用中」の NST 三金レート（1 外貨 = X 円）を返す。

    nst.currency_rate（基準通貨=日本円）を 1 クエリで読み、当月更新の無い月は
    直前レートを沿用（page36 月度マトリクスと同口径 · Boss 2026-07-10:
    月次換算は固定値でなく当月レートを使う）。テーブルが読めない（ローカル
    SQLite 等）/ 該当通貨なしの場合は FX_TO_JPY の固定値で全月埋める。
    """
    iso = currency.upper()
    fallback = FX_TO_JPY.get(iso, 0.0)
    months = [str(m)[:7] for m in months]
    rows: list[tuple[str, float]] = []
    try:
        cur = conn.execute(
            "SELECT effective_date, exchange_rate FROM nst.currency_rate "
            "WHERE base_currency_id = '1' "
            "AND (tx_currency LIKE ? OR tx_currency LIKE ?) "
            "ORDER BY effective_date",
            (f"%{FX_NAMES_JA.get(iso, iso)}%", f"%{iso}%"))
        rows = [(str(r[0])[:10], float(r[1])) for r in cur.fetchall()]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    if not rows:
        return {m: fallback for m in months}
    out: dict[str, float] = {}
    for m in months:
        y, mo = int(m[:4]), int(m[5:7])
        nxt = f"{y + (mo == 12)}-{(mo % 12) + 1:02d}-01"
        appl = [r for d, r in rows if d < nxt]
        out[m] = appl[-1] if appl else fallback
    return out


def to_jpy(amount: float, currency: str) -> float:
    """外币金额 → JPY."""
    rate = FX_TO_JPY.get(currency.upper(), 0.0)
    return amount * rate


def fmt(amount: float, currency: str) -> str:
    """格式化: ₱1,234 / ¥56,789"""
    sym = FX_SYMBOLS.get(currency.upper(), currency.upper() + " ")
    return f"{sym}{amount:,.0f}"
