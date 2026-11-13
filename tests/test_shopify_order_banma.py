"""page44: 報関情報は斑马が主・PG は補完（Boss 2026-10-29）。"""
from shared import shopify_order as so


class _Fake:
    def __init__(self, skus, customs):
        self.skus, self.customs = skus, customs

    def call(self, method, path, params=None):
        if path == "/v1/product/sku":
            want = params["Code"].split(",")
            return {"SKUs": [s for s in self.skus if s["Code"] in want]}
        return {"Customs": self.customs[path.split("/")[4]]}


def test_banma_overrides_pg_and_pg_fills_gaps():
    cl = _Fake([{"Code": "4902505476785", "SKUID": "1"}, {"Code": "4900000000002", "SKUID": "2"}],
               {"1": {"Code": "321590", "NameEn": "PILOT cartridge ink", "Weight": 11},
                "2": {"Code": "", "NameEn": "", "Weight": 0}})
    bm = so.banma_customs_by_jan(cl, ["4902505476785", "4900000000002", ""])
    assert bm == {"4902505476785": {"hs_code": "321590", "customs_name_en": "PILOT cartridge ink",
                                    "gross_weight_g": 11},
                  "4900000000002": {}}
    pg = {"4902505476785": {"hs_code": None, "net_weight_g": 5, "origin_country": "JP"},
          "4900000000002": {"hs_code": "330499", "customs_name_en": "Cream", "gross_weight_g": 50}}
    m = so.merge_banma(pg, bm)
    assert m["4902505476785"]["hs_code"] == "321590" and m["4902505476785"]["origin_country"] == "JP"
    assert m["4902505476785"]["net_weight_g"] is None and m["4902505476785"]["gross_weight_g"] == 11
    assert m["4900000000002"] == pg["4900000000002"]          # 斑马が空なら PG のまま
    assert pg["4902505476785"]["hs_code"] is None             # 元の dict は壊さない


def test_invoice_uses_current_quantity_after_cancel_20261113(monkeypatch):
    """#1089: 2 個 → 客の希望で 1 個取消（数量の返金）→ Invoice も 1 個。0 個になった行は出さない。単価は元の口径のまま。"""
    def li(sku, q, cur, total):
        return {"title": sku, "sku": sku, "quantity": q, "currentQuantity": cur,
                "discountedTotalSet": {"shopMoney": {"amount": total}}, "variant": {"barcode": sku}}
    order = {"name": "#1089", "lineItems": {"nodes": [li("4987643120080", 2, 1, "29.98"),
                                                      li("4589919566549", 2, 0, "45.98")]}}
    monkeypatch.setattr(so, "_call", lambda q, v: {"orders": {"nodes": [order]}})
    assert "currentQuantity" in so.ORDER_Q
    o = so.fetch_order("1089")
    assert [x["sku"] for x in o["lineItems"]["nodes"]] == ["4987643120080"]
    lines, _ = so.to_ci_lines(o, {})
    assert [(x.units, x.unit_value) for x in lines] == [(1, 14.99)]
