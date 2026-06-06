"""审计管线 — 统一编排：规则审计 → AI审计 → 入库 → 推送。

用法:
    pipeline = AuditPipeline("strategy", rule_auditor, ai_auditor, repo=repo)
    result = pipeline.run("2026-06-01", push=False)
    # result = {"findings": [...], "improvements": [...], "lessons": [...]}
"""

import sqlite3

from system.utils.logger import get_system_logger

logger = get_system_logger("audit_pipeline")

TELEGRAM_REPORT_CHAT_ID = None
TELEGRAM_REPORT_BOT_TOKEN = None


class MessageSender:
    """Telegram 消息发送器。"""

    def __init__(self, chat_id: str = None, token: str = None):
        self.chat_id = chat_id
        self.token = token

    def send(self, text: str) -> bool:
        if not self.chat_id:
            return False
        try:
            from system.message.sender import send_telegram

            send_telegram(text, chat_id=self.chat_id, token=self.token)
            return True
        except Exception:
            logger.warning("审计报告推送失败", exc_info=True)
            return False


class AuditPipeline:
    """审计管线：编排规则+AI两步审计，生成改进建议，推送结果。

    rule_auditor 和 ai_auditor 由调用方注入（支持依赖注入和 mock 测试）。
    """

    def __init__(self, domain: str, rule_auditor, ai_auditor, repo=None):
        self.domain = domain
        self.rule_auditor = rule_auditor
        self.ai_auditor = ai_auditor
        self.repo = repo
        self.db_path = getattr(repo, "db_path", None) if repo else None

    # ── 入口 ──────────────────────────────────────────

    def run(
        self,
        push_date: str,
        push: bool = False,
        ai_only: bool = False,
        rule_only: bool = False,
    ) -> dict:
        """执行审计管线。返回 {"findings": [...], "improvements": [...], "lessons": [...]}。"""
        # 第一步：规则审计
        findings = []
        if not ai_only:
            try:
                findings = self.rule_auditor.audit(push_date)
                logger.info(f"[{self.domain}] 规则审计: {len(findings)} 条发现")
            except Exception:
                logger.warning("规则审计异常", exc_info=True)

        # 第二步：AI 审计
        ai_result = {"improvements": [], "lessons": []}
        if not rule_only and self.ai_auditor is not None:
            try:
                ai_result = self.ai_auditor.review(
                    findings, {"date": push_date, "domain": self.domain}
                )
                logger.info(
                    f"[{self.domain}] AI 审计: "
                    f"{len(ai_result.get('improvements', []))} 条改进, "
                    f"{len(ai_result.get('lessons', []))} 条教训"
                )
            except Exception:
                logger.warning("AI 审计异常", exc_info=True)

        improvements = ai_result.get("improvements", [])
        lessons = ai_result.get("lessons", [])

        # 第三步：入库
        if self.repo:
            self._save_findings(push_date, findings)
            self._save_improvements(push_date, improvements)

        # 第四步：推送
        if push and TELEGRAM_REPORT_CHAT_ID:
            self._push_report(push_date, findings, improvements)

        return {
            "findings": findings,
            "improvements": improvements,
            "lessons": lessons,
        }

    # ── 入库 ──────────────────────────────────────────

    def _save_findings(self, push_date: str, findings: list[dict]):
        """保存审计发现到 audit_findings 表。"""
        if not self.db_path:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS audit_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    finding_type TEXT,
                    severity TEXT,
                    pattern_desc TEXT,
                    evidence TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            for f in findings:
                conn.execute(
                    "INSERT INTO audit_findings "
                    "(trade_date, finding_type, severity, pattern_desc, evidence) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        push_date,
                        f.get("finding_type") or f.get("type", ""),
                        f.get("severity", "info"),
                        f.get("pattern_desc", ""),
                        f.get("evidence", "{}"),
                    ),
                )
            conn.commit()
            conn.close()
        except Exception:
            logger.warning("保存审计发现异常", exc_info=True)

    def _save_improvements(self, push_date: str, improvements: list[dict]):
        """保存改进建议到 watcher_improvements 表。"""
        if not self.db_path:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS watcher_improvements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT,
                    improvement_type TEXT,
                    target_module TEXT,
                    target_param TEXT,
                    suggested_change TEXT,
                    code_diff TEXT,
                    rationale TEXT,
                    evidence_ids TEXT,
                    status TEXT DEFAULT 'pending',
                    applied_date TEXT,
                    effectiveness_check TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            for imp in improvements:
                conn.execute(
                    "INSERT INTO watcher_improvements "
                    "(trade_date, improvement_type, target_module, suggested_change, rationale, status) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (
                        push_date,
                        imp.get("improvement_type") or imp.get("type", ""),
                        imp.get("target", ""),
                        imp.get("suggested_change", ""),
                        imp.get("rationale", ""),
                    ),
                )
            conn.commit()
            conn.close()
        except Exception:
            logger.warning("保存改进建议异常", exc_info=True)

    # ── 推送 ──────────────────────────────────────────

    def _push_report(
        self, push_date: str, findings: list[dict], improvements: list[dict]
    ):
        """推送审计报告到 Telegram。"""
        try:
            imp_count = len(improvements)
            rule_count = len(findings)
            lines = [
                f"📊 {self.domain}审计 — {push_date}",
                f"发现 {rule_count} 条规则问题，{imp_count} 条改进建议",
            ]
            sender = MessageSender(chat_id=TELEGRAM_REPORT_CHAT_ID)
            sender.send("\n".join(lines))
        except Exception:
            logger.warning("审计报告推送异常", exc_info=True)


# ── 模块级工具函数 ──────────────────────────────────


def apply_improvement(repo=None, imp_id: int = None) -> bool:
    """应用一条改进建议。兼容 main.py 调用: apply_improvement(repo, imp_id)。"""
    from audit.strategy_improvement import ImprovementApplier

    # 兼容两种调用方式: apply_improvement(repo, imp_id) 和 apply_improvement(imp_id, db_path=...)
    if imp_id is None and isinstance(repo, int):
        imp_id, repo = repo, None
    db_path = getattr(repo, "db_path", None) if repo else None
    try:
        applier = ImprovementApplier(db_path=db_path)
        return applier.apply(imp_id)
    except Exception:
        logger.error(f"应用改进 #{imp_id} 失败", exc_info=True)
        return False


def list_pending(repo=None) -> list[dict]:
    """列出所有待处理的改进建议。兼容 main.py 调用: list_pending(repo)。"""
    from audit.strategy_improvement import ImprovementApplier

    db_path = getattr(repo, "db_path", None) if repo else None
    try:
        applier = ImprovementApplier(db_path=db_path)
        return applier.repo.get_pending_improvements()
    except Exception:
        logger.error("列出待处理改进失败", exc_info=True)
        return []
