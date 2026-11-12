"""shared/skuid_fill（page15 SKUID 空チェック）の単体テスト — 網・凭証不要。"""
from __future__ import annotations

import csv
import datetime as dt
import io

from shared import skuid_fill as SF
from shared.hs_classify import Customs
import pytest
from unittest import mock


@pytest.fixture(autouse=True)
def _ui_ja():
    """メッセージは日文原文で検査する（中文モードの訳は shared/i18n.py の TRANSLATIONS_ZH。t() は既定 zh なので ja に固定）。"""
    with mock.patch("shared.i18n.get_lang", return_value="ja"):
        yield


def _item(code, jan="", **kw):
    return {"id": "1", "code": code, "jan": jan, "name": f"名 {code}", "dept": "輸出事業", "handling": "取扱中",
            "inactive": "F", "item_rank": None, **kw}


def test_blank_items_sql_period_20261112():
    seen = []
    SF.blank_items(lambda q: seen.append(q) or [], days=30, today=dt.date(2026, 11, 12))
    SF.blank_items(lambda q: seen.append(q) or [], days=None)
    assert "custitem_item_skuid IS NULL" in seen[0] and "TO_DATE('2026-10-13', 'YYYY-MM-DD')" in seen[0]
    assert seen[0].rstrip().endswith("ORDER BY item.id")       # suiteql_all のページ分けに必須
    assert "createddate >=" not in seen[1]


def test_pick_defaults_and_blank_values():
    items = [_item("a"), _item("b", handling="取扱中止"), _item("c", inactive="T"), _item("d", dept="輸出（中国）"),
             _item("e", item_rank="NEW"), _item("f", handling=None)]
    d = SF.DEFAULTS
    got = SF.pick(items, depts=d["depts"], handlings=d["handlings"], states=d["states"], ranks=d["ranks"])
    assert [r["code"] for r in got] == ["a", "d", "e"]
    got = SF.pick(items, depts=("輸出事業",), handlings=(SF.BLANK,), states=SF.STATES, ranks=SF.RANKS)
    assert [r["code"] for r in got] == ["f"]


def test_match_code_then_jan_and_conflicts_20261112():
    """実例: soel-064 と 4571502021054 は同じ斑马 SKU（JAN 4571502021054）を指す → どちらも CSV に出さない。"""
    items = [_item("X1", "4900000000011"), _item("X2", "4900000000028"), _item("soel-064", "4571502021054"),
             _item("4571502021054", "4571502021054"), _item("X3", "4900000000035"), _item("X4", "4900000000042")]
    bm = {"X1": "101", "4900000000028": "102", "4571502021054": "103", "4900000000042": "104"}
    hits, conflicts, misses = SF.match(items, bm, by_jan=True, in_use={"104": ["OLD-1"]})
    assert [(h["code"], h["sku_id"], h["key"]) for h in hits] == [("X1", "101", "型番"), ("X2", "102", "JAN")]
    assert {c["code"] for c in conflicts} == {"soel-064", "4571502021054", "X4"}
    assert "今回の対象 4571502021054" in next(c["note"] for c in conflicts if c["code"] == "soel-064")
    assert "NST で設定済み OLD-1" in next(c["note"] for c in conflicts if c["code"] == "X4")
    assert [m["code"] for m in misses] == ["X3"]
    hits, conflicts, misses = SF.match(items[:2], bm, by_jan=False)
    assert [h["code"] for h in hits] == ["X1"] and [m["code"] for m in misses] == ["X2"]


def test_lookup_keys_and_in_use_query():
    items = [_item("X1", "4900000000011"), _item("4900000000011", "4900000000011"), _item("X2", "")]
    assert SF.lookup_keys(items, by_jan=True) == ["4900000000011", "X1", "X2"]
    assert SF.lookup_keys(items, by_jan=False) == ["4900000000011", "X1", "X2"]
    seen = []
    got = SF.skuids_in_use(lambda q: seen.append(q) or [{"code": "OLD", "sku_id": "77"}], ["77", "x'; DROP", ""])
    assert got == {"77": ["OLD"]} and "IN ('77')" in seen[0] and "DROP" not in seen[0]
    assert SF.skuids_in_use(lambda q: 1 / 0, ["", "abc"]) == {}      # 数字以外は照会しない


class _Conn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        return self

    def fetchall(self):
        return self.rows


def test_mirror_and_ledger_loaders():
    c = _Conn([{"code": "X1", "sku_id": 101}])
    assert SF.mirror_skus(c, ["X1", "X9"]) == {"X1": "101"} and c.sql[0][1] == ("X1", "X9")
    c = _Conn([{"item_code": "X1", "payload": '{"商品原価": "377"}', "bm_payload": {"海关编码": "950300"}}])
    assert SF.load_ledger(c, ["X1"]) == {"X1": {"payload": {"商品原価": "377"}, "bm": {"海关编码": "950300"}}}


def test_register_rows_ledger_first_then_nst():
    misses = [_item("X1", "4900000000011", cost="377", pkg_weight="80"),
              _item("X2", "4900000000028", name="ライオン クリニカ ハミガキ 30g", maker="ライオン", cost="120",
                    pkg_weight="45", pkg_height="15", pkg_width="4", pkg_depth="3")]
    ledger = {"X1": {"payload": {"アイテム名": "台帳の名前", "商品原価": "380", "パッケージ重量(g)": "",
                                 "JANコード": "4900000000011"},
                     "bm": {"海关编码": "950300", "英文名称": "Toy", "图片URL": "https://img/x1.jpg"}}}
    seen = []
    rows, extra = SF.register_rows(misses, ledger, lambda n, m, j: seen.append((n, m, j))
                                   or Customs("330610", "Toothpaste, 30g", "歯磨き", "jp-name"))
    assert rows[0]["アイテム名"] == "台帳の名前" and rows[0]["商品原価"] == "380"
    assert rows[0]["パッケージ重量(g)"] == "80"                     # 台帳が空なら NST の値
    assert extra["X1"] == {"_hs": "950300", "_name_en": "Toy", "_image_url": "https://img/x1.jpg", "source": "台帳"}
    assert rows[1]["パッケージ高さ(cm)"] == "15" and rows[1]["商品原価"] == "120"
    assert extra["X2"]["_hs"] == "330610" and extra["X2"]["source"] == "NST" and extra["X2"]["_image_url"] == ""
    assert seen == [("ライオン クリニカ ハミガキ 30g", "ライオン", "4900000000028")]   # 台帳の品は判定しない


def test_update_csv_template_header():
    got = list(csv.reader(io.StringIO(SF.update_csv([("X1", "101"), ("X2", ""), ("", "9")]).decode("utf-8-sig"))))
    assert got[0] == ["型番", "SKUID", "サポート提供 "] and got[1:] == [["X1", "101", "T"]]
