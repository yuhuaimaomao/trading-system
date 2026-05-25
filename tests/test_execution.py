# -*- coding: utf-8 -*-
"""执行层单元测试 — ManualExecutor & PaperExecutor"""

from unittest.mock import MagicMock

import pytest

from system.config.settings import MIN_COMMISSION, STAMP_TAX_RATE, DEFAULT_COMMISSION_RATE
from trade.execution.manual import ManualExecutor
from trade.execution.paper import PaperExecutor
from trade.portfolio.portfolio import Portfolio
from analysis.signals import OrderSignal, SignalType, SignalSource


# =====================  Fixtures  =====================


@pytest.fixture
def buy_signal():
    return OrderSignal(
        stock_code="000001",
        stock_name="平安银行",
        signal_type=SignalType.BUY,
        source=SignalSource.RULE,
        buy_zone_min=12.00,
        buy_zone_max=13.00,
        target_position=0.1,
        stop_loss=11.00,
        take_profit=15.00,
        strategy_name="trend_momentum",
        signal_score=75.0,
        reason="趋势强劲",
    )


@pytest.fixture
def sell_signal():
    return OrderSignal(
        stock_code="000001",
        stock_name="平安银行",
        signal_type=SignalType.SELL,
        source=SignalSource.RULE,
        sell_reason="止盈",
        strategy_name="trend_momentum",
    )


@pytest.fixture
def mock_repo():
    repo = MagicMock()
    repo.insert_signal.return_value = 42
    repo.insert_order.return_value = 99
    return repo


@pytest.fixture
def mock_telegram():
    return MagicMock()


@pytest.fixture
def portfolio():
    return Portfolio(initial_cash=100000)


# =====================  ManualExecutor Tests  =====================


class TestManualExecutorSubmit:
    def test_submit_returns_signal_id(self, buy_signal, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram)
        executor.repo = mock_repo
        signal_id = executor.submit(buy_signal)
        assert signal_id == 42

    def test_submit_calls_telegram(self, buy_signal, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram)
        executor.repo = mock_repo
        executor.submit(buy_signal)
        mock_telegram.send.assert_called_once()

    def test_submit_does_not_call_telegram_when_none(self, buy_signal, mock_repo):
        executor = ManualExecutor(telegram_bot=None)
        executor.repo = mock_repo
        executor.submit(buy_signal)
        # Should not raise

    def test_submit_inserts_correct_fields(self, buy_signal, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram)
        executor.repo = mock_repo
        executor.submit(buy_signal)
        call_args = mock_repo.insert_signal.call_args[0][0]
        assert call_args["stock_code"] == "000001"
        assert call_args["stock_name"] == "平安银行"
        assert call_args["signal_type"] == "BUY"
        assert call_args["status"] == "pending"
        assert call_args["trade_date"] is not None

    def test_submit_caches_pending_signal(self, buy_signal, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram)
        executor.repo = mock_repo
        executor.submit(buy_signal)
        assert 42 in executor._pending_signals
        assert executor._pending_signals[42]["stock_code"] == "000001"


class TestManualExecutorConfirm:
    def test_confirm_updates_signal_status(self, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram)
        executor.repo = mock_repo
        executor.confirm(signal_id=1, price=12.50, volume=1000, code="000001")
        mock_repo.update_signal_status.assert_called_with(1, "executed")

    def test_confirm_inserts_order(self, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram)
        executor.repo = mock_repo
        executor.confirm(signal_id=1, price=12.50, volume=1000, code="000001")
        order = mock_repo.insert_order.call_args[0][0]
        assert order["stock_code"] == "000001"
        assert order["order_type"] == "buy"
        assert order["order_price"] == 12.50
        assert order["order_volume"] == 1000
        assert order["filled_amount"] == 12500.0

    def test_confirm_returns_order_id(self, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram)
        executor.repo = mock_repo
        order_id = executor.confirm(signal_id=1, price=12.50, volume=1000, code="000001")
        assert order_id == 99

    def test_confirm_creates_portfolio_position(self, mock_repo, mock_telegram, portfolio):
        executor = ManualExecutor(telegram_bot=mock_telegram, portfolio=portfolio)
        executor.repo = mock_repo
        executor.confirm(signal_id=1, price=12.50, volume=1000, code="000001", name="平安银行")
        pos = portfolio.positions.get("000001")
        assert pos is not None
        assert pos.volume == 1000
        assert pos.avg_cost == 12.50
        assert portfolio.cash < 100000  # 现金减少

    def test_confirm_uses_cached_info_when_code_empty(self, buy_signal, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram)
        executor.repo = mock_repo
        executor.submit(buy_signal)
        executor.confirm(signal_id=42, price=12.50, volume=500)
        order = mock_repo.insert_order.call_args[0][0]
        assert order["stock_code"] == "000001"

    def test_confirm_no_portfolio_no_crash(self, mock_repo, mock_telegram):
        executor = ManualExecutor(telegram_bot=mock_telegram, portfolio=None)
        executor.repo = mock_repo
        executor.confirm(signal_id=1, price=12.50, volume=1000, code="000001")


