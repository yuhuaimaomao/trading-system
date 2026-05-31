import pytest

from analysis.screening.profiles import ProfileBuilder


class TestProfileBuilder:
    @pytest.fixture
    def builder(self, test_db_path):
        return ProfileBuilder(db_path=test_db_path)

    def test_build_returns_profiles(self, builder):
        from analysis.signals import StockScore

        candidate = StockScore(
            stock_code="000001",
            stock_name="平安银行",
            trend_mode="strong",
            score=85,
            price=10.68,
            change_pct=0,
            mcap=2072,
            circ_mcap=2072,
            turnover_rate=0.34,
            volume_ratio=0.74,
            ma5=10.74,
            ma10=10.90,
            ma20=11.11,
            ma5_angle=-0.28,
            industry="银行Ⅱ",
            mf_wan=-1118,
            mf_ratio=-0.005,
        )
        profiles = builder.build([candidate], trade_date="2026-05-25")

        assert len(profiles) == 1
        p = profiles[0]
        assert p.code == "000001"
        assert p.name == "平安银行"
        assert "price" in p.snapshot
        assert "ma5" in p.history
        assert isinstance(p.sectors, list)
        assert isinstance(p.telegraphs, list)
        assert isinstance(p.rps, dict)
        assert isinstance(p.valuation, dict)

    def test_build_empty_input(self, builder):
        profiles = builder.build([], trade_date="2026-05-25")
        assert profiles == []

    def test_build_multiple_stocks(self, builder):
        from analysis.signals import StockScore

        candidates = [
            StockScore(
                stock_code=code,
                stock_name=name,
                trend_mode="strong",
                score=75,
                price=10.0,
                change_pct=1.0,
                mcap=1000,
                circ_mcap=800,
                turnover_rate=2.0,
                volume_ratio=1.5,
                ma5=10.5,
                ma10=10.3,
                ma20=10.0,
                ma5_angle=0.5,
                industry="银行",
                mf_wan=100,
                mf_ratio=0.5,
            )
            for code, name in [("000001", "平安银行"), ("000002", "万科A")]
        ]
        profiles = builder.build(candidates, trade_date="2026-05-25")
        assert len(profiles) == 2
        codes = [p.code for p in profiles]
        assert "000001" in codes
        assert "000002" in codes

    def test_rps_has_keys(self, builder):
        from analysis.signals import StockScore

        candidate = StockScore(
            stock_code="000001",
            stock_name="平安银行",
            trend_mode="strong",
            score=85,
            price=10.68,
            change_pct=0,
            mcap=2072,
            circ_mcap=2072,
            turnover_rate=0.34,
            volume_ratio=0.74,
            ma5=10.74,
            ma10=10.90,
            ma20=11.11,
            ma5_angle=-0.28,
            industry="银行Ⅱ",
            mf_wan=-1118,
            mf_ratio=-0.005,
        )
        profiles = builder.build([candidate], trade_date="2026-05-25")
        p = profiles[0]
        assert "rps_20" in p.rps
        assert "rps_60" in p.rps
        assert "rps_120" in p.rps

    def test_market_state_passed_through(self, builder):
        from analysis.signals import StockScore

        candidate = StockScore(
            stock_code="000001",
            stock_name="平安银行",
            trend_mode="strong",
            score=85,
            price=10.68,
            change_pct=0,
            mcap=2072,
            circ_mcap=2072,
            turnover_rate=0.34,
            volume_ratio=0.74,
            ma5=10.74,
            ma10=10.90,
            ma20=11.11,
            ma5_angle=-0.28,
            industry="银行Ⅱ",
            mf_wan=-1118,
            mf_ratio=-0.005,
        )
        profiles = builder.build(
            [candidate], trade_date="2026-05-25", market_state="普涨"
        )
        assert profiles[0].market_state == "普涨"
