"""shared/banma_fix（page15 斑马登録内容チェック・書き戻し）の単体テスト — 網・凭証不要。"""
from __future__ import annotations

import copy
import io

from openpyxl import load_workbook

from shared import banma_fix as BF
from shared.item_register import PLACEHOLDER_IMAGE_URL
import pytest
from unittest import mock


@pytest.fixture(autouse=True)
def _ui_ja():
    """メッセージは日文原文で検査する（中文モードの訳は shared/i18n.py の TRANSLATIONS_ZH。t() は既定 zh なので ja に固定）。"""
    with mock.patch("shared.i18n.get_lang", return_value="ja"):
        yield


class _Conn:
    def __init__(self, fail_insert=False):
        self.sql, self.fail_insert = [], fail_insert

    def execute(self, sql, params=()):
        if self.fail_insert and sql.startswith("INSERT"):
            raise RuntimeError("relation banma.cms_write_log does not exist")
        self.sql.append((sql, params))
        return self

    def fetchone(self):
        return {"id": 7}

    def fetchall(self):
        return []

    def commit(self):
        pass


def _product(desc="", pack=None):
    return {"SPU": {"SPUID": "S1", "Code": "4900000000011", "Title": "名", "Image": "", "DeveloperID": 5},
            "Descriptions": {"Html": desc, "Text": "", "Short": ""}, "Seo": None, "PackMaterials": pack or [],
            "SKUs": [{"SKUID": "K1", "Code": "4900000000011", "Image": "", "Weight": 1, "Length": 1, "Width": 1,
                      "Height": 1, "CostPrice": 377, "UpdateTime": "t0"},
                     {"SKUID": "K2", "Code": "4900000000011_2", "Image": "https://a/2.jpg", "Weight": 300,
                      "Length": 10, "Width": 8, "Height": 6, "CostPrice": 754, "UpdateTime": "t0"}],
            "Options": [], "Images": [], "Requirements": [], "Sources": [], "Tags": [], "Suppliers": []}


