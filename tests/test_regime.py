"""trade/decision/regime.py 测试"""

from trade.decision.regime import PATTERN_ALERT, PATTERN_REGIME, _upgrade_risk, assess_regime


class TestUpgradeRisk:
    def test_safe_to_cautious(self):
        assert _upgrade_risk("safe") == "cautious"

    def test_cautious_to_dangerous(self):
        assert _upgrade_risk("cautious") == "dangerous"

    def test_dangerous_to_extreme(self):
        assert _upgrade_risk("dangerous") == "extreme"

    def test_extreme_stays(self):
        assert _upgrade_risk("extreme") == "extreme"


class TestAssessRegime:
    def test_normal(self):
        regime = assess_regime("normal", 3400, 3390, 0.003)
        assert regime.pattern == "normal"
        assert regime.risk_level == "safe"
        assert regime.allow_buy
        assert regime.position_mult == 1.0

    def test_uptrend(self):
        regime = assess_regime("uptrend", 3450, 3390, 0.018)
        assert regime.pattern == "uptrend"
        assert regime.allow_buy

    def test_panic(self):
        regime = assess_regime("panic", 3300, 3400, -0.03)
        assert regime.risk_level == "extreme"
        assert not regime.allow_buy

    def test_below_ma20(self):
        regime = assess_regime("normal", 3350, 3400, -0.015, ma20=3400, ma60=3450)
        assert regime.risk_level in ("cautious", "dangerous", "extreme")

    def test_breadth_down(self):
        regime = assess_regime("normal", 3400, 3390, 0.001, market_breadth={"up": 200, "down": 800})
        assert regime.risk_level in ("cautious", "dangerous")

    def test_pattern_regime_all_16(self):
        assert len(PATTERN_REGIME) == 16
        for pattern in PATTERN_REGIME:
            cfg = PATTERN_REGIME[pattern]
            assert "risk_level" in cfg
            assert "allow_buy" in cfg
            assert "position_mult" in cfg

    def test_pattern_alert_all(self):
        assert len(PATTERN_ALERT) == 16
