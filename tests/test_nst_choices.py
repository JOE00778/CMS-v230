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


def test_categories_and_owners_follow_nst_template_v260326ex():
    # 原本 データベース R2:W2 の 6 大分類・各中分類件数・C2:C12 の 11 人（2026-11-09 原本と全件照合済）
    F = C.MIDDLE_BY_LARGE
    assert list(F) == ["カー・バイク用品", "アウトドア・防災", "生活雑貨", "食品", "中国", "パール"]
    assert [len(v) for v in F.values()] == [4, 3, 11, 5, 20, 1]
    assert all(len(v) == len(set(v)) for v in F.values())
    assert len(C.OWNERS) == 11 and "079 隋艶偉" not in C.OWNERS


def test_load_choices(conn):
    ch = C.load_choices(conn)
    assert ch.suppliers[0] == "0001 ベストアンサ株式会社"
    assert ch.owners == C.OWNERS and ch.middle_by_large == C.MIDDLE_BY_LARGE and ch.warnings == ()


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
