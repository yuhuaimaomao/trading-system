"""trade_orders 表数据访问"""

from data.repo.repo_base import BaseRepository, _cols_from_str

_ORDER_COLS = frozenset(
    {
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
    }
)

_ORDER_ALL_COLS = (
    "id, signal_id, trade_date, order_time, stock_code, order_type, "
    "order_price, order_volume, price_type, order_status, filled_volume, "
    "filled_price, filled_amount, commission, qmt_order_id, reject_reason, "
    "strategy_name, updated_at, account"
)


class OrderRepo(BaseRepository):
    """trade_orders 表 CRUD"""

    def insert(self, order_dict: dict) -> int:
        return self._insert("trade_orders", order_dict, _ORDER_COLS)

    def get_by_date(self, trade_date: str, account: str = None) -> list[dict]:
        cols = _cols_from_str(_ORDER_ALL_COLS.replace(" ", ""))
        with self._conn() as conn:
            if account:
                rows = conn.execute(
                    f"SELECT {_ORDER_ALL_COLS} FROM trade_orders "
                    "WHERE trade_date=? AND account=? ORDER BY order_time",
                    (trade_date, account),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_ORDER_ALL_COLS} FROM trade_orders "
                    "WHERE trade_date=? ORDER BY order_time",
                    (trade_date,),
                ).fetchall()
        return [dict(zip(cols, row)) for row in rows]
