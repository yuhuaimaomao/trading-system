"""共享 fixtures：内存数据库 + 样本数据"""

import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def db_path():
    """临时 SQLite 数据库，测试后自动清理"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    _init_test_db(path)
    yield path
    Path(path).unlink(missing_ok=True)


def _init_test_db(path: str):
    """创建测试所需的最小表结构（与生产 schema 一致）"""
    conn = sqlite3.connect(path)
    conn.executescript("""
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
            account TEXT DEFAULT 'paper',
            expected_trend TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS trade_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
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
            updated_at TEXT,
            account TEXT DEFAULT 'paper'
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
            account TEXT DEFAULT 'paper',
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS trade_portfolio_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            account TEXT DEFAULT 'paper',
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            volume INTEGER,
            avg_cost REAL,
            current_price REAL,
            market_value REAL,
            pnl REAL,
            pnl_pct REAL,
            pre_close REAL DEFAULT 0,
            daily_pnl REAL DEFAULT 0,
            entry_date TEXT DEFAULT '',
            locked_volume INTEGER DEFAULT 0,
            stop_loss REAL,
            take_profit REAL,
            holding_days INTEGER DEFAULT 0,
            sector_code TEXT,
            created_at TEXT,
            UNIQUE(trade_date, account, stock_code)
        );
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def sample_signal():
    """标准买入信号"""
    from datetime import datetime

    return {
        "trade_date": "2026-06-01",
        "created_at": datetime.now().isoformat(),
        "signal_type": "BUY",
        "stock_code": "002371",
        "stock_name": "北方华创",
        "buy_zone_min": 380.0,
        "buy_zone_max": 400.0,
        "stop_loss": 370.0,
        "take_profit": 440.0,
        "trailing_stop": 0.05,
        "signal_score": 75,
        "signal_source": "AI_ENHANCED",
    }
