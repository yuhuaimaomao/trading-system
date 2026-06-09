"""
早报数据查询

策略线 — 早盘简报用到的数据查询。
"""


class MorningReader:
    """早报数据读取器（静态方法，传入 conn）"""

    @staticmethod
    def get_macro_latest(conn) -> dict | None:
        """查询最新宏观数据。"""
        row = conn.execute("SELECT * FROM macro_daily ORDER BY trade_date DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_telegraph_today(conn, trade_date: str) -> list:
        """查询当日电报。"""
        rows = conn.execute(
            "SELECT title, score, plate_tags, subject_tags, ctime "
            "FROM cls_telegraph WHERE trade_date = ? "
            "ORDER BY score DESC LIMIT 30",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_stock_tracker_codes(conn, trade_date: str) -> list:
        """查询当日追踪的股票代码。"""
        rows = conn.execute(
            "SELECT stock_code, stock_name FROM stock_tracker "
            "WHERE push_date = (SELECT MAX(push_date) FROM stock_tracker WHERE source='早报')",
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def cancel_signal(conn, signal_id: int):
        """取消信号。"""
        conn.execute(
            "UPDATE trade_signals SET status='cancelled', executed_at=datetime('now') WHERE id=?",
            (signal_id,),
        )

    @staticmethod
    def update_signal_fields(conn, signal_id: int, sets: dict):
        """更新信号字段。"""
        set_str = ", ".join(f"{k}=?" for k in sets)
        vals = list(sets.values()) + [signal_id]
        conn.execute(f"UPDATE trade_signals SET {set_str} WHERE id=?", vals)

    @staticmethod
    def update_signal_score(conn, signal_id: int, score: float, reason: str):
        """更新信号评分。"""
        conn.execute(
            "UPDATE trade_signals SET signal_score=?, reason=? WHERE id=?",
            (score, reason, signal_id),
        )

    @staticmethod
    def upsert_sector_bias(conn, trade_date: str, sector_name: str, data: dict):
        """写入板块偏差。"""
        conn.execute(
            "INSERT OR REPLACE INTO morning_sector_bias "
            "(trade_date, sector_name, bias, priority, size_multiplier, stock_codes, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                trade_date,
                sector_name,
                data.get("bias", ""),
                data.get("priority", 0),
                data.get("size_multiplier", 1.0),
                data.get("stock_codes", ""),
                data.get("reason", ""),
            ),
        )