class TestManualExecutorReject:
    def test_reject_updates_status(self, mock_repo):
        executor = ManualExecutor()
        executor.repo = mock_repo
        executor.reject(signal_id=5)
        mock_repo.update_signal_status.assert_called_with(5, "rejected")


# =====================  PaperExecutor Tests  =====================


class TestPaperExecutorCommission:
    def test_buy_commission_min(self):
        executor = PaperExecutor()
        # 金额很小，佣金低于最低5元
        result = executor._calc_commission(1000, is_sell=False)
        assert result == MIN_COMMISSION  # 至少5元

    def test_sell_commission_includes_stamp_tax(self):
        executor = PaperExecutor()
        amount = 100000
        result = executor._calc_commission(amount, is_sell=True)
        expected_brokerage = max(amount * DEFAULT_COMMISSION_RATE, MIN_COMMISSION)
        expected_stamp = amount * STAMP_TAX_RATE
        assert result == pytest.approx(expected_brokerage + expected_stamp)

    def test_commission_large_amount(self):
        executor = PaperExecutor()
        amount = 500000
        result = executor._calc_commission(amount, is_sell=False)
        expected = amount * DEFAULT_COMMISSION_RATE
        assert result == pytest.approx(expected)

    def test_commission_sell_below_min_still_pays_stamp(self):
        executor = PaperExecutor()
        amount = 10000
        result = executor._calc_commission(amount, is_sell=True)
        # brokerage reaches MIN_COMMISSION, stamp is on top
        assert result >= MIN_COMMISSION
        assert result > amount * DEFAULT_COMMISSION_RATE + amount * STAMP_TAX_RATE - 1


class TestPaperExecutorCalcShares:
    def test_calc_shares_basic(self, buy_signal):
        p = Portfolio(initial_cash=100000)
        executor = PaperExecutor(portfolio=p)
        shares = executor._calc_shares(buy_signal, price=12.50)
        target_value = p.total_value * 0.1  # 10000
        expected = int(target_value / 12.50 / 100) * 100
        assert shares == expected  # 800股

    def test_calc_shares_min_lot(self, buy_signal):
        p = Portfolio(initial_cash=1000)  # 小资金
        executor = PaperExecutor(portfolio=p)
        shares = executor._calc_shares(buy_signal, price=12.50)
        assert shares == 100  # 至少100股

    def test_calc_shares_returns_zero_no_portfolio(self, buy_signal):
        executor = PaperExecutor(portfolio=None)
        shares = executor._calc_shares(buy_signal, price=12.50)
        assert shares == 0


