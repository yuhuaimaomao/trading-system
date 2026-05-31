# -*- coding: utf-8 -*-
"""AI 审计引擎 — 审查策略 AI 推理质量"""

import json
import os
import re
from typing import Optional
from analysis.audit.prompts import STRATEGY_AUDIT_PROMPT
from data.repo import TradeRepository
from system.utils.logger import get_system_logger

logger = get_system_logger("ai_auditor")


class AIAuditor:
    """审查策略 AI 的推理质量，发现偏见/遗漏/改进点"""

    def __init__(self, db_path: str = None):
        self.repo = TradeRepository(db_path=db_path)

    def audit(self, push_date: str, rule_findings: list[dict]) -> dict:
        """执行 AI 审计，返回结构化审计结果"""
        decisions = self.repo.get_ai_decisions(push_date)
        if not decisions:
            logger.warning(f"无 AI 决策记录: {push_date}")
            return {}

        funnel = self.repo.get_funnel_records(push_date)
        lessons = self.repo.get_active_lessons()

        prompt = self._build_prompt(push_date, decisions, funnel, rule_findings, lessons)

        response_text = self._call_ai(prompt)
        if not response_text:
            logger.error("审计 AI 调用失败")
            return {}

        result = self._parse_response(response_text)
        if not result:
            return {}

        self._save_results(push_date, result)
        return result

    # ----------------------------------------------------------------
    # Prompt 构建
    # ----------------------------------------------------------------

    def _build_prompt(
        self, push_date: str, decisions: list[dict],
        funnel: list[dict], rule_findings: list[dict],
        lessons: list[dict],
    ) -> str:
        decisions_text = []
        for d in decisions:
            verdict_emoji = "✅" if d.get("verdict") == "buy" else "❌"
            decisions_text.append(
                f"{verdict_emoji} {d['stock_code']} {d.get('stock_name','')} "
                f"({d.get('verdict','')}, conf={d.get('confidence','')})"
            )
            if d.get("what_i_see"):
                decisions_text.append(f"  看到: {d['what_i_see'][:200]}")
            if d.get("what_concerns_me"):
                decisions_text.append(f"  担忧: {d['what_concerns_me'][:200]}")
            if d.get("decisive_factor"):
                decisions_text.append(f"  关键: {d['decisive_factor'][:200]}")
            if d.get("skip_reason"):
                decisions_text.append(f"  跳过原因: {d['skip_reason'][:200]}")
            if d.get("would_reconsider_if"):
                decisions_text.append(f"  重新考虑条件: {d['would_reconsider_if'][:200]}")

        outcomes_text = []
        for d in decisions:
            chg = d.get("day_change_pct")
            if chg is not None:
                emoji = "🟢" if chg > 0 else "🔴"
                outcomes_text.append(
                    f"{emoji} {d['stock_code']} {d.get('stock_name','')}: {chg:+.1f}%"
                )

        rule_text = json.dumps(rule_findings, ensure_ascii=False, indent=2) if rule_findings else "无"

        lessons_text = ""
        for l in lessons[-10:]:
            lessons_text += f"- [{l['lesson_type']}] {l['lesson_content'][:100]}\n"

        return STRATEGY_AUDIT_PROMPT.format(
            push_date=push_date,
            market_context="（从复盘上下文提取）",
            holdings_context="（从持仓快照提取）",
            ai_decisions="\n".join(decisions_text),
            actual_outcomes="\n".join(outcomes_text),
            rule_findings=rule_text,
            historical_lessons=lessons_text or "无历史教训",
        )

    # ----------------------------------------------------------------
    # AI 调用
    # ----------------------------------------------------------------

    def _call_ai(self, prompt: str) -> Optional[str]:
        from analysis.review.analyzer import AIAnalyzer

        if not os.getenv("DASHSCOPE_API_KEY"):
            logger.warning("DASHSCOPE_API_KEY 未配置，跳过 AI 审计")
            return None

        try:
            analyzer = AIAnalyzer()
            analyzer.model = "qwen3.6-plus"
            text = analyzer._call_ai(
                prompt=prompt,
                system_prompt="你是策略审计分析师。严格按 JSON 格式输出（用```json包裹），不要额外解释。",
                max_tokens=4096,
            )
            return text
        except Exception as e:
            logger.error(f"审计 AI 调用异常: {e}")
            return None

    # ----------------------------------------------------------------
    # 解析
    # ----------------------------------------------------------------

    def _parse_response(self, text: str) -> dict:
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        json_str = json_match.group(1) if json_match else text.strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            start = json_str.find("{")
            end = json_str.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(json_str[start:end + 1])
                except json.JSONDecodeError:
                    pass
            logger.error(f"审计结果 JSON 解析失败: {text[:200]}")
            return {}

    # ----------------------------------------------------------------
    # 入库
    # ----------------------------------------------------------------

    def _save_results(self, push_date: str, result: dict):
        from datetime import date

        today = date.today().isoformat()

        for lesson in result.get("lessons", []):
            self.repo.upsert_lesson({
                "lesson_type": lesson.get("type", ""),
                "lesson_key": lesson.get("key", ""),
                "lesson_content": lesson.get("content", ""),
                "trigger_conditions": json.dumps(lesson.get("trigger_conditions", {}), ensure_ascii=False),
                "first_date": today,
                "last_date": today,
            })

        for imp in result.get("improvements", []):
            self.repo.insert_improvement({
                "push_date": push_date,
                "improvement_type": imp.get("type", ""),
                "target_module": imp.get("target", ""),
                "target_param": None,
                "suggested_change": imp.get("suggested_change", ""),
                "code_diff": None,
                "rationale": imp.get("rationale", ""),
                "evidence_ids": json.dumps([]),
                "status": "pending",
            })

        logger.info(
            f"审计结果已入库: {len(result.get('lessons', []))} 条教训, "
            f"{len(result.get('improvements', []))} 条改进建议"
        )
