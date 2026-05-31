"""回测框架单元测试"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

from analysis.backtest import (
    BacktestConfig,
    BacktestEngine,
    DataLoader,
    OrderSignal,
    Trade,
    calculate_metrics,
)

# =====================  Fixtures  =====================


@pytest.fixture
def mock_db():
    """创建临时数据库并插入日线测试数据"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute("""
        CREATE TABLE stock_basic (
            trade_date DATE NOT NULL,
            stock_code VARCHAR(20) NOT NULL,
            stock_name VARCHAR(50) NOT NULL,
            price DECIMAL(10,2),
            open DECIMAL(10,2),
            high DECIMAL(10,2),
            low DECIMAL(10,2),
            volume DECIMAL(20,2),
            turnover_rate DECIMAL(8,4),
            change_pct DECIMAL(8,2)
        )
    """)

    # 000001: 稳步上涨趋势
    # 每日 open=100, high=102, low=99, close=101 缓慢上涨
    base = 100.0
    for i in range(20):
        d = (datetime(2025, 1, 2) + timedelta(days=i)).strftime("%Y-%m-%d")
        price = base + i * 0.5
        conn.execute(
            "INSERT INTO stock_basic VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                d,
                "000001",
                "测试A",
                price,
                price - 0.3,
                price + 1.0,
                price - 1.0,
                1_000_000,
                2.0,
                0.5,
            ),
        )

    # 000002: 震荡下跌，用于测试止损
    base2 = 50.0
    for i in range(20):
        d = (datetime(2025, 1, 2) + timedelta(days=i)).strftime("%Y-%m-%d")
        price = base2 - i * 0.3
        conn.execute(
            "INSERT INTO stock_basic VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                d,
                "000002",
                "测试B",
                price,
                price - 0.2,
                price + 0.5,
                price - 0.8,
                2_000_000,
                3.0,
                -0.3,
            ),
        )

    # 000003: 先跌后涨，用于测试止盈
    base3 = 80.0
    for i in range(20):
        d = (datetime(2025, 1, 2) + timedelta(days=i)).strftime("%Y-%m-%d")
        if i < 5:
            price = base3 - i * 1.0  # 先跌
        else:
            price = base3 - 5 + (i - 5) * 2.0  # 后涨
        conn.execute(
            "INSERT INTO stock_basic VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                d,
                "000003",
                "测试C",
                price,
                price - 0.1,
                price + 1.5,
                price - 1.0,
                1_500_000,
                1.5,
                1.0,
            ),
        )

    conn.commit()
    conn.close()
    yield tmp.name
    os.unlink(tmp.name)


@pytest.fixture
def sample_daily_data():
    """纯 DataFrame 测试数据，不依赖数据库"""
    import pandas as pd

    rows = []
    base_a = 100.0
    base_b = 50.0
    for i in range(10):
        d = (datetime(2025, 1, 2) + timedelta(days=i)).strftime("%Y-%m-%d")
        # 000001 稳步上涨
        ca = base_a + i * 0.5
        rows.append(
            {
                "trade_date": d,
                "stock_code": "000001",
                "open": ca - 0.3,
                "high": ca + 1.0,
                "low": ca - 1.0,
                "close": ca,
                "volume": 1_000_000,
                "turnover_rate": 2.0,
                "change_pct": 0.5,
            }
        )
        # 000002 缓慢下跌
        cb = base_b - i * 0.3
        rows.append(
            {
                "trade_date": d,
                "stock_code": "000002",
                "open": cb - 0.2,
                "high": cb + 0.5,
                "low": cb - 0.8,
                "close": cb,
                "volume": 2_000_000,
                "turnover_rate": 3.0,
                "change_pct": -0.3,
            }
        )

    return pd.DataFrame(rows)


@pytest.fixture
def signal_buy_a():
    """买入 000001 信号"""
    return OrderSignal(
        stock_code="000001",
        signal_date="2025-01-02",
        stop_loss=95.0,
        take_profit=None,
    )


@pytest.fixture
def signal_buy_b():
    """买入 000002 信号（含止损）"""
    return OrderSignal(
        stock_code="000002",
        signal_date="2025-01-02",
        stop_loss=47.0,
        take_profit=None,
    )


# =====================  DataLoader Tests  =====================


class TestDataLoader:
    def test_load_daily(self, mock_db):
        loader = DataLoader(db_path=mock_db)
        df = loader.load_daily(["000001", "000002"], "2025-01-02", "2025-01-10")
        assert not df.empty
        assert "trade_date" in df.columns
        assert "stock_code" in df.columns
        assert "close" in df.columns
        assert len(df[df["stock_code"] == "000001"]) > 0
        assert len(df[df["stock_code"] == "000002"]) > 0

    def test_load_daily_empty(self, mock_db):
        loader = DataLoader(db_path=mock_db)
        df = loader.load_daily(["999999"], "2025-01-02", "2025-01-10")
        assert df.empty

    def test_load_prices(self, mock_db):
        loader = DataLoader(db_path=mock_db)
        pivot = loader.load_prices(["000001", "000002"], "2025-01-02", "2025-01-10")
        assert not pivot.empty
        assert "000001" in pivot.columns
        assert "000002" in pivot.columns
        # 日期作为 index
        assert pivot.index.name == "trade_date"

    def test_load_prices_empty(self, mock_db):
        loader = DataLoader(db_path=mock_db)
        pivot = loader.load_prices(["999999"], "2025-01-02", "2025-01-10")
        assert pivot.empty


# =====================  BacktestEngine Tests  =====================


class TestBacktestEngine:
    def test_simple_buy_and_hold(self, sample_daily_data):
        """单信号买入并持有到期末"""
        engine = BacktestEngine()
        signal = OrderSignal(
            stock_code="000001",
            signal_date="2025-01-02",
        )
        metrics = engine.run([signal], sample_daily_data)
        assert metrics["total_trades"] == 1
        assert metrics["total_return"] > 0  # 上涨趋势应有正收益
        assert len(engine.equity_curve) == 10
        assert len(engine.trades) == 1
        t = engine.trades[0]
        assert t.stock_code == "000001"
        assert t.exit_reason == "end_of_period"

    def test_stop_loss_trigger(self, sample_daily_data):
        """止损触发"""
        engine = BacktestEngine()
        signal = OrderSignal(
            stock_code="000002",
            signal_date="2025-01-02",
            stop_loss=49.0,  # 很快会触发止损
        )
        metrics = engine.run([signal], sample_daily_data)
        assert metrics["total_trades"] >= 1
        t = engine.trades[0]
        assert t.exit_reason == "stop_loss"
        assert t.exit_price is not None and t.exit_price <= 49.0

    def test_no_stop_loss_missed(self, sample_daily_data):
        """止损价未触及，持有到期末"""
        engine = BacktestEngine()
        signal = OrderSignal(
            stock_code="000001",
            signal_date="2025-01-02",
            stop_loss=50.0,  # 远低于价格，不会触发
        )
        metrics = engine.run([signal], sample_daily_data)
        assert metrics["total_trades"] == 1
        assert engine.trades[0].exit_reason == "end_of_period"

    def test_multiple_signals(self, sample_daily_data):
        """多个股票同时持仓"""
        engine = BacktestEngine()
        sig_a = OrderSignal(stock_code="000001", signal_date="2025-01-02")
        sig_b = OrderSignal(stock_code="000002", signal_date="2025-01-02")
        metrics = engine.run([sig_a, sig_b], sample_daily_data)
        assert metrics["total_trades"] == 2
        # 两个 trades 的 exit reason 应该都是 end_of_period
        reasons = [t.exit_reason for t in engine.trades]
        assert all(r == "end_of_period" for r in reasons)

    def test_no_signals(self, sample_daily_data):
        """空信号列表"""
        engine = BacktestEngine()
        metrics = engine.run([], sample_daily_data)
        assert metrics["total_trades"] == 0
        assert metrics["total_return"] == 0.0
        # 权益曲线应该记录了每日数据
        assert len(engine.equity_curve) == 10

    def test_same_stock_same_day(self, sample_daily_data):
        """同一天同一股票的两个信号（应只入场一次）"""
        engine = BacktestEngine()
        sig_a = OrderSignal(stock_code="000001", signal_date="2025-01-02")
        sig_b = OrderSignal(stock_code="000001", signal_date="2025-01-02")
        metrics = engine.run([sig_a, sig_b], sample_daily_data)
        assert metrics["total_trades"] == 1  # 应只入场一次

    def test_custom_config(self, sample_daily_data):
        """自定义回测参数"""
        config = BacktestConfig(
            initial_cash=500_000,
            commission_rate=0.0,
            min_commission=0.0,
            stamp_tax_rate=0.0,
        )
        engine = BacktestEngine(config=config)
        signal = OrderSignal(stock_code="000001", signal_date="2025-01-02")
        metrics = engine.run([signal], sample_daily_data)
        assert metrics["total_trades"] == 1
        assert engine.config.initial_cash == 500_000
        assert engine.config.commission_rate == 0.0

    def test_take_profit_trigger(self):
        """止盈触发测试 — 使用模拟的快速上涨行情"""
        import pandas as pd

        rows = []
        for i in range(10):
            d = (datetime(2025, 1, 2) + timedelta(days=i)).strftime("%Y-%m-%d")
            price = 100.0 + i * 3.0  # 每天涨3块
            rows.append(
                {
                    "trade_date": d,
                    "stock_code": "000001",
                    "open": price - 0.5,
                    "high": price + 2.0,
                    "low": price - 2.0,
                    "close": price,
                    "volume": 1_000_000,
                    "turnover_rate": 2.0,
                    "change_pct": 3.0,
                }
            )
        data = pd.DataFrame(rows)

        engine = BacktestEngine()
        signal = OrderSignal(
            stock_code="000001",
            signal_date="2025-01-02",
            take_profit=105.0,  # 很快会触发止盈
        )
        metrics = engine.run([signal], data)
        assert metrics["total_trades"] == 1
        t = engine.trades[0]
        assert t.exit_reason == "take_profit"
        # 止盈价应在 105 左右
        assert t.exit_price is not None and t.exit_price >= 105.0


# =====================  Metrics Tests  =====================


class TestMetrics:
    def test_empty_trades(self):
        """空交易和空权益曲线"""
        result = calculate_metrics([], [], initial_cash=100_000)
        assert result["total_trades"] == 0
        assert result["total_return"] == 0.0

    def test_single_trade_win(self):
        """单笔盈利交易"""
        trades = [
            Trade(
                stock_code="000001",
                entry_date="2025-01-02",
                exit_date="2025-01-10",
                entry_price=100.0,
                exit_price=110.0,
                shares=100,
                pnl=950.0,
                pnl_pct=9.5,
            ),
        ]
        equity = [
            {
                "date": "2025-01-02",
                "cash": 100_000,
                "market_value": 0,
                "total": 100_000,
            },
            {
                "date": "2025-01-10",
                "cash": 100_950,
                "market_value": 0,
                "total": 100_950,
            },
        ]
        result = calculate_metrics(trades, equity, initial_cash=100_000)
        assert result["total_trades"] == 1
        assert result["win_rate"] == 1.0
        assert result["total_return"] > 0

    def test_all_losses(self):
        """全部亏损"""
        trades = [
            Trade(
                stock_code="000001",
                entry_date="2025-01-02",
                exit_date="2025-01-05",
                entry_price=100.0,
                exit_price=90.0,
                shares=100,
                pnl=-1050.0,
                pnl_pct=-10.5,
            ),
            Trade(
                stock_code="000002",
                entry_date="2025-01-03",
                exit_date="2025-01-06",
                entry_price=50.0,
                exit_price=45.0,
                shares=200,
                pnl=-1050.0,
                pnl_pct=-10.5,
            ),
        ]
        equity = [
            {
                "date": "2025-01-02",
                "cash": 100_000,
                "market_value": 0,
                "total": 100_000,
            },
            {"date": "2025-01-06", "cash": 97_900, "market_value": 0, "total": 97_900},
        ]
        result = calculate_metrics(trades, equity, initial_cash=100_000)
        assert result["total_trades"] == 2
        assert result["win_rate"] == 0.0
        assert result["total_return"] < 0
        assert result["avg_win"] == 0.0
        assert result["avg_loss"] < 0

    def test_all_wins(self):
        """全部盈利"""
        trades = [
            Trade(
                stock_code="000001",
                entry_date="2025-01-02",
                exit_date="2025-01-05",
                entry_price=100.0,
                exit_price=110.0,
                shares=100,
                pnl=950.0,
                pnl_pct=9.5,
            ),
            Trade(
                stock_code="000002",
                entry_date="2025-01-03",
                exit_date="2025-01-06",
                entry_price=50.0,
                exit_price=55.0,
                shares=200,
                pnl=950.0,
                pnl_pct=9.5,
            ),
        ]
        equity = [
            {
                "date": "2025-01-02",
                "cash": 100_000,
                "market_value": 0,
                "total": 100_000,
            },
            {
                "date": "2025-01-06",
                "cash": 101_900,
                "market_value": 0,
                "total": 101_900,
            },
        ]
        result = calculate_metrics(trades, equity, initial_cash=100_000)
        assert result["total_trades"] == 2
        assert result["win_rate"] == 1.0
        assert result["total_return"] > 0
        assert result["avg_loss"] == 0.0

    def test_mixed_results(self):
        """盈亏各半"""
        trades = [
            Trade(
                stock_code="000001",
                entry_date="2025-01-02",
                exit_date="2025-01-05",
                entry_price=100.0,
                exit_price=110.0,
                shares=100,
                pnl=950.0,
                pnl_pct=9.5,
            ),
            Trade(
                stock_code="000002",
                entry_date="2025-01-03",
                exit_date="2025-01-06",
                entry_price=50.0,
                exit_price=45.0,
                shares=200,
                pnl=-1050.0,
                pnl_pct=-10.5,
            ),
        ]
        equity = [
            {
                "date": "2025-01-02",
                "cash": 100_000,
                "market_value": 0,
                "total": 100_000,
            },
            {"date": "2025-01-06", "cash": 99_900, "market_value": 0, "total": 99_900},
        ]
        result = calculate_metrics(trades, equity, initial_cash=100_000)
        assert result["total_trades"] == 2
        assert result["win_rate"] == 0.5
        assert result["profit_factor"] == pytest.approx(950 / 1050, rel=0.01)
        assert result["avg_win"] > 0
        assert result["avg_loss"] < 0

    def test_sharpe_and_drawdown(self):
        """夏普和回撤计算"""
        trades = []
        # 模拟稳步上涨的权益曲线
        equity = []
        cash = 100_000
        for i in range(10):
            cash += 200  # 每天赚 200
            equity.append(
                {
                    "date": f"2025-01-{i + 2:02d}",
                    "cash": cash,
                    "market_value": 0,
                    "total": cash,
                }
            )
        result = calculate_metrics(trades, equity, initial_cash=100_000)
        assert result["total_return"] > 0
        assert result["max_drawdown"] == 0.0  # 每天都涨
        assert result["sharpe_ratio"] > 0  # 正收益应有正夏普

    def test_max_drawdown_with_volatility(self):
        """有波动的最大回撤"""
        equity = [
            {"date": "2025-01-01", "total": 100_000},
            {"date": "2025-01-02", "total": 110_000},  # peak
            {"date": "2025-01-03", "total": 90_000},  # drawdown = (110-90)/110 = 18.18%
            {"date": "2025-01-04", "total": 105_000},
            {
                "date": "2025-01-05",
                "total": 85_000,
            },  # max drawdown = (110-85)/110 = 22.73%
            {"date": "2025-01-06", "total": 120_000},  # new peak
        ]
        result = calculate_metrics([], equity, initial_cash=100_000)
        expected_dd = (110_000 - 85_000) / 110_000
        assert result["max_drawdown"] == pytest.approx(expected_dd, rel=0.01)