class TestPaperExecutorBuy:
    def test_execute_buy_returns_order_id(self, buy_signal, mock_repo):
        p = Portfolio(initial_cash=100000)
        executor = PaperExecutor(portfolio=p)
        executor.repo = mock_repo
        order_id = executor.execute_buy(buy_signal, current_price=12.50)
        assert order_id == 99

    def test_execute_buy_updates_portfolio(self, buy_signal, mock_repo):
        p = Portfolio(initial_cash=100000)
        executor = PaperExecutor(portfolio=p)
        executor.repo = mock_repo
        executor.execute_buy(buy_signal, current_price=12.50)
        assert "000001" in p.positions
        assert p.cash < 100000

    def test_execute_buy_inserts_signal(self, buy_signal, mock_repo):
        executor = PaperExecutor()
        executor.repo = mock_repo
        executor.execute_buy(buy_signal, current_price=12.50, volume=1000)
        call_args = mock_repo.insert_signal.call_args[0][0]
        assert call_args["status"] == "executed"

    def test_execute_buy_inserts_order(self, buy_signal, mock_repo):
        executor = PaperExecutor()
        executor.repo = mock_repo
        executor.execute_buy(buy_signal, current_price=12.50, volume=1000)
        order = mock_repo.insert_order.call_args[0][0]
        assert order["order_type"] == "buy"
        assert order["commission"] >= MIN_COMMISSION

    def test_execute_buy_with_explicit_volume(self, buy_signal, mock_repo):
        p = Portfolio(initial_cash=100000)
        executor = PaperExecutor(portfolio=p)
        executor.repo = mock_repo
        order_id = executor.execute_buy(buy_signal, current_price=12.50, volume=500)
        assert order_id == 99
        assert p.positions["000001"].volume == 500

    def test_execute_buy_insufficient_cash(self, buy_signal, mock_repo):
        p = Portfolio(initial_cash=100)  # 现金不够
        executor = PaperExecutor(portfolio=p)
        executor.repo = mock_repo
        result = executor.execute_buy(buy_signal, current_price=12.50, volume=1000)
        assert result is None
        assert "000001" not in p.positions

    def test_execute_buy_zero_volume_returns_none(self, buy_signal, mock_repo):
        executor = PaperExecutor(portfolio=None)
        executor.repo = mock_repo
        result = executor.execute_buy(buy_signal, current_price=12.50, volume=0)
        assert result is None

    def test_execute_buy_applies_slippage(self, buy_signal, mock_repo):
        executor = PaperExecutor(portfolio=None, slippage=0.002)
        executor.repo = mock_repo
        executor.execute_buy(buy_signal, current_price=12.50, volume=1000)
        order = mock_repo.insert_order.call_args[0][0]
        expected_price = round(12.50 * (1 + 0.002), 2)
        assert order["order_price"] == expected_price

    def test_execute_buy_volume_rounded_to_100(self, buy_signal, mock_repo):
        """即使非100整数倍也要归正"""
        executor = PaperExecutor(portfolio=None)
        executor.repo = mock_repo
        executor.execute_buy(buy_signal, current_price=12.50, volume=123)
        order = mock_repo.insert_order.call_args[0][0]
        assert order["order_volume"] % 100 == 0  # 应为100


class TestPaperExecutorSell:
    def test_execute_sell_returns_order_id(self, mock_repo):
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "平安银行", 1000, 12.00, entry_date="2025-01-01")
        executor = PaperExecutor(portfolio=p)
        executor.repo = mock_repo
        order_id = executor.execute_sell("000001", current_price=13.00)
        assert order_id == 99

    def test_execute_sell_removes_position(self, mock_repo):
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "平安银行", 1000, 12.00, entry_date="2025-01-01")
        executor = PaperExecutor(portfolio=p)
        executor.repo = mock_repo
        executor.execute_sell("000001", current_price=13.00)
        assert "000001" not in p.positions

    def test_execute_sell_increases_cash(self, mock_repo):
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "平安银行", 1000, 12.00, entry_date="2025-01-01")
        cash_before = p.cash
        executor = PaperExecutor(portfolio=p)
        executor.repo = mock_repo
        executor.execute_sell("000001", current_price=13.00)
        assert p.cash > cash_before

    def test_execute_sell_no_position_returns_none(self, mock_repo):
        p = Portfolio(initial_cash=100000)
        executor = PaperExecutor(portfolio=p)
        executor.repo = mock_repo
        result = executor.execute_sell("000001", current_price=13.00)
        assert result is None

    def test_execute_sell_inserts_order(self, mock_repo):
        executor = PaperExecutor(portfolio=None)
        executor.repo = mock_repo
        executor.execute_sell("000001", current_price=13.00, volume=500)
        order = mock_repo.insert_order.call_args[0][0]
        assert order["order_type"] == "sell"
        assert order["order_volume"] == 500

    def test_execute_sell_applies_negative_slippage(self, mock_repo):
        executor = PaperExecutor(portfolio=None)
        executor.repo = mock_repo
        executor.execute_sell("000001", current_price=13.00, volume=500)
        order = mock_repo.insert_order.call_args[0][0]
        expected_price = round(13.00 * (1 - executor.slippage), 2)
        assert order["order_price"] == expected_price

    def test_execute_sell_commission_includes_stamp(self, mock_repo):
        executor = PaperExecutor(portfolio=None)
        executor.repo = mock_repo
        executor.execute_sell("000001", current_price=13.00, volume=1000)
        order = mock_repo.insert_order.call_args[0][0]
        # 卖出时佣金 = 经纪费 + 印花税，应大于同等金额买入的佣金
        buy_commission = executor._calc_commission(13000, is_sell=False)
        sell_commission = executor._calc_commission(13000, is_sell=True)
        assert sell_commission > buy_commission
