"""交易系统专用表结构"""

import sqlite3
from contextlib import suppress

from system.config.settings import DATABASE_PATH


def ensure_tables():
    """创建 trade_ 前缀的交易系统表（幂等）"""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    cursor.executescript("""
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
            account TEXT DEFAULT 'real',
            UNIQUE(trade_date, stock_code, account)
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
            pre_close REAL DEFAULT 0,
            daily_pnl REAL DEFAULT 0,
            entry_date TEXT DEFAULT '',
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

        CREATE INDEX IF NOT EXISTS idx_trade_signals_date
            ON trade_signals(trade_date);
        CREATE INDEX IF NOT EXISTS idx_trade_orders_date
            ON trade_orders(trade_date);

        -- 实时数据采集表（watcher 盘中容灾恢复用）
        CREATE TABLE IF NOT EXISTS market_snapshots (
            trade_date TEXT NOT NULL,
            ts REAL NOT NULL,
            code TEXT NOT NULL,
            change_pct REAL DEFAULT 0,
            price REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            PRIMARY KEY (trade_date, ts, code)
        );

        CREATE TABLE IF NOT EXISTS index_snapshots (
            trade_date TEXT NOT NULL,
            ts REAL NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            high REAL DEFAULT 0,
            low REAL DEFAULT 0,
            pre_close REAL DEFAULT 0,
            change_pct REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            PRIMARY KEY (trade_date, ts)
        );
    """)

    conn.commit()

    # locked_volume 列 — T+1 锁仓持久化（幂等迁移）
    with suppress(sqlite3.OperationalError):
        cursor.execute("ALTER TABLE trade_portfolio_positions ADD COLUMN locked_volume INTEGER DEFAULT 0")

    # stop_loss / take_profit 列 — 止损止盈持久化（幂等迁移）
    with suppress(sqlite3.OperationalError):
        cursor.execute("ALTER TABLE trade_portfolio_positions ADD COLUMN stop_loss REAL DEFAULT 0")
    with suppress(sqlite3.OperationalError):
        cursor.execute("ALTER TABLE trade_portfolio_positions ADD COLUMN take_profit REAL DEFAULT 0")

    # 添加 account 字段（幂等迁移）
    for table in [
        "trade_signals",
        "trade_orders",
        "trade_portfolio_snapshots",
    ]:
        with suppress(sqlite3.OperationalError):
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN account TEXT DEFAULT 'real'")

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

    # stock_basic 基础表（与生产库一致，共 50 字段含 id）
    # 注意：DDL 仅作文档用，生产库由采集器 INSERT 自动创建列，不要手动 ALTER
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stock_basic (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            price REAL,
            change_pct REAL,
            change_amount REAL,
            volume REAL,
            turnover REAL,
            amplitude REAL,
            turnover_rate REAL,
            pe_dynamic REAL,
            volume_ratio REAL,
            high REAL,
            low REAL,
            open REAL,
            prev_close REAL,
            total_market_cap REAL,
            circ_market_cap REAL,
            pb_ratio REAL,
            total_shares REAL,
            circ_shares REAL,
            revenue_growth REAL,
            profit_growth REAL,
            undistributed_profit REAL,
            asset_liability_ratio REAL,
            main_force_net REAL,
            super_large_net REAL,
            large_net REAL,
            medium_net REAL,
            small_net REAL,
            main_force_ratio REAL,
            super_large_ratio REAL,
            large_ratio REAL,
            medium_ratio REAL,
            small_ratio REAL,
            pe_ttm REAL,
            industry TEXT,
            region TEXT,
            concepts TEXT,
            bps REAL,
            listing_date TEXT,
            updated_at TEXT,
            avg_price REAL,
            ma5 REAL,
            ma20 REAL,
            ma5_angle REAL,
            ma10 REAL,
            avg_vol_5d REAL,
            avg_vol_20d REAL
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

        -- 盯盘自审计表
        CREATE TABLE IF NOT EXISTS watcher_decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            ts TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            stock_code TEXT,
            decision_data TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            stock_code TEXT,
            decision_log_ids TEXT,
            pattern_desc TEXT NOT NULL,
            evidence TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS watcher_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_type TEXT NOT NULL,
            lesson_key TEXT NOT NULL,
            lesson_content TEXT NOT NULL,
            trigger_conditions TEXT,
            occurrence_count INTEGER DEFAULT 1,
            first_date DATE NOT NULL,
            last_date DATE NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lesson_type, lesson_key)
        );

        CREATE TABLE IF NOT EXISTS review_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_type TEXT NOT NULL,
            lesson_key TEXT NOT NULL,
            lesson_content TEXT NOT NULL,
            occurrence_count INTEGER DEFAULT 1,
            first_date TEXT NOT NULL,
            last_date TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lesson_type, lesson_key)
        );

        CREATE TABLE IF NOT EXISTS watcher_improvements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            improvement_type TEXT NOT NULL,
            target_module TEXT NOT NULL,
            target_param TEXT,
            suggested_change TEXT NOT NULL,
            code_diff TEXT,
            rationale TEXT NOT NULL,
            evidence_ids TEXT,
            status TEXT DEFAULT 'pending',
            applied_date DATE,
            effectiveness_check TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS morning_sector_bias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            sector_name TEXT NOT NULL,
            bias TEXT NOT NULL CHECK(bias IN ('focus','avoid','neutral','selective')),
            priority INTEGER DEFAULT 3,
            max_positions INTEGER DEFAULT 0,
            relaxed_thresholds TEXT,
            size_multiplier REAL DEFAULT 1.0,
            stock_codes TEXT,
            reason TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(trade_date, sector_name)
        );
    """)

    # 索引（与上面 SQL 分开，因为多条 executescript 不能带索引）
    cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_wdl_date_type ON watcher_decision_log(trade_date, decision_type);
        CREATE INDEX IF NOT EXISTS idx_af_date_sev ON audit_findings(trade_date, severity);
        CREATE INDEX IF NOT EXISTS idx_wl_type ON watcher_lessons(lesson_type);
        CREATE INDEX IF NOT EXISTS idx_wi_status ON watcher_improvements(status);
    """)

    conn.commit()
    conn.close()
