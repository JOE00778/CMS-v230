"""shared/banma_register — 斑马 API 新規登録（網络不要・client はフェイク）。"""
from __future__ import annotations

from shared import banma_register as BMR
from shared.item_register import COL_HS, COL_IMAGE, COL_NAME_EN, PLACEHOLDER_IMAGE_URL

ROW = {"型番": "4901234567894", "JANコード": "4901234567894", "アイテム名": "テスト クッキー",
       "商品原価": "900", "パッケージ重量(g)": "120.4", "パッケージ高さ(cm)": "10",
       "パッケージ幅(cm)": "5.2", "パッケージ奥行(cm)": "3",
       COL_HS: "190590", COL_NAME_EN: "Cookie", COL_IMAGE: "https://img/x.jpg"}


class FakeClient:
    def __init__(self, existing=None, after=None, post_error=None, customs_error=None):
        self.calls, self.existing, self.after = [], existing, after
        self.post_error, self.customs_error = post_error, customs_error
        self.posted = False

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            hit = self.after if self.posted else self.existing
            return {"SKUs": [hit] if hit else []}
        if path == "/v1/product/":
            self.posted = True
            if self.post_error:
                raise self.post_error
            return {}
        if self.customs_error:
            raise self.customs_error
        return {}


SKU = {"SKUID": "11", "SPUID": "22", "Code": "4901234567894", "CostPrice": 900.0, "Weight": 121}


def test_product_body_matches_xlsx_conventions():
    b = BMR.product_body(ROW)
    sku = b["Skus"][0]
    assert b["Spu"] == {"Code": "4901234567894", "Title": "テスト クッキー", "Image": "https://img/x.jpg",
                        "Source": "手工创建"}
    assert sku["CostPrice"] == 900.0                       # 円の数値そのまま（既存運用）
    assert (sku["Weight"], sku["Height"], sku["Width"], sku["Length"]) == (121, 10, 6, 3)
    assert sku["Specification"] == "4901234567894" and sku["Options"] == [{"Name": "1", "Value": "4901234567894"}]
    assert b["Images"] == [{"Src": "https://img/x.jpg", "Sort": 0}]
    assert BMR.product_body({**ROW, COL_IMAGE: ""})["Spu"]["Image"] == PLACEHOLDER_IMAGE_URL


def test_existing_sku_is_skipped_without_post():
    c = FakeClient(existing=SKU)
    r = BMR.register_one(c, ROW)
    assert r["status"] == "skipped" and not any(m == "POST" for m, _, _ in c.calls)


def test_new_sku_posts_product_then_customs():
    c = FakeClient(after=SKU)
    r = BMR.register_one(c, ROW)
    assert r == {"status": "ok", "sku_id": "11", "spu_id": "22", "message": ""}
    posts = [(p, b) for m, p, b in c.calls if m == "POST"]
    assert posts[0][0] == "/v1/product/"
    assert posts[1] == ("/v1/product/sku/11/customs",
                        {"Code": "190590", "NameCn": "テスト クッキー", "NameEn": "Cookie", "Weight": 121})


def test_post_error_but_created_is_ok_and_not_resent():
    c = FakeClient(after=SKU, post_error=TimeoutError("read"))
    r = BMR.register_one(c, ROW)
    assert r["status"] == "ok"
    assert sum(1 for m, p, _ in c.calls if m == "POST" and p == "/v1/product/") == 1


def test_post_error_and_not_found_is_fail():
    r = BMR.register_one(FakeClient(post_error=RuntimeError("400 bad")), ROW)
    assert r["status"] == "fail" and "400 bad" in r["message"]


def test_missing_hs_or_mismatch_is_partial():
    r = BMR.register_one(FakeClient(after=SKU), {**ROW, COL_HS: ""})
    assert r["status"] == "partial" and "報関情報なし" in r["message"]
    r = BMR.register_one(FakeClient(after={**SKU, "CostPrice": 0}), ROW)
    assert r["status"] == "partial" and "CostPrice" in r["message"]
    r = BMR.register_one(FakeClient(after=SKU, customs_error=RuntimeError("x")), ROW)
    assert r["status"] == "partial" and "報関情報の書込に失敗" in r["message"]


def test_register_all_records_each_row():
    seen = []
    out = BMR.register_all(FakeClient(after=SKU), [ROW], record=lambda code, r: seen.append((code, r["status"])))
    assert seen == [("4901234567894", "ok")] and out[0][1]["status"] == "ok"
