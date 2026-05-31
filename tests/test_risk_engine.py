"""RiskEngine 单元测试 — 开仓检查/持仓巡检/止损止盈"""

import pytest
from trade.portfolio.portfolio import Portfolio, Position
from trade.risk.engine import RiskEngine
from trade.risk.rules.stop_loss import check_stop_loss, check_time_stop
from trade.risk.rules.take_profit import check_take_profit, check_trailing_stop
from trade.risk.rules.concentration import check_concentration
from trade.risk.rules.market_env import get_market_environment


class TestStopLoss:
    def test_trigger(self):
        pos = Position(stock_code="000001", avg_cost=10.0, volume=100)
        pos.take_profit = 0
        pos.stop_loss = 9.5
        pos.update_price(9.4)
        result = check_stop_loss(pos)
        assert result != ""

    def test_no_trigger(self):
        pos = Position(stock_code="000001", avg_cost=10.0, volume=100)
        pos.stop_loss = 9.5
        pos.update_price(9.8)
        assert check_stop_loss(pos) == ""

    def test_no_stop_set(self):
        pos = Position(stock_code="000001", volume=100)
        pos.stop_loss = 0
        pos.update_price(5.0)
        assert check_stop_loss(pos) == ""


class TestTakeProfit:
    def test_trigger(self):
        pos = Position(stock_code="000001", volume=100)
        pos.take_profit = 15.0
        pos.update_price(15.5)
        result = check_take_profit(pos)
        assert "目标止盈" in result

    def test_no_trigger(self):
        pos = Position(stock_code="000001", volume=100)
        pos.take_profit = 15.0
        pos.update_price(14.0)
        assert check_take_profit(pos) == ""

    def test_no_tp_set(self):
        pos = Position(stock_code="000001", volume=100)
        pos.take_profit = 0
        assert check_take_profit(pos) == ""


class TestTrailingStop:
    def test_trigger(self):
        pos = Position(stock_code="000001", avg_cost=10.0, volume=100)
        pos.trailing_stop = 0.05
        pos.highest_price = 20.0
        pos.update_price(18.0)  # 18 < 20 * 0.95 = 19.0
        result = check_trailing_stop(pos)
        assert "移动止盈" in result

    def test_no_trigger(self):
        pos = Position(stock_code="000001", avg_cost=10.0, volume=100)
        pos.trailing_stop = 0.05
        pos.highest_price = 20.0
        pos.update_price(19.5)  # 19.5 > 19.0
        assert check_trailing_stop(pos) == ""


class TestConcentration:
    def test_single_stock_limit(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "票A", 10000, 10.0)  # 100k
        ok, _ = check_concentration("000002", 0.10, "", p, 0.20, 0.50)
        assert ok

    def test_single_stock_exceeded(self):
        p = Portfolio(initial_cash=200000)
        # 直接请求 > 20% 仓位
        ok, msg = check_concentration("000001", 0.25, "", p, 0.20, 0.50)
        assert not ok
        assert "超上限" in msg


class TestMarketEnv:
    def test_bull(self):
        env = get_market_environment(3300, 3180, 3100, 0.20, 3.0, 0.01, 8)
        assert env == "bull"

    def test_bear(self):
        env = get_market_environment(3000, 3200, 3100, -0.20, 0.3, 0.04, 1)
        assert env == "bear"

    def test_swing(self):
        # 价格略低于 MA20 且偏离不大 → 中性偏弱
        env = get_market_environment(3180, 3200, 3140, 0.02, 1.0, 0.02, 3)
        assert env == "swing"


