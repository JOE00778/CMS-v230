import io

from openpyxl import load_workbook

from shared.banma_dims import build, grid, report_xlsx, select


def sku(i, code, spu="P1", typ="普通", w=0, d=(0, 0, 0), comb=None, cat="Export"):
    return {"sku_id": i, "code": code, "spu_id": spu, "spu_title": "t", "specification": "",
            "category_top": cat, "sku_type": typ, "is_valid": True, "weight": w,
            "length": d[0], "width": d[1], "height": d[2], "combine": comb or []}


def test_grid():
    assert grid((10, 5, 3), 2) == (10, 6, 5)
    assert grid((10, 5, 3), 1) == (10, 5, 3)
    assert grid((13, 12, 10), 12)[0] <= 30


def test_build_rules():
    skus = [
        sku("1", "4900000000001", w=300),                                  # 占位 → NST
        sku("2", "4900000000002", spu="P2", w=55, d=(8, 4, 3)),            # 实值不动
        sku("3", "4900000000003", spu="P3"),                               # NST 数值异常 → 人工
        sku("4", "4900000000004", spu="P4"),                  # NST 三边与兄弟差 >3cm
        sku("5", "4900000000005", spu="P4", d=(6, 5, 5), w=40),            # 兄弟
        sku("6", "4900000000001_2", typ="组合", comb=[{"SKUID": "1", "SKUCode": "4900000000001", "Quantity": 2}]),
        sku("7", "4900000000002_3", typ="组合", w=100,
            comb=[{"SKUID": "2", "SKUCode": "4900000000002", "Quantity": 2}]),  # 后缀≠件数
        sku("8", "4900000000009", cat="EC"),                               # 非 Export 不管
    ]
    nst = {"4900000000001": 120, "4900000000003": 6000, "4900000000004": 50}
    dims = {"4900000000001": (10, 5, 3), "4900000000004": (20, 15, 10)}
    rows, issues = build(skus, nst, dims)
    up = {r["SKU编码"]: r for r in rows}
    assert up["4900000000001"]["重量(g)"] == 120 and up["4900000000001"]["长(cm)"] == 10
    assert up["4900000000001_2"]["重量(g)"] == 240 and (up["4900000000001_2"]["长(cm)"], up["4900000000001_2"]["宽(cm)"],
                                                        up["4900000000001_2"]["高(cm)"]) == (10, 6, 5)
    assert "4900000000002" not in up and "4900000000009" not in up
    kinds = {(i["SKU编码"], i["问题"]) for i in issues}
    assert ("4900000000003", "重量待人工") in kinds
    assert ("4900000000004", "三边待人工") in kinds
    assert ("4900000000002_3", "组合品·编码与明细件数不一致") in kinds
    r2, i2 = select(rows, issues, {"重量"})
    assert all(r["长(cm)"] is None for r in r2) and {i["问题"] for i in i2} >= {"重量待人工"}
    assert "三边待人工" not in {i["问题"] for i in i2}
    wb = load_workbook(io.BytesIO(report_xlsx(r2, i2)))
    assert wb.sheetnames == ["数据", "示例", "问题清单", "导入明细"]
    vals = list(wb["数据"].iter_rows(values_only=True))
    assert vals[0][0] == "SKU编码" and vals[1][1] is None   # 成本价留空


def test_rank_scope():
    skus = [sku("1", "4900000000001"), sku("2", "4900000000002", spu="P2"),
            sku("3", "4900000000001_2", typ="组合", comb=[{"SKUID": "1", "SKUCode": "4900000000001", "Quantity": 2}]),
            sku("4", "4900000000002_2", typ="组合", comb=[{"SKUID": "2", "SKUCode": "4900000000002", "Quantity": 2}])]
    nst = {"4900000000001": 120, "4900000000002": 80}
    dims = {"4900000000001": (10, 5, 3), "4900000000002": (9, 5, 3)}
    rows, issues = build(skus, nst, dims, ranked={"4900000000001"})
    assert {r["SKU编码"] for r in rows} == {"4900000000001", "4900000000001_2"} and not issues


