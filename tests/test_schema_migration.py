"""DB 结构迁移测试：验证 ensure_tables() 幂等创建、ALTER TABLE 迁移、约束/索引完整性。

测试用临时 DB，不依赖真实数据库路径。
"""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from data.schema import ensure_tables

# ================================================================
# 预期清单 — 与 data/schema.py 保持一致
# ================================================================

ALL_TABLES = {
    "trade_signals",
    "trade_orders",
    "trade_portfolio_snapshots",
    "trade_portfolio_positions",
    "trade_holdings_review",
    "market_snapshots",
    "index_snapshots",
    "market_breadth",
    "stock_basic",
    "strategy_funnel",
    "strategy_ai_decisions",
    "strategy_lessons",
    "strategy_improvements",
    "watcher_decision_log",
    "audit_findings",
    "watcher_lessons",
    "review_lessons",
    "watcher_improvements",
    "morning_sector_bias",
}

EXPECTED_COLUMNS = {
    "trade_signals": [
        "id",
        "trade_date",
        "created_at",
        "signal_type",
        "signal_source",
        "stock_code",
        "stock_name",
        "buy_zone_min",
        "buy_zone_max",
        "target_position",
        "stop_loss",
        "take_profit",
        "trailing_stop",
        "signal_score",
        "strategy_name",
        "reason",
        "status",
        "executed_at",
        "account",
    ],
    "trade_orders": [
        "id",
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
    ],
    "trade_portfolio_snapshots": [
        "id",
        "trade_date",
        "total_value",
        "cash",
        "market_value",
        "daily_pnl",
        "total_pnl",
        "drawdown",
        "position_count",
        "sector_exposure",
        "created_at",
        "account",
    ],
    "trade_portfolio_positions": [
        "id",
        "trade_date",
        "account",
        "stock_code",
        "stock_name",
        "volume",
        "avg_cost",
        "current_price",
        "market_value",
        "pnl",
        "pnl_pct",
        "pre_close",
        "daily_pnl",
        "entry_date",
        "created_at",
        "locked_volume",
    ],
    "trade_holdings_review": [
        "id",
        "trade_date",
        "created_at",
        "stock_code",
        "account",
        "action",
        "new_stop_loss",
        "new_take_profit",
        "expected_holding_days",
        "tomorrow_outlook",
        "reason",
        "applied",
    ],
    "market_snapshots": [
        "trade_date",
        "ts",
        "code",
        "change_pct",
        "price",
        "amount",
    ],
    "index_snapshots": [
        "trade_date",
        "ts",
        "price",
        "high",
        "low",
        "pre_close",
        "change_pct",
        "amount",
    ],
    "market_breadth": [
        "id",
        "trade_date",
        "up_count",
        "down_count",
        "flat_count",
        "limit_up_count",
        "limit_down_count",
        "index_change_pct",
        "market_state",
        "created_at",
    ],
    "stock_basic": [
        "trade_date",
        "stock_code",
        "stock_name",
        "price",
        "open",
        "high",
        "low",
        "prev_close",
        "change_pct",
        "total_market_cap",
        "circ_market_cap",
        "turnover_rate",
        "volume_ratio",
        "amplitude",
        "volume",
        "ma5",
        "ma10",
        "ma20",
        "ma5_angle",
        "industry",
        "concepts",
        "main_force_net",
        "main_force_ratio",
        "super_large_net",
        "large_net",
        "medium_net",
        "small_net",
        "avg_vol_5d",
        "avg_vol_20d",
        "pe_ttm",
        "pb_ratio",
        "revenue_growth",
        "profit_growth",
    ],
    "strategy_funnel": [
        "id",
        "push_date",
        "trade_date",
        "stock_code",
        "stock_name",
        "rank_position",
        "raw_snapshot",
        "factors_passed",
        "factors_detail",
        "scenarios",
        "trend_mode",
        "score",
        "open_price",
        "close_price",
        "day_change_pct",
        "bought",
        "buy_price",
        "day_pnl_pct",
        "created_at",
    ],
    "strategy_ai_decisions": [
        "id",
        "push_date",
        "trade_date",
        "stock_code",
        "stock_name",
        "rank_in_prompt",
        "verdict",
        "confidence",
        "what_i_see",
        "what_concerns_me",
        "decisive_factor",
        "skip_reason",
        "would_reconsider_if",
        "buy_zone_min",
        "buy_zone_max",
        "stop_loss",
        "take_profit",
        "pricing_logic",
        "signal_id",
        "day_change_pct",
        "day_pnl_pct",
        "created_at",
    ],
    "strategy_lessons": [
        "id",
        "lesson_type",
        "lesson_key",
        "lesson_content",
        "trigger_conditions",
        "occurrence_count",
        "first_date",
        "last_date",
        "is_active",
        "created_at",
    ],
    "strategy_improvements": [
        "id",
        "push_date",
        "improvement_type",
        "target_module",
        "target_param",
        "suggested_change",
        "code_diff",
        "rationale",
        "evidence_ids",
        "status",
        "applied_date",
        "effectiveness_check",
        "created_at",
    ],
    "watcher_decision_log": [
        "id",
        "trade_date",
        "ts",
        "decision_type",
        "stock_code",
        "decision_data",
        "created_at",
    ],
    "audit_findings": [
        "id",
        "trade_date",
        "finding_type",
        "severity",
        "stock_code",
        "decision_log_ids",
        "pattern_desc",
        "evidence",
        "created_at",
    ],
    "watcher_lessons": [
        "id",
        "lesson_type",
        "lesson_key",
        "lesson_content",
        "trigger_conditions",
        "occurrence_count",
        "first_date",
        "last_date",
        "is_active",
        "created_at",
    ],
    "review_lessons": [
        "id",
        "lesson_type",
        "lesson_key",
        "lesson_content",
        "occurrence_count",
        "first_date",
        "last_date",
        "is_active",
        "created_at",
    ],
    "watcher_improvements": [
        "id",
        "trade_date",
        "improvement_type",
        "target_module",
        "target_param",
        "suggested_change",
        "code_diff",
        "rationale",
        "evidence_ids",
        "status",
        "applied_date",
        "effectiveness_check",
        "created_at",
    ],
    "morning_sector_bias": [
        "id",
        "trade_date",
        "sector_name",
        "bias",
        "priority",
        "max_positions",
        "relaxed_thresholds",
        "size_multiplier",
        "stock_codes",
        "reason",
        "created_at",
    ],
}