class TestRiskEngine:
    def test_can_open_normal(self):
        engine = RiskEngine()
        engine.update_market_env(3250, 3200, 3100, 0.05, 1.5, 0.01, 5)
        result = engine.can_open("000001", 0.10)
        assert result.allowed

    def test_can_open_bear_market(self):
        engine = RiskEngine()
        engine.update_market_env(3000, 3200, 3100, -0.20, 0.3, 0.04, 1)
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 10000, 10.0)
        result = engine.can_open("000002", 0.30, portfolio=portfolio)
        assert not result.allowed  # bear 上限 20%, 已有 50%+

    def test_can_open_blacklisted(self):
        # 黑名单目前为空（PERMANENT_BLACKLIST = set()）
        # 普通代码不触发黑名单
        from trade.risk.rules.blacklist import is_blacklisted
        assert not is_blacklisted("600519")
        assert not is_blacklisted("000001")

    def test_check_positions_stop_loss(self):
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos = portfolio.positions["000001"]
        pos.stop_loss = 9.5
        pos.take_profit = 15.0
        pos.trailing_stop = 0.05
        pos.highest_price = 10.0

        signals = engine.check_positions(
            {"000001": 9.0}, portfolio  # 跌破止损
        )
        assert len(signals) > 0
        assert signals[0]["stock_code"] == "000001"
        assert signals[0]["priority"] == 5  # 止损优先级


# ═══════════════════════════════════════════════════════════════
# 纯函数测试（should_stop_loss / should_take_profit / should_trailing_stop）
# ═══════════════════════════════════════════════════════════════

class TestPureStopLoss:
    def test_triggers(self):
        from trade.risk.rules.stop_loss import should_stop_loss
        triggered, sl = should_stop_loss(9.5, 10.0, 9.8)
        assert triggered
        assert sl == pytest.approx(9.8)

    def test_no_trigger(self):
        from trade.risk.rules.stop_loss import should_stop_loss
        triggered, _ = should_stop_loss(9.9, 10.0, 9.5)
        assert not triggered

    def test_tighten_triggers_earlier(self):
        from trade.risk.rules.stop_loss import should_stop_loss
        # tighten=0.7: effective=10-(10-9.5)*0.7=9.65, floor=9.5*0.85=8.075
        # price 9.6 <= 9.65 → trigger
        triggered, sl = should_stop_loss(9.6, 10.0, 9.5, tighten=0.70)
        assert triggered
        assert sl == pytest.approx(9.65, abs=0.01)

    def test_floor_protection(self):
        from trade.risk.rules.stop_loss import should_stop_loss
        # tighten=5.0 (极端): effective=10-(10-9.8)*5.0=9.0, floor=8.33
        # max(9.0, 8.33)=9.0, price 8.5 <= 9.0 → trigger
        triggered, sl = should_stop_loss(8.5, 10.0, 9.8, tighten=5.0)
        assert triggered
        assert sl == pytest.approx(9.0, abs=0.01)


class TestPureTakeProfit:
    def test_triggers(self):
        from trade.risk.rules.take_profit import should_take_profit
        triggered, tp = should_take_profit(12.5, 10.0, 12.0)
        assert triggered
        assert tp == 12.0

    def test_no_trigger(self):
        from trade.risk.rules.take_profit import should_take_profit
        triggered, _ = should_take_profit(11.0, 10.0, 12.0)
        assert not triggered

    def test_tp_lower_triggers_earlier(self):
        from trade.risk.rules.take_profit import should_take_profit
        # tp_lower=0.8: effective_tp=10+(15-10)*0.8=14.0, price 14.2 >=14 → trigger
        triggered, tp = should_take_profit(14.2, 10.0, 15.0, tp_lower=0.80)
        assert triggered
        assert tp == pytest.approx(14.0)


class TestPureTrailingStop:
    def test_triggers(self):
        from trade.risk.rules.take_profit import should_trailing_stop
        triggered, trail = should_trailing_stop(9.0, 20.0, 0.05)
        # 20*0.95=19, 9 <= 19 → trigger
        assert triggered

    def test_no_trigger(self):
        from trade.risk.rules.take_profit import should_trailing_stop
        triggered, _ = should_trailing_stop(19.5, 20.0, 0.05)
        # 19.5 > 19 → no trigger
        assert not triggered

    def test_tighten_triggers_earlier(self):
        from trade.risk.rules.take_profit import should_trailing_stop
        # trail_tighten=0.7: effective_trail=0.05*0.7=0.035, trail=20*0.965=19.3
        # 19.2 <= 19.3 → trigger
        triggered, _ = should_trailing_stop(19.2, 20.0, 0.05, trail_tighten=0.70)
        assert triggered
