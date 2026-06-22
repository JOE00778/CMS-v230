"""shared/sourcing の純ロジックテスト（DB/Streamlit 不要）.

最新报价抽出 + 綜合加权スコア + 推奨判定 + 上传列正規化 を固定。
"""
from __future__ import annotations

import pandas as pd

from shared import sourcing as sc
from shared.sourcing import Weights

_W = Weights()  # 0.7/0.1/0.1/0.1


def test_latest_quotes_picks_newest_per_supplier_jan():
    df = pd.DataFrame([
        {"supplier_name": "A", "jan": "J1", "price": 100, "quote_date": "2026-01-01"},
        {"supplier_name": "A", "jan": "J1", "price": 90, "quote_date": "2026-03-01"},  # newer
        {"supplier_name": "B", "jan": "J1", "price": 95, "quote_date": "2026-02-01"},
    ])
    out = sc.latest_quotes(df)
    assert len(out) == 2
    a = out[out["supplier_name"] == "A"].iloc[0]
    assert a["price"] == 90  # 最新を採用


def test_recommend_lowest_price_when_only_price_matters():
    latest = pd.DataFrame([
        {"supplier_name": "A", "jan": "J1", "price": 100},
        {"supplier_name": "B", "jan": "J1", "price": 80},
        {"supplier_name": "C", "jan": "J1", "price": 120},
    ])
    out = sc.recommend(latest, _W)
    rec = out[out["is_recommended"]]
    assert len(rec) == 1 and rec.iloc[0]["supplier_name"] == "B"


def test_recommend_prepay_penalty_can_flip_when_price_tie():
    # 価格同じなら预付(現金払い)がペナルティ → 掛け払いを推奨
    latest = pd.DataFrame([
        {"supplier_name": "A", "jan": "J1", "price": 100, "is_prepay": True},
        {"supplier_name": "B", "jan": "J1", "price": 100, "is_prepay": False},
    ])
    out = sc.recommend(latest, _W)
    rec = out[out["is_recommended"]].iloc[0]
    assert rec["supplier_name"] == "B"


def test_recommend_lead_penalty():
    # 価格同じ → 納期短い方を推奨
    latest = pd.DataFrame([
        {"supplier_name": "A", "jan": "J1", "price": 100, "lead_days": 30},
        {"supplier_name": "B", "jan": "J1", "price": 100, "lead_days": 5},
    ])
    out = sc.recommend(latest, _W)
    assert out[out["is_recommended"]].iloc[0]["supplier_name"] == "B"


def test_recommend_per_jan_independent():
    latest = pd.DataFrame([
        {"supplier_name": "A", "jan": "J1", "price": 100},
        {"supplier_name": "B", "jan": "J1", "price": 80},
        {"supplier_name": "A", "jan": "J2", "price": 50},
        {"supplier_name": "B", "jan": "J2", "price": 70},
    ])
    out = sc.recommend(latest, _W)
    recs = {r["jan"]: r["supplier_name"] for _, r in out[out["is_recommended"]].iterrows()}
    assert recs == {"J1": "B", "J2": "A"}


def test_single_supplier_jan_is_recommended():
    latest = pd.DataFrame([{"supplier_name": "A", "jan": "J9", "price": 999}])
    out = sc.recommend(latest, _W)
    assert out.iloc[0]["is_recommended"]


def test_price_weight_dominates_small_lead_diff():
    # 大幅安い A は納期が長くても推奨されるべき（price 権重 0.7）
    latest = pd.DataFrame([
        {"supplier_name": "A", "jan": "J1", "price": 50, "lead_days": 40},
        {"supplier_name": "B", "jan": "J1", "price": 100, "lead_days": 1},
    ])
    out = sc.recommend(latest, _W)
    assert out[out["is_recommended"]].iloc[0]["supplier_name"] == "A"


def test_normalize_upload_aliases():
    df = pd.DataFrame([{"仕入先": "Maple", "本地SKU": "4900001", "仕入金額": 680,
                        "発注ロット": 144, "納期": 7}])
    out, missing = sc.normalize_upload(df)
    assert missing == []
    assert out.iloc[0]["supplier_name"] == "Maple"
    assert out.iloc[0]["jan"] == "4900001"
    assert out.iloc[0]["price"] == 680
    assert out.iloc[0]["order_lot"] == 144
    assert out.iloc[0]["lead_days"] == 7


def test_normalize_upload_missing_required():
    df = pd.DataFrame([{"商品名": "x", "納期": 5}])
    out, missing = sc.normalize_upload(df)
    assert set(missing) == {"supplier_name", "jan", "price"}


# ---- 仕入先管理リスト 多供货商抽取 ----
def _vendor_raw():
    return pd.DataFrame([
        [None, "商品名", "単価", "ロット", None, "注文最低金額"],
        ["4900001000017", "商品A", "680", "144", None, "50000"],
        ["4900002000024", "商品B", "￥1,001", "36", None, "50000"],
        ["bad", "ゴミ行", "x", "x", None, "x"],
        ["4900001000017", "商品A重複", "999", "144", None, "50000"],  # 同JAN→先頭優先
    ])


def test_parse_vendor_sheet_basic():
    out = sc.parse_vendor_sheet("Maple", _vendor_raw())
    assert list(out["jan"]) == ["4900001000017", "4900002000024"]  # bad 行除外·重複除外
    r0 = out.iloc[0]
    assert r0["supplier_name"] == "Maple" and r0["price"] == 680.0
    assert r0["order_lot"] == 144.0 and r0["min_order_amount"] == 50000.0
    assert out.iloc[1]["price"] == 1001.0  # ￥, カンマ清洗


def test_price_priority_prefers_mitumori():
    raw = pd.DataFrame([
        ["JAN", "商品名", "希望納価", "御見積価格", "ロット"],
        ["4900001000017", "X", "873", "1001", "60"],
    ])
    out = sc.parse_vendor_sheet("NEW WIND", raw)
    assert out.iloc[0]["price"] == 1001.0  # 御見積 > 希望納価


def test_extract_skips_data_sheets():
    sheets = {
        "Maple": _vendor_raw(),
        "SUPABASE用": _vendor_raw(),      # data sheet → skip
        "一元 Data12.4更新": _vendor_raw(),  # data sheet → skip
    }
    allq, counts = sc.extract_vendor_quotes(sheets)
    assert set(allq["supplier_name"]) == {"Maple"}
    assert "SUPABASE用" not in counts and "一元 Data12.4更新" not in counts
