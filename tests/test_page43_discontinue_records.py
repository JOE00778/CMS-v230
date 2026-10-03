"""page43（产品停产记录）を AppTest で無頭実行。DB はモック。"""
from __future__ import annotations

import pathlib
from unittest import mock

import streamlit as st
from streamlit.testing.v1 import AppTest

PAGE = pathlib.Path(__file__).resolve().parents[1] / "pages" / "43_🛑_产品停产记录.py"

ALERTS = [
    {"detected_at": "2026-10-05", "signal_type": "販売終了", "source": "netdeoroshi", "jan": "4900000000001",
     "name": "品A", "maker": "KAO", "item_rank": "Aランク", "handling_cd": "取扱中", "internal_id": "11",
     "matched_name": "【販売終了】品A", "product_url": "https://netdeoroshi.com/product.php?id=1"},
    {"detected_at": "2026-10-05", "signal_type": "初回確認", "source": "netdeoroshi", "jan": "4900000000002",
     "name": "品B", "maker": "LION", "item_rank": "NEW", "handling_cd": "取扱中止", "internal_id": "12",
     "matched_name": "【販売終了】品B", "product_url": "https://netdeoroshi.com/product.php?id=2"},
    {"detected_at": "2026-10-05", "signal_type": "消失", "source": "netdeoroshi", "jan": "4900000000003",
     "name": "品C", "maker": "P&G", "item_rank": "取扱中止", "handling_cd": "取扱中止", "internal_id": "13",
     "matched_name": None, "product_url": None},
    {"detected_at": "2026-05-18T22:52:45", "signal_type": "boss_flagged: 取扱中止", "source": "manual_boss",
     "jan": "4901301447647", "name": None, "maker": None, "item_rank": "Cランク", "handling_cd": "取扱中",
     "internal_id": "14", "matched_name": None, "product_url": None},
]
STOCK = [{"item_internal_id": "11", "qty": 24}, {"item_internal_id": "14", "qty": 3}]
RUNS = [{"run_date": "2026-10-05", "scanned": 5052, "hit": 1200, "known_stop": 40, "errors": 2, "newly": 1,
         "gone": 1, "baseline": 39, "is_partial": False, "created_at": "2026-10-05 02:40"}]


def _conn(fail_runs=False):
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn

    def execute(sql, params=None):
        res = mock.MagicMock()
        if "discontinue_alerts" in sql:
            res.fetchall.return_value = ALERTS
        elif "inventory_snapshot" in sql:
            res.fetchall.return_value = STOCK
        elif "discontinue_scan_runs" in sql:
            if fail_runs:
                raise RuntimeError('relation "public.discontinue_scan_runs" does not exist')
            res.fetchall.return_value = RUNS
        return res
    conn.execute.side_effect = execute
    return conn


def _run(conn):
    st.cache_data.clear()
    with mock.patch("shared.db.get_readonly_connection", return_value=conn):
        at = AppTest.from_file(str(PAGE), default_timeout=60)
        at.session_state["__auth_ok"] = True
        at.run()
    return at


def test_renders_with_records_and_default_rank_filter():
    at = _run(_conn())
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error
    df = at.dataframe[0].value
    # 既定 = A/B/C/NEW のみ → ランク「取扱中止」の品C は出ない・手動フラグ（Cランク）は出る
    assert set(df["JAN"]) == {"4900000000001", "4900000000002", "4901301447647"}
    assert any(m.label.startswith("最近一次扫描") or "最新" in m.label for m in at.metric)
    stock = dict(zip(df["JAN"], df.iloc[:, 7]))
    assert stock["4900000000001"] == 24


def test_missing_runs_table_shows_error_but_records_still_render():
    at = _run(_conn(fail_runs=True))
    assert not at.exception
    assert any("扫描记录读取失败" in e.value for e in at.error)
    assert len(at.dataframe[0].value) == 3


def test_page_is_in_product_maintenance_nav():
    from shared import i18n
    src = pathlib.Path(i18n.__file__).read_text(encoding="utf-8")
    grp = src[src.index('("📝 商品信息维护", ['):]
    grp = grp[:grp.index("]),")]
    assert "pages/43_🛑_产品停产记录.py" in grp
