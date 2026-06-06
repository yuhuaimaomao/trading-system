"""AI 异步调用队列 — 委托给 infra.ai.ai_service，保持向后兼容。"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AIQueue:
    """后台 AI 调用队列 — 委托给全局 ai_service。

    用法不变：
        ai_queue = AIQueue()
        ai_queue.start()
        ai_queue.submit("chase:000001", prompt, max_tokens=100)
        result = ai_queue.pop_result("chase:000001")
    """

    def __init__(self):
        self._started = False

    def start(self):
        if self._started:
            return
        from system.ai import ai

        ai.start_worker()
        self._started = True

    def stop(self):
        from system.ai import ai

        ai.stop_worker()
        self._started = False

    def submit(
        self,
        key: str,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 100,
        dedupe: bool = True,
    ) -> bool:
        from system.ai import ai

        return ai.submit(
            key,
            prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            model="watcher",
        )

    def pop_result(self, key: str) -> Optional[str]:
        from system.ai import ai

        return ai.pop(key)

    def peek_result(self, key: str) -> Optional[str]:
        return None

    def has_pending(self, key: str) -> bool:
        from system.ai import ai

        return ai.pending(key)

    @property
    def pending_count(self) -> int:
        from system.ai import ai

        return ai.qsize
