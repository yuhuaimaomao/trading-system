import pytest

from analysis.strategy import StrategyPipeline


class TestStrategyE2E:
    def test_full_pipeline_runs(self, test_db_path):
        """整个管线能跑通，不抛异常"""
        pipeline = StrategyPipeline(db_path=test_db_path)
        try:
            signals = pipeline.run(trade_date="2026-05-25")
            assert isinstance(signals, list)
        except Exception as e:
            pytest.fail(f"管线运行异常: {e}")

    def test_market_breadth_integration(self, test_db_path):
        """大盘数据写入成功"""
        from analysis.screening.breadth import MarketBreadth

        mb = MarketBreadth(db_path=test_db_path)
        data = mb.get("2026-05-25")
        if data:
            assert data["up_count"] is not None
            assert data["market_state"] in (
                "普涨",
                "分化",
                "普跌",
                "恐慌",
                "连跌修复",
                "超跌末端",
            )
        else:
            data = mb.save("2026-05-25")
            assert data["up_count"] > 0

    def test_screening_produces_candidates(self, test_db_path):
        """筛选能产出候选"""
        from analysis.screening.trend import TrendScreener

        ts = TrendScreener(db_path=test_db_path, top_n=5)
        results = ts.screen("2026-05-25")
        assert len(results) > 0

    def test_profile_builder_enriches(self, test_db_path):
        """ProfileBuilder 能成功富化"""
        from analysis.screening.profiles import ProfileBuilder
        from analysis.screening.trend import TrendScreener

        ts = TrendScreener(db_path=test_db_path, top_n=3)
        candidates = ts.screen("2026-05-25")
        if candidates:
            builder = ProfileBuilder(db_path=test_db_path)
            profiles = builder.build(candidates, "2026-05-25")
            assert len(profiles) > 0
            p = profiles[0]
            assert p.code
            assert p.name
            assert p.score > 0
            assert isinstance(p.tags, list)
            assert "price" in p.snapshot
