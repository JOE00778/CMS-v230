"""shared/price_alert の純ロジックテスト（DB/Streamlit 不要）.

4 規則の境界（dev_pp/qty_up/mgn_drop/red_line）+ 色優先度（赤>黄>OK）+ baseline 無し を固定。
"""
from __future__ import annotations

from shared import price_alert as pa
from shared.price_alert import AlertParams

_P = AlertParams()  # 既定 5pp / 100% / 5pp / 45%


def _ev(**kw):
    base = dict(cur_margin=60.0, avg7_margin=60.0, cur_qty=100.0, avg7_qty=100.0,
                cur_rev=1000.0, avg7_rev=1000.0, params=_P, has_baseline=True)
    base.update(kw)
    return pa.evaluate(**base)


# ---- R3 绝对红线（< 45%）----
def test_r3_below_red_line():
    s, fired = _ev(cur_margin=44.9, avg7_margin=44.9)  # 跟均值持平→只 R3
    assert pa.R3 in fired and s == pa.ALERT_RED


def test_r3_boundary_not_fired_at_45():
    s, fired = _ev(cur_margin=45.0, avg7_margin=45.0)
    assert pa.R3 not in fired and s == pa.ALERT_OK


def test_r3_fires_without_baseline():
    s, fired = pa.evaluate(cur_margin=40.0, avg7_margin=None, cur_qty=None,
                           avg7_qty=None, cur_rev=None, avg7_rev=None,
                           params=_P, has_baseline=False)
    assert fired == [pa.R3] and s == pa.ALERT_RED


# ---- R1 跌破近7日均 dev_pp ----
def test_r1_fires_when_drop_exceeds_dev_pp():
    s, fired = _ev(cur_margin=54.9, avg7_margin=60.0)  # 跌 5.1pp > 5
    assert pa.R1 in fired and s == pa.ALERT_RED


def test_r1_boundary_not_fired_at_exactly_5pp():
    s, fired = _ev(cur_margin=55.0, avg7_margin=60.0)  # 恰跌 5pp（不 > 5）
    assert pa.R1 not in fired


# ---- R2 降价冲量（销量翻倍 且 毛利率跌>5pp）----
def test_r2_fires_qty_double_and_margin_drop():
    s, fired = _ev(cur_margin=54.0, avg7_margin=60.0, cur_qty=201.0, avg7_qty=100.0)
    assert pa.R2 in fired and pa.R1 in fired and s == pa.ALERT_RED  # 同时触发 R1


def test_r2_not_fired_when_qty_not_doubled():
    s, fired = _ev(cur_margin=54.0, avg7_margin=60.0, cur_qty=199.0, avg7_qty=100.0)
    assert pa.R2 not in fired  # 销量未超过 +100%


def test_r2_not_fired_when_margin_ok_despite_qty_spike():
    s, fired = _ev(cur_margin=59.0, avg7_margin=60.0, cur_qty=300.0, avg7_qty=100.0)
    assert pa.R2 not in fired  # 毛利率只跌 1pp


# ---- R4 收益涨但毛利率没跟（黄）----
def test_r4_yellow_revenue_up_margin_slightly_down():
    s, fired = _ev(cur_margin=58.0, avg7_margin=60.0, cur_rev=1200.0, avg7_rev=1000.0)
    # 收益涨、毛利率跌 2pp（<5pp 不触红）→ 仅 R4 黄
    assert fired == [pa.R4] and s == pa.ALERT_YELLOW


def test_r4_not_fired_when_revenue_flat():
    s, fired = _ev(cur_margin=58.0, avg7_margin=60.0, cur_rev=1000.0, avg7_rev=1000.0)
    assert pa.R4 not in fired and s == pa.ALERT_OK  # 收益没涨


def test_r4_not_fired_when_margin_up():
    s, fired = _ev(cur_margin=61.0, avg7_margin=60.0, cur_rev=1200.0, avg7_rev=1000.0)
    assert pa.R4 not in fired and s == pa.ALERT_OK  # 收益涨且毛利率也涨→健康


# ---- 色优先级：红盖黄 ----
def test_red_overrides_yellow():
    # 收益涨(R4) + 毛利率跌6pp(R1) → 红
    s, fired = _ev(cur_margin=54.0, avg7_margin=60.0, cur_rev=1200.0, avg7_rev=1000.0)
    assert pa.R1 in fired and pa.R4 in fired and s == pa.ALERT_RED
    assert pa.severity(pa.ALERT_RED) > pa.severity(pa.ALERT_YELLOW)


def test_healthy_all_ok():
    s, fired = _ev()  # 全持平
    assert fired == [] and s == pa.ALERT_OK
