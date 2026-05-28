import pytest
from analysis.screening.factors import (
    check_hard_gates,
    check_volume_breakout,
    check_volume_pullback,
    check_amplitude_contract,
    check_main_force_buy,
    check_chip_concentrate,
    check_consecutive_yang,
    check_pullback_hold,
    check_trend_persist,
    check_low_volatility,
    check_volume_expand,
    check_trend_strength,
    check_rps_20_strong,
    check_rps_60_strong,
    check_rps_120_strong,
    check_rps_resonance,
    check_sector_hot,
    check_leader_in_sector,
    check_stronger_than_sector,
    check_sector_fund_resonance,
)


# ============================================================
# 硬关卡
# ============================================================

class TestHardGates:
    def _ok_row(self, **overrides):
        row = {
            "stock_name": "平安银行", "stock_code": "000001",
            "change_pct": 2.0, "total_market_cap": 200_0000_0000,
            "volume_ratio": 1.2, "ma5": 11.0, "ma10": 10.8, "ma20": 10.5,
            "price": 11.2,
        }
        row.update(overrides)
        return row

    def test_all_pass(self):
        assert check_hard_gates(self._ok_row()) is True

    def test_st_fail(self):
        assert check_hard_gates(self._ok_row(stock_name="*ST华泽")) is False

    def test_688_fail(self):
        assert check_hard_gates(self._ok_row(stock_code="688001")) is False

    def test_limit_up_fail(self):
        assert check_hard_gates(self._ok_row(change_pct=9.8)) is False

    def test_small_mcap_fail(self):
        assert check_hard_gates(self._ok_row(total_market_cap=30_0000_0000)) is False

    def test_volume_ratio_low_fail(self):
        assert check_hard_gates(self._ok_row(volume_ratio=0.3)) is False

    def test_ma_not_aligned_fail(self):
        # 新硬关卡: price>ma20 且 ma10>ma20，ma10<ma20 应失败
        assert check_hard_gates(self._ok_row(ma10=10.3, ma20=10.5)) is False

    def test_below_ma20_fail(self):
        assert check_hard_gates(self._ok_row(price=10.0, ma20=10.5)) is False


# ============================================================
# 量价类
# ============================================================

class TestVolumeFactors:
    def test_breakout(self):
        assert check_volume_breakout({"volume_ratio": 1.8}, []) == "放量启动"

    def test_breakout_none_low(self):
        assert check_volume_breakout({"volume_ratio": 1.2}, []) is None

    def test_pullback(self):
        row = {"volume_ratio": 0.6, "change_pct": -1.0}
        assert check_volume_pullback(row, []) == "缩量回调"

    def test_pullback_none_up(self):
        assert check_volume_pullback({"volume_ratio": 0.6, "change_pct": 1.0}, []) is None

    def test_pullback_none_vol_high(self):
        assert check_volume_pullback({"volume_ratio": 0.9, "change_pct": -1.0}, []) is None

    def test_amplitude_contract(self):
        assert check_amplitude_contract({"amplitude": 2.5}, []) == "蓄力中"

    def test_amplitude_not_contract(self):
        assert check_amplitude_contract({"amplitude": 5.0}, []) is None


# ============================================================
# 资金类
# ============================================================

class TestFundFlowFactors:
    def test_main_force_buy(self):
        row = {"main_force_net": 50000000, "main_force_ratio": 4.0}
        assert check_main_force_buy(row, []) == "主力介入"

    def test_main_force_not_ratio_low(self):
        row = {"main_force_net": 50000000, "main_force_ratio": 2.0}
        assert check_main_force_buy(row, []) is None

    def test_main_force_not_net_neg(self):
        row = {"main_force_net": -1000000, "main_force_ratio": 5.0}
        assert check_main_force_buy(row, []) is None

    def test_chip_concentrate(self):
        row = {"main_force_net": 5000000, "small_net": -3000000}
        assert check_chip_concentrate(row, []) == "筹码集中"

    def test_chip_not_concentrate(self):
        row = {"main_force_net": 5000000, "small_net": 1000000}
        assert check_chip_concentrate(row, []) is None


