"""数据访问层 — 按业务线拆分，TradeRepository 保持向后兼容。"""

import os

from data.audit.decision_log import AuditRepo
from data.strategy.funnel import StrategyRepo
from data.trade.orders import OrderRepo
from data.trade.portfolio import PortfolioRepo
from data.trade.signals import SignalRepo

__all__ = [
    "SignalRepo",
    "OrderRepo",
    "PortfolioRepo",
    "StrategyRepo",
    "AuditRepo",
    "TradeRepository",
]


class TradeRepository:
    """向后兼容的 TradeRepository — 委托给拆分后的 Repo。

    新代码应直接使用 SignalRepo / OrderRepo / PortfolioRepo / StrategyRepo / AuditRepo。
    旧代码通过 TradeRepository 继续工作，无需改动。
    """

    def __init__(self, db_path: str = None):
        from system.config.settings import DATABASE_PATH

        if db_path:
            self.db_path = db_path
        elif os.environ.get("E2E_TEST_MODE") == "1":
            raise RuntimeError(
                "E2E_TEST_MODE=1 但 TradeRepository 未传入 db_path，拒绝使用生产库路径。请显式传入测试 DB 路径。"
            )
        else:
            self.db_path = DATABASE_PATH

        if os.environ.get("E2E_TEST_MODE") == "1":
            prod_path = os.path.realpath(DATABASE_PATH)
            actual_path = os.path.realpath(self.db_path)
            if actual_path == prod_path:
                raise RuntimeError(
                    f"E2E_TEST_MODE=1 但 TradeRepository 的 db_path 指向生产库:\n"
                    f"  {actual_path}\n  请传入测试 DB 路径，不要使用生产库。"
                )

        self._signal = SignalRepo(self.db_path)
        self._order = OrderRepo(self.db_path)
        self._portfolio = PortfolioRepo(self.db_path)
        self._strategy = StrategyRepo(self.db_path)
        self._audit = AuditRepo(self.db_path)

    # ---- trade_signals ----
    def insert_signal(self, signal_dict: dict) -> int:
        return self._signal.insert(signal_dict)

    def get_pending_signals(self, trade_date: str = None, account: str = None) -> list[dict]:
        return self._signal.get_pending(trade_date, account)

    def get_expired_signals(self, before_date: str) -> list[dict]:
        return self._signal.get_expired(before_date)

    def update_signal_status(self, signal_id: int, status: str):
        self._signal.update_status(signal_id, status)

    def expire_old_pending_signals(self, trade_date: str):
        self._signal.expire_old_pending(trade_date)

    # ---- trade_orders ----
    def insert_order(self, order_dict: dict) -> int:
        return self._order.insert(order_dict)

    def get_orders_by_date(self, trade_date: str, account: str = None) -> list[dict]:
        return self._order.get_by_date(trade_date, account)

    def get_sold_codes(self, codes: list[str], account: str) -> set[str]:
        return self._order.get_sold_codes(codes, account)

    # ---- portfolio ----
    def insert_snapshot(self, snap_dict: dict):
        self._portfolio.insert_snapshot(snap_dict)

    def get_latest_snapshot(self, account: str) -> dict | None:
        return self._portfolio.get_latest_snapshot(account)

    def get_latest_snapshot_before(self, trade_date: str, account: str) -> dict | None:
        return self._portfolio.get_latest_snapshot_before(trade_date, account)

    def get_first_snapshot_of_day(self, trade_date: str, account: str) -> dict | None:
        return self._portfolio.get_latest_snapshot(account)

    def get_snapshots(self, start: str = None, end: str = None) -> list[dict]:
        return self._portfolio.get_snapshots(start, end)

    def get_positions_by_date(self, trade_date: str, account: str) -> list[dict]:
        return self._portfolio.get_positions_by_date(trade_date, account)

    def get_latest_positions(self, account: str) -> list[dict]:
        return self._portfolio.get_latest_positions(account)

    def insert_positions(self, trade_date: str, account: str, positions: list[dict]):
        self._portfolio.insert_positions(trade_date, account, positions)

    # ---- strategy ----
    def insert_funnel_batch(self, rows: list[dict]):
        self._strategy.insert_funnel_batch(rows)

    def backfill_funnel_close(self, trade_date: str, updates: list[dict]):
        self._strategy.backfill_funnel_close(trade_date, updates)

    def get_funnel_records(self, push_date: str) -> list[dict]:
        return self._strategy.get_funnel_records(push_date)

    def insert_ai_decisions_batch(self, decisions: list[dict]):
        self._strategy.insert_ai_decisions_batch(decisions)

    def get_ai_decisions(self, push_date: str, verdict: str = None) -> list[dict]:
        return self._strategy.get_ai_decisions(push_date, verdict)

    def backfill_ai_decisions_close(self, trade_date: str, updates: list[dict]):
        self._strategy.backfill_ai_decisions_close(trade_date, updates)

    def insert_improvement(self, imp_dict: dict) -> int:
        return self._strategy.insert_improvement(imp_dict)

    def get_pending_improvements(self) -> list[dict]:
        return self._strategy.get_pending_improvements()

    def apply_improvement(self, imp_id: int):
        self._strategy.apply_improvement(imp_id)

    # ---- audit ----
    def insert_decision_log(
        self,
        trade_date: str,
        ts: str,
        decision_type: str,
        stock_code: str | None,
        decision_data: dict,
    ) -> int:
        return self._audit.insert_decision_log(trade_date, ts, decision_type, stock_code, decision_data)

    def get_decision_logs(self, trade_date: str, decision_type: str = None) -> list[dict]:
        return self._audit.get_decision_logs(trade_date, decision_type)

    def insert_audit_finding(self, finding: dict) -> int:
        return self._audit.insert_audit_finding(finding)

    def get_audit_findings(self, trade_date: str) -> list[dict]:
        return self._audit.get_audit_findings(trade_date)

    def insert_watcher_improvement(self, imp: dict) -> int:
        return self._audit.insert_watcher_improvement(imp)

    def get_pending_watcher_improvements(self) -> list[dict]:
        return self._audit.get_pending_watcher_improvements()

    def update_watcher_improvement_status(self, imp_id: int, status: str, applied_date: str = None):
        self._audit.update_watcher_improvement_status(imp_id, status, applied_date)

    def update_watcher_improvement_effectiveness(self, imp_id: int, check: str):
        self._audit.update_watcher_improvement_effectiveness(imp_id, check)

    # ---- holdings_review ----
    def insert_holdings_review(self, review_dict: dict) -> int:
        return self._audit.insert_holdings_review(review_dict)

    def apply_holdings_review_sl_tp(
        self,
        trade_date: str,
        stock_code: str,
        new_stop_loss: float = None,
        new_take_profit: float = None,
    ):
        self._audit.apply_holdings_review_sl_tp(trade_date, stock_code, new_stop_loss, new_take_profit)

    # ---- legacy (kept for backward compat) ----
    def upsert_lesson(self, lesson_dict: dict):
        with self._audit._conn() as conn:
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

    def get_active_lessons(self, lesson_type: str = None) -> list[dict]:
        with self._audit._conn() as conn:
            if lesson_type:
                rows = conn.execute(
                    "SELECT * FROM strategy_lessons WHERE is_active=1 AND lesson_type=? ORDER BY last_date DESC",
                    (lesson_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM strategy_lessons WHERE is_active=1 ORDER BY last_date DESC",
                ).fetchall()
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

    def upsert_watcher_lesson(
        self,
        lesson_type: str,
        lesson_key: str,
        lesson_content: str,
        trigger_conditions: dict = None,
        trade_date: str = None,
    ) -> int:
        import json

        with self._audit._conn() as conn:
            existing = conn.execute(
                "SELECT id, occurrence_count FROM watcher_lessons WHERE lesson_type=? AND lesson_key=?",
                (lesson_type, lesson_key),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE watcher_lessons SET occurrence_count=?, last_date=?, is_active=1 WHERE id=?",
                    (existing[1] + 1, trade_date, existing[0]),
                )
                conn.commit()
                return existing[0]
            else:
                cursor = conn.execute(
                    "INSERT INTO watcher_lessons "
                    "(lesson_type, lesson_key, lesson_content, trigger_conditions, first_date, last_date) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        lesson_type,
                        lesson_key,
                        lesson_content,
                        json.dumps(trigger_conditions, ensure_ascii=False) if trigger_conditions else None,
                        trade_date,
                        trade_date,
                    ),
                )
                conn.commit()
                return cursor.lastrowid

    def get_active_watcher_lessons(self, lesson_type: str = None) -> list[dict]:
        where = ["is_active=1"]
        params = []
        if lesson_type:
            where.append("lesson_type=?")
            params.append(lesson_type)
        sql = f"SELECT * FROM watcher_lessons WHERE {' AND '.join(where)} ORDER BY occurrence_count DESC"
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
        with self._audit._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    # ---- 盯盘辅助查询 ----

    def get_morning_sector_bias(self, trade_date: str) -> list[dict]:
        import json

        with self._audit._conn() as conn:
            rows = conn.execute(
                "SELECT sector_name, bias, priority, size_multiplier, stock_codes, reason "
                "FROM morning_sector_bias WHERE trade_date=?",
                (trade_date,),
            ).fetchall()
        return [
            {
                "sector_name": r[0],
                "bias": r[1],
                "priority": r[2],
                "size_multiplier": r[3],
                "stock_codes": json.loads(r[4]) if r[4] else [],
                "reason": r[5],
            }
            for r in rows
        ]

    def get_signal_for_pos_meta(self, code: str) -> dict | None:
        import sqlite3

        with self._signal._conn() as conn:
            conn.row_factory = sqlite3.Row
            sig = conn.execute(
                "SELECT stop_loss, take_profit, trailing_stop, signal_score, "
                "strategy_name, id FROM trade_signals "
                "WHERE stock_code=? AND status='bought' ORDER BY id DESC LIMIT 1",
                (code,),
            ).fetchone()
        return dict(sig) if sig else None

    def get_buy_dates(self, codes: list[str]) -> dict[str, str]:
        if not codes:
            return {}
        placeholders = ",".join("?" * len(codes))
        with self._order._conn() as conn:
            rows = conn.execute(
                f"SELECT stock_code, MIN(date(order_time)) as buy_date "
                "FROM trade_orders WHERE order_type='buy' AND order_status='filled' "
                f"AND account='paper' AND stock_code IN ({placeholders}) "
                "GROUP BY stock_code",
                codes,
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_index_ma60(self) -> float:
        try:
            with self._signal._conn() as conn:
                row = conn.execute(
                    "SELECT ma60 FROM stock_basic WHERE stock_code='000001' ORDER BY trade_date DESC LIMIT 1"
                ).fetchone()
                return (row[0] or 0) if row else 0
        except Exception:
            return 0

    def get_volume_trend(self) -> float:
        try:
            with self._signal._conn() as conn:
                rows = conn.execute(
                    "SELECT index_change_pct FROM market_breadth ORDER BY trade_date DESC LIMIT 5"
                ).fetchall()
            if len(rows) < 3:
                return 0
            changes = [abs(r[0]) for r in rows if r[0] is not None]
            if len(changes) < 3:
                return 0
            recent_avg = sum(changes[:2]) / 2
            prev_avg = sum(changes[2:]) / (len(changes) - 2)
            return (recent_avg - prev_avg) / prev_avg if prev_avg > 0 else 0
        except Exception:
            return 0

    def get_market_snapshots_batch(self, trade_date: str, latest_ts: float) -> list[dict]:
        with self._signal._conn() as conn:
            rows = conn.execute(
                "SELECT ts, code, change_pct, price, amount FROM market_snapshots WHERE trade_date=? AND ts=?",
                (trade_date, latest_ts),
            ).fetchall()
        return [
            {
                "ts": r[0],
                "code": r[1],
                "change_pct": r[2],
                "price": r[3] or 0,
                "amount": r[4] or 0,
            }
            for r in rows
        ]

    def get_latest_market_ts(self, trade_date: str) -> float | None:
        with self._signal._conn() as conn:
            row = conn.execute(
                "SELECT MAX(ts) FROM market_snapshots WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def get_index_snapshot_history(self, days: int = 3) -> list[dict]:
        with self._signal._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM index_snapshots ORDER BY trade_date DESC LIMIT ?",
                (days,),
            ).fetchall()
        if len(rows) < 3:
            return []
        prices = []
        for (td,) in rows:
            r = conn.execute(
                "SELECT price FROM index_snapshots WHERE trade_date=? ORDER BY ts DESC LIMIT 1",
                (td,),
            ).fetchone()
            if r:
                prices.append({"trade_date": td, "price": r[0]})
        return prices

    def get_review_signal_zones(self, trade_date: str) -> dict[str, tuple]:
        """加载 REVIEW 信号的买入区间。返回 {code: (buy_min, buy_max, sl, tp)}。"""
        with self._signal._conn() as conn:
            rows = conn.execute(
                "SELECT stock_code, buy_zone_min, buy_zone_max, stop_loss, take_profit "
                "FROM trade_signals WHERE trade_date=? AND signal_source='REVIEW' "
                "AND status='pending' AND account='paper'",
                (trade_date,),
            ).fetchall()
        return {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0) for r in rows if r[1] and r[2]}

    def get_review_picks_latest(self) -> list[dict]:
        """查询最新复盘推荐标的。"""
        with self._signal._conn() as conn:
            rows = conn.execute(
                "SELECT stock_code, stock_name, stop_loss, target_price, abandon_condition "
                "FROM stock_tracker WHERE push_date = ("
                "SELECT MAX(push_date) FROM stock_tracker WHERE source='复盘')"
            ).fetchall()
        return [
            {
                "stock_code": r[0],
                "stock_name": r[1],
                "stop_loss": r[2] or 0,
                "target_price": r[3] or 0,
                "abandon_condition": r[4] or "",
            }
            for r in rows
        ]

    def resolve_name(self, code: str) -> str:
        with self._signal._conn() as conn:
            row = conn.execute(
                "SELECT stock_name FROM stock_basic "
                "WHERE stock_code=? AND trade_date=(SELECT MAX(trade_date) FROM stock_basic) "
                "LIMIT 1",
                (code,),
            ).fetchone()
        return row[0] if row else code

    # ---- StockReader 封装（消除 monitor 文件中的直接 sqlite3） ----

    def get_daily_indicators(self, code: str) -> dict | None:
        from data.readers.stock_reader import StockReader

        with self._signal._conn() as conn:
            return StockReader.get_daily_indicators(conn, code)

    def get_money_flow(self, code: str) -> dict | None:
        from data.readers.stock_reader import StockReader

        with self._signal._conn() as conn:
            return StockReader.get_money_flow(conn, code)

    def get_support_resistance(self, code: str, price: float) -> dict:
        from data.readers.stock_reader import StockReader

        with self._signal._conn() as conn:
            return StockReader.get_support_resistance(conn, code, price)

    def get_stock_basic(self, code: str) -> dict | None:
        """查询单只股票最新基础数据（给 check_hard_gates 用）。"""
        from data.readers.stock_reader import StockReader

        with self._signal._conn() as conn:
            conn.row_factory = __import__("sqlite3").Row
            return StockReader.get_stock_basic(conn, code)

    def get_stock_basic_batch(self, trade_date: str, codes: list[str]) -> dict[str, dict]:
        """批量查询当日 stock_basic，返回 {code: row_dict}。"""
        from data.readers.stock_reader import StockReader

        with self._signal._conn() as conn:
            return StockReader.get_stock_basic_batch(conn, trade_date, codes)

    def get_latest_stock_basic_batch(self, codes: list[str]) -> dict[str, dict]:
        """批量查询最新 stock_basic（MAX trade_date），返回 {code: row_dict}。"""
        from data.readers.stock_reader import StockReader

        with self._signal._conn() as conn:
            return StockReader.get_latest_stock_basic_batch(conn, codes)

    def get_bought_sl_tp_batch(self, codes: list[str]) -> dict[str, dict]:
        return self._signal.get_bought_sl_tp_batch(codes)

    def get_account_summary(self, account: str) -> dict | None:
        """查最新快照的账户概况。"""
        return self.get_latest_snapshot(account)

    def get_signals_by_date_source(self, trade_date: str, source: str) -> list[dict]:
        """按日期+来源查信号（backfill 用）。"""
        import sqlite3

        with self._signal._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, stock_code FROM trade_signals WHERE trade_date=? AND signal_source=?",
                (trade_date, source),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_decision_signal_id(self, push_date: str, stock_code: str, signal_id: int):
        """回填 strategy_ai_decisions.signal_id。"""
        with self._signal._conn() as conn:
            conn.execute(
                "UPDATE strategy_ai_decisions SET signal_id=? WHERE push_date=? AND stock_code=?",
                (signal_id, push_date, stock_code),
            )
            conn.commit()

    def get_stock_price(self, code: str, trade_date: str) -> float | None:
        """查询某日收盘价。"""
        with self._signal._conn() as conn:
            row = conn.execute(
                "SELECT price FROM stock_basic WHERE stock_code=? AND trade_date=? ORDER BY trade_date DESC LIMIT 1",
                (code, trade_date),
            ).fetchone()
        return float(row[0]) if row and row[0] else None

    def get_bought_signals_with_entry(self) -> list[dict]:
        """查询已买入信号 + 成交均价 + 买入时间（_check_bought_signals 用）。"""
        import sqlite3

        with self._order._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT ts.*, buy_info.entry_price, buy_info.buy_time
                   FROM trade_signals ts
                   JOIN (
                       SELECT signal_id,
                              SUM(filled_price * filled_volume) / SUM(filled_volume) as entry_price,
                              MAX(order_time) as buy_time
                       FROM trade_orders
                       WHERE order_type='buy' AND order_status='filled'
                         AND filled_volume > 0 AND account='paper'
                       GROUP BY signal_id
                   ) buy_info ON buy_info.signal_id = ts.id
                   WHERE ts.status='bought' AND ts.account='paper'""",
            ).fetchall()
        return [dict(r) for r in rows]
