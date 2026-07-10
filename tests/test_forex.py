"""nst_monthly_rates（月次三金レート · 前月沿用 + フォールバック）の単体テスト。"""
import sqlite3

from shared.forex import FX_TO_JPY, nst_monthly_rates


def _conn_with_rates(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("ATTACH DATABASE ':memory:' AS nst")
    conn.execute(
        "CREATE TABLE nst.currency_rate ("
        "base_currency_id TEXT, tx_currency TEXT, "
        "exchange_rate REAL, effective_date TEXT)")
    conn.executemany(
        "INSERT INTO nst.currency_rate VALUES ('1', ?, ?, ?)", rows)
    return conn


def test_monthly_switch_and_carry_forward():
    conn = _conn_with_rates([
        ("大韓民国ウォン", 0.1, "2026-04-01"),
        ("大韓民国ウォン", 0.105, "2026-05-01"),
        ("大韓民国ウォン", 0.095, "2026-07-01"),
    ])
    out = nst_monthly_rates(conn, "KRW", ["2026-04", "2026-05", "2026-06", "2026-07"])
    assert out["2026-04"] == 0.1
    assert out["2026-05"] == 0.105
    assert out["2026-06"] == 0.105  # 6月更新なし → 5月レートを沿用
    assert out["2026-07"] == 0.095


def test_month_before_first_rate_falls_back():
    conn = _conn_with_rates([("大韓民国ウォン", 0.1, "2026-04-01")])
    out = nst_monthly_rates(conn, "KRW", ["2026-03"])
    assert out["2026-03"] == FX_TO_JPY["KRW"]


def test_missing_table_falls_back_to_fixed():
    conn = sqlite3.connect(":memory:")  # nst スキーマなし
    out = nst_monthly_rates(conn, "KRW", ["2026-04", "2026-05"])
    assert out == {"2026-04": FX_TO_JPY["KRW"], "2026-05": FX_TO_JPY["KRW"]}


def test_iso_code_match_when_ja_name_absent():
    conn = _conn_with_rates([("フィリピン（PHP）", 2.45, "2026-05-01")])
    out = nst_monthly_rates(conn, "PHP", ["2026-05"])
    assert out["2026-05"] == 2.45
