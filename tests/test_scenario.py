"""trade/scenario/ 模块测试"""

from trade.monitor.state import MicroSignals
from trade.scenario.scenario_engine import ScenarioEngine
from trade.scenario.definitions import PROBABILITY_URGENCY, SCENARIO_SIGNALS


class TestScenarioEngine:
    def test_init(self):
        engine = ScenarioEngine()
        assert len(engine.probs) == 8
        assert abs(sum(engine.probs.values()) - 1.0) < 0.01

    def test_update_normal_stable(self):
        engine = ScenarioEngine()
        micro = MicroSignals(price_velocity=0.01, ema12_pos="on")
        outlook = engine.update(micro)
        assert outlook.primary.name == "normal_stable"
        assert outlook.primary.probability > 0.3

    def test_update_bearish(self):
        engine = ScenarioEngine()
        micro = MicroSignals(
            price_velocity=-0.05, price_accel=-0.03,
            ema12_pos="below", breadth_trend="deteriorating",
            lower_highs=True,
        )
        outlook = engine.update(micro)
        # 下跌信号应推高下跌情景概率
        assert outlook.primary.direction in ("bearish", "neutral")

    def test_update_bullish(self):
        engine = ScenarioEngine()
        micro = MicroSignals(
            price_velocity=0.05, price_accel=0.01,
            ema12_pos="above", breadth_trend="improving",
            higher_lows=True, higher_highs=True,
        )
        outlook = engine.update(micro)
        assert outlook.bias in ("bullish", "neutral")

    def test_probabilities_sum_to_one(self):
        engine = ScenarioEngine()
        micro = MicroSignals()
        for _ in range(10):
            engine.update(micro)
        assert abs(sum(engine.probs.values()) - 1.0) < 0.01

    def test_key_levels(self):
        engine = ScenarioEngine()
        micro = MicroSignals(price_velocity=0.02, ema12_pos="above")
        outlook = engine.update(micro, key_support=[3380.0], key_resistance=[3450.0])
        assert outlook.key_support == [3380.0]
        assert outlook.key_resistance == [3450.0]


class TestScenarioSignals:
    def test_all_scenarios_have_required_fields(self):
        for name, cfg in SCENARIO_SIGNALS.items():
            assert "label" in cfg
            assert "direction" in cfg
            assert "confirm" in cfg
            assert "reject" in cfg
            assert "threshold" in cfg
            assert "pre_action" in cfg

    def test_urgency_thresholds(self):
        assert len(PROBABILITY_URGENCY) == 4
        thresholds = [t for t, _, _ in PROBABILITY_URGENCY]
        assert thresholds == sorted(thresholds, reverse=True)
