"""综合 CRUD 测试 — 覆盖所有 data/repo/ 模块的 Repository。

每个测试使用 db_path fixture 创建临时 SQLite + _extend_schema 补充额外表。
测试后自动清理。使用 direct sqlite3 查询验证实际 DB 状态。
"""

import json
import os
import sqlite3
from datetime import datetime

import pytest

from data.repo import TradeRepository
from data.repo.audit_repo import AuditRepo
from data.repo.order_repo import OrderRepo
from data.repo.portfolio_repo import PortfolioRepo
from data.repo.signal_repo import SignalRepo
from data.repo.strategy_repo import StrategyRepo

# ---------------------------------------------------------------------------
# 辅助：补充 conftest 未创建的额外表
# ---------------------------------------------------------------------------


def _extend_schema(conn):
    """创建 strategy / audit / watcher / sector 系列表。

    conftest.py 的 _init_test_db 只建了 4 张核心交易表，
    此函数补充 schema.py 中其余表，确保额外 repo 可正常工作。
    """
    conn.executescript("""
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
            applied_date TEXT,
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

        CREATE INDEX IF NOT EXISTS idx_sf_push ON strategy_funnel(push_date);
        CREATE INDEX IF NOT EXISTS idx_sad_push ON strategy_ai_decisions(push_date, verdict);
        CREATE INDEX IF NOT EXISTS idx_wdl_date_type ON watcher_decision_log(trade_date, decision_type);
        CREATE INDEX IF NOT EXISTS idx_af_date_sev ON audit_findings(trade_date, severity);
        CREATE INDEX IF NOT EXISTS idx_wl_type ON watcher_lessons(lesson_type);
        CREATE INDEX IF NOT EXISTS idx_wi_status ON watcher_improvements(status);

        CREATE TABLE IF NOT EXISTS stock_basic (
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            trade_date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            pre_close REAL, volume REAL, amount REAL,
            change_pct REAL, turnover REAL, pe_dynamic REAL,
            pb REAL, total_market_cap REAL, circ_market_cap REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma60 REAL,
            PRIMARY KEY (stock_code, trade_date)
        );
    """)
    conn.commit()


def _extend(db_path):
    """对 db_path 对应的数据库执行 _extend_schema。"""
    conn = sqlite3.connect(db_path)
    try:
        _extend_schema(conn)
    finally:
        conn.close()


