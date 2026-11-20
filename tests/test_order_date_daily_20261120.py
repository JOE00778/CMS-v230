"""店铺毛利「月内每日推移」按下单日重新分摊（Boss 2026-11-20）。"""
import datetime as dt

import pandas as pd

from shared.order_date_daily import redistribute

D = dt.date


def _daily():
    # 11/16(周一) PH 开票 1,000 円：其中一半是 11/15(周日) 下单
    # 11/16 Lazada 开票 200 円：无下单日 → 留在开票日
    return pd.DataFrame([
        {"shop": "Shopee PH", "sale_date": D(2026, 11, 16), "qty": 10,
         "revenue": 1000.0, "defined_cost": 400.0, "gross_profit": 600.0},
        {"shop": "Lazada PH", "sale_date": D(2026, 11, 16), "qty": 2,
         "revenue": 200.0, "defined_cost": 100.0, "gross_profit": 100.0},
        {"shop": "Shopee PH", "sale_date": D(2026, 11, 2), "qty": 4,
         "revenue": 400.0, "defined_cost": 200.0, "gross_profit": 200.0},
    ])


def _shares():
    return pd.DataFrame([
        {"shop": "Shopee PH", "inv_date": D(2026, 11, 16), "order_date": D(2026, 11, 15), "amt": 500.0},
        {"shop": "Shopee PH", "inv_date": D(2026, 11, 16), "order_date": D(2026, 11, 16), "amt": 500.0},
        # 11/2 开票的一半是上月下单
        {"shop": "Shopee PH", "inv_date": D(2026, 11, 2), "order_date": D(2026, 10, 31), "amt": 100.0},
        {"shop": "Shopee PH", "inv_date": D(2026, 11, 2), "order_date": D(2026, 11, 1), "amt": 100.0},
    ])


def test_sunday_gets_its_orders():
    out, prior = redistribute(_daily(), _shares(), "2026-11")
    by = out.set_index("sale_date")
    assert by.loc[D(2026, 11, 15), "revenue"] == 500.0
    assert by.loc[D(2026, 11, 15), "gross_profit"] == 300.0
    assert by.loc[D(2026, 11, 16), "revenue"] == 700.0      # 500 Shopee + 200 Lazada
    assert by.loc[D(2026, 11, 1), "revenue"] == 200.0
    assert D(2026, 11, 2) not in by.index


def test_prior_month_orders_excluded_and_reported():
    out, prior = redistribute(_daily(), _shares(), "2026-11")
    assert prior == 200.0
    assert out["revenue"].sum() + prior == _daily()["revenue"].sum()


def test_no_shares_keeps_invoice_date():
    out, prior = redistribute(_daily(), _shares().iloc[0:0], "2026-11")
    assert prior == 0.0
    assert set(out["sale_date"]) == {D(2026, 11, 16), D(2026, 11, 2)}
    assert out["revenue"].sum() == 1600.0


def test_daily_order_counts_by_order_date():
    from shared.order_date_daily import daily_order_counts
    o = pd.DataFrame([
        {"shop": "Shopee PH", "order_no": "A", "inv_date": D(2026, 11, 16), "order_date": D(2026, 11, 15)},
        {"shop": "Shopee PH", "order_no": "B", "inv_date": D(2026, 11, 16), "order_date": D(2026, 11, 16)},
        {"shop": "Lazada PH", "order_no": "C", "inv_date": D(2026, 11, 16), "order_date": None},   # 下单日不明→开票日
        {"shop": "Shopee PH", "order_no": "D", "inv_date": D(2026, 11, 2), "order_date": D(2026, 10, 31)},  # 上月下单→除外
    ])
    by = daily_order_counts(o, "2026-11").set_index("sale_date")["orders"]
    assert by.to_dict() == {D(2026, 11, 15): 1, D(2026, 11, 16): 2}