def test_new_items_go_to_page47():
    """NST 作成 30 日内の単品は page47 の担当 → page46 は見ない。組合品は page47 が見ないので残す。"""
    from shared.banma_dims import customs_issues, in_scope
    skus = [sku("1", "4900000000001"), sku("2", "4900000000002", spu="P2"),
            sku("3", "4900000000001_2", typ="组合", comb=[{"SKUID": "1", "SKUCode": "4900000000001", "Quantity": 2}])]
    new = {"4900000000001"}
    assert {s["code"] for s in in_scope(skus, None, new)} == {"4900000000002", "4900000000001_2"}
    rows, issues = build(skus, {"4900000000001": 120, "4900000000002": 80}, {}, None, new)
    assert "4900000000001" not in {r["SKU编码"] for r in rows} | {i["SKU编码"] for i in issues}
    assert "4900000000001" not in {i["SKU编码"] for i in customs_issues(skus, {}, [], None, new)}


def test_nst_dims():
    from shared.banma_dims import nst_dims
    assert nst_dims((3.2, 15, 4)) == (15, 4, 4)
    assert nst_dims((10, 10, 10)) is None and nst_dims((0, 5, 5)) is None and nst_dims((None, 1, 1)) is None


def test_customs_issues():
    from shared.banma_dims import customs_issues
    std = [("340111", "香皂", "Toilet soap bar, solid, for washing")]
    skus = [sku("1", "4900000000001"), sku("2", "4900000000002", spu="P2"), sku("3", "4900000000003", spu="P3"),
            sku("4", "4900000000001_3", typ="组合", comb=[{"SKUID": "1", "SKUCode": "4900000000001", "Quantity": 3}]),
            sku("5", "4900000000004", spu="P4")]
    customs = {"1": {"name_en": "Toilet soap bar, solid, for washing, 80g", "hs_code": "340111"},
               "2": {"name_en": "Pelican SoapKATE", "hs_code": "1"},
               "3": {"name_en": "Toilet soap bar, solid, for washing", "hs_code": "330499"},
               "4": {"name_en": "Toilet soap bar, solid, for washing, 80g 2set", "hs_code": "340111"}}
    got = {(i["SKU编码"], i["问题"]) for i in customs_issues(skus, customs, std, None)}
    assert got == {("4900000000002", "HS编码缺失或格式错误"), ("4900000000002", "英文名不是标准报关名"),
                   ("4900000000003", "英文名与HS不对应"), ("4900000000001_3", "组合品英文名缺「Nset」"),
                   ("4900000000004", "无报关信息")}


def test_report_overflow_sheets():
    rows = [{"SKU编码": str(4900000000000 + i), "重量(g)": 10, "长(cm)": None, "宽(cm)": None, "高(cm)": None}
            for i in range(2001)]
    wb = load_workbook(io.BytesIO(report_xlsx(rows, [])))
    assert wb.sheetnames[:4] == ["数据", "数据2", "数据3", "示例"]
    assert wb["数据"].max_row == 1001 and wb["数据3"].max_row == 2


def test_report_strips_control_chars():
    issues = [{"SKU编码": "4903301321217", "问题": "x", "商品名": "abc\x02def"}]
    wb = load_workbook(io.BytesIO(report_xlsx([], issues)))
    assert wb["问题清单"]["C2"].value == "abcdef"


def test_customs_std_without_hs():
    from shared.banma_dims import customs_issues
    std = [("", "洗濯用洗剤", "Laundry detergent, retail pack")]
    customs = {"1": {"name_en": "Laundry detergent, retail pack, 900g", "hs_code": "340220"}}
    assert customs_issues([sku("1", "4900000000001")], customs, std, None) == []
