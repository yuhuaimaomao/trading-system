"""TradeRepository 单元测试 — 信号/订单 CRUD"""

from data.repo import TradeRepository


class TestSignalCRUD:
    def test_insert_and_get_pending(self, db_path, sample_signal):
        repo = TradeRepository(db_path=db_path)
        sid = repo.insert_signal(sample_signal)
        assert sid > 0

        pending = repo.get_pending_signals("2026-06-01", account="paper")
        assert len(pending) == 1
        assert pending[0]["stock_code"] == "002371"

    def test_update_status(self, db_path, sample_signal):
        repo = TradeRepository(db_path=db_path)
        sid = repo.insert_signal(sample_signal)
        repo.update_signal_status(sid, "bought")

        pending = repo.get_pending_signals("2026-06-01", account="paper")
        assert len(pending) == 0  # bought 的不在 pending 里

    def test_expire_old_signals(self, db_path, sample_signal):
        repo = TradeRepository(db_path=db_path)
        # 插入一个"旧日期"的信号
        old_signal = {**sample_signal, "trade_date": "2026-05-30"}
        repo.insert_signal(old_signal)
        # 用 2026-06-01 过期它（trade_date < 2026-06-01）
        repo.expire_old_pending_signals("2026-06-01")

        pending = repo.get_pending_signals("2026-05-30", account="paper")
        assert len(pending) == 0

    def test_get_pending_empty(self, db_path):
        repo = TradeRepository(db_path=db_path)
        pending = repo.get_pending_signals("2026-06-01", account="paper")
        assert pending == []


class TestOrderCRUD:
    def test_insert_and_get(self, db_path):
        repo = TradeRepository(db_path=db_path)
        oid = repo.insert_order(
            {
                "trade_date": "2026-06-01",
                "order_time": "2026-06-01 10:00:00",
                "stock_code": "002371",
                "order_type": "buy",
                "order_price": 390.0,
                "order_volume": 100,
                "filled_price": 390.0,
                "filled_volume": 100,
                "filled_amount": 39000.0,
                "commission": 33.15,
                "order_status": "filled",
                "account": "paper",
            }
        )
        assert oid > 0

        orders = repo.get_orders_by_date("2026-06-01", account="paper")
        assert len(orders) == 1
        assert orders[0]["stock_code"] == "002371"


class TestSnapshotCRUD:
    def test_insert_and_latest(self, db_path):
        repo = TradeRepository(db_path=db_path)
        repo.insert_snapshot(
            {
                "trade_date": "2026-06-01",
                "total_value": 210000,
                "cash": 50000,
                "market_value": 160000,
                "daily_pnl": 10000,
                "total_pnl": 10000,
                "drawdown": 0.0,
                "position_count": 2,
                "sector_exposure": "{}",
                "account": "paper",
            }
        )

        snap = repo.get_latest_snapshot("paper")
        assert snap is not None
        assert snap["total_value"] == 210000
        assert snap["position_count"] == 2

    def test_get_latest_empty(self, db_path):
        repo = TradeRepository(db_path=db_path)
        snap = repo.get_latest_snapshot("paper")
        assert snap is None


class TestPositionsCRUD:
    def test_insert_and_get(self, db_path):
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
                    "pnl_pct": 0.025,
                },
            ],
        )
        positions = repo.get_positions_by_date("2026-06-01", "paper")
        assert len(positions) == 1
        assert positions[0]["stock_code"] == "002371"


class TestDeletedMethods:
    """验证已删除的方法确实不存在"""

    def test_no_factor_values_methods(self, db_path):
        repo = TradeRepository(db_path=db_path)
        assert not hasattr(repo, "save_factor_values")
        assert not hasattr(repo, "get_factor_values")

    def test_no_save_metrics(self, db_path):
        repo = TradeRepository(db_path=db_path)
        assert not hasattr(repo, "save_metrics")
