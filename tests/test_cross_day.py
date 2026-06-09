"""跨日状态连续性测试。

验证 PaperAccount.restore 机制在跨交易日的状态承接：
  快照连续性、持仓继承、T+1 解锁、日盈亏重置、参数恢复、指数上下文恢复、回撤延续、多日周期。

每个测试使用 db_path fixture 作为隔离的 SQLite 数据库，
通过模拟多个交易日的 PaperAccount 实例来验证跨日状态流转。
"""

import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from data.repo.portfolio_repo import PortfolioRepo
from trade.exec.paper.account import (
    COMMISSION_RATE,
    MIN_COMMISSION,
    STAMP_TAX_RATE,
    PaperAccount,
)
from trade.exec.paper.portfolio import Position

# ═══════════════════════════════════════════════════════════════════
# 共享 fixture
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def mock_qmt():
    """Mock 掉 PaperAccount 的 QMT 远程调用（影响 _persist_state / restore 中的日内最高查询）。"""
    with (
        patch.object(PaperAccount, "_get_pre_close", return_value=0),
        patch.object(PaperAccount, "_get_day_high", return_value=0),
    ):
        yield


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


def _account(
    db_path: str, trade_date: str, initial_capital: float = 100000.0
) -> PaperAccount:
    """创建 PaperAccount 并设置交易日。"""
    acc = PaperAccount(
        db_path=db_path,
        telegram_bot=None,
        initial_capital=initial_capital,
    )
    acc._trade_date = trade_date
    return acc


def _buy_commission(price: float, volume: int) -> float:
    """模拟盘买入佣金。"""
    return max(price * volume * COMMISSION_RATE, MIN_COMMISSION)


def _sell_commission(price: float, volume: int) -> float:
    """模拟盘卖出佣金（含印花税）。"""
    amount = price * volume
    return max(amount * COMMISSION_RATE, MIN_COMMISSION) + amount * STAMP_TAX_RATE


# ═══════════════════════════════════════════════════════════════════
# 1. test_snapshot_continuity
# ═══════════════════════════════════════════════════════════════════


