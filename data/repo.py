"""交易系统数据库访问层"""

import sqlite3
from datetime import datetime
from system.config.settings import DATABASE_PATH


class TradeRepository:
    def __init__(self):
        self.db_path = DATABASE_PATH

    def _conn(self):
        return sqlite3.connect(self.db_path)

    # ---- trade_signals ----

    def insert_signal(self, signal_dict: dict) -> int:
        conn = self._conn()
        cols = ", ".join(signal_dict.keys())
        placeholders = ", ".join(["?" for _ in signal_dict])
        sql = f"INSERT OR IGNORE INTO trade_signals ({cols}) VALUES ({placeholders})"
        cursor = conn.execute(sql, list(signal_dict.values()))
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id

    def get_pending_signals(self, trade_date: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM trade_signals WHERE trade_date=? AND status='pending'",
            (trade_date,),
        ).fetchall()
        conn.close()
        cols = ["id", "trade_date", "created_at", "signal_type", "signal_source",
                "stock_code", "stock_name", "buy_zone_min", "buy_zone_max",
                "target_position", "stop_loss", "take_profit", "trailing_stop",
                "signal_score", "strategy_name", "reason", "status", "executed_at"]
        return [dict(zip(cols, row)) for row in rows]

    def update_signal_status(self, signal_id: int, status: str):
        conn = self._conn()
        conn.execute(
            "UPDATE trade_signals SET status=?, executed_at=? WHERE id=?",
            (status, datetime.now().isoformat(), signal_id),
        )
        conn.commit()
        conn.close()

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

    def get_orders_by_date(self, trade_date: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM trade_orders WHERE trade_date=? ORDER BY order_time",
            (trade_date,),
        ).fetchall()
        conn.close()
        cols = ["id", "signal_id", "trade_date", "order_time", "stock_code",
                "order_type", "order_price", "order_volume", "price_type",
                "order_status", "filled_volume", "filled_price", "filled_amount",
                "commission", "qmt_order_id", "reject_reason", "strategy_name",
                "updated_at"]
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
        cols = ["id", "trade_date", "total_value", "cash", "market_value",
                "daily_pnl", "total_pnl", "drawdown", "position_count",
                "sector_exposure", "created_at"]
        return [dict(zip(cols, row)) for row in rows]

    # ---- trade_factor_values ----

    def save_factor_values(self, trade_date: str, factor_name: str,
                           values: dict) -> int:
        """批量保存因子值 {stock_code: value}"""
        conn = self._conn()
        now = datetime.now().isoformat()
        count = 0
        for code, value in values.items():
            conn.execute(
                """INSERT OR REPLACE INTO trade_factor_values
                   (trade_date, stock_code, factor_name, factor_value, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (trade_date, code, factor_name, value, now),
            )
            count += 1
        conn.commit()
        conn.close()
        return count

    def get_factor_values(self, trade_date: str, factor_name: str) -> dict:
        conn = self._conn()
        rows = conn.execute(
            "SELECT stock_code, factor_value FROM trade_factor_values WHERE trade_date=? AND factor_name=?",
            (trade_date, factor_name),
        ).fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}

    # ---- trade_strategy_metrics ----

    def save_metrics(self, metrics: dict):
        conn = self._conn()
        cols = ", ".join(metrics.keys())
        placeholders = ", ".join(["?" for _ in metrics])
        sql = f"INSERT OR REPLACE INTO trade_strategy_metrics ({cols}) VALUES ({placeholders})"
        conn.execute(sql, list(metrics.values()))
        conn.commit()
        conn.close()
