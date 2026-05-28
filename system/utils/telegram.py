# -*- coding: utf-8 -*-
"""Telegram 消息推送工具 — 通过 Bot API 直接发送"""

import os
from datetime import datetime

import requests
import urllib3

from system.utils.logger import get_system_logger

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = get_system_logger('telegram_bot')

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"

_proxy_url = os.environ.get("TELEGRAM_PROXY", "http://127.0.0.1:1082")
_proxy_url = _proxy_url.strip() if _proxy_url else ""
TELEGRAM_PROXIES = {"http": _proxy_url, "https": _proxy_url} if _proxy_url else None


class MessageSender:
    """Telegram 消息发送器"""

    def __init__(self, chat_id: str = None, bot_token: str = None):
        from system.config.settings import TELEGRAM_CHAT_ID, TELEGRAM_REPORT_CHAT_ID, TELEGRAM_REPORT_BOT_TOKEN

        self.chat_id = chat_id or TELEGRAM_REPORT_CHAT_ID or TELEGRAM_CHAT_ID
        self.bot_token = bot_token or TELEGRAM_REPORT_BOT_TOKEN

        if not self.bot_token:
            raise ValueError("TELEGRAM_REPORT_BOT_TOKEN 未配置，请在 .env 文件中设置")

        logger.info(f"使用 Bot 模式（token: {self.bot_token[:8]}...）")

    def send(self, message: str):
        try:
            url = TELEGRAM_API_URL.format(token=self.bot_token)

            max_len = 4000
            chunks = [message[i:i+max_len] for i in range(0, len(message), max_len)]

            for i, chunk_text in enumerate(chunks):
                escaped_chunk = chunk_text.replace('<', '&lt;').replace('>', '&gt;')
                payload = {
                    'chat_id': self.chat_id,
                    'text': escaped_chunk,
                    'parse_mode': 'HTML',
                    'disable_web_page_preview': True,
                }

                if i > 0 and hasattr(self, '_last_message_id'):
                    payload['reply_to_message_id'] = self._last_message_id

                resp = requests.post(url, json=payload, timeout=30, verify=False, proxies=TELEGRAM_PROXIES)
                resp.raise_for_status()

                result = resp.json()
                if result.get('ok'):
                    self._last_message_id = result['result']['message_id']
                    logger.info(f"发送成功 (片段 {i+1}/{len(chunks)})")
                else:
                    logger.error(f"Telegram API 返回错误: {result}")

        except Exception as e:
            logger.error(f"发送异常：{e}")


class MessageReceiver:
    """Telegram 消息接收器 — getUpdates 长轮询。

    用法:
        receiver = MessageReceiver()
        updates = receiver.fetch_updates()  # 返回新消息列表
    """

    def __init__(self, bot_token: str = None):
        from system.config.settings import TELEGRAM_REPORT_BOT_TOKEN, PROJECT_ROOT

        self.bot_token = bot_token or TELEGRAM_REPORT_BOT_TOKEN
        if not self.bot_token:
            raise ValueError("TELEGRAM_REPORT_BOT_TOKEN 未配置")
        self._state_file = str(PROJECT_ROOT / "storage" / "telegram_last_update_id.txt")
        self._last_update_id: int = self._load_last_update_id()

    def _load_last_update_id(self) -> int:
        try:
            with open(self._state_file) as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return 0

    def _save_last_update_id(self):
        try:
            with open(self._state_file, "w") as f:
                f.write(str(self._last_update_id))
        except OSError:
            pass

    def fetch_updates(self, timeout: int = 10) -> list[dict]:
        """获取新消息。timeout 秒长轮询。

        Returns:
            [{chat_id, user, username, text, message_id, ts}]
        """
        try:
            url = TELEGRAM_UPDATES_URL.format(token=self.bot_token)
            params = {
                "offset": self._last_update_id + 1,
                "timeout": timeout,
                "allowed_updates": ["message"],
            }
            resp = requests.get(url, params=params, timeout=timeout + 10, verify=False, proxies=TELEGRAM_PROXIES)
            resp.raise_for_status()
            data = resp.json()

            if not data.get("ok"):
                logger.error(f"getUpdates API 返回错误: {data}")
                return []

            updates = data.get("result", [])
            if not updates:
                return []

            messages: list[dict] = []
            for upd in updates:
                self._last_update_id = upd["update_id"]
                msg = upd.get("message", {})
                text = msg.get("text", "")
                if not text:
                    continue

                from_user = msg.get("from", {})
                messages.append({
                    "chat_id": str(msg.get("chat", {}).get("id", "")),
                    "user": from_user.get("first_name", ""),
                    "username": from_user.get("username", ""),
                    "text": text.strip(),
                    "message_id": str(msg.get("message_id", "")),
                    "ts": msg.get("date", 0),
                })

            self._save_last_update_id()
            return messages

        except Exception as e:
            logger.warning(f"getUpdates 异常: {e}")
            return []
