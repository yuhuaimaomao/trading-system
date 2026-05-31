"""Telegram 消息接收单元测试 — MessageReceiver + Watcher 集成"""

from unittest.mock import MagicMock, patch

import pytest

from system.utils.telegram import MessageReceiver

# =====================  Fixtures  =====================


@pytest.fixture
def receiver():
    return MessageReceiver(bot_token="test:token")


@pytest.fixture
def mock_updates_response():
    """模拟 Telegram getUpdates API 返回格式。"""
    return {
        "ok": True,
        "result": [
            {
                "update_id": 101,
                "message": {
                    "message_id": 55,
                    "from": {
                        "id": 771664450,
                        "first_name": "TestUser",
                        "username": "testuser",
                    },
                    "chat": {"id": 771664450, "type": "private"},
                    "date": 1717000000,
                    "text": "模拟盘 000001 1000股 12.50",
                },
            },
            {
                "update_id": 102,
                "message": {
                    "message_id": 56,
                    "from": {
                        "id": 771664450,
                        "first_name": "TestUser",
                        "username": "testuser",
                    },
                    "chat": {"id": 771664450, "type": "private"},
                    "date": 1717000060,
                    "text": "实盘 000002 800股 25.00",
                },
            },
        ],
    }


@pytest.fixture
def mock_empty_response():
    return {"ok": True, "result": []}


@pytest.fixture
def mock_watcher(mock_telegram, mock_qmt):
    from trade.monitor.watcher import Watcher

    w = Watcher(telegram_bot=mock_telegram, qmt_quote=mock_qmt, db_path=":memory:")
    w._get_paper_trader = MagicMock()
    w._trade_date = "2026-05-26"
    w._load_review_picks = MagicMock(return_value=[])
    return w


@pytest.fixture
def mock_telegram():
    return MagicMock()


@pytest.fixture
def mock_qmt():
    client = MagicMock()
    client.get_realtime.return_value = {}
    return client


# =====================  MessageReceiver 测试  =====================


class TestMessageReceiver:
    def test_init_with_token(self):
        r = MessageReceiver(bot_token="abc:123")
        assert r.bot_token == "abc:123"
        assert r._last_update_id >= 0

    def test_init_without_token_raises(self):
        with patch("system.config.settings.TELEGRAM_REPORT_BOT_TOKEN", ""):
            with pytest.raises(ValueError, match="未配置"):
                MessageReceiver()

    def test_fetch_updates_returns_messages(self, receiver, mock_updates_response):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_updates_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            msgs = receiver.fetch_updates()
            assert len(msgs) == 2
            assert msgs[0]["text"] == "模拟盘 000001 1000股 12.50"
            assert msgs[0]["chat_id"] == "771664450"
            assert msgs[0]["user"] == "TestUser"
            assert msgs[1]["text"] == "实盘 000002 800股 25.00"

    def test_fetch_updates_tracks_last_id(self, receiver, mock_updates_response):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_updates_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            receiver.fetch_updates()
            assert receiver._last_update_id == 102

    def test_fetch_updates_respects_offset(self, receiver, mock_empty_response):
        receiver._last_update_id = 100
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_empty_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            msgs = receiver.fetch_updates()
            assert len(msgs) == 0
            call_args = mock_get.call_args
            assert call_args[1]["params"]["offset"] == 101

    def test_fetch_updates_empty(self, receiver, mock_empty_response):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_empty_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            msgs = receiver.fetch_updates()
            assert msgs == []

    def test_fetch_updates_skips_non_text(self, receiver):
        """没有 text 字段的消息被跳过。"""
        response = {
            "ok": True,
            "result": [
                {
                    "update_id": 200,
                    "message": {
                        "message_id": 60,
                        "from": {"id": 1, "first_name": "X"},
                        "chat": {"id": 1},
                        "date": 1717000000,
                        # 无 text 字段
                    },
                }
            ],
        }
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            msgs = receiver.fetch_updates()
            assert len(msgs) == 0

    def test_fetch_updates_api_error(self, receiver):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ok": False, "description": "error"}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            msgs = receiver.fetch_updates()
            assert msgs == []

    def test_fetch_updates_network_error(self, receiver):
        with patch("requests.get", side_effect=Exception("timeout")):
            msgs = receiver.fetch_updates()
            assert msgs == []

    def test_fetch_updates_sets_timeout(self, receiver, mock_empty_response):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_empty_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            receiver.fetch_updates(timeout=20)
            call_args = mock_get.call_args
            assert call_args[1]["params"]["timeout"] == 20


# =====================  Watcher 集成测试  =====================


class TestWatcherReplies:
    def test_check_replies_integration(self, mock_watcher, mock_updates_response):
        """_check_replies 拉消息 → 手动执行器处理 → 确认消息转发到 telegram。"""
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_updates_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            # patch TradeRepository to avoid real DB
            with patch(
                "data.repo.TradeRepository.get_pending_signals",
                return_value=[
                    {
                        "id": 1,
                        "stock_code": "000001",
                        "stock_name": "平安银行",
                    }
                ],
            ):
                with patch("data.repo.TradeRepository.insert_order", return_value=100):
                    with patch("data.repo.TradeRepository.update_signal_status"):
                        with patch(
                            "data.repo.TradeRepository.get_orders_by_date",
                            return_value=[],
                        ):
                            mock_watcher._check_replies()

        # 确认 telegram.send 被调用了（确认消息）
        assert mock_watcher.telegram.send.called

    def test_check_replies_no_messages(self, mock_watcher, mock_empty_response):
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = mock_empty_response
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            mock_watcher._check_replies()
            # 无消息时不应该调用 send
            mock_watcher.telegram.send.assert_not_called()

    def test_check_replies_receiver_error(self, mock_watcher):
        with patch("requests.get", side_effect=Exception("network down")):
            # 不应该抛异常
            mock_watcher._check_replies()
            mock_watcher.telegram.send.assert_not_called()

    def test_check_replies_lazy_init(self, mock_watcher):
        assert mock_watcher._receiver is None
        assert mock_watcher._executor is None

        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"ok": True, "result": []}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            mock_watcher._check_replies()

        assert mock_watcher._receiver is not None
        assert mock_watcher._executor is not None
