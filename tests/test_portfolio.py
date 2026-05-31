# -*- coding: utf-8 -*-
"""组合管理器单元测试 — Portfolio + Position + PortfolioSnapshot"""

import pytest
from trade.portfolio.portfolio import Portfolio, Position, PortfolioSnapshot


# ====================== Position ======================


class TestPosition:
    def test_update_price_sets_market_value_and_pnl(self):
        p = Position(stock_code="000001", stock_name="平安银行", volume=1000,
                     avg_cost=12.00, current_price=12.00, market_value=12000)
        p.update_price(13.00)
        assert p.current_price == 13.00
        assert p.market_value == 13000
        assert p.pnl == 1000
        assert p.pnl_pct == pytest.approx(1 / 12)

    def test_update_price_tracks_highest(self):
        p = Position(stock_code="000001", stock_name="平安银行", volume=1000,
                     avg_cost=12.00, current_price=12.00)
        p.update_price(13.00)
        assert p.highest_price == 13.00
        p.update_price(11.00)
        assert p.highest_price == 13.00  # 不降低

    def test_update_price_zero_cost_handles_pnl_pct(self):
        p = Position(stock_code="000001", volume=1000, avg_cost=0)
        p.update_price(10.00)
        assert p.pnl_pct == 0.0


# ====================== Portfolio 初始化 ======================


class TestPortfolioInit:
    def test_default_initial_cash(self):
        pf = Portfolio()
        assert pf.cash == 100000
        assert pf.initial_cash == 100000
        assert pf.total_value == 100000

    def test_custom_initial_cash(self):
        pf = Portfolio(initial_cash=200000)
        assert pf.cash == 200000
        assert pf.total_value == 200000

    def test_empty_portfolio_properties(self):
        pf = Portfolio()
        assert pf.position_ratio == 0.0
        assert pf.drawdown == 0.0
        assert pf.daily_pnl == 0.0
        assert pf.total_pnl == 0.0
        assert len(pf.positions) == 0


# ====================== 开仓 ======================


class TestOpenPosition:
    @pytest.fixture
    def pf(self):
        return Portfolio(initial_cash=100000)

    def test_open_position_deducts_cash(self, pf):
        ok = pf.open_position("000001", "平安银行", 1000, 12.00, commission=5.0)
        assert ok is True
        assert pf.cash == 100000 - 12000 - 5.0
        assert "000001" in pf.positions
        pos = pf.positions["000001"]
        assert pos.volume == 1000
        assert pos.avg_cost == pytest.approx((12000 + 5.0) / 1000)

    def test_open_position_sets_metadata(self, pf):
        pf.open_position("000001", "平安银行", 1000, 12.00,
                         sector_code="bank", entry_date="2026-05-29",
                         stop_loss=11.00, take_profit=15.00, trailing_stop=0.05)
        pos = pf.positions["000001"]
        assert pos.sector_code == "bank"
        assert pos.entry_date == "2026-05-29"
        assert pos.stop_loss == 11.00
        assert pos.take_profit == 15.00
        assert pos.trailing_stop == 0.05
        assert pos.highest_price == 12.00

    def test_open_position_insufficient_cash(self, pf):
        pf.cash = 1000
        ok = pf.open_position("000001", "平安银行", 1000, 12.00)
        assert ok is False
        assert "000001" not in pf.positions
        assert pf.cash == 1000  # 未扣款

    def test_open_position_exact_cash(self, pf):
        pf.cash = 12000
        ok = pf.open_position("000001", "平安银行", 1000, 12.00, commission=0)
        assert ok is True
        assert pf.cash == 0

    def test_open_position_just_one_cent_short(self, pf):
        pf.cash = 11999.99
        ok = pf.open_position("000001", "平安银行", 1000, 12.00, commission=0)
        assert ok is False

    def test_open_position_adds_trade_log(self, pf):
        pf.open_position("000001", "平安银行", 1000, 12.00, entry_date="2026-05-29")
        assert len(pf.trade_log) == 1
        assert pf.trade_log[0]["type"] == "buy"
        assert pf.trade_log[0]["stock_code"] == "000001"

    def test_open_duplicate_stock_allowed(self, pf):
        """同一个代码再买一次（can_open_position 不做持仓去重）"""
        pf.open_position("000001", "平安银行", 500, 12.00)
        pf.cash += 100000  # 加钱
        ok = pf.open_position("000001", "平安银行", 500, 13.00)
        assert ok is True
        assert pf.positions["000001"].volume == 500  # 后面的覆盖

    def test_open_position_zero_volume(self, pf):
        pf.open_position("000001", "平安银行", 0, 12.00)
        pos = pf.positions["000001"]
        assert pos.market_value == 0


