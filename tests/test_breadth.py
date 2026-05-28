import pytest
from analysis.screening.breadth import MarketBreadth, classify_market_state


class TestClassifyMarketState:
    def test_bull_when_up_gt_3000(self):
        assert classify_market_state(3500, 1500, 50, 10, 0.5) == "普涨"

    def test_divide_when_up_1500_to_3000(self):
        assert classify_market_state(2000, 3000, 30, 20, -0.2) == "分化"

    def test_bear_when_up_800_to_1500(self):
        assert classify_market_state(1000, 4000, 15, 50, -1.5) == "普跌"

    def test_panic_when_up_lt_800(self):
        assert classify_market_state(500, 5000, 5, 800, -3.0) == "恐慌"

    def test_bounce_after_panic(self):
        assert (
            classify_market_state(2500, 2500, 50, 20, 1.5, prev_state="恐慌")
            == "连跌修复"
        )

    def test_oversold_end_3day_fall_and_limit_down_retreat(self):
        assert (
            classify_market_state(
                1200, 4000, 20, 100, -0.5,
                consecutive_down_days=4,
                limit_down_peak=300,
            )
            == "超跌末端"
        )

    def test_zero_total_returns_divide(self):
        assert classify_market_state(0, 0, 0, 0, 0) == "分化"


class TestMarketBreadth:
    def test_compute_returns_expected_keys(self):
        mb = MarketBreadth()
        result = mb.compute("2026-05-25")
        for key in (
            "up_count", "down_count", "flat_count",
            "limit_up_count", "limit_down_count",
            "index_change_pct", "market_state",
        ):
            assert key in result
        assert result["up_count"] + result["down_count"] + result["flat_count"] > 5000
        assert result["market_state"] in (
            "普涨", "分化", "普跌", "恐慌", "连跌修复", "超跌末端",
        )

    def test_save_and_read(self):
        mb = MarketBreadth()
        mb.save("2026-05-25")
        data = mb.get("2026-05-25")
        assert data is not None
        assert data["up_count"] is not None
        assert data["market_state"] in (
            "普涨", "分化", "普跌", "恐慌", "连跌修复", "超跌末端",
        )

    def test_get_nonexistent_returns_none(self):
        mb = MarketBreadth()
        assert mb.get("1999-01-01") is None
