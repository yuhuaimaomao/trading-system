"""审计基类 — BaseRuleAuditor (自动收集 check_* 方法) + BaseAIAuditor (AI 审查)。

所有审计器继承这两个基类，确保统一的 audit/review 接口。
"""

import json
import re

from system.utils.logger import get_audit_logger

logger = get_audit_logger("pipeline")


class BaseRuleAuditor:
    """规则审计基类。自动收集所有 check_* 方法，audit() 聚合执行。"""

    def __init__(self, repo=None, db_path: str = None):
        self.repo = repo
        self.db_path = db_path

    def audit(self, date: str) -> list[dict]:
        """扫描所有 check_* 方法并执行，聚合返回 findings 列表。"""
        findings = []
        for name in sorted(dir(self)):
            if not name.startswith("check_"):
                continue
            method = getattr(self, name)
            if not callable(method):
                continue
            try:
                result = method(date)
                if result:
                    findings.extend(result)
            except Exception:
                logger.warning(f"审计方法 {name} 异常，已跳过", exc_info=True)
        return findings


class BaseAIAuditor:
    """AI 审计基类。子类覆写 _build_prompt / _system_prompt，调用 review() 触发 AI 审查。"""

    def __init__(self, repo=None, db_path: str = None):
        self.repo = repo
        self.db_path = db_path

    def review(self, findings: list[dict], context: dict | None = None) -> dict:
        """调用 AI 审查规则审计发现，返回结构化结果。"""
        ctx = context or {}
        prompt = self._build_prompt(findings, ctx)
        if not prompt:
            return {}

        system = self._system_prompt()
        try:
            from system.ai.ai_service import ai

            text = ai.chat(
                prompt=prompt,
                model="audit",
                system_prompt=system or "你是审计分析师。",
            )
            if text:
                return self._parse(None, text)
        except Exception:
            logger.warning("AI 审计调用异常", exc_info=True)

        return {}

    def _build_prompt(self, findings: list[dict], context: dict) -> str:
        """子类覆写 — 构建发送给 AI 的 prompt。"""
        return ""

    def _system_prompt(self) -> str:
        """子类覆写 — 返回 system prompt。"""
        return ""

    @staticmethod
    def _parse(_self, raw: str) -> dict:
        """从 AI 回复中提取 JSON，容错多种格式。

        _self 参数用于兼容实例方法调用（可为 None）。
        """
        default = {"improvements": [], "lessons": []}

        # 优先匹配 ```json ... ``` 代码块
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        json_str = json_match.group(1) if json_match else raw.strip()

        try:
            return json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            pass

        # 回退：提取第一个 { 到最后一个 }
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                pass

        logger.warning(f"审计 AI 返回无法解析: {raw[:200]}")
        return default
