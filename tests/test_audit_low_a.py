"""監査 2026-11-17 低危 A 組（L1 / L2 / L3 / L9 / L10 / L23）の回帰テスト。"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---- L1: 側栏ナビのパスは全部実在するファイル ----
def test_l1_nav_paths_exist():
    from shared.i18n import _NAV_GROUPS
    missing = [p for _, pages in _NAV_GROUPS for p, _ in pages if not (ROOT / p).exists()]
    assert not missing, missing


# ---- L23: 非 ASCII のパスワードでも例外にならず「不一致」を返す ----
def test_l23_non_ascii_password_is_mismatch_not_typeerror():
    from shared.auth import _check
    assert _check("ｐａｓｓ密码", "secret") is False
    assert _check("密码", "密码") is True
    assert _check("secret", "secret") is True


# ---- L9: KPI クエリが 1 本でも失敗したら快照を書かない ----
def _sqlite_with_nst(*, sales_ok: bool = True):
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH ':memory:' AS nst")
    conn.execute("CREATE TABLE nst.item_master_raw (internal_id INTEGER, jan TEXT, cost_estimate REAL)")
    conn.execute("CREATE TABLE nst.inventory_snapshot "
                 "(item_internal_id INTEGER, qty_on_hand REAL, snapshot_date TEXT, warehouse TEXT)")
    if sales_ok:
        conn.execute("CREATE TABLE nst.sales_daily (sale_date TEXT, revenue REAL, gross_profit REAL)")
    conn.create_function("to_char", 2, lambda d, fmt: (d or "")[:7])
    return conn


def _history_rows(conn):
    return conn.execute("SELECT * FROM kpi_monthly_history").fetchall()


def test_l9_query_failure_skips_snapshot():
    from shared.kpi_history import take_snapshot
    conn = _sqlite_with_nst(sales_ok=False)   # sales_daily が無い = 売上クエリ失敗
    with pytest.raises(Exception):
        take_snapshot(conn)
    assert _history_rows(conn) == []


def test_l9_real_zero_is_still_written():
    from shared.kpi_history import take_snapshot
    conn = _sqlite_with_nst()
    take_snapshot(conn)
    rows = _history_rows(conn)
    assert len(rows) == 1 and rows[0][1] == 0 and rows[0][3] == 0.0


# ---- L10: 本月销售は上月同期（上月 1 日〜上月同日）と比べる ----
@pytest.mark.parametrize("today,expect", [
    (date(2026, 11, 18), (date(2026, 10, 1), date(2026, 10, 18))),
    (date(2026, 3, 31), (date(2026, 2, 1), date(2026, 2, 28))),   # 上月に同日が無い → 月末
    (date(2026, 1, 5), (date(2025, 12, 1), date(2025, 12, 5))),    # 年またぎ
])
def test_l10_prev_month_same_period(today, expect):
    from shared.kpi_history import prev_month_same_period
    assert prev_month_same_period(today) == expect


def test_l10_get_delta_no_longer_compares_mtd_to_full_month():
    from shared.kpi_history import get_delta
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kpi_monthly_history (year_month TEXT PRIMARY KEY, sku_total INTEGER, "
                 "stock_value_jpy REAL, month_revenue_jpy REAL, gross_margin REAL, snapshot_at TEXT)")
    conn.executemany("INSERT INTO kpi_monthly_history VALUES (?,?,?,?,?,?)", [
        ("202610", 10, 100.0, 1000.0, 0.3, "x"), ("202611", 10, 100.0, 50.0, 0.3, "x")])
    assert "revenue_delta_pct" not in get_delta(conn)


def test_l10_home_uses_same_period_query():
    src = (ROOT / "cms.py").read_text(encoding="utf-8")
    assert "prev_month_same_period(" in src
    assert '_kpi_delta["revenue_delta_pct"]' not in src


# ---- L2: 整批回滚時は ok=N を出さず「全部未保存（已回滚）」と言う ----
def test_l2_rollback_message_is_truthful():
    src = (ROOT / "pages" / "05_🏪_店铺别毛利.py").read_text(encoding="utf-8")
    i = src.index('key="owner_save"')
    block = src[src.index("if _fail:", i):src.index("conn_w.commit()", i)]
    assert "ok={_ok}" not in block
    assert 't("保存失败，本批全部未保存（已回滚）")' in block


# ---- L3: 飞书 OAuth state はランダム・発行ブラウザの cookie と一致したときだけ code を換える ----
class _FakeSt:
    def __init__(self, qp: dict, cookies: dict):
        from types import SimpleNamespace

        class _QP(dict):
            def clear(self):
                super().clear()
        self.query_params = _QP(qp)
        self.context = SimpleNamespace(cookies=cookies)
        self.secrets = {}


def _run_callback(monkeypatch, qp, cookies):
    from shared import lark_auth
    for k, v in {"LARK_APP_ID": "cli_x", "LARK_APP_SECRET": "s", "LARK_REDIRECT_URI": "https://cms"}.items():
        monkeypatch.setenv(k, v)
    calls = []
    monkeypatch.setattr(lark_auth, "st", _FakeSt(qp, cookies))
    monkeypatch.setattr(lark_auth, "exchange_code_for_user",
                        lambda code: calls.append(code) or {"name": "u", "union_id": code})
    return lark_auth.try_handle_oauth_callback(), calls


def test_l3_matching_state_logs_in(monkeypatch):
    user, calls = _run_callback(monkeypatch, {"code": "c1", "state": "cms-abc"}, {"cms_lark_state": "cms-abc"})
    assert user and calls == ["c1"]


@pytest.mark.parametrize("qp,cookies", [
    ({"code": "c1", "state": "cms-abc"}, {}),                              # 受害者に cookie なし
    ({"code": "c1", "state": "cms-abc"}, {"cms_lark_state": "cms-xyz"}),   # 別ブラウザの state
    ({"code": "c1", "state": "cms"}, {"cms_lark_state": "cms-xyz"}),       # 旧固定 state
    ({"code": "c1"}, {"cms_lark_state": "cms-xyz"}),                       # state なし
])
def test_l3_state_mismatch_is_rejected_without_exchanging_code(monkeypatch, qp, cookies):
    user, calls = _run_callback(monkeypatch, qp, cookies)
    assert user is None and calls == []


def test_l3_state_is_random_and_cookie_js_carries_it():
    from shared import lark_auth
    s1, s2 = lark_auth.new_state(), lark_auth.new_state()
    assert s1 != s2 and len(s1) > 16
    assert f"state={s1}" in lark_auth.build_login_url(s1)
    assert f"{lark_auth.STATE_COOKIE}={s1};" in lark_auth.state_cookie_js(s1)
