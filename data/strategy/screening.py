"""
筛选因子数据查询

策略线 — breadth/trend/profiles 筛选因子用到的数据库查询。
"""


class ScreeningReader:
    """筛选数据读取器（静态方法，传入 conn）"""

    # ── breadth ──

    @staticmethod
    def get_breadth_data(conn, trade_date: str) -> dict | None:
        """查询广度数据：涨跌停数、MA5上方比例、均线多头比例、量比>1.5比例。"""
        row = conn.execute(
            """SELECT
                COUNT(CASE WHEN change_pct > 0 THEN 1 END) as up,
                COUNT(CASE WHEN change_pct < 0 THEN 1 END) as down,
                COUNT(CASE WHEN change_pct >= 9.5 THEN 1 END) as limit_up,
                COUNT(CASE WHEN change_pct <= -9.5 THEN 1 END) as limit_down,
                AVG(CASE WHEN price > ma5 THEN 1.0 ELSE 0.0 END) as above_ma5,
                AVG(CASE WHEN ma5 > ma20 THEN 1.0 ELSE 0.0 END) as ma_bull,
                AVG(CASE WHEN volume_ratio > 1.5 THEN 1.0 ELSE 0.0 END) as vol_expand
            FROM stock_basic WHERE trade_date = ?
              AND stock_name NOT LIKE '%ST%' AND stock_code NOT LIKE '688%'""",
            (trade_date,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_breadth_industry(conn, trade_date: str) -> dict | None:
        """行业板块广度。"""
        row = conn.execute(
            """SELECT
                AVG(CASE WHEN change_percent > 0 THEN 1.0 ELSE 0.0 END) as up_ratio,
                AVG(change_percent) as avg_change
            FROM sector_industry WHERE trade_date = ?""",
            (trade_date,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_index_change(conn, trade_date: str) -> float:
        """查上证指数当日涨跌幅。"""
        row = conn.execute(
            "SELECT change_percent FROM index_realtime_data WHERE index_code = 'sh000001' AND trade_date = ?",
            (trade_date,),
        ).fetchone()
        return (row[0] or 0) if row else 0

    @staticmethod
    def insert_breadth(conn, data: dict):
        """写入市场广度记录。"""
        cols = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        conn.execute(
            f"INSERT OR REPLACE INTO market_breadth ({cols}) VALUES ({placeholders})",
            list(data.values()),
        )

    @staticmethod
    def get_breadth_record(conn, trade_date: str) -> dict | None:
        """查某日广度记录。"""
        row = conn.execute("SELECT * FROM market_breadth WHERE trade_date = ?", (trade_date,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_breadth_market_state(conn, trade_date: str) -> str | None:
        """查某日市场状态。"""
        row = conn.execute(
            "SELECT market_state FROM market_breadth WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def get_breadth_history(conn, limit: int = 20) -> list:
        """查询广度历史。"""
        rows = conn.execute(
            "SELECT index_change_pct FROM market_breadth ORDER BY trade_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_max_limit_down(conn) -> float:
        """查历史最大跌停数。"""
        row = conn.execute("SELECT MAX(limit_down_count) FROM market_breadth").fetchone()
        return (row[0] or 0) if row else 0

    @staticmethod
    def get_recent_trade_dates(conn, limit: int = 5) -> list:
        """查最近交易日列表。"""
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM stock_basic ORDER BY trade_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    @staticmethod
    def get_dt_count(conn, trade_date: str) -> int:
        """查某日跌停数。"""
        row = conn.execute(
            "SELECT COUNT(*) FROM limit_pool WHERE trade_date=? AND pool_type='跌停'",
            (trade_date,),
        ).fetchone()
        return row[0] if row else 0

    # ── trend ──

    @staticmethod
    def get_sector_stocks_batch(conn, codes: list[str]) -> dict[str, list]:
        """批量查股票所属板块。返回 {code: [sector_code, ...]}。"""
        if not codes:
            return {}
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT stock_code, sector_code FROM sector_stocks WHERE stock_code IN ({placeholders})",
            codes,
        ).fetchall()
        result = {}
        for r in rows:
            result.setdefault(r[0], []).append(r[1])
        return result

    @staticmethod
    def get_sector_changes(conn, trade_date: str, sector_codes: list) -> dict[str, float]:
        """批量查板块涨跌幅。"""
        if not sector_codes:
            return {}
        placeholders = ",".join("?" * len(sector_codes))
        result = {}
        for table in ("sector_industry", "sector_concept"):
            rows = conn.execute(
                f"SELECT sector_code, change_percent FROM {table} "
                f"WHERE trade_date=? AND sector_code IN ({placeholders})",
                [trade_date] + sector_codes,
            ).fetchall()
            for r in rows:
                if r[0] not in result:
                    result[r[0]] = r[1] or 0
        return result

    @staticmethod
    def get_sector_hot_counts(conn, sector_codes: list) -> dict[str, int]:
        """批量查板块热度上榜次数。"""
        if not sector_codes:
            return {}
        placeholders = ",".join("?" * len(sector_codes))
        rows = conn.execute(
            f"SELECT sector_code, COUNT(*) as cnt FROM sector_hot_history "
            f"WHERE sector_code IN ({placeholders}) GROUP BY sector_code",
            sector_codes,
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    @staticmethod
    def get_sector_money_flow(conn, trade_date: str, sector_codes: list) -> dict[str, float]:
        """批量查板块主力净流入。"""
        if not sector_codes:
            return {}
        placeholders = ",".join("?" * len(sector_codes))
        result = {}
        for table in ("sector_industry", "sector_concept"):
            rows = conn.execute(
                f"SELECT sector_code, main_force_net FROM {table} "
                f"WHERE trade_date=? AND sector_code IN ({placeholders})",
                [trade_date] + sector_codes,
            ).fetchall()
            for r in rows:
                if r[0] not in result:
                    result[r[0]] = (r[1] or 0) / 100000000
        return result

    @staticmethod
    def get_regulatory_codes(conn, codes: list[str]) -> set[str]:
        """查有监管风险的股票代码集合。"""
        if not codes:
            return set()
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT DISTINCT stock_code FROM regulatory_letter WHERE stock_code IN ({placeholders})",
            codes,
        ).fetchall()
        return {r[0] for r in rows}

    @staticmethod
    def get_telegraph_tags(conn) -> list:
        """查电报标签。"""
        rows = conn.execute(
            "SELECT stock_tags, title FROM cls_telegraph WHERE ctime >= date('now', '-3 days')"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_stock_bbi_weekly(conn, codes: list[str]) -> dict[str, float]:
        """批量查周线BBI。"""
        if not codes:
            return {}
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT stock_code, bbi_weekly FROM stock_indicators "
            f"WHERE stock_code IN ({placeholders}) "
            f"ORDER BY trade_date DESC",
            codes,
        ).fetchall()
        result = {}
        for r in rows:
            if r[0] not in result:
                result[r[0]] = r[1] or 0
        return result

    @staticmethod
    def get_stock_price_history(conn, code: str, limit: int = 60) -> list:
        """查个股历史价量。"""
        rows = conn.execute(
            "SELECT trade_date, price, open, high, low, prev_close, "
            "volume, turnover_rate, volume_ratio "
            "FROM stock_basic WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT ?",
            (code, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── profiles ──

    @staticmethod
    def get_stock_today_profile(conn, code: str, trade_date: str) -> dict | None:
        """查个股今日盘口数据。"""
        row = conn.execute(
            "SELECT price, open, high, low, change_pct, volume_ratio, amplitude, "
            "turnover_rate, total_market_cap, circ_market_cap, "
            "main_force_net, main_force_ratio, "
            "ma5, ma10, ma20, ma5_angle, "
            "pe_ttm, pb_ratio, revenue_growth, profit_growth "
            "FROM stock_basic WHERE stock_code=? AND trade_date=?",
            (code, trade_date),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_stock_history_for_profile(conn, code: str, limit: int = 60) -> list:
        """查个股历史（盘口分析用）。"""
        rows = conn.execute(
            "SELECT trade_date, price, open, high, low, prev_close, "
            "volume, turnover_rate, volume_ratio, change_pct "
            "FROM stock_basic WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT ?",
            (code, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_stock_count(conn, trade_date: str) -> int:
        """查某日股票总数。"""
        row = conn.execute(
            "SELECT COUNT(DISTINCT stock_code) FROM stock_basic WHERE trade_date=?",
            (trade_date,),
        ).fetchone()
        return row[0] if row else 0

    @staticmethod
    def get_stock_change_rank(conn, trade_date: str, change_pct: float) -> int:
        """查某涨跌幅的排名。"""
        row = conn.execute(
            "SELECT COUNT(*) + 1 FROM stock_basic WHERE trade_date=? AND change_pct > ?",
            (trade_date, change_pct),
        ).fetchone()
        return row[0] if row else 1

    @staticmethod
    def get_stock_indicators_current(conn, code: str) -> dict | None:
        """查个股最新技术指标。"""
        row = conn.execute(
            "SELECT macd_dif, macd_dea, macd_bar, "
            "kdj_k, kdj_d, kdj_j, "
            "rsi6, rsi12, rsi24, "
            "bb_upper, bb_mid, bb_lower, bbi_daily "
            "FROM stock_indicators WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1",
            (code,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_stock_indicators_prev(conn, code: str) -> dict | None:
        """查个股前一交易日技术指标。"""
        row = conn.execute(
            "SELECT macd_dif, macd_dea, macd_bar, "
            "kdj_k, kdj_d, kdj_j "
            "FROM stock_indicators WHERE stock_code=? "
            "ORDER BY trade_date DESC LIMIT 1 OFFSET 1",
            (code,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_vs_peers(conn, trade_date: str, industry: str, code: str, limit: int = 10) -> list:
        """查同行业对比（按涨幅排序）。"""
        rows = conn.execute(
            "SELECT stock_code, stock_name, change_pct, total_market_cap/100000000 as mcap, "
            "turnover_rate, volume_ratio, main_force_net/10000 as mf_wan "
            "FROM stock_basic WHERE trade_date=? AND industry=? AND stock_code!=? "
            "ORDER BY change_pct DESC LIMIT ?",
            (trade_date, industry, code, limit),
        ).fetchall()
        return [dict(r) for r in rows]
