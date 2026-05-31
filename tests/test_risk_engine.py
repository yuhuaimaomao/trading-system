# -*- coding: utf-8 -*-
"""风控引擎单元测试 — RiskEngine + 6 条风控规则"""

import pytest
from datetime import date
from unittest.mock import patch

from trade.risk.engine import RiskEngine, RiskResult
from trade.portfolio.portfolio import Portfolio, Position
from trade.risk.rules.stop_loss import check_stop_loss, check_time_stop, check_ma_stop
from trade.risk.rules.take_profit import check_take_profit, check_trailing_stop
from trade.risk.rules.max_drawdown import check_daily_loss_limit
from trade.risk.rules.concentration import check_concentration, get_sector_overexposure
from trade.risk.rules.market_env import get_market_environment, get_max_position
from trade.risk.rules.blacklist import is_blacklisted, is_risk_suspect, PERMANENT_BLACKLIST


# ====================== RiskEngine 初始化 ======================


class TestRiskEngineInit:
    def test_default_config(self):
        engine = RiskEngine()
        assert engine.max_single_pct == 0.20
        assert engine.max_sector_pct == 0.50
        assert engine.daily_loss_limit == 0.03
        assert engine.market_env == "swing"

    def test_custom_config(self):
        engine = RiskEngine(config={"max_single_pct": 0.15, "daily_loss_limit": 0.02})
        assert engine.max_single_pct == 0.15
        assert engine.daily_loss_limit == 0.02


# ====================== 黑名单 ======================


class TestBlacklist:
    def test_code_in_permanent_blacklist(self):
        assert is_blacklisted("000001") is False  # PERMANENT_BLACKLIST is empty

    def test_code_not_in_blacklist(self):
        assert is_blacklisted("600519") is False

    def test_risk_suspect_st_prefix(self):
        assert is_risk_suspect("ST瑞德") is True

    def test_risk_suspect_new_stock(self):
        assert is_risk_suspect("N三峡") is True

    def test_risk_suspect_normal(self):
        assert is_risk_suspect("平安银行") is False


# ====================== 市场环境 ======================


class TestMarketEnv:
    def test_bull_high_score(self):
        """强市：价格远超MA20+MA60+放量+普涨+多板块"""
        env = get_market_environment(index_price=3500, index_ma20=3200,
                                     index_ma60=3000, volume_trend=0.2,
                                     breadth_ratio=3.0, daily_amplitude=0.01,
                                     active_sectors=6)
        assert env == "bull"

    def test_bear_low_score(self):
        """弱市：价格远低MA20+MA60+缩量+普跌+少板块"""
        env = get_market_environment(index_price=3000, index_ma20=3300,
                                     index_ma60=3500, volume_trend=-0.2,
                                     breadth_ratio=0.3, daily_amplitude=0.04,
                                     active_sectors=1)
        assert env == "bear"

    def test_swing_mid_score(self):
        """指数略高于MA20但低于MA60，量能一般，得分在0-2之间为震荡"""
        env = get_market_environment(index_price=3320, index_ma20=3300,
                                     index_ma60=3350, volume_trend=0.02,
                                     breadth_ratio=1.0, daily_amplitude=0.01,
                                     active_sectors=3)
        assert env == "swing"

    def test_get_max_position_bull(self):
        assert get_max_position("bull") == 0.80

    def test_get_max_position_swing(self):
        assert get_max_position("swing") == 0.50

    def test_get_max_position_bear(self):
        assert get_max_position("bear") == 0.20

    def test_get_max_position_unknown_defaults_swing(self):
        assert get_max_position("unknown") == 0.50

    def test_zero_ma_handled(self):
        """MA 为 0 时跳过该维度"""
        env = get_market_environment(index_price=3300, index_ma20=0,
                                     index_ma60=0, volume_trend=0,
                                     breadth_ratio=0, daily_amplitude=0,
                                     active_sectors=0)
        assert env in ("bull", "swing", "bear")


# ====================== 集中度 ======================


