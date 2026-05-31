"""交易系统数据库访问层"""

import logging
import os
import sqlite3
from datetime import datetime

from system.config.settings import DATABASE_PATH


class TradeRepository:
    def __init__(self, db_path: str = None):
        if db_path:
            self.db_path = db_path
        elif os.environ.get("E2E_TEST_MODE") == "1":
            raise RuntimeError(
                "E2E_TEST_MODE=1 但 TradeRepository 未传入 db_path，"
                "拒绝使用生产库路径。请显式传入测试 DB 路径。"
            )
        else:
            self.db_path = DATABASE_PATH

        # 测试模式下，即使传了 db_path，也不能等于生产库路径
        if os.environ.get("E2E_TEST_MODE") == "1":
            prod_path = os.path.realpath(DATABASE_PATH)
            actual_path = os.path.realpath(self.db_path)
            if actual_path == prod_path:
                raise RuntimeError(
                    f"E2E_TEST_MODE=1 但 TradeRepository 的 db_path 指向生产库:\n"
                    f"  {actual_path}\n"
                    f"  请传入测试 DB 路径，不要使用生产库。"
                )

    def _conn(self):
        return sqlite3.connect(self.db_path)

    # ---- trade_signals ----

    def insert_signal(self, signal_dict: dict) -> int:
        conn = self._conn()
        cols = ", ".join(signal_dict.keys())
        placeholders = ", ".join(["?" for _ in signal_dict])
        sql = f"INSERT OR REPLACE INTO trade_signals ({cols}) VALUES ({placeholders})"
        cursor = conn.execute(sql, list(signal_dict.values()))
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id

    def get_pending_signals(
        self, trade_date: str = None, account: str = None
    ) -> list[dict]:
        conn = self._conn()
        where = ["status='pending'"]
        params: list = []
        if trade_date:
            where.append("trade_date=?")
            params.append(trade_date)
        if account:
            where.append("account=?")
            params.append(account)
        sql = f"SELECT * FROM trade_signals WHERE {' AND '.join(where)}"
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        cols = [
            "id",
            "trade_date",
            "created_at",
            "signal_type",
            "signal_source",
            "stock_code",
            "stock_name",
            "buy_zone_min",
            "buy_zone_max",
            "target_position",
            "stop_loss",
            "take_profit",
            "trailing_stop",
            "signal_score",
            "strategy_name",
            "reason",
            "status",
            "executed_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    def get_expired_signals(self, before_date: str) -> list[dict]:
        """获取指定日期之前过期且未被重新推荐过的 AI 信号。"""
        conn = self._conn()
        rows = conn.execute(
            """SELECT * FROM trade_signals
               WHERE status='expired' AND trade_date < ?
                 AND strategy_name LIKE 'ai_advisor%'
               ORDER BY trade_date DESC""",
            (before_date,),
        ).fetchall()
        conn.close()
        cols = [
            "id",
            "trade_date",
            "created_at",
            "signal_type",
            "signal_source",
            "stock_code",
            "stock_name",
            "buy_zone_min",
            "buy_zone_max",
            "target_position",
            "stop_loss",
            "take_profit",
            "trailing_stop",
            "signal_score",
            "strategy_name",
            "reason",
            "status",
            "executed_at",
            "account",
            "expected_trend",
        ]
        return [dict(zip(cols, row)) for row in rows]

    def update_signal_status(self, signal_id: int, status: str):
        conn = self._conn()
        conn.execute(
            "UPDATE trade_signals SET status=?, executed_at=? WHERE id=?",
            (status, datetime.now().isoformat(), signal_id),
        )
        conn.commit()
        conn.close()

    def expire_old_pending_signals(self, trade_date: str):
        """将非当日 pending 信号标记为 expired，避免旧信号永远积压。"""
        conn = self._conn()
        cursor = conn.execute(
            "UPDATE trade_signals SET status='expired', executed_at=? "
            "WHERE status='pending' AND trade_date < ?",
            (datetime.now().isoformat(), trade_date),
        )
        n = cursor.rowcount
        conn.commit()
        conn.close()
        if n:
            logger = logging.getLogger(__name__)
            logger.info(f"清理 {n} 条过期 pending 信号（早于 {trade_date}）")

    # ---- trade_orders ----

    def insert_order(self, order_dict: dict) -> int:
        conn = self._conn()
        cols = ", ".join(order_dict.keys())
        placeholders = ", ".join(["?" for _ in order_dict])
        sql = f"INSERT INTO trade_orders ({cols}) VALUES ({placeholders})"
        cursor = conn.execute(sql, list(order_dict.values()))
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id

    def get_orders_by_date(self, trade_date: str, account: str = None) -> list[dict]:
        conn = self._conn()
        if account:
            rows = conn.execute(
                "SELECT * FROM trade_orders WHERE trade_date=? AND account=? ORDER BY order_time",
                (trade_date, account),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_orders WHERE trade_date=? ORDER BY order_time",
                (trade_date,),
            ).fetchall()
        conn.close()
        cols = [
            "id",
            "signal_id",
            "trade_date",
            "order_time",
            "stock_code",
            "order_type",
            "order_price",
            "order_volume",
            "price_type",
            "order_status",
            "filled_volume",
            "filled_price",
            "filled_amount",
            "commission",
            "qmt_order_id",
            "reject_reason",
            "strategy_name",
            "updated_at",
            "account",
        ]
        return [dict(zip(cols, row)) for row in rows]

    # ---- trade_portfolio_snapshots ----

    def insert_snapshot(self, snap_dict: dict):
        conn = self._conn()
        cols = ", ".join(snap_dict.keys())
        placeholders = ", ".join(["?" for _ in snap_dict])
        sql = f"INSERT OR REPLACE INTO trade_portfolio_snapshots ({cols}) VALUES ({placeholders})"
        conn.execute(sql, list(snap_dict.values()))
        conn.commit()
        conn.close()

    def get_snapshots(self, start: str = None, end: str = None) -> list[dict]:
        conn = self._conn()
        if start and end:
            rows = conn.execute(
                "SELECT * FROM trade_portfolio_snapshots WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                (start, end),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trade_portfolio_snapshots ORDER BY trade_date"
            ).fetchall()
        conn.close()
        cols = [
            "id",
            "trade_date",
            "total_value",
            "cash",
            "market_value",
            "daily_pnl",
            "total_pnl",
            "drawdown",
            "position_count",
            "sector_exposure",
            "created_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    # ---- trade_holdings_review ----

    def insert_holdings_review(self, review_dict: dict) -> int:
        """保存 AI 持仓审查建议。apply_sl_tp=True 时同步更新 trade_signals 止损止盈。"""
        conn = self._conn()
        cols = ", ".join(review_dict.keys())
        placeholders = ", ".join(["?" for _ in review_dict])
        sql = f"INSERT OR REPLACE INTO trade_holdings_review ({cols}) VALUES ({placeholders})"
        cursor = conn.execute(sql, list(review_dict.values()))
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id

    def apply_holdings_review_sl_tp(
        self,
        trade_date: str,
        stock_code: str,
        new_stop_loss: float = None,
        new_take_profit: float = None,
    ):
        """将 AI 建议的止损/止盈应用到 bought 状态的信号上"""
        conn = self._conn()
        if new_stop_loss is not None:
            conn.execute(
                "UPDATE trade_signals SET stop_loss=? WHERE id=(SELECT id FROM trade_signals"
                " WHERE trade_date<=? AND stock_code=? AND status='bought'"
                " ORDER BY id DESC LIMIT 1)",
                (new_stop_loss, trade_date, stock_code),
            )
        if new_take_profit is not None:
            conn.execute(
                "UPDATE trade_signals SET take_profit=? WHERE id=(SELECT id FROM trade_signals"
                " WHERE trade_date<=? AND stock_code=? AND status='bought'"
                " ORDER BY id DESC LIMIT 1)",
                (new_take_profit, trade_date, stock_code),
            )
        conn.commit()
        conn.close()

    # ---- trade_portfolio_positions ----

    def get_latest_snapshot(self, account: str) -> dict | None:
        """查最新一条快照，用于启动恢复。"""
        conn = self._conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT * FROM trade_portfolio_snapshots
               WHERE account=? ORDER BY trade_date DESC, id DESC LIMIT 1""",
            (account,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_positions_by_date(self, trade_date: str, account: str) -> list[dict]:
        """查某日持仓明细，用于启动恢复。"""
        conn = self._conn()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM trade_portfolio_positions
               WHERE account=? AND trade_date=? ORDER BY stock_code""",
            (account, trade_date),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def insert_positions(self, trade_date: str, account: str, positions: list[dict]):
        """批量保存持仓明细（按日按账户覆盖）。"""
        conn = self._conn()
        conn.execute(
            "DELETE FROM trade_portfolio_positions WHERE trade_date=? AND account=?",
            (trade_date, account),
        )
        for p in positions:
            p["trade_date"] = trade_date
            p["account"] = account
            p["created_at"] = datetime.now().isoformat()
            cols = ", ".join(p.keys())
            placeholders = ", ".join(["?" for _ in p])
            conn.execute(
                f"INSERT INTO trade_portfolio_positions ({cols}) VALUES ({placeholders})",
                list(p.values()),
            )
        conn.commit()
        conn.close()

    # ---- strategy_funnel ----

    def insert_funnel_batch(self, rows: list[dict]):
        if not rows:
            return
        conn = self._conn()
        cols = list(rows[0].keys())
        col_str = ", ".join(cols)
        placeholders = ", ".join(["?" for _ in cols])
        sql = f"INSERT INTO strategy_funnel ({col_str}) VALUES ({placeholders})"
        for r in rows:
            conn.execute(sql, [r.get(c) for c in cols])
        conn.commit()
        conn.close()

    def backfill_funnel_close(self, trade_date: str, updates: list[dict]):
        conn = self._conn()
        for u in updates:
            conn.execute(
                "UPDATE strategy_funnel SET close_price=?, day_change_pct=?, open_price=?, "
                "bought=?, buy_price=?, day_pnl_pct=? WHERE push_date=? AND stock_code=?",
                (
                    u.get("close_price"),
                    u.get("day_change_pct"),
                    u.get("open_price"),
                    u.get("bought", 0),
                    u.get("buy_price"),
                    u.get("day_pnl_pct"),
                    trade_date,
                    u["stock_code"],
                ),
            )
        conn.commit()
        conn.close()

    def get_funnel_records(self, push_date: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM strategy_funnel WHERE push_date=? ORDER BY rank_position",
            (push_date,),
        ).fetchall()
        conn.close()
        cols = [
            "id",
            "push_date",
            "trade_date",
            "stock_code",
            "stock_name",
            "rank_position",
            "raw_snapshot",
            "factors_passed",
            "factors_detail",
            "scenarios",
            "trend_mode",
            "score",
            "open_price",
            "close_price",
            "day_change_pct",
            "bought",
            "buy_price",
            "day_pnl_pct",
            "created_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    # ---- strategy_ai_decisions ----

    def insert_ai_decisions_batch(self, decisions: list[dict]):
        if not decisions:
            return
        conn = self._conn()
        cols = list(decisions[0].keys())
        col_str = ", ".join(cols)
        placeholders = ", ".join(["?" for _ in cols])
        sql = f"INSERT INTO strategy_ai_decisions ({col_str}) VALUES ({placeholders})"
        for d in decisions:
            conn.execute(sql, [d.get(c) for c in cols])
        conn.commit()
        conn.close()

    def get_ai_decisions(self, push_date: str, verdict: str = None) -> list[dict]:
        conn = self._conn()
        if verdict:
            rows = conn.execute(
                "SELECT * FROM strategy_ai_decisions WHERE push_date=? AND verdict=? ORDER BY rank_in_prompt",
                (push_date, verdict),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strategy_ai_decisions WHERE push_date=? ORDER BY rank_in_prompt",
                (push_date,),
            ).fetchall()
        conn.close()
        cols = [
            "id",
            "push_date",
            "trade_date",
            "stock_code",
            "stock_name",
            "rank_in_prompt",
            "verdict",
            "confidence",
            "what_i_see",
            "what_concerns_me",
            "decisive_factor",
            "skip_reason",
            "would_reconsider_if",
            "buy_zone_min",
            "buy_zone_max",
            "stop_loss",
            "take_profit",
            "pricing_logic",
            "signal_id",
            "day_change_pct",
            "day_pnl_pct",
            "created_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    def backfill_ai_decisions_close(self, trade_date: str, updates: list[dict]):
        conn = self._conn()
        for u in updates:
            conn.execute(
                "UPDATE strategy_ai_decisions SET day_change_pct=?, day_pnl_pct=? "
                "WHERE push_date=? AND stock_code=?",
                (
                    u.get("day_change_pct"),
                    u.get("day_pnl_pct"),
                    trade_date,
                    u["stock_code"],
                ),
            )
        conn.commit()
        conn.close()

    # ---- strategy_lessons ----

    def upsert_lesson(self, lesson_dict: dict):
        conn = self._conn()
        existing = conn.execute(
            "SELECT id, occurrence_count FROM strategy_lessons WHERE lesson_type=? AND lesson_key=?",
            (lesson_dict["lesson_type"], lesson_dict["lesson_key"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE strategy_lessons SET occurrence_count=?, last_date=?, "
                "lesson_content=?, trigger_conditions=? WHERE id=?",
                (
                    existing[1] + 1,
                    lesson_dict["last_date"],
                    lesson_dict.get("lesson_content", ""),
                    lesson_dict.get("trigger_conditions", ""),
                    existing[0],
                ),
            )
        else:
            cols = ", ".join(lesson_dict.keys())
            placeholders = ", ".join(["?" for _ in lesson_dict])
            conn.execute(
                f"INSERT INTO strategy_lessons ({cols}) VALUES ({placeholders})",
                list(lesson_dict.values()),
            )
        conn.commit()
        conn.close()

    def get_active_lessons(self, lesson_type: str = None) -> list[dict]:
        conn = self._conn()
        if lesson_type:
            rows = conn.execute(
                "SELECT * FROM strategy_lessons WHERE is_active=1 AND lesson_type=? ORDER BY last_date DESC",
                (lesson_type,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM strategy_lessons WHERE is_active=1 ORDER BY last_date DESC",
            ).fetchall()
        conn.close()
        cols = [
            "id",
            "lesson_type",
            "lesson_key",
            "lesson_content",
            "trigger_conditions",
            "occurrence_count",
            "first_date",
            "last_date",
            "is_active",
            "created_at",
        ]
        return [dict(zip(cols, row)) for row in rows]

    # ---- strategy_improvements ----

    def insert_improvement(self, imp_dict: dict) -> int:
        conn = self._conn()
        cols = ", ".join(imp_dict.keys())
        placeholders = ", ".join(["?" for _ in imp_dict])
        sql = f"INSERT INTO strategy_improvements ({cols}) VALUES ({placeholders})"
        cursor = conn.execute(sql, list(imp_dict.values()))
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id

    def get_pending_improvements(self) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM strategy_improvements WHERE status='pending' ORDER BY id",
        ).fetchall()
        conn.close()
        cols = [
            "id",
            "push_date",
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
        return [dict(zip(cols, row)) for row in rows]

    def apply_improvement(self, imp_id: int):
        from datetime import date

        conn = self._conn()
        conn.execute(
            "UPDATE strategy_improvements SET status='applied', applied_date=? WHERE id=?",
            (date.today().isoformat(), imp_id),
        )
        conn.commit()
        conn.close()