def _count(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ===================================================================
# SignalRepo
# ===================================================================


class TestSignalRepo:
    """trade_signals 表 CRUD"""

    @staticmethod
    def _signal(**kw):
        now = datetime.now().isoformat()
        d = {
            "trade_date": "2026-06-01",
            "created_at": now,
            "signal_type": "BUY",
            "signal_source": "TEST",
            "stock_code": "002371",
            "stock_name": "北方华创",
            "buy_zone_min": 380.0,
            "buy_zone_max": 400.0,
            "stop_loss": 370.0,
            "take_profit": 440.0,
            "trailing_stop": 0.05,
            "signal_score": 75.0,
            "strategy_name": "ai_advisor_test",
            "reason": "测试信号",
            "status": "pending",
            "account": "paper",
            "expected_trend": "up",
        }
        d.update(kw)
        return d

    def test_insert_returns_id(self, db_path):
        repo = SignalRepo(db_path)
        sid = repo.insert(self._signal())
        assert sid > 0

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT id, stock_code, status FROM trade_signals WHERE id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "002371"
        assert row[2] == "pending"

    def test_get_pending_signals_paper(self, db_path):
        repo = SignalRepo(db_path)
        repo.insert(self._signal(trade_date="2026-06-01"))
        repo.insert(self._signal(trade_date="2026-06-02", stock_code="000001"))

        results = repo.get_pending(account="paper")
        assert len(results) == 2
        for r in results:
            assert r["status"] == "pending"
            assert r["account"] == "paper"

    def test_get_pending_signals_real_empty(self, db_path):
        repo = SignalRepo(db_path)
        repo.insert(self._signal(account="paper"))
        results = repo.get_pending(account="real")
        assert results == []

    def test_update_signal_status(self, db_path):
        repo = SignalRepo(db_path)
        sid = repo.insert(self._signal(status="pending"))
        repo.update_status(sid, "bought")

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status, executed_at FROM trade_signals WHERE id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row[0] == "bought"
        assert row[1] is not None  # executed_at 被填入

    def test_expire_old_signals(self, db_path):
        repo = SignalRepo(db_path)
        sid_old = repo.insert(self._signal(trade_date="2026-05-01"))
        sid_new = repo.insert(self._signal(trade_date="2026-06-01"))

        repo.expire_old_pending("2026-06-01")

        conn = sqlite3.connect(db_path)
        rows = dict(conn.execute("SELECT id, status FROM trade_signals").fetchall())
        conn.close()
        assert rows[sid_old] == "expired"
        assert rows[sid_new] == "pending"

    def test_insert_duplicate_graceful(self, db_path):
        """重复插入不抛异常，INSERT OR REPLACE 返回有效 ID。

        注：conftest 的 trade_signals 不含 UNIQUE 约束，
        因此这里验证的是多次插入不崩溃且全部可见。生产环境有
        UNIQUE(trade_date, stock_code, account) 约束会替换。
        """
        repo = SignalRepo(db_path)
        data = self._signal()
        sid1 = repo.insert(data)
        sid2 = repo.insert(dict(data, signal_score=80.0))
        assert sid1 > 0
        assert sid2 > 0
        # conftest 无 UNIQUE 约束 → 两条记录都存在
        assert _count(db_path, "trade_signals") == 2

    def test_get_pending_with_trade_date_filter(self, db_path):
        repo = SignalRepo(db_path)
        repo.insert(self._signal(trade_date="2026-06-01"))
        repo.insert(self._signal(trade_date="2026-06-02"))
        results = repo.get_pending(trade_date="2026-06-01")
        assert len(results) == 1
        assert results[0]["trade_date"] == "2026-06-01"

    def test_get_expired_signals(self, db_path):
        repo = SignalRepo(db_path)
        repo.insert(self._signal(trade_date="2026-04-01", status="expired"))
        results = repo.get_expired(before_date="2026-05-01")
        assert len(results) == 1


# ===================================================================
# OrderRepo
# ===================================================================


class TestOrderRepo:
    """trade_orders 表 CRUD"""

    @staticmethod
    def _order(**kw):
        d = {
            "signal_id": 1,
            "trade_date": "2026-06-01",
            "order_time": "2026-06-01 09:30:00",
            "stock_code": "002371",
            "order_type": "buy",
            "order_price": 390.0,
            "order_volume": 100,
            "price_type": "limit",
            "order_status": "filled",
            "filled_volume": 100,
            "filled_price": 390.0,
            "filled_amount": 39000.0,
            "account": "paper",
            "strategy_name": "test",
        }
        d.update(kw)
        return d

    def test_insert_buy(self, db_path):
        repo = OrderRepo(db_path)
        oid = repo.insert(self._order())
        assert oid > 0

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT id, order_type, stock_code FROM trade_orders WHERE id=?", (oid,)
        ).fetchone()
        conn.close()
        assert row[1] == "buy"
        assert row[2] == "002371"

    def test_insert_sell(self, db_path):
        repo = OrderRepo(db_path)
        oid = repo.insert(self._order(order_type="sell", order_price=420.0))
        assert oid > 0

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT order_type, order_price FROM trade_orders WHERE id=?", (oid,)
        ).fetchone()
        conn.close()
        assert row[0] == "sell"
        assert row[1] == 420.0

    def test_get_orders_by_date(self, db_path):
        repo = OrderRepo(db_path)
        o1 = repo.insert(self._order(trade_date="2026-06-01"))
        o2 = repo.insert(self._order(trade_date="2026-06-01", stock_code="000001"))
        repo.insert(self._order(trade_date="2026-06-02"))

        results = repo.get_by_date("2026-06-01")
        assert len(results) == 2
        ids = {r["id"] for r in results}
        assert o1 in ids
        assert o2 in ids

    def test_get_orders_no_results(self, db_path):
        repo = OrderRepo(db_path)
        results = repo.get_by_date("2099-01-01")
        assert results == []

    def test_get_orders_by_date_and_account(self, db_path):
        repo = OrderRepo(db_path)
        repo.insert(self._order(account="paper"))
        results = repo.get_by_date("2026-06-01", account="real")
        assert results == []

    def test_get_orders_by_signal(self, db_path):
        """验证 order.signal_id 正确关联到 signal。"""
        repo = OrderRepo(db_path)
        repo.insert(self._order(signal_id=42))
        repo.insert(self._order(signal_id=42, stock_code="000001"))

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT id, stock_code FROM trade_orders WHERE signal_id=?", (42,)
        ).fetchall()
        conn.close()
        assert len(rows) == 2

    def test_insert_order_with_minimal_fields(self, db_path):
        """只传必填字段也能成功插入。"""
        repo = OrderRepo(db_path)
        oid = repo.insert(
            {
                "trade_date": "2026-06-01",
                "order_time": "2026-06-01 10:00:00",
                "stock_code": "002371",
                "order_type": "buy",
                "order_volume": 200,
                "order_price": 390.0,
                "account": "paper",
            }
        )
        assert oid > 0

    def test_order_status_update_direct(self, db_path):
        """验证订单状态的更新路径可用。"""
        repo = OrderRepo(db_path)
        oid = repo.insert(self._order(order_status="pending"))

        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE trade_orders SET order_status=? WHERE id=?",
            ("cancelled", oid),
        )
        conn.commit()
        row = conn.execute(
            "SELECT order_status FROM trade_orders WHERE id=?", (oid,)
        ).fetchone()
        conn.close()
        assert row[0] == "cancelled"

    def test_get_orders_items_have_all_columns(self, db_path):
        repo = OrderRepo(db_path)
        repo.insert(self._order())
        results = repo.get_by_date("2026-06-01")
        assert len(results) == 1
        item = results[0]
        assert "id" in item
        assert "signal_id" in item
        assert "stock_code" in item
        assert "order_type" in item
        assert "order_status" in item
        assert "account" in item


# ===================================================================
# PortfolioRepo
# ===================================================================