class TestConcentration:
    def test_ok_within_limit(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "A", 1000, 10.00, sector_code="bank")
        ok, msg = check_concentration("000002", 0.10, "tech", pf, 0.20, 0.50)
        assert ok is True
        assert msg == ""

    def test_single_stock_exceeds(self):
        ok, msg = check_concentration("000001", 0.25, "", Portfolio(), 0.20, 0.50)
        assert ok is False

    def test_sector_exceeds(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "A", 2000, 20.00, sector_code="bank")
        ok, msg = check_concentration("000002", 0.05, "bank", pf, 0.20, 0.30)
        assert ok is False
        assert "板块" in msg

    def test_already_held_always_ok(self):
        pf = Portfolio()
        pf.open_position("000001", "A", 1000, 10.00, sector_code="bank")
        ok, msg = check_concentration("000001", 0.50, "bank", pf, 0.20, 0.30)
        assert ok is True

    def test_get_sector_overexposure_finds_overconcentrated(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "A", 6000, 10.00, sector_code="bank")
        result = get_sector_overexposure(pf, max_sector=0.50)
        assert "bank" in result

    def test_get_sector_overexposure_empty(self):
        pf = Portfolio()
        assert get_sector_overexposure(pf) == []


# ====================== 止损 ======================


class TestStopLoss:
    def test_triggered_when_price_below_stop(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       stop_loss=11.00)
        pos.update_price(10.50)
        result = check_stop_loss(pos)
        assert result != ""
        assert "止损" in result

    def test_not_triggered_when_price_above_stop(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       stop_loss=11.00)
        pos.update_price(11.50)
        assert check_stop_loss(pos) == ""

    def test_not_triggered_without_stop_loss_set(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       stop_loss=0)
        pos.update_price(10.00)
        assert check_stop_loss(pos) == ""

    def test_triggered_at_exact_stop_price(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       stop_loss=11.00)
        pos.update_price(11.00)
        result = check_stop_loss(pos)
        assert result != ""

    def test_time_stop_over_5_days_losing(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00)
        pos.update_price(10.00)  # pnl_pct < 0
        result = check_time_stop(pos, hold_days=6, max_days=5)
        assert result != ""
        assert "时间止损" in result

    def test_time_stop_not_triggered_at_5_days(self):
        """hold_days=5 不触发（> max_days, not >=）"""
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00)
        pos.update_price(10.00)
        assert check_time_stop(pos, hold_days=5, max_days=5) == ""

    def test_time_stop_not_triggered_when_profitable(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00)
        pos.update_price(15.00)
        assert check_time_stop(pos, hold_days=10) == ""

    def test_ma_stop_triggered(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00)
        pos.update_price(10.00)
        result = check_ma_stop(pos, 11.00)
        assert "均线止损" in result

    def test_ma_stop_not_triggered(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00)
        pos.update_price(12.00)
        assert check_ma_stop(pos, 11.00) == ""


# ====================== 止盈 ======================


class TestTakeProfit:
    def test_triggered_when_price_above_target(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       take_profit=15.00)
        pos.update_price(15.50)
        result = check_take_profit(pos)
        assert "止盈" in result

    def test_not_triggered_when_below_target(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       take_profit=15.00)
        pos.update_price(14.00)
        assert check_take_profit(pos) == ""

    def test_not_triggered_without_target(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       take_profit=0)
        pos.update_price(20.00)
        assert check_take_profit(pos) == ""

    def test_trailing_stop_triggered(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       trailing_stop=0.05)
        pos.update_price(15.00)  # highest=15, trail=14.25
        pos.update_price(14.00)  # 14.00 <= 14.25
        result = check_trailing_stop(pos)
        assert result != ""
        assert "移动止盈" in result

    def test_trailing_stop_not_triggered_above_trail(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       trailing_stop=0.05)
        pos.update_price(15.00)  # highest=15, trail=14.25
        pos.update_price(14.50)  # 14.50 > 14.25
        assert check_trailing_stop(pos) == ""

    def test_trailing_stop_no_highest_price(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       trailing_stop=0.05, highest_price=0)
        pos.update_price(9.00)
        assert check_trailing_stop(pos) == ""

    def test_trailing_stop_zero_trailing(self):
        pos = Position(stock_code="000001", volume=1000, avg_cost=12.00,
                       trailing_stop=0)
        pos.update_price(10.00)
        assert check_trailing_stop(pos) == ""


# ====================== 日内熔断 ======================


