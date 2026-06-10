"""持仓 + 快照 表数据访问

交易线 — portfolio snapshots + positions CRUD。
"""

import sqlite3
from datetime import datetime

from data._base import BaseRepository, cols_from_str, round_val, validate_cols

_SNAPSHOT_COLS = frozenset(
    {
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
        "account",
    }
)

_POSITION_COLS = frozenset(
    {
        "stock_code",
        "stock_name",
        "volume",
        "avg_cost",
        "current_price",
        "market_value",
        "pnl",
        "pnl_pct",
        "pre_close",
        "daily_pnl",
        "holding_days",
        "entry_date",
        "locked_volume",
        "stop_loss",
        "take_profit",
        "trade_date",
        "account",
        "created_at",
    }
)

_SNAPSHOT_ALL_COLS = (
    "id, trade_date, total_value, cash, market_value, daily_pnl, "
    "total_pnl, drawdown, position_count, sector_exposure, created_at, account"
)

_POSITION_ALL_COLS = (
    "id, trade_date, account, stock_code, stock_name, volume, "
    "avg_cost, current_price, market_value, pnl, pnl_pct, "
    "pre_close, daily_pnl, holding_days, entry_date, locked_volume, "
    "stop_loss, take_profit, created_at"
)


class PortfolioRepo(BaseRepository):
    """trade_portfolio_snapshots + trade_portfolio_positions CRUD"""

    def insert_snapshot(self, snap_dict: dict):
        validate_cols(_SNAPSHOT_COLS, snap_dict.keys())
        vals = [round_val(v) for v in snap_dict.values()]
        cols = ", ".join(snap_dict.keys())
        placeholders = ", ".join(["?" for _ in snap_dict])
        sql = f"INSERT OR REPLACE INTO trade_portfolio_snapshots ({cols}) VALUES ({placeholders})"
        with self._conn() as conn:
            conn.execute(sql, vals)
            conn.commit()

    def get_latest_snapshot(self, account: str) -> dict | None:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"""SELECT {_SNAPSHOT_ALL_COLS} FROM trade_portfolio_snapshots
                   WHERE account=? ORDER BY trade_date DESC, id DESC LIMIT 1""",
                (account,),
            ).fetchone()
        return dict(row) if row else None

    def get_latest_snapshot_before(self, trade_date: str, account: str) -> dict | None:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"""SELECT {_SNAPSHOT_ALL_COLS} FROM trade_portfolio_snapshots
                   WHERE trade_date < ? AND account=? ORDER BY id DESC LIMIT 1""",
                (trade_date, account),
            ).fetchone()
        return dict(row) if row else None

    def get_snapshots(self, start: str = None, end: str = None) -> list[dict]:
        cols = cols_from_str(_SNAPSHOT_ALL_COLS.replace(" ", ""))
        with self._conn() as conn:
            if start and end:
                rows = conn.execute(
                    f"SELECT {_SNAPSHOT_ALL_COLS} FROM trade_portfolio_snapshots "
                    "WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
                    (start, end),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_SNAPSHOT_ALL_COLS} FROM trade_portfolio_snapshots ORDER BY trade_date"
                ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def get_positions_by_date(self, trade_date: str, account: str) -> list[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""SELECT {_POSITION_ALL_COLS} FROM trade_portfolio_positions
                   WHERE account=? AND trade_date=? ORDER BY stock_code""",
                (account, trade_date),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_positions(self, account: str) -> list[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            latest_date = conn.execute(
                "SELECT MAX(trade_date) FROM trade_portfolio_positions WHERE account=?",
                (account,),
            ).fetchone()
            if not latest_date or not latest_date[0]:
                return []
            rows = conn.execute(
                f"""SELECT {_POSITION_ALL_COLS} FROM trade_portfolio_positions
                   WHERE account=? AND trade_date=? ORDER BY stock_code""",
                (account, latest_date[0]),
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_positions(self, trade_date: str, account: str, positions: list[dict]):
        with self._conn() as conn:
            for p in positions:
                p["trade_date"] = trade_date
                p["account"] = account
                p["created_at"] = datetime.now().isoformat()
                validate_cols(_POSITION_COLS, p.keys())
                cols = ", ".join(p.keys())
                placeholders = ", ".join(["?" for _ in p])
                vals = [round_val(v) for v in p.values()]
                conn.execute(
                    f"INSERT OR REPLACE INTO trade_portfolio_positions ({cols}) VALUES ({placeholders})",
                    vals,
                )
            conn.commit()
