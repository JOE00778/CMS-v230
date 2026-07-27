"""page38 广告 ROI 判定层测试（doc 23 §5）。

重点覆盖唯一会**静默出错**的地方：比率类指标的周/月聚合口径。
写错不报错、不崩溃、曲线照画，只是全是错的。
"""
import pandas as pd
import pytest

from modules.ads_roi import (
    add_ratio_columns,
    breakeven_roas,
    period_key,
    resample,
    safe_ratio,
    verdict,
)


def _df(rows):
    return pd.DataFrame(rows)


@pytest.fixture
def skewed_week():
    """同一周内：高消耗低回报日 + 低消耗高回报日。

    日 ROAS 分别是 0.5 与 5.0（均值 2.75），但加权真值 = 550/1010 ≈ 0.545。
    两者差一个数量级，足以让错误口径暴露。
    """
    return _df([
        {"report_date": pd.Timestamp("2026-07-06"), "cost_usd": 1000.0,
         "impressions": 10000, "clicks": 500, "conversions": 10.0,
         "conversions_value": 500.0},
        {"report_date": pd.Timestamp("2026-07-07"), "cost_usd": 10.0,
         "impressions": 100, "clicks": 10, "conversions": 8.0,
         "conversions_value": 50.0},
    ])


class TestAggregationTrap:
    """doc 23 §3.5：比率必须先聚合分子分母再相除。"""

    def test_weekly_roas_is_weighted_not_mean(self, skewed_week):
        wk = resample(skewed_week, "week", today=pd.Timestamp("2026-07-20"))
        assert len(wk) == 1
        assert wk["roas"].iloc[0] == pytest.approx(550.0 / 1010.0)

    def test_weekly_roas_differs_from_naive_mean(self, skewed_week):
        wk = resample(skewed_week, "week", today=pd.Timestamp("2026-07-20"))
        naive = ((skewed_week["conversions_value"] / skewed_week["cost_usd"]).mean())
        assert abs(wk["roas"].iloc[0] - naive) > 1.0, "落入日比率取均值的陷阱"

    def test_cpa_ctr_cpc_also_weighted(self, skewed_week):
        wk = resample(skewed_week, "week", today=pd.Timestamp("2026-07-20")).iloc[0]
        assert wk["cpa"] == pytest.approx(1010.0 / 18.0)
        assert wk["ctr"] == pytest.approx(510.0 / 10100.0)
        assert wk["cpc"] == pytest.approx(1010.0 / 510.0)

    def test_same_iso_week_groups_together(self, skewed_week):
        """周一与周二必须同周——pandas W-MON 会把周一切到上一周。"""
        keys = period_key(skewed_week["report_date"], "week")
        assert keys.nunique() == 1
        assert keys.iloc[0] == pd.Timestamp("2026-07-06")


class TestPartialPeriod:
    def test_current_month_excluded(self):
        df = _df([
            {"report_date": pd.Timestamp("2026-06-15"), "cost_usd": 100.0,
             "impressions": 1, "clicks": 1, "conversions": 1.0, "conversions_value": 1.0},
            {"report_date": pd.Timestamp("2026-07-15"), "cost_usd": 100.0,
             "impressions": 1, "clicks": 1, "conversions": 1.0, "conversions_value": 1.0},
        ])
        out = resample(df, "month", today=pd.Timestamp("2026-07-27"))
        assert list(out["period"].dt.month) == [6]

    def test_day_grain_keeps_today(self):
        df = _df([{"report_date": pd.Timestamp("2026-07-27"), "cost_usd": 1.0,
                   "impressions": 1, "clicks": 1, "conversions": 1.0,
                   "conversions_value": 1.0}])
        assert len(resample(df, "day", today=pd.Timestamp("2026-07-27"))) == 1

    def test_drop_partial_off(self):
        df = _df([{"report_date": pd.Timestamp("2026-07-15"), "cost_usd": 1.0,
                   "impressions": 1, "clicks": 1, "conversions": 1.0,
                   "conversions_value": 1.0}])
        out = resample(df, "month", today=pd.Timestamp("2026-07-27"), drop_partial=False)
        assert len(out) == 1


class TestSafeRatio:
    @pytest.mark.parametrize("num,den", [(1.0, 0), (1.0, 0.0), (None, 5), (5, None)])
    def test_returns_none_not_inf(self, num, den):
        assert safe_ratio(num, den) is None

    def test_normal(self):
        assert safe_ratio(10, 4) == pytest.approx(2.5)

    def test_zero_conversions_yields_none_cpa(self):
        """转化 0 → CPA 无意义(分母 0)；但 ROAS 分母是 cost，0/50 = 0.0 是真结果。"""
        out = add_ratio_columns(_df([{"cost_usd": 50.0, "conversions": 0.0,
                                      "conversions_value": 0.0, "clicks": 10,
                                      "impressions": 100}]))
        assert out["cpa"].iloc[0] is None
        assert out["roas"].iloc[0] == pytest.approx(0.0)

    def test_no_inf_anywhere_in_mixed_column(self):
        """混合列里 pandas 把 None 转 NaN(Streamlit 渲染为空)——但绝不能出现 inf。"""
        out = add_ratio_columns(_df([
            {"cost_usd": 0.0, "conversions": 0.0, "conversions_value": 10.0,
             "clicks": 0, "impressions": 0},
            {"cost_usd": 20.0, "conversions": 4.0, "conversions_value": 80.0,
             "clicks": 10, "impressions": 200},
        ]))
        import numpy as np
        for col in ("roas", "cpa", "ctr", "cpc"):
            vals = pd.to_numeric(out[col], errors="coerce")
            assert not np.isinf(vals.dropna()).any(), f"{col} 出现 inf"
        assert out["roas"].iloc[1] == pytest.approx(4.0)


class TestBreakeven:
    def test_half_margin_is_2x(self):
        assert breakeven_roas(0.50) == pytest.approx(2.0)

    @pytest.mark.parametrize("bad", [0, -0.1, 1.5, None])
    def test_invalid_margin(self, bad):
        assert breakeven_roas(bad) is None


class TestVerdict:
    def test_insufficient_sample_wins_over_loss(self):
        """转化不足时不许显示亏损红灯——那会误导 Boss 砍掉有效广告。"""
        assert verdict(0.1, 2.0, conversions=3)[0] == "insufficient"

    def test_insufficient_sample_wins_over_profit(self):
        assert verdict(9.9, 2.0, conversions=3)[0] == "insufficient"

    @pytest.mark.parametrize("roas,expected", [
        (2.4, "profit"), (2.5, "profit"), (2.0, "marginal"),
        (2.39, "marginal"), (1.99, "loss"), (0.0, "loss"),
    ])
    def test_bands(self, roas, expected):
        assert verdict(roas, 2.0, conversions=50)[0] == expected

    def test_missing_roas(self):
        assert verdict(None, 2.0, conversions=50)[0] == "unknown"


class TestEmpty:
    def test_empty_frame(self):
        out = resample(pd.DataFrame(), "week")
        assert out.empty
        assert "roas" in out.columns

    def test_unknown_grain(self):
        with pytest.raises(ValueError):
            resample(_df([{"report_date": pd.Timestamp("2026-07-01"),
                           "cost_usd": 1.0, "impressions": 1, "clicks": 1,
                           "conversions": 1.0, "conversions_value": 1.0}]), "year")
