"""発注AI v3（可售天数基準）の計算検証.

原典『订货规则与数据说明』2026-09-15 の算例・規則をそのままテストにする。
"""
from __future__ import annotations

import math

import pytest

from shared import purchase_v3 as pv3


# ── 原典 §18 の完全な算例 ─────────────────────────────────
def test_doc_worked_example():
    """前30天42 / 可售18日 / 在途30 / 箱規10 / 単価500 → 5箱50個¥25,000・75.14日."""
    r = pv3.compute_order(
        sold_30d=42,
        stock=25.2,          # 25.2 / 1.4 = 18 日（原典は可售天数 18 を直接与える）
        transit=30, pack=10, price=500, item_rank="A")
    assert r is not None
    assert r["daily"] == pytest.approx(1.4)
    assert r["current_days"] == pytest.approx(18.0)
    assert r["after_transit_days"] == pytest.approx(39.4286, abs=1e-3)
    assert r["need_units"] == pytest.approx(42.8, abs=1e-2)
    assert r["boxes"] == 5
    assert r["order_units"] == pytest.approx(50)
    assert r["order_days"] == pytest.approx(75.14, abs=1e-2)
    assert r["amount"] == pytest.approx(25000)
    # 箱規後の新規補充は 35.7 日分（50日以内）· 発注後 75.14 日 > 75 日
    assert r["status"] == pv3.STATUS_OVER_75


# ── 目標日数（原典 §11）────────────────────────────────────
@pytest.mark.parametrize("rank,expected", [
    ("A", 70), ("B", 70), ("NEW", 70), ("", 70), (None, 70),
    ("C", 35), ("Cランク", 35), ("C等级", 35), ("c", 35),
    ("CD", 70),          # C 始まりでも別ランクは 70 のまま
])
def test_target_days(rank, expected):
    assert pv3.target_days(rank) == expected


# ── 取扱中止は 2 列とも見る（Boss 2026-09-28）──────────────
@pytest.mark.parametrize("rank,handling,expected", [
    ("取扱中止", "継続品", True),        # 等級側だけに入っている（原典と同じ）
    ("A", "取扱中止", True),             # ★ 等級側は普通・経営状態だけ中止 → 原典では漏れる
    ("B", "廃盤", True),
    ("C", "終売", True),
    ("A", "継続品", False),
    ("A", None, False),
    (None, None, False),
])
def test_is_discontinued_checks_both_columns(rank, handling, expected):
    assert pv3.is_discontinued(rank, handling) is expected


# ── 発注不要 / 計算不能で None（原典 §3 §16）──────────────
def test_no_order_when_sales_zero():
    assert pv3.compute_order(sold_30d=0, stock=0, transit=0,
                             pack=10, price=500, item_rank="A") is None


def test_no_order_when_transit_already_covers_target():
    # 日均 1.0 · 在庫 40 日 + 在途 40 日 = 80 日 ≥ 70 日 → 発注しない
    r = pv3.compute_order(sold_30d=30, stock=40, transit=40,
                          pack=10, price=500, item_rank="A")
    assert r is None


def test_c_rank_uses_35_days():
    # 日均 1.0 · 在庫 40 日 → A なら発注、C なら不要
    common = dict(sold_30d=30, stock=40, transit=0, pack=10, price=100)
    assert pv3.compute_order(**common, item_rank="C") is None
    assert pv3.compute_order(**common, item_rank="A") is not None


def test_no_order_when_pack_missing():
    assert pv3.compute_order(sold_30d=30, stock=0, transit=0,
                             pack=0, price=500, item_rank="A") is None


# ── 50 日上限と箱規例外（原典 §5 §13）─────────────────────
def test_refill_capped_at_50_days():
    """在庫 0・目標 70 日でも 1 回の補充は 50 日分まで（原典 §12）。"""
    r = pv3.compute_order(sold_30d=30, stock=0, transit=0,
                          pack=1, price=10, item_rank="A")
    assert r["order_units"] == pytest.approx(50)     # 日均 1.0 × 50 日
    assert r["order_days"] == pytest.approx(50)
    assert r["status"] == pv3.STATUS_SUGGEST


def test_pack_exception_when_rounding_exceeds_50_days():
    """箱規が大きく、整箱にすると 50 日分を超える → 箱规例外（減らさない）。"""
    r = pv3.compute_order(sold_30d=30, stock=0, transit=0,
                          pack=200, price=10, item_rank="A")
    assert r["boxes"] == 1
    assert r["order_units"] == pytest.approx(200)    # 200 日分 > 50 日分
    assert r["status"] == pv3.STATUS_PACK_EXCEPTION  # 75 日超より優先


