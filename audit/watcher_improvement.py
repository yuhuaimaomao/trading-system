# -*- coding: utf-8 -*-
"""改进建议应用器 — 格式化和推送改进卡片。"""

import logging
from datetime import date

logger = logging.getLogger(__name__)

TYPE_LABELS = {
    "param_tune": "参数调优",
    "rule_add": "新增规则",
    "rule_modify": "修改规则",
    "watch_add": "新增盯盘维度",
}


def format_improvement_card(imp: dict) -> str:
    lines = [
        f"🔧 盯盘改进建议 #{imp['id']}",
        "   ─────────────────────────",
        f"   类型: {TYPE_LABELS.get(imp['improvement_type'], imp['improvement_type'])}",
        f"   模块: {imp['target_module']}",
    ]
    if imp.get("target_param"):
        lines.append(f"   参数: {imp['target_param']}")

    lines += [
        "",
        f"   建议: {imp['suggested_change']}",
        "",
        f"   理由: {imp['rationale']}",
    ]

    if imp.get("code_diff"):
        lines += ["", "   ```diff", imp["code_diff"], "   ```"]

    return "\n".join(lines)


class ImprovementApplier:
    def __init__(self, repo):
        self.repo = repo

    def apply(self, imp_id: int) -> str:
        imp = self._get_improvement(imp_id)
        if imp is None:
            return f"未找到改进 #{imp_id}"

        today = date.today().isoformat()
        self.repo.update_watcher_improvement_status(imp["id"], "applied", today)

        diff = imp.get("code_diff", "")
        if diff:
            return f"改进 #{imp_id} 已标记为 applied。\n手动执行:\n```diff\n{diff}\n```"
        return f"改进 #{imp_id} 已标记为 applied（无 code_diff，请手动实现）"

    def _get_improvement(self, imp_id: int) -> dict | None:
        with self.repo._conn() as conn:
            row = conn.execute(
                "SELECT * FROM watcher_improvements WHERE id=? AND status='pending'",
                (imp_id,),
            ).fetchone()
        if not row:
            return None
        cols = [
            "id",
            "trade_date",
            "improvement_type",
            "target_module",
            "target_param",
            "suggested_change",
            "code_diff",
            "rationale",
            "evidence_ids",
            "status",
            "applied_date",
            "effectiveness_check",
            "created_at",
        ]
        return dict(zip(cols, row))