# ====================== 平仓 ======================


class TestClosePosition:
    @pytest.fixture
    def pf(self):
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "平安银行", 1000, 12.00, commission=5.0)
        return p

    def test_close_position_returns_proceeds(self, pf):
        cash_before = pf.cash
        ok = pf.close_position("000001", 15.00, reason="止盈", commission=8.0)
        assert ok is True
        assert "000001" not in pf.positions
        assert pf.cash == cash_before + 15000 - 8.0

    def test_close_position_records_trade_log(self, pf):
        pf.close_position("000001", 15.00, reason="止盈")
        sell_log = [t for t in pf.trade_log if t["type"] == "sell"]
        assert len(sell_log) == 1
        assert sell_log[0]["reason"] == "止盈"
        assert sell_log[0]["pnl"] > 0

    def test_close_nonexistent_position(self, pf):
        ok = pf.close_position("999999", 10.00)
        assert ok is False

    def test_close_position_with_loss(self, pf):
        pf.close_position("000001", 10.00, reason="止损", commission=5.0)
        sell_log = [t for t in pf.trade_log if t["type"] == "sell"][0]
        assert sell_log["pnl"] < 0


# ====================== 价格更新 ======================


class TestUpdatePrices:
    @pytest.fixture
    def pf(self):
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "平安银行", 1000, 12.00)
        p.open_position("000002", "万科A", 500, 25.00)
        return p

    def test_update_prices_updates_all_held(self, pf):
        pf.update_prices({"000001": 13.00, "000002": 26.00})
        assert pf.positions["000001"].current_price == 13.00
        assert pf.positions["000001"].market_value == 13000
        assert pf.positions["000002"].current_price == 26.00

    def test_update_prices_partial_data(self, pf):
        pf.update_prices({"000001": 13.00})
        assert pf.positions["000001"].current_price == 13.00
        assert pf.positions["000002"].current_price == 25.00  # 不变

    def test_update_prices_updates_peak_value(self, pf):
        pf.update_prices({"000001": 13.00, "000002": 26.00})
        cur = pf.total_value
        assert pf._peak_value == cur  # 峰值已更新

    def test_update_prices_empty_dict(self, pf):
        pf.update_prices({})
        assert pf.positions["000001"].current_price == 12.00  # 不变

    def test_update_prices_ignores_unheld_codes(self, pf):
        pf.update_prices({"999999": 100.00})
        # 不报错即可


# ====================== 回撤 ======================


class TestDrawdown:
    def test_drawdown_from_peak(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 12.00)
        pf.update_prices({"000001": 15.00})  # peak = cash + 15000
        peak = pf.total_value
        pf.update_prices({"000001": 10.00})
        expected_dd = (peak - pf.total_value) / peak
        assert pf.drawdown == pytest.approx(expected_dd)

    def test_drawdown_zero_when_no_peak_loss(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 12.00)
        pf.update_prices({"000001": 12.50})
        assert pf.drawdown == 0.0

    def test_drawdown_total_loss_means_total_drawdown(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "测试", 1000, 100.00, commission=0)
        pf.update_prices({"000001": 0.01})  # 几乎归零
        assert pf.drawdown > 0.9


# ====================== 日盈亏 ======================


class TestDailyPnL:
    def test_daily_pnl_after_snapshot(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 12.00)
        pf.update_prices({"000001": 13.00})
        snap = pf.snapshot("2026-05-29")
        assert snap.daily_pnl > 0

    def test_daily_pnl_before_any_snapshot(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 12.00)
        assert pf.daily_pnl == 0.0


# ====================== 仓位比例 ======================


class TestPositionRatio:
    def test_empty_portfolio_zero_ratio(self):
        assert Portfolio().position_ratio == 0.0

    def test_full_position(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 100.00, commission=0)
        assert pf.position_ratio == 1.0

    def test_half_position(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 50.00, commission=0)
        assert pf.position_ratio == 0.5


# ====================== 开仓检查 ======================