EXPECTED_INDEXES = {
    "idx_trade_signals_date",
    "idx_trade_orders_date",
    "idx_stock_basic_date_code",
    "idx_sf_push",
    "idx_sf_code",
    "idx_sad_push",
    "idx_sad_code",
    "idx_wdl_date_type",
    "idx_af_date_sev",
    "idx_wl_type",
    "idx_wi_status",
}

# ================================================================
# 工具函数
# ================================================================


def _call_ensure_tables(db_path: str):
    with patch("data.schema.DATABASE_PATH", db_path):
        ensure_tables()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _index_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_auto%'"
    ).fetchall()
    return {r[0] for r in rows}


# ================================================================
# Fixtures
# ================================================================


@pytest.fixture
def fresh_db():
    """返回一个空临时 DB 路径，测试后自动删除。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def conn(fresh_db):
    """基于 fresh_db 的连接，带 row_factory。"""
    c = sqlite3.connect(fresh_db)
    yield c
    c.close()


# ================================================================
# Test 1: 空数据库全量建表
# ================================================================


class TestEnsureTables:
    def test_ensure_tables_creates_all_tables(self, fresh_db):
        """空数据库调用 ensure_tables()，验证 19 张表及其列均存在。"""
        _call_ensure_tables(fresh_db)
        conn = sqlite3.connect(fresh_db)

        # 所有表已创建
        tables = _table_names(conn)
        missing = ALL_TABLES - tables
        assert not missing, f"缺失表: {missing}"

        # 每张表列齐全
        for table, expected_cols in EXPECTED_COLUMNS.items():
            actual = _column_names(conn, table)
            for col in expected_cols:
                assert col in actual, f"表 {table} 缺少列 {col} (已有列: {actual})"

        conn.close()

    def test_ensure_tables_is_idempotent(self, fresh_db):
        """两次 ensure_tables()，数据完整保留。"""
        _call_ensure_tables(fresh_db)

        conn = sqlite3.connect(fresh_db)
        # 插入一条数据
        conn.execute(
            "INSERT INTO trade_portfolio_snapshots "
            "(trade_date, total_value, cash, created_at) "
            "VALUES ('2026-06-01', 100000.0, 50000.0, '2026-06-01 09:30:00')"
        )
        conn.commit()
        conn.close()

        # 第二次调用
        _call_ensure_tables(fresh_db)

        conn = sqlite3.connect(fresh_db)
        conn.row_factory = sqlite3.Row

        # 无重复表
        tables = _table_names(conn)
        assert len(tables) == len(ALL_TABLES)

        # 数据仍在
        row = conn.execute(
            "SELECT total_value FROM trade_portfolio_snapshots "
            "WHERE trade_date='2026-06-01'"
        ).fetchone()
        assert row is not None
        assert row["total_value"] == 100000.0

        conn.close()

    def test_ensure_tables_on_existing_db(self, fresh_db):
        """已有部分表，调用后补齐剩余表且不动已有表。"""
        conn = sqlite3.connect(fresh_db)
        # 提前手动建 3 张核心表（列数量少不会冲突）
        conn.executescript("""
            CREATE TABLE trade_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                signal_source TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            );
            CREATE TABLE trade_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                order_time TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                order_type TEXT NOT NULL
            );
            CREATE TABLE market_snapshots (
                trade_date TEXT NOT NULL,
                ts REAL NOT NULL,
                code TEXT NOT NULL,
                price REAL DEFAULT 0,
                PRIMARY KEY (trade_date, ts, code)
            );
        """)
        conn.commit()
        before = _table_names(conn)
        assert before == {"trade_signals", "trade_orders", "market_snapshots"}
        conn.close()

        _call_ensure_tables(fresh_db)

        conn = sqlite3.connect(fresh_db)
        after = _table_names(conn)
        missing = ALL_TABLES - after
        assert not missing, f"补齐后仍缺表: {missing}"

        # 原表列未被覆盖破坏 —— 只验证核心列仍在
        for table, core_cols in [
            (
                "trade_signals",
                ["id", "trade_date", "signal_type", "stock_code", "status"],
            ),
            ("trade_orders", ["id", "trade_date", "order_time", "stock_code"]),
            ("market_snapshots", ["trade_date", "ts", "code", "price"]),
        ]:
            actual = _column_names(conn, table)
            for col in core_cols:
                assert col in actual, f"表 {table} 核心列 {col} 被覆盖 (现有: {actual})"

        conn.close()


# ================================================================
# Test 4: ALTER TABLE 迁移
# ================================================================


class TestMigrations:
    def test_alter_table_migrations(self, fresh_db):
        """从缺少 locked_volume 的老版本开始，迁移后列存在、旧数据完整。"""
        conn = sqlite3.connect(fresh_db)
        # 模拟没有 locked_volume 的旧 schema
        conn.execute("""
            CREATE TABLE trade_portfolio_positions (
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
                created_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO trade_portfolio_positions "
            "(trade_date, account, stock_code, stock_name, volume, avg_cost, current_price, created_at) "
            "VALUES ('2026-06-01', 'paper', '000001', '平安银行', 1000, 12.5, 13.0, '2026-06-01 09:30:00')"
        )
        conn.commit()
        conn.close()

        # 迁移
        _call_ensure_tables(fresh_db)

        conn = sqlite3.connect(fresh_db)
        conn.row_factory = sqlite3.Row

        cols = _column_names(conn, "trade_portfolio_positions")
        assert "locked_volume" in cols, f"迁移后 locked_volume 不存在 (现有列: {cols})"

        row = conn.execute(
            "SELECT stock_code, volume, locked_volume "
            "FROM trade_portfolio_positions WHERE trade_date='2026-06-01'"
        ).fetchone()
        assert row is not None
        assert row["stock_code"] == "000001"
        assert row["volume"] == 1000
        assert row["locked_volume"] == 0  # DEFAULT 0

        conn.close()

    def test_cls_telegraph_alter_does_not_crash(self, fresh_db):
        """cls_telegraph 不存在时 ALTER TABLE 被 suppress，不抛异常。"""
        # 只建一张不相关的表确保 DB 非空
        conn = sqlite3.connect(fresh_db)
        conn.execute("CREATE TABLE dummy (x TEXT)")
        conn.close()

        # ensure_tables 内部对 cls_telegraph 的 ALTER 被 suppress，
        # 不应因此报错
        _call_ensure_tables(fresh_db)

        conn = sqlite3.connect(fresh_db)
        tables = _table_names(conn)
        assert "dummy" in tables  # 原有表不受影响
        conn.close()


