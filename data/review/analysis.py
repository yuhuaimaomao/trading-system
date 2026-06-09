"""
复盘分析查询 — 宏观、指数、涨停表现、电报等分析用查询。

复盘线数据层。
"""


class AnalysisReader:
    """复盘分析数据读取器（静态方法，传入 conn）"""

    @staticmethod
    def get_macro_daily(conn) -> dict | None:
        """查询最新宏观数据。"""
        row = conn.execute("SELECT * FROM macro_daily ORDER BY trade_date DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_index_history(conn, index_code: str = None) -> list:
        """查询指数历史数据。"""
        if index_code:
            rows = conn.execute(
                "SELECT index_code, trade_date, close_price "
                "FROM index_realtime_data WHERE index_code = ? "
                "ORDER BY trade_date",
                (index_code,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT index_code, trade_date, close_price FROM index_realtime_data ORDER BY trade_date"
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_index_latest(conn) -> list:
        """查询最新指数快照。"""
        rows = conn.execute(
            "SELECT index_code, index_name, close_price, open_price, high_price, low_price, "
            "change_percent, change_amount, up_count, down_count "
            "FROM index_realtime_data ORDER BY index_code"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_date_row_counts(conn, trade_date: str) -> dict:
        """查某日各表行数（数据完整性检查）。"""
        tables = {
            "stock_basic": "SELECT COUNT(*) FROM stock_basic WHERE trade_date = ?",
            "sector_industry": "SELECT COUNT(*) FROM sector_industry WHERE trade_date = ?",
            "sector_concept": "SELECT COUNT(*) FROM sector_concept WHERE trade_date = ?",
        }
        result = {}
        for name, sql in tables.items():
            row = conn.execute(sql, (trade_date,)).fetchone()
            result[name] = row[0] if row else 0
        return result

    @staticmethod
    def get_limit_pool_stats(conn, trade_date: str, prev_date: str = None) -> dict:
        """涨停池统计（当日 + 昨日对比）。"""
        today = {}
        for pool_type in ("涨停", "炸板", "跌停"):
            row = conn.execute(
                "SELECT pool_type, COUNT(*) as cnt FROM limit_pool WHERE trade_date = ? AND pool_type = ?",
                (trade_date, pool_type),
            ).fetchone()
            today[pool_type] = row["cnt"] if row else 0

        result = {"today": today}

        if prev_date:
            prev = {}
            for pool_type in ("涨停", "炸板", "跌停"):
                row = conn.execute(
                    "SELECT pool_type, COUNT(*) as cnt FROM limit_pool WHERE trade_date = ? AND pool_type = ?",
                    (prev_date, pool_type),
                ).fetchone()
                prev[pool_type] = row["cnt"] if row else 0
            result["prev"] = prev

        return result

    @staticmethod
    def get_limit_pool_codes(conn, trade_date: str) -> list:
        """查某日涨停股代码列表。"""
        rows = conn.execute(
            "SELECT stock_code FROM limit_pool WHERE trade_date = ?",
            (trade_date,),
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_limit_pool_detail(conn, trade_date: str) -> list:
        """涨停池明细（含封板时间、开板次数）。"""
        rows = conn.execute(
            "SELECT stock_code, first_seal_time, last_seal_time, open_count "
            "FROM limit_pool WHERE trade_date = ? AND pool_type = '涨停'",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_limit_pool_consecutive(conn, trade_date: str) -> list:
        """查连板股。"""
        rows = conn.execute(
            "SELECT stock_code, consecutive_boards FROM limit_pool "
            "WHERE trade_date = ? AND pool_type = '涨停' AND consecutive_boards >= 2",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_frequent_limit_stocks(conn, trade_date: str, lookback_days: int = 30) -> list:
        """查近N天内多次涨停的股票。"""
        rows = conn.execute(
            "SELECT stock_code, COUNT(*) as freq FROM limit_pool "
            "WHERE trade_date <= ? AND pool_type = '涨停' "
            "GROUP BY stock_code HAVING COUNT(*) >= 2 "
            "ORDER BY freq DESC",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_yzt_performance_detail(conn, trade_date: str) -> list:
        """查询昨日涨停今日表现详情。"""
        rows = conn.execute(
            "SELECT stock_code, stock_name, close_price, change_percent, "
            "yesterday_board_count, yesterday_seal_time, industry "
            "FROM yesterday_zt_performance WHERE trade_date = ? "
            "ORDER BY change_percent DESC",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_lhb_detail(conn, trade_date: str) -> list:
        """查龙虎榜明细。"""
        rows = conn.execute(
            "SELECT ls.stock_code, ls.seat_name, ls.buy_amount, ls.sell_amount, "
            "ls.is_institution, ls.is_hot_money, ls.seat_type "
            "FROM lhb_seats ls WHERE ls.trade_date = ? "
            "ORDER BY ls.stock_code, ls.buy_amount DESC",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_shareholder_changes(conn, trade_date: str) -> list:
        """查股东变更信息。"""
        rows = conn.execute(
            "SELECT stock_code, stock_name, holder_name, change_type, "
            "change_amount, change_ratio, announcement_date "
            "FROM share_holder_change WHERE trade_date = ?",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_stock_monitor(conn, trade_date: str) -> list:
        """查异动监控。"""
        rows = conn.execute(
            "SELECT stock_code, stock_name, monitor_type, trigger_rule, status FROM stock_monitor WHERE trade_date = ?",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_notices(conn, trade_date: str) -> list:
        """查公告信息。"""
        rows = conn.execute(
            "SELECT stock_code, stock_name, announcement_title, announcement_type "
            "FROM future_announcements WHERE trade_date = ? "
            "ORDER BY announcement_type",
            (trade_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_telegraph(conn, trade_date: str, limit: int = 20) -> list:
        """查财联电报。"""
        rows = conn.execute(
            "SELECT * FROM cls_telegraph WHERE trade_date = ? ORDER BY ctime DESC LIMIT ?",
            (trade_date, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def upsert_sector_hot(conn, trade_date: str, sector_type: str, rows_data: list):
        """批量写入板块热度历史。"""
        for r in rows_data:
            conn.execute(
                "INSERT OR REPLACE INTO sector_hot_history "
                "(trade_date, sector_code, sector_name, sector_type, hot_score, rank) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    trade_date,
                    r.get("sector_code", ""),
                    r.get("name", ""),
                    sector_type,
                    r.get("hot_score", 0),
                    r.get("rank", 0),
                ),
            )

    @staticmethod
    def get_market_turnover(conn, trade_date: str) -> float:
        """查全市场成交额。"""
        row = conn.execute(
            "SELECT SUM(turnover)/100000000 as prev_turnover FROM stock_basic WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        return (row[0] or 0) if row else 0
