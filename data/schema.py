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
            UNIQUE(trade_date, stock_code)
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
            account TEXT DEFAULT 'real',
            UNIQUE(trade_date, account)
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
            stop_loss REAL,
            take_profit REAL,
            holding_days INTEGER DEFAULT 0,
            sector_code TEXT,
            created_at TEXT,
            UNIQUE(trade_date, account, stock_code)
        );

        CREATE TABLE IF NOT EXISTS trade_holdings_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            account TEXT DEFAULT 'paper',
            action TEXT NOT NULL,
            new_stop_loss REAL,
            new_take_profit REAL,
            expected_holding_days INTEGER,
            tomorrow_outlook TEXT,
            reason TEXT,
            applied INTEGER DEFAULT 0,
            UNIQUE(trade_date, stock_code, account)
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
    for table in [
        "trade_signals",
        "trade_orders",
        "trade_portfolio_snapshots",
        "trade_factor_values",
        "trade_strategy_metrics",
    ]:
        try:
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN account TEXT DEFAULT 'real'"
            )
        except sqlite3.OperationalError:
            pass

    # cls_telegraph AI 结构化字段（幂等迁移）
    for col, col_type in [
        ("ai_summary", "TEXT"),
        ("ai_sentiment", "TEXT"),
        ("ai_impact", "TEXT"),
        ("ai_stocks", "TEXT"),
        ("ai_sectors", "TEXT"),
        ("ai_importance", "INTEGER DEFAULT 0"),
        ("ai_direction", "TEXT"),
        ("ai_status", "TEXT DEFAULT 'pending'"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE cls_telegraph ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    # market_breadth 表（涨跌家数 + 大盘状态）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_breadth (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL UNIQUE,
            up_count INTEGER,
            down_count INTEGER,
            flat_count INTEGER,
            limit_up_count INTEGER,
            limit_down_count INTEGER,
            index_change_pct REAL,
            market_state TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # stock_basic 基础表（测试环境可能不存在）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_basic (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            price REAL, open REAL, high REAL, low REAL, prev_close REAL,
            change_pct REAL, total_market_cap REAL, circ_market_cap REAL,
            turnover_rate REAL, volume_ratio REAL, amplitude REAL, volume REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma5_angle REAL,
            industry TEXT, concepts TEXT,
            main_force_net REAL, main_force_ratio REAL,
            super_large_net REAL, large_net REAL, medium_net REAL, small_net REAL,
            avg_vol_5d REAL, avg_vol_20d REAL,
            pe_ttm REAL, pb_ratio REAL, revenue_growth REAL, profit_growth REAL
        );
    """)

    # stock_basic 唯一索引（支持 INSERT OR REPLACE upsert）
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_basic_date_code
        ON stock_basic(trade_date, stock_code);
    """)

    # 选股自我进化 — 漏斗 + AI 日志 + 决策 + 教训 + 改进
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS strategy_funnel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            push_date TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            rank_position INTEGER,
            raw_snapshot TEXT NOT NULL,
            factors_passed TEXT,
            factors_detail TEXT,
            scenarios TEXT,
            trend_mode TEXT,
            score REAL,
            open_price REAL,
            close_price REAL,
            day_change_pct REAL,
            bought INTEGER DEFAULT 0,
            buy_price REAL,
            day_pnl_pct REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_sf_push ON strategy_funnel(push_date);
        CREATE INDEX IF NOT EXISTS idx_sf_code ON strategy_funnel(stock_code);

        CREATE TABLE IF NOT EXISTS strategy_ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            push_date TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            rank_in_prompt INTEGER,
            verdict TEXT,
            confidence TEXT,
            what_i_see TEXT,
            what_concerns_me TEXT,
            decisive_factor TEXT,
            skip_reason TEXT,
            would_reconsider_if TEXT,
            buy_zone_min REAL,
            buy_zone_max REAL,
            stop_loss REAL,
            take_profit REAL,
            pricing_logic TEXT,
            signal_id INTEGER,
            day_change_pct REAL,
            day_pnl_pct REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_sad_push ON strategy_ai_decisions(push_date, verdict);
        CREATE INDEX IF NOT EXISTS idx_sad_code ON strategy_ai_decisions(stock_code);

        CREATE TABLE IF NOT EXISTS strategy_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_type TEXT NOT NULL,
            lesson_key TEXT NOT NULL,
            lesson_content TEXT NOT NULL,
            trigger_conditions TEXT,
            occurrence_count INTEGER DEFAULT 1,
            first_date TEXT NOT NULL,
            last_date TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lesson_type, lesson_key)
        );

        CREATE TABLE IF NOT EXISTS strategy_improvements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            push_date TEXT NOT NULL,
            improvement_type TEXT NOT NULL,
            target_module TEXT,
            target_param TEXT,
            suggested_change TEXT NOT NULL,
            code_diff TEXT,
            rationale TEXT NOT NULL,
            evidence_ids TEXT,
            status TEXT DEFAULT 'pending',
            applied_date TEXT,
            effectiveness_check TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.commit()
    conn.close()
