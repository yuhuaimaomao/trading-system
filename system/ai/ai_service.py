"""统一 AI 服务 — 多模型 + 异步队列 + FC 工具调用。

用法:
    from system.ai import ai
    ai.chat(prompt, model="screening", system_prompt="你是...")
    ai.submit("key", prompt, model="watcher", system_prompt="你是...")

模型通过环境变量配置，不在代码里硬编码:
    .env: AI_MODEL=deepseek-v4-pro  AI_MODEL_REVIEW=qwen3.7-plus  AI_MODEL_MORNING=qwen3.7-plus
"""

import os
import queue
import threading
import time
from typing import Optional

import requests

from system.ai.function_calling import FunctionCallingEngine
from system.ai.stock_tools import TOOLS_DEFINITION
from system.config import settings
from system.utils.logger import get_system_logger

logger = get_system_logger("ai")

_RETRYABLE = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
_MAX_RETRIES = 2
_RETRY_BACKOFF = 5

QWEN_PREFIXES = ("qwen", "qwq", "qvq")
DEEPSEEK_PREFIXES = ("deepseek",)

# 业务 → 环境变量映射（纯配置，不含模型名）
_MODEL_ENV_MAP = {
    "review": "AI_MODEL_REVIEW",
    "screening": "AI_MODEL_SCREENING",
    "strategy": "AI_MODEL_SCREENING",
    "morning": "AI_MODEL_MORNING",
    "watcher": "AI_MODEL_WATCHER",
    "watcher_chase": "AI_MODEL_WATCHER",
    "watcher_swap": "AI_MODEL_WATCHER",
    "watcher_index": "AI_MODEL_WATCHER",
    "watcher_trapped": "AI_MODEL_WATCHER",
    "watcher_breakout": "AI_MODEL_WATCHER",
    "audit": "AI_MODEL_AUDIT",
}


def _resolve_model(business: str = "") -> str:
    """纯配置驱动：业务名 → 环境变量 → 模型名。"""
    if business:
        env_key = _MODEL_ENV_MAP.get(business, "")
        if env_key:
            model = os.environ.get(env_key, "")
            if model:
                return model
    # 回退到全局 AI_MODEL
    return os.environ.get("AI_MODEL", "")


def _resolve_provider(model: str) -> tuple[str, str, str]:
    """provider, api_key, endpoint"""
    provider = settings.AI_PROVIDER
    model_lower = model.lower() if model else ""
    if not provider or provider == "auto":
        if any(model_lower.startswith(p) for p in QWEN_PREFIXES):
            provider = "dashscope"
        elif any(model_lower.startswith(p) for p in DEEPSEEK_PREFIXES):
            provider = "deepseek"
        else:
            provider = "dashscope"
    if provider == "deepseek":
        return provider, os.getenv("DEEPSEEK_API_KEY", ""), settings.DEEPSEEK_ENDPOINT
    return provider, os.getenv("DASHSCOPE_API_KEY", ""), settings.DASHSCOPE_ENDPOINT


