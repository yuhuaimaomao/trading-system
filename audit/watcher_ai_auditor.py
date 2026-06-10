# -*- coding: utf-8 -*-
"""AIAuditor — AI 驱动的盯盘审计，串联因果、提炼模式、生成改进建议。"""

import json
import re
from datetime import datetime

from data._base import connect
from system.ai.prompts.watcher_audit import WATCHER_AUDIT_SYSTEM, WATCHER_AUDIT_USER


class AIAuditor:
    def __init__(self, repo, model: str = None):
        self.repo = repo
        from system.config.settings import AUDIT_AI_MODEL, DATABASE_PATH

        self.model = model or AUDIT_AI_MODEL
        self.db_path = str(DATABASE_PATH)
        self._ai = None

    @property
    def ai(self):
        if self._ai is None:
            from system.ai.ai_service import ai as ai_svc

            self._ai = ai_svc
        return self._ai

    def review(self, findings: list[dict], context: dict | None = None) -> dict:
        """审计管线统一接口 — 委托给 audit()。"""
        ctx = context or {}
        trade_date = ctx.get("date", datetime.now().strftime("%Y-%m-%d"))
        result = self.audit(trade_date)
        if result is None:
            return {"improvements": [], "lessons": []}
        return result

    def audit(self, trade_date: str) -> dict | None:
        prompt = self._build_prompt(trade_date)
        if prompt is None:
            return None

        text = self.ai.chat(prompt=prompt, model="audit", system_prompt=WATCHER_AUDIT_SYSTEM)
        if not text:
            return None

        return self._parse_response(text)

    def _build_prompt(self, trade_date: str) -> str | None:
        logs = self.repo.get_decision_logs(trade_date)
        if not logs:
            return None

        timeline_lines = []
        for log in logs:
            data = json.loads(log["decision_data"]) if isinstance(log["decision_data"], str) else log["decision_data"]
            code = log.get("stock_code") or "-"
            data_str = json.dumps(data, ensure_ascii=False)[:200]
            timeline_lines.append(f"[{log['ts']}] {log['decision_type']} {code} | {data_str}")
        decision_timeline = "\n".join(timeline_lines)

        findings = self.repo.get_audit_findings(trade_date)
        sev_emoji = {"P0": "🚨", "P1": "⚠️", "P2": "📝", "P3": "💡"}
        finding_lines = []
        for f in findings:
            finding_lines.append(f"{sev_emoji.get(f['severity'], '')} [{f['severity']}] {f['pattern_desc']}")
        rule_findings = "\n".join(finding_lines) if finding_lines else "无 P0/P1 发现"

        market_structure = self._build_market_structure(trade_date)

        lessons = self.repo.get_active_watcher_lessons()
        lesson_lines = []
        for lesson in lessons[:20]:
            lesson_lines.append(
                f"[{lesson['lesson_type']}] ({lesson['occurrence_count']}次) {lesson['lesson_content']}"
            )
        historical_lessons = "\n".join(lesson_lines) if lesson_lines else "无历史教训"

        current_params = self._get_current_params()

        return WATCHER_AUDIT_USER.format(
            decision_timeline=decision_timeline,
            rule_findings=rule_findings,
            market_structure=market_structure,
            historical_lessons=historical_lessons,
            current_params=current_params,
        )

    def _build_market_structure(self, trade_date: str) -> str:
        conn = connect(self.db_path)
        rows = conn.execute(
            "SELECT ts, sector_name, avg_change FROM sector_snapshots WHERE trade_date=? ORDER BY ts",
            (trade_date,),
        ).fetchall()
        conn.close()
        if not rows:
            return "无板块数据"
        sampled = {}
        for ts, name, chg in rows:
            hour = ts[:13] if isinstance(ts, str) else datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%dT%H")
            key = (hour, name)
            if key not in sampled:
                sampled[key] = chg
        return "\n".join(f"{h} {n}: {c:+.2f}%" for (h, n), c in sorted(sampled.items())[:50])

    def _get_current_params(self) -> str:
        from system.config import settings

        params = []
        for name in dir(settings):
            if name.isupper() and not name.startswith("_"):
                val = getattr(settings, name)
                if isinstance(val, (int, float, str, bool)):
                    params.append(f"{name}={val}")
        return "\n".join(sorted(params)[:40])

    def _parse_response(self, text: str) -> dict | None:
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

    def run_and_save(self, trade_date: str) -> dict | None:
        result = self.audit(trade_date)
        if result is None:
            return None
        for imp in result.get("improvements", []):
            self.repo.insert_watcher_improvement(
                {
                    "trade_date": trade_date,
                    "improvement_type": imp.get("type", "rule_add"),
                    "target_module": imp.get("target_module", ""),
                    "target_param": imp.get("target_method", ""),
                    "suggested_change": imp.get("suggested_change", ""),
                    "code_diff": imp.get("code_diff", ""),
                    "rationale": imp.get("rationale", ""),
                    "evidence_ids": "[]",
                }
            )
        for lesson in result.get("lessons", []):
            self.repo.upsert_watcher_lesson(
                lesson_type=lesson.get("type", "unknown"),
                lesson_key=lesson.get("key", ""),
                lesson_content=lesson.get("content", ""),
                trigger_conditions=lesson.get("trigger_conditions"),
                trade_date=trade_date,
            )
        return result
