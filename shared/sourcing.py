"""供货商采购决策（仕入先比価）· 純ロジック（DB/Streamlit 不要）.

page 34「🏢 供货商数据库」が使う。供货商报价(supplier_quote·append 留历史)から
JAN ごとに「最新报价」を取り、価格/納期/预付/起訂量 を綜合加权して最も合理的な
仕入先を判定する。閾値/权重は呼び出し側で可変。

データモデル（PG · sourcing schema · ページ側で idempotent 建表）:
  sourcing.supplier        供货商主档（起訂金額/納期/预付/启用）
  sourcing.supplier_quote  报价（supplier×jan×price×moq×lot×lead×quote_date·append）
最新报价 = (supplier, jan) ごとに quote_date 最大（同日は id 最大）。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Weights:
    """綜合加权（合計は任意·内部で正規化はしない·各項 0-1 メトリクスへ係数）。"""
    price: float = 0.70    # 価格（低いほど良）
    lead: float = 0.10     # 納期（短いほど良）
    prepay: float = 0.10   # 预付（現金払い=負担→ペナルティ）
    moq: float = 0.10      # 起訂量（多いほど柔軟性低→ペナルティ）


def latest_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """报价明細 → (supplier_name, jan) ごとの最新 1 行。

    必須列: supplier_name, jan, quote_date。id があれば同日 tie-break に使う。
    """
    if df.empty:
        return df
    d = df.copy()
    d["quote_date"] = pd.to_datetime(d["quote_date"], errors="coerce")
    _sort = ["quote_date"] + (["id"] if "id" in d.columns else [])
    d = d.sort_values(_sort)
    return (d.drop_duplicates(subset=["supplier_name", "jan"], keep="last")
            .reset_index(drop=True))


def _norm(series: pd.Series) -> pd.Series:
    """0(最良/最小)〜1(最悪/最大) に正規化。range=0 なら全て 0。欠損は中央 0.5。"""
    s = pd.to_numeric(series, errors="coerce")
    lo, hi = s.min(), s.max()
    if pd.isna(lo) or pd.isna(hi) or hi <= lo:
        return s.notna().astype(float) * 0.0 + s.isna().astype(float) * 0.5
    out = (s - lo) / (hi - lo)
    return out.fillna(0.5)


def score_group(g: pd.DataFrame, w: Weights) -> pd.DataFrame:
    """1 つの JAN に属する各仕入先の最新报价群にスコア付け（低い=推奨）。

    必須列: price。任意: lead_days, is_prepay(bool), moq。
    戻り値: g + score 列 + is_recommended(最小スコア·tie は price 最小)。
    """
    g = g.copy()
    _price_n = _norm(g["price"])
    _lead_n = _norm(g["lead_days"]) if "lead_days" in g.columns else 0.0
    _moq_n = _norm(g["moq"]) if "moq" in g.columns else 0.0
    if "is_prepay" in g.columns:
        _prepay_n = g["is_prepay"].fillna(False).astype(bool).astype(float)
    else:
        _prepay_n = 0.0
    g["score"] = (w.price * _price_n + w.lead * _lead_n
                  + w.prepay * _prepay_n + w.moq * _moq_n)
    # 推奨 = score 最小（tie は price 最小 → さらに tie は先頭）
    g = g.sort_values(["score", "price"], na_position="last")
    g["is_recommended"] = False
    if not g.empty:
        g.iloc[0, g.columns.get_loc("is_recommended")] = True
    return g


def recommend(latest: pd.DataFrame, w: Weights) -> pd.DataFrame:
    """最新报价(全 JAN) → JAN ごとにスコア付け + 推奨フラグ。"""
    if latest.empty:
        return latest
    parts = [score_group(g, w) for _, g in latest.groupby("jan", sort=False)]
    return pd.concat(parts, ignore_index=True)


# 上传报价表 → 标准列。よくある列名のエイリアスを吸収。
_ALIASES = {
    "supplier_name": ["supplier", "供货商", "仕入先", "メイン仕入先", "供应商", "vendor"],
    "jan": ["jan", "JAN", "本地SKU", "商品コード", "sku", "商品编码"],
    "item_name": ["item_name", "商品名", "display_name", "品名"],
    "price": ["price", "采购价", "仕入金額", "メイン仕入金額", "単価", "最安値", "実績原価"],
    "moq": ["moq", "起订量", "起訂量", "最低订量", "min_qty"],
    "order_lot": ["order_lot", "発注ロット", "発注lot", "ロット", "发注ロット", "lot"],
    "lead_days": ["lead_days", "納期", "纳期", "lead"],
}


def normalize_upload(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """上传 DataFrame の列名を標準スキーマへ寄せる。

    戻り値: (標準列だけの DataFrame, 解決できなかった必須列リスト)。
    必須 = supplier_name, jan, price。
    """
    df = df.rename(columns=lambda c: str(c).strip())
    lower = {str(c).strip().lower(): c for c in df.columns}
    out = pd.DataFrame()
    for std, aliases in _ALIASES.items():
        hit = None
        for a in [std] + aliases:
            if a in df.columns:
                hit = a
                break
            if a.lower() in lower:
                hit = lower[a.lower()]
                break
        if hit is not None:
            out[std] = df[hit]
    missing = [c for c in ("supplier_name", "jan", "price") if c not in out.columns]
    return out, missing