class TestPortfolioRepo:
    """trade_portfolio_snapshots + trade_portfolio_positions CRUD"""

    @staticmethod
    def _snap(**kw):
        d = {
            "trade_date": "2026-06-01",
            "total_value": 1_000_000.0,
            "cash": 500_000.0,
            "market_value": 500_000.0,
            "daily_pnl": 10_000.0,
            "total_pnl": 50_000.0,
            "drawdown": 0.05,
            "position_count": 3,
            "account": "paper",
        }
        d.update(kw)
        return d

    @staticmethod
    def _pos(**kw):
        d = {
            "stock_code": "002371",
            "stock_name": "北方华创",
            "volume": 100,
            "avg_cost": 390.0,
            "current_price": 400.0,
            "market_value": 40000.0,
            "pnl": 1000.0,
            "pnl_pct": 2.56,
        }
        d.update(kw)
        return d

    # -- snapshots --

    def test_insert_snapshot(self, db_path):
        repo = PortfolioRepo(db_path)
        repo.insert_snapshot(self._snap())
        assert _count(db_path, "trade_portfolio_snapshots") == 1

    def test_get_latest_snapshot(self, db_path):
        repo = PortfolioRepo(db_path)
        repo.insert_snapshot(self._snap(trade_date="2026-06-01", total_value=900_000.0))
        repo.insert_snapshot(self._snap(trade_date="2026-06-02", total_value=950_000.0))
        latest = repo.get_latest_snapshot(account="paper")
        assert latest is not None
        assert latest["trade_date"] == "2026-06-02"
        assert latest["total_value"] == 950_000.0

    def test_get_latest_snapshot_empty(self, db_path):
        repo = PortfolioRepo(db_path)
        result = repo.get_latest_snapshot(account="nonexistent")
        assert result is None

    def test_get_latest_snapshot_before(self, db_path):
        repo = PortfolioRepo(db_path)
        repo.insert_snapshot(self._snap(trade_date="2026-06-01", total_value=900_000.0))
        repo.insert_snapshot(self._snap(trade_date="2026-06-03", total_value=950_000.0))
        result = repo.get_latest_snapshot_before("2026-06-03", account="paper")
        assert result is not None
        assert result["trade_date"] == "2026-06-01"
        assert result["total_value"] == 900_000.0

    def test_get_latest_snapshot_before_empty(self, db_path):
        repo = PortfolioRepo(db_path)
        result = repo.get_latest_snapshot_before("2026-01-01", account="paper")
        assert result is None

    def test_get_snapshots_range(self, db_path):
        repo = PortfolioRepo(db_path)
        repo.insert_snapshot(self._snap(trade_date="2026-06-01"))
        repo.insert_snapshot(self._snap(trade_date="2026-06-02"))
        repo.insert_snapshot(self._snap(trade_date="2026-06-05"))
        results = repo.get_snapshots(start="2026-06-01", end="2026-06-03")
        assert len(results) == 2

    def test_get_snapshots_all(self, db_path):
        repo = PortfolioRepo(db_path)
        repo.insert_snapshot(self._snap(trade_date="2026-06-01"))
        repo.insert_snapshot(self._snap(trade_date="2026-06-02"))
        results = repo.get_snapshots()
        assert len(results) == 2

    def test_snapshot_upsert_replace(self, db_path):
        """相同 trade_date + account 的 snapshot 替换后读取最新值。

        注：conftest 的 trade_portfolio_snapshots 不含 UNIQUE 约束，
        因此两条记录都会存在（生产环境 UNIQUE(trade_date, account)
        约束会触发 INSERT OR REPLACE 替换）。
        """
        repo = PortfolioRepo(db_path)
        repo.insert_snapshot(self._snap(trade_date="2026-06-01", total_value=900_000.0))
        repo.insert_snapshot(self._snap(trade_date="2026-06-01", total_value=950_000.0))
        latest = repo.get_latest_snapshot(account="paper")
        assert latest is not None
        assert latest["total_value"] == 950_000.0

    # -- positions --

    def test_insert_position(self, db_path):
        repo = PortfolioRepo(db_path)
        repo.insert_positions("2026-06-01", "paper", [self._pos()])
        assert _count(db_path, "trade_portfolio_positions") == 1

    def test_get_positions_by_date(self, db_path):
        repo = PortfolioRepo(db_path)
        repo.insert_positions(
            "2026-06-01",
            "paper",
            [self._pos(), self._pos(stock_code="000001", stock_name="平安银行")],
        )
        results = repo.get_positions_by_date("2026-06-01", "paper")
        assert len(results) == 2
        codes = {r["stock_code"] for r in results}
        assert "002371" in codes
        assert "000001" in codes

    def test_get_positions_by_date_empty(self, db_path):
        repo = PortfolioRepo(db_path)
        results = repo.get_positions_by_date("2099-01-01", "paper")
        assert results == []

    def test_get_latest_positions(self, db_path):
        repo = PortfolioRepo(db_path)
        repo.insert_positions("2026-06-01", "paper", [self._pos()])
        repo.insert_positions(
            "2026-06-02",
            "paper",
            [
                self._pos(),
                self._pos(stock_code="000001", stock_name="平安银行"),
            ],
        )
        latest = repo.get_latest_positions("paper")
        assert len(latest) == 2

    def test_get_latest_positions_empty(self, db_path):
        repo = PortfolioRepo(db_path)
        results = repo.get_latest_positions("paper")
        assert results == []

    def test_upsert_position_overwrites(self, db_path):
        """INSERT OR REPLACE 在 UNIQUE(trade_date, account, stock_code) 冲突时替换。"""
        repo = PortfolioRepo(db_path)
        repo.insert_positions("2026-06-01", "paper", [self._pos(volume=100)])
        repo.insert_positions("2026-06-01", "paper", [self._pos(volume=200)])
        assert _count(db_path, "trade_portfolio_positions") == 1

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT volume FROM trade_portfolio_positions "
            "WHERE trade_date='2026-06-01' AND account='paper' AND stock_code='002371'"
        ).fetchone()
        conn.close()
        assert row[0] == 200

    def test_insert_positions_with_extra_columns(self, db_path):
        """传入的额外字段（如 pre_close, holding_days）应被正确存储。"""
        repo = PortfolioRepo(db_path)
        repo.insert_positions(
            "2026-06-01",
            "paper",
            [
                self._pos(pre_close=385.0, holding_days=5),
            ],
        )
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT pre_close, holding_days FROM trade_portfolio_positions "
            "WHERE stock_code='002371'"
        ).fetchone()
        conn.close()
        assert row[0] == 385.0
        assert row[1] == 5


# ===================================================================
# StrategyRepo
# ===================================================================