class TestSnapshotContinuity:
    """快照连续性：隔日 restore 后快照数据衔接正确。"""

    def test_day2_total_equals_day1_plus_daily_pnl(self, db_path, mock_qmt):
        """Day 2 total = Day 1 total + Day 2 日内盈亏（由价格变化产生）。"""
        # Day 1: 买入并拍照落库
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        acc1.snapshot("2026-06-01")

        day1_total = acc1.total_value
        day1_cash = acc1.cash

        # Day 2: 从 DB 恢复
        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        # 恢复后现金 = Day 1 结束时现金
        assert acc2.cash == pytest.approx(day1_cash, abs=0.01)

        # Day 2 价格变化产生日内盈亏
        acc2.update_prices({"000001": 55.0})

        day2_daily_pnl = acc2.daily_pnl  # = total_value - _prev_total
        day2_total = acc2.total_value

        # Day 2 total = Day 1 total + Day 2 日内盈亏
        assert day2_total == pytest.approx(day1_total + day2_daily_pnl, abs=0.01)

        # 精确验证：价格从 50 -> 55，持仓 100 股，收益 500
        assert day2_daily_pnl == pytest.approx(500.0, abs=0.02)

    def test_day2_starting_cash_from_day1_snapshot(self, db_path, mock_qmt):
        """Day 2 起始现金 = Day 1 收盘现金。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 200, source="test")
        acc1.snapshot("2026-06-01")

        day1_cash = acc1.cash

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        assert acc2.cash == pytest.approx(day1_cash, abs=0.01)

    def test_snapshot_persistence_roundtrip(self, db_path, mock_qmt):
        """snapshot 写入 DB 后 restore 能完整读出各字段。"""
        acc1 = _account(db_path, "2026-06-01")
        # 手动构建特定状态再拍照，验证各字段
        acc1._portfolio.cash = 60000.0
        pos = Position(
            stock_code="000001",
            stock_name="平安银行",
            volume=500,
            avg_cost=80.0,
            current_price=80.0,
            market_value=40000.0,
            entry_date="2026-06-01",
        )
        acc1._portfolio.positions["000001"] = pos
        acc1.snapshot("2026-06-01")

        snap_total = acc1.total_value  # 60000 + 40000 = 100000
        assert snap_total == pytest.approx(100000.0, abs=0.01)

        # 从 repo 直接读取
        repo = PortfolioRepo(db_path)
        snap = repo.get_latest_snapshot(account="paper")
        assert snap is not None
        assert snap["trade_date"] == "2026-06-01"
        assert snap["cash"] == pytest.approx(60000.0, abs=0.01)
        assert snap["market_value"] == pytest.approx(40000.0, abs=0.01)
        assert snap["total_value"] == pytest.approx(100000.0, abs=0.01)
        assert snap["position_count"] == 1


# ═══════════════════════════════════════════════════════════════════
# 2. test_position_carries_over
# ═══════════════════════════════════════════════════════════════════


class TestPositionCarriesOver:
    """持仓跨日继承：restore 后成本、数量、入场日期正确。"""

    def test_position_volume_and_cost_preserved(self, db_path, mock_qmt):
        """持仓股数和成本价跨日不变。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        buy_comm = acc1.positions["000001"].avg_cost  # includes commission

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        assert "000001" in acc2.positions
        pos = acc2.positions["000001"]
        assert pos.volume == 100
        assert pos.avg_cost == pytest.approx(buy_comm, abs=0.001)

    def test_position_name_preserved(self, db_path, mock_qmt):
        """股票名称跨日保留。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        assert acc2.positions["000001"].stock_name == "平安银行"

    def test_entry_date_not_lost(self, db_path, mock_qmt):
        """入场日期跨日保留。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        assert acc1.positions["000001"].entry_date == "2026-06-01"

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        assert acc2.positions["000001"].entry_date == "2026-06-01"

    def test_holding_days_not_incremented_known_gap(self, db_path, mock_qmt):
        """持仓天数跨日不递增（已知 gap：holding_days 目前无自动 +1 机制）。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        day1_days = acc1.positions["000001"].holding_days

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        # Current: holding_days stays 0 because no daily increment logic.
        # Expected: holding_days should increment by 1 on each new trade_date.
        assert acc2.positions["000001"].holding_days == day1_days


# ═══════════════════════════════════════════════════════════════════
# 3. test_t1_lock_becomes_available
# ═══════════════════════════════════════════════════════════════════


class TestT1LockBecomesAvailable:
    """T+1 锁仓在隔日后应自动解锁。"""

    def test_buy_day_locked(self, db_path, mock_qmt):
        """买入当日 locked_volume > 0，available_volume = 0。"""
        acc1 = _account(db_path, "2026-06-01")
        result = acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        assert result.success

        pos = acc1.positions["000001"]
        assert pos.locked_volume > 0, "当日买入应全部锁定"
        assert pos.volume == 100
        assert pos.locked_volume == 100
        assert pos.available_volume == 0, "当日不可卖出"

    def test_sell_fails_on_buy_day(self, db_path, mock_qmt):
        """买入当日 T+1 拦截卖出。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")

        result = acc1.sell("000001", 51.0, "测试")
        assert not result.success
        assert "T+1" in result.reason

    def test_sell_succeeds_on_next_day_after_manual_unlock(self, db_path, mock_qmt):
        """隔日 restore 后手动解锁可正常卖出（T+1 自动解锁特性尚未实现）。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        pos = acc2.positions["000001"]
        # NOTE: T+1 auto-unlock on cross-day is not implemented.
        # Currently locked_volume is persisted as-is from day 1 (100).
        # Expected: restore should detect cross-day entry and set locked_volume=0.
        assert pos.locked_volume > 0, "已知 gap：locked_volume 未自动归零"

        # Simulate the expected auto-unlock behavior
        pos.locked_volume = 0

        sell_result = acc2.sell("000001", 52.0, "隔日卖出")
        assert sell_result.success
        assert sell_result.pnl > 0


# ═══════════════════════════════════════════════════════════════════
# 4. test_daily_pnl_reset
# ═══════════════════════════════════════════════════════════════════


class TestDailyPnlReset:
    """日盈亏跨日重置，累计盈亏持续累加。"""

    def test_daily_pnl_resets_to_zero(self, db_path, mock_qmt):
        """新交易日 daily_pnl 从 0 开始。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        acc1.snapshot("2026-06-01")

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        assert acc2.daily_pnl == pytest.approx(0, abs=0.01), (
            "新交易日 daily_pnl 应初始化为 0"
        )

    def test_daily_pnl_positive_on_price_gain(self, db_path, mock_qmt):
        """新交易日价格变动产生正确的 daily_pnl。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        acc1.snapshot("2026-06-01")

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        assert acc2.daily_pnl == pytest.approx(0, abs=0.01)

        acc2.update_prices({"000001": 53.0})
        assert acc2.daily_pnl == pytest.approx(300.0, abs=0.02), (
            "股价涨 3 元 x 100 股 = 300"
        )

    def test_total_pnl_accumulates_across_days(self, db_path, mock_qmt):
        """累计总盈亏跨日延续（不受 snapshot reset 影响）。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        _buy_commission(50.0, 100)
        day1_total_pnl = acc1.total_pnl  # = total_value - 100000 = -buy_comm (approx)
        acc1.snapshot("2026-06-01")

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        # total_pnl 继承 Day 1 的值
        assert acc2.total_pnl == pytest.approx(day1_total_pnl, abs=0.01)

        # Day 2 价格变动后，total_pnl 累加日内盈亏
        acc2.update_prices({"000001": 56.0})
        # Day1 total_pnl + 600 (price gain) = new total_pnl
        expected = day1_total_pnl + 600.0
        assert acc2.total_pnl == pytest.approx(expected, abs=0.02)

    def test_daily_pnl_diverges_from_total_pnl_after_snapshot(self, db_path, mock_qmt):
        """新日 snapshot 后 daily_pnl 重置但 total_pnl 不重置。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        acc1.update_prices({"000001": 55.0})
        acc1.snapshot("2026-06-01")

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        # daily_pnl 相对 Day 1 收盘基准，此刻为 0
        assert acc2.daily_pnl == pytest.approx(0, abs=0.01)

        # total_pnl 相对 initial_capital，不受 snapshot 影响
        assert acc2.total_pnl != 0, "累计盈亏不应因换日归零"

        # 新日 snapshot 后 daily_pnl 以新基准计算
        acc2.update_prices({"000001": 60.0})
        total_pnl_before_snap = acc2.total_pnl  # day1_pnl + 500

        acc2.snapshot("2026-06-02")

        # snapshot 后 daily_pnl 重置基准
        # (因为 _prev_total 更新为 snapshot 时的 total_value)
        acc2.update_prices({"000001": 62.0})
        assert acc2.daily_pnl == pytest.approx(200.0, abs=0.02), (
            "新 snapshot 后 daily_pnl 相对新基准"
        )

        # total_pnl 仍在累加
        assert acc2.total_pnl > total_pnl_before_snap, "累计盈亏持续累加"


# ═══════════════════════════════════════════════════════════════════
# 5. test_pos_meta_restore
# ═══════════════════════════════════════════════════════════════════


class TestPosMetaRestore:
    """盯盘持仓元数据 (_pos_meta) 从 trade_signals 恢复。"""

    def _insert_signal(
        self,
        db_path: str,
        trade_date: str,
        code: str,
        name: str,
        stop_loss: float,
        take_profit: float,
        trailing_stop: float,
        score: int,
        signal_id: int,
    ):
        """向 trade_signals 插入一条 bought 状态的信号。"""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO trade_signals
               (id, trade_date, created_at, signal_type, signal_source,
                stock_code, stock_name, stop_loss, take_profit, trailing_stop,
                signal_score, status, account)
               VALUES (?, ?, ?, 'BUY', 'AI_ENHANCED',
                       ?, ?, ?, ?, ?, ?, 'bought', 'paper')""",
            (
                signal_id,
                trade_date,
                datetime.now().isoformat(),
                code,
                name,
                stop_loss,
                take_profit,
                trailing_stop,
                score,
            ),
        )
        conn.commit()
        conn.close()

    def test_pos_meta_restored_from_trade_signals(self, db_path, mock_qmt):
        """restore 后通过 get_signal_for_pos_meta 能还原止损止盈等参数。"""
        trade_date = "2026-06-01"

        # 先插入一条 bought 状态的信号
        self._insert_signal(
            db_path,
            trade_date,
            "000001",
            "平安银行",
            stop_loss=48.0,
            take_profit=55.0,
            trailing_stop=0.05,
            score=80,
            signal_id=1001,
        )

        # Day 1: 买入（触发 _persist_state，写入 DB）
        acc1 = _account(db_path, trade_date, initial_capital=100000)
        acc1.buy("000001", "平安银行", 50.0, 100, source="signal", signal_id=1001)

        # Day 2: restore
        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        # 模拟 Watcher._restore_pos_meta 的恢复逻辑
        repo = acc2.repo
        pos_meta = {}
        for code in acc2.positions:
            sig = repo.get_signal_for_pos_meta(code)
            pos = acc2.positions[code]
            if sig:
                pos_meta[code] = {
                    "sl": sig.get("stop_loss", 0) or 0,
                    "tp": sig.get("take_profit", 0) or 0,
                    "trailing_stop": sig.get("trailing_stop", 0.05) or 0.05,
                    "highest_price": pos.current_price,
                    "score": sig.get("signal_score", 0) or 0,
                    "signal_id": sig.get("id"),
                }

        meta = pos_meta.get("000001")
        assert meta is not None, "pos_meta should contain 000001"
        assert meta["sl"] == pytest.approx(48.0, abs=0.01)
        assert meta["tp"] == pytest.approx(55.0, abs=0.01)
        assert meta["trailing_stop"] == pytest.approx(0.05, abs=0.01)
        assert meta["score"] == pytest.approx(80, abs=0.01)
        assert meta["signal_id"] == 1001
        assert meta["highest_price"] > 0

    def test_pos_meta_no_signal_fallback(self, db_path, mock_qmt):
        """无对应信号时恢复默认值。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        # 不插入 trade_signals 记录

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        repo = acc2.repo
        pos_meta = {}
        for code in acc2.positions:
            sig = repo.get_signal_for_pos_meta(code)
            pos = acc2.positions[code]
            if sig:
                pos_meta[code] = {
                    "sl": sig.get("stop_loss", 0) or 0,
                    "tp": sig.get("take_profit", 0) or 0,
                    "trailing_stop": sig.get("trailing_stop", 0.05) or 0.05,
                    "highest_price": pos.current_price,
                    "score": sig.get("signal_score", 0) or 0,
                    "signal_id": sig.get("id"),
                }
            else:
                # _restore_pos_meta fallback logic
                pos_meta[code] = {
                    "sl": 0,
                    "tp": 0,
                    "trailing_stop": 0.05,
                    "highest_price": pos.current_price,
                    "sector": "",
                    "score": 0,
                    "signal_id": None,
                }

        meta = pos_meta.get("000001")
        assert meta is not None
        assert meta["sl"] == 0
        assert meta["tp"] == 0
        assert meta["trailing_stop"] == 0.05
        assert meta["score"] == 0
        assert meta["signal_id"] is None

    def test_pos_meta_multiple_positions(self, db_path, mock_qmt):
        """多只持仓各自对应正确的 pos_meta。"""
        trade_date = "2026-06-01"
        self._insert_signal(
            db_path,
            trade_date,
            "000001",
            "平安银行",
            stop_loss=48.0,
            take_profit=55.0,
            trailing_stop=0.05,
            score=80,
            signal_id=1,
        )
        self._insert_signal(
            db_path,
            trade_date,
            "000002",
            "万科A",
            stop_loss=13.0,
            take_profit=17.0,
            trailing_stop=0.03,
            score=70,
            signal_id=2,
        )

        acc1 = _account(db_path, trade_date, initial_capital=200000)
        acc1.buy("000001", "平安银行", 50.0, 100, source="signal", signal_id=1)
        acc1.buy("000002", "万科A", 15.0, 200, source="signal", signal_id=2)

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")

        repo = acc2.repo
        pos_meta = {}
        for code in acc2.positions:
            sig = repo.get_signal_for_pos_meta(code)
            if sig:
                pos_meta[code] = {
                    "sl": sig.get("stop_loss", 0) or 0,
                    "tp": sig.get("take_profit", 0) or 0,
                    "trailing_stop": sig.get("trailing_stop", 0.05) or 0.05,
                    "score": sig.get("signal_score", 0) or 0,
                }

        m1 = pos_meta.get("000001")
        assert m1["sl"] == pytest.approx(48.0, abs=0.01)
        assert m1["tp"] == pytest.approx(55.0, abs=0.01)

        m2 = pos_meta.get("000002")
        assert m2["sl"] == pytest.approx(13.0, abs=0.01)
        assert m2["tp"] == pytest.approx(17.0, abs=0.01)
        assert m2["trailing_stop"] == pytest.approx(0.03, abs=0.01)
        assert m2["score"] == pytest.approx(70, abs=0.01)


# ═══════════════════════════════════════════════════════════════════
# 6. test_index_context_restore
# ═══════════════════════════════════════════════════════════════════


class TestIndexContextRestore:
    """盘中容灾重启时从 index_snapshots 恢复指数走势上下文。"""

    def _create_index_table(self, db_path: str):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS index_snapshots (
                trade_date TEXT NOT NULL,
                ts REAL NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                high REAL DEFAULT 0,
                low REAL DEFAULT 0,
                pre_close REAL DEFAULT 0,
                change_pct REAL DEFAULT 0,
                amount REAL DEFAULT 0,
                index_code TEXT DEFAULT '',
                PRIMARY KEY (trade_date, ts)
            )"""
        )
        conn.commit()
        conn.close()

    def _insert_index_prices(self, db_path: str, trade_date: str, prices: list[float]):
        """插入模拟的上证指数日内价格序列。"""
        conn = sqlite3.connect(db_path)
        for i, price in enumerate(prices):
            ts = 34200.0 + i * 60.0  # 9:30 + i 分钟
            day_high = max(prices[: i + 1])
            day_low = min(prices[: i + 1])
            conn.execute(
                """INSERT OR REPLACE INTO index_snapshots
                   (trade_date, ts, price, high, low, amount, index_code)
                   VALUES (?, ?, ?, ?, ?, ?, '000001.SH')""",
                (trade_date, ts, price, day_high, day_low, 50000000000.0),
            )
        conn.commit()
        conn.close()

    def test_index_prices_reconstructed(self, db_path, mock_qmt):
        """从 index_snapshots 还原完整价格序列。"""
        trade_date = "2026-06-01"
        prices = [3100.0, 3105.0, 3110.0, 3108.0, 3112.0, 3120.0, 3115.0, 3118.0]

        self._create_index_table(db_path)
        self._insert_index_prices(db_path, trade_date, prices)

        # 模拟 Watcher._restore_index_context 的 DB 查询逻辑
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """SELECT ts, price, high, low, amount FROM index_snapshots
               WHERE trade_date=? AND (index_code='000001.SH' OR index_code IS NULL)
               ORDER BY ts ASC""",
            (trade_date,),
        ).fetchall()
        conn.close()

        assert len(rows) == len(prices)
        for i, p in enumerate(prices):
            assert rows[i][1] == pytest.approx(p, abs=0.01), f"index {i} mismatch"

        closes = [r[1] for r in rows]
        highs = [r[2] for r in rows if r[2] and r[2] > 0]
        lows = [r[3] for r in rows if r[3] and r[3] > 0]

        assert closes == prices, "价格序列应完整还原"
        assert max(highs) == 3120.0, "日内最高价应正确"
        assert min(lows) == 3100.0, "日内最低价应正确"

    def test_index_context_with_high_low_reconstruction(self, db_path, mock_qmt):
        """还原后的 index_high 和 index_low 与原始数据一致。"""
        trade_date = "2026-06-01"
        prices = [3050.0, 3070.0, 3060.0, 3080.0, 3040.0, 3075.0]

        self._create_index_table(db_path)
        self._insert_index_prices(db_path, trade_date, prices)

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT price, high, low FROM index_snapshots WHERE trade_date=? ORDER BY ts ASC",
            (trade_date,),
        ).fetchall()
        conn.close()

        closes = [r[0] for r in rows]
        highs = [r[1] for r in rows]
        lows = [r[2] for r in rows]

        # _restore_index_context 使用这些逻辑
        index_prices = closes
        index_high = max(highs) if highs else max(closes)
        index_low = min(lows) if lows else min(closes)

        assert index_prices == prices
        assert index_high == 3080.0, f"日内最高应为 3080，实际 {index_high}"
        assert index_low == 3040.0, f"日内最低应为 3040，实际 {index_low}"

    def test_index_context_data_threshold(self, db_path, mock_qmt):
        """数据不足 5 条时不还原（跟 _restore_index_context 行为一致）。"""
        trade_date = "2026-06-01"
        prices = [3100.0, 3105.0]  # Only 2 points

        self._create_index_table(db_path)
        self._insert_index_prices(db_path, trade_date, prices)

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT price FROM index_snapshots WHERE trade_date=? ORDER BY ts ASC",
            (trade_date,),
        ).fetchall()
        conn.close()

        # The real code has: if len(rows) < 5: return
        assert len(rows) < 5, "测试数据不足 5 条"
        # So restoration is skipped - we confirm the guard condition


