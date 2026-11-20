"""自建站上架（page54）· 口径 / 上传检查 / Excel 往返（Boss 2026-11-20）。"""
import io

from openpyxl import load_workbook

from shared.shopify_listing import spec as S
from shared.shopify_listing import validate as V
from shared.shopify_listing import workbook as W

ALL_CAT = {t for _, (t1, subs) in S.CATEGORIES.items() for t in [t1, *subs.values()]}


def _exp(row):
    return {"vendor": row["vendor"], "brand": row["brand"], "brand_display": row["brand_display"],
            "price_usd": row["price_usd"], "brand_tags": ["brand-kao"]}


def _chk(row, **kw):
    base = dict(existing_jans=set(), existing_handles=set(), live_cat_tags=ALL_CAT, dup_handles=set())
    base.update(kw)
    orig = next((x for x in W.samples() if x["jan"] == row.get("jan")), row)   # 预期 = 下载时的原值
    return V.check_row(row, _exp(orig), **base)


def test_price_usd():
    assert S.price_usd(300) == 5.99            # 07 规则第 7 章示例
    assert S.price_usd(6 * 55.8) == 6.99       # 刚好整数 → 下一个 .99（同 us_price_from_definition_cost.py）
    assert S.price_usd(1) is None and S.price_usd(None) is None and S.price_usd("") is None


def test_slug_and_display():
    assert S.slug("P&G Febreze Car Clip 2.2mL") == "p-g-febreze-car-clip-2-2ml"
    assert S.slug("Bioré UV") == "biore-uv"
    assert S.display_name("Kao", "Biore UV") == "Kao Biore UV"
    assert S.display_name("IDA Laboratories", "Canmake") == "Canmake"


def test_samples_pass():
    for s in W.samples():
        res = _chk(dict(s))
        assert res["errors"] == [], res["errors"]


def _errs(row, **kw):
    return {f for f, _ in _chk(row, **kw)["errors"]}


def test_errors_caught():
    s = W.samples()[0]
    assert "title" in _errs({**s, "title": "Kao Curel Bodywash Bioré 340ml"})
    assert "title" in _errs({**s, "title": "Kao Curel Body Wash x3"})
    assert "handle" in _errs({**s, "handle": "Kao_Curel"})
    assert "l2" in _errs({**s, "l2": "Makeup", "l1": "Lifestyle"})
    assert "description" in _errs({**s, "description": "It will cure dry skin."})
    assert "ingredients" in _errs({**s, "ingredients": "待查"})
    assert "geo_us" in _errs({**s, "geo_us": "Ships from Japan."})
    assert "brand_subtitle" in _errs({**s, "brand_subtitle": "KAO Japanese brand"})
    assert "price_usd" in _errs({**s, "price_usd": 1.99})
    assert "vendor" in _errs({**s, "vendor": "Kao Corporation"})
    assert "jan" in _errs(s, existing_jans={s["jan"]})
    assert "handle" in _errs(s, existing_handles={s["handle"]})
    assert "tags" in _errs({**s, "tags": "sale-blackfriday"})
    assert "tags" in _errs({**s, "tags": "concern-made-up"})
    assert "seo_title" in _errs({**s, "seo_title": "x" * 71})
    assert "food_allergen" in _errs({**s, "page_type": "food"})
    assert "jan" in _errs({**s, "jan": "4.9013E+12"})


def test_not_candidate_and_dup_jan():
    s = W.samples()[0]
    res = V.check_all([s, dict(s)], {}, existing_jans=set(), existing_handles=set(), live_cat_tags=ALL_CAT)
    msgs = [m for f, m in res[0]["errors"] if f == "jan"]
    assert any("候选" in m for m in msgs) and any("多行" in m for m in res[1]["errors"][-1][1:])
    assert any(f == "handle" for f, _ in res[0]["errors"])       # 同表 handle 重复


def test_final_tags():
    s = W.samples()[0]
    tags = V.final_tags(s, {"brand_tags": ["brand-kao", "brand-curel"]})
    assert tags[:2] == ["cat-beauty", "cat-beauty-personal-care"]
    assert "brand-curel" in tags and "template-standard" in tags and "item-body-wash" in tags
    assert len(tags) == len(set(tags))


def test_workbook_roundtrip():
    s = W.samples()[0]
    cand = {k: s.get(k) for k in S.PRE_KEYS + ["nst_name", "brand_display", "stock"]}
    data = W.build([cand])
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames[0] == "README" and W.SHEET in wb.sheetnames
    assert wb["README"].max_row > 40
    ws = wb[W.SHEET]
    keys = [c.value for c in ws[1]]
    assert keys == S.COL_KEYS
    assert ws.cell(2, 1).value == s["jan"] and ws.cell(2, 1).number_format == "@"
    assert ws.cell(2, keys.index("title") + 1).value is None      # AI 列留空
    for k in S.COL_KEYS:                                             # 模拟 AI 填好
        if k not in S.PRE_KEYS:
            ws.cell(2, keys.index(k) + 1, s.get(k))
    buf = io.BytesIO()
    wb.save(buf)
    rows = W.read(buf.getvalue())
    assert len(rows) == 1 and rows[0]["jan"] == s["jan"] and rows[0]["title"] == s["title"]
    assert _chk(rows[0])["errors"] == []


def test_vendor_open_when_not_in_brand_table():
    s = dict(W.samples()[0])
    exp = {"vendor": "", "brand": "", "brand_display": "", "price_usd": s["price_usd"]}
    kw = dict(existing_jans=set(), existing_handles=set(), live_cat_tags=ALL_CAT, dup_handles=set())
    r = V.check_row({**s, "vendor": "Kao", "brand": "Curel"}, exp, **kw)
    assert r["errors"] == [] and any(f == "vendor" for f, _ in r["warns"])     # AI 填的厂商 → 人工确认
    r = V.check_row({**s, "vendor": "", "brand": ""}, exp, **kw)
    assert any(f == "vendor" for f, _ in r["errors"])                          # 没填 → 错
    r = V.check_row({**s, "vendor": "KOSÉ", "brand": ""}, exp, **kw)
    assert any("重音" in m for _, m in r["errors"])
    assert "brand-curel" in V.final_tags({**s, "vendor": "Kao", "brand": "Curel"}, exp)
    from shared.shopify_listing.shopify import build_input
    inp = build_input({**s, "vendor": "Kao", "brand": "Curel"}, exp)
    assert inp["vendor"] == "Kao" and inp["status"] == "DRAFT"
    assert inp["variants"][0]["inventoryPolicy"] == "DENY" and inp["variants"][0]["inventoryItem"]["tracked"]
    assert {"namespace": "smikie", "key": "brand_display", "type": "single_line_text_field",
            "value": "Kao Curel"} in inp["metafields"]


def test_workbook_vendor_cells_white_when_unknown():
    s = W.samples()[0]
    known = {k: s.get(k) for k in S.PRE_KEYS + ["nst_name", "brand_display", "stock"]}
    unknown = {**known, "jan": "4900000000001", "vendor": "", "brand": "", "brand_display": ""}
    ws = load_workbook(io.BytesIO(W.build([known, unknown])))[W.SHEET]
    col = [c.value for c in ws[1]].index("vendor") + 1
    assert ws.cell(2, col).fill.fgColor.rgb.endswith("E5E7EB")       # 已定 → 灰
    assert not str(ws.cell(3, col).fill.fgColor.rgb).endswith("E5E7EB")   # 未定 → 白
