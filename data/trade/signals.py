"""trade_signals 表数据访问

交易线 — 信号 CRUD。
"""

from datetime import datetime

from data._base import BaseRepository, cols_from_str
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
        sql = f"SELECT {_SIGNAL_ALL_COLS} FROM trade_signals WHERE {' AND '.join(where)}"
        cols = cols_from_str(_SIGNAL_ALL_COLS.replace(" ", ""))
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
        cols = cols_from_str(_SIGNAL_ALL_COLS.replace(" ", ""))
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
            conn.execute(
                "UPDATE trade_signals SET status='expired', executed_at=? WHERE status='pending' AND trade_date < ?",
                (datetime.now().isoformat(), trade_date),
            )
            conn.commit()

    def get_bought_sl_tp_batch(self, codes: list[str]) -> dict[str, dict]:
        """批量查已买入信号的止损止盈，返回 {code: {stop_loss, take_profit, score}}。"""
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT stock_code, stop_loss, take_profit, signal_score "
                f"FROM trade_signals "
                f"WHERE status='bought' AND stock_code IN ({placeholders}) "
                f"ORDER BY id DESC",
                list(codes),
            ).fetchall()
        result = {}
        for r in rows:
            if r[0] not in result:
                result[r[0]] = {"stop_loss": r[1] or 0, "take_profit": r[2] or 0, "score": r[3] or 0}
        return result