class TestStrategyRepo:
    """strategy_funnel / strategy_ai_decisions / strategy_improvements CRUD"""

    @classmethod
    def _extend(cls, db_path):
        _extend(db_path)

    # -- funnel --

    def test_insert_funnel_record(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        repo.insert_funnel_batch(
            [
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "002371",
                    "stock_name": "北方华创",
                    "rank_position": 1,
                    "raw_snapshot": '{"price": 390.0}',
                    "factors_passed": "trend,volume",
                    "score": 85.0,
                }
            ]
        )
        assert _count(db_path, "strategy_funnel") == 1

        records = repo.get_funnel_records("2026-06-01")
        assert len(records) == 1
        assert records[0]["stock_code"] == "002371"

    def test_insert_funnel_batch_empty(self, db_path):
        """空列表应静默跳过。"""
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        repo.insert_funnel_batch([])
        assert _count(db_path, "strategy_funnel") == 0

    def test_backfill_funnel_close(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        repo.insert_funnel_batch(
            [
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "002371",
                    "stock_name": "北方华创",
                    "rank_position": 1,
                    "raw_snapshot": "{}",
                }
            ]
        )
        repo.backfill_funnel_close(
            "2026-06-01",
            [
                {
                    "stock_code": "002371",
                    "close_price": 395.0,
                    "day_change_pct": 1.28,
                    "bought": 1,
                    "buy_price": 390.0,
                    "day_pnl_pct": 1.5,
                    "open_price": 388.0,
                }
            ],
        )

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT close_price, bought, buy_price, day_pnl_pct FROM strategy_funnel "
            "WHERE push_date='2026-06-01' AND stock_code='002371'"
        ).fetchone()
        conn.close()
        assert row[0] == 395.0
        assert row[1] == 1
        assert row[2] == 390.0
        assert row[3] == 1.5

    # -- ai_decisions --

    def test_insert_ai_decision(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        repo.insert_ai_decisions_batch(
            [
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "002371",
                    "stock_name": "北方华创",
                    "rank_in_prompt": 1,
                    "verdict": "buy",
                    "confidence": "high",
                }
            ]
        )
        assert _count(db_path, "strategy_ai_decisions") == 1

        decisions = repo.get_ai_decisions("2026-06-01")
        assert len(decisions) == 1
        assert decisions[0]["verdict"] == "buy"

    def test_get_ai_decisions_filter_by_verdict(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        repo.insert_ai_decisions_batch(
            [
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "002371",
                    "verdict": "buy",
                    "rank_in_prompt": 1,
                },
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "000001",
                    "verdict": "skip",
                    "rank_in_prompt": 2,
                },
            ]
        )
        results = repo.get_ai_decisions("2026-06-01", verdict="skip")
        assert len(results) == 1
        assert results[0]["stock_code"] == "000001"

    def test_backfill_ai_decisions_close(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        repo.insert_ai_decisions_batch(
            [
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "002371",
                    "verdict": "buy",
                    "rank_in_prompt": 1,
                }
            ]
        )
        repo.backfill_ai_decisions_close(
            "2026-06-01",
            [
                {
                    "stock_code": "002371",
                    "day_change_pct": 2.1,
                    "day_pnl_pct": 1.5,
                }
            ],
        )

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT day_change_pct, day_pnl_pct FROM strategy_ai_decisions "
            "WHERE push_date='2026-06-01' AND stock_code='002371'"
        ).fetchone()
        conn.close()
        assert row[0] == 2.1
        assert row[1] == 1.5

    # -- improvements --

    def test_insert_improvement(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        imp_id = repo.insert_improvement(
            {
                "push_date": "2026-06-01",
                "improvement_type": "threshold_tuning",
                "target_module": "funnel",
                "target_param": "min_score",
                "suggested_change": "set min_score to 65",
                "rationale": "过滤太幅度过大",
                "evidence_ids": "E001,E002",
            }
        )
        assert imp_id > 0
        assert _count(db_path, "strategy_improvements") == 1

    def test_get_pending_strategy_improvements(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        repo.insert_improvement(
            {
                "push_date": "2026-06-01",
                "improvement_type": "threshold_tuning",
                "target_module": "funnel",
                "suggested_change": "set min_score to 65",
                "rationale": "test",
                "status": "pending",
            }
        )
        repo.insert_improvement(
            {
                "push_date": "2026-06-01",
                "improvement_type": "new_factor",
                "target_module": "funnel",
                "suggested_change": "add volume_ratio filter",
                "rationale": "test",
                "status": "applied",
            }
        )
        pending = repo.get_pending_improvements()
        assert len(pending) == 1
        assert pending[0]["improvement_type"] == "threshold_tuning"

    def test_apply_improvement(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        imp_id = repo.insert_improvement(
            {
                "push_date": "2026-06-01",
                "improvement_type": "threshold_tuning",
                "target_module": "funnel",
                "suggested_change": "set min_score to 65",
                "rationale": "test",
            }
        )
        repo.apply_improvement(imp_id)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status, applied_date FROM strategy_improvements WHERE id=?",
            (imp_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "applied"
        assert row[1] is not None

    def test_get_funnel_records_empty(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        results = repo.get_funnel_records("2099-01-01")
        assert results == []

    def test_get_ai_decisions_empty(self, db_path):
        self._extend(db_path)
        repo = StrategyRepo(db_path)
        results = repo.get_ai_decisions("2099-01-01")
        assert results == []


# ===================================================================
# AuditRepo
# ===================================================================


class TestAuditRepo:
    """watcher_decision_log / audit_findings / watcher_improvements
    / trade_holdings_review CRUD"""

    @classmethod
    def _extend(cls, db_path):
        _extend(db_path)

    # -- decision_log --

    def test_insert_decision_log(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        log_id = repo.insert_decision_log(
            trade_date="2026-06-01",
            ts="09:35:00",
            decision_type="entry_check",
            stock_code="002371",
            decision_data={"price": 390.0, "verdict": "ok"},
        )
        assert log_id > 0

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT decision_type, stock_code, decision_data FROM watcher_decision_log "
            "WHERE id=?",
            (log_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "entry_check"
        assert row[1] == "002371"
        assert "ok" in row[2]

    def test_get_decision_logs(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        repo.insert_decision_log(
            "2026-06-01", "09:35", "entry_check", "002371", {"v": 1}
        )
        repo.insert_decision_log(
            "2026-06-01", "09:36", "exit_check", "002371", {"v": 2}
        )
        repo.insert_decision_log(
            "2026-06-02", "09:35", "entry_check", "000001", {"v": 3}
        )

        results = repo.get_decision_logs("2026-06-01")
        assert len(results) == 2

        results_filtered = repo.get_decision_logs(
            "2026-06-01", decision_type="exit_check"
        )
        assert len(results_filtered) == 1
        assert results_filtered[0]["decision_type"] == "exit_check"

    def test_get_decision_logs_empty(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        results = repo.get_decision_logs("2099-01-01")
        assert results == []

    # -- audit_findings --

    def test_insert_finding(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        finding_id = repo.insert_audit_finding(
            {
                "trade_date": "2026-06-01",
                "finding_type": "late_entry",
                "severity": "P1",
                "stock_code": "002371",
                "pattern_desc": "建仓时机没有在开盘后 5 分钟内完成",
                "evidence": '{"order_time": "09:38"}',
            }
        )
        assert finding_id > 0

    def test_get_findings_by_date(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        repo.insert_audit_finding(
            {
                "trade_date": "2026-06-01",
                "finding_type": "late_entry",
                "severity": "P1",
                "stock_code": "002371",
                "pattern_desc": "test",
                "evidence": "{}",
            }
        )
        repo.insert_audit_finding(
            {
                "trade_date": "2026-06-01",
                "finding_type": "wrong_zone",
                "severity": "P0",
                "stock_code": "000001",
                "pattern_desc": "test2",
                "evidence": "{}",
            }
        )
        repo.insert_audit_finding(
            {
                "trade_date": "2026-06-02",
                "finding_type": "test",
                "severity": "P2",
                "pattern_desc": "test3",
                "evidence": "{}",
            }
        )

        results = repo.get_audit_findings("2026-06-01")
        assert len(results) == 2
        # severity 排序：P0 应在 P1 前面
        assert results[0]["severity"] == "P0"

    def test_get_findings_empty(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        results = repo.get_audit_findings("2099-01-01")
        assert results == []

    # -- watcher_improvements --

    def test_insert_watcher_improvement(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        imp_id = repo.insert_watcher_improvement(
            {
                "trade_date": "2026-06-01",
                "improvement_type": "threshold_tuning",
                "target_module": "watcher",
                "target_param": "check_interval",
                "suggested_change": "reduce from 60s to 30s",
                "rationale": "响应速度太慢",
            }
        )
        assert imp_id > 0

    def test_get_pending_watcher_improvements(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        repo.insert_watcher_improvement(
            {
                "trade_date": "2026-06-01",
                "improvement_type": "a",
                "target_module": "watcher",
                "suggested_change": "x",
                "rationale": "test",
                "status": "pending",
            }
        )
        repo.insert_watcher_improvement(
            {
                "trade_date": "2026-06-01",
                "improvement_type": "b",
                "target_module": "watcher",
                "suggested_change": "y",
                "rationale": "test",
                "status": "applied",
            }
        )
        pending = repo.get_pending_watcher_improvements()
        assert len(pending) == 1
        assert pending[0]["improvement_type"] == "a"

    def test_apply_watcher_improvement(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        imp_id = repo.insert_watcher_improvement(
            {
                "trade_date": "2026-06-01",
                "improvement_type": "a",
                "target_module": "watcher",
                "suggested_change": "x",
                "rationale": "test",
            }
        )
        repo.update_watcher_improvement_status(imp_id, "applied", "2026-06-02")

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status, applied_date FROM watcher_improvements WHERE id=?",
            (imp_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "applied"
        assert row[1] == "2026-06-02"

    def test_update_watcher_improvement_effectiveness(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        imp_id = repo.insert_watcher_improvement(
            {
                "trade_date": "2026-06-01",
                "improvement_type": "a",
                "target_module": "watcher",
                "suggested_change": "x",
                "rationale": "test",
            }
        )
        repo.update_watcher_improvement_effectiveness(imp_id, "有效改善了响应速度")
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT effectiveness_check FROM watcher_improvements WHERE id=?", (imp_id,)
        ).fetchone()
        conn.close()
        assert row[0] is not None

    # -- holdings_review --

    def test_insert_holdings_review(self, db_path):
        self._extend(db_path)
        repo = AuditRepo(db_path)
        rid = repo.insert_holdings_review(
            {
                "trade_date": "2026-06-01",
                "created_at": datetime.now().isoformat(),
                "stock_code": "002371",
                "account": "paper",
                "action": "hold",
                "new_stop_loss": 375.0,
                "new_take_profit": 450.0,
                "expected_holding_days": 5,
                "tomorrow_outlook": "看涨",
                "reason": "趋势良好",
            }
        )
        assert rid > 0

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT action, new_stop_loss FROM trade_holdings_review WHERE id=?", (rid,)
        ).fetchone()
        conn.close()
        assert row[0] == "hold"
        assert row[1] == 375.0

    def test_insert_holdings_review_duplicate(self, db_path):
        """trade_holdings_review 有 UNIQUE(trade_date, stock_code, account)。"""
        self._extend(db_path)
        repo = AuditRepo(db_path)
        d = {
            "trade_date": "2026-06-01",
            "created_at": datetime.now().isoformat(),
            "stock_code": "002371",
            "account": "paper",
            "action": "hold",
            "reason": "test",
        }
        repo.insert_holdings_review(d)
        repo.insert_holdings_review(dict(d, action="sell"))
        assert _count(db_path, "trade_holdings_review") == 1


# ===================================================================
# TradeRepository (Facade)
# ===================================================================


class TestTradeRepository:
    """向后兼容 TradeRepository 的委托是否正确。"""

    def test_init_custom_path(self, db_path):
        repo = TradeRepository(db_path=db_path)
        assert repo.db_path == db_path
        assert repo._signal.db_path == db_path
        assert repo._order.db_path == db_path
        assert repo._portfolio.db_path == db_path
        assert repo._strategy.db_path == db_path
        assert repo._audit.db_path == db_path

    def test_init_raises_in_e2e_mode(self, db_path):
        os.environ["E2E_TEST_MODE"] = "1"
        try:
            with pytest.raises(RuntimeError, match="E2E_TEST_MODE"):
                TradeRepository()
        finally:
            del os.environ["E2E_TEST_MODE"]

    def test_init_default_path(self, db_path):
        """不传 db_path 时会使用生产 DATABASE_PATH。"""
        from system.config.settings import DATABASE_PATH

        repo = TradeRepository()
        assert repo.db_path == DATABASE_PATH

    # -- 验证 delegate 方法正确转发 --

    def test_insert_signal_delegates(self, db_path):
        repo = TradeRepository(db_path=db_path)
        sid = repo.insert_signal(
            {
                "account": "paper",
                "trade_date": "2026-06-01",
                "created_at": datetime.now().isoformat(),
                "signal_type": "BUY",
                "signal_source": "TEST",
                "stock_code": "002371",
                "stock_name": "北方华创",
            }
        )
        assert sid > 0
        pending = repo.get_pending_signals(account="paper")
        assert len(pending) == 1

    def test_update_signal_status_delegates(self, db_path):
        repo = TradeRepository(db_path=db_path)
        sid = repo.insert_signal(
            {
                "account": "paper",
                "trade_date": "2026-06-01",
                "created_at": datetime.now().isoformat(),
                "signal_type": "BUY",
                "signal_source": "TEST",
                "stock_code": "002371",
            }
        )
        repo.update_signal_status(sid, "bought")
        pending = repo.get_pending_signals(account="paper")
        assert len(pending) == 0

    def test_expire_signals_delegates(self, db_path):
        repo = TradeRepository(db_path=db_path)
        repo.insert_signal(
            {
                "account": "paper",
                "trade_date": "2026-05-01",
                "created_at": datetime.now().isoformat(),
                "signal_type": "BUY",
                "signal_source": "TEST",
                "stock_code": "002371",
                "strategy_name": "ai_advisor_v1",
            }
        )
        repo.expire_old_pending_signals("2026-06-01")
        expired = repo.get_expired_signals(before_date="2026-07-01")
        assert len(expired) == 1

    def test_order_crud_delegates(self, db_path):
        repo = TradeRepository(db_path=db_path)
        oid = repo.insert_order(
            {
                "account": "paper",
                "trade_date": "2026-06-01",
                "order_time": "09:30:00",
                "stock_code": "002371",
                "order_type": "buy",
                "order_price": 390.0,
                "order_volume": 100,
            }
        )
        assert oid > 0
        orders = repo.get_orders_by_date("2026-06-01", account="paper")
        assert len(orders) == 1

    def test_snapshot_crud_delegates(self, db_path):
        repo = TradeRepository(db_path=db_path)
        repo.insert_snapshot(
            {
                "trade_date": "2026-06-01",
                "total_value": 1000000.0,
                "cash": 500000.0,
                "market_value": 500000.0,
                "account": "paper",
            }
        )
        snap = repo.get_latest_snapshot("paper")
        assert snap is not None
        assert snap["total_value"] == 1000000.0

    def test_snapshot_before_delegates(self, db_path):
        repo = TradeRepository(db_path=db_path)
        repo.insert_snapshot(
            {"trade_date": "2026-05-31", "total_value": 900000.0, "account": "paper"}
        )
        repo.insert_snapshot(
            {"trade_date": "2026-06-01", "total_value": 1000000.0, "account": "paper"}
        )
        snap = repo.get_latest_snapshot_before("2026-06-01", "paper")
        assert snap["total_value"] == 900000.0

    def test_get_first_snapshot_of_day(self, db_path):
        """get_first_snapshot_of_day 在实现上目前和 get_latest_snapshot 相同。"""
        repo = TradeRepository(db_path=db_path)
        repo.insert_snapshot(
            {"trade_date": "2026-06-01", "total_value": 100.0, "account": "paper"}
        )
        result = repo.get_first_snapshot_of_day("2026-06-01", "paper")
        assert result is not None

    def test_position_crud_delegates(self, db_path):
        repo = TradeRepository(db_path=db_path)
        repo.insert_positions(
            "2026-06-01",
            "paper",
            [
                {
                    "stock_code": "002371",
                    "stock_name": "北方华创",
                    "volume": 100,
                    "avg_cost": 390.0,
                    "current_price": 400.0,
                    "market_value": 40000.0,
                    "pnl": 1000.0,
                    "pnl_pct": 2.56,
                },
            ],
        )
        pos = repo.get_positions_by_date("2026-06-01", "paper")
        assert len(pos) == 1
        latest = repo.get_latest_positions("paper")
        assert len(latest) == 1

    def test_strategy_methods_delegate(self, db_path):
        _extend(db_path)
        repo = TradeRepository(db_path=db_path)
        repo.insert_funnel_batch(
            [
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "002371",
                    "stock_name": "北方华创",
                    "rank_position": 1,
                    "raw_snapshot": "{}",
                }
            ]
        )
        records = repo.get_funnel_records("2026-06-01")
        assert len(records) == 1

        repo.insert_ai_decisions_batch(
            [
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "002371",
                    "rank_in_prompt": 1,
                    "verdict": "buy",
                }
            ]
        )
        decisions = repo.get_ai_decisions("2026-06-01")
        assert len(decisions) == 1

        imp_id = repo.insert_improvement(
            {
                "push_date": "2026-06-01",
                "improvement_type": "a",
                "target_module": "funnel",
                "suggested_change": "x",
                "rationale": "test",
            }
        )
        pending = repo.get_pending_improvements()
        assert len(pending) == 1

        repo.apply_improvement(imp_id)
        pending_after = repo.get_pending_improvements()
        assert len(pending_after) == 0

    def test_audit_methods_delegate(self, db_path):
        _extend(db_path)
        repo = TradeRepository(db_path=db_path)

        # decision_log
        log_id = repo.insert_decision_log(
            "2026-06-01", "09:35", "entry_check", "002371", {"v": 1}
        )
        assert log_id > 0
        logs = repo.get_decision_logs("2026-06-01")
        assert len(logs) == 1

        # audit_finding
        finding_id = repo.insert_audit_finding(
            {
                "trade_date": "2026-06-01",
                "finding_type": "late_entry",
                "severity": "P1",
                "stock_code": "002371",
                "pattern_desc": "test",
                "evidence": "{}",
            }
        )
        assert finding_id > 0
        findings = repo.get_audit_findings("2026-06-01")
        assert len(findings) == 1

        # watcher_improvement
        wimp_id = repo.insert_watcher_improvement(
            {
                "trade_date": "2026-06-01",
                "improvement_type": "a",
                "target_module": "watcher",
                "suggested_change": "x",
                "rationale": "test",
            }
        )
        assert wimp_id > 0

        pending_wimps = repo.get_pending_watcher_improvements()
        assert len(pending_wimps) == 1

        repo.update_watcher_improvement_status(wimp_id, "applied", "2026-06-02")
        pending_wimps_after = repo.get_pending_watcher_improvements()
        assert len(pending_wimps_after) == 0

        # holdings_review
        review_id = repo.insert_holdings_review(
            {
                "trade_date": "2026-06-01",
                "created_at": datetime.now().isoformat(),
                "stock_code": "002371",
                "account": "paper",
                "action": "hold",
                "reason": "test",
            }
        )
        assert review_id > 0

    def test_apply_holdings_review_sl_tp(self, db_path):
        _extend(db_path)
        repo = TradeRepository(db_path=db_path)
        # 先插入一个 status=bought 的 signal
        sid = repo.insert_signal(
            {
                "account": "paper",
                "trade_date": "2026-06-01",
                "created_at": datetime.now().isoformat(),
                "signal_type": "BUY",
                "signal_source": "REVIEW",
                "stock_code": "002371",
                "status": "bought",
            }
        )
        assert sid > 0

        repo.apply_holdings_review_sl_tp(
            "2026-06-02", "002371", new_stop_loss=370.0, new_take_profit=450.0
        )
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT stop_loss, take_profit FROM trade_signals WHERE id=?", (sid,)
        ).fetchone()
        conn.close()
        assert row[0] == 370.0
        assert row[1] == 450.0

    # -- legacy methods --

    def test_upsert_and_get_lessons(self, db_path):
        _extend(db_path)
        repo = TradeRepository(db_path=db_path)
        # insert
        repo.upsert_lesson(
            {
                "lesson_type": "strategy",
                "lesson_key": "no_entry_after_0935",
                "lesson_content": "建仓窗口在开盘后 5 分钟内",
                "trigger_conditions": "time > 09:35",
                "first_date": "2026-06-01",
                "last_date": "2026-06-01",
            }
        )
        lessons = repo.get_active_lessons()
        assert len(lessons) == 1
        assert lessons[0]["occurrence_count"] == 1

        # upsert — 相同 key 增加 count
        repo.upsert_lesson(
            {
                "lesson_type": "strategy",
                "lesson_key": "no_entry_after_0935",
                "lesson_content": "建仓窗口在开盘后 5 分钟内",
                "trigger_conditions": "time > 09:35",
                "first_date": "2026-06-01",
                "last_date": "2026-06-02",
            }
        )
        lessons = repo.get_active_lessons("strategy")
        assert len(lessons) == 1
        assert lessons[0]["occurrence_count"] == 2
        assert lessons[0]["last_date"] == "2026-06-02"

    def test_upsert_and_get_watcher_lessons(self, db_path):
        _extend(db_path)
        repo = TradeRepository(db_path=db_path)
        wid = repo.upsert_watcher_lesson(
            lesson_type="pattern",
            lesson_key="gap_up_fade",
            lesson_content="跳空上涨后回落风险高",
            trigger_conditions={"gap_pct": ">3%"},
            trade_date="2026-06-01",
        )
        assert wid > 0

        lessons = repo.get_active_watcher_lessons()
        assert len(lessons) == 1

        # upsert again — 增加 occurrence_count
        repo.upsert_watcher_lesson(
            lesson_type="pattern",
            lesson_key="gap_up_fade",
            lesson_content="same",
            trade_date="2026-06-02",
        )
        lessons = repo.get_active_watcher_lessons()
        assert lessons[0]["occurrence_count"] == 2

    def test_get_morning_sector_bias(self, db_path):
        _extend(db_path)
        repo = TradeRepository(db_path=db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO morning_sector_bias (trade_date, sector_name, bias, stock_codes, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-06-01", "半导体", "focus", '["002371", "688981"]', "政策利好"),
        )
        conn.commit()
        conn.close()

        results = repo.get_morning_sector_bias("2026-06-01")
        assert len(results) == 1
        assert results[0]["sector_name"] == "半导体"
        assert results[0]["bias"] == "focus"
        assert "002371" in results[0]["stock_codes"]

    def test_get_bought_signals_with_entry(self, db_path):
        repo = TradeRepository(db_path=db_path)
        sid = repo.insert_signal(
            {
                "account": "paper",
                "trade_date": "2026-06-01",
                "created_at": datetime.now().isoformat(),
                "signal_type": "BUY",
                "signal_source": "REVIEW",
                "stock_code": "002371",
                "status": "bought",
            }
        )
        repo.insert_order(
            {
                "account": "paper",
                "signal_id": sid,
                "trade_date": "2026-06-01",
                "order_time": "09:31:00",
                "stock_code": "002371",
                "order_type": "buy",
                "order_status": "filled",
                "filled_volume": 100,
                "filled_price": 390.0,
            }
        )
        bought = repo.get_bought_signals_with_entry()
        assert len(bought) == 1
        assert bought[0]["entry_price"] == 390.0

    def test_get_signal_for_pos_meta(self, db_path):
        repo = TradeRepository(db_path=db_path)
        repo.insert_signal(
            {
                "account": "paper",
                "trade_date": "2026-06-01",
                "created_at": datetime.now().isoformat(),
                "signal_type": "BUY",
                "signal_source": "REVIEW",
                "stock_code": "002371",
                "stop_loss": 370.0,
                "take_profit": 440.0,
                "status": "bought",
            }
        )
        meta = repo.get_signal_for_pos_meta("002371")
        assert meta is not None
        assert meta["stop_loss"] == 370.0

    def test_get_signal_for_pos_meta_none(self, db_path):
        repo = TradeRepository(db_path=db_path)
        meta = repo.get_signal_for_pos_meta("nonexistent")
        assert meta is None

    def test_get_buy_dates(self, db_path):
        repo = TradeRepository(db_path=db_path)
        repo.insert_order(
            {
                "account": "paper",
                "trade_date": "2026-06-01",
                "order_time": "2026-06-01 09:31:00",
                "stock_code": "002371",
                "order_type": "buy",
                "order_status": "filled",
            }
        )
        repo.insert_order(
            {
                "account": "paper",
                "trade_date": "2026-06-02",
                "order_time": "2026-06-02 09:31:00",
                "stock_code": "000001",
                "order_type": "buy",
                "order_status": "filled",
            }
        )
        dates = repo.get_buy_dates(["002371", "000001"])
        assert "002371" in dates
        assert "000001" in dates

    def test_get_buy_dates_empty(self, db_path):
        repo = TradeRepository(db_path=db_path)
        assert repo.get_buy_dates([]) == {}
        assert repo.get_buy_dates(["nonexistent"]) == {}

    def test_resolve_name(self, db_path):
        repo = TradeRepository(db_path=db_path)
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stock_basic (
                trade_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                price REAL, open REAL, high REAL, low REAL, prev_close REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_basic_date_code
                ON stock_basic(trade_date, stock_code);
        """)
        conn.execute(
            "INSERT INTO stock_basic (trade_date, stock_code, stock_name) "
            "VALUES ('2026-06-01', '002371', '北方华创')"
        )
        conn.commit()
        conn.close()

        name = repo.resolve_name("002371")
        assert name == "北方华创"
        name_missing = repo.resolve_name("999999")
        assert name_missing == "999999"


# ===================================================================
# Transaction / Integrity
# ===================================================================


class TestTransactionHandling:
    """批量操作与事务完整性验证"""

    def test_batch_funnel_commit_atomicity(self, db_path):
        """批量插入多条漏斗记录：全部成功则全部可见。"""
        _extend(db_path)
        repo = StrategyRepo(db_path)
        rows = [
            {
                "push_date": "2026-06-01",
                "trade_date": "2026-06-01",
                "stock_code": "002371",
                "stock_name": "A",
                "rank_position": 1,
                "raw_snapshot": "{}",
            },
            {
                "push_date": "2026-06-01",
                "trade_date": "2026-06-01",
                "stock_code": "000001",
                "stock_name": "B",
                "rank_position": 2,
                "raw_snapshot": "{}",
            },
            {
                "push_date": "2026-06-01",
                "trade_date": "2026-06-01",
                "stock_code": "600519",
                "stock_name": "C",
                "rank_position": 3,
                "raw_snapshot": "{}",
            },
        ]
        repo.insert_funnel_batch(rows)
        assert _count(db_path, "strategy_funnel") == 3

    def test_failed_insert_does_not_corrupt_existing(self, db_path):
        """非法字段的插入应抛出异常，不影响已有数据。"""
        _extend(db_path)
        repo = StrategyRepo(db_path)

        # 先插入一条合法记录
        repo.insert_funnel_batch(
            [
                {
                    "push_date": "2026-06-01",
                    "trade_date": "2026-06-01",
                    "stock_code": "002371",
                    "stock_name": "A",
                    "rank_position": 1,
                    "raw_snapshot": "{}",
                }
            ]
        )
        assert _count(db_path, "strategy_funnel") == 1

        # 尝试插入非法字段 -> 抛 ValueError
        with pytest.raises(ValueError, match="非法列名"):
            repo.insert_funnel_batch(
                [
                    {
                        "push_date": "2026-06-01",
                        "trade_date": "2026-06-01",
                        "stock_code": "000001",
                        "stock_name": "B",
                        "rank_position": 2,
                        "raw_snapshot": "{}",
                        "nonexistent_field": "xxx",  # 不在 _FUNNEL_COLS 中
                    }
                ]
            )

        # 已有数据保持不变
        assert _count(db_path, "strategy_funnel") == 1

    def test_multiple_inserts_in_sequence_all_succeed(self, db_path):
        """连续多次插入不同表，数据完整。"""
        repo_s = SignalRepo(db_path)
        repo_o = OrderRepo(db_path)

        now = datetime.now().isoformat()
        sid1 = repo_s.insert(
            {
                "trade_date": "2026-06-01",
                "created_at": now,
                "signal_type": "BUY",
                "signal_source": "TEST",
                "stock_code": "002371",
                "account": "paper",
            }
        )
        sid2 = repo_s.insert(
            {
                "trade_date": "2026-06-01",
                "created_at": now,
                "signal_type": "SELL",
                "signal_source": "TEST",
                "stock_code": "000001",
                "account": "paper",
            }
        )
        repo_o.insert(
            {
                "trade_date": "2026-06-01",
                "order_time": now,
                "stock_code": "002371",
                "order_type": "buy",
                "order_price": 390.0,
                "order_volume": 100,
                "signal_id": sid1,
                "account": "paper",
            }
        )
        repo_o.insert(
            {
                "trade_date": "2026-06-01",
                "order_time": now,
                "stock_code": "000001",
                "order_type": "sell",
                "order_price": 12.0,
                "order_volume": 1000,
                "signal_id": sid2,
                "account": "paper",
            }
        )

        assert _count(db_path, "trade_signals") == 2
        assert _count(db_path, "trade_orders") == 2

        conn = sqlite3.connect(db_path)
        s_002371 = conn.execute(
            "SELECT id, signal_type FROM trade_signals WHERE stock_code='002371'"
        ).fetchone()
        o_002371 = conn.execute(
            "SELECT id, signal_id FROM trade_orders WHERE stock_code='002371'"
        ).fetchone()
        conn.close()
        assert s_002371[1] == "BUY"
        assert o_002371[1] == sid1

    def test_audit_finding_with_nested_data(self, db_path):
        """验证 audit_finding 中 dict/list 字段被 JSON 序列化存储。"""
        _extend(db_path)
        repo = AuditRepo(db_path)
        evidence = {"orders": [{"id": 1, "time": "09:30"}], "score": 0.85}
        finding_id = repo.insert_audit_finding(
            {
                "trade_date": "2026-06-01",
                "finding_type": "test",
                "severity": "P2",
                "pattern_desc": "test pattern",
                "evidence": evidence,
            }
        )
        assert finding_id > 0

        conn = sqlite3.connect(db_path)
        raw = conn.execute(
            "SELECT evidence FROM audit_findings WHERE id=?", (finding_id,)
        ).fetchone()[0]
        conn.close()
        parsed = json.loads(raw)
        assert parsed["score"] == 0.85
        assert len(parsed["orders"]) == 1

    def test_watcher_improvement_with_list_evidence(self, db_path):
        """验证 watcher_improvement 中 list evidence_ids 被 JSON 序列化存储。"""
        _extend(db_path)
        repo = AuditRepo(db_path)
        imp_id = repo.insert_watcher_improvement(
            {
                "trade_date": "2026-06-01",
                "improvement_type": "threshold_tuning",
                "target_module": "watcher",
                "suggested_change": "x",
                "rationale": "test",
                "evidence_ids": ["E001", "E002"],
            }
        )
        conn = sqlite3.connect(db_path)
        raw = conn.execute(
            "SELECT evidence_ids FROM watcher_improvements WHERE id=?", (imp_id,)
        ).fetchone()[0]
        conn.close()
        parsed = json.loads(raw)
        assert parsed == ["E001", "E002"]

    def test_signal_insert_without_optional_fields(self, db_path):
        """只传必填字段仍能成功插入。"""
        repo = SignalRepo(db_path)
        now = datetime.now().isoformat()
        sid = repo.insert(
            {
                "trade_date": "2026-06-01",
                "created_at": now,
                "signal_type": "BUY",
                "signal_source": "TEST",
                "stock_code": "002371",
                "account": "paper",
            }
        )
        assert sid > 0
        assert _count(db_path, "trade_signals") == 1

    def test_order_insert_without_optional_fields(self, db_path):
        repo = OrderRepo(db_path)
        oid = repo.insert(
            {
                "trade_date": "2026-06-01",
                "order_time": "09:30:00",
                "stock_code": "002371",
                "order_type": "buy",
                "order_volume": 100,
                "order_price": 390.0,
                "account": "paper",
            }
        )
        assert oid > 0
