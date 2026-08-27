"""shared/owners の期間対応担当者マッピング（DB/Streamlit 不要の純ロジック）.

Boss 2026-08-27 依頼の 2 つの不変条件を固定する:
  1) 担当者の設定・変更は「発効年月以降」だけに効く = 過去月の数字は動かない
  2) 担当者未設定の店舗は集計に入れない（has_owner が False）
"""
from __future__ import annotations

import pandas as pd

from shared.owners import (
    OWNER_EXCLUDED, OWNER_NA, OWNER_NONE,
    add_owner_column, has_owner, resolve_owner_map,
)

_BASE = {"A店": "甲", "B店": "乙"}


def _r(ym, base=None):
    return resolve_owner_map(_RECS, ym, baseline=_BASE if base is None else base)


_RECS = [
    ("A店", "2026-08", "丙"),      # A店 は 8 月から 甲 → 丙
    ("C店", "2026-08", "丁"),      # 新規店舗を 8 月から 丁
    ("B店", "2026-09", ""),        # B店 は 9 月から担当者なし（未来発効）
]


# ---- 発効年月の境界（Boss の核心要件）----
def test_before_effective_month_keeps_baseline():
    assert _r("2026-07")["A店"] == "甲"          # 7 月は据え置き


def test_on_effective_month_applies():
    assert _r("2026-08")["A店"] == "丙"


def test_after_effective_month_persists():
    assert _r("2026-12")["A店"] == "丙"


def test_future_effective_not_applied_yet():
    assert _r("2026-08")["B店"] == "乙"          # 9 月発効はまだ効かない


def test_future_effective_applied_later():
    assert _r("2026-09")["B店"] == OWNER_NONE    # 9 月から担当者なし


def test_new_shop_only_from_effective_month():
    assert "C店" not in _r("2026-07")
    assert _r("2026-08")["C店"] == "丁"


def test_latest_record_wins():
    recs = [("A店", "2026-08", "丙"), ("A店", "2026-10", "戊")]
    m = resolve_owner_map(recs, "2026-11", baseline=_BASE)
    assert m["A店"] == "戊"


def test_empty_table_falls_back_to_baseline():
    assert resolve_owner_map([], "2026-08", baseline=_BASE) == _BASE


def test_malformed_records_ignored():
    recs = [("", "2026-08", "丙"), ("A店", "", "丙"), (None, None, None)]
    assert resolve_owner_map(recs, "2026-08", baseline=_BASE) == _BASE


# ---- 集計対象の判定 ----
def test_has_owner_excludes_unset_and_placeholders():
    assert has_owner("甲")
    assert not has_owner(OWNER_NONE)
    assert not has_owner(None)
    assert not has_owner("  ")
    assert not has_owner(OWNER_NA)        # 未分配
    assert not has_owner(OWNER_EXCLUDED)  # 対象外（日本店）


# ---- DataFrame への付与 ----
def test_add_owner_column_with_map():
    df = pd.DataFrame({"shop": [" A店 ", "B店", "未知店"], "v": [1, 2, 3]})
    out = add_owner_column(df, owner_map=_r("2026-08"))
    assert list(out["owner"]) == ["丙", "乙", OWNER_NONE]
    assert list(out[out["owner"].map(has_owner)]["v"]) == [1, 2]


def test_add_owner_column_without_map_keeps_legacy_behaviour():
    df = pd.DataFrame({"shop": ["Shopee PH", "6:ヤフー　SONIC PLAZA", "未知店"]})
    out = add_owner_column(df)
    assert list(out["owner"]) == ["刘颖", OWNER_EXCLUDED, OWNER_NA]


def test_add_owner_column_empty_df():
    out = add_owner_column(pd.DataFrame({"shop": []}), owner_map={})
    assert "owner" in out.columns and out.empty


# ---- load_owner_map（DB 読み込み層 · 接続はダミー）----
class _FakeCur:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """conn.execute(sql).fetchall() が (shop, effective_ym, owner) を返すだけの器。"""

    def __init__(self, rows=None, boom=False):
        self._rows, self._boom = rows or [], boom
        self.rolled_back = False

    def execute(self, sql, params=None):
        if self._boom:
            raise RuntimeError("relation \"ops.shop_owner\" does not exist")
        return _FakeCur(self._rows)

    def rollback(self):
        self.rolled_back = True


def test_load_owner_map_reads_records():
    from shared.owners import load_owner_map
    conn = _FakeConn([("Shopee PH", "2026-08", "新担当")])
    m = load_owner_map(conn, "2026-08")
    assert m["Shopee PH"] == "新担当"
    assert m["Shopee TW"] == "邓晓庆"          # 履歴の無い店舗は基線


def test_load_owner_map_respects_effective_month():
    from shared.owners import load_owner_map
    conn = _FakeConn([("Shopee PH", "2026-08", "新担当")])
    assert load_owner_map(conn, "2026-07")["Shopee PH"] == "刘颖"   # 7 月は基線のまま


def test_load_owner_map_falls_back_on_db_error():
    from shared.owners import load_owner_map
    conn = _FakeConn(boom=True)
    m = load_owner_map(conn, "2026-08")
    assert conn.rolled_back
    assert m["Shopee PH"] == "刘颖"           # テーブル無しでも基線で動く


def test_load_owner_map_tolerates_extra_columns():
    from shared.owners import load_owner_map
    conn = _FakeConn([("Shopee PH", "2026-08", "新担当", "2026-08-27T10:00")])
    assert load_owner_map(conn, "2026-08")["Shopee PH"] == "新担当"
