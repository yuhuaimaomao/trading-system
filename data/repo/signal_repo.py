"""trade_signals 表数据访问"""

from datetime import datetime

from data.repo.repo_base import BaseRepository, _cols_from_str
from system.utils.logger import get_system_logger

logger = get_system_logger("data")

_SIGNAL_COLS = frozenset(
    {
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
    }
)

_SIGNAL_ALL_COLS = (
    "id, trade_date, created_at, signal_type, signal_source, "
    "stock_code, stock_name, buy_zone_min, buy_zone_max, "
    "target_position, stop_loss, take_profit, trailing_stop, "
    "signal_score, strategy_name, reason, status, executed_at, "
    "account, expected_trend"
)


class SignalRepo(BaseRepository):
    """trade_signals 表 CRUD"""

    def insert(self, signal_dict: dict) -> int:
        return self._insert("trade_signals", signal_dict, _SIGNAL_COLS)

    def get_pending(self, trade_date: str = None, account: str = None) -> list[dict]:
        where = ["status='pending'"]
        params = []
        if trade_date:
            where.append("trade_date=?")
            params.append(trade_date)
        if account:
            where.append("account=?")
            params.append(account)
        sql = (
            f"SELECT {_SIGNAL_ALL_COLS} FROM trade_signals WHERE {' AND '.join(where)}"
        )
        cols = _cols_from_str(_SIGNAL_ALL_COLS.replace(" ", ""))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def get_expired(self, before_date: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                f"""SELECT {_SIGNAL_ALL_COLS} FROM trade_signals
                   WHERE status='expired' AND trade_date < ?
                     AND strategy_name LIKE 'ai_advisor%'
                   ORDER BY trade_date DESC""",
                (before_date,),
            ).fetchall()
        cols = _cols_from_str(_SIGNAL_ALL_COLS.replace(" ", ""))
        return [dict(zip(cols, row)) for row in rows]

    def update_status(self, signal_id: int, status: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE trade_signals SET status=?, executed_at=? WHERE id=?",
                (status, datetime.now().isoformat(), signal_id),
            )
            conn.commit()

    def expire_old_pending(self, trade_date: str):
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE trade_signals SET status='expired', executed_at=? "
                "WHERE status='pending' AND trade_date < ?",
                (datetime.now().isoformat(), trade_date),
            )
            n = cursor.rowcount
            conn.commit()
        if n:
            logger.info(f"清理 {n} 条过期 pending 信号（早于 {trade_date}）")
