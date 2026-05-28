# -*- coding: utf-8 -*-
"""盯盘进程单元测试 — Watcher"""

from datetime import date, time as dt_time, datetime
from unittest.mock import MagicMock, patch

import pytest

from trade.monitor.watcher import (
    Watcher,
    MORNING_START,
    MORNING_END,
    AFTERNOON_START,
    MARKET_CLOSE,
)
from trade.portfolio.portfolio import Portfolio


# =====================  Fixtures  =====================


@pytest.fixture
def mock_telegram():
    return MagicMock()


@pytest.fixture
def mock_qmt():
    client = MagicMock()
    client.get_realtime.return_value = {
        "000001": {"lastPrice": 12.50, "price": 12.50},
        "000002": {"lastPrice": 25.00, "price": 25.00},
    }
    return client


@pytest.fixture
def watcher(mock_telegram, mock_qmt):
    w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
    w._trade_date = "2026-05-22"
    w._load_review_picks = MagicMock(return_value=[])
    return w


@pytest.fixture
def portfolio_with_position():
    p = Portfolio(initial_cash=100000)
    p.open_position(
        stock_code="000001",
        stock_name="平安银行",
        volume=1000,
        price=12.00,
        entry_date="2026-05-22",
        stop_loss=11.00,
        take_profit=14.00,
        trailing_stop=0.05,
    )
    return p


# =====================  Trading Hours Detection  =====================


class TestTradingHours:
    def test_morning_session(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 10, 0, 0)
            mock_dt.time.return_value = dt_time(10, 0, 0)
            assert Watcher._in_trading_hours() is True

    def test_morning_open(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 9, 25, 0)
            mock_dt.time.return_value = dt_time(9, 25, 0)
            assert Watcher._in_trading_hours() is True

    def test_morning_close(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 11, 29, 59)
            mock_dt.time.return_value = dt_time(11, 29, 59)
            assert Watcher._in_trading_hours() is True

    def test_afternoon_session(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 14, 0, 0)
            mock_dt.time.return_value = dt_time(14, 0, 0)
            assert Watcher._in_trading_hours() is True

    def test_afternoon_open(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 13, 0, 0)
            mock_dt.time.return_value = dt_time(13, 0, 0)
            assert Watcher._in_trading_hours() is True

    def test_afternoon_close(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 14, 59, 59)
            mock_dt.time.return_value = dt_time(14, 59, 59)
            assert Watcher._in_trading_hours() is True

    def test_not_trading_hours_before_market(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 9, 0, 0)
            mock_dt.time.return_value = dt_time(9, 0, 0)
            assert Watcher._in_trading_hours() is False

    def test_not_trading_hours_after_market(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 15, 1, 0)
            mock_dt.time.return_value = dt_time(15, 1, 0)
            assert Watcher._in_trading_hours() is False


class TestLunchBreak:
    def test_in_lunch_break(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 12, 0, 0)
            mock_dt.time.return_value = dt_time(12, 0, 0)
            assert Watcher._in_lunch_break() is True

    def test_not_in_lunch_break_morning(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 10, 0, 0)
            mock_dt.time.return_value = dt_time(10, 0, 0)
            assert Watcher._in_lunch_break() is False

    def test_not_in_lunch_break_afternoon(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 14, 0, 0)
            mock_dt.time.return_value = dt_time(14, 0, 0)
            assert Watcher._in_lunch_break() is False


class TestBeforeMarket:
    def test_before_market_true(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 9, 0, 0)
            mock_dt.time.return_value = dt_time(9, 0, 0)
            assert Watcher._before_market() is True

    def test_before_market_false(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 9, 25, 0)
            mock_dt.time.return_value = dt_time(9, 25, 0)
            assert Watcher._before_market() is False


