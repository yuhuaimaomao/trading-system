"""system/message/ 模块测试 — Telegram 消息发送、接收、路由。

所有外部调用 (requests) 均通过 unittest.mock 隔离。
部分功能 (send_photo, send_document, parse_command) 标记为 xfail，
表示尚需实现但预期会新增的方法。
"""

from unittest.mock import MagicMock, patch

import pytest

from system.message.receiver import MessageReceiver
from system.message.router import AlertRouter
from system.message.sender import MessageSender

# ============================================================
# Helpers
# ============================================================


def _sender(chat_id="111", bot_token="test:token"):
    """创建 MessageSender 实例，绕过环境依赖。"""
    return MessageSender(chat_id=chat_id, bot_token=bot_token)


def _receiver(bot_token="test:token"):
    """创建 MessageReceiver 实例，绕过文件 IO 和环境依赖。"""
    with patch.object(MessageReceiver, "_load_last_update_id", return_value=0):
        r = MessageReceiver(bot_token=bot_token)
    r._save_last_update_id = MagicMock()  # 避免污染 production 状态文件
    return r


# ============================================================
# MessageSender
# ============================================================


class TestMessageSender:
    """MessageSender — Telegram Bot 消息发送"""

    # ── __init__ ──

    def test_init_with_token(self):
        """传入 bot_token 和 chat_id → 正常初始化。"""
        s = MessageSender(chat_id="c1", bot_token="my:token")
        assert s.bot_token == "my:token"
        assert s.chat_id == "c1"

    def test_init_from_settings(self):
        """不传 token → 从 system.config.settings 加载。"""
        with patch("system.config.settings.TELEGRAM_REPORT_BOT_TOKEN", "env_token"):
            s = MessageSender(chat_id="c1")
            assert s.bot_token == "env_token"

    def test_init_no_token_raises_value_error(self):
        """既无参数也无 env 配置 → 抛 ValueError。"""
        with patch("system.config.settings.TELEGRAM_REPORT_BOT_TOKEN", ""):
            with pytest.raises(ValueError, match="未配置"):
                MessageSender(chat_id="c1")

    # ── send (send_message) ──

    @patch("system.message.sender.requests.post")
    def test_send_calls_post_with_correct_url(self, mock_post):
        """send() → requests.post 调用 /sendMessage 端点。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 42}}
        mock_post.return_value = mock_response

        s = _sender(chat_id="c1", bot_token="tk")
        s.send("hello")

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        assert "bottk/sendMessage" in url

    @patch("system.message.sender.requests.post")
    def test_send_payload_contains_parse_mode(self, mock_post):
        """send() payload 包含 parse_mode=HTML。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 42}}
        mock_post.return_value = mock_response

        s = _sender(chat_id="c1", bot_token="tk")
        s.send("hello")

        payload = mock_post.call_args[1]["json"]
        assert payload["parse_mode"] == "HTML"
        assert payload["chat_id"] == "c1"
        assert payload["text"] == "hello"
        assert payload["disable_web_page_preview"] is True

    @patch("system.message.sender.requests.post")
    def test_send_html_escapes_special_chars(self, mock_post):
        """文本中的 & < > 被转义。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 42}}
        mock_post.return_value = mock_response

        s = _sender()
        s.send("<b>B&W</b>")

        payload = mock_post.call_args[1]["json"]
        assert "&lt;b&gt;B&amp;W&lt;/b&gt;" in payload["text"]

    @patch("system.message.sender.requests.post")
    def test_send_long_message_chunking(self, mock_post):
        """超过 4000 字符 → 自动分片，第二段带 reply_to_message_id。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 99}}
        mock_post.return_value = mock_response

        s = _sender()
        s.send("X" * 6000)

        assert mock_post.call_count == 2
        payload1 = mock_post.call_args_list[0][1]["json"]
        payload2 = mock_post.call_args_list[1][1]["json"]
        assert len(payload1["text"]) == 4000
        assert len(payload2["text"]) == 2000
        assert payload2["reply_to_message_id"] == 99

    @patch("system.message.sender.requests.post")
    def test_send_passes_proxies(self, mock_post):
        """send() 传递 proxies 参数。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 42}}
        mock_post.return_value = mock_response

        with patch(
            "system.message.sender.TELEGRAM_PROXIES",
            {"http": "http://p", "https": "http://p"},
        ):
            s = _sender()
            s.send("hi")

        assert mock_post.call_args[1].get("proxies") == {
            "http": "http://p",
            "https": "http://p",
        }

    @patch("system.message.sender.requests.post")
    @patch("system.message.sender.logger")
    def test_send_handles_network_error(self, mock_logger, mock_post):
        """网络异常 → 不崩溃，日志记录错误。"""
        mock_post.side_effect = ConnectionError("connection refused")

        s = _sender()
        s.send("test")  # should not raise

        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "connection refused" in error_msg.lower()

    @patch("system.message.sender.requests.post")
    @patch("system.message.sender.logger")
    def test_send_handles_http_error(self, mock_logger, mock_post):
        """非 2xx 状态码 → raise_for_status 抛异常，日志记录。"""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("400 Bad Request")
        mock_post.return_value = mock_response

        s = _sender()
        s.send("test")

        mock_logger.error.assert_called_once()
        error_msg = mock_logger.error.call_args[0][0]
        assert "发送异常" in error_msg

    @patch("system.message.sender.requests.post")
    @patch("system.message.sender.logger")
    def test_send_handles_api_error_response(self, mock_logger, mock_post):
        """200 但 ok=false → 日志记录 API 错误信息。"""
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "ok": False,
            "description": "bot was blocked",
        }
        mock_post.return_value = mock_response

        s = _sender()
        s.send("test")

        mock_logger.error.assert_called_once_with(
            "Telegram API 返回错误: {'ok': False, 'description': 'bot was blocked'}"
        )

    # ── send_photo（尚未实现）──

    @pytest.mark.xfail(reason="send_photo 尚未实现", raises=AttributeError)
    @patch("system.message.sender.requests.post")
    def test_send_photo_calls_send_photo_endpoint(self, mock_post):
        """send_photo → 调用 /sendPhoto 端点。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        mock_post.return_value = mock_response

        s = _sender()
        s.send_photo(photo=b"fake-image", caption="chart")

        url = mock_post.call_args[0][0]
        assert "sendPhoto" in url

    @pytest.mark.xfail(reason="send_photo 尚未实现", raises=AttributeError)
    @patch("system.message.sender.requests.post")
    def test_send_photo_payload(self, mock_post):
        """send_photo payload 包含 photo 和 caption。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        mock_post.return_value = mock_response

        s = _sender()
        s.send_photo(photo=b"img", caption="chart", parse_mode="HTML")

        kwargs = mock_post.call_args[1]
        assert kwargs["json"]["photo"] == b"img"
        assert kwargs["json"]["caption"] == "chart"

    # ── send_document（尚未实现）──

    @pytest.mark.xfail(reason="send_document 尚未实现", raises=AttributeError)
    @patch("system.message.sender.requests.post")
    def test_send_document_calls_send_document_endpoint(self, mock_post):
        """send_document → 调用 /sendDocument 端点。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        mock_post.return_value = mock_response

        s = _sender()
        s.send_document(document=b"fake-pdf", caption="report")

        url = mock_post.call_args[0][0]
        assert "sendDocument" in url

    @pytest.mark.xfail(reason="send_document 尚未实现", raises=AttributeError)
    @patch("system.message.sender.requests.post")
    def test_send_document_payload(self, mock_post):
        """send_document payload 包含 document 和 caption。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        mock_post.return_value = mock_response

        s = _sender()
        s.send_document(document=b"pdf", caption="report")

        kwargs = mock_post.call_args[1]
        assert kwargs["json"]["document"] == b"pdf"
        assert kwargs["json"]["caption"] == "report"


# ============================================================
# MessageReceiver
# ============================================================


class TestMessageReceiver:
    """MessageReceiver — Telegram 消息接收（getUpdates 长轮询）"""

    # ── __init__ ──

    def test_init_with_token(self):
        """传入 bot_token → 正常初始化。"""
        with patch.object(MessageReceiver, "_load_last_update_id", return_value=0):
            r = MessageReceiver(bot_token="my:token")
        assert r.bot_token == "my:token"

    def test_init_from_settings(self):
        """不传 token → 从 settings 加载。"""
        with patch("system.config.settings.TELEGRAM_REPORT_BOT_TOKEN", "env_token"):
            with patch.object(MessageReceiver, "_load_last_update_id", return_value=0):
                r = MessageReceiver()
        assert r.bot_token == "env_token"

    def test_init_no_token_raises_value_error(self):
        """无 token → 抛 ValueError。"""
        with patch("system.config.settings.TELEGRAM_REPORT_BOT_TOKEN", ""):
            with patch.object(MessageReceiver, "_load_last_update_id", return_value=0):
                with pytest.raises(ValueError, match="未配置"):
                    MessageReceiver()

    # ── _load_last_update_id ──

    def test_load_last_update_id_file_exists(self, tmp_path):
        """状态文件存在 → 读取内容并返回 int。"""
        state_file = tmp_path / "telegram_last_update_id.txt"
        state_file.write_text("42")

        r = _receiver()
        r._state_file = str(state_file)  # 替换为临时文件路径
        result = r._load_last_update_id()

        assert result == 42

    def test_load_last_update_id_file_missing(self):
        """状态文件不存在 → 返回 0。"""
        r = _receiver()
        r._state_file = "/tmp/_test_telegram_nonexistent_xxx.txt"
        result = r._load_last_update_id()

        assert result == 0

    # ── fetch_updates (get_updates) ──

    @patch("system.message.receiver.requests.get")
    def test_fetch_updates_calls_telegram_api(self, mock_get):
        """fetch_updates → requests.get 调用 Telegram API。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": []}
        mock_get.return_value = mock_response

        r = _receiver()
        r.fetch_updates(timeout=10)

        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        assert "bottest:token/getUpdates" in url

    @patch("system.message.receiver.requests.get")
    def test_fetch_updates_params(self, mock_get):
        """fetch_updates 传参：offset、timeout、allowed_updates。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": []}
        mock_get.return_value = mock_response

        r = _receiver()
        r._last_update_id = 50
        r.fetch_updates(timeout=15)

        params = mock_get.call_args[1]["params"]
        assert params["offset"] == 51
        assert params["timeout"] == 15
        assert params["allowed_updates"] == ["message"]

        # requests.get 的 timeout 比 Telegram 长轮询多 10 秒
        assert mock_get.call_args[1]["timeout"] == 25

    @patch("system.message.receiver.requests.get")
    def test_fetch_updates_returns_messages(self, mock_get):
        """fetch_updates 解析更新并返回消息列表。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 100,
                    "message": {
                        "message_id": 1,
                        "from": {"id": 1, "first_name": "Alice", "username": "alice"},
                        "chat": {"id": -100, "type": "group"},
                        "text": "/start",
                        "date": 1234567890,
                    },
                }
            ],
        }
        mock_get.return_value = mock_response

        r = _receiver()
        messages = r.fetch_updates()

        assert len(messages) == 1
        msg = messages[0]
        assert msg["chat_id"] == "-100"
        assert msg["text"] == "/start"
        assert msg["username"] == "alice"
        assert msg["message_id"] == "1"
        assert msg["ts"] == 1234567890

    @patch("system.message.receiver.requests.get")
    def test_fetch_updates_skips_empty_text(self, mock_get):
        """更新中 message.text 为空 → 跳过。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 200,
                    "message": {
                        "message_id": 2,
                        "from": {"id": 1},
                        "chat": {"id": -100},
                        "text": "",  # empty text
                        "date": 1234567891,
                    },
                }
            ],
        }
        mock_get.return_value = mock_response

        r = _receiver()
        messages = r.fetch_updates()

        assert messages == []
        # _last_update_id 仍被更新
        assert r._last_update_id == 200

    @patch("system.message.receiver.requests.get")
    def test_fetch_updates_empty_result(self, mock_get):
        """无新更新 → 返回空列表。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": []}
        mock_get.return_value = mock_response

        r = _receiver()
        messages = r.fetch_updates()

        assert messages == []

    @patch("system.message.receiver.requests.get")
    def test_fetch_updates_handles_error_response(self, mock_get):
        """API 返回 ok=false → 返回空列表。"""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "description": "unauthorized"}
        mock_get.return_value = mock_response

        r = _receiver()
        messages = r.fetch_updates()

        assert messages == []

    @patch("system.message.receiver.requests.get")
    def test_fetch_updates_handles_network_timeout(self, mock_get):
        """网络超时 → 返回空列表。"""
        mock_get.side_effect = TimeoutError("timed out")

        r = _receiver()
        messages = r.fetch_updates()

        assert messages == []

    @patch("system.message.receiver.requests.get")
    def test_fetch_updates_handles_http_error(self, mock_get):
        """HTTP 错误 → 返回空列表。"""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("500 Server Error")
        mock_get.return_value = mock_response

        r = _receiver()
        messages = r.fetch_updates()

        assert messages == []

    # ── parse_command（尚未实现）──

    @pytest.mark.xfail(reason="parse_command 尚未实现", raises=AttributeError)
    def test_parse_command_extracts_command(self):
        """parse_command('/start hello world') → '/start'。"""
        r = _receiver()
        cmd = r.parse_command("/start hello world")
        assert cmd == "/start"

    @pytest.mark.xfail(reason="parse_command 尚未实现", raises=AttributeError)
    def test_parse_command_no_command(self):
        """parse_command('plain text') → None。"""
        r = _receiver()
        assert r.parse_command("plain text") is None

    @pytest.mark.xfail(reason="parse_command 尚未实现", raises=AttributeError)
    def test_parse_command_empty_string(self):
        """parse_command('') → None。"""
        r = _receiver()
        assert r.parse_command("") is None

    @pytest.mark.xfail(reason="parse_command 尚未实现", raises=AttributeError)
    def test_parse_command_only_slash(self):
        """parse_command('/') → None（不是有效命令）。"""
        r = _receiver()
        assert r.parse_command("/") is None


