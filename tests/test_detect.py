"""trade/detect/ 模块测试"""

from trade.detect.market_pattern import _session_phase, classify_market_pattern
from trade.detect.sector_trend import (
    get_concept_trend_score,
    get_sector_change,
    get_sector_decline,
    get_sector_recovery_risk,
    get_sector_trend,
)


class TestMarketPattern:
    def test_short_data(self):
        result = classify_market_pattern([3400.0] * 10, 3400, 3400)
        assert result == "normal"

    def test_uptrend(self):
        px = [3400.0 + i * 0.5 for i in range(100)]
        result = classify_market_pattern(px, max(px), px[0])
        assert result == "uptrend"

    def test_one_sided(self):
        px = [3450.0 - i * 0.5 for i in range(100)]
        result = classify_market_pattern(px, px[0], min(px))
        assert result == "one_sided"

    def test_panic_or_strong_downtrend(self):
        # 加速下跌 + 价格在低位。时段影响结果（尾盘可能判 late_dump）
        px = [3450.0] * 20 + [3450.0 - i * 1.5 for i in range(80)]
        result = classify_market_pattern(px, 3450, min(px))
        assert result != "uptrend" and result != "normal"
        assert isinstance(result, str)

    def test_same_hi_lo_returns_normal(self):
        result = classify_market_pattern([3400.0] * 50, 3400, 3400)
        assert result == "normal"

    def test_session_phase(self):
        phase = _session_phase()
        assert phase in (
            "pre_open",
            "opening",
            "morning",
            "late_morning",
            "lunch",
            "afternoon",
            "late_afternoon",
            "closing",
        )


class TestSectorTrend:
    def test_empty_industry(self):
        result = get_sector_trend("000001", {}, {})
        assert result == ""

    def test_insufficient_data(self):
        stats = {"dummy": {"trend_history": [1.0]}}
        result = get_sector_trend("000001", {"000001": "dummy"}, stats)
        assert "数据" in result

    def test_uptrend(self):
        history = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        stats = {
            "test_sector": {
                "trend_history": history,
                "relative": 0.6,
                "breadth": 0.5,
                "vol_ratio": 1.8,
                "continuity": 4,
            }
        }
        result = get_sector_trend("000001", {"000001": "test_sector"}, stats)
        assert "走强" in result or "持续走强" in result

    def test_concept_score(self):
        concept_stats = {"概念A": {"change_pct": 2.0}, "概念B": {"change_pct": 1.5}, "概念C": {"change_pct": 1.2}}
        concept_cache = {"000001": ["概念A", "概念B", "概念C"]}
        score, reason = get_concept_trend_score("000001", concept_cache, concept_stats)
        assert score >= 0

    def test_sector_change(self):
        stats = {"test_sector": {"change_pct": 2.5}}
        result = get_sector_change("000001", {"000001": "test_sector"}, stats)
        assert result == 2.5

    def test_sector_decline(self):
        stats = {"test_sector": {"trend_history": [3.0, 2.5, 2.0, 1.5, 1.0]}}
        result = get_sector_decline("000001", {"000001": "test_sector"}, stats)
        assert result is not None and result > 0

    def test_sector_recovery_risk(self):
        stats = {"test_sector": {"trend_history": [-3.0, -2.0, -1.5, -0.5, 0.0, 0.3, 0.5]}}
        result = get_sector_recovery_risk("000001", {"000001": "test_sector"}, stats)
        assert result is not None and result > 2.0