class TestAfterMarket:
    def test_after_market_true(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 15, 0, 0)
            mock_dt.time.return_value = dt_time(15, 0, 0)
            assert Watcher._after_market() is True

    def test_after_market_false(self):
        with patch("trade.monitor.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 5, 22, 14, 59, 59)
            mock_dt.time.return_value = dt_time(14, 59, 59)
            assert Watcher._after_market() is False


# =====================  Signal Checking  =====================


class TestCheckSignals:
    def test_price_in_buy_zone_triggers_alert(self, watcher, mock_telegram):
        """信号买入区间 [12.00, 13.00]，现价 12.50 应触发警报"""
        mock_signals = [
            {
                "id": 1,
                "stock_code": "000001",
                "stock_name": "平安银行",
                "buy_zone_min": 12.00,
                "buy_zone_max": 13.00,
                "stop_loss": 11.00,
                "take_profit": 14.00,
            },
        ]
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = mock_signals

        prices = {"000001": 12.50}
        watcher._check_signals(prices, True)

        mock_telegram.send.assert_called_once()
        msg = mock_telegram.send.call_args[0][0]
        assert "买入信号" in msg
        assert "000001" in msg
        assert "平安银行" in msg
        assert "12.50" in msg
        assert "12.00" in msg
        assert "13.00" in msg

    def test_price_outside_zone_no_alert(self, watcher, mock_telegram):
        """信号买入区间 [12.00, 13.00]，现价 11.50 不应触发警报"""
        mock_signals = [
            {
                "id": 1,
                "stock_code": "000001",
                "stock_name": "平安银行",
                "buy_zone_min": 12.00,
                "buy_zone_max": 13.00,
                "stop_loss": 11.00,
                "take_profit": 14.00,
            },
        ]
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = mock_signals

        prices = {"000001": 11.50}
        watcher._check_signals(prices, True)

        mock_telegram.send.assert_not_called()

    def test_no_duplicate_alert(self, watcher, mock_telegram):
        """同一信号在连续扫描中只应触发一次警报"""
        mock_signals = [
            {
                "id": 1,
                "stock_code": "000001",
                "stock_name": "平安银行",
                "buy_zone_min": 12.00,
                "buy_zone_max": 13.00,
                "stop_loss": 11.00,
                "take_profit": 14.00,
            },
        ]
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = mock_signals

        prices = {"000001": 12.50}
        # First scan - should alert
        watcher._check_signals(prices, True)
        # Second scan - should NOT alert
        watcher._check_signals(prices, True)

        assert mock_telegram.send.call_count == 1

    def test_multiple_signals_all_triggered(self, watcher, mock_telegram):
        """多个信号同时进入买入区间"""
        mock_signals = [
            {
                "id": 1,
                "stock_code": "000001",
                "stock_name": "平安银行",
                "buy_zone_min": 12.00,
                "buy_zone_max": 13.00,
                "stop_loss": 11.00,
                "take_profit": 14.00,
            },
            {
                "id": 2,
                "stock_code": "000002",
                "stock_name": "万科A",
                "buy_zone_min": 24.00,
                "buy_zone_max": 26.00,
                "stop_loss": 23.00,
                "take_profit": 28.00,
            },
        ]
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = mock_signals

        prices = {"000001": 12.50, "000002": 25.00}
        watcher._check_signals(prices, True)

        assert mock_telegram.send.call_count == 2

    def test_signal_no_buy_zone_skipped(self, watcher, mock_telegram):
        """信号没有买入区间不检查"""
        mock_signals = [
            {
                "id": 1,
                "stock_code": "000001",
                "stock_name": "平安银行",
                "buy_zone_min": None,
                "buy_zone_max": None,
            },
        ]
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = mock_signals

        prices = {"000001": 12.50}
        watcher._check_signals(prices, True)

        mock_telegram.send.assert_not_called()

    def test_signal_price_missing_skipped(self, watcher, mock_telegram):
        """信号对应股票没有行情数据，跳过"""
        mock_signals = [
            {
                "id": 1,
                "stock_code": "000001",
                "stock_name": "平安银行",
                "buy_zone_min": 12.00,
                "buy_zone_max": 13.00,
            },
        ]
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = mock_signals

        prices = {"000002": 25.00}  # 000001 不在行情中
        watcher._check_signals(prices, True)

        mock_telegram.send.assert_not_called()


# =====================  Position Checking  =====================


class TestCheckPositions:
    def test_stop_loss_triggered(self, mock_telegram, mock_qmt):
        """现价 <= 止损价应触发止损警报"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00, take_profit=14.00,
        )
        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        prices = {"000001": 10.50}
        w._check_positions(prices)

        mock_telegram.send.assert_called_once()
        msg = mock_telegram.send.call_args[0][0]
        assert "止损触发" in msg
        assert "000001" in msg
        assert "10.50" in msg
        assert "11.00" in msg

    def test_stop_loss_boundary(self, mock_telegram, mock_qmt):
        """现价 == 止损价应触发止损警报"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00,
        )
        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        prices = {"000001": 11.00}
        w._check_positions(prices)

        mock_telegram.send.assert_called_once()
        assert "止损触发" in mock_telegram.send.call_args[0][0]

    def test_take_profit_triggered(self, mock_telegram, mock_qmt):
        """现价 >= 止盈价应触发止盈警报"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00, take_profit=14.00,
        )
        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        prices = {"000001": 14.50}
        w._check_positions(prices)

        mock_telegram.send.assert_called_once()
        msg = mock_telegram.send.call_args[0][0]
        assert "止盈触发" in msg

    def test_take_profit_boundary(self, mock_telegram, mock_qmt):
        """现价 == 止盈价应触发止盈警报"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00, take_profit=14.00,
        )
        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        prices = {"000001": 14.00}
        w._check_positions(prices)

        mock_telegram.send.assert_called_once()
        assert "止盈触发" in mock_telegram.send.call_args[0][0]

    def test_trailing_stop_triggered(self, mock_telegram, mock_qmt):
        """价格从高点回落超过 trailing_stop 应触发移动止盈"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00, take_profit=14.00,
            trailing_stop=0.05,
        )
        # 模拟价格先涨到 13.00
        pos = p.positions["000001"]
        pos.update_price(13.00)  # highest_price = 13.00
        assert pos.highest_price == 13.00

        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        # 价格回落到 12.30，触发价 = 13.00 * (1-0.05) = 12.35
        prices = {"000001": 12.30}
        w._check_positions(prices)

        mock_telegram.send.assert_called_once()
        msg = mock_telegram.send.call_args[0][0]
        assert "移动止盈触发" in msg
        assert "13.00" in msg  # 最高价

    def test_trailing_stop_not_triggered_above_threshold(self, mock_telegram, mock_qmt):
        """价格回落但未超过 trailing_stop 阈值不应触发"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00, take_profit=14.00,
            trailing_stop=0.05,
        )
        pos = p.positions["000001"]
        pos.update_price(13.00)  # highest_price = 13.00

        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        # 价格回落到 12.40，触发价 = 13.00 * 0.95 = 12.35，12.40 > 12.35 不触发
        prices = {"000001": 12.40}
        w._check_positions(prices)

        mock_telegram.send.assert_not_called()
        # 虽然价格回到 12.40，但最高价应更新
        assert p.positions["000001"].current_price == 12.40

    def test_highest_price_updates(self, mock_telegram, mock_qmt):
        """价格创出新高应更新 highest_price"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00, take_profit=14.00,
        )
        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        prices = {"000001": 13.50}
        w._check_positions(prices)

        pos = p.positions["000001"]
        assert pos.highest_price == 13.50
        assert pos.current_price == 13.50

    def test_no_trigger_when_price_normal(self, mock_telegram, mock_qmt):
        """正常价格波动不触发任何警报"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00, take_profit=14.00,
        )
        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        prices = {"000001": 12.50}
        w._check_positions(prices)

        mock_telegram.send.assert_not_called()

    def test_sl_priority_over_tp(self, mock_telegram, mock_qmt):
        """同一周期两者同时满足时，止损优先"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00, take_profit=14.00,
        )
        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        # 价格同时满足止损和止盈（不可能但测试边界)
        prices = {"000001": 11.00}  # stop loss at 11.00
        # Also mock position with take profit and trailing that could also trigger
        pos = p.positions["000001"]
        pos.take_profit = 11.00  # 止盈也设在11.00
        pos.highest_price = 15.00  # 移动止盈触发价 = 14.25
        w._check_positions(prices)

        mock_telegram.send.assert_called_once()
        assert "止损触发" in mock_telegram.send.call_args[0][0]

    def test_price_missing_skipped(self, mock_telegram, mock_qmt):
        """持仓股票无行情数据，跳过"""
        p = Portfolio(initial_cash=100000)
        p.open_position(
            stock_code="000001", stock_name="平安银行", volume=1000, price=12.00,
            entry_date="2026-05-22", stop_loss=11.00,
        )
        w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt)
        w.portfolio = p

        prices = {}  # 无行情数据
        w._check_positions(prices)

        mock_telegram.send.assert_not_called()


# =====================  Price Fetching  =====================


class TestGetPrices:
    def test_qmt_returns_prices(self, watcher, mock_qmt):
        """QMT 正常返回行情"""
        prices = watcher._get_realtime_prices(["000001", "000002"])
        assert prices == {"000001": 12.50, "000002": 25.00}

    def test_qmt_fails_no_fallback(self, watcher):
        """QMT 不可用时直接返回空，不 fallback DB"""
        watcher.qmt = None
        prices = watcher._get_realtime_prices(["000001", "000002"])
        assert prices == {}

    def test_qmt_exception_no_fallback(self, watcher, mock_qmt):
        """QMT 抛出异常后返回空，不 fallback DB"""
        mock_qmt.get_realtime.side_effect = ConnectionError("QMT offline")
        prices = watcher._get_realtime_prices(["000001"])
        assert prices == {}

    def test_empty_codes(self, watcher):
        """空列表返回空字典"""
        assert watcher._get_realtime_prices([]) == {}

    def test_qmt_partial_prices(self, watcher, mock_qmt):
        """QMT 返回部分数据"""
        mock_qmt.get_realtime.return_value = {
            "000001": {"lastPrice": 12.50},
        }
        prices = watcher._get_realtime_prices(["000001", "000002"])
        assert "000001" in prices
        assert "000002" not in prices  # Missing from QMT response


# =====================  Watch Codes  =====================


class TestGetWatchCodes:
    def test_combines_signals_and_positions(self, watcher):
        """同时获取信号和持仓中的股票代码"""
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = [
            {"stock_code": "000001"}, {"stock_code": "000002"},
        ]
        watcher.portfolio.open_position(
            stock_code="000003", stock_name="测试", volume=100, price=10.00,
        )
        codes = watcher._get_watch_codes()
        assert sorted(codes) == ["000001", "000002", "000003"]

    def test_only_signals(self, watcher):
        """仅有信号无持仓"""
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = [
            {"stock_code": "000001"},
        ]
        codes = watcher._get_watch_codes()
        assert codes == ["000001"]

    def test_only_positions(self, watcher):
        """仅有持仓无信号"""
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = []
        watcher.portfolio.open_position(
            stock_code="000001", stock_name="测试", volume=100, price=10.00,
        )
        codes = watcher._get_watch_codes()
        assert codes == ["000001"]

    def test_empty_when_nothing_to_watch(self, watcher):
        """无信号无持仓"""
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.return_value = []
        codes = watcher._get_watch_codes()
        assert codes == []


# =====================  Alert  =====================


class TestAlert:
    def test_alert_sends_telegram(self, watcher, mock_telegram):
        watcher._alert("测试消息")
        mock_telegram.send.assert_called_once_with("测试消息")

    def test_alert_no_telegram(self):
        """没有 Telegram bot 时不应抛异常"""
        w = Watcher(telegram_bot=None)
        w._alert("测试消息")  # Should not crash

    def test_alert_telegram_exception(self, mock_telegram):
        """Telegram 发送失败不应抛异常"""
        mock_telegram.send.side_effect = Exception("send failed")
        w = Watcher(telegram_bot=mock_telegram)
        w._alert("测试消息")  # Should not crash


# =====================  Scan Cycle  =====================


class TestScan:
    def test_scan_checks_signals_and_positions(self, watcher):
        """一次扫描同时检查信号和持仓"""
        watcher._get_watch_codes = MagicMock(return_value=["000001"])
        watcher._get_realtime_prices = MagicMock(return_value={"000001": 12.50})
        watcher._check_market_state = MagicMock(return_value=True)
        watcher._check_signals = MagicMock()
        watcher._check_positions = MagicMock()
        watcher._check_review_picks = MagicMock()

        watcher._scan()

        watcher._check_signals.assert_called_once_with({"000001": 12.50}, True)
        watcher._check_positions.assert_called_once_with({"000001": 12.50})

    def test_scan_empty_codes_skipped(self, watcher):
        """没有需要监控的代码，跳过"""
        watcher._get_watch_codes = MagicMock(return_value=[])
        watcher._get_realtime_prices = MagicMock()
        watcher._check_signals = MagicMock()
        watcher._check_positions = MagicMock()

        watcher._scan()

        watcher._get_realtime_prices.assert_not_called()
        watcher._check_signals.assert_not_called()

    def test_scan_no_prices_skipped(self, watcher):
        """没有行情数据，跳过"""
        watcher._get_watch_codes = MagicMock(return_value=["000001"])
        watcher._get_realtime_prices = MagicMock(return_value={})
        watcher._check_signals = MagicMock()
        watcher._check_positions = MagicMock()

        watcher._scan()

        watcher._check_signals.assert_not_called()
        watcher._check_positions.assert_not_called()

    def test_scan_repo_exception_does_not_crash(self, watcher):
        """扫描中信号获取异常不影响本次扫描"""
        watcher.repo = MagicMock()
        watcher.repo.get_pending_signals.side_effect = Exception("db error")
        # Ensure no positions so no prices are fetched
        # Should not raise
        watcher._scan()