class TestDailyLossLimit:
    def test_triggered_when_loss_exceeds(self):
        result = check_daily_loss_limit(daily_pnl=-5000, total_value=100000, max_loss_pct=0.03)
        assert result is True

    def test_not_triggered_within_limit(self):
        result = check_daily_loss_limit(daily_pnl=-2000, total_value=100000, max_loss_pct=0.03)
        assert result is False

    def test_not_triggered_when_profitable(self):
        result = check_daily_loss_limit(daily_pnl=3000, total_value=100000, max_loss_pct=0.03)
        assert result is False

    def test_zero_total_value_handled(self):
        result = check_daily_loss_limit(daily_pnl=-1000, total_value=0, max_loss_pct=0.03)
        assert result is False

    def test_exact_at_limit(self):
        # 恰好 3% 不触发（> not >=）
        result = check_daily_loss_limit(daily_pnl=-3000, total_value=100000, max_loss_pct=0.03)
        assert result is False


# ====================== RiskEngine 开仓检查 ======================


class TestRiskEngineCanOpen:
    @pytest.fixture
    def engine(self):
        return RiskEngine()

    @pytest.fixture
    def pf(self):
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "平安银行", 1000, 20.00, sector_code="bank")
        return p

    def test_can_open_normal(self, engine, pf):
        result = engine.can_open("600519", 0.10, sector_code="tech", portfolio=pf)
        assert result.allowed is True

    def test_can_open_market_env_limit(self, engine, pf):
        engine.market_env = "bear"
        result = engine.can_open("600519", 0.25, portfolio=pf)
        assert result.allowed is False

    def test_can_open_concentration_fails(self, engine, pf):
        result = engine.can_open("000002", 0.25, sector_code="bank", portfolio=pf)
        assert result.allowed is False

    def test_can_open_no_portfolio_skips_concentration(self, engine):
        """无 portfolio 时跳过集中度检查，只检查黑名单和市场环境"""
        engine.market_env = "bull"
        result = engine.can_open("600519", 0.10)
        assert result.allowed is True

    def test_can_open_sector_within_limit(self, engine, pf):
        """不同板块，集中度不超限"""
        result = engine.can_open("000002", 0.05, sector_code="tech", portfolio=pf)
        assert result.allowed is True


# ====================== RiskEngine 持仓巡检 ======================


class TestRiskEngineCheckPositions:
    @pytest.fixture
    def engine(self):
        return RiskEngine()

    @pytest.fixture
    def pf(self):
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "平安银行", 1000, 12.00, stop_loss=11.00,
                        take_profit=15.00, trailing_stop=0.05)
        p.open_position("000002", "万科A", 500, 25.00, stop_loss=22.00)
        return p

    def test_no_close_signals_when_normal(self, engine, pf):
        signals = engine.check_positions(
            {"000001": 12.50, "000002": 26.00}, pf)
        assert len(signals) == 0

    def test_stop_loss_triggered(self, engine, pf):
        signals = engine.check_positions(
            {"000001": 10.50, "000002": 26.00}, pf)
        sl_signal = [s for s in signals if s["stock_code"] == "000001"]
        assert len(sl_signal) == 1
        assert sl_signal[0]["priority"] == 5

    def test_take_profit_triggered(self, engine, pf):
        signals = engine.check_positions(
            {"000001": 15.50, "000002": 26.00}, pf)
        tp_signal = [s for s in signals if "止盈" in s["reason"]]
        assert len(tp_signal) >= 1

    def test_trailing_stop_triggered(self, engine, pf):
        pf.positions["000001"].update_price(16.00)
        pf.positions["000001"].update_price(15.10)
        signals = engine.check_positions(
            {"000001": 15.10, "000002": 26.00}, pf)
        trail_signal = [s for s in signals if "移动" in s["reason"]]
        assert len(trail_signal) >= 1

    def test_daily_loss_meltdown(self, engine, pf):
        pf._prev_total = pf.total_value
        pf.update_prices({"000001": 10.00, "000002": 20.00})
        signals = engine.check_positions(
            {"000001": 10.00, "000002": 20.00}, pf)
        meltdown = [s for s in signals if "熔断" in s["reason"]]
        assert len(meltdown) >= 1
        assert meltdown[0]["priority"] == 4

    def test_time_stop_over_5_days(self, engine):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "A", 1000, 12.00,
                         entry_date="2026-05-20", trailing_stop=0)
        pf.positions["000001"].update_price(10.00)
        signals = engine.check_positions(
            {"000001": 10.00}, pf, trade_date="2026-05-29")
        time_sig = [s for s in signals if "时间止损" in s["reason"]]
        assert len(time_sig) >= 1
        assert time_sig[0]["priority"] == 8

    def test_stop_loss_takes_priority_over_take_profit(self, engine, pf):
        """同时触发止损和止盈时，止损先检查"""
        pf.positions["000001"].stop_loss = 14.00
        pf.positions["000001"].take_profit = 13.00
        signals = engine.check_positions(
            {"000001": 13.50, "000002": 26.00}, pf)
        sl_signal = [s for s in signals if s["stock_code"] == "000001"]
        assert len(sl_signal) >= 1
        assert "止损" in sl_signal[0]["reason"]

    def test_empty_positions(self, engine):
        pf = Portfolio()
        signals = engine.check_positions({}, pf)
        assert len(signals) == 0

    def test_missing_price_uses_current(self, engine, pf):
        """价格缺失时用 pos.current_price"""
        pf.positions["000001"].update_price(10.50)
        signals = engine.check_positions({"000002": 26.00}, pf)
        sl_signal = [s for s in signals if s["stock_code"] == "000001"]
        assert len(sl_signal) == 1


