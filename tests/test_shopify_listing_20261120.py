"""自建站上架（page54）· 口径 / 上传检查 / Excel 往返（Boss 2026-11-20：模板全部由 AI 填，上传时对照内部数据）。"""
import io

from openpyxl import load_workbook

from shared.shopify_listing import spec as S
from shared.shopify_listing import validate as V
from shared.shopify_listing import workbook as W
from shared.shopify_listing.shopify import build_input

ALL_CAT = {t for _, (t1, subs) in S.CATEGORIES.items() for t in [t1, *subs.values()]}
KW = dict(existing_jans=set(), existing_handles=set(), live_cat_tags=ALL_CAT, dup_handles=set())
NST_OK = {"nst": {"status": "取扱中", "inactive": False, "cost": None, "name": "x"},
          "brand": {"vendor": "Kao", "brand": "Curel", "boss_confirmed": False}}


def _s0():
    return dict(W.samples()[0])


def _chk(row, ctx=NST_OK, **kw):
    return V.check_row(row, ctx, **{**KW, **kw})


def _errs(row, ctx=NST_OK, **kw):
    return {f for f, _ in _chk(row, ctx, **kw)["errors"]}


def _warn_msgs(row, ctx=NST_OK):
    return [m for _, m in _chk(row, ctx)["warns"]]


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
        assert _chk(dict(s))["errors"] == [], _chk(dict(s))["errors"]


def test_rule_errors_caught():
    s = _s0()
    assert "title" in _errs({**s, "title": "Kao Curel Bodywash Bioré 340ml"})
    assert "title" in _errs({**s, "title": "Kao Curel Body Wash x3"})
    assert "handle" in _errs({**s, "handle": "Kao_Curel"})
    assert "l2" in _errs({**s, "l2": "Makeup", "l1": "Lifestyle"})
    assert "description" in _errs({**s, "description": "It will cure dry skin."})
    assert "ingredients" in _errs({**s, "ingredients": "待查"})
    assert "geo_us" in _errs({**s, "geo_us": "Ships from Japan."})
    assert "brand_subtitle" in _errs({**s, "brand_subtitle": "KAO Japanese brand"})
    assert "jan" in _errs(s, existing_jans={s["jan"]})
    assert "handle" in _errs(s, existing_handles={s["handle"]})
    assert "tags" in _errs({**s, "tags": "sale-blackfriday"})
    assert "tags" in _errs({**s, "tags": "concern-made-up"})
    assert "seo_title" in _errs({**s, "seo_title": "x" * 71})
    assert "food_allergen" in _errs({**s, "page_type": "food"})
    assert "jan" in _errs({**s, "jan": "4.9013E+12"})
    assert "sources" in _errs({**s, "sources": "kao official site"})


def test_jan_formats():
    s = _s0()
    assert "jan" not in _errs({**s, "jan": "14902105285920"})            # 14 位自编 JAN 不拦（Boss 2026-11-20）
    assert any("自编码" in m for m in _warn_msgs({**s, "jan": "ETERNAL-BONSAI-NV"}))
    assert "jan" in _errs({**s, "jan": "49 01"})


def test_price_rules():
    s = _s0()
    assert "price_usd" in _errs({**s, "price_usd": None})                  # 空 → 拦（在页面上填）
    assert "price_usd" in _errs({**s, "price_usd": 9.5})                   # 不是 .99
    ctx = {**NST_OK, "nst": {**NST_OK["nst"], "cost": 300}}               # 公式价 5.99
    assert any("公式价 $5.99" in m for m in _warn_msgs({**s, "price_usd": 9.99}, ctx))
    assert not any(f == "price_usd" for f, _ in _chk({**s, "price_usd": 5.99}, ctx)["warns"])
    assert any("没有原価依据" in m for m in _warn_msgs(s))                 # NST 无原価、表里也没填


def test_not_in_nst_only_warns():
    s = _s0()
    r = _chk(s, ctx={"nst": None, "brand": None})
    assert r["errors"] == []
    msgs = [m for _, m in r["warns"]]
    assert any("NST 里没有" in m for m in msgs) and any("口径表里没有" in m for m in msgs)
    r = _chk(s, ctx={**NST_OK, "nst": {**NST_OK["nst"], "status": "取扱中止"}})
    assert r["errors"] == [] and any("取扱中止" in m for _, m in r["warns"])


def test_brand_table_conflicts():
    s = _s0()
    conf = {**NST_OK, "brand": {"vendor": "Kao", "brand": "Curel", "boss_confirmed": True}}
    assert "vendor" in _errs({**s, "brand": "CUREL Body"}, conf)                       # Boss 已确认 → 不可覆盖
    assert "vendor" not in _errs({**s, "brand": "CUREL"}, conf)                         # 大小写/重音差不算冲突
    soft = {**NST_OK, "brand": {"vendor": "Kao", "brand": "Curel", "boss_confirmed": False}}
    assert "vendor" not in _errs({**s, "vendor": "KAO Corp"}, soft)
    assert any("口径表不一致" in m for m in _warn_msgs({**s, "vendor": "KAO Corp"}, soft))
    assert "vendor" in _errs({**s, "vendor": "KOSÉ"})


def test_dup_rows():
    s = _s0()
    res = V.check_all([s, dict(s)], {s["jan"]: NST_OK}, existing_jans=set(), existing_handles=set(),
                      live_cat_tags=ALL_CAT)
    assert any("多行" in m for _, m in res[1]["errors"]) and any(f == "handle" for f, _ in res[0]["errors"])


def test_final_tags_and_build_input():
    s = _s0()
    tags = V.final_tags(s)
    assert tags[:2] == ["cat-beauty", "cat-beauty-personal-care"]
    assert {"brand-kao", "brand-curel", "template-standard", "item-body-wash"} <= set(tags)
    assert len(tags) == len(set(tags))
    inp = build_input(s)
    assert inp["vendor"] == "Kao" and inp["status"] == "DRAFT" and inp["variants"][0]["price"] == "9.99"
    assert inp["variants"][0]["inventoryPolicy"] == "DENY" and inp["variants"][0]["inventoryItem"]["tracked"]
    assert {"namespace": "smikie", "key": "brand_display", "type": "single_line_text_field",
            "value": "Kao Curel"} in inp["metafields"]


def test_workbook_roundtrip():
    s = _s0()
    data = W.build([s["jan"], "14902105285920"])
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["README", W.SHEET, "字段说明", "选项", "样例", "禁用词"]
    assert wb["README"].max_row > 60
    ws = wb[W.SHEET]
    keys = [c.value for c in ws[1]]
    assert keys == S.COL_KEYS
    assert ws.cell(2, 1).value == s["jan"] and ws.cell(2, 1).number_format == "@"
    assert ws.cell(3, 1).value == "14902105285920"
    assert ws.cell(2, keys.index("title") + 1).value is None
    for k in S.COL_KEYS:                                             # 模拟 AI 填好第 1 行
        ws.cell(2, keys.index(k) + 1, s.get(k))
    ws.cell(3, 1).value = None                                        # 第 2 行清空 → 读回时跳过
    buf = io.BytesIO()
    wb.save(buf)
    rows = W.read(buf.getvalue())
    assert len(rows) == 1 and rows[0]["jan"] == s["jan"] and rows[0]["title"] == s["title"]
    assert _chk(rows[0])["errors"] == []


def test_blank_template():
    ws = load_workbook(io.BytesIO(W.build()))[W.SHEET]
    assert ws.cell(2, 1).value is None and ws.cell(2, 1).number_format == "@"