class AIService:
    """全局 AI 服务。"""

    def __init__(self):
        self._session = requests.Session()
        self._fc = FunctionCallingEngine()
        self._q: queue.Queue = queue.Queue(maxsize=30)
        self._results: dict[str, str] = {}
        self._lock = threading.Lock()
        self._running = False
        self._worker: Optional[threading.Thread] = None

    # ═══ 同步 ═══

    def chat(
        self,
        prompt: str,
        *,
        model: str = "",
        system_prompt: str = "",
        max_tokens: int = None,
        temperature: float = 0.6,
    ) -> str:
        """同步单次调用。max_tokens 不传则由 API 自行决定。"""
        task = model or "unknown"
        return self._call(
            prompt,
            _resolve_model(model),
            task=task,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def chat_with_tools_raw(
        self,
        messages: list[dict],
        *,
        model: str = "",
        tools: list[dict] = None,
        tool_choice: str = "auto",
        max_tokens: int = None,
    ) -> dict:
        """FC 单轮调用，返回 {'content': str, 'tool_calls': list}。"""
        if tools is None:
            tools = TOOLS_DEFINITION
        payload = self._build_payload(
            _resolve_model(model),
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
        )
        data = self._request(_resolve_model(model), payload)
        msg = data["choices"][0].get("message", {})
        return {
            "content": msg.get("content", "") or "",
            "tool_calls": msg.get("tool_calls", []),
        }

    def chat_with_tools(
        self,
        messages: list[dict],
        *,
        model: str = "",
        tools: list[dict] = None,
        tool_choice: str = "auto",
        max_tokens: int = None,
        max_rounds: int = 4,
    ) -> str:
        """FC 多轮对话，返回最终文本。"""
        if tools is None:
            tools = TOOLS_DEFINITION
        msgs = list(messages)
        for _ in range(max_rounds):
            payload = self._build_payload(
                _resolve_model(model),
                messages=msgs,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
            )
            data = self._request(_resolve_model(model), payload)
            choice = data["choices"][0]
            msg = choice.get("message", {})
            content = msg.get("content", "") or ""
            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                return content
            msgs.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            msgs.extend(self._fc.process_tool_calls(tool_calls))
        return content or ""

    # ═══ 异步 ═══

    def start_worker(self):
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._work, daemon=True, name="ai-worker")
        self._worker.start()

    def stop_worker(self):
        self._running = False
        if self._worker:
            self._worker.join(timeout=5)

    def submit(
        self,
        key: str,
        prompt: str,
        *,
        model: str = "",
        system_prompt: str = "",
        max_tokens: int = None,
    ) -> bool:
        if not self._running:
            return False
        task = (key, prompt, model, system_prompt, max_tokens)
        try:
            self._q.put_nowait(task)
        except queue.Full:
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

    def pop(self, key: str) -> Optional[str]:
        with self._lock:
            return self._results.pop(key, None)

    def pending(self, key: str) -> bool:
        with self._lock:
            if key in self._results:
                return False
        return any(k == key for k, *_ in list(self._q.queue))

    @property
    def qsize(self) -> int:
        return self._q.qsize()

    # ═══ 内部 ═══

    def _call(
        self,
        prompt: str,
        model_name: str,
        *,
        task: str,
        system_prompt: str,
        max_tokens: int = None,
        temperature: float = 0.6,
    ) -> str:
        from datetime import datetime
        from pathlib import Path

        from system.config.settings import PROJECT_ROOT

        payload = self._build_payload(
            model_name,
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        date_str = datetime.now().strftime("%Y-%m-%d")

        # 落盘 prompt
        try:
            prompt_dir = Path(PROJECT_ROOT) / "storage" / "prompts"
            prompt_dir.mkdir(parents=True, exist_ok=True)
            prompt_path = prompt_dir / f"{task}_prompt_{date_str}.txt"
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write(f"任务: {task}\n")
                f.write(f"模型: {model_name}\n")
                f.write(f"日期: {date_str}\n")
                f.write(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
                f.write(f"[system] {system_prompt}\n\n")
                f.write(f"[user] {prompt}\n")
        except Exception:
            pass

        data = self._request(model_name, payload)
        content = data["choices"][0].get("message", {}).get("content", "")
        text = (content or "").strip()

        # 落盘 response
        try:
            report_dir = Path(PROJECT_ROOT) / "storage" / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            report_path = report_dir / f"{task}_ai_response_{date_str}_{model_name}.txt"
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(f"任务: {task}\n")
                f.write(f"模型: {model_name}\n")
                f.write(f"日期: {date_str}\n")
                f.write(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write("=" * 60 + "\n\n")
                f.write(text)
        except Exception:
            pass

        return text

    @staticmethod
    def _build_payload(
        model_name: str,
        *,
        messages=None,
        prompt=None,
        system_prompt: str = "",
        max_tokens: int = None,
        temperature: float = 0.6,
        tools=None,
        tool_choice=None,
    ):
        msgs = [{"role": "system", "content": system_prompt}]
        if messages:
            msgs.extend(messages)
        elif prompt:
            msgs.append({"role": "user", "content": prompt})
        payload = {
            "model": model_name,
            "messages": msgs,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        return payload

    def _request(
        self,
        model_name: str,
        payload: dict,
        read_timeout: int = 600,
        connect_timeout: int = 30,
    ) -> dict:
        _, api_key, endpoint = _resolve_provider(model_name)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = (connect_timeout, read_timeout)
        last_error = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.post(endpoint, json=payload, headers=headers, timeout=timeout)
                if resp.status_code >= 500:
                    raise requests.exceptions.HTTPError(f"服务端错误 {resp.status_code}", response=resp)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                if e.response is not None and 400 <= e.response.status_code < 500:
                    raise
                last_error = e
            except _RETRYABLE as e:
                last_error = e
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF * (2 ** (attempt - 1)))
        raise last_error

    def _work(self):
        while self._running:
            try:
                key, prompt, model, sys_prompt, max_tok = self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                result = self.chat(
                    prompt,
                    model=model or "watcher",
                    system_prompt=sys_prompt,
                    max_tokens=max_tok,
                )
                with self._lock:
                    self._results[key] = result
            except Exception as e:
                logger.error(f"AI异步[{key}]: {e}")
                with self._lock:
                    self._results[key] = ""
            finally:
                self._q.task_done()


ai = AIService()