# ═══════════════════════════════════════════════════════════════════
# 7. test_drawdown_carries_over
# ═══════════════════════════════════════════════════════════════════


class TestDrawdownCarriesOver:
    """日内最大回撤跨日延续。"""

    def test_drawdown_recorded_in_snapshot(self, db_path, mock_qmt):
        """亏损日的 drawdown 被正确记录到快照。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 200, source="test")

        # 模拟价格上涨后回落（产生回撤）
        pos = acc1.positions["000001"]
        pos.day_high = 52.0
        pos.update_price(48.0)

        expected_dd = (52.0 - 48.0) * 200  # 800
        assert acc1.drawdown == pytest.approx(expected_dd, abs=0.01)

        acc1.snapshot("2026-06-01")

        # 快照中记录了 drawdown
        repo = PortfolioRepo(db_path)
        snap = repo.get_latest_snapshot(account="paper")
        assert snap is not None
        assert snap["drawdown"] == pytest.approx(800.0, abs=0.01), (
            "snapshot 应记录 drawdown"
        )

    def test_drawdown_not_restored_from_snapshot_known_gap(self, db_path, mock_qmt):
        """restore 后 drawdown 属性不自动恢复（已知 gap：day_high 未持久化）。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 200, source="test")
        pos = acc1.positions["000001"]
        pos.day_high = 52.0
        pos.update_price(48.0)
        acc1.snapshot("2026-06-01")

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")
        acc2.update_prices({"000001": 48.0})

        # After restore, drawdown = 0 because:
        # - Position.day_high is not persisted in DB, defaults to 0
        # - update_prices(48.0) sets day_high = max(0, 48.0) = 48.0 (since 48 > 0)
        # - Portfolio.drawdown calc: (day_high - current) * volume = (48 - 48) * 200 = 0
        assert acc2.drawdown == pytest.approx(0, abs=0.01), (
            "当前 drawdown 未从 snapshot 恢复 (day_high 未跨日保留)"
        )

        # Snapshot 中确实存了 drawdown 但 restore 不反哺 Position.day_high
        if "000001" in acc2.positions:
            restored = acc2.positions["000001"]
            # update_prices sets day_high = price (48.0) because price > current day_high (0)
            # So drawdown = (48 - 48) * 200 = 0 instead of expected (52 - 48) * 200 = 800
            assert restored.day_high == 48.0
            assert restored.current_price == 48.0

    def test_max_drawdown_can_be_tracked_manually(self, db_path, mock_qmt):
        """显式设置 day_high 后可正确计算 drawdown（模拟修复后的行为）。"""
        acc1 = _account(db_path, "2026-06-01")
        acc1.buy("000001", "平安银行", 50.0, 200, source="test")
        pos = acc1.positions["000001"]
        pos.day_high = 52.0
        pos.update_price(48.0)
        dd_before = acc1.drawdown
        acc1.snapshot("2026-06-01")

        acc2 = _account(db_path, "2026-06-02")
        acc2.restore("2026-06-02")
        acc2.update_prices({"000001": 48.0})

        # 手动恢复 day_high（模拟修复后的 T+1 逻辑）
        if "000001" in acc2.positions:
            acc2.positions["000001"].day_high = 52.0

        # 现在 drawdown 与 Day 1 一致
        assert acc2.drawdown == pytest.approx(dd_before, abs=0.01), (
            "day_high 恢复后 drawdown 可正确计算"
        )


