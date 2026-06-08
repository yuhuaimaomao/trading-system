"""Telegram 消息推送工具 — 通过 Bot API 直接发送"""

import os
import time

import requests

from system.utils.logger import get_message_logger

logger = get_message_logger("sender")

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"

_proxy_url = os.environ.get("TELEGRAM_PROXY", "http://127.0.0.1:1082")
_proxy_url = _proxy_url.strip() if _proxy_url else ""
TELEGRAM_PROXIES = {"http": _proxy_url, "https": _proxy_url} if _proxy_url else None

# Telegram 对同一 chat 限制 ~20条/分钟，保守设最小间隔 1.5 秒
# 实际发送中片段(chunk)也算独立请求，所以需要节流
_MIN_SEND_INTERVAL = 1.5  # 秒
# 遇到 429 后的退避时间
_RATE_LIMIT_BACKOFF = 15  # 秒


class MessageSender:
    """Telegram 消息发送器"""

    def __init__(self, chat_id: str = None, bot_token: str = None):
        from system.config.settings import (
            TELEGRAM_CHAT_ID,
            TELEGRAM_REPORT_BOT_TOKEN,
            TELEGRAM_REPORT_CHAT_ID,
        )

        self.chat_id = chat_id or TELEGRAM_REPORT_CHAT_ID or TELEGRAM_CHAT_ID
        self.bot_token = bot_token or TELEGRAM_REPORT_BOT_TOKEN

        if not self.bot_token:
            raise ValueError("TELEGRAM_REPORT_BOT_TOKEN 未配置，请在 .env 文件中设置")

        self._last_send_time: float = 0.0
        # 全局退避截止时间（遇到 429 后所有发送暂停到该时间点）
        self._backoff_until: float = 0.0

        logger.info("Telegram Bot 已初始化")

    def send(self, message: str):
        try:
            url = TELEGRAM_API_URL.format(token=self.bot_token)

            max_len = 4000
            chunks = [message[i : i + max_len] for i in range(0, len(message), max_len)]

            for i, chunk_text in enumerate(chunks):
                # 全局退避检查
                now = time.time()
                if now < self._backoff_until:
                    wait = self._backoff_until - now
                    logger.info(f"发送节流等待 {wait:.1f}s（429退避）")
                    time.sleep(wait)

                # 最小间隔保证（每条消息包含的 chunk 都算一次请求）
                elapsed = time.time() - self._last_send_time
                if elapsed < _MIN_SEND_INTERVAL:
                    time.sleep(_MIN_SEND_INTERVAL - elapsed)

                escaped_chunk = (
                    chunk_text.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                payload = {
                    "chat_id": self.chat_id,
                    "text": escaped_chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }

                if i > 0 and hasattr(self, "_last_message_id"):
                    payload["reply_to_message_id"] = self._last_message_id

                resp = requests.post(
                    url,
                    json=payload,
                    timeout=30,
                    proxies=TELEGRAM_PROXIES,
                )
                self._last_send_time = time.time()

                # 429 退避：暂停后续发送
                if resp.status_code == 429:
                    self._backoff_until = time.time() + _RATE_LIMIT_BACKOFF
                    logger.warning(
                        f"触发 Telegram 频率限制，暂停发送 {_RATE_LIMIT_BACKOFF}s"
                    )
                    return  # 本轮剩余 fragment 也不发了，下轮再试

                resp.raise_for_status()

                result = resp.json()
                if result.get("ok"):
                    self._last_message_id = result["result"]["message_id"]
                    logger.info(f"发送成功 (片段 {i + 1}/{len(chunks)})")
                else:
                    logger.error(f"Telegram API 返回错误: {result}")

        except Exception as e:
            logger.error(f"发送异常：{e}")
