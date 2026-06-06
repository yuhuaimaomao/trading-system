"""盯盘自审计表 + 持仓审查 数据访问"""

import json

from data.repo.repo_base import BaseRepository

_REVIEW_COLS = frozenset(
    {
        "trade_date",
        "created_at",
        "stock_code",
        "account",
        "action",
        "new_stop_loss",
        "new_take_profit",
        "expected_holding_days",
        "tomorrow_outlook",
        "reason",
        "applied",
    }
)


class AuditRepo(BaseRepository):
    """watcher_decision_log + audit_findings + watcher_lessons + watcher_improvements
    + trade_holdings_review"""

    # -- holdings_review --

    def insert_holdings_review(self, review_dict: dict) -> int:
        return self._insert("trade_holdings_review", review_dict, _REVIEW_COLS)

    def apply_holdings_review_sl_tp(
        self,
        trade_date: str,
        stock_code: str,
        new_stop_loss: float = None,
        new_take_profit: float = None,
    ):
        with self._conn() as conn:
            if new_stop_loss is not None:
                conn.execute(
                    "UPDATE trade_signals SET stop_loss=? WHERE id=(SELECT id FROM "
                    "trade_signals WHERE trade_date<=? AND stock_code=? AND "
                    "status='bought' ORDER BY id DESC LIMIT 1)",
                    (new_stop_loss, trade_date, stock_code),
                )
            if new_take_profit is not None:
                conn.execute(
                    "UPDATE trade_signals SET take_profit=? WHERE id=(SELECT id FROM "
                    "trade_signals WHERE trade_date<=? AND stock_code=? AND "
                    "status='bought' ORDER BY id DESC LIMIT 1)",
                    (new_take_profit, trade_date, stock_code),
                )
            conn.commit()

    # -- decision_log --

    def insert_decision_log(
        self,
        trade_date: str,
        ts: str,
        decision_type: str,
        stock_code: str | None,
        decision_data: dict,
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO watcher_decision_log "
                "(trade_date, ts, decision_type, stock_code, decision_data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    trade_date,
                    ts,
                    decision_type,
                    stock_code,
                    json.dumps(decision_data, ensure_ascii=False),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_decision_logs(
        self, trade_date: str, decision_type: str = None
    ) -> list[dict]:
        where = ["trade_date=?"]
        params = [trade_date]
        if decision_type:
            where.append("decision_type=?")
            params.append(decision_type)
        sql = (
            "SELECT * FROM watcher_decision_log WHERE "
            f"{' AND '.join(where)} ORDER BY ts"
        )
        cols = [
            "id",
            "trade_date",
            "ts",
            "decision_type",
            "stock_code",
            "decision_data",
            "created_at",
        ]
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    # -- audit_findings --

    def insert_audit_finding(self, finding: dict) -> int:
        with self._conn() as conn:
            cols = ", ".join(finding.keys())
            placeholders = ", ".join(["?" for _ in finding])
            vals = []
            for k in finding:
                v = finding[k]
                vals.append(
                    json.dumps(v, ensure_ascii=False)
                    if isinstance(v, (dict, list))
                    else v
                )
            sql = f"INSERT INTO audit_findings ({cols}) VALUES ({placeholders})"
            cursor = conn.execute(sql, vals)
            conn.commit()
            return cursor.lastrowid

    def get_audit_findings(self, trade_date: str) -> list[dict]:
        sql = (
            "SELECT * FROM audit_findings WHERE trade_date=? "
            "ORDER BY CASE severity "
            "WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END"
        )
        cols = [
            "id",
            "trade_date",
            "finding_type",
            "severity",
            "stock_code",
            "decision_log_ids",
            "pattern_desc",
            "evidence",
            "created_at",
        ]
        with self._conn() as conn:
            rows = conn.execute(sql, (trade_date,)).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    # -- watcher_improvements --

    def insert_watcher_improvement(self, imp: dict) -> int:
        with self._conn() as conn:
            cols = ", ".join(imp.keys())
            placeholders = ", ".join(["?" for _ in imp])
            vals = []
            for k in imp:
                v = imp[k]
                vals.append(
                    json.dumps(v, ensure_ascii=False)
                    if isinstance(v, (dict, list))
                    else v
                )
            sql = f"INSERT INTO watcher_improvements ({cols}) VALUES ({placeholders})"
            cursor = conn.execute(sql, vals)
            conn.commit()
            return cursor.lastrowid

    def get_pending_watcher_improvements(self) -> list[dict]:
        sql = "SELECT * FROM watcher_improvements WHERE status='pending' ORDER BY id"
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
        with self._conn() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def update_watcher_improvement_status(
        self, imp_id: int, status: str, applied_date: str = None
    ):
        with self._conn() as conn:
            conn.execute(
                "UPDATE watcher_improvements SET status=?, applied_date=? WHERE id=?",
                (status, applied_date, imp_id),
            )
            conn.commit()

    def update_watcher_improvement_effectiveness(self, imp_id: int, check: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE watcher_improvements SET effectiveness_check=? WHERE id=?",
                (check, imp_id),
            )
            conn.commit()
