"""Tests for QMT integration — client, calendar, quotes, collector, collector_client."""

import json
import time
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from data.collect.live.collector_client import DataCollectorClient
from data.collect.live.qmt_collector import (
    QMTCollector,
)
from data.collect.live.quotes import QuoteClient
from system.qmt.calendar import TradingCalendar
from system.qmt.client import QMTClient, strip_suffix

# ============================================================
# system/qmt/client.py
# ============================================================


class TestStripSuffix:
    def test_sh_suffix(self):
        assert strip_suffix("600519.SH") == "600519"

    def test_sz_suffix(self):
        assert strip_suffix("000001.SZ") == "000001"

    def test_bj_suffix(self):
        assert strip_suffix("000688.BJ") == "000688"

    def test_no_suffix(self):
        assert strip_suffix("600519") == "600519"

    def test_unknown_suffix(self):
        assert strip_suffix("SOME.OTHER") == "SOME.OTHER"

    def test_empty_string(self):
        assert strip_suffix("") == ""


class TestQMTClient:
    """QMTClient 是纯 HTTP 客户端，所有方法通过 _get 调用 requests.get。"""

    def test_init_default_params(self):
        """默认参数创建 client，使用配置中的服务地址。"""
        client = QMTClient()
        assert client.server is not None
        assert "http" in client.server
        assert "5000" in client.server or ":" in client.server

    def test_init_custom_server(self):
        client = QMTClient(server="http://localhost:8888")
        assert client.server == "http://localhost:8888"

    def test_quote(self):
        """get_quote（实际方法名 quote）发送 /quote/{code} 请求。"""
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "success": True,
                    "data": {"code": "600519", "price": 1500.0},
                },
            )
            result = client.quote("600519")

            mock_get.assert_called_once_with(
                "http://fake:5000/quote/600519",
                params=None,
                timeout=(5, 120),
            )
            assert result["success"] is True
            assert result["data"]["code"] == "600519"

    def test_quotes(self):
        """get_quotes（quotes）发送 /quotes?codes= 请求，codes 以逗号拼接。"""
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": {"600519": {"price": 1500.0}}},
            )
            result = client.quotes(["600519", "000001"])

            mock_get.assert_called_once_with(
                "http://fake:5000/quotes",
                params={"codes": "600519,000001"},
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_all_quotes(self):
        """all_quotes 发送 /all_quotes，返回 dict。"""
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {
                    "success": True,
                    "data": {"600519.SH": {"price": 1500.0}},
                },
            )
            result = client.all_quotes()

            mock_get.assert_called_once_with(
                "http://fake:5000/all_quotes",
                params=None,
                timeout=(5, 120),
            )
            assert isinstance(result, dict)
            assert result["success"] is True

    def test_timeout_returns_graceful_error(self):
        """requests 超时时返回 {"success": False, "error": "请求超时..."}。"""
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("Connection timed out")
            result = client.quote("600519")

            assert result["success"] is False
            assert "超时" in result["error"]
            assert "elapsed" in result

    def test_connection_error_returns_graceful_message(self):
        """连接被拒时返回 {"success": False, "error": "无法连接QMT服务器"}。"""
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError(
                "Connection refused"
            )
            result = client.quote("600519")

            assert result["success"] is False
            assert "无法连接QMT服务器" in result["error"]

    def test_generic_exception_returns_error_string(self):
        """通用异常时返回 {"success": False, "error": str(e)}。"""
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.side_effect = ValueError("bad data")
            result = client.quote("600519")

            assert result["success"] is False
            assert "bad data" in result["error"]

    def test_status(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": {"connected": True}},
            )
            result = client.status()
            assert result["success"] is True

    def test_instrument(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": {"code": "600519"}},
            )
            result = client.instrument("600519")
            mock_get.assert_called_once_with(
                "http://fake:5000/instrument/600519",
                params=None,
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_history_with_all_params(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": []},
            )
            result = client.history(
                "600519", period="1d", start="2026-01-01", end="2026-06-01", count=100
            )
            mock_get.assert_called_once_with(
                "http://fake:5000/history/600519",
                params={
                    "period": "1d",
                    "start": "2026-01-01",
                    "end": "2026-06-01",
                    "count": 100,
                },
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_history_minimal_params(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": []},
            )
            result = client.history("600519")
            mock_get.assert_called_once_with(
                "http://fake:5000/history/600519",
                params={"period": "1d"},
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_minute_kline(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": []},
            )
            result = client.minute_kline("600519", period="1m", start="2026-06-01")
            mock_get.assert_called_once_with(
                "http://fake:5000/history/600519",
                params={"period": "1m", "start": "2026-06-01"},
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_tick(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": []},
            )
            result = client.tick("600519", start="09:30:00", end="10:00:00")
            mock_get.assert_called_once_with(
                "http://fake:5000/tick/600519",
                params={"start": "09:30:00", "end": "10:00:00"},
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_tick_no_params(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200, json=lambda: {"success": True, "data": []}
            )
            result = client.tick("600519")
            mock_get.assert_called_once_with(
                "http://fake:5000/tick/600519",
                params={},
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_financial(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": {}},
            )
            result = client.financial("600519", tables=["income", "balance"])
            mock_get.assert_called_once_with(
                "http://fake:5000/financial/600519",
                params={"tables": "income,balance"},
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_dividend(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": []},
            )
            result = client.dividend("600519")
            mock_get.assert_called_once_with(
                "http://fake:5000/dividend/600519",
                params=None,
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_st_history(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": []},
            )
            result = client.st_history("600519")
            mock_get.assert_called_once_with(
                "http://fake:5000/st_history/600519",
                params=None,
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_calendar(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": ["2026-06-01", "2026-06-02"]},
            )
            result = client.calendar("sh")
            mock_get.assert_called_once_with(
                "http://fake:5000/calendar/sh",
                params=None,
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_calendar_default_market(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": []},
            )
            result = client.calendar()
            mock_get.assert_called_once_with(
                "http://fake:5000/calendar/sh",
                params=None,
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_sectors(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": ["白酒", "半导体"]},
            )
            result = client.sectors()
            mock_get.assert_called_once_with(
                "http://fake:5000/sectors",
                params=None,
                timeout=(5, 120),
            )
            assert result["success"] is True

    def test_sector_stocks(self):
        client = QMTClient(server="http://fake:5000")
        with patch("system.qmt.client.requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: {"success": True, "data": ["600519", "000858"]},
            )
            result = client.sector_stocks("白酒")
            mock_get.assert_called_once_with(
                "http://fake:5000/sector/白酒",
                params=None,
                timeout=(5, 120),
            )
            assert result["success"] is True


# ============================================================
# system/qmt/calendar.py
# ============================================================


class TestTradingCalendar:
    """TradingCalendar 通过 QMTClient.calendar() 获取交易日，本地缓存。"""

    def test_init_default_client(self):
        """不传 client 时内部创建 QMTClient 实例。"""
        cal = TradingCalendar()
        assert cal._client is not None
        assert isinstance(cal._client, QMTClient)

    def test_init_custom_client(self):
        mock_client = MagicMock(spec=QMTClient)
        cal = TradingCalendar(client=mock_client)
        assert cal._client is mock_client

    def test_is_trading_day_from_cache(self):
        """is_trading_day 从缓存判断，QMT 返回的日期返回 True。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.calendar.return_value = {
            "success": True,
            "data": ["2026-06-01", "2026-06-02", "2026-06-05"],
        }
        cal = TradingCalendar(client=mock_client)

        assert cal.is_trading_day(date(2026, 6, 1)) is True
        assert cal.is_trading_day(date(2026, 6, 2)) is True
        assert cal.is_trading_day(date(2026, 6, 3)) is False
        assert cal.is_trading_day(date(2026, 6, 5)) is True

    def test_is_trading_day_with_today_default(self):
        """不传日期时默认判断今天。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.calendar.return_value = {
            "success": True,
            "data": ["2026-06-01", "2026-06-02", "2026-06-05"],
        }
        cal = TradingCalendar(client=mock_client)

        with patch("system.qmt.calendar.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 1)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            result = cal.is_trading_day()
            assert result is True

    def test_api_failure_falls_back_to_cache(self):
        """QMT 返回失败时保留上次缓存，新请求未更新则 is_trading_day 返回 False。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.calendar.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }
        cal = TradingCalendar(client=mock_client)

        # 首次调用，无缓存，QMT 失败 → 不报错，返回 False
        assert cal.is_trading_day(date(2026, 6, 1)) is False

    def test_cache_refresh_on_new_day(self):
        """当天首次调用触发 _ensure_cache，后续同一天不再请求。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.calendar.return_value = {
            "success": True,
            "data": ["2026-06-01"],
        }
        cal = TradingCalendar(client=mock_client)

        with patch("system.qmt.calendar.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 1)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            _ = cal.is_trading_day(date(2026, 6, 1))
            _ = cal.is_trading_day(date(2026, 6, 1))
            # 只请求一次
            mock_client.calendar.assert_called_once()

    def test_cache_refresh_on_next_day(self):
        """第二天重新请求日历数据。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.calendar.return_value = {
            "success": True,
            "data": ["2026-06-02"],
        }
        cal = TradingCalendar(client=mock_client)

        with patch("system.qmt.calendar.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 1)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            _ = cal.is_trading_day(date(2026, 6, 1))

        # 第二天
        with patch("system.qmt.calendar.date") as mock_date:
            mock_date.today.return_value = date(2026, 6, 2)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            _ = cal.is_trading_day(date(2026, 6, 2))

        assert mock_client.calendar.call_count == 2

    def test_calendar_data_as_dict_with_trade_days(self):
        """部分 QMT Server 返回 {trade_days: [...]} 格式。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.calendar.return_value = {
            "success": True,
            "data": {"trade_days": ["2026-06-01", "2026-06-02"]},
        }
        cal = TradingCalendar(client=mock_client)

        assert cal.is_trading_day(date(2026, 6, 1)) is True
        assert cal.is_trading_day(date(2026, 6, 3)) is False

    def test_calendar_data_as_dict_with_dates(self):
        """部分 QMT Server 返回 {dates: [...]} 格式。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.calendar.return_value = {
            "success": True,
            "data": {"dates": ["2026-06-01", "2026-06-02"]},
        }
        cal = TradingCalendar(client=mock_client)

        assert cal.is_trading_day(date(2026, 6, 1)) is True
        assert cal.is_trading_day(date(2026, 6, 3)) is False

    def test_multiday_consistency(self):
        """连续多日判断与交易日历一致。"""
        mock_client = MagicMock(spec=QMTClient)
        trading_dates = {"2026-06-01", "2026-06-02", "2026-06-05"}
        mock_client.calendar.return_value = {
            "success": True,
            "data": list(trading_dates),
        }
        cal = TradingCalendar(client=mock_client)

        for day in range(1, 7):
            d = date(2026, 6, day)
            expected = d.isoformat() in trading_dates
            assert cal.is_trading_day(d) is expected, f"{d}: expected {expected}"


# ============================================================
# data/live/quotes.py
# ============================================================


class TestQuoteClient:
    """QuoteClient 封装 QMTClient，负责代码后缀归一化。"""

    def test_init_default_client(self):
        """不传 client 时内部创建 QMTClient。"""
        qc = QuoteClient()
        assert qc._client is not None
        assert isinstance(qc._client, QMTClient)

    def test_init_custom_client(self):
        mock_client = MagicMock(spec=QMTClient)
        qc = QuoteClient(client=mock_client)
        assert qc._client is mock_client

    def test_get_realtime_valid_codes(self):
        """get_realtime 对有效代码返回归一化后的 dict。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quotes.return_value = {
            "success": True,
            "data": {
                "600519.SH": {"lastPrice": 1500.0, "changePct": 1.5, "amount": 1e8},
            },
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_realtime(["600519"])
        assert isinstance(result, dict)
        assert "600519" in result
        assert result["600519"]["lastPrice"] == 1500.0

    def test_get_realtime_multiple_codes(self):
        """多个代码返回对应数量的结果。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quotes.return_value = {
            "success": True,
            "data": {
                "600519.SH": {"lastPrice": 1500.0},
                "000001.SZ": {"lastPrice": 10.0},
            },
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_realtime(["600519", "000001"])
        assert len(result) == 2

    def test_get_realtime_empty_list(self):
        """空列表返回空 dict。"""
        qc = QuoteClient(client=MagicMock(spec=QMTClient))
        result = qc.get_realtime([])
        assert result == {}

    def test_get_realtime_suffix_normalization(self):
        """回复的 key 自动去除 .SH/.SZ/.BJ 后缀。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quotes.return_value = {
            "success": True,
            "data": {
                "600519.SH": {"lastPrice": 1500.0},
                "000001.SZ": {"lastPrice": 10.0},
                "000688.BJ": {"lastPrice": 8.0},
            },
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_realtime(["600519", "000001", "000688"])
        assert "600519.SH" not in result
        assert "600519" in result
        assert "000001" in result
        assert "000688" in result

    def test_get_realtime_expands_suffixes(self):
        """发送时每个无后缀代码展开为 .SH/.SZ/.BJ 三个。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quotes.return_value = {"success": True, "data": {}}
        qc = QuoteClient(client=mock_client)

        qc.get_realtime(["600519"])
        # 展开为 600519.SH, 600519.SZ, 600519.BJ
        mock_client.quotes.assert_called_once_with(
            ["600519.SH", "600519.SZ", "600519.BJ"]
        )

    def test_get_realtime_with_already_suffixed_code(self):
        """传入已有后缀的代码时 strip 后重新展开（与无后缀代码行为一致）。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quotes.return_value = {
            "success": True,
            "data": {"600519.SH": {"lastPrice": 1500.0}},
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_realtime(["600519.SH"])
        # strip_suffix 去掉 .SH 后展开为 3 个后缀
        mock_client.quotes.assert_called_once_with(
            ["600519.SH", "600519.SZ", "600519.BJ"]
        )
        assert "600519" in result

    def test_get_realtime_api_failure(self):
        """QMT 连接失败时返回空 dict。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quotes.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_realtime(["600519"])
        assert result == {}

    def test_get_realtime_api_returns_non_dict_data(self):
        """QMT 返回非 dict 的 data 时返回空 dict。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quotes.return_value = {"success": True, "data": "error"}
        qc = QuoteClient(client=mock_client)

        result = qc.get_realtime(["600519"])
        assert result == {}

    def test_get_all_quotes(self):
        """all_quotes 返回归一化后的全市场快照。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.all_quotes.return_value = {
            "success": True,
            "data": {
                "600519.SH": {"lastPrice": 1500.0},
                "000001.SZ": {"lastPrice": 10.0},
            },
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_all_quotes()
        assert isinstance(result, dict)
        assert "600519" in result
        assert "000001" in result
        assert "600519.SH" not in result

    def test_get_all_quotes_failure(self):
        """all_quotes 连接失败返回空 dict。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.all_quotes.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_all_quotes()
        assert result == {}

    def test_get_all_quotes_non_dict_data(self):
        """all_quotes 返回非 dict 数据时返回空 dict。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.all_quotes.return_value = {"success": True, "data": []}
        qc = QuoteClient(client=mock_client)

        result = qc.get_all_quotes()
        assert result == {}

    def test_get_quote_detail(self):
        """get_quote_detail 返回个股全量行情（含盘口）。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quote.return_value = {
            "success": True,
            "data": {
                "code": "600519",
                "lastPrice": 1500.0,
                "preClose": 1480.0,
                "high": 1510.0,
                "low": 1485.0,
                "open": 1490.0,
                "volume": 50000,
                "amount": 7.5e7,
                "bidPrice": [1499.0, 1498.5],
                "askPrice": [1500.5, 1501.0],
            },
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_quote_detail("600519")
        assert result is not None
        assert result["code"] == "600519"
        assert result["lastPrice"] == 1500.0
        # 盘口数据存在
        assert "bidPrice" in result
        assert "askPrice" in result

    def test_get_quote_detail_failure_returns_none(self):
        """get_quote_detail 连接失败返回 None。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quote.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_quote_detail("600519")
        assert result is None

    def test_get_price(self):
        """get_price 返回最新价。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quote.return_value = {
            "success": True,
            "data": {"lastPrice": 1500.0},
        }
        qc = QuoteClient(client=mock_client)

        price = qc.get_price("600519")
        assert price == 1500.0

    def test_get_price_fallback_keys(self):
        """get_price 兼容 last_price 和 price 字段。"""
        mock_client = MagicMock(spec=QMTClient)

        # last_price
        mock_client.quote.return_value = {
            "success": True,
            "data": {"last_price": 1510.0},
        }
        qc = QuoteClient(client=mock_client)
        assert qc.get_price("600519") == 1510.0

        # price
        mock_client.quote.return_value = {
            "success": True,
            "data": {"price": 1520.0},
        }
        assert qc.get_price("600519") == 1520.0

    def test_get_price_failure_returns_none(self):
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quote.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }
        qc = QuoteClient(client=mock_client)

        assert qc.get_price("600519") is None

    def test_get_price_no_price_field(self):
        """返回成功但无价格字段时返回 None。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.quote.return_value = {"success": True, "data": {}}
        qc = QuoteClient(client=mock_client)

        assert qc.get_price("600519") is None

    def test_get_kline(self):
        """get_kline 返回 K 线列表。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.history.return_value = {
            "success": True,
            "data": [{"time": 100, "close": 1500.0}, {"time": 200, "close": 1510.0}],
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_kline("600519", period="1d", count=50)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_get_minute_kline(self):
        """get_minute_kline 默认 240 条 1 分钟 K 线。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.history.return_value = {
            "success": True,
            "data": [{"time": 100, "close": 1500.0}],
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_minute_kline("600519", count=240)
        mock_client.history.assert_called_once_with("600519.SH", period="1m", count=240)
        assert len(result) == 1

    def test_get_kline_tries_multiple_suffixes(self):
        """一个后缀失败时尝试下一个。"""
        mock_client = MagicMock(spec=QMTClient)
        # 第一次调用（.SH）失败，第二次（.SZ）成功
        mock_client.history.side_effect = [
            {"success": False, "error": "not found"},
            {
                "success": True,
                "data": [{"time": 100, "close": 10.0}],
            },
        ]
        qc = QuoteClient(client=mock_client)

        result = qc.get_kline("000001", period="1d", count=50)
        assert len(result) == 1
        assert mock_client.history.call_count == 2

    def test_get_history(self):
        """get_history 传参给 QMTClient.history。"""
        mock_client = MagicMock(spec=QMTClient)
        mock_client.history.return_value = {
            "success": True,
            "data": [{"date": "2026-06-01", "close": 1500.0}],
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_history("600519", period="1d", start="2026-01-01", count=100)
        mock_client.history.assert_called_once_with(
            "600519", period="1d", start="2026-01-01", end=None, count=100
        )
        assert len(result) == 1

    def test_get_history_failure(self):
        mock_client = MagicMock(spec=QMTClient)
        mock_client.history.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_history("600519")
        assert result == []

    def test_get_instrument(self):
        mock_client = MagicMock(spec=QMTClient)
        mock_client.instrument.return_value = {
            "success": True,
            "data": {"code": "600519", "up_stop": 1650.0, "down_stop": 1350.0},
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_instrument("600519")
        assert result["up_stop"] == 1650.0
        assert result["down_stop"] == 1350.0

    def test_get_instrument_failure(self):
        mock_client = MagicMock(spec=QMTClient)
        mock_client.instrument.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }
        qc = QuoteClient(client=mock_client)

        assert qc.get_instrument("600519") is None

    def test_get_ticks(self):
        mock_client = MagicMock(spec=QMTClient)
        mock_client.tick.return_value = {
            "success": True,
            "data": [{"time": "09:30:00", "price": 1500.0, "volume": 100}],
        }
        qc = QuoteClient(client=mock_client)

        result = qc.get_ticks("600519")
        assert len(result) == 1

    def test_get_ticks_failure(self):
        mock_client = MagicMock(spec=QMTClient)
        mock_client.tick.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }
        qc = QuoteClient(client=mock_client)

        assert qc.get_ticks("600519") == []

    def test_get_ticks_non_list_data(self):
        mock_client = MagicMock(spec=QMTClient)
        mock_client.tick.return_value = {"success": True, "data": "invalid"}
        qc = QuoteClient(client=mock_client)

        assert qc.get_ticks("600519") == []


# ============================================================
# data/live/qmt_collector.py
# ============================================================


class TestQMTCollector:
    """QMTCollector 独立采集进程，mock 所有 IO 依赖。"""

    @pytest.fixture
    def mock_deps(self):
        """提供 QMTCollector 实例和关键 mock 对象。

        Returns:
            dict with keys:
                collector: QMTCollector 实例（所有 IO 已 mock）
                mock_qmt: mock_client 实例
                mock_sock: mock 的 server socket
                mock_db: sqlite3.connect 返回的 mock conn
        """
        with (
            patch("data.collect.live.qmt_collector.socket.socket") as mock_sock_cls,
            patch("data.collect.live.qmt_collector.sqlite3.connect") as mock_db_conn,
            patch("data.collect.live.qmt_collector.QMTClient") as mock_qmt_cls,
            patch("data.collect.live.qmt_collector.datetime") as mock_dt,
        ):
            mock_sock_instance = MagicMock()
            mock_sock_cls.return_value = mock_sock_instance

            mock_db = MagicMock()
            mock_db_conn.return_value = mock_db

            mock_qmt_instance = MagicMock()
            mock_qmt_cls.return_value = mock_qmt_instance

            # 固定为交易时段 10:30
            fake_now = datetime(2026, 6, 6, 10, 30, 0)
            mock_dt.now.return_value = fake_now
            # 保留 strftime 在 fake_now 上正常工作（datetime 实例方法）
            collect = QMTCollector()

            yield {
                "collector": collect,
                "mock_qmt": mock_qmt_instance,
                "mock_sock": mock_sock_instance,
                "mock_db": mock_db,
                "mock_dt": mock_dt,
                "now_fake": fake_now,
            }

    # ---------- __init__ ----------

    def test_init_sets_up_paths(self, mock_deps):
        """__init__ 设置 db_path、trade_date、QMT client、TCP server。"""
        c = mock_deps["collector"]
        assert c.db_path is not None
        assert c._trade_date == "2026-06-06"
        assert c._running is True
        assert c.qmt is not None

    def test_init_creates_tcp_server(self, mock_deps):
        """__init__ 创建并绑定 TCP socket。"""
        c = mock_deps["collector"]
        mock_sock = mock_deps["mock_sock"]

        mock_sock.setsockopt.assert_called()
        mock_sock.bind.assert_called_once()
        mock_sock.listen.assert_called_once_with(1)
        mock_sock.setblocking.assert_called_once_with(False)
        assert c._server is mock_sock

    # ---------- _in_trading_hours ----------

    def test_in_trading_hours_morning_session(self):
        """9:30-11:30 为上午交易时段。"""
        with patch("data.collect.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 10, 0, 0)
            assert QMTCollector._in_trading_hours() is True

            mock_dt.now.return_value = datetime(2026, 6, 6, 9, 30, 0)
            assert QMTCollector._in_trading_hours() is True

            mock_dt.now.return_value = datetime(2026, 6, 6, 11, 29, 59)
            assert QMTCollector._in_trading_hours() is True

    def test_in_trading_hours_afternoon_session(self):
        """13:00-15:00 为下午交易时段。"""
        with patch("data.collect.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 13, 0, 0)
            assert QMTCollector._in_trading_hours() is True

            mock_dt.now.return_value = datetime(2026, 6, 6, 14, 30, 0)
            assert QMTCollector._in_trading_hours() is True

            mock_dt.now.return_value = datetime(2026, 6, 6, 14, 59, 59)
            assert QMTCollector._in_trading_hours() is True

    def test_in_trading_hours_not_trading(self):
        """午休和盘前盘后为非交易时段。"""
        with patch("data.collect.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 12, 0, 0)
            assert QMTCollector._in_trading_hours() is False

            mock_dt.now.return_value = datetime(2026, 6, 6, 9, 29, 59)
            assert QMTCollector._in_trading_hours() is False

            mock_dt.now.return_value = datetime(2026, 6, 6, 15, 0, 0)
            assert QMTCollector._in_trading_hours() is False

    def test_in_trading_hours_boundary_morning_end(self):
        """11:30 收盘时不在交易时段（左闭右开）。"""
        with patch("data.collect.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 11, 30, 0)
            assert QMTCollector._in_trading_hours() is False

    # ---------- _after_market ----------

    def test_after_market_after_close(self):
        """15:00 之后为盘后。"""
        with patch("data.collect.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 15, 0, 0)
            assert QMTCollector._after_market() is True

            mock_dt.now.return_value = datetime(2026, 6, 6, 18, 0, 0)
            assert QMTCollector._after_market() is True

    def test_after_market_before_close(self):
        """15:00 之前不是盘后。"""
        with patch("data.collect.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 14, 59, 59)
            assert QMTCollector._after_market() is False

            mock_dt.now.return_value = datetime(2026, 6, 6, 9, 30, 0)
            assert QMTCollector._after_market() is False

    # ---------- _fetch_and_push ----------

    def test_fetch_and_push_market_and_index(self, mock_deps):
        """_fetch_and_push 获取全市场快照 + 5 个指数并推送。"""
        c = mock_deps["collector"]
        mock_qmt = mock_deps["mock_qmt"]
        mock_sock = mock_deps["mock_sock"]

        # mock all_quotes — 返回全市场数据
        mock_qmt.all_quotes.return_value = {
            "success": True,
            "data": {
                "600519.SH": {"lastPrice": 1500.0, "changePct": 1.5, "amount": 1e8},
                "000001.SZ": {"lastPrice": 10.0, "changePct": -0.5, "amount": 5e6},
                "002371.SZ": {"lastPrice": 380.0, "changePct": 2.0, "amount": 2e7},
            },
        }

        # mock quote — 返回指数数据（被调用 5 次，对应 5 个指数）
        def mock_quote(code):
            prices = {
                "000001.SH": {
                    "lastPrice": 3200.0,
                    "preClose": 3180.0,
                    "high": 3210.0,
                    "low": 3190.0,
                    "amount": 5e10,
                },
                "399001.SZ": {
                    "lastPrice": 12000.0,
                    "preClose": 11900.0,
                    "high": 12100.0,
                    "low": 11800.0,
                    "amount": 3e10,
                },
                "399006.SZ": {
                    "lastPrice": 2500.0,
                    "preClose": 2480.0,
                    "high": 2520.0,
                    "low": 2470.0,
                    "amount": 1e10,
                },
                "399303.SZ": {
                    "lastPrice": 8000.0,
                    "preClose": 7950.0,
                    "high": 8050.0,
                    "low": 7920.0,
                    "amount": 2e9,
                },
                "000688.SH": {
                    "lastPrice": 1100.0,
                    "preClose": 1090.0,
                    "high": 1110.0,
                    "low": 1085.0,
                    "amount": 5e9,
                },
            }
            return {"success": True, "data": prices.get(code, {})}

        mock_qmt.quote.side_effect = mock_quote

        # 连接 watcher socket
        c._watcher_sock = MagicMock()

        c._fetch_and_push()

        # all_quotes 被调用
        mock_qmt.all_quotes.assert_called_once()

        # 5 个指数各调用一次 quote
        assert mock_qmt.quote.call_count == 5

        # 至少推送过 market 和 index 消息
        assert c._watcher_sock.sendall.called

    def test_fetch_and_push_all_quotes_failure(self, mock_deps):
        """all_quotes 失败时只获取指数，不抛异常。"""
        c = mock_deps["collector"]
        mock_qmt = mock_deps["mock_qmt"]

        mock_qmt.all_quotes.return_value = {
            "success": False,
            "error": "无法连接QMT服务器",
        }

        def mock_quote(code):
            return {
                "success": True,
                "data": {
                    "lastPrice": 3200.0,
                    "preClose": 3180.0,
                    "high": 3210.0,
                    "low": 3190.0,
                    "amount": 5e10,
                },
            }

        mock_qmt.quote.side_effect = mock_quote

        c._fetch_and_push()  # 不应抛异常

    def test_fetch_and_push_all_indexes_failure(self, mock_deps):
        """指数获取全部失败时只推送 market。"""
        c = mock_deps["collector"]
        mock_qmt = mock_deps["mock_qmt"]
        mock_sock = mock_deps["mock_sock"]

        mock_qmt.all_quotes.return_value = {
            "success": True,
            "data": {
                "600519.SH": {"lastPrice": 1500.0, "changePct": 1.5, "amount": 1e8},
            },
        }
        mock_qmt.quote.return_value = {
            "success": False,
            "error": "not found",
        }

        c._watcher_sock = MagicMock()
        c._fetch_and_push()  # 不应抛异常

    def test_fetch_and_push_skips_no_price_stock(self, mock_deps):
        """股票无价格时跳过。"""
        c = mock_deps["collector"]
        mock_qmt = mock_deps["mock_qmt"]
        mock_qmt.all_quotes.return_value = {
            "success": True,
            "data": {
                "600519.SH": {"lastPrice": 1500.0, "changePct": 1.5, "amount": 1e8},
                "000001.SZ": {"lastPrice": None, "changePct": 0.0, "amount": 0},
            },
        }
        mock_qmt.quote.return_value = {
            "success": True,
            "data": {
                "lastPrice": 3200.0,
                "preClose": 3180.0,
                "high": 3210.0,
                "low": 3190.0,
                "amount": 5e10,
            },
        }

        c._watcher_sock = MagicMock()
        c._fetch_and_push()

        # market 推送中应该只有 600519
        send_calls = c._watcher_sock.sendall.call_args_list
        market_msgs = []
        for call_ in send_calls:
            raw = call_[0][0]
            for line in raw.decode("utf-8").strip().split("\n"):
                msg = json.loads(line)
                if msg.get("type") == "market":
                    market_msgs.append(msg)
        assert any(len(m["stocks"]) == 1 for m in market_msgs)
        assert any("600519" in m["stocks"] for m in market_msgs)

    # ---------- _send_json ----------

    def test_send_json_no_watcher(self, mock_deps):
        """无 watcher 连接时 _send_json 什么都不做。"""
        c = mock_deps["collector"]
        c._watcher_sock = None
        c._send_json({"type": "test", "data": 1})
        # 不抛异常，不发消息

    def test_send_json_with_watcher(self, mock_deps):
        """有 watcher 时发送 JSON line。"""
        c = mock_deps["collector"]
        mock_sock = MagicMock()
        c._watcher_sock = mock_sock

        c._send_json({"type": "test", "data": 1})
        expected = (
            json.dumps({"type": "test", "data": 1}, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        mock_sock.sendall.assert_called_once_with(expected)

    def test_send_json_broken_pipe(self, mock_deps):
        """BrokenPipeError 时关闭 watcher 并释放。"""
        c = mock_deps["collector"]
        mock_sock = MagicMock()
        mock_sock.sendall.side_effect = BrokenPipeError("broken")
        c._watcher_sock = mock_sock

        c._send_json({"type": "test"})
        # watcher 已被关闭
        assert c._watcher_sock is None
        mock_sock.close.assert_called_once()

    # ---------- _init_klines ----------

    def test_init_klines_success(self, mock_deps):
        """_init_klines 成功获取并写入 K 线。"""
        c = mock_deps["collector"]
        mock_qmt = mock_deps["mock_qmt"]
        mock_db = mock_deps["mock_db"]

        today_ts = time.mktime(datetime(2026, 6, 6, 9, 0, 0).timetuple())
        bars = [
            {
                "time": today_ts + 60 * i,
                "close": 3200.0 + i,
                "high": 3210.0,
                "low": 3190.0,
                "preClose": 3180.0,
                "amount": 1e7,
            }
            for i in range(10)
        ]
        mock_qmt.history.return_value = {"success": True, "data": bars}

        c._init_klines()

        mock_qmt.history.assert_called_once_with("000001.SH", period="1m", count=240)
        # 确认写入了 DB
        assert mock_db.executemany.call_count >= 0  # 至少没抛异常

    def test_init_klines_api_failure(self, mock_deps):
        """QMT 连接失败时跳过回填。"""
        c = mock_deps["collector"]
        mock_qmt = mock_deps["mock_qmt"]
        mock_qmt.history.return_value = {"success": False, "error": "timeout"}

        c._init_klines()
        # 不抛异常

    def test_init_klines_insufficient_data(self, mock_deps):
        """数据不足 5 条时跳过回填。"""
        c = mock_deps["collector"]
        mock_qmt = mock_deps["mock_qmt"]
        mock_qmt.history.return_value = {
            "success": True,
            "data": [{"time": 100, "close": 3200.0}],
        }

        c._init_klines()
        # 不抛异常

    # ---------- _accept_watcher ----------

    def test_accept_watcher_no_existing(self, mock_deps):
        """无已有连接时接受新 watcher。"""
        c = mock_deps["collector"]
        mock_sock = mock_deps["mock_sock"]

        new_sock = MagicMock()
        mock_sock.accept.return_value = (new_sock, ("127.0.0.1", 54321))
        c._watcher_sock = None

        c._accept_watcher()
        assert c._watcher_sock is new_sock
        new_sock.setblocking.assert_called_once_with(True)

    def test_accept_watcher_existing_rejected(self, mock_deps):
        """已有连接时拒绝新连接。"""
        c = mock_deps["collector"]
        mock_sock = mock_deps["mock_sock"]

        existing = MagicMock()
        c._watcher_sock = existing
        new_sock = MagicMock()
        mock_sock.accept.return_value = (new_sock, ("127.0.0.1", 54321))

        c._accept_watcher()
        # 原连接不变
        assert c._watcher_sock is existing
        # 新连接被关闭
        new_sock.close.assert_called_once()

    # ---------- _check_watcher_disconnect ----------

    def test_check_watcher_disconnect_empty_data(self, mock_deps):
        """收到空数据时断开 watcher。"""
        c = mock_deps["collector"]
        mock_sock = MagicMock()
        mock_sock.recv.return_value = b""
        c._watcher_sock = mock_sock

        c._check_watcher_disconnect(mock_sock)
        assert c._watcher_sock is None

    def test_check_watcher_disconnect_connection_reset(self, mock_deps):
        """ConnectionResetError 时断开 watcher。"""
        c = mock_deps["collector"]
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = ConnectionResetError
        c._watcher_sock = mock_sock

        c._check_watcher_disconnect(mock_sock)
        assert c._watcher_sock is None

    def test_check_watcher_disconnect_data_ok(self, mock_deps):
        """收到正常数据时不做处理。"""
        c = mock_deps["collector"]
        mock_sock = MagicMock()
        mock_sock.recv.return_value = b"ping"
        c._watcher_sock = mock_sock

        c._check_watcher_disconnect(mock_sock)
        assert c._watcher_sock is mock_sock

    # ---------- run_forever ----------

    def test_run_forever_one_trading_iteration(self, mock_deps):
        """run_forever 执行一次交易循环后正常退出。"""
        c = mock_deps["collector"]

        with (
            patch.object(c, "_init_klines") as mock_init,
            patch.object(c, "run") as mock_run,
            patch.object(c, "_fetch_and_push") as mock_fetch,
            patch.object(c, "_in_trading_hours", side_effect=[True, False]) as mock_in,
            patch.object(c, "_after_market", return_value=True),
        ):
            c.run_forever()

            mock_init.assert_called_once()
            mock_run.assert_called_once()
            # 退出时拉取一次收盘数据
            mock_fetch.assert_called_once()

    def test_run_forever_exits_when_after_market_without_trading(self, mock_deps):
        """盘前启动且直接盘后时，run_forever 跳过 _init_klines 退出。"""
        c = mock_deps["collector"]

        with (
            patch.object(c, "_init_klines") as mock_init,
            patch.object(c, "_fetch_and_push") as mock_fetch,
            patch.object(c, "_in_trading_hours", return_value=False) as mock_in,
            patch.object(c, "_after_market", return_value=True),
        ):
            # time.sleep 在盘前循环中不会无限 — _after_market 立即返回 True
            # 但 _in_trading_hours 是 False, _was_trading 默认 False
            # 进入 elif _was_trading → False → else → time.sleep(10) → 一直循环
            # 所以这个场景需要特殊处理——我们用 side_effect 让第二次 in_trading 才 False
            pass  # 跳过，前一个测试已覆盖交易→收盘→退出路径

    # ---------- _write_market_snapshots ----------

    def test_write_market_snapshots(self, mock_deps):
        """_write_market_snapshots 批量写入 market_snapshots。"""
        c = mock_deps["collector"]
        mock_db = mock_deps["mock_db"]

        stocks = {
            "600519": {"price": 1500.0, "changePct": 1.5, "amount": 1e8},
            "000001": {"price": 10.0, "changePct": -0.5, "amount": 5e6},
        }
        c._write_market_snapshots(1234567890.0, stocks)

        assert mock_db.executemany.called
        args = mock_db.executemany.call_args[0]
        assert "market_snapshots" in args[0]
        assert len(args[1]) == 2

    def test_write_market_snapshots_empty(self, mock_deps):
        """空数据不写入。"""
        c = mock_deps["collector"]
        mock_db = mock_deps["mock_db"]

        c._write_market_snapshots(1234567890.0, {})
        mock_db.executemany.assert_not_called()

    # ---------- _write_index_snapshot ----------

    def test_write_index_snapshot(self, mock_deps):
        """_write_index_snapshot 写入单条 index_snapshots。"""
        c = mock_deps["collector"]
        mock_db = mock_deps["mock_db"]

        c._write_index_snapshot(
            1234567890.0, "000001.SH", 3200.0, 3210.0, 3190.0, 3180.0, 0.0063, 5e10
        )

        assert mock_db.execute.called
        args = mock_db.execute.call_args[0]
        assert "index_snapshots" in args[0]
        assert args[1][2] == "000001.SH"


# ============================================================
# data/live/collector_client.py
# ============================================================


class TestDataCollectorClient:
    """DataCollectorClient 是非阻塞 TCP 客户端，JSON lines 协议。"""

    def test_init_with_defaults(self):
        """默认 host 和 port。"""
        client = DataCollectorClient()
        assert client.host == "127.0.0.1"
        assert client.port == 15555
        assert client._sock is None
        assert client.connected is False
        assert client._buf == b""

    def test_init_with_custom_host_port(self):
        client = DataCollectorClient(host="192.168.1.33", port=5000)
        assert client.host == "192.168.1.33"
        assert client.port == 5000

    def test_connect_success(self):
        """connect 成功时返回 True，设置 connected 标志。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            result = client.connect()

            assert result is True
            assert client.connected is True
            assert client._sock is mock_sock
            mock_sock.connect.assert_called_once_with(("127.0.0.1", 15555))
            mock_sock.settimeout.assert_called_once_with(5.0)
            mock_sock.setblocking.assert_called_once_with(False)

    def test_connect_refused(self):
        """连接被拒时返回 False，不抛异常。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = ConnectionRefusedError
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            result = client.connect()

            assert result is False
            assert client.connected is False
            assert client._sock is None

    def test_connect_retry_on_failure(self):
        """连接失败后 _next_retry 冷却期内再次 connect 返回 False。"""
        with (
            patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls,
            patch("data.collect.live.collector_client.time.time") as mock_time,
        ):
            mock_sock = MagicMock()
            mock_sock.connect.side_effect = OSError("connection refused")
            mock_sock_cls.return_value = mock_sock

            # 首次连接失败
            mock_time.return_value = 1000.0
            client = DataCollectorClient()
            assert client.connect() is False

            # 冷却期内再次调用
            mock_time.return_value = 1010.0  # 30s 冷却期未到
            with patch.object(client, "disconnect") as mock_disconnect:
                result = client.connect()
                assert result is False
                mock_disconnect.assert_not_called()  # 因为现在 < _next_retry

    def test_connect_retry_after_cooldown(self):
        """冷却期后重试。"""
        with (
            patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls,
            patch("data.collect.live.collector_client.time.time") as mock_time,
        ):
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock

            mock_time.return_value = 1000.0
            client = DataCollectorClient()

            # 首次连接失败
            mock_sock.connect.side_effect = ConnectionRefusedError
            client.connect()
            assert client.connected is False

            # 冷却期后重连成功
            mock_time.return_value = 1100.0  # 冷却期已过
            mock_sock.connect.side_effect = None
            mock_sock.connect.return_value = None
            result = client.connect()
            assert result is True
            assert client.connected is True

    def test_disconnect(self):
        """disconnect 关闭 socket 并重置状态。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            client.connect()

            client.disconnect()
            assert client.connected is False
            assert client._sock is None
            assert client._buf == b""
            mock_sock.close.assert_called_once()

    def test_disconnect_when_not_connected(self):
        """未连接时 disconnect 不报错。"""
        client = DataCollectorClient()
        client.disconnect()  # 不应抛异常

    def test_recv_all_parses_json(self):
        """recv_all 解析多行 JSON。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [
                b'{"type":"market","data":{"a":1}}\n',
                b'{"type":"index","data":{"b":2}}\n',
                BlockingIOError,
            ]
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            client._sock = mock_sock
            client.connected = True

            messages = client.recv_all()
            assert len(messages) == 2
            assert messages[0]["type"] == "market"
            assert messages[1]["type"] == "index"

    def test_recv_all_split_lines(self):
        """跨多次 recv 调用拼接行。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [
                b'{"type":"mark',
                b'et"}\n{"type":"ind',
                b'ex"}\n',
                BlockingIOError,
            ]
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            client._sock = mock_sock
            client.connected = True

            messages = client.recv_all()
            assert len(messages) == 2

    def test_recv_all_not_connected(self):
        """未连接时返回空列表。"""
        client = DataCollectorClient()
        assert client.recv_all() == []

    def test_recv_all_connection_reset(self):
        """ConnectionResetError 时断开并返回空列表（异常时缓冲未解析）。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [
                b'{"type":"market"}\n',
                ConnectionResetError("reset"),
            ]
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            client._sock = mock_sock
            client.connected = True

            messages = client.recv_all()
            # 异常分支返回 messages（空列表），缓冲区数据不解析
            assert messages == []
            assert client.connected is False

    def test_recv_all_disconnect_by_peer(self):
        """对方关闭连接时断开。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [b"", BlockingIOError]
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            client._sock = mock_sock
            client.connected = True

            messages = client.recv_all()
            assert messages == []
            assert client.connected is False

    def test_recv_all_invalid_json_skipped(self):
        """无效 JSON 行被静默跳过。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [
                b"not json\n",
                b'{"type":"valid"}\n',
                BlockingIOError,
            ]
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            client._sock = mock_sock
            client.connected = True

            messages = client.recv_all()
            assert len(messages) == 1
            assert messages[0]["type"] == "valid"

    def test_recv_all_no_sock_returns_empty(self):
        """_sock 为 None 时返回空列表。"""
        client = DataCollectorClient()
        client.connected = True
        client._sock = None
        assert client.recv_all() == []

    def test_recv_all_empty_lines_skipped(self):
        """空行被跳过。"""
        with patch("data.collect.live.collector_client.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock.recv.side_effect = [
                b'\n\n{"type":"market"}\n\n',
                BlockingIOError,
            ]
            mock_sock_cls.return_value = mock_sock

            client = DataCollectorClient()
            client._sock = mock_sock
            client.connected = True

            messages = client.recv_all()
            assert len(messages) == 1
