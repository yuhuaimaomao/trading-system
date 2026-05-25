"""交易系统专用表结构"""

import sqlite3
from system.config.settings import DATABASE_PATH


def ensure_tables():
    """创建 trade_ 前缀的交易系统表（幂等）"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS trade_factor_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            factor_name TEXT NOT NULL,
            factor_value REAL,
            factor_zscore REAL,
            updated_at TEXT,
            UNIQUE(trade_date, stock_code, factor_name)
        );

        CREATE TABLE IF NOT EXISTS trade_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            signal_source TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            buy_zone_min REAL,
            buy_zone_max REAL,
            target_position REAL,
            stop_loss REAL,
            take_profit REAL,
            trailing_stop REAL,
            signal_score REAL,
            strategy_name TEXT,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            executed_at TEXT,
            UNIQUE(trade_date, stock_code, created_at)
        );

        CREATE TABLE IF NOT EXISTS trade_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER REFERENCES trade_signals(id),
            trade_date TEXT NOT NULL,
            order_time TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            order_type TEXT NOT NULL,
            order_price REAL,
            order_volume INTEGER,
            price_type TEXT DEFAULT 'limit',
            order_status TEXT DEFAULT 'pending',
            filled_volume INTEGER DEFAULT 0,
            filled_price REAL,
            filled_amount REAL,
            commission REAL,
            qmt_order_id TEXT,
            reject_reason TEXT,
            strategy_name TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS trade_portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            total_value REAL,
            cash REAL,
            market_value REAL,
            daily_pnl REAL,
            total_pnl REAL,
            drawdown REAL,
            position_count INTEGER,
            sector_exposure TEXT,
            created_at TEXT,
            UNIQUE(trade_date)
        );

        CREATE TABLE IF NOT EXISTS trade_strategy_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL,
            version TEXT,
            start_date TEXT,
            end_date TEXT,
            total_trades INTEGER,
            win_rate REAL,
            avg_profit REAL,
            avg_loss REAL,
            profit_loss_ratio REAL,
            max_drawdown REAL,
            sharpe_ratio REAL,
            total_return REAL,
            benchmark_return REAL,
            alpha REAL,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_trade_factor_values_date
            ON trade_factor_values(trade_date);
        CREATE INDEX IF NOT EXISTS idx_trade_factor_values_stock
            ON trade_factor_values(trade_date, stock_code);
        CREATE INDEX IF NOT EXISTS idx_trade_signals_date
            ON trade_signals(trade_date);
        CREATE INDEX IF NOT EXISTS idx_trade_orders_date
            ON trade_orders(trade_date);
    """)

    conn.commit()

    # 添加 account 字段（幂等迁移）
    for table in ["trade_signals", "trade_orders", "trade_portfolio_snapshots",
                  "trade_factor_values", "trade_strategy_metrics"]:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN account TEXT DEFAULT 'real'")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()
