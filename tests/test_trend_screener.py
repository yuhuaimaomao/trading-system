from analysis.screening.trend import TrendScreener


class TestTrendScreener:
    def test_screen_returns_list(self, test_db_path):
        ts = TrendScreener(db_path=test_db_path)
        results = ts.screen("2026-05-25")
        assert isinstance(results, list)
        for r in results:
            assert hasattr(r, "stock_code")
            assert hasattr(r, "score")
            assert r.score > 0

    def test_screen_respects_top_n(self, test_db_path):
        ts = TrendScreener(db_path=test_db_path, top_n=3)
        results = ts.screen("2026-05-25")
        assert len(results) <= 6  # top_n * 2

    def test_hard_gates_filter_st(self, test_db_path):
        ts = TrendScreener(db_path=test_db_path)
        results = ts.screen("2026-05-25")
        names = [r.stock_name for r in results]
        assert not any("ST" in str(n) for n in names)

    def test_hard_gates_filter_688(self, test_db_path):
        ts = TrendScreener(db_path=test_db_path)
        results = ts.screen("2026-05-25")
        codes = [r.stock_code for r in results]
        assert not any(str(c).startswith("688") for c in codes)

    def test_panic_market_returns_empty(self, test_db_path):
        ts = TrendScreener(db_path=test_db_path)
        results = ts.screen("2026-05-25", market_state="恐慌")
        assert results == []

    def test_normal_market_produces_strong(self, test_db_path):
        ts = TrendScreener(db_path=test_db_path, top_n=3)
        results = ts.screen("2026-05-25", market_state="普涨")
        modes = [r.trend_mode for r in results]
        assert "strong" in modes or "normal" in modes

    def test_score_range(self, test_db_path):
        ts = TrendScreener(db_path=test_db_path)
        results = ts.screen("2026-05-25")
        for r in results:
            assert 0 <= r.score <= 100
