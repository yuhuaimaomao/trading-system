"""pytest 共享 fixtures — 所有测试使用独立的临时数据库"""

import os
import sqlite3
import pytest
from pathlib import Path


def _init_test_db(db_path: str):
    """在测试 DB 中创建策略管线所需的核心表。"""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_basic (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            price REAL, open REAL, high REAL, low REAL, prev_close REAL,
            change_pct REAL, total_market_cap REAL, circ_market_cap REAL,
            turnover_rate REAL, volume_ratio REAL, amplitude REAL, volume REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma5_angle REAL,
            industry TEXT, main_force_net REAL, main_force_ratio REAL,
            super_large_net REAL, large_net REAL, medium_net REAL, small_net REAL,
            avg_vol_5d REAL, avg_vol_20d REAL,
            pe_ttm REAL, pb_ratio REAL, revenue_growth REAL, profit_growth REAL
        );
        CREATE TABLE IF NOT EXISTS sector_stocks (
            stock_code TEXT, sector_code TEXT
        );
        CREATE TABLE IF NOT EXISTS sector_hot_history (
            trade_date TEXT, sector_type TEXT, rank INTEGER,
            sector_code TEXT, sector_name TEXT, hot_score REAL
        );
        CREATE TABLE IF NOT EXISTS sector_industry (
            trade_date TEXT, sector_code TEXT, change_percent REAL,
            sector_name TEXT, main_force_net REAL
        );
        CREATE TABLE IF NOT EXISTS sector_concept (
            trade_date TEXT, sector_code TEXT, change_percent REAL,
            sector_name TEXT, main_force_net REAL
        );
        CREATE TABLE IF NOT EXISTS limit_pool (
            trade_date TEXT, stock_code TEXT, pool_type TEXT
        );
        CREATE TABLE IF NOT EXISTS index_realtime_data (
            index_code TEXT, trade_date TEXT, trade_time TEXT, change_percent REAL
        );
        CREATE TABLE IF NOT EXISTS stock_indicators (
            trade_date TEXT, stock_code TEXT, bbi_weekly REAL,
            macd_dif REAL, macd_dea REAL, macd_bar REAL,
            rsi6 REAL, rsi12 REAL, rsi24 REAL,
            kdj_k REAL, kdj_d REAL, kdj_j REAL,
            bb_upper REAL, bb_mid REAL, bb_lower REAL, bb_width REAL, bb_pct_b REAL
        );
        CREATE TABLE IF NOT EXISTS regulatory_letter (
            trade_date TEXT, stock_code TEXT, risk_level INTEGER,
            risk_type TEXT, title TEXT
        );
        CREATE TABLE IF NOT EXISTS cls_telegraph (
            trade_date TEXT, ctime TEXT, ai_status TEXT, ai_sentiment TEXT,
            ai_stocks TEXT, ai_summary TEXT
        );
        CREATE TABLE IF NOT EXISTS market_breadth (
            trade_date TEXT, up_count INTEGER, down_count INTEGER,
            flat_count INTEGER, limit_up_count INTEGER, limit_down_count INTEGER,
            index_change_pct REAL, market_state TEXT
        );
        CREATE TABLE IF NOT EXISTS trade_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, created_at TEXT, signal_type TEXT, signal_source TEXT,
            stock_code TEXT, stock_name TEXT,
            buy_zone_min REAL, buy_zone_max REAL,
            target_position REAL, stop_loss REAL, take_profit REAL,
            trailing_stop REAL, signal_score REAL, strategy_name TEXT,
            reason TEXT, status TEXT DEFAULT 'pending', executed_at TEXT,
            account TEXT DEFAULT 'paper', expected_trend TEXT
        );
        CREATE TABLE IF NOT EXISTS trade_holdings_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, created_at TEXT, stock_code TEXT,
            account TEXT DEFAULT 'paper', action TEXT,
            new_stop_loss REAL, new_take_profit REAL,
            expected_holding_days INTEGER, tomorrow_outlook TEXT,
            reason TEXT, applied INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS trade_portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, total_value REAL, cash REAL, market_value REAL,
            daily_pnl REAL, total_pnl REAL, drawdown REAL,
            position_count INTEGER, sector_exposure TEXT, created_at TEXT,
            account TEXT DEFAULT 'real'
        );
        CREATE TABLE IF NOT EXISTS trade_portfolio_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, account TEXT DEFAULT 'paper',
            stock_code TEXT, stock_name TEXT, volume INTEGER,
            avg_cost REAL, current_price REAL, market_value REAL,
            pnl REAL, pnl_pct REAL, stop_loss REAL, take_profit REAL,
            holding_days INTEGER DEFAULT 0, sector_code TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS strategy_funnel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            push_date TEXT, trade_date TEXT, stock_code TEXT, stock_name TEXT,
            rank_position INTEGER, raw_snapshot TEXT, factors_passed TEXT,
            factors_detail TEXT, scenarios TEXT, trend_mode TEXT,
            score REAL, open_price REAL, close_price REAL,
            day_change_pct REAL, bought INTEGER DEFAULT 0, buy_price REAL,
            day_pnl_pct REAL, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS strategy_ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            push_date TEXT, trade_date TEXT, stock_code TEXT, stock_name TEXT,
            rank_in_prompt INTEGER, verdict TEXT, confidence TEXT,
            what_i_see TEXT, what_concerns_me TEXT, decisive_factor TEXT,
            skip_reason TEXT, would_reconsider_if TEXT,
            buy_zone_min REAL, buy_zone_max REAL, stop_loss REAL, take_profit REAL,
            pricing_logic TEXT, signal_id INTEGER,
            day_change_pct REAL, day_pnl_pct REAL, created_at TEXT
        );
    """)
    conn.commit()
    conn.close()


def seed_stock_basic(db_path: str, trade_date: str, stocks: list[dict]):
    """向测试 DB 写入 stock_basic 种子数据。"""
    conn = sqlite3.connect(db_path)
    cols = [
        "trade_date", "stock_code", "stock_name", "price", "open", "high", "low",
        "prev_close", "change_pct", "total_market_cap", "circ_market_cap",
        "turnover_rate", "volume_ratio", "amplitude", "volume", "ma5", "ma10", "ma20",
        "ma5_angle", "industry", "main_force_net", "main_force_ratio",
        "super_large_net", "large_net", "medium_net", "small_net",
        "avg_vol_5d", "avg_vol_20d",
    ]
    placeholders = ",".join(["?" for _ in cols])
    sql = f"INSERT OR REPLACE INTO stock_basic ({','.join(cols)}) VALUES ({placeholders})"
    for s in stocks:
        conn.execute(sql, [s.get(c) for c in cols])
    conn.commit()
    conn.close()


def _make_bull_stock(code: str, name: str, industry: str = "半导体",
                     price: float = 25.0, change_pct: float = 3.0,
                     mcap: float = 200 * 1e8) -> dict:
    """构造一个趋势向上的种子股票。"""
    return {
        "trade_date": "2026-05-25",
        "stock_code": code, "stock_name": name,
        "price": price, "open": price - 0.3, "high": price + 0.5,
        "low": price - 0.5, "prev_close": price - 0.5,
        "change_pct": change_pct,
        "total_market_cap": mcap, "circ_market_cap": mcap * 0.6,
        "turnover_rate": 2.5, "volume_ratio": 1.8, "amplitude": 2.5, "volume": 15000000,
        "ma5": price - 0.5, "ma10": price - 1.0, "ma20": price - 1.5,
        "ma5_angle": 2.0, "industry": industry,
        "main_force_net": 5000000,"main_force_ratio": 4.0,
        "super_large_net": 2000000, "large_net": 1000000,
        "medium_net": 500000, "small_net": -500000,
        "avg_vol_5d": 12000000, "avg_vol_20d": 9000000,
    }


@pytest.fixture
def test_db_path(tmp_path):
    """创建带核心表的临时测试数据库，写入种子数据后返回路径。"""
    db_path = str(tmp_path / "test_market.db")
    _init_test_db(db_path)

    # 写入足够的种子股票数据（涨跌各半，模拟 A 股规模）
    stocks = []
    for i in range(3000):
        stocks.append(_make_bull_stock(f"{600000 + i:06d}", f"测试股{i}",
                                       price=20 + (i % 50), change_pct=2.0 + (i % 5),
                                       mcap=100 * 1e8 + i * 1e8))
    for i in range(2000):
        code = f"{300000 + i:06d}"
        stocks.append({
            "trade_date": "2026-05-25",
            "stock_code": code, "stock_name": f"测试股{3000 + i}",
            "price": 15.0, "open": 15.5, "high": 15.8, "low": 14.8,
            "prev_close": 15.5, "change_pct": -2.0,
            "total_market_cap": 80 * 1e8 + i * 1e8,
            "circ_market_cap": 50 * 1e8,
            "turnover_rate": 1.0, "volume_ratio": 0.6, "amplitude": 3.0, "volume": 8000000,
            "ma5": 16.0, "ma10": 16.5, "ma20": 17.0,
            "ma5_angle": -1.5, "industry": "房地产",
            "main_force_net": -3000000, "main_force_ratio": -2.0,
            "super_large_net": -1000000, "large_net": -1000000,
            "medium_net": -500000, "small_net": 1000000,
            "avg_vol_5d": 8000000, "avg_vol_20d": 10000000,
        })
    # 科创板（688开头，应被过滤）
    stocks.append(_make_bull_stock("688001", "科创测试", price=50, mcap=150 * 1e8))
    # ST 股（应被过滤）
    st = _make_bull_stock("600800", "ST测试", price=5, mcap=30 * 1e8, change_pct=1.0)
    st["stock_name"] = "*ST测试"
    stocks.append(st)
    # ProfileBuilder 测试用的固定 code
    stocks.append(_make_bull_stock("000001", "平安银行", industry="银行Ⅱ", price=10.68, change_pct=0, mcap=2072 * 1e8))
    stocks.append(_make_bull_stock("000002", "万科A", industry="房地产", price=15.0, change_pct=1.0))
    # 涨停股
    stocks.append(_make_bull_stock("601000", "涨停测试", price=22.0, change_pct=10.0, mcap=200 * 1e8))
    # 跌停股（应被硬关卡过滤）
    stocks.append(_make_bull_stock("601001", "跌停测试", price=18.0, change_pct=-10.0, mcap=200 * 1e8))

    seed_stock_basic(db_path, "2026-05-25", stocks)

    # 写入 sector_stocks（给涨停测试股分配板块）
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO sector_stocks VALUES ('601000', 'BK0001')")
    conn.execute("INSERT INTO sector_stocks VALUES ('600000', 'BK0001')")
    # sector_hot_history: 让 BK0001 上榜
    conn.execute("INSERT INTO sector_hot_history VALUES ('2026-05-25', 'concept', 2, 'BK0001', '测试板块', 85)")
    conn.execute("INSERT INTO sector_concept VALUES ('2026-05-25', 'BK0001', 1.5, '测试概念板块', 2000000)")
    conn.commit()
    conn.close()

    # 强制测试隔离：TradeRepository() 不带 db_path 直接报错
    os.environ["E2E_TEST_MODE"] = "1"
    return db_path
