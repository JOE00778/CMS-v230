"""発注計算の月販は「締まった月」だけで見る（当月は途中で少なく見える → 系統的に少なく発注・監査 H1 2026-11-17）。"""
from shared import purchase_engine as PE


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        rows = self.rows

        class _R:
            def fetchall(self_inner):
                return rows
        return _R()


def test_current_month_is_excluded_from_sales_periods(monkeypatch):
    monkeypatch.setattr(PE, "_this_month", lambda: "2026-11")
    rows = [{"jan": "4900000000011", "period_start": ym, "qty": 100}
            for ym in ("2026-07", "2026-08", "2026-09", "2026-10")] + \
           [{"jan": "4900000000011", "period_start": "2026-11", "qty": 30}]
    df, _ = PE._load_monthly_sales(_Conn(rows), months=4)
    assert sorted(df["period_start"].unique()) == ["2026-07", "2026-08", "2026-09", "2026-10"]