class TestCanOpenPosition:
    @pytest.fixture
    def pf(self):
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "平安银行", 1000, 30.00, sector_code="bank")
        return p

    def test_already_held_always_ok(self, pf):
        ok, msg = pf.can_open_position("000001", 0.3)
        assert ok is True

    def test_total_position_exceeds_100pct(self, pf):
        ok, msg = pf.can_open_position("000002", 0.9)
        assert ok is False
        assert "总仓位" in msg

    def test_single_stock_exceeds_max(self, pf):
        ok, msg = pf.can_open_position("000002", 0.25, max_single_pct=0.20)
        assert ok is False
        assert "超上限" in msg

    def test_sector_concentration_exceeds_max(self, pf):
        # 已持有 bank 30%，再买 15% bank → 总 45% 超 30% 上限
        ok, msg = pf.can_open_position("000002", 0.15, sector_code="bank",
                                       max_single_pct=0.20, max_sector_pct=0.30)
        assert ok is False
        assert "板块" in msg

    def test_sector_different_ok(self, pf):
        ok, msg = pf.can_open_position("000002", 0.15, sector_code="tech",
                                       max_sector_pct=0.30)
        assert ok is True

    def test_empty_sector_always_ok(self, pf):
        ok, msg = pf.can_open_position("000002", 0.15, sector_code="",
                                       max_sector_pct=0.30)
        assert ok is True


# ====================== 快照 ======================


class TestSnapshot:
    def test_snapshot_contains_all_fields(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 12.00, entry_date="2026-05-29")
        snap = pf.snapshot("2026-05-29")
        assert snap.date == "2026-05-29"
        assert snap.cash < 100000
        assert snap.total_value > 0
        assert snap.position_count == 1

    def test_snapshot_to_db_dict(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 12.00, sector_code="bank")
        snap = pf.snapshot("2026-05-29")
        d = snap.to_db_dict(account="paper")
        assert d["trade_date"] == "2026-05-29"
        assert d["account"] == "paper"
        assert d["position_count"] == 1
        assert "bank" in d["sector_exposure"]

    def test_snapshot_updates_prev_total(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "平安银行", 1000, 12.00)
        pf.snapshot("2026-05-29")
        pf.update_prices({"000001": 10.00})
        snap2 = pf.snapshot("2026-05-29")
        assert snap2.daily_pnl < 0

    def test_snapshot_stored_in_list(self):
        pf = Portfolio()
        pf.snapshot("2026-05-29")
        pf.snapshot("2026-05-29")
        assert len(pf.snapshots) == 2


# ====================== 极端情况 ======================


class TestEdgeCases:
    def test_zero_initial_cash(self):
        pf = Portfolio(initial_cash=0)
        assert pf.total_value == 0
        ok = pf.open_position("000001", "测试", 100, 10.00)
        assert ok is False

    def test_negative_price_handled(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "测试", 1000, 12.00)
        pf.update_prices({"000001": -5.00})
        # 不崩溃，但数值为负
        assert pf.positions["000001"].current_price == -5.00

    def test_very_large_numbers(self):
        pf = Portfolio(initial_cash=1_000_000_000)
        pf.open_position("000001", "测试", 10_000_000, 50.00, commission=0)
        assert pf.position_ratio > 0

    def test_many_positions(self):
        pf = Portfolio(initial_cash=1_000_000)
        for i in range(50):
            code = f"{600000 + i:06d}"
            pf.open_position(code, f"stock_{i}", 100, 10.00 + i)
        assert len(pf.positions) == 50
        assert pf.position_ratio > 0

    def test_close_all_positions(self):
        pf = Portfolio(initial_cash=100000)
        pf.open_position("000001", "A", 500, 20.00)
        pf.open_position("000002", "B", 500, 20.00)
        pf.close_position("000001", 20.00)
        pf.close_position("000002", 20.00)
        assert len(pf.positions) == 0
        assert pf.position_ratio == 0.0

    def test_sector_exposure_multiple_positions(self):
        pf = Portfolio(initial_cash=200000)
        pf.open_position("000001", "A", 1000, 20.00, sector_code="bank")
        pf.open_position("000002", "B", 500, 40.00, sector_code="bank")
        exp = pf.get_sector_exposure()
        assert "bank" in exp
        assert exp["bank"] > 0.15

    def test_get_sector_exposure_empty(self):
        pf = Portfolio()
        assert pf.get_sector_exposure() == {}

    def test_snapshot_get_sector_exposure(self):
        pf = Portfolio(initial_cash=50000)
        pf.open_position("000001", "A", 1000, 10.00, sector_code="tech")
        snap = pf.snapshot("2026-05-29")
        exp = snap.get_sector_exposure()
        assert "tech" in exp