# ============================================================
# 多日类
# ============================================================

class TestMultiDayFactors:
    def test_consecutive_yang_3(self):
        history = [
            {"open": 10.0, "price": 10.5},
            {"open": 10.4, "price": 10.8},
            {"open": 10.7, "price": 11.0},
        ]
        assert check_consecutive_yang({}, history) == "强势连阳"

    def test_consecutive_yang_not_enough(self):
        history = [
            {"open": 10.0, "price": 9.5},
            {"open": 10.4, "price": 10.8},
        ]
        assert check_consecutive_yang({}, history) is None

    def test_consecutive_yang_short_history(self):
        assert check_consecutive_yang({}, [{"open": 10.0, "price": 10.5}]) is None

    def test_pullback_hold(self):
        history = [{"high": 11.5} for _ in range(5)]
        row = {"price": 11.25, "ma10": 11.0}
        assert check_pullback_hold(row, history) == "回踩确认"

    def test_pullback_hold_break_ma10(self):
        history = [{"high": 11.5} for _ in range(5)]
        row = {"price": 11.25, "ma10": 11.3}
        assert check_pullback_hold(row, history) is None

    def test_pullback_hold_large_pullback(self):
        history = [{"high": 11.5} for _ in range(5)]
        row = {"price": 10.5, "ma10": 10.0}  # (11.5-10.5)/11.5 > 5%
        assert check_pullback_hold(row, history) is None

    def test_trend_persist_5days(self):
        history = [
            {"ma5": 11.0, "ma10": 10.8, "ma20": 10.5},
            {"ma5": 11.2, "ma10": 11.0, "ma20": 10.7},
            {"ma5": 11.5, "ma10": 11.2, "ma20": 10.9},
            {"ma5": 11.8, "ma10": 11.5, "ma20": 11.1},
            {"ma5": 12.0, "ma10": 11.7, "ma20": 11.3},
        ]
        assert check_trend_persist({}, history) == "趋势延续"

    def test_trend_persist_broken(self):
        history = [
            {"ma5": 11.0, "ma10": 10.8, "ma20": 10.5},
            {"ma5": 11.2, "ma10": 12.0, "ma20": 10.7},  # break
        ]
        assert check_trend_persist({}, history) is None

    def test_low_volatility_short_history(self):
        row = {"price": 10.0}
        history = [{"high": 10.2, "low": 9.9, "prev_close": 10.0}]
        assert check_low_volatility(row, history) is None  # < 14 days

    def test_low_volatility_true(self):
        row = {"price": 10.0}
        history = [{"high": 10.1, "low": 9.95, "prev_close": 10.0} for _ in range(14)]
        assert check_low_volatility(row, history) == "低波蓄力"

    def test_volume_expand(self):
        row = {"avg_vol_5d": 1300000, "avg_vol_20d": 1000000}
        assert check_volume_expand(row, []) == "量能放大"

    def test_volume_not_expand(self):
        row = {"avg_vol_5d": 1100000, "avg_vol_20d": 1000000}
        assert check_volume_expand(row, []) is None

    def test_trend_strength_strong(self):
        # 10-day cum return = (1.01)^10 - 1 ≈ 10.46% > 5%
        # varied values so std > 0 for sharpe
        history = [
            {"change_pct": 1.0}, {"change_pct": 0.8}, {"change_pct": 1.2},
            {"change_pct": 1.1}, {"change_pct": 0.9}, {"change_pct": 1.3},
            {"change_pct": 1.0}, {"change_pct": 0.7}, {"change_pct": 1.1},
            {"change_pct": 1.0}, {"change_pct": 0.9}, {"change_pct": 1.2},
            {"change_pct": 1.0}, {"change_pct": 0.8}, {"change_pct": 1.1},
            {"change_pct": 1.3}, {"change_pct": 0.9}, {"change_pct": 1.0},
            {"change_pct": 1.1}, {"change_pct": 0.8},
        ]
        result = check_trend_strength({}, history)
        assert result == "趋势强劲"

    def test_trend_strength_weak_return(self):
        history = [{"change_pct": 0.3} for _ in range(20)]
        result = check_trend_strength({}, history)
        assert result is None  # cum return ~3% < 5%

    def test_trend_strength_short_history(self):
        assert check_trend_strength({}, []) is None