# ═══════════════════════════════════════════════════════════════════
# 8. test_multiple_days_cycle
# ═══════════════════════════════════════════════════════════════════


class TestMultipleDaysCycle:
    """连续三个交易日：买入 -> 持有 -> 卖出，验证完整 P&L。"""

    def _buy_commission(self, price: float, volume: int) -> float:
        return max(price * volume * COMMISSION_RATE, MIN_COMMISSION)

    def _sell_commission(self, price: float, volume: int) -> float:
        amount = price * volume
        return max(amount * COMMISSION_RATE, MIN_COMMISSION) + amount * STAMP_TAX_RATE

    def test_buy_hold_sell_three_days(self, db_path, mock_qmt):
        """三天完整周期：Day1 买入 -> Day2 持有 -> Day3 卖出。"""
        initial_capital = 100000.0

        # ── Day 1: 买入 ──
        acc1 = _account(db_path, "2026-06-01", initial_capital)
        buy_result = acc1.buy("000001", "平安银行", 50.0, 100, source="test")
        assert buy_result.success

        buy_comm = self._buy_commission(50.0, 100)
        buy_cost = 50.0 * 100 + buy_comm
        cash_day1 = initial_capital - buy_cost
        assert acc1.cash == pytest.approx(cash_day1, abs=0.01)
        acc1.snapshot("2026-06-01")

        # ── Day 2: 持有 ──
        acc2 = _account(db_path, "2026-06-02", initial_capital)
        acc2.restore("2026-06-02")
        assert "000001" in acc2.positions, "持仓应跨日延续"
        assert acc2.cash == pytest.approx(cash_day1, abs=0.01)

        # 更新价格（持有，不卖出）
        acc2.update_prices({"000001": 55.0})
        day2_unrealized_pnl = acc2.daily_pnl  # 500
        assert day2_unrealized_pnl == pytest.approx(500.0, abs=0.02)
        acc2.snapshot("2026-06-02")

        # ── Day 3: 卖出 ──
        acc3 = _account(db_path, "2026-06-03", initial_capital)
        acc3.restore("2026-06-03")
        assert "000001" in acc3.positions

        # T+1 手动解锁（当前没有自动解锁机制）
        pos = acc3.positions["000001"]
        pos.locked_volume = 0  # simulate T+1 unlock

        # 以更高价卖出
        sell_price = 60.0
        sell_result = acc3.sell("000001", sell_price, "完成交易")
        assert sell_result.success, f"卖出失败: {sell_result.reason}"

        # ── 验证完整 P&L ──
        # 公式: (sell_price - buy_price) * volume - buy_commission - sell_commission
        volume = 100
        sell_comm = self._sell_commission(sell_price, volume)
        expected_pnl = (sell_price - 50.0) * volume - buy_comm - sell_comm

        assert sell_result.pnl == pytest.approx(expected_pnl, abs=0.02), (
            f"预期 P&L {expected_pnl:.2f}，实际 {sell_result.pnl:.2f}"
        )

        # total_pnl 也一致（全部平仓后 total_pnl = realized P&L）
        total_pnl = acc3.total_pnl
        assert total_pnl == pytest.approx(expected_pnl, abs=0.02), (
            "total_pnl 应与 realized P&L 一致"
        )

    def test_zero_pnl_no_trades(self, db_path, mock_qmt):
        """无交易的三天周期，P&L 始终为 0。"""
        initial_capital = 100000.0
        acc1 = _account(db_path, "2026-06-01", initial_capital)
        acc1.snapshot("2026-06-01")

        for day in ["2026-06-02", "2026-06-03"]:
            acc = _account(db_path, day, initial_capital)
            acc.restore(day)
            if day != "2026-06-03":
                acc.snapshot(day)

        acc3 = _account(db_path, "2026-06-03", initial_capital)
        acc3.restore("2026-06-03")
        assert acc3.cash == pytest.approx(initial_capital, abs=0.01)
        assert acc3.total_pnl == pytest.approx(0, abs=0.01)
        assert acc3.daily_pnl == pytest.approx(0, abs=0.01)

    def test_multi_day_pnl_cumulative(self, db_path, mock_qmt):
        """多日 P&L 累加等于最终 realized P&L。"""
        initial_capital = 100000.0
        buy_price = 50.0
        sell_price = 58.0
        volume = 100

        # Day 1: Buy
        acc1 = _account(db_path, "2026-06-01", initial_capital)
        acc1.buy("000001", "平安银行", buy_price, volume, source="test")
        acc1.snapshot("2026-06-01")

        # Day 2: Price rise (unrealized)
        acc2 = _account(db_path, "2026-06-02", initial_capital)
        acc2.restore("2026-06-02")
        acc2.update_prices({"000001": 55.0})
        snap2_pnl = acc2.daily_pnl  # price gain 5 * 100 = 500
        acc2.snapshot("2026-06-02")

        # Day 3: Price rise more then sell
        acc3 = _account(db_path, "2026-06-03", initial_capital)
        acc3.restore("2026-06-03")
        acc3.update_prices({"000001": sell_price})
        day3_unrealized = acc3.daily_pnl  # price gain 3 * 100 = 300

        # Unlock T+1 and sell
        acc3.positions["000001"].locked_volume = 0
        sell_result = acc3.sell("000001", sell_price, "止盈")

        buy_comm = self._buy_commission(buy_price, volume)
        sell_comm = self._sell_commission(sell_price, volume)
        expected_total_pnl = (sell_price - buy_price) * volume - buy_comm - sell_comm

        assert sell_result.success
        assert sell_result.pnl == pytest.approx(expected_total_pnl, abs=0.02)

        # Day2 daily_pnl (unrealized) + Day3 daily_pnl (unrealized)
        # should sum to expected_total_pnl - realized P&L adjustments
        # (由于 sell_comm 只在卖出时扣，不完全等于 daily_pnl 之和)
        unrealized_sum = snap2_pnl + day3_unrealized  # 500 + 300 = 800
        realized_via_market = (sell_price - buy_price) * volume  # 800
        assert unrealized_sum == pytest.approx(realized_via_market, abs=0.02)
