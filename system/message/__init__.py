"""系统消息子系统 — 传输 + 接收 + 路由。

三层架构:
- sender.py:   MessageSender — 发送消息到 Telegram
- receiver.py: MessageReceiver — getUpdates 接收用户回复
- router.py:   AlertRouter — 带去重/冷却的高频告警路由
"""

__all__ = ["MessageReceiver", "AlertRouter", "MessageSender"]

from system.message.receiver import MessageReceiver
from system.message.router import AlertRouter
from system.message.sender import MessageSender
