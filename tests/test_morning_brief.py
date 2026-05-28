# -*- coding: utf-8 -*-
"""早盘简报 v2 单元测试 — MorningBrief (AI 驱动)"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from analysis.morning import MorningBrief


@pytest.fixture
def mock_telegram():
    return MagicMock()


@pytest.fixture
def morning_brief(mock_telegram):
    b = MorningBrief(telegram_bot=mock_telegram)
    b.logger = MagicMock()
    return b


# =====================  Instantiation  =====================


class TestInstantiation:
    def test_create_with_telegram(self, mock_telegram):
        brief = MorningBrief(telegram_bot=mock_telegram)
        assert brief.telegram is mock_telegram

    def test_create_without_telegram(self):
        brief = MorningBrief(telegram_bot=None)
        assert brief.telegram is None


# =====================  Load Review Report  =====================


class TestLoadReviewReport:
    def test_found(self, morning_brief):
        """找到昨日复盘报告时返回全文"""
        mock_file = MagicMock()
        mock_file.read_text.return_value = '复盘报告全文内容'
        with patch('pathlib.Path.glob', return_value=[mock_file]):
            result = morning_brief._load_review_report('2026-05-24')
            assert result == '复盘报告全文内容'

    def test_not_found(self, morning_brief):
        """无复盘报告时返回空字符串"""
        with patch('pathlib.Path.glob', return_value=[]):
            result = morning_brief._load_review_report('2026-05-24')
            assert result == ''


# =====================  Macro  =====================


class TestGetMacroText:
    def test_none_row_returns_empty(self, morning_brief):
        """无数据时返回空字符串"""
        with patch('sqlite3.connect') as mock_conn:
            mock_conn.return_value.execute.return_value.fetchone.return_value = None
            # _get_macro_text 内部先调 MacroCollector，mock 掉避免网络调用
            with patch('data.collectors.macro.macro_collector.MacroCollector'):
                result = morning_brief._get_macro_text()
                assert result == ''


# =====================  Format Articles  =====================


class TestFmtArticles:
    def test_empty(self):
        assert '暂无早报文章' in MorningBrief._fmt_articles({})

    def test_none(self):
        assert '暂无早报文章' in MorningBrief._fmt_articles(None)

    def test_with_morning(self):
        articles = {
            'morning': {'content': '早报正文内容'},
        }
        result = MorningBrief._fmt_articles(articles)
        assert '早报' in result
        assert '早报正文内容' in result

    def test_with_both(self):
        articles = {
            'morning': {'content': '早报正文'},
            'morning_news': {'content': '早间新闻精选正文'},
        }
        result = MorningBrief._fmt_articles(articles)
        assert '早报正文' in result
        assert '早间新闻精选正文' in result

    def test_ignores_bileizhen(self):
        """避雷针不在 fmt_articles 中输出（已单独提取）"""
        articles = {
            'bileizhen': {'content': '避雷针内容'},
        }
        assert '暂无早报文章' in MorningBrief._fmt_articles(articles)


# =====================  Send  =====================


class TestSend:
    def test_send_with_telegram(self, morning_brief, mock_telegram):
        morning_brief._send("test brief")
        mock_telegram.send.assert_called_once_with("test brief")

    def test_send_without_telegram(self):
        brief = MorningBrief(telegram_bot=None)
        brief.logger = MagicMock()
        brief._send("test brief")  # Should not raise

    def test_telegram_failure_fallback(self, morning_brief, mock_telegram):
        mock_telegram.send.side_effect = Exception("network error")
        morning_brief._send("test brief")  # Should not raise


# =====================  Full Flow  =====================


class TestGenerateAndSend:
    def test_full_flow(self, morning_brief):
        """完整流程：加载数据 → 调 AI → 推送"""
        with patch.object(morning_brief, '_load_review_report', return_value='复盘') as mock_r:
            with patch.object(morning_brief, '_get_macro_text', return_value='宏观') as mock_m:
                with patch.object(morning_brief, '_get_morning_articles', return_value={'morning': {'content': '早报'}}) as mock_a:
                    with patch.object(morning_brief, '_get_overnight_telegraphs', return_value='电报') as mock_t:
                        with patch.object(morning_brief, '_call_ai', return_value='AI生成内容') as mock_ai:
                            with patch.object(morning_brief, '_send') as mock_send:
                                morning_brief.generate_and_send('2026-05-25')

                                mock_r.assert_called_once_with('2026-05-24')
                                mock_m.assert_called_once()
                                mock_a.assert_called_once()
                                mock_t.assert_called_once_with('2026-05-24')
                                mock_ai.assert_called_once()
                                mock_send.assert_called_once()
                                assert '2026-05-25' in mock_send.call_args[0][0]
                                assert 'AI生成内容' in mock_send.call_args[0][0]

    def test_default_trade_date(self, morning_brief):
        """不传 trade_date 时使用当天日期"""
        today = datetime.now().strftime('%Y-%m-%d')
        with patch.object(morning_brief, '_load_review_report', return_value=''):
            with patch.object(morning_brief, '_get_macro_text', return_value=''):
                with patch.object(morning_brief, '_get_morning_articles', return_value={}):
                    with patch.object(morning_brief, '_get_overnight_telegraphs', return_value=''):
                        with patch.object(morning_brief, '_call_ai', return_value='AI'):
                            with patch.object(morning_brief, '_send'):
                                morning_brief.generate_and_send()

    def test_ai_failure_no_crash(self, morning_brief):
        """AI 调用失败时不崩溃"""
        with patch.object(morning_brief, '_load_review_report', return_value=''):
            with patch.object(morning_brief, '_get_macro_text', return_value=''):
                with patch.object(morning_brief, '_get_morning_articles', return_value={}):
                    with patch.object(morning_brief, '_get_overnight_telegraphs', return_value=''):
                        with patch.object(morning_brief, '_call_ai', return_value=None):
                            with patch.object(morning_brief, '_send') as mock_send:
                                morning_brief.generate_and_send('2026-05-25')
                                mock_send.assert_not_called()

    def test_missing_data_sources_continue(self, morning_brief):
        """各数据源缺失时仍能完成流程"""
        with patch.object(morning_brief, '_load_review_report', return_value=''):
            with patch.object(morning_brief, '_get_macro_text', return_value=''):
                with patch.object(morning_brief, '_get_morning_articles', return_value={}):
                    with patch.object(morning_brief, '_get_overnight_telegraphs', return_value=''):
                        with patch.object(morning_brief, '_call_ai', return_value='AI') as mock_ai:
                            with patch.object(morning_brief, '_send'):
                                morning_brief.generate_and_send('2026-05-25')
                                mock_ai.assert_called_once()