# ============================================================
# AlertRouter
# ============================================================


class TestAlertRouter:
    """AlertRouter — 带去重/冷却的高频告警路由"""

    # ── __init__ ──

    def test_init_stores_bots(self):
        """构造时保存 group_bot 和 private_bot。"""
        group, private = MagicMock(), MagicMock()
        router = AlertRouter(group_bot=group, private_bot=private)
        assert router._group is group
        assert router._private is private

    def test_init_defaults_to_none(self):
        """不传 bot → _group / _private 为 None（不会崩溃）。"""
        router = AlertRouter()
        assert router._group is None
        assert router._private is None

    # ── new_round ──

    def test_new_round_updates_scan_count(self):
        """new_round 更新扫描轮次计数。"""
        router = AlertRouter()
        assert router._scan_count == 0
        router.new_round(42)
        assert router._scan_count == 42

    # ── send (route) ──

    def test_send_group_channel(self):
        """send(channel='group') → 仅调用 group bot 的 send_message。"""
        group, private = MagicMock(), MagicMock()
        router = AlertRouter(group_bot=group, private_bot=private)

        router.send("hello", channel="group")

        group.send.assert_called_once_with("hello")
        private.send.assert_not_called()

    def test_send_private_channel(self):
        """send(channel='private') → 仅调用 private bot 的 send。"""
        group, private = MagicMock(), MagicMock()
        router = AlertRouter(group_bot=group, private_bot=private)

        router.send("hello", channel="private")

        private.send.assert_called_once_with("hello")
        group.send.assert_not_called()

    def test_send_both_channels(self):
        """send(channel='both') → 同时调用两个 bot。"""
        group, private = MagicMock(), MagicMock()
        router = AlertRouter(group_bot=group, private_bot=private)

        router.send("hello", channel="both")

        group.send.assert_called_once_with("hello")
        private.send.assert_called_once_with("hello")

    def test_send_defaults_to_group(self):
        """不指定 channel → 默认 group。"""
        group, private = MagicMock(), MagicMock()
        router = AlertRouter(group_bot=group, private_bot=private)

        router.send("hello")

        group.send.assert_called_once_with("hello")
        private.send.assert_not_called()

    def test_send_skips_when_no_bot(self):
        """对应 bot 为 None → 静默跳过，不崩溃。"""
        router = AlertRouter()
        router.send("hello", channel="both")  # should not raise
        router.send("hello", channel="group")
        router.send("hello", channel="private")

    def test_send_private_no_bot(self):
        """private bot 为 None → private/both 通道静默跳过。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)

        router.send("hello", channel="private")
        router.send("hello", channel="both")

        group.send.assert_called_once_with("hello")

    # ── alert — 指纹去重 ──

    def test_alert_fingerprint_dedup_within_window(self):
        """相同 fingerprint 在 fingerprint_rounds 内 → 抑制。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)
        router.new_round(1)

        sent1 = router.alert("msg", fingerprint="f1")
        sent2 = router.alert("msg", fingerprint="f1")

        assert sent1 is True
        assert sent2 is False
        group.send.assert_called_once()

    def test_alert_fingerprint_after_window(self):
        """相同 fingerprint 超过 fingerprint_rounds → 再次发送。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)
        router.new_round(1)
        router.alert("msg", fingerprint="f1", fingerprint_rounds=10)

        router.new_round(100)  # 超出冷却窗口
        sent = router.alert("msg", fingerprint="f1", fingerprint_rounds=10)

        assert sent is True
        assert group.send.call_count == 2

    def test_alert_fingerprint_different_fingerprints_no_dedup(self):
        """不同 fingerprint → 各自发送，不受影响。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)
        router.new_round(1)

        router.alert("msg1", fingerprint="f1")
        router.alert("msg2", fingerprint="f2")

        assert group.send.call_count == 2

    # ── alert — code 冷却 ──

    def test_alert_round_cooldown_suppresses(self):
        """同 round 内相同 code → round 冷却抑制。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)
        router.new_round(1)

        assert router.alert("a", code="000001", price=10.0, cooldown_rounds=5) is True
        # price 变了也抑制，因为 round 检查先触发
        assert router.alert("b", code="000001", price=20.0, cooldown_rounds=5) is False

        group.send.assert_called_once()

    def test_alert_price_cooldown_round_expired(self):
        """round 冷却过期但价格变化 <0.5% → 价格冷却抑制。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)
        router.new_round(1)
        router.alert("first", code="000001", price=10.0, cooldown_rounds=5)

        # round 冷却已过期 (10 - 1 = 9 >= 5)
        router.new_round(10)
        sent = router.alert("second", code="000001", price=10.03, cooldown_rounds=5)
        # 0.03/10.0 = 0.3% < 0.5% → 抑制

        assert sent is False
        group.send.assert_called_once()

    def test_alert_price_change_exceeds_sends(self):
        """round 冷却过期且价格变化 >0.5% → 再次发送。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)
        router.new_round(1)
        router.alert("first", code="000001", price=10.0, cooldown_rounds=5)

        router.new_round(10)  # round expired
        sent = router.alert("second", code="000001", price=10.10, cooldown_rounds=5)
        # 0.10/10.0 = 1% > 0.5% → 发送

        assert sent is True
        assert group.send.call_count == 2

    def test_alert_price_cooldown_uses_stored_price(self):
        """价格冷却是相对于上一次存储的价格，不是最新的尝试价。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)
        router.new_round(1)
        router.alert("first", code="000001", price=10.0, cooldown_rounds=5)

        router.new_round(10)
        # 尝试发送但被价格冷却抑制 → _cooldown 不更新
        router.alert("second", code="000001", price=10.03, cooldown_rounds=5)

        router.new_round(20)
        # 继续使用原价 10.0 对比，10.03 - 10.0 = 0.3% 仍然 < 0.5%
        sent = router.alert("third", code="000001", price=10.03, cooldown_rounds=5)
        assert sent is False
        group.send.assert_called_once()

    def test_alert_different_code_no_cooldown(self):
        """不同 code → 各自冷却互不影响。"""
        group = MagicMock()
        router = AlertRouter(group_bot=group)
        router.new_round(1)

        router.alert("a", code="000001", price=10.0, cooldown_rounds=5)
        router.alert("b", code="000002", price=20.0, cooldown_rounds=5)

        assert group.send.call_count == 2

    # ── alert — channel 参数 ──

    def test_alert_sends_to_group_by_default(self):
        """alert 默认发到 group 通道。"""
        group, private = MagicMock(), MagicMock()
        router = AlertRouter(group_bot=group, private_bot=private)
        router.new_round(1)

        router.alert("msg", fingerprint="f1")

        group.send.assert_called_once()
        private.send.assert_not_called()

    def test_alert_private_helper(self):
        """alert_private → 发送到 private 通道。"""
        private = MagicMock()
        router = AlertRouter(private_bot=private)
        router.new_round(1)

        sent = router.alert_private("secret", fingerprint="p1")

        assert sent is True
        private.send.assert_called_once_with("secret")

    # ── is_cooling ──

    def test_is_cooling_returns_true_when_active(self):
        """is_cooling 返回 True（code 在冷却期内）。"""
        router = AlertRouter()
        router.new_round(10)
        router.alert("msg", code="000001", price=10.0, cooldown_rounds=7)

        assert router.is_cooling("000001", rounds=7) is True

    def test_is_cooling_returns_false_when_expired(self):
        """is_cooling 返回 False（code 已过冷却期）。"""
        router = AlertRouter()
        router.new_round(10)
        router.alert("msg", code="000001", price=10.0, cooldown_rounds=5)

        router.new_round(20)  # 20 - 10 = 10 >= 5
        assert router.is_cooling("000001", rounds=5) is False

    def test_is_cooling_unknown_code(self):
        """不存在的 code → 返回 False。"""
        router = AlertRouter()
        router.new_round(10)
        router.alert("msg", code="000001", price=10.0)

        assert router.is_cooling("999999") is False

    def test_is_cooling_no_alert_sent(self):
        """从未发过告警 → 返回 False。"""
        router = AlertRouter()
        router.new_round(10)
        assert router.is_cooling("000001") is False
