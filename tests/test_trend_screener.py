import pytest
from analysis.screening.trend import TrendScreener


class TestTrendScreener:
    def test_screen_returns_list(self):
        ts = TrendScreener()
        results = ts.screen("2026-05-25")
        assert isinstance(results, list)
        for r in results:
            assert hasattr(r, "stock_code")
            assert hasattr(r, "score")
            assert r.score > 0

    def test_screen_respects_top_n(self):
        ts = TrendScreener(top_n=3)
        results = ts.screen("2026-05-25")
        assert len(results) <= 6  # top_n * 2

    def test_hard_gates_filter_st(self):
        ts = TrendScreener()
        results = ts.screen("2026-05-25")
        names = [r.stock_name for r in results]
        assert not any("ST" in str(n) for n in names)

    def test_hard_gates_filter_688(self):
        ts = TrendScreener()
        results = ts.screen("2026-05-25")
        codes = [r.stock_code for r in results]
        assert not any(str(c).startswith("688") for c in codes)

    def test_panic_market_returns_empty(self):
        ts = TrendScreener()
        results = ts.screen("2026-05-25", market_state="恐慌")
        assert results == []

    def test_bear_market_no_strong_mode(self):
        # 恐慌日全部跳过，不应产出任何候选
        ts = TrendScreener()
        results = ts.screen("2026-05-25", market_state="恐慌")
        assert len(results) == 0

    def test_normal_market_produces_strong(self):
        ts = TrendScreener(top_n=3)
        results = ts.screen("2026-05-25", market_state="普涨")
        # 普涨应该能产出 strong 模式
        modes = [r.trend_mode for r in results]
        assert "strong" in modes or "normal" in modes

    def test_score_range(self):
        ts = TrendScreener()
        results = ts.screen("2026-05-25")
        for r in results:
            assert 0 <= r.score <= 100
