"""合规判定引擎单测(纯函数,无 DB)。"""
from __future__ import annotations

from shared.compliance_engine import check_ingredients, check_keywords, judge

KW = [
    {"country": "ALL", "category": "drug", "pattern": r"第\s*[123一二三]\s*類医薬品|要指導医薬品",
     "severity": "red", "note": "OTC药品类别词"},
    {"country": "ALL", "category": "drug", "pattern": "医薬品", "severity": "red", "note": "医薬品字样"},
    {"country": "US", "category": "category-ban", "pattern": r"\bSPF\s*\d+|日焼け止め|\bsunscreen\b",
     "severity": "red", "note": "防晒=US OTC drug"},
    {"country": "ALL", "category": "soft-claim", "pattern": r"\bwhitening\b|美白",
     "severity": "yellow", "note": "美白宣称观察级"},
    {"country": "ALL", "category": "dg", "pattern": r"スプレー|\baerosols?\b",
     "severity": "info", "note": "气雾剂运输危险品"},
]
ING = [
    {"country": "US", "ingredient": "Mercury compounds", "match_terms": ["mercury", "水銀"],
     "rule_type": "restricted", "condition_note": "痕量<1ppm", "source_ref": "21 CFR 700.13", "source": "eCFR"},
    {"country": "PH", "ingredient": "Hydroquinone", "match_terms": ["hydroquinone", "ハイドロキノン"],
     "rule_type": "prohibited", "condition_note": None, "source_ref": "ACD Annex II", "source": "CosIng-bootstrap"},
]


def test_quasi_drug_not_flagged():
    """医薬部外品不报警(Boss 2026-07-22 口径)。"""
    r = judge(KW, ING, "PH", {"商品名(日)": "薬用ではない 医薬部外品 クリーム"}, None)
    assert not any(h["category"] == "drug" for h in r["hits"])


def test_otc_drug_word_red_everywhere():
    for country in ("US", "PH", "CA"):
        r = judge(KW, ING, country, {"商品名(日)": "ロート 第2類医薬品 目薬"}, None)
        assert r["verdict"] == "red"


def test_sunscreen_red_only_us():
    fields = {"商品名(日)": "ビオレUV 日焼け止め SPF50"}
    assert judge(KW, ING, "US", fields, None)["verdict"] == "red"
    assert judge(KW, ING, "PH", fields, None)["verdict"] == "green"


def test_prohibited_ingredient_red_and_restricted_yellow():
    r = judge(KW, ING, "PH", {"商品名(EN)": "Cream"}, "water, glycerin, Hydroquinone, fragrance")
    assert r["verdict"] == "red" and r["ingredient_checked"]
    r2 = judge(KW, ING, "US", {"商品名(EN)": "Cream"}, "water, mercury compound")
    assert r2["verdict"] == "yellow"
    assert any("700.13" in h["note"] for h in r2["hits"])


def test_ingredient_in_title_caught_without_ingredient_text():
    """成分未收录但品名写「ハイドロキノン配合」也要抓到。"""
    r = judge(KW, ING, "PH", {"商品名(日)": "ハイドロキノン配合 美容クリーム"}, None)
    assert not r["ingredient_checked"]
    assert any(h["kind"] == "成分" for h in r["hits"])
    assert r["verdict"] == "red"


def test_soft_claim_yellow_and_info_only_green_verdict():
    assert judge(KW, ING, "CA", {"商品名(EN)": "Whitening essence"}, "water")["verdict"] == "yellow"
    # info 级(气雾剂)不该把 verdict 抬到 yellow
    assert judge(KW, ING, "CA", {"商品名(日)": "ヘアスプレー"}, "water")["verdict"] == "green"


def test_hits_sorted_red_first():
    r = judge(KW, ING, "US", {"商品名(日)": "美白 スプレー 日焼け止め SPF30"}, None)
    sevs = [h["severity"] for h in r["hits"]]
    assert sevs == sorted(sevs, key=lambda s: {"red": 0, "yellow": 1, "info": 2}[s])


def test_bad_regex_skipped():
    bad = [{"country": "ALL", "category": "drug", "pattern": "([", "severity": "red", "note": "x"}]
    assert check_keywords(bad, "US", {"f": "text"}) == []


def test_empty_everything_green():
    r = judge([], [], "US", {"商品名(日)": ""}, None)
    assert r["verdict"] == "green" and r["hits"] == [] and not r["ingredient_checked"]


def test_check_ingredients_country_filter():
    assert check_ingredients(ING, "CA", "hydroquinone cream", None) == []
