# -*- coding: utf-8 -*-
"""模拟 Telegram — 捕获所有推送消息 + 模拟用户回复。"""


class SimTelegram:
    """消息捕获器。"""

    def __init__(self):
        self.messages: list[str] = []
        self.private_messages: list[str] = []
        self._reply_queue: list[str] = []  # 模拟用户回复

    def send(self, msg: str):
        self.messages.append(msg)

    def send_private(self, msg: str):
        self.private_messages.append(msg)

    def queue_reply(self, text: str):
        """模拟用户回复消息。"""
        self._reply_queue.append(text)

    def fetch_updates(self, timeout: int = 10) -> list[dict]:
        """模拟 getUpdates，返回队列中的回复。"""
        updates = []
        while self._reply_queue:
            text = self._reply_queue.pop(0)
            updates.append({
                "chat_id": "123456",
                "user": "test_user",
                "username": "testuser",
                "text": text,
                "message_id": str(len(updates) + 1),
                "ts": 0,
            })
        return updates

    def reset(self):
        self.messages.clear()
        self.private_messages.clear()
        self._reply_queue.clear()

    def count_containing(self, text: str) -> int:
        return sum(1 for m in self.messages if text in m)

    def all_text(self) -> str:
        return "\n---\n".join(self.messages)

    def private_text(self) -> str:
        return "\n---\n".join(self.private_messages)