class _Client:
    """斑马の振る舞いを真似る: PUT body の SKU 項目を保存（server_hook で暗黙の変更も再現）。"""

    def __init__(self, product, customs=None, server_hook=None):
        self.p, self.customs, self.hook, self.calls = product, customs, server_hook, []

    def call(self, method, path, params=None, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path.endswith("/customs"):
            if method == "GET":
                if self.customs is None:
                    raise RuntimeError("Banma HTTP 404: NotFound")
                return {"Customs": copy.deepcopy(self.customs)}
            self.customs = {**(self.customs or {}), **body, "UpdateTime": "t1"}
            return {}
        if method == "GET":
            return {"Product": copy.deepcopy(self.p)}
        skus = {s["SKUID"]: s for s in body["Skus"]}
        for s in self.p["SKUs"]:
            s.update(skus[s["SKUID"]])
            s["UpdateTime"] = "t1"
        self.p["SPU"]["Image"] = body["Spu"].get("Image")
        self.p["Images"] = [{"ID": 99, **i} for i in body["Images"]]
        if self.hook:
            self.hook(self.p)
        return {}


def test_problems_blank_and_placeholder_20261112():
    sku = {"Weight": 1, "Length": 1, "Width": 1, "Height": 1, "Image": PLACEHOLDER_IMAGE_URL}
    assert BF.problems(sku, None, checks=BF.CHECKS, placeholder=True) == ["重量", "三辺", "画像", "英文名", "HS"]
    assert BF.problems(sku, {"Code": "330510", "NameEn": "Shampoo"}, checks=BF.CHECKS, placeholder=False) == []
    sku = {"Weight": 0, "Length": 12, "Width": 0, "Height": 3, "Image": ""}
    assert BF.problems(sku, {"Code": "33051", "NameEn": "-"}, checks=("重量", "画像", "HS"), placeholder=False) \
        == ["重量", "画像", "HS"]


def test_write_product_skips_when_description_or_pack():
    for p in (_product(desc="<p>説明</p>"), _product(pack=[{"ID": 1}])):
        c, conn = _Client(p), _Conn()
        r = BF.write_product(c, conn, "S1", {"K1": {"Weight": 80}}, "4900000000011")
        assert r["status"] == "skipped" and not any(m == "PUT" for m, *_ in c.calls) and not conn.sql


def test_write_product_ok_keeps_other_sku_and_spu_fields():
    c, conn = _Client(_product()), _Conn()
    r = BF.write_product(c, conn, "S1", {"K1": {"Weight": 80, "Length": 12, "Width": 8, "Height": 3,
                                                 "Image": "https://img/x.jpg"}}, "4900000000011")
    assert r == {"status": "ok", "message": ""}
    put = next(b for m, _, b in c.calls if m == "PUT")
    assert put["Spu"]["DeveloperID"] == 5                               # 回送しないと 0 にされる（10/07）
    assert put["Skus"][1]["Weight"] == 300 and put["Skus"][1]["CostPrice"] == 754   # 同 SPU の他 SKU も回送
    assert put["Spu"]["Image"] == "https://img/x.jpg" and put["Images"] == [{"Src": "https://img/x.jpg", "Sort": 0}]
    assert conn.sql[0][0].startswith("INSERT") and "ok" in conn.sql[-1][1]     # 書く前に台帳 → 結果


def test_write_product_adds_placeholder_image_when_spu_has_none_20261113():
    """斑马は画像が 1 つも無い商品の PUT を 400 で拒む（2026-11-13 page47 重量のみ 70/70）→ 新規登録と同じ「待上図」を入れる。"""
    p = _product()
    p["SKUs"] = p["SKUs"][:1]
    c, conn = _Client(p), _Conn()
    r = BF.write_product(c, conn, "S1", {"K1": {"Weight": 35}}, "x")
    put = next(b for m, _, b in c.calls if m == "PUT")
    assert put["Skus"][0]["Weight"] == 35 and put["Skus"][0]["Image"] == PLACEHOLDER_IMAGE_URL
    assert put["Spu"]["Image"] == PLACEHOLDER_IMAGE_URL and put["Images"] == [{"Src": PLACEHOLDER_IMAGE_URL, "Sort": 0}]
    assert r["status"] == "ok" and "待上図" in r["message"]
    c2 = _Client(_product())                         # どこかに画像がある SPU には足さない
    assert BF.write_product(c2, _Conn(), "S1", {"K1": {"Weight": 35}}, "x") == {"status": "ok", "message": ""}
    put2 = next(b for m, _, b in c2.calls if m == "PUT")
    assert put2["Skus"][0]["Image"] == "" and put2["Spu"]["Image"] == "" and put2["Images"] == []


def test_write_product_mismatch_when_server_changes_other_fields():
    def hook(p):
        p["SPU"]["DeveloperID"] = 0
        p["SKUs"][1]["CostPrice"] = 0
    c, conn = _Client(_product(), server_hook=hook), _Conn()
    r = BF.write_product(c, conn, "S1", {"K1": {"Weight": 80}}, "x")
    assert r["status"] == "mismatch" and "DeveloperID" in r["message"] and "CostPrice" in r["message"]


def test_write_product_no_log_no_write_and_unknown_sku():
    c = _Client(_product())
    assert BF.write_product(c, _Conn(fail_insert=True), "S1", {"K1": {"Weight": 80}}, "x")["status"] == "fail"
    assert BF.write_product(c, _Conn(), "S1", {"K9": {"Weight": 80}}, "x")["status"] == "fail"
    assert not any(m == "PUT" for m, *_ in c.calls)


def test_write_customs_put_keeps_fields_and_post_needs_both():
    cur = {"SKUID": "K1", "NameCn": "名", "NameEn": "-", "Code": "", "Weight": 80, "Material": "m", "Purpose": "p"}
    c = _Client(_product(), customs=dict(cur))
    r = BF.write_customs(c, _Conn(), "K1", "4900000000011", {"Code": "330510", "NameEn": "Shampoo"})
    assert r["status"] == "ok"
    body = next(b for m, _, b in c.calls if m == "PUT")
    assert body["Weight"] == 80 and body["Material"] == "m" and body["NameCn"] == "名" and body["Code"] == "330510"
    c = _Client(_product(), customs=None)
    r = BF.write_customs(c, _Conn(), "K1", "4900000000011", {"Code": "330510"})
    assert r["status"] == "skipped" and not any(m in ("PUT", "POST") for m, *_ in c.calls)
    r = BF.write_customs(c, _Conn(), "K1", "4900000000011", {"Code": "330510", "NameEn": "Shampoo"},
                         name_cn="名", weight=80)
    assert r["status"] == "created" and next(m for m, *_ in c.calls if m in ("PUT", "POST")) == "POST"


def test_dims_xlsx_template():
    wb = load_workbook(io.BytesIO(BF.dims_xlsx([{"SKU编码": "4900000000011", "重量(g)": 80, "长(cm)": 12,
                                                  "宽(cm)": 8, "高(cm)": 3}])))
    rows = list(wb.active.values)
    assert rows[0][0] == "SKU编码" and rows[1] == ("4900000000011", None, 80, 12, 8, 3)


def test_expect_guard_skips_when_banma_changed_since_check_20261112():
    """写し（04:00）やチェック後に斑马の値が変わっていたら書かない（上書きしない）。"""
    c, conn = _Client(_product()), _Conn()
    r = BF.write_product(c, conn, "S1", {"K1": {"Weight": 80}}, "x", expect={"K1": {"Weight": 0}})
    assert r["status"] == "skipped" and "チェック時" in r["message"] and not conn.sql
    r = BF.write_product(c, _Conn(), "S1", {"K1": {"Weight": 80}}, "x", expect={"K1": {"Weight": 1}})
    assert r["status"] == "ok"
    c = _Client(_product(), customs={"SKUID": "K1", "NameEn": "Real name", "Code": "330510"})
    r = BF.write_customs(c, _Conn(), "K1", "x", {"Code": "999999", "NameEn": "X"}, expect={"Code": "", "NameEn": ""})
    assert r["status"] == "skipped" and not any(m in ("PUT", "POST") for m, *_ in c.calls)


def test_mirror_customs_scope_none_only_for_ranked_export():
    class C(_Conn):
        def execute(self, sql, params=()):
            self.last = sql
            return self

        def fetchall(self):
            return ([{"sku_id": "K1", "name_cn": "名", "name_en": "Toy", "hs_code": "950300"}]
                    if "sku_customs" in self.last else [{"sku_id": "K2"}, {"sku_id": "K3"}])
    got = BF.mirror_customs(C(), ["K1", "K2", "K3", "K4"], ranked={"K1", "K2"})
    assert got == {"K1": {"Code": "950300", "NameEn": "Toy", "NameCn": "名"}, "K2": None}   # K3 範囲外・K4 写しに無し → API へ