# ============================================================
# RPS 类
# ============================================================

class TestRPSFactors:
    def test_rps20_strong(self):
        assert check_rps_20_strong({"rps_20": 0.85}, []) == "RPS20强"

    def test_rps20_not_strong(self):
        assert check_rps_20_strong({"rps_20": 0.75}, []) is None

    def test_rps60_strong(self):
        assert check_rps_60_strong({"rps_60": 0.90}, []) == "RPS60强"

    def test_rps120_strong(self):
        assert check_rps_120_strong({"rps_120": 0.82}, []) == "RPS120强"

    def test_rps_resonance(self):
        row = {"rps_20": 0.75, "rps_60": 0.80}
        assert check_rps_resonance(row, []) == "RPS多周期共振"

    def test_rps_resonance_none(self):
        row = {"rps_20": 0.75, "rps_60": 0.60}
        assert check_rps_resonance(row, []) is None


# ============================================================
# 板块类
# ============================================================

class TestSectorFactors:
    def test_sector_hot(self):
        row = {"stock_code": "000001"}
        sector_hot = {"BK1036": 2}
        stock_sectors = {"000001": ["BK1036"]}
        assert (
            check_sector_hot(row, [], sector_hot=sector_hot, stock_sectors=stock_sectors)
            == "板块加持"
        )

    def test_sector_not_hot(self):
        row = {"stock_code": "000001"}
        assert (
            check_sector_hot(row, [], sector_hot={}, stock_sectors={"000001": ["BK1036"]})
            is None
        )

    def test_leader_in_sector(self):
        row = {"stock_code": "000001", "change_pct": 5.0}
        sector_stocks_pct = {"000001": 5.0, "000002": 3.0, "000003": 2.0}
        stock_sectors = {"000001": ["BK1036"], "000002": ["BK1036"], "000003": ["BK1036"]}
        assert (
            check_leader_in_sector(
                row, [], sector_stocks_pct=sector_stocks_pct, stock_sectors=stock_sectors,
            )
            == "领涨龙头"
        )

    def test_not_leader_in_sector(self):
        # 000005 在 5 只股票中排第 4，不在前 3
        row = {"stock_code": "000005", "change_pct": 2.0}
        sector_stocks_pct = {
            "000001": 5.0, "000002": 4.5, "000003": 3.5,
            "000004": 3.0, "000005": 2.0,
        }
        stock_sectors = {
            "000001": ["BK1036"], "000002": ["BK1036"],
            "000003": ["BK1036"], "000004": ["BK1036"],
            "000005": ["BK1036"],
        }
        assert (
            check_leader_in_sector(
                row, [], sector_stocks_pct=sector_stocks_pct, stock_sectors=stock_sectors,
            )
            is None
        )

    def test_stronger_than_sector(self):
        row = {"stock_code": "000001", "change_pct": 2.5}
        sector_changes = {"BK1036": 1.0}
        stock_sectors = {"000001": ["BK1036"]}
        assert (
            check_stronger_than_sector(
                row, [], sector_changes=sector_changes, stock_sectors=stock_sectors,
            )
            == "强于板块"
        )

    def test_not_stronger_than_sector(self):
        row = {"stock_code": "000001", "change_pct": 1.2}
        sector_changes = {"BK1036": 1.0}
        stock_sectors = {"000001": ["BK1036"]}
        assert (
            check_stronger_than_sector(
                row, [], sector_changes=sector_changes, stock_sectors=stock_sectors,
            )
            is None
        )

    def test_sector_fund_resonance(self):
        row = {"stock_code": "000001", "main_force_net": 5000000}
        sector_funds = {"BK1036": 1000000}
        stock_sectors = {"000001": ["BK1036"]}
        assert (
            check_sector_fund_resonance(
                row, [], sector_funds=sector_funds, stock_sectors=stock_sectors,
            )
            == "资金共振"
        )
