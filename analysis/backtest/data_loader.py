# -*- coding: utf-8 -*-
"""历史数据加载器 — 从 stock_basic 表加载日线数据"""

import sqlite3

from system.config.settings import DATABASE_PATH


class DataLoader:
    """从 SQLite 加载回测所需历史数据"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DATABASE_PATH

    def load_daily(self, stock_codes: list[str], start_date: str,
                   end_date: str):
        """加载日线数据

        Returns: pd.DataFrame with columns:
            trade_date, stock_code, open, high, low, close,
            volume, turnover_rate, change_pct
        """
        import pandas as pd

        placeholders = ",".join(["?" for _ in stock_codes])
        sql = f"""
            SELECT trade_date, stock_code,
                   open, high, low, price AS close,
                   volume, turnover_rate, change_pct
            FROM stock_basic
            WHERE stock_code IN ({placeholders})
              AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date, stock_code
        """
        params = list(stock_codes) + [start_date, end_date]
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df

    def load_prices(self, stock_codes: list[str], start_date: str,
                    end_date: str):
        """加载价格数据，返回 pivot table: index=date, columns=stock_code, values=close"""
        df = self.load_daily(stock_codes, start_date, end_date)
        if df.empty:
            return df
        return df.pivot_table(index="trade_date", columns="stock_code", values="close")
