"""shared/nst_choices の単体テスト — SuiteQL と PG conn はモック（网络/DB 不要）。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shared import nst_choices as C


class _Conn:
    """shared.db の互換（row["col"] で引ける）を sqlite で再現。nst スキーマを attach。"""

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("ATTACH ':memory:' AS nst")
        self.db.execute("CREATE TABLE nst.vendor_master (vendor_code TEXT, company_name TEXT)")
        self.db.execute("CREATE TABLE nst.item_master_raw (jan TEXT, maker TEXT)")

    def execute(self, sql, params=()):
        return self.db.execute(sql, params)


@pytest.fixture
def conn():
    c = _Conn()
    c.db.executemany("INSERT INTO nst.vendor_master VALUES (?,?)", [
        ("0002", "株式会社あいう"), ("0001", "ベストアンサ株式会社"),
        ("0003", ""), ("", "コード無し"), ("0004", None)])
    c.db.executemany("INSERT INTO nst.item_master_raw VALUES (?,?)", [
        ("4901234000011", "花王"), ("4901234000028", "花王"),
        ("4909999000011", "A社"), ("4909999000028", "B社"),     # 2 種類 → 採らない
        ("4905555000011", ""), ("12345678", "短JAN"),
        ("4987000000011", " ライオン ")])
    return c


def _boom(*a, **k):
    raise RuntimeError("NST HTTP 503")


def test_load_suppliers_full_name_sorted_and_skips_blank(conn):
    assert C.load_suppliers(conn) == ["0001 ベストアンサ株式会社", "0002 株式会社あいう"]


def test_fallback_matches_spec_listing():
    F = C.FALLBACK_MIDDLE_BY_LARGE
    assert set(F) == {"カー・バイク用品", "生活雑貨", "アウトドア・防災", "ASEAN",
                      "パール", "USA", "中国", "食品"}
    assert F["ASEAN"][0] == "日用消耗品" and "その他" in F["カー・バイク用品"]
    assert all(len(v) == len(set(v)) for v in F.values())


def test_load_categories_groups_by_count_and_passes_limit():
    seen = {}

    def fake(sql, *, limit):
        seen["sql"], seen["limit"] = sql, limit
        return [{"large": "食品", "middle": "菓子", "n": "5"},
                {"large": "生活雑貨", "middle": "日用品", "n": "4"},
                {"large": "生活雑貨", "middle": "DIY・工具", "n": "100"},
                {"large": "食品", "middle": "加工食品", "n": "50"},
                {"large": None, "middle": "x", "n": "9"}]

    m, warns = C.load_categories(suiteql=fake)
    assert m == {"生活雑貨": ("DIY・工具", "日用品"), "食品": ("加工食品", "菓子")}
    assert warns == []
    assert "GROUP BY" in seen["sql"] and "department" not in seen["sql"]
    assert seen["limit"] >= 100


def test_load_categories_falls_back_on_error_and_empty():
    m, warns = C.load_categories(suiteql=_boom)
    assert m == C.FALLBACK_MIDDLE_BY_LARGE and "503" in warns[0]
    m2, warns2 = C.load_categories(suiteql=lambda sql, *, limit: [])
    assert m2 == C.FALLBACK_MIDDLE_BY_LARGE and warns2


def test_load_choices(conn):
    ch = C.load_choices(conn, suiteql=_boom)
    assert ch.suppliers[0] == "0001 ベストアンサ株式会社"
    assert ch.owners == ("037 米澤和敏", "043 徐越", "005 川崎里子", "079 隋艶偉")
    assert ch.large[0] == "ASEAN" and len(ch.warnings) == 1


def test_existing_jans_suiteql_chunks_of_200():
    calls = []

    def fake(sql, *, limit):
        calls.append(sql)
        return [{"upccode": "4900000000000"}, {"upccode": "4900000000250"},
                {"upccode": "9999999999999"}]          # 問い合わせ外は混ぜない

    jans = [f"49000000{i:05d}" for i in range(450)] + ["", "4900000000000"]
    found, warns = C.existing_jans(jans, suiteql=fake)
    assert len(calls) == 3
    assert found == {"4900000000000", "4900000000250"} and warns == []


def test_existing_jans_escapes_quote():
    calls = []
    C.existing_jans(["49'1"], suiteql=lambda sql, *, limit: calls.append(sql) or [])
    assert "'49''1'" in calls[0]


def test_existing_jans_falls_back_to_pg_with_warning(conn):
    found, warns = C.existing_jans(["4901234000011", "4900000000000"],
                                   conn=conn, suiteql=_boom)
    assert found == {"4901234000011"}
    assert "輸出部門のみ" in warns[0]


def test_existing_jans_no_conn_warns_not_silent():
    found, warns = C.existing_jans(["4901234000011"], suiteql=_boom)
    assert found == set() and "重複チェック未実施 1 件" in warns[0]


def test_existing_jans_empty_input_no_call():
    assert C.existing_jans(["", " "], suiteql=_boom) == (set(), [])


def test_load_prefix_makers_only_unique(conn):
    m = C.load_prefix_makers(conn)
    assert m == {"4901234": "花王", "4987000": "ライオン"}


def test_resolve_maker_order_and_truncate():
    pm = {"4901234": "花王"}
    assert C.resolve_maker("4901234999999", "花王株式会社", pm) == ("花王", "nst-prefix")
    assert C.resolve_maker("4909999000011", " B社 ", pm) == ("B社", "jancode")
    assert C.resolve_maker("12345678", "", pm) == ("", "none")
    long = "あ" * 70
    assert C.resolve_maker("4900000000000", long, pm) == ("あ" * 60, "jancode")
    assert C.resolve_maker("4901234000000", "", {"4901234": long})[0] == "あ" * 60


def test_page10_uses_shared_loader():
    src = (Path(__file__).resolve().parent.parent / "pages" / "10_📦_発注書作成.py").read_text("utf-8")
    assert "from shared.nst_choices import load_suppliers" in src
    assert "load_suppliers(conn)" in src and "def _load_suppliers" not in src
