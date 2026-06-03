"""AI 异步调用队列 — 后台线程处理 AI 请求，不阻塞主扫描循环。"""

import logging
import queue
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class _AITask:
    """单个 AI 任务。"""

    __slots__ = ("key", "prompt", "system_prompt", "max_tokens", "submitted_at")
    key: str
    prompt: str
    system_prompt: str
    max_tokens: int
    submitted_at: float

    def __init__(
        self, key: str, prompt: str, system_prompt: str = "", max_tokens: int = 100
    ):
        self.key = key
        self.prompt = prompt
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.submitted_at = time.time()


class AIQueue:
    """后台单线程 AI 调用队列。

    用法：
        ai_queue = AIQueue()
        ai_queue.start()

        # 提交任务（非阻塞）
        ai_queue.submit("chase:000001", prompt, max_tokens=100)

        # 下一轮扫描取结果
        result = ai_queue.pop_result("chase:000001")  # None = 尚未完成
    """

    MAX_QUEUE = 30  # 队列容量上限，超出丢弃最旧任务

    def __init__(self):
        self._q: queue.Queue = queue.Queue(maxsize=self.MAX_QUEUE)
        self._results: dict[str, str] = {}  # key → AI 返回文本
        self._results_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._running = False
        self._ai = None  # 延迟初始化，线程内创建

    # ---- public API ----

    def start(self):
        """启动后台工作线程。"""
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(
            target=self._work, daemon=True, name="ai-worker"
        )
        self._worker.start()
        logger.info("AI 后台队列已启动")

    def stop(self):
        """停止后台线程。"""
        self._running = False
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)

    def submit(
        self,
        key: str,
        prompt: str,
        system_prompt: str = "",
        max_tokens: int = 100,
        dedupe: bool = True,
    ) -> bool:
        """提交 AI 任务到后台队列。返回 True 表示已入队。

        dedupe=True 时，同名 key 的旧任务会被替换（结果会被覆盖）。
        """
        if not self._running:
            return False

        task = _AITask(key, prompt, system_prompt, max_tokens)

        try:
            self._q.put_nowait(task)
        except queue.Full:
            # 队列满 → 丢弃最旧任务
            try:
                self._q.get_nowait()
                self._q.task_done()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(task)
            except queue.Full:
                return False

        return True

    def pop_result(self, key: str) -> Optional[str]:
        """取出已完成任务的结果。取后即删，防止重复处理。"""
        with self._results_lock:
            return self._results.pop(key, None)

    def peek_result(self, key: str) -> Optional[str]:
        """查看结果但不删除。"""
        with self._results_lock:
            return self._results.get(key)

    def has_pending(self, key: str) -> bool:
        """检查是否有待处理的任务（在队列中或正在执行中）。"""
        with self._results_lock:
            if key in self._results:
                return False  # 已完成
        # 检查队列中是否有同名任务
        for task in list(self._q.queue):
            if task.key == key:
                return True
        return False

    @property
    def pending_count(self) -> int:
        return self._q.qsize()

    # ---- 内部 ----

    def _work(self):
        """后台工作线程主循环。"""
        # 线程内创建 AIAnalyzer 实例（requests.Session 非线程安全，每线程独立）
        try:
            from analysis.review.analyzer import AIAnalyzer

            self._ai = AIAnalyzer()
        except Exception as e:
            logger.error(f"AI 后台线程初始化失败: {e}")
            return

        while self._running:
            try:
                task = self._q.get(timeout=1)
            except queue.Empty:
                continue

            try:
                result = self._ai._call_ai(
                    task.prompt,
                    system_prompt=task.system_prompt
                    or "你是一个专业的 A 股量化分析师。",
                    max_tokens=task.max_tokens,
                )
                text = result.strip() if result else ""
                elapsed = time.time() - task.submitted_at
                if text:
                    logger.info(f"AI[{task.key}] 完成 ({elapsed:.1f}s, {len(text)}字)")
                else:
                    logger.warning(f"AI[{task.key}] 返回空 ({elapsed:.1f}s)")
                with self._results_lock:
                    self._results[task.key] = text
            except Exception as e:
                logger.error(f"AI[{task.key}] 异常: {e}")
                with self._results_lock:
                    self._results[task.key] = ""
            finally:
                self._q.task_done()
