"""Portfolio 单元测试 — 开仓/平仓/盈亏/回撤"""

import pytest

from trade.exec.paper.portfolio import Portfolio, Position


class TestPosition:
    def test_update_price(self):
        p = Position(stock_code="000001", volume=100, avg_cost=10.0)
        p.update_price(12.0)
        assert p.current_price == 12.0
        assert p.market_value == 1200.0
        assert p.pnl == 200.0
        assert p.pnl_pct == pytest.approx(0.2)

    def test_update_price_loss(self):
        p = Position(stock_code="000001", volume=100, avg_cost=10.0)
        p.update_price(9.0)
        assert p.pnl == -100.0
        assert p.pnl_pct == pytest.approx(-0.1)

    def test_update_price_zero_cost(self):
        p = Position(stock_code="000001", volume=100, avg_cost=0.0)
        p.update_price(10.0)
        assert p.pnl_pct == 0.0  # 除零保护


class TestPortfolio:
    def test_initial_state(self):
        p = Portfolio(initial_cash=200000)
        assert p.cash == 200000
        assert p.total_value == 200000
        assert len(p.positions) == 0
        assert p.position_ratio == 0.0
        assert p.drawdown == 0.0

    def test_open_position(self):
        p = Portfolio(initial_cash=200000)
        ok = p.open_position("000001", "平安银行", 1000, 15.0, commission=12.75)
        assert ok
        assert "000001" in p.positions
        pos = p.positions["000001"]
        assert pos.volume == 1000
        # avg_cost 含佣金: (15000 + 12.75) / 1000 = 15.01275 → round(4) = 15.0128
        assert pos.avg_cost == pytest.approx(15.0128)
        assert p.cash == 200000 - 15000 - 12.75

    def test_open_position_insufficient_cash(self):
        p = Portfolio(initial_cash=1000)
        ok = p.open_position("000001", "平安银行", 1000, 50.0)
        assert not ok

    def test_close_position(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "平安银行", 1000, 15.0)
        p.positions["000001"].update_price(16.0)

        result = p.close_position("000001", 16.0, "止盈", commission=13.6)
        assert result is True
        assert "000001" not in p.positions
        # 卖出后现金增加了
        assert p.cash > 185000

    def test_close_nonexistent(self):
        p = Portfolio(initial_cash=200000)
        result = p.close_position("000001", 10.0, "test")
        assert result is False

    def test_update_prices(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "平安银行", 1000, 15.0)
        p.open_position("000002", "万科A", 500, 20.0)

        p.update_prices({"000001": 16.0, "000002": 19.0})
        assert p.positions["000001"].current_price == 16.0
        assert p.positions["000002"].pnl_pct == pytest.approx(-0.05)

    def test_drawdown_tracking(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "平安银行", 1000, 15.0)
        # update_prices 触发 _peak_value 更新
        p.update_prices({"000001": 20.0})
        assert p._peak_value > 200000

        p.update_prices({"000001": 10.0})
        assert p.drawdown > 0

    def test_position_ratio(self):
        p = Portfolio(initial_cash=200000)
        assert p.position_ratio == 0.0
        p.open_position("000001", "平安银行", 1000, 15.0)
        p.positions["000001"].update_price(15.0)
        assert p.position_ratio == pytest.approx(15000 / 200000)

    def test_snapshot(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "平安银行", 1000, 15.0)
        snap = p.snapshot("2026-06-01")
        assert snap.date == "2026-06-01"
        assert snap.position_count == 1
        assert snap.cash < 200000

    def test_multiple_positions(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "票A", 1000, 10.0)
        p.open_position("000002", "票B", 500, 20.0)
        p.open_position("000003", "票C", 200, 50.0)
        assert len(p.positions) == 3
        assert p.position_ratio == pytest.approx(30000 / 200000)
