"""健康检查框架 + 独立重算验证测试"""

import pytest
from trade.monitor.health_checks import (
    CheckContext,
    _expected_base_tighten,
    _expected_sector_mult,
    _recompute_adjustment,
    run_checks,
)


class TestExpectedBaseTighten:
    def test_safe(self):
        assert _expected_base_tighten("safe") == (1.0, 1.0, 1.0)

    def test_cautious(self):
        sl, tp, trail = _expected_base_tighten("cautious")
        assert sl == 0.92
        assert tp == 1.0
        assert trail == 0.92

    def test_dangerous(self):
        sl, tp, trail = _expected_base_tighten("dangerous")
        assert sl == 0.85
        assert tp == 0.90
        assert trail == 0.85

    def test_extreme(self):
        sl, tp, trail = _expected_base_tighten("extreme")
        assert sl == 0.70
        assert tp == 0.80
        assert trail == 0.70

    def test_unknown_defaults_to_safe(self):
        assert _expected_base_tighten("unknown") == (1.0, 1.0, 1.0)


class TestExpectedSectorMult:
    def test_accel_down(self):
        assert _expected_sector_mult("持续走弱 加速 -3% 弱于大盘") == 0.90

    def test_weak(self):
        assert _expected_sector_mult("持续走弱 -1%") == 0.95

    def test_normal(self):
        assert _expected_sector_mult("横盘") == 1.0

    def test_strong(self):
        assert _expected_sector_mult("持续走强 +2%") == 1.0


class TestRecomputeAdjustment:
    def test_dangerous_sector_accel_matches(self):
        """危险大盘 + 板块加速走弱 → 预期 0.85*0.90=0.765"""
        ctx = CheckContext(
            risk_level="dangerous",
            sector_trends={"000001": "持续走弱 加速 -3% 弱于大盘"},
            pos_meta={"000001": {"_sl_tighten": 0.765, "_tp_lower": 0.81, "_trail_tighten": 0.765}},
        )
        alerts = _recompute_adjustment(ctx)
        assert len(alerts) == 0  # 一致

    def test_divergence_detected(self):
        """系统用了错误值 → 应该告警"""
        from trade.portfolio.portfolio import Position
        ctx = CheckContext(
            risk_level="extreme",
            sector_trends={"000001": "横盘"},
            positions={"000001": Position(stock_code="000001", volume=100, avg_cost=10.0)},
            pos_meta={"000001": {"_sl_tighten": 1.0}},  # 应该是 0.70*1.0=0.70
        )
        alerts = _recompute_adjustment(ctx)
        assert any("止损因子偏离" in a for a in alerts)

    def test_empty_meta_skipped(self):
        """还没跑过 _check_positions 的持仓跳过"""
        ctx = CheckContext(
            risk_level="safe",
            sector_trends={"000001": "横盘"},
            pos_meta={},  # 空
        )
        assert _recompute_adjustment(ctx) == []


class TestRunChecks:
    def test_all_pass_on_clean_context(self):
        """正常数据不应产生告警"""
        ctx = CheckContext(
            cash=100000,
            total_value=100000,
            daily_pnl=0,
            positions={},
            max_positions=5,
            prices={},
            index_prices=[],
            index_pre_close=3200,
            qmt_change_pct=0.0,
            pos_meta={},
            bought_watch={},
            scan_count=1,
            baseline_pre_close=3200,
            trade_date="2026-06-01",
        )
        alerts = run_checks(ctx)
        assert len(alerts) == 0

    def test_account_imbalance_detected(self):
        """账户不平 → 告警"""
        ctx = CheckContext(
            cash=100000, total_value=105000,  # 差了 5000
            positions={},
        )
        alerts = run_checks(ctx)
        assert any("账户不一致" in a for a in alerts)

    def test_preclose_drift_detected(self):
        """昨收价漂移 → 告警"""
        ctx = CheckContext(
            baseline_pre_close=3200.0,
            index_pre_close=3190.0,  # 变了 10 点
            index_prices=[3190.0],
            qmt_change_pct=-0.0031,
        )
        alerts = run_checks(ctx)
        assert any("昨收价漂移" in a for a in alerts)
