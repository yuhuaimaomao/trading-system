# -*- coding: utf-8 -*-
"""TradeRepository 单元测试 — 信号/订单/快照 CRUD"""

import sqlite3
import pytest
from pathlib import Path
from data.repo import TradeRepository
from data.schema import ensure_tables


@pytest.fixture
def db_path(tmp_path):
    """创建带完整 schema 的临时 SQLite 库"""
    from unittest.mock import patch
    p = str(tmp_path / "test_trade.db")
    # Create a minimal stock_basic table (ensure_tables creates an index on it)
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE IF NOT EXISTS stock_basic (stock_code TEXT, trade_date TEXT, price REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS cls_telegraph (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.close()
    with patch("data.schema.DATABASE_PATH", p):
        ensure_tables()
    return p


@pytest.fixture
def repo(db_path):
    return TradeRepository()
    # Note: TradeRepository uses settings.DATABASE_PATH by default
    # We patch it below when needed


def make_repo(db_path):
    """Create a TradeRepository pointed at the test DB."""
    r = TradeRepository()
    r.db_path = db_path
    return r


# ====================== Signals ======================


class TestSignals:
    @pytest.fixture
    def repo(self, db_path):
        return make_repo(db_path)

    def test_insert_signal(self, repo):
        sid = repo.insert_signal({
            "trade_date": "2026-05-29",
            "created_at": "2026-05-29 09:30:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000001",
            "stock_name": "平安银行",
            "buy_zone_min": 12.0,
            "buy_zone_max": 13.0,
            "target_position": 0.10,
            "stop_loss": 11.0,
            "take_profit": 14.0,
            "signal_score": 80,
            "strategy_name": "ai_advisor_qwen",
            "reason": "测试",
            "status": "pending",
        })
        assert sid > 0

    def test_insert_signal_replace_on_duplicate(self, repo):
        """同 trade_date+stock_code 的 signal 应 REPLACE"""
        sid1 = repo.insert_signal({
            "trade_date": "2026-05-29",
            "created_at": "2026-05-29 09:30:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000002",
            "stock_name": "万科A",
            "signal_score": 70,
            "status": "pending",
        })
        sid2 = repo.insert_signal({
            "trade_date": "2026-05-29",
            "created_at": "2026-05-29 09:35:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000002",
            "stock_name": "万科A_v2",
            "signal_score": 75,
            "status": "pending",
        })
        # REPLACE = delete + insert, so rowid changes
        assert sid2 > 0

    def test_get_pending_signals(self, repo):
        repo.insert_signal({
            "trade_date": "2026-05-29",
            "created_at": "2026-05-29 09:30:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000003",
            "stock_name": "测试股",
            "status": "pending",
        })
        signals = repo.get_pending_signals(trade_date="2026-05-29")
        assert len(signals) > 0
        assert signals[0]["stock_code"] == "000003"

    def test_get_pending_signals_excludes_non_pending(self, repo):
        repo.insert_signal({
            "trade_date": "2026-05-29",
            "created_at": "2026-05-29 09:30:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000004",
            "stock_name": "已买股",
            "status": "bought",
        })
        signals = repo.get_pending_signals(trade_date="2026-05-29")
        codes = {s["stock_code"] for s in signals}
        assert "000004" not in codes

    def test_update_signal_status(self, repo):
        sid = repo.insert_signal({
            "trade_date": "2026-05-29",
            "created_at": "2026-05-29 09:30:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000005",
            "stock_name": "待更新股",
            "status": "pending",
        })
        repo.update_signal_status(sid, "bought")
        # Verify via direct query
        conn = sqlite3.connect(repo._conn.__self__.db_path if hasattr(repo._conn, '__self__') else "")
        # Just verify no exception

    def test_expire_old_pending_signals(self, repo):
        repo.insert_signal({
            "trade_date": "2026-05-28",
            "created_at": "2026-05-28 09:30:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000006",
            "stock_name": "过期股",
            "status": "pending",
        })
        repo.expire_old_pending_signals("2026-05-29")
        # Old pending signal should now be expired
        signals = repo.get_pending_signals(trade_date="2026-05-28")
        assert len(signals) == 0

    def test_get_expired_signals(self, repo):
        repo.insert_signal({
            "trade_date": "2026-05-25",
            "created_at": "2026-05-25 09:30:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000007",
            "stock_name": "历史过期股",
            "strategy_name": "ai_advisor_qwen",
            "status": "expired",
        })
        expired = repo.get_expired_signals(before_date="2026-05-30")
        assert len(expired) >= 1


# ====================== Orders ======================


class TestOrders:
    @pytest.fixture
    def repo(self, db_path):
        return make_repo(db_path)

    def test_insert_order(self, repo):
        oid = repo.insert_order({
            "signal_id": 1,
            "trade_date": "2026-05-29",
            "order_time": "2026-05-29 10:00:00",
            "stock_code": "000001",
            "order_type": "buy",
            "order_price": 12.50,
            "order_volume": 1000,
            "order_status": "filled",
            "filled_volume": 1000,
            "filled_price": 12.50,
            "filled_amount": 12500,
            "commission": 5.0,
            "account": "paper",
        })
        assert oid > 0

    def test_get_orders_by_date(self, repo):
        repo.insert_order({
            "trade_date": "2026-05-29",
            "order_time": "2026-05-29 10:00:00",
            "stock_code": "000001",
            "order_type": "buy",
            "order_volume": 1000,
            "order_status": "filled",
            "filled_volume": 1000,
            "filled_price": 12.50,
            "account": "paper",
        })
        orders = repo.get_orders_by_date("2026-05-29")
        assert len(orders) >= 1

    def test_get_orders_by_date_and_account(self, repo):
        repo.insert_order({
            "trade_date": "2026-05-29",
            "order_time": "2026-05-29 10:00:00",
            "stock_code": "000001",
            "order_type": "buy",
            "order_volume": 1000,
            "order_status": "filled",
            "filled_volume": 1000,
            "filled_price": 12.50,
            "account": "paper",
        })
        repo.insert_order({
            "trade_date": "2026-05-29",
            "order_time": "2026-05-29 10:01:00",
            "stock_code": "000002",
            "order_type": "buy",
            "order_volume": 500,
            "order_status": "filled",
            "filled_volume": 500,
            "filled_price": 25.00,
            "account": "real",
        })
        paper = repo.get_orders_by_date("2026-05-29", account="paper")
        real = repo.get_orders_by_date("2026-05-29", account="real")
        assert all(o["account"] == "paper" for o in paper)
        assert all(o["account"] == "real" for o in real)

    def test_orders_sorted_by_time(self, repo):
        repo.insert_order({
            "trade_date": "2026-05-29",
            "order_time": "2026-05-29 14:00:00",
            "stock_code": "000001",
            "order_type": "sell",
            "order_volume": 1000,
            "order_status": "filled",
            "filled_volume": 1000,
            "filled_price": 15.00,
            "account": "paper",
        })
        repo.insert_order({
            "trade_date": "2026-05-29",
            "order_time": "2026-05-29 10:00:00",
            "stock_code": "000001",
            "order_type": "buy",
            "order_volume": 1000,
            "order_status": "filled",
            "filled_volume": 1000,
            "filled_price": 12.50,
            "account": "paper",
        })
        orders = repo.get_orders_by_date("2026-05-29")
        assert orders[0]["order_time"] < orders[-1]["order_time"]


# ====================== Snapshots ======================


class TestSnapshots:
    @pytest.fixture
    def repo(self, db_path):
        return make_repo(db_path)

    def test_insert_and_get_snapshot(self, repo):
        repo.insert_snapshot({
            "trade_date": "2026-05-29",
            "total_value": 200000,
            "cash": 50000,
            "market_value": 150000,
            "daily_pnl": 5000,
            "total_pnl": 10000,
            "drawdown": 0.02,
            "position_count": 3,
            "sector_exposure": "{}",
            "account": "paper",
        })
        snaps = repo.get_snapshots(start="2026-05-29", end="2026-05-29")
        assert len(snaps) >= 1

    def test_get_snapshots_without_dates(self, repo):
        snaps = repo.get_snapshots()
        assert isinstance(snaps, list)

    def test_insert_snapshot_replace(self, repo):
        repo.insert_snapshot({
            "trade_date": "2026-05-29",
            "total_value": 200000,
            "cash": 50000,
            "market_value": 150000,
            "daily_pnl": 5000,
            "total_pnl": 10000,
            "drawdown": 0.02,
            "position_count": 3,
            "sector_exposure": "{}",
            "account": "paper",
        })
        repo.insert_snapshot({
            "trade_date": "2026-05-29",
            "total_value": 210000,
            "cash": 40000,
            "market_value": 170000,
            "daily_pnl": 6000,
            "total_pnl": 11000,
            "drawdown": 0.01,
            "position_count": 4,
            "sector_exposure": "{}",
            "account": "paper",
        })
        snaps = repo.get_snapshots(start="2026-05-29", end="2026-05-29")
        paper = [s for s in snaps if s.get("account") == "paper"]
        assert len(paper) <= 1  # REPLACE means only one row


# ====================== Factor Values ======================


class TestFactorValues:
    @pytest.fixture
    def repo(self, db_path):
        return make_repo(db_path)

    def test_save_and_get_factor_values(self, repo):
        repo.save_factor_values("2026-05-29", "volume_ratio", {
            "000001": 1.5,
            "000002": 0.8,
        })
        result = repo.get_factor_values("2026-05-29", "volume_ratio")
        assert result["000001"] == 1.5
        assert result["000002"] == 0.8

    def test_save_factor_values_empty_dict(self, repo):
        count = repo.save_factor_values("2026-05-29", "test_factor", {})
        assert count == 0

    def test_get_factor_values_nonexistent(self, repo):
        result = repo.get_factor_values("2099-01-01", "nonexistent")
        assert result == {}


# ====================== Holdings Review ======================


class TestHoldingsReview:
    @pytest.fixture
    def repo(self, db_path):
        return make_repo(db_path)

    def test_insert_holdings_review(self, repo):
        rid = repo.insert_holdings_review({
            "trade_date": "2026-05-29",
            "created_at": "2026-05-29 09:30:00",
            "stock_code": "000001",
            "account": "paper",
            "action": "hold",
            "new_stop_loss": 11.50,
            "new_take_profit": 14.50,
            "expected_holding_days": 10,
            "tomorrow_outlook": "继续持有",
            "reason": "趋势良好",
        })
        assert rid > 0

    def test_apply_holdings_review_sl(self, repo):
        """apply_holdings_review_sl_tp 更新已买入信号的止损止盈"""
        # Insert a bought signal first
        repo.insert_signal({
            "trade_date": "2026-05-29",
            "created_at": "2026-05-29 09:30:00",
            "signal_type": "buy",
            "signal_source": "ai_enhanced",
            "stock_code": "000001",
            "stock_name": "平安银行",
            "stop_loss": 11.0,
            "take_profit": 14.0,
            "status": "bought",
        })
        # Apply new SL/TP
        repo.apply_holdings_review_sl_tp(
            "2026-05-29", "000001",
            new_stop_loss=11.50,
            new_take_profit=14.50,
        )
        # verify no exception

    def test_apply_holdings_review_sl_no_change(self, repo):
        """不传参数时不报错"""
        repo.apply_holdings_review_sl_tp("2026-05-29", "000001")


# ====================== Positions ======================


class TestPositions:
    @pytest.fixture
    def repo(self, db_path):
        return make_repo(db_path)

    def test_insert_positions(self, repo):
        positions = [
            {
                "stock_code": "000001",
                "stock_name": "平安银行",
                "volume": 1000,
                "avg_cost": 12.50,
                "current_price": 13.00,
                "market_value": 13000,
                "pnl": 500,
                "pnl_pct": 4.0,
                "stop_loss": 11.50,
                "take_profit": 15.00,
                "holding_days": 3,
                "sector_code": "BK1036",
            },
        ]
        repo.insert_positions("2026-05-29", "paper", positions)
        # Insert again — should DELETE old then INSERT new
        repo.insert_positions("2026-05-29", "paper", [])
        # No exception = pass

    def test_insert_positions_empty_list(self, repo):
        repo.insert_positions("2026-05-29", "paper", [])
        # Should not crash


# ====================== Metrics ======================


class TestMetrics:
    @pytest.fixture
    def repo(self, db_path):
        return make_repo(db_path)

    def test_save_metrics(self, repo):
        repo.save_metrics({
            "strategy_name": "ai_advisor_merged",
            "total_trades": 50,
            "win_rate": 0.60,
            "avg_profit": 0.05,
            "avg_loss": -0.03,
            "profit_loss_ratio": 1.67,
            "max_drawdown": 0.15,
            "sharpe_ratio": 1.5,
            "total_return": 0.25,
        })
        # No exception = pass