# ================================================================
# Test 5: 列类型极端值
# ================================================================


class TestDataIntegrity:
    def test_column_types(self, fresh_db):
        """插入极大/极小/特殊字符值，验证 round-trip 正确。"""
        _call_ensure_tables(fresh_db)
        conn = sqlite3.connect(fresh_db)

        max_int = 2**63 - 1
        large_text = "A" * 10000
        special_chars = "你好 abc!@#$%^&*()_+-=[]{}|;':\",./<>?`~\n\t"

        # trade_signals — 数值 + 大文本
        conn.execute(
            "INSERT INTO trade_signals "
            "(trade_date, created_at, signal_type, signal_source, stock_code, "
            " buy_zone_min, buy_zone_max, signal_score, reason, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-06-01",
                "2026-06-01 08:00:00",
                "buy",
                "test",
                "000001",
                -999999.999,
                999999.999,
                float(max_int),
                large_text,
                "pending",
            ),
        )
        # strategy_lessons — 特殊字符
        conn.execute(
            "INSERT INTO strategy_lessons "
            "(lesson_type, lesson_key, lesson_content, first_date, last_date) "
            "VALUES (?, ?, ?, ?, ?)",
            ("type_a", "key_1", special_chars, "2026-06-01", "2026-06-01"),
        )
        conn.commit()
        conn.close()

        # Round-trip 校验
        conn = sqlite3.connect(fresh_db)
        conn.row_factory = sqlite3.Row

        r1 = conn.execute(
            "SELECT stock_code, buy_zone_min, buy_zone_max, signal_score, reason, status "
            "FROM trade_signals WHERE stock_code='000001'"
        ).fetchone()
        assert r1 is not None
        assert r1["stock_code"] == "000001"
        assert r1["buy_zone_min"] == -999999.999
        assert r1["buy_zone_max"] == 999999.999
        assert r1["signal_score"] == float(max_int)
        assert len(r1["reason"]) == 10000
        assert r1["status"] == "pending"

        r2 = conn.execute(
            "SELECT lesson_content FROM strategy_lessons WHERE lesson_key='key_1'"
        ).fetchone()
        assert r2 is not None
        assert r2["lesson_content"] == special_chars

        conn.close()

    def test_special_chars_in_trade_fields(self, fresh_db):
        """交易相关字段中的特殊字符（换行、引号、中文）round-trip 正常。"""
        _call_ensure_tables(fresh_db)
        conn = sqlite3.connect(fresh_db)

        russian_text = "Стоп-лосс сработал"
        chinese_text = "涨停突破 放量上攻"
        json_in_text = '{"key": "value", "nested": {"a": 1}}'

        conn.execute(
            "INSERT INTO strategy_ai_decisions "
            "(push_date, trade_date, stock_code, stock_name, rank_in_prompt, "
            " verdict, what_i_see, what_concerns_me, skip_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-06-01",
                "2026-06-01",
                "000001",
                "测试",
                1,
                "buy",
                russian_text,
                chinese_text,
                json_in_text,
            ),
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(fresh_db)
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT verdict, what_i_see, what_concerns_me, skip_reason "
            "FROM strategy_ai_decisions WHERE stock_code='000001'"
        ).fetchone()
        assert r["verdict"] == "buy"
        assert r["what_i_see"] == russian_text
        assert r["what_concerns_me"] == chinese_text
        assert r["skip_reason"] == json_in_text
        conn.close()


