# -*- coding: utf-8 -*-
"""早盘简报单元测试 — MorningBrief"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from analysis.morning import MorningBrief


# =====================  Fixtures  =====================


@pytest.fixture
def mock_telegram():
    return MagicMock()


@pytest.fixture
def morning_brief(mock_telegram):
    b = MorningBrief(telegram_bot=mock_telegram)
    b.logger = MagicMock()
    return b


@pytest.fixture
def sample_macro():
    """模拟 macro_daily 行数据"""
    return {
        "trade_date": "2026-05-21",
        "nasdaq_change": 1.23,
        "kweb_change": -0.56,
        "usd_cny_rate": 7.2450,
        "a50_price": 13520.50,
        "a50_change": 0.35,
        "crude_oil_price": 78.50,
        "crude_oil_change": -0.89,
        "gold_price": 2715.30,
        "gold_change": 0.12,
    }


@pytest.fixture
def sample_signals():
    """模拟 pending BUY 信号"""
    return [
        {
            "id": 1,
            "stock_code": "000001",
            "stock_name": "平安银行",
            "signal_type": "BUY",
            "buy_zone_min": 12.00,
            "buy_zone_max": 13.00,
            "stop_loss": 11.50,
            "take_profit": 14.00,
            "signal_score": 85.0,
            "strategy_name": "trend_follow",
            "reason": "突破MA20放量",
        },
        {
            "id": 2,
            "stock_code": "600519",
            "stock_name": "贵州茅台",
            "signal_type": "BUY",
            "buy_zone_min": 1800.00,
            "buy_zone_max": 1850.00,
            "stop_loss": 1750.00,
            "take_profit": 2000.00,
            "signal_score": 72.0,
            "strategy_name": "ai_advisor",
            "reason": "超跌反弹",
        },
    ]


# =====================  Instantiation  =====================


class TestInstantiation:
    def test_create_with_telegram(self, mock_telegram):
        brief = MorningBrief(telegram_bot=mock_telegram)
        assert brief.telegram is mock_telegram
        assert brief.repo is not None

    def test_create_without_telegram(self):
        brief = MorningBrief(telegram_bot=None)
        assert brief.telegram is None
        assert brief.repo is not None


# =====================  Macro  =====================


class TestGetLatestMacro:
    def test_returns_dict(self, morning_brief):
        """macros 表有数据时正常返回 dict"""
        mock_row = {"trade_date": "2026-05-21", "nasdaq_change": 1.23}
        with patch("sqlite3.connect") as mock_conn:
            mock_conn.return_value.row_factory = None
            mock_execute = MagicMock()
            mock_conn.return_value.execute.return_value = mock_execute
            mock_execute.fetchone.return_value = mock_row

            result = morning_brief._get_latest_macro()
            assert result == mock_row

    def test_empty_table(self, morning_brief):
        """macros 表无数据时返回空 dict"""
        with patch("sqlite3.connect") as mock_conn:
            mock_conn.return_value.row_factory = None
            mock_execute = MagicMock()
            mock_conn.return_value.execute.return_value = mock_execute
            mock_execute.fetchone.return_value = None

            result = morning_brief._get_latest_macro()
            assert result == {}

    def test_table_not_exists(self, morning_brief):
        """表不存在时返回空 dict，不抛异常"""
        with patch("sqlite3.connect") as mock_conn:
            mock_conn.return_value.row_factory = None
            mock_conn.return_value.execute.side_effect = Exception("no such table")
            result = morning_brief._get_latest_macro()
            assert result == {}


class TestUpdateMacro:
    def test_success(self, morning_brief):
        """正常更新宏观数据"""
        mock_collector = MagicMock()
        with patch(
            "data.collectors.macro.macro_collector.MacroCollector", return_value=mock_collector
        ):
            morning_brief._update_macro("2026-05-22")
            mock_collector.fetch_and_save.assert_called_once()

    def test_failure_logged(self, morning_brief):
        """采集失败不抛异常"""
        with patch("data.collectors.macro.macro_collector.MacroCollector") as mock_cls:
            mock_cls.side_effect = Exception("macro error")
            # Should not raise
            morning_brief._update_macro("2026-05-22")
            morning_brief.logger.warning.assert_called_once()


# =====================  Pending Signals  =====================


class TestGetPendingSignals:
    def test_returns_list(self, morning_brief, sample_signals):
        """正常返回待处理信号列表"""
        morning_brief.repo = MagicMock()
        morning_brief.repo.get_pending_signals.return_value = sample_signals

        result = morning_brief._get_pending_signals("2026-05-22")
        assert result == sample_signals
        morning_brief.repo.get_pending_signals.assert_called_once_with("2026-05-22")

    def test_empty(self, morning_brief):
        """无待处理信号返回空列表"""
        morning_brief.repo = MagicMock()
        morning_brief.repo.get_pending_signals.return_value = []

        result = morning_brief._get_pending_signals("2026-05-22")
        assert result == []

    def test_db_exception(self, morning_brief):
        """DB 异常时返回空列表，不抛异常"""
        morning_brief.repo = MagicMock()
        morning_brief.repo.get_pending_signals.side_effect = Exception("db error")

        result = morning_brief._get_pending_signals("2026-05-22")
        assert result == []


# =====================  Build Brief  =====================


class TestBuildBrief:
    def test_all_sections_present(self, morning_brief, sample_macro, sample_signals):
        """完整数据时包含三个板块"""
        brief = morning_brief._build_brief("2026-05-22", sample_macro, sample_signals)
        assert "早盘简报" in brief
        assert "2026-05-22" in brief
        assert "【隔夜宏观】" in brief
        assert "【候选池确认】" in brief
        assert "【今日关注】" in brief

    def test_macro_values_formatted(self, morning_brief, sample_macro, sample_signals):
        """宏观数据显示格式正确"""
        brief = morning_brief._build_brief("2026-05-22", sample_macro, sample_signals)
        assert "+1.23%" in brief  # nasdaq_change
        assert "-0.56%" in brief  # kweb_change
        assert "7.2450" in brief  # usd_cny_rate
        assert "13520.50" in brief  # a50_price
        assert "-0.89%" in brief  # crude_oil_change

    def test_signal_listings(self, morning_brief, sample_macro, sample_signals):
        """候选池中每个信号的详细信息"""
        brief = morning_brief._build_brief("2026-05-22", sample_macro, sample_signals)
        assert "000001" in brief
        assert "平安银行" in brief
        assert "600519" in brief
        assert "贵州茅台" in brief
        # buy zones
        assert "12.00" in brief
        assert "13.00" in brief
        assert "1800.00" in brief
        assert "1850.00" in brief
        # stop loss
        assert "11.50" in brief
        assert "1750.00" in brief

    def test_watch_list_count(self, morning_brief, sample_macro, sample_signals):
        """今日关注板块中显示候选数量"""
        brief = morning_brief._build_brief("2026-05-22", sample_macro, sample_signals)
        assert "2" in brief  # 2 candidates

    def test_empty_macro(self, morning_brief, sample_signals):
        """宏观数据为空"""
        brief = morning_brief._build_brief("2026-05-22", {}, sample_signals)
        assert "暂无宏观数据" in brief
        assert "【候选池确认】" in brief
        assert "平安银行" in brief

    def test_empty_signals(self, morning_brief, sample_macro):
        """无候选信号"""
        brief = morning_brief._build_brief("2026-05-22", sample_macro, [])
        assert "(0只)" in brief
        assert "无待处理买入信号" in brief
        assert "【隔夜宏观】" in brief
        assert "暂无候选交易信号" in brief

    def test_empty_macro_and_signals(self, morning_brief):
        """宏观和信号均为空"""
        brief = morning_brief._build_brief("2026-05-22", {}, [])
        assert "暂无宏观数据" in brief
        assert "(0只)" in brief
        assert "无待处理买入信号" in brief

    def test_signal_without_buy_zone(self, morning_brief, sample_macro):
        """信号没有买入区间仍正常显示"""
        signals = [
            {
                "id": 1,
                "stock_code": "000001",
                "stock_name": "平安银行",
                "buy_zone_min": None,
                "buy_zone_max": None,
                "stop_loss": 11.50,
                "take_profit": 14.00,
                "signal_score": None,
            }
        ]
        brief = morning_brief._build_brief("2026-05-22", sample_macro, signals)
        assert "000001" in brief
        assert "--" in brief  # 无区间显示 --

    def test_macro_with_none_values(self, morning_brief, sample_signals):
        """宏观字段有 None 值时容错"""
        macro = {
            "nasdaq_change": None,
            "a50_price": None,
            "a50_change": None,
        }
        # Should not crash
        brief = morning_brief._build_brief("2026-05-22", macro, sample_signals)
        assert "【隔夜宏观】" in brief
        assert "暂无宏观数据" in brief  # All None -> fallback


# =====================  Send  =====================


class TestSend:
    def test_send_with_telegram(self, morning_brief, mock_telegram):
        """有 Telegram bot 时调用 send"""
        morning_brief._send("test brief")
        mock_telegram.send.assert_called_once_with("test brief")

    def test_send_without_telegram(self):
        """无 Telegram bot 时降级到 print（不抛异常）"""
        brief = MorningBrief(telegram_bot=None)
        brief.logger = MagicMock()
        # Should not raise
        brief._send("test brief")

    def test_telegram_failure_fallback(self, morning_brief, mock_telegram):
        """Telegram 发送失败时降级到 print（不抛异常）"""
        mock_telegram.send.side_effect = Exception("network error")
        # Should not raise
        morning_brief._send("test brief")


# =====================  Formatting Helpers  =====================


class TestFormatHelpers:
    def test_fmt_change_positive(self):
        assert MorningBrief._fmt_change(1.23) == "+1.23%"

    def test_fmt_change_negative(self):
        assert MorningBrief._fmt_change(-0.56) == "-0.56%"

    def test_fmt_change_zero(self):
        assert MorningBrief._fmt_change(0.0) == "+0.00%"

    def test_fmt_change_none(self):
        assert MorningBrief._fmt_change(None) == "--"

    def test_fmt_price(self):
        assert MorningBrief._fmt_price(12.3456) == "12.35"

    def test_fmt_price_none(self):
        assert MorningBrief._fmt_price(None) == "--"

    def test_fmt_price_custom_decimal(self):
        assert MorningBrief._fmt_price(7.2450, 4) == "7.2450"


# =====================  Full Flow  =====================


class TestGenerateAndSend:
    def test_full_flow(self, morning_brief, sample_macro, sample_signals):
        """完整流程：更新宏观 -> 加载信号 -> 构建 -> 推送"""
        with patch.object(morning_brief, "_update_macro") as mock_update:
            with patch.object(
                morning_brief, "_get_latest_macro", return_value=sample_macro
            ):
                with patch.object(
                    morning_brief,
                    "_get_pending_signals",
                    return_value=sample_signals,
                ):
                    with patch.object(morning_brief, "_build_brief") as mock_build:
                        with patch.object(morning_brief, "_send") as mock_send:
                            mock_build.return_value = "formatted brief"

                            morning_brief.generate_and_send("2026-05-22")

                            mock_update.assert_called_once_with("2026-05-22")
                            morning_brief._get_latest_macro.assert_called_once()
                            morning_brief._get_pending_signals.assert_called_once_with(
                                "2026-05-22"
                            )
                            mock_build.assert_called_once_with(
                                "2026-05-22", sample_macro, sample_signals
                            )
                            mock_send.assert_called_once_with("formatted brief")

    def test_default_trade_date(self, morning_brief, sample_macro, sample_signals):
        """不传 trade_date 时使用当天日期"""
        with patch.object(morning_brief, "_update_macro") as mock_update:
            with patch.object(
                morning_brief, "_get_latest_macro", return_value=sample_macro
            ):
                with patch.object(
                    morning_brief, "_get_pending_signals", return_value=[]
                ):
                    with patch.object(morning_brief, "_build_brief") as mock_build:
                        with patch.object(morning_brief, "_send") as mock_send:
                            mock_build.return_value = "brief"

                            morning_brief.generate_and_send()

                            # 验证传入了今天的日期
                            today = datetime.now().strftime("%Y-%m-%d")
                            mock_update.assert_called_once_with(today)

    def test_macro_update_failure_continues(self, morning_brief, sample_signals):
        """宏观更新失败不影响后续流程（使用缓存数据）"""
        with patch.object(morning_brief, "_update_macro") as mock_update:
            with patch.object(
                morning_brief, "_get_latest_macro", return_value={}
            ):
                with patch.object(
                    morning_brief,
                    "_get_pending_signals",
                    return_value=sample_signals,
                ):
                    with patch.object(morning_brief, "_build_brief") as mock_build:
                        with patch.object(morning_brief, "_send") as mock_send:
                            mock_build.return_value = "brief"

                            morning_brief.generate_and_send("2026-05-22")
                            mock_build.assert_called_once()
                            mock_send.assert_called_once()

    def test_send_failure_logged(self, morning_brief, sample_macro):
        """推送失败不抛异常"""
        with patch.object(morning_brief, "_update_macro"):
            with patch.object(
                morning_brief, "_get_latest_macro", return_value=sample_macro
            ):
                with patch.object(
                    morning_brief, "_get_pending_signals", return_value=[]
                ):
                    with patch.object(
                        morning_brief, "_send"
                    ) as mock_send:
                        mock_send.side_effect = Exception("send error")
                        # Should not raise
                        morning_brief.generate_and_send("2026-05-22")
