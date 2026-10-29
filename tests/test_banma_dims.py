import io
import zipfile

from openpyxl import load_workbook

from shared.banma_dims import build, grid, upload_zip


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
        sku("1", "4900000000001", w=300),                                  # 占位 → JDL
        sku("2", "4900000000002", spu="P2", w=55, d=(8, 4, 3)),            # 实值不动
        sku("3", "4900000000003", spu="P3"),                               # 两源冲突 → 人工
        sku("4", "4900000000004", spu="P4"),                  # JDL 三边与兄弟差 >3cm
        sku("5", "4900000000005", spu="P4", d=(6, 5, 5), w=40),            # 兄弟
        sku("6", "4900000000001_2", typ="组合", comb=[{"SKUID": "1", "SKUCode": "4900000000001", "Quantity": 2}]),
        sku("7", "4900000000002_3", typ="组合", w=100,
            comb=[{"SKUID": "2", "SKUCode": "4900000000002", "Quantity": 2}]),  # 后缀≠件数
        sku("8", "4900000000009", cat="EC"),                               # 非 Export 不管
    ]
    jdl = {"4900000000001": (120, (100, 50, 30)), "4900000000003": (100, None),
           "4900000000004": (50, (200, 150, 100))}
    nst = {"4900000000003": (300)}
    rows, issues = build(skus, jdl, nst)
    up = {r["SKU编码"]: r for r in rows}
    assert up["4900000000001"]["重量(g)"] == 120 and up["4900000000001"]["长(cm)"] == 10
    assert up["4900000000001_2"]["重量(g)"] == 240 and (up["4900000000001_2"]["长(cm)"], up["4900000000001_2"]["宽(cm)"],
                                                        up["4900000000001_2"]["高(cm)"]) == (10, 6, 5)
    assert "4900000000002" not in up and "4900000000009" not in up
    kinds = {(i["SKU编码"], i["问题"]) for i in issues}
    assert ("4900000000003", "重量待人工") in kinds
    assert ("4900000000004", "三边待人工") in kinds
    assert ("4900000000002_3", "组合品·编码与明细件数不一致") in kinds
    z = zipfile.ZipFile(io.BytesIO(upload_zip(rows, "x")))
    f = [n for n in z.namelist() if n.startswith("斑马导入_01")][0]
    wb = load_workbook(io.BytesIO(z.read(f)))
    assert wb.sheetnames == ["数据", "示例"]
    vals = list(wb["数据"].iter_rows(values_only=True))
    assert vals[0][0] == "SKU编码" and vals[1][1] is None   # 成本价留空


def test_rank_scope():
    skus = [sku("1", "4900000000001"), sku("2", "4900000000002", spu="P2"),
            sku("3", "4900000000001_2", typ="组合", comb=[{"SKUID": "1", "SKUCode": "4900000000001", "Quantity": 2}]),
            sku("4", "4900000000002_2", typ="组合", comb=[{"SKUID": "2", "SKUCode": "4900000000002", "Quantity": 2}])]
    jdl = {"4900000000001": (120, (100, 50, 30)), "4900000000002": (80, (90, 50, 30))}
    rows, issues = build(skus, jdl, {}, ranked={"4900000000001"})
    assert {r["SKU编码"] for r in rows} == {"4900000000001", "4900000000001_2"} and not issues