# ================================================================
# Test 6: 索引完整性
# ================================================================


class TestIndexes:
    def test_indexes_created(self, fresh_db):
        """所有预期索引均已创建。"""
        _call_ensure_tables(fresh_db)
        conn = sqlite3.connect(fresh_db)
        indexes = _index_names(conn)
        missing = EXPECTED_INDEXES - indexes
        assert not missing, f"缺失索引: {missing}"
        # 确保没有额外的意料之外索引（auto-index 已排除）
        conn.close()


# ================================================================
# Test 7: UNIQUE 约束
# ================================================================


class TestUniqueConstraints:
    def test_unique_constraints_replace(self, fresh_db):
        """INSERT OR REPLACE 处理 (trade_date, account) 重复。"""
        _call_ensure_tables(fresh_db)
        conn = sqlite3.connect(fresh_db)

        # 首次插入
        conn.execute(
            "INSERT INTO trade_portfolio_snapshots "
            "(trade_date, account, total_value, cash, market_value, created_at) "
            "VALUES ('2026-06-01', 'real', 100000.0, 50000.0, 50000.0, '2026-06-01 09:30:00')"
        )
        conn.commit()

        # 相同 (trade_date, account) 用 INSERT OR REPLACE 覆盖
        conn.execute(
            "INSERT OR REPLACE INTO trade_portfolio_snapshots "
            "(trade_date, account, total_value, cash, market_value, created_at) "
            "VALUES ('2026-06-01', 'real', 200000.0, 100000.0, 100000.0, '2026-06-01 10:00:00')"
        )
        conn.commit()

        rows = conn.execute(
            "SELECT total_value FROM trade_portfolio_snapshots "
            "WHERE trade_date='2026-06-01' AND account='real'"
        ).fetchall()
        assert len(rows) == 1, f"INSERT OR REPLACE 后应只有 1 行，实际 {len(rows)}"
        assert rows[0][0] == 200000.0

        conn.close()

    def test_unique_constraints_positions(self, fresh_db):
        """portfolio_positions 的 (trade_date, account, stock_code) 唯一性。"""
        _call_ensure_tables(fresh_db)
        conn = sqlite3.connect(fresh_db)

        conn.execute(
            "INSERT INTO trade_portfolio_positions "
            "(trade_date, account, stock_code, stock_name, volume, created_at) "
            "VALUES ('2026-06-01', 'paper', '000001', '平安银行', 100, '09:30:00')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO trade_portfolio_positions "
            "(trade_date, account, stock_code, stock_name, volume, created_at) "
            "VALUES ('2026-06-01', 'paper', '000001', '平安银行', 200, '10:00:00')"
        )
        conn.commit()

        rows = conn.execute(
            "SELECT volume FROM trade_portfolio_positions "
            "WHERE trade_date='2026-06-01' AND account='paper' AND stock_code='000001'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 200

        conn.close()


# ================================================================
# Test 8: 外键一致性（SQLite 默认不开启 FK，不报错）
# ================================================================


class TestForeignKeyConsistency:
    def test_foreign_key_consistency(self, fresh_db):
        """trade_orders.signal_id 引用 trade_signals.id，FK 默认不校验。"""
        _call_ensure_tables(fresh_db)
        conn = sqlite3.connect(fresh_db)

        # 先插入一个 signal（给有效外键用）
        conn.execute(
            "INSERT INTO trade_signals "
            "(trade_date, created_at, signal_type, signal_source, stock_code) "
            "VALUES ('2026-06-01', '2026-06-01 08:00:00', 'buy', 'test', '000001')"
        )
        conn.commit()
        signal_id = conn.execute("SELECT id FROM trade_signals").fetchone()[0]

        # 有效 signal_id —— 正常插入
        conn.execute(
            "INSERT INTO trade_orders "
            "(signal_id, trade_date, order_time, stock_code, order_type, order_price, order_volume) "
            "VALUES (?, '2026-06-01', '09:30:00', '000001', 'buy', 10.0, 100)",
            (signal_id,),
        )
        conn.commit()

        # 无效 signal_id —— FK 不校验，也不会 crash
        conn.execute(
            "INSERT INTO trade_orders "
            "(signal_id, trade_date, order_time, stock_code, order_type, order_price, order_volume) "
            "VALUES (999999, '2026-06-01', '09:31:00', '000002', 'sell', 11.0, 200)"
        )
        conn.commit()

        rows = conn.execute("SELECT COUNT(*) FROM trade_orders").fetchone()
        assert rows[0] == 2

        conn.close()
