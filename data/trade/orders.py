"""trade_orders 表数据访问

交易线 — 订单 CRUD。
"""

from data._base import BaseRepository, cols_from_str

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
        cols = cols_from_str(_ORDER_ALL_COLS.replace(" ", ""))
        with self._conn() as conn:
            if account:
                rows = conn.execute(
                    f"SELECT {_ORDER_ALL_COLS} FROM trade_orders WHERE trade_date=? AND account=? ORDER BY order_time",
                    (trade_date, account),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_ORDER_ALL_COLS} FROM trade_orders WHERE trade_date=? ORDER BY order_time",
                    (trade_date,),
                ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def get_sold_codes(self, codes: list[str], account: str) -> set[str]:
        """返回 codes 中有 sell 成交记录的代码集合（不限日期）。"""
        if not codes:
            return set()
        placeholders = ",".join("?" * len(codes))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT stock_code FROM trade_orders "
                f"WHERE stock_code IN ({placeholders}) "
                "AND order_type='sell' AND order_status='filled' AND account=?",
                (*codes, account),
            ).fetchall()
        return {r[0] for r in rows}