# ====================== RiskEngine 状态摘要 ======================


class TestRiskEngineStatus:
    def test_get_risk_status_empty(self):
        engine = RiskEngine()
        pf = Portfolio(initial_cash=100000)
        status = engine.get_risk_status(pf)
        assert status["market_env"] == "swing"
        assert status["position_count"] == 0
        assert status["total_value"] == 100000

    def test_get_risk_status_with_positions(self):
        engine = RiskEngine()
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "A", 1000, 20.00, sector_code="bank")
        pf.update_prices({"000001": 21.00})
        status = engine.get_risk_status(pf, prices={"000001": 21.00})
        assert status["position_count"] == 1
        assert len(status["positions"]) == 1
        assert "bank" in status["sector_exposure"]


# ====================== update_market_env ======================


class TestUpdateMarketEnv:
    def test_updates_env_to_bull(self):
        engine = RiskEngine()
        engine.update_market_env(
            index_ma20=3200, index_price=3500, index_ma60=3000,
            volume_trend=0.2, breadth_ratio=3.0, daily_amplitude=0.01,
            active_sectors=6)
        assert engine.market_env == "bull"

    def test_updates_env_to_bear(self):
        engine = RiskEngine()
        engine.update_market_env(
            index_ma20=3300, index_price=3000, index_ma60=3500,
            volume_trend=-0.2, breadth_ratio=0.3, daily_amplitude=0.04,
            active_sectors=1)
        assert engine.market_env == "bear"


# ====================== 多规则组合 ======================


class TestMultipleRulesCombined:
    def test_multiple_positions_different_signals(self):
        engine = RiskEngine()
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "A", 1000, 12.00, stop_loss=11.00)
        pf.open_position("000002", "B", 500, 25.00, take_profit=28.00)
        pf.open_position("000003", "C", 800, 15.00, trailing_stop=0.05)
        pf.positions["000003"].update_price(20.00)  # highest=20
        pf.positions["000003"].update_price(18.50)  # trail=19, triggered

        signals = engine.check_positions(
            {"000001": 10.00, "000002": 30.00, "000003": 18.50}, pf)
        # 止损(A) + 止盈(B) + 移动止盈(C) = 3
        assert len(signals) == 3

    def test_meltdown_and_stop_loss_simultaneous(self):
        """日内熔断 + 个股止损同时触发：两者都在列表中"""
        engine = RiskEngine()
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "A", 10000, 10.00, stop_loss=9.50)
        pf._prev_total = pf.total_value
        pf.update_prices({"000001": 9.20})
        signals = engine.check_positions({"000001": 9.20}, pf)
        reasons = "|".join(s["reason"] for s in signals)
        assert "熔断" in reasons
        assert "止损" in reasons

    def test_two_positions_same_sector_both_stop(self):
        """同一板块两只票同时触发止损"""
        engine = RiskEngine()
        pf = Portfolio(initial_cash=200000)
        pf.open_position("000001", "A", 1000, 15.00, stop_loss=12.00,
                         sector_code="bank")
        pf.open_position("000002", "B", 500, 30.00, stop_loss=25.00,
                         sector_code="bank")
        signals = engine.check_positions(
            {"000001": 10.00, "000002": 20.00}, pf)
        assert len(signals) >= 2