def test_missing_price_still_gives_quantity():
    """価格欠落でも数量は出す。金額は None・状態で明示（原典 §16）。"""
    r = pv3.compute_order(sold_30d=30, stock=0, transit=0,
                          pack=10, price="", item_rank="A")
    assert r["order_units"] == pytest.approx(50)
    assert r["amount"] is None
    assert r["status"] == pv3.STATUS_NO_PRICE


# ── 数値クリーニング（原典 §9.1）──────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("¥25,550", 25550), ("￥1,200", 1200), ("1,609.00", 1609.0),
    ("", 0), ("  ", 0), (None, 0), ("abc", 0), (48, 48), (585.0, 585.0),
])
def test_number_cleaning(raw, expected):
    assert pv3._num(raw) == pytest.approx(expected)


def test_jan_keeps_leading_zero():
    assert pv3.normalize_jan(" 0027084120134 ") == "0027084120134"
    assert pv3.normalize_jan("4901234567890.0") == "4901234567890"
    assert pv3.normalize_jan(None) == ""


# ── 規則読み込み: 起訂量欠落は黙って捨てない ──────────────
def test_load_rules_surfaces_missing_pack():
    rules, issues = pv3.load_rules([
        {"vendor_name": "0474 五洲", "jan": "4901872391141",
         "display_name": "X", "起订量": "60", "价格": "492"},
        {"vendor_name": "0474 五洲", "jan": "4909978215200",
         "display_name": "Y", "起订量": "", "价格": ""},
    ])
    assert rules["4901872391141"]["pack"] == 60
    assert rules["4909978215200"]["pack"] is None
    assert len(issues) == 1 and "起订量" in issues[0]["_issue"]


def test_load_rules_flags_duplicate_jan():
    rules, issues = pv3.load_rules([
        {"vendor_name": "A", "jan": "1", "起订量": "10", "价格": "1"},
        {"vendor_name": "B", "jan": "1", "起订量": "20", "价格": "2"},
    ])
    assert rules["1"]["vendor_name"] == "A"          # 先勝ち
    assert any("重複" in i["_issue"] for i in issues)


# ── 振り分けフロー（原典 §9）──────────────────────────────
def _sku(jan, **kw):
    base = {"jan": jan, "display_name": f"商品{jan}", "maker": "M",
            "item_rank": "A", "handling_cd": "継続品",
            "sold_30d": 30, "stock": 0, "transit": 0}
    return {**base, **kw}


def test_flow_buckets():
    rules, _ = pv3.load_rules([
        {"vendor_name": "V1", "jan": "1", "起订量": "10", "价格": "100"},
        {"vendor_name": "V2", "jan": "3", "起订量": "", "价格": "100"},
        {"vendor_name": "V3", "jan": "4", "起订量": "10", "价格": "100"},
    ])
    out = pv3.build_recommendations([
        _sku("1"),                                    # → 発注
        _sku("2"),                                    # 規則なし → 第2頁
        _sku("3"),                                    # 箱規なし → rule_incomplete
        _sku("4", stock=10000),                       # 在庫過多 → 発注不要
        _sku("5", handling_cd="取扱中止"),             # 中止 → どこにも出ない
        _sku("6", item_rank="取扱中止"),               # 中止（規則も無いが第2頁に出さない）
    ], rules)

    assert [r["jan"] for r in out["orders"]] == ["1"]
    assert [r["jan"] for r in out["unmatched"]] == ["2"]
    assert [r["jan"] for r in out["rule_incomplete"]] == ["3"]
    assert out["no_need"] == 1
    assert out["skipped_discontinued"] == 2


def test_unmatched_keeps_zero_sales_items():
    """販売 0 でも規則に無ければ第 2 頁に出す（照合が販売チェックより先 · 原典 §16）。"""
    out = pv3.build_recommendations([_sku("9", sold_30d=0)], {})
    assert [r["jan"] for r in out["unmatched"]] == ["9"]


def test_discontinued_never_reaches_unmatched():
    """取扱中止は仕入先照合より前に弾くので第 2 頁にも出ない（原典 §5.1）。"""
    out = pv3.build_recommendations([_sku("9", handling_cd="取扱中止")], {})
    assert out["unmatched"] == []
    assert out["skipped_discontinued"] == 1
