"""輸出事業 月次売上予算（Boss 2026-11-09「看预算就可以」）.

予算 = Boss の予実表「輸出（合計）」の予算行（2026-03〜2027-02 を初回投入）。
実績 = nst.sales_daily の当月 SUM(revenue)（= NST「売上予算vs実績_FB」の輸出事業 実績と同口径・
       2026-10 で 52,891,408 円一致を確認）。予実表の「実績」行は使わない。
置き場 finance.sales_budget_monthly（PG・cms 書込）。編集は page32「系统参数设定 → 💴 输出营业额预算」。
"""
from __future__ import annotations

TABLE = "finance.sales_budget_monthly"


def ensure_schema(conn) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS finance")
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} ("
        "ym TEXT PRIMARY KEY, "                 # 'YYYY-MM'
        "budget_jpy BIGINT NOT NULL, "
        "updated_at TIMESTAMPTZ DEFAULT NOW())")
    conn.commit()


def load(conn) -> dict[str, int]:
    return {r[0]: int(r[1]) for r in conn.execute(
        f"SELECT ym, budget_jpy FROM {TABLE} ORDER BY ym").fetchall()}


def save(conn, rows: dict[str, int]) -> int:
    """ym → 予算 を UPSERT。0 / 空は削除。戻り値 = 書いた行数。"""
    n = 0
    for ym, v in rows.items():
        if v:
            conn.execute(
                f"INSERT INTO {TABLE} (ym, budget_jpy, updated_at) VALUES (?, ?, NOW()) "
                "ON CONFLICT (ym) DO UPDATE SET budget_jpy = EXCLUDED.budget_jpy, updated_at = NOW()",
                (ym, int(v)))
        else:
            conn.execute(f"DELETE FROM {TABLE} WHERE ym = ?", (ym,))
        n += 1
    conn.commit()
    return n

