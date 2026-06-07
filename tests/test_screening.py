"""Tests for strategy.screening module — breadth, factors, trend, profiles."""

import sqlite3

import pytest

from stock.signals import StockScore
from strategy.screening.breadth import MarketBreadth, classify_market_state
from strategy.screening.factors import (
    check_amplitude_contract,
    check_chip_concentrate,
    check_consecutive_yang,
    check_hard_gates,
    check_low_volatility,
    check_main_force_buy,
    check_pullback_hold,
    check_trend_persist,
    check_trend_strength,
    check_volume_breakout,
    check_volume_expand,
    check_volume_pullback,
)
from strategy.screening.profiles import ProfileBuilder
from strategy.screening.trend import TrendScreener

# ===================================================================
# classify_market_state  — 纯函数，不依赖DB
# ===================================================================


class TestClassifyMarketState:
    def test_total_zero_returns_fenge(self):
        assert classify_market_state(0, 0, 0, 0, 0) == "分化"

    def test_prev_state_konghuang_bounce(self):
        """prev_state=恐慌 && up > 2000 -> 连跌修复"""
        state = classify_market_state(2500, 500, 20, 10, 2.0, prev_state="恐慌")
        assert state == "连跌修复"

    def test_konghuang_bounce_not_enough(self):
        """prev_state=恐慌 but up <= 2000 -> 普跌"""
        state = classify_market_state(1000, 3000, 5, 50, -3.0, prev_state="恐慌")
        # 1000 <= 3000 (BULL), 1000 < 1500 (DIVIDE), 1000 >= 800 (BEAR) -> 普跌
        assert state == "普跌"

    def test_chaodie_mo_triad_consec(self):
        """连跌3天+跌停峰值回落30%+ -> 超跌末端"""
        state = classify_market_state(
            500,
            3500,
            2,
            30,
            -4.0,
            prev_state="普跌",
            consecutive_down_days=3,
            limit_down_peak=100,
        )
        # limit_down=30 < 100*0.7=70, consec>=3 -> 超跌末端
        assert state == "超跌末端"

    def test_chaodie_not_enough_peak_drop(self):
        """连跌3天但跌停未从峰值回落30% -> 不触发超跌末端"""
        state = classify_market_state(
            500,
            3500,
            2,
            80,
            -4.0,
            prev_state="普跌",
            consecutive_down_days=3,
            limit_down_peak=100,
        )
        # limit_down=80 >= 100*0.7=70 -> 不触发, up=500 < 800 -> 恐慌
        assert state == "恐慌"

    def test_up_over_bull(self):
        assert classify_market_state(3500, 500, 100, 0, 3.0) == "普涨"

    def test_up_over_divide(self):
        assert classify_market_state(1800, 2200, 30, 5, 0.5) == "分化"

    def test_up_over_bear(self):
        assert classify_market_state(900, 3100, 5, 30, -2.0) == "普跌"

    def test_below_bear(self):
        assert classify_market_state(300, 3700, 1, 100, -5.0) == "恐慌"

    def test_bounce_overrides_bull(self):
        """恐慌次日即使上涨多 -> 连跌修复优先于普涨"""
        state = classify_market_state(3200, 400, 80, 2, 3.5, prev_state="恐慌")
        assert state == "连跌修复"

    def test_chaodie_overrides_bear(self):
        """超跌末端优先于普跌"""
        state = classify_market_state(
            500,
            3500,
            0,
            15,
            -3.0,
            prev_state="普跌",
            consecutive_down_days=4,
            limit_down_peak=50,
        )
        assert state == "超跌末端"


# ===================================================================
# MarketBreadth — DB 依赖
# ===================================================================


def _init_breadth_tables(conn: sqlite3.Connection):
    """初始化 MarketBreadth 所需的表（先 drop 再 create 保证 schema 正确）"""
    conn.executescript("""
        DROP TABLE IF EXISTS stock_basic;
        CREATE TABLE stock_basic (
            stock_code TEXT,
            trade_date TEXT,
            change_pct REAL
        );
        DROP TABLE IF EXISTS limit_pool;
        CREATE TABLE limit_pool (
            stock_code TEXT,
            trade_date TEXT,
            pool_type TEXT
        );
        DROP TABLE IF EXISTS index_realtime_data;
        CREATE TABLE index_realtime_data (
            index_code TEXT,
            trade_date TEXT,
            trade_time TEXT,
            change_percent REAL
        );
        DROP TABLE IF EXISTS market_breadth;
        CREATE TABLE market_breadth (
            trade_date TEXT PRIMARY KEY,
            up_count INTEGER DEFAULT 0,
            down_count INTEGER DEFAULT 0,
            flat_count INTEGER DEFAULT 0,
            limit_up_count INTEGER DEFAULT 0,
            limit_down_count INTEGER DEFAULT 0,
            index_change_pct REAL DEFAULT 0,
            market_state TEXT DEFAULT ''
        );
    """)


class TestMarketBreadth:
    def test_init_default_db_path(self):
        mb = MarketBreadth(db_path=":memory:")
        assert mb.db_path == ":memory:"

    def test_compute_empty_data(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_breadth_tables(conn)
        conn.commit()
        conn.close()

        mb = MarketBreadth(db_path=db_path)
        result = mb.compute("2026-06-01")
        assert result["up_count"] == 0
        assert result["down_count"] == 0
        assert result["flat_count"] == 0
        assert result["limit_up_count"] == 0
        assert result["limit_down_count"] == 0
        assert result["market_state"] != ""

    def test_compute_all_up_stocks(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_breadth_tables(conn)
        for code in ["000001", "000002", "000003", "000004", "000005"]:
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES (?, '2026-06-01', ?)",
                (code, 2.0),
            )
        conn.execute(
            "INSERT INTO index_realtime_data (index_code, trade_date, trade_time, change_percent) "
            "VALUES ('sh000001', '2026-06-01', '15:00:00', 1.5)"
        )
        conn.commit()
        conn.close()

        mb = MarketBreadth(db_path=db_path)
        result = mb.compute("2026-06-01")
        assert result["up_count"] == 5
        assert result["down_count"] == 0
        assert result["flat_count"] == 0

    def test_compute_all_down_stocks(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_breadth_tables(conn)
        for code in ["000001", "000002", "000003"]:
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES (?, '2026-06-01', ?)",
                (code, -3.0),
            )
        conn.execute(
            "INSERT INTO index_realtime_data (index_code, trade_date, trade_time, change_percent) "
            "VALUES ('sh000001', '2026-06-01', '15:00:00', -2.0)"
        )
        conn.commit()
        conn.close()

        mb = MarketBreadth(db_path=db_path)
        result = mb.compute("2026-06-01")
        assert result["up_count"] == 0
        assert result["down_count"] == 3
        assert result["market_state"] == "恐慌"

    def test_compute_mixed_data(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_breadth_tables(conn)
        for i, code in enumerate(["000001", "000002", "000003", "000004", "000005"]):
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES (?, '2026-06-01', ?)",
                (code, 1.0 + i),
            )
        for code in ["000006", "000007", "000008"]:
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES (?, '2026-06-01', ?)",
                (code, -2.0),
            )
        conn.execute("INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES ('000009', '2026-06-01', 0)")
        conn.execute(
            "INSERT INTO index_realtime_data (index_code, trade_date, trade_time, change_percent) "
            "VALUES ('sh000001', '2026-06-01', '15:00:00', 0.5)"
        )
        conn.commit()
        conn.close()

        mb = MarketBreadth(db_path=db_path)
        result = mb.compute("2026-06-01")
        assert result["up_count"] == 5
        assert result["down_count"] == 3
        assert result["flat_count"] == 1

    def test_compute_with_limit_pool(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_breadth_tables(conn)
        for code in ["000001", "000002"]:
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES (?, '2026-06-01', ?)",
                (code, 10.0),
            )
        for code in ["000003", "000004"]:
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES (?, '2026-06-01', ?)",
                (code, -10.0),
            )
        conn.execute(
            "INSERT INTO limit_pool (stock_code, trade_date, pool_type) VALUES ('000001', '2026-06-01', '涨停')"
        )
        conn.execute(
            "INSERT INTO limit_pool (stock_code, trade_date, pool_type) VALUES ('000003', '2026-06-01', '跌停')"
        )
        conn.execute(
            "INSERT INTO index_realtime_data (index_code, trade_date, trade_time, change_percent) "
            "VALUES ('sh000001', '2026-06-01', '15:00:00', 0.0)"
        )
        conn.commit()
        conn.close()

        mb = MarketBreadth(db_path=db_path)
        result = mb.compute("2026-06-01")
        assert result["limit_up_count"] == 1
        assert result["limit_down_count"] == 1

    def test_save_and_get(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_breadth_tables(conn)
        conn.execute(
            "INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES ('000001', '2026-06-01', 2.0)"
        )
        conn.execute(
            "INSERT INTO index_realtime_data (index_code, trade_date, trade_time, change_percent) "
            "VALUES ('sh000001', '2026-06-01', '15:00:00', 1.0)"
        )
        conn.commit()
        conn.close()

        mb = MarketBreadth(db_path=db_path)
        result = mb.save("2026-06-01")
        assert result["up_count"] == 1

        loaded = mb.get("2026-06-01")
        assert loaded is not None
        assert loaded["up_count"] == 1
        assert loaded["down_count"] == 0

    def test_get_nonexistent(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_breadth_tables(conn)
        conn.commit()
        conn.close()

        mb = MarketBreadth(db_path=db_path)
        assert mb.get("2099-01-01") is None

    def test_compute_with_prev_context(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_breadth_tables(conn)
        # 前一日记录
        conn.execute(
            "INSERT INTO market_breadth (trade_date, up_count, down_count, index_change_pct, market_state) "
            "VALUES ('2026-05-31', 300, 3700, -5.0, '恐慌')"
        )
        for code in ["000001", "000002", "000003", "000004"]:
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, change_pct) VALUES (?, '2026-06-01', ?)",
                (code, 2.0),
            )
        conn.execute(
            "INSERT INTO index_realtime_data (index_code, trade_date, trade_time, change_percent) "
            "VALUES ('sh000001', '2026-06-01', '15:00:00', 2.0)"
        )
        conn.commit()
        conn.close()

        mb = MarketBreadth(db_path=db_path)
        result = mb.compute("2026-06-01")
        assert result["market_state"] != ""
        assert result["up_count"] == 4


# ===================================================================
# check_hard_gates  — 纯函数
# ===================================================================


def _make_stock(**overrides) -> dict:
    """构造一个通过所有硬关卡的标准股票字典"""
    base = {
        "stock_code": "002371",
        "stock_name": "北方华创",
        "price": 50.0,
        "change_pct": 2.5,
        "total_market_cap": 500_0000_0000 + 1,  # > 50亿
        "volume_ratio": 1.2,
        "ma5": 48.0,
        "ma10": 47.0,
        "ma20": 46.0,
    }
    base.update(overrides)
    return base


class TestCheckHardGates:
    def test_normal_stock_passes(self):
        assert check_hard_gates(_make_stock()) is True

    def test_st_in_name_rejected(self):
        assert check_hard_gates(_make_stock(stock_name="ST华业")) is False

    def test_asterisk_st_in_name_rejected(self):
        assert check_hard_gates(_make_stock(stock_name="*ST康得")) is False

    def test_st_lowercase_rejected(self):
        assert check_hard_gates(_make_stock(stock_name="st华业")) is False

    def test_normal_name_passes(self):
        assert check_hard_gates(_make_stock(stock_name="东方创业")) is True

    def test_688_code_rejected(self):
        assert check_hard_gates(_make_stock(stock_code="688001")) is False

    def test_change_pct_negative_big_rejected(self):
        assert check_hard_gates(_make_stock(change_pct=-10.0)) is False

    def test_change_pct_exactly_negative_95_rejected(self):
        assert check_hard_gates(_make_stock(change_pct=-9.5)) is False

    def test_change_pct_945_passes(self):
        assert check_hard_gates(_make_stock(change_pct=-9.49)) is True

    def test_change_pct_positive_big_sets_limit_flag(self):
        row = _make_stock(change_pct=10.0)
        assert check_hard_gates(row) is True
        assert row.get("is_limit_up") is True

    def test_market_cap_below_50b_rejected(self):
        assert check_hard_gates(_make_stock(total_market_cap=49_0000_0000)) is False

    def test_market_cap_exactly_50b_passes(self):
        assert check_hard_gates(_make_stock(total_market_cap=50_0000_0000)) is True

    def test_volume_ratio_below_03_rejected(self):
        assert check_hard_gates(_make_stock(volume_ratio=0.3)) is False

    def test_volume_ratio_above_03_passes(self):
        assert check_hard_gates(_make_stock(volume_ratio=0.31)) is True

    def test_not_above_ma20_rejected(self):
        assert check_hard_gates(_make_stock(price=45.0, ma10=46.0, ma20=46.0)) is False

    def test_ma10_not_above_ma20_rejected(self):
        assert check_hard_gates(_make_stock(price=47.0, ma10=46.0, ma20=46.5)) is False

    def test_missing_fields_handled(self):
        assert check_hard_gates({}) is False

    def test_partial_fields(self):
        row = {
            "price": 50.0,
            "change_pct": 2.0,
            "total_market_cap": 500_0000_0000 + 1,
            "volume_ratio": 1.5,
            "ma5": 49.0,
            "ma10": 48.0,
            "ma20": 47.0,
        }
        assert check_hard_gates(row) is True

    def test_name_is_non_string(self):
        assert check_hard_gates(_make_stock(stock_name=12345)) is True

    def test_change_pct_is_none(self):
        row = _make_stock(change_pct=None)
        assert check_hard_gates(row) is True


# ===================================================================
# 因子函数 — 纯函数
# ===================================================================


class TestFactorFunctions:
    def test_volume_breakout_above_15(self):
        assert check_volume_breakout({"volume_ratio": 2.0}, []) == "放量启动"

    def test_volume_breakout_below_15(self):
        assert check_volume_breakout({"volume_ratio": 1.0}, []) is None

    def test_volume_breakout_missing(self):
        assert check_volume_breakout({}, []) is None

    def test_volume_pullback_low_vol_negative_chg(self):
        assert check_volume_pullback({"volume_ratio": 0.5, "change_pct": -1.0}, []) == "缩量回调"

    def test_volume_pullback_exact_boundary(self):
        assert check_volume_pullback({"volume_ratio": 0.5, "change_pct": -2.0}, []) == "缩量回调"

    def test_volume_pullback_positive_chg(self):
        assert check_volume_pullback({"volume_ratio": 0.5, "change_pct": 1.0}, []) is None

    def test_volume_pullback_high_vol(self):
        assert check_volume_pullback({"volume_ratio": 1.0, "change_pct": -1.0}, []) is None

    def test_amplitude_contract_below_3(self):
        assert check_amplitude_contract({"amplitude": 2.0}, []) == "蓄力中"

    def test_amplitude_contract_above_3(self):
        assert check_amplitude_contract({"amplitude": 3.5}, []) is None

    def test_amplitude_contract_missing(self):
        assert check_amplitude_contract({}, []) is None

    def test_main_force_buy_positive_net_high_ratio(self):
        result = check_main_force_buy({"main_force_net": 500_0000, "main_force_ratio": 5.0}, [])
        assert result == "主力介入"

    def test_main_force_buy_negative_net(self):
        assert check_main_force_buy({"main_force_net": -100, "main_force_ratio": 5.0}, []) is None

    def test_main_force_buy_low_ratio(self):
        assert check_main_force_buy({"main_force_net": 100, "main_force_ratio": 2.0}, []) is None

    def test_chip_concentrate_positive_mf_negative_small(self):
        result = check_chip_concentrate({"main_force_net": 500_0000, "small_net": -200_0000}, [])
        assert result == "筹码集中"

    def test_chip_concentrate_no_small(self):
        assert check_chip_concentrate({"main_force_net": 500_0000, "small_net": 0}, []) is None

    def test_chip_concentrate_no_mf(self):
        assert check_chip_concentrate({"main_force_net": 0, "small_net": -100}, []) is None


# ===================================================================
# 多日因子 — 纯函数
# ===================================================================


class TestMultiDayFactors:
    def test_consecutive_yang_3_days(self):
        history = [
            {"price": 51, "open": 50},
            {"price": 52, "open": 51},
            {"price": 53, "open": 52},
        ]
        assert check_consecutive_yang({}, history) == "强势连阳"

    def test_consecutive_yang_not_enough_history(self):
        assert check_consecutive_yang({}, [{"price": 50, "open": 49}]) is None

    def test_consecutive_yang_has_yin(self):
        history = [
            {"price": 51, "open": 50},
            {"price": 50, "open": 51},  # 阴线
            {"price": 53, "open": 52},
        ]
        assert check_consecutive_yang({}, history) is None

    def test_pullback_hold_not_enough_history(self):
        assert check_pullback_hold({}, [{"high": 50}]) is None

    def test_pullback_hold_valid(self):
        history = [{"high": 100}] * 5
        row = {"price": 97, "ma10": 95, "high": 98}
        assert check_pullback_hold(row, history) == "回踩确认"

    def test_pullback_hold_pullback_too_deep(self):
        history = [{"high": 100}] * 5
        row = {"price": 92, "ma10": 95, "high": 93}
        assert check_pullback_hold(row, history) is None

    def test_pullback_hold_below_ma10(self):
        history = [{"high": 100}] * 5
        row = {"price": 94, "ma10": 95, "high": 95}
        assert check_pullback_hold(row, history) is None

    def test_trend_persist_not_enough_history(self):
        assert check_trend_persist({}, []) is None

    def test_trend_persist_valid(self):
        history = [{"ma5": 50, "ma10": 48, "ma20": 46}] * 5
        assert check_trend_persist({}, history) == "趋势延续"

    def test_trend_persist_one_broken(self):
        history = [{"ma5": 50, "ma10": 48, "ma20": 46}] * 4
        history.append({"ma5": 47, "ma10": 48, "ma20": 46})  # ma5 < ma10
        assert check_trend_persist({}, history) is None

    def test_low_volatility_not_enough_history(self):
        assert check_low_volatility({}, [{"high": 50, "low": 49}] * 10) is None

    def test_low_volatility_valid(self):
        history = [{"high": 50.5, "low": 49.5, "prev_close": 50.0}] * 14
        row = {"price": 50.0}
        # TR = max(1.0, 0.5, 0.5) = 1.0; ATR14 = 1.0; 1.0/50.0 = 2% < 3%
        assert check_low_volatility(row, history) == "低波蓄力"

    def test_low_volatility_high_vol(self):
        history = [{"high": 55.0, "low": 45.0, "prev_close": 50.0}] * 14
        row = {"price": 50.0}
        assert check_low_volatility(row, history) is None

    def test_volume_expand_avg5_greater_than_avg20(self):
        assert check_volume_expand({"avg_vol_5d": 2000, "avg_vol_20d": 1000}, []) == "量能放大"

    def test_volume_expand_not_enough(self):
        assert check_volume_expand({"avg_vol_5d": 1000, "avg_vol_20d": 1000}, []) is None

    def test_volume_expand_missing(self):
        assert check_volume_expand({}, []) is None

    def test_trend_strength_not_enough_history(self):
        """历史 < 10 日 → None"""
        assert check_trend_strength({}, [{"change_pct": 1.0}] * 9) is None

    def test_trend_strength_low_return(self):
        """10日涨幅 <= 5% → None"""
        history = [{"change_pct": 0.5}] * 10
        assert check_trend_strength({}, history) is None

    def test_trend_strength_not_enough_for_sharpe(self):
        """10-19 条历史：累计收益 > 5% 但缺少 20 日 sharpe 数据 → None"""
        history = [{"change_pct": 3.5}] * 3 + [{"change_pct": 3.0}] * 7
        result = check_trend_strength({}, history)
        assert result is None

    def test_trend_strength_valid(self):
        """20 日涨幅 > 5% 且 sharpe > 1.0 → 趋势强劲"""
        # 用略有变化的涨幅使 std > 0
        chgs = [
            5.0,
            5.5,
            4.8,
            5.2,
            5.1,
            4.9,
            5.3,
            5.0,
            4.7,
            5.4,
            5.2,
            4.8,
            5.5,
            5.0,
            4.9,
            5.3,
            5.1,
            4.7,
            5.4,
            5.0,
        ]
        history = [{"change_pct": c} for c in chgs]
        result = check_trend_strength({}, history)
        assert result == "趋势强劲"

    def test_trend_strength_sharpe_below_1(self):
        """sharpe <= 1.0 → None"""
        chgs = [2.0, -1.0, 1.5, -0.5, 2.0, -1.0, 1.0, 0.5, -2.0, 1.0] * 2
        history = [{"change_pct": c} for c in chgs]
        assert check_trend_strength({}, history) is None


# ===================================================================
# TrendScreener — 纯方法（无需 DB）
# ===================================================================


class TestTrendScreenerStatic:
    def test_screen_panic_state_returns_empty(self):
        ts = TrendScreener(db_path=":memory:")
        result = ts.screen("2026-06-01", market_state="恐慌")
        assert result == []

    def test_check_sector_blacklist_no_sectors(self):
        assert (
            TrendScreener._check_sector_blacklist(
                {"stock_code": "000001"},
                {"BK0001"},
                {},
            )
            is True
        )

    def test_check_sector_blacklist_blocked(self):
        assert (
            TrendScreener._check_sector_blacklist(
                {"stock_code": "000001"},
                {"BK0001"},
                {"000001": ["BK0001"]},
            )
            is False
        )

    def test_check_sector_blacklist_not_blocked(self):
        assert (
            TrendScreener._check_sector_blacklist(
                {"stock_code": "000001"},
                {"BK9999"},
                {"000001": ["BK0001"]},
            )
            is True
        )

    def test_check_sector_gate_no_hot_data(self):
        assert (
            TrendScreener._check_sector_gate(
                {"stock_code": "000001"},
                {},
                {"000001": ["BK0001"]},
            )
            is True
        )

    def test_check_sector_gate_no_sectors(self):
        assert (
            TrendScreener._check_sector_gate(
                {"stock_code": "000001"},
                {"BK0001": 3},
                {},
            )
            is True
        )

    def test_check_sector_gate_in_hot(self):
        assert (
            TrendScreener._check_sector_gate(
                {"stock_code": "000001"},
                {"BK0001": 3},
                {"000001": ["BK0001"]},
            )
            is True
        )

    def test_check_sector_gate_not_in_hot(self):
        assert (
            TrendScreener._check_sector_gate(
                {"stock_code": "000001"},
                {"BK9999": 3},
                {"000001": ["BK0001"]},
            )
            is False
        )

    def test_is_20d_high(self):
        ts = TrendScreener()
        history = [{"high": 100}] * 19 + [{"high": 105}]
        assert ts._is_20d_high({"price": 104}, history) is True

    def test_not_20d_high(self):
        ts = TrendScreener()
        history = [{"high": 100}] * 20
        assert ts._is_20d_high({"price": 90}, history) is False

    def test_is_reversal(self):
        ts = TrendScreener()
        history = [{"change_pct": -3.0}]
        assert ts._is_reversal({"change_pct": 2.0}, history) is True

    def test_is_not_reversal(self):
        ts = TrendScreener()
        history = [{"change_pct": 2.0}]
        assert ts._is_reversal({"change_pct": 1.0}, history) is False

    def test_is_reversal_no_history(self):
        ts = TrendScreener()
        assert ts._is_reversal({"change_pct": 1.0}, []) is False

    def test_near_ma(self):
        ts = TrendScreener()
        assert ts._near_ma({"price": 50, "ma5": 49.5}, "ma5", 2) is True

    def test_not_near_ma(self):
        ts = TrendScreener()
        assert ts._near_ma({"price": 50, "ma5": 45}, "ma5", 2) is False

    def test_near_ma_ma_zero(self):
        ts = TrendScreener()
        assert ts._near_ma({"price": 50, "ma5": 0}, "ma5", 2) is False

    def test_tight_consolidation(self):
        ts = TrendScreener()
        history = [{"high": 52, "low": 50}] * 5
        assert ts._is_tight_consolidation({}, history) is True

    def test_not_tight_consolidation(self):
        ts = TrendScreener()
        history = [{"high": 60, "low": 40}] * 5
        assert ts._is_tight_consolidation({}, history) is False

    def test_not_tight_consolidation_no_history(self):
        ts = TrendScreener()
        assert ts._is_tight_consolidation({}, []) is False

    def test_bounce_from_below_ma20(self):
        ts = TrendScreener()
        history = [{"price": 45, "ma20": 46}]
        row = {"price": 47, "ma20": 46}
        assert ts._is_bounce_from_below_ma20(row, history) is True

    def test_not_bounce_from_below(self):
        ts = TrendScreener()
        history = [{"price": 47, "ma20": 46}]
        row = {"price": 48, "ma20": 46.5}
        assert ts._is_bounce_from_below_ma20(row, history) is False

    def test_is_ma_diverging_basic(self):
        """均线刚开始发散 — prev_spread < 2%, need >=3 history items"""
        ts = TrendScreener()
        # 前 2 天随便填，第 3 天（history[-1]）才是判断依据
        history = [
            {"ma5": 49.0, "ma10": 48.0, "ma20": 47.0},
            {"ma5": 50.0, "ma10": 49.0, "ma20": 48.0},
            {"ma5": 50.5, "ma10": 50, "ma20": 49.8},  # spread ≈ 1.00 < 2
        ]
        row = {"ma5": 55, "ma10": 52, "ma20": 50}
        assert ts._is_ma_diverging(row, history) is True

    def test_is_ma_diverging_not_diverging(self):
        """prev_spread >= 2% → 非发散"""
        ts = TrendScreener()
        # spread = max(|51-49|, |49-48|) / max(48,1) * 100 = 2/48*100 ≈ 4.17 >= 2
        history = [
            {"ma5": 49.0, "ma10": 48.0, "ma20": 47.0},
            {"ma5": 50.0, "ma10": 49.0, "ma20": 48.0},
            {"ma5": 51, "ma10": 49, "ma20": 48},
        ]
        row = {"ma5": 55, "ma10": 52, "ma20": 50}
        assert ts._is_ma_diverging(row, history) is False

    def test_is_ma_diverging_not_bullish(self):
        """多头排列不成立 → False"""
        ts = TrendScreener()
        history = [{"ma5": 48, "ma10": 49, "ma20": 50}]
        assert ts._is_ma_diverging({"ma5": 48, "ma10": 49, "ma20": 50}, history) is False

    def test_is_ma_diverging_no_history(self):
        ts = TrendScreener()
        assert ts._is_ma_diverging({}, []) is False

    def test_is_ma_diverging_less_than_3_history(self):
        ts = TrendScreener()
        assert ts._is_ma_diverging({}, [{"ma5": 50, "ma10": 48, "ma20": 46}]) is False

    def test_compute_score_basic(self):
        ts = TrendScreener()
        score = ts._compute_score(["放量启动", "趋势延续"], ["突破追涨"], {"ma5_angle": 5})
        # base=20, tags=2*5=10, scenarios=1*8=8, angle=5*2=10 → 48
        assert score == 48.0

    def test_compute_score_caps_at_100(self):
        ts = TrendScreener()
        tags = ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"]
        scenarios = ["s1", "s2", "s3", "s4"]
        score = ts._compute_score(tags, scenarios, {"ma5_angle": 50})
        assert score == 100.0

    def test_compute_score_negative_angle(self):
        ts = TrendScreener()
        score = ts._compute_score([], [], {"ma5_angle": -10})
        # base=20, angle=-20 clamped to -10 → 10
        assert score == 10.0

    def test_rank_and_limit(self):
        ts = TrendScreener(top_n=2)

        def _ss(code, name, score, mode):
            return StockScore(
                stock_code=code,
                stock_name=name,
                score=score,
                trend_mode=mode,
                price=10,
                change_pct=1,
                mcap=100,
                circ_mcap=80,
                turnover_rate=2,
                volume_ratio=1.2,
                ma5=9.5,
                ma10=9,
                ma20=8.5,
                ma5_angle=3,
                industry="IT",
                mf_wan=100,
                mf_ratio=5,
                tags=[],
                scenarios=[],
            )

        candidates = [
            _ss("000002", "B", 80, "strong"),
            _ss("000001", "A", 90, "strong"),
            _ss("000003", "C", 70, "normal"),
        ]
        result = ts._rank_and_limit(candidates)
        assert len(result) == 3
        assert result[0].stock_code == "000001"
        assert result[0].score == 90
        assert result[1].stock_code == "000002"
        assert result[2].stock_code == "000003"


class TestTrendScreenerMode:
    def test_determine_mode_strong(self):
        """bias5 <= 3%, angle >= 1, ma5 > ma10 > ma20 → strong"""
        ts = TrendScreener()
        row = {"price": 50.5, "ma5": 50.0, "ma10": 49.0, "ma20": 48.0, "ma5_angle": 2}
        assert ts._determine_mode([], row, []) == "strong"

    def test_determine_mode_strong_angle_fallback(self):
        ts = TrendScreener()
        row = {"price": 55.0, "ma5": 50.0, "ma10": 49.0, "ma20": 48.0, "ma5_angle": 3}
        assert ts._determine_mode([], row, []) == "strong"

    def test_determine_mode_normal(self):
        ts = TrendScreener()
        row = {"price": 50, "ma5": 49, "ma10": 48, "ma20": 47, "ma5_angle": 0.5}
        assert ts._determine_mode([], row, []) == "normal"

    def test_determine_mode_ma_zero(self):
        ts = TrendScreener()
        row = {"price": 50, "ma5": 0, "ma10": 0, "ma20": 0, "ma5_angle": 0}
        assert ts._determine_mode([], row, []) == "normal"

    def test_has_ma20_bounce(self):
        ts = TrendScreener()
        history = [{"low": 46, "ma20": 46.5}]  # abs(46-46.5)/46.5*100 = 1.07% < 3%
        assert ts._has_ma20_bounce(history, 46.5) is True

    def test_no_ma20_bounce(self):
        ts = TrendScreener()
        history = [{"low": 40, "ma20": 46}]
        assert ts._has_ma20_bounce(history, 46) is False

    def test_has_ma20_bounce_empty_history(self):
        ts = TrendScreener()
        assert ts._has_ma20_bounce([], 46) is False


class TestTrendScreenerScenarios:
    def test_match_breakout_scenario(self):
        ts = TrendScreener()
        row = {"price": 50}
        history = [{"high": 48}] * 20
        scenarios = ts._match_scenarios(row, history, ["放量启动"], "")
        assert "突破追涨" in scenarios

    def test_match_scenario_not_panic(self):
        """恐慌状态不产生突破类场景"""
        ts = TrendScreener()
        scenarios = ts._match_scenarios({}, [], ["放量启动"], "恐慌")
        assert "突破追涨" not in scenarios

    def test_match_new_high_breakout(self):
        ts = TrendScreener()
        row = {"price": 100}
        history = [{"high": 100}] * 20  # price >= high*0.99
        scenarios = ts._match_scenarios(row, history, ["放量启动"], "")
        assert "新高突破" in scenarios

    def test_match_trend_speedup(self):
        ts = TrendScreener()
        scenarios = ts._match_scenarios({}, [], ["趋势延续", "主力介入"], "")
        assert "趋势加速" in scenarios

    def test_match_qiangshi_reversal(self):
        ts = TrendScreener()
        history = [{"change_pct": -3.0}]
        row = {"change_pct": 5.0}
        scenarios = ts._match_scenarios(row, history, ["放量启动"], "")
        assert "强势反包" in scenarios

    def test_match_huicai_ma5(self):
        ts = TrendScreener()
        row = {"price": 50, "ma5": 49.5}
        scenarios = ts._match_scenarios(row, [], ["缩量回调"], "")
        assert "回踩MA5" in scenarios

    def test_match_huicai_ma10(self):
        ts = TrendScreener()
        row = {"price": 50, "ma10": 49.5}
        scenarios = ts._match_scenarios(row, [], ["缩量回调", "趋势延续"], "")
        assert "回踩MA10" in scenarios

    def test_match_huicai_ma20(self):
        ts = TrendScreener()
        row = {"price": 50, "ma20": 49}
        scenarios = ts._match_scenarios(row, [], ["量能放大"], "")
        assert "回踩MA20" in scenarios

    def test_match_qiangshi_hengpan(self):
        ts = TrendScreener()
        history = [{"high": 51, "low": 49}] * 5
        scenarios = ts._match_scenarios({}, history, ["蓄力中"], "")
        assert "强势横盘" in scenarios

    def test_match_dibu_fantan(self):
        ts = TrendScreener()
        history = [{"price": 45, "ma20": 46}]
        row = {"price": 47, "ma20": 46}
        scenarios = ts._match_scenarios(row, history, ["量能放大"], "")
        assert "底部反弹" in scenarios

    def test_match_quxian_fasan(self):
        ts = TrendScreener()
        history = [
            {"ma5": 49.0, "ma10": 48.0, "ma20": 47.0},
            {"ma5": 50.0, "ma10": 49.0, "ma20": 48.0},
            {"ma5": 50.5, "ma10": 50, "ma20": 49.8},
        ]
        row = {"ma5": 55, "ma10": 52, "ma20": 50}
        scenarios = ts._match_scenarios(row, history, [], "")
        assert "均线发散" in scenarios

    def test_match_trend_xingjin_fallback(self):
        ts = TrendScreener()
        scenarios = ts._match_scenarios(
            {"price": 50, "ma20": 48},
            [{"high": 47}] * 20,
            ["趋势延续"],
            "",
        )
        assert "趋势行进" in scenarios

    def test_no_scenarios_no_fallback(self):
        ts = TrendScreener()
        scenarios = ts._match_scenarios({}, [], ["缩量回调"], "")
        assert scenarios == []


# ===================================================================
# TrendScreener — DB 依赖（基础 DB 交互）
# ===================================================================


def _init_trend_tables(conn: sqlite3.Connection):
    """创建 TrendScreener 所需的表结构"""
    conn.executescript("""
        DROP TABLE IF EXISTS stock_basic;
        CREATE TABLE stock_basic (
            stock_code TEXT, stock_name TEXT,
            trade_date TEXT,
            change_pct REAL, price REAL,
            total_market_cap REAL,
            circ_market_cap REAL,
            turnover_rate REAL, volume_ratio REAL, amplitude REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma5_angle REAL,
            industry TEXT,
            open REAL, high REAL, low REAL, prev_close REAL,
            main_force_net REAL, main_force_ratio REAL,
            super_large_net REAL, large_net REAL,
            medium_net REAL, small_net REAL,
            avg_vol_5d REAL, avg_vol_20d REAL,
            pe_ttm REAL, pb_ratio REAL,
            revenue_growth REAL, profit_growth REAL
        );
        DROP TABLE IF EXISTS sector_stocks;
        CREATE TABLE sector_stocks (
            stock_code TEXT, sector_code TEXT
        );
        DROP TABLE IF EXISTS sector_industry;
        CREATE TABLE sector_industry (
            sector_code TEXT, trade_date TEXT,
            change_percent REAL, sector_name TEXT,
            main_force_net REAL
        );
        DROP TABLE IF EXISTS sector_concept;
        CREATE TABLE sector_concept (
            sector_code TEXT, trade_date TEXT,
            change_percent REAL, sector_name TEXT,
            main_force_net REAL
        );
        DROP TABLE IF EXISTS sector_hot_history;
        CREATE TABLE sector_hot_history (
            sector_code TEXT, trade_date TEXT,
            rank INTEGER
        );
        DROP TABLE IF EXISTS regulatory_letter;
        CREATE TABLE regulatory_letter (
            stock_code TEXT, trade_date TEXT,
            risk_level INTEGER, risk_type TEXT, title TEXT
        );
        DROP TABLE IF EXISTS cls_telegraph;
        CREATE TABLE cls_telegraph (
            trade_date TEXT, ctime TEXT,
            title TEXT, stock_tags TEXT
        );
        DROP TABLE IF EXISTS stock_indicators;
        CREATE TABLE stock_indicators (
            stock_code TEXT, trade_date TEXT,
            bbi_weekly REAL
        );
    """)


def _insert_trend_stock(conn, code, name, **kw):
    """辅助：插入一条 stock_basic 记录"""
    defaults = {
        "stock_code": code,
        "stock_name": name,
        "trade_date": "2026-06-01",
        "change_pct": 2.0,
        "price": 50.0,
        "total_market_cap": 500_0000_0000 * 2,
        "circ_market_cap": 300_0000_0000,
        "turnover_rate": 3.0,
        "volume_ratio": 1.5,
        "amplitude": 4.0,
        "ma5": 49.0,
        "ma10": 48.0,
        "ma20": 47.0,
        "ma5_angle": 2.0,
        "industry": "科技",
        "open": 49.5,
        "high": 51.0,
        "low": 49.0,
        "prev_close": 49.0,
        "main_force_net": 100_0000,
        "main_force_ratio": 5.0,
        "super_large_net": 50_0000,
        "large_net": 30_0000,
        "medium_net": -20_0000,
        "small_net": -60_0000,
        "avg_vol_5d": 2000,
        "avg_vol_20d": 1500,
        "pe_ttm": 30.0,
        "pb_ratio": 5.0,
        "revenue_growth": 0.2,
        "profit_growth": 0.15,
    }
    defaults.update(kw)
    cols = ", ".join(defaults.keys())
    vals = ", ".join("?" * len(defaults))
    conn.execute(f"INSERT INTO stock_basic ({cols}) VALUES ({vals})", list(defaults.values()))


class TestTrendScreenerDB:
    def test_screen_empty_db(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_trend_tables(conn)
        conn.commit()
        conn.close()

        ts = TrendScreener(db_path=db_path, top_n=5)
        result = ts.screen("2026-06-01")
        assert result == []

    def test_screen_with_valid_stock(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_trend_tables(conn)
        _insert_trend_stock(
            conn,
            "002371",
            "北方华创",
            price=52.0,
            ma5=51.0,
            ma10=50.0,
            ma20=48.0,
            ma5_angle=5.0,
            volume_ratio=2.0,
            main_force_net=1000_0000,
        )
        conn.execute("INSERT INTO sector_stocks (stock_code, sector_code) VALUES ('002371', 'BK0001')")
        conn.execute(
            "INSERT INTO sector_hot_history (sector_code, trade_date, rank) VALUES ('BK0001', '2026-06-01', 1)"
        )
        conn.commit()
        conn.close()

        ts = TrendScreener(db_path=db_path, top_n=5)
        result = ts.screen("2026-06-01")
        assert len(result) >= 1
        assert result[0].stock_code == "002371"
        assert result[0].stock_name == "北方华创"

    def test_screen_filters_risk_stocks(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_trend_tables(conn)
        _insert_trend_stock(
            conn,
            "002371",
            "北方华创",
            price=52.0,
            ma5=51.0,
            ma10=50.0,
            ma20=48.0,
            ma5_angle=5.0,
            volume_ratio=2.0,
            main_force_net=1000_0000,
        )
        conn.execute("INSERT INTO sector_stocks (stock_code, sector_code) VALUES ('002371', 'BK0001')")
        conn.execute(
            "INSERT INTO sector_hot_history (sector_code, trade_date, rank) VALUES ('BK0001', '2026-06-01', 1)"
        )
        conn.execute(
            "INSERT INTO regulatory_letter (stock_code, trade_date, risk_level, risk_type, title) "
            "VALUES ('002371', '2026-05-30', 3, '财务造假', '虚构利润')"
        )
        conn.commit()
        conn.close()

        ts = TrendScreener(db_path=db_path, top_n=5)
        result = ts.screen("2026-06-01")
        assert len(result) == 0

    def test_get_history(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_trend_tables(conn)
        for i, day in enumerate(["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"]):
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, price, open, high, low, prev_close, "
                "change_pct, volume_ratio, ma5, ma10, ma20) "
                "VALUES ('000001', ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0)",
                (day, float(i + 1)),
            )
        conn.commit()
        conn.close()

        ts = TrendScreener(db_path=db_path)
        conn2 = sqlite3.connect(db_path)
        history = ts._get_history(conn2, "000001", "2026-06-05", days=5)
        conn2.close()
        assert len(history) >= 3
        assert history[-1]["trade_date"] <= "2026-06-04"

    def test_screen_passes_sector_gate(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_trend_tables(conn)
        _insert_trend_stock(
            conn,
            "002371",
            "北方华创",
            price=52.0,
            ma5=51.0,
            ma10=50.0,
            ma20=48.0,
            ma5_angle=5.0,
            volume_ratio=2.0,
            main_force_net=1000_0000,
        )
        conn.execute("INSERT INTO sector_stocks (stock_code, sector_code) VALUES ('002371', 'BK0001')")
        conn.execute(
            "INSERT INTO sector_hot_history (sector_code, trade_date, rank) VALUES ('BK0001', '2026-06-01', 1)"
        )
        # 不在热点上的板块
        _insert_trend_stock(
            conn,
            "000002",
            "万科A",
            stock_code="000002",
            stock_name="万科A",
            price=52.0,
            ma5=51.0,
            ma10=50.0,
            ma20=48.0,
            ma5_angle=5.0,
            volume_ratio=2.0,
            main_force_net=1000_0000,
        )
        conn.execute("INSERT INTO sector_stocks (stock_code, sector_code) VALUES ('000002', 'BK9999')")
        conn.commit()
        conn.close()

        ts = TrendScreener(db_path=db_path, top_n=5)
        result = ts.screen("2026-06-01")
        codes = {r.stock_code for r in result}
        assert "002371" in codes
        assert "000002" not in codes

    def test_screen_filters_sector_blacklist(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_trend_tables(conn)
        _insert_trend_stock(
            conn,
            "002371",
            "测试票",
            price=52.0,
            ma5=51.0,
            ma10=50.0,
            ma20=48.0,
            ma5_angle=5.0,
            volume_ratio=2.0,
            main_force_net=1000_0000,
        )
        # BK1575 = 白酒, 在黑名单中
        conn.execute("INSERT INTO sector_stocks (stock_code, sector_code) VALUES ('002371', 'BK1575')")
        conn.execute(
            "INSERT INTO sector_hot_history (sector_code, trade_date, rank) VALUES ('BK1575', '2026-06-01', 1)"
        )
        conn.commit()
        conn.close()

        ts = TrendScreener(db_path=db_path, top_n=5)
        result = ts.screen("2026-06-01")
        assert len(result) == 0


# ===================================================================
# ProfileBuilder — DB 依赖
# ===================================================================


def _init_profile_tables(conn: sqlite3.Connection):
    conn.executescript("""
        DROP TABLE IF EXISTS stock_basic;
        CREATE TABLE stock_basic (
            stock_code TEXT, trade_date TEXT,
            price REAL, open REAL, high REAL, low REAL, prev_close REAL,
            change_pct REAL, volume_ratio REAL, amplitude REAL,
            main_force_net REAL, main_force_ratio REAL, small_net REAL,
            industry TEXT, turnover_rate REAL,
            total_market_cap REAL,
            pe_ttm REAL, pb_ratio REAL,
            revenue_growth REAL, profit_growth REAL,
            volume REAL,
            ma5 REAL, ma10 REAL, ma20 REAL
        );
        DROP TABLE IF EXISTS sector_stocks;
        CREATE TABLE sector_stocks (
            stock_code TEXT, sector_code TEXT
        );
        DROP TABLE IF EXISTS sector_industry;
        CREATE TABLE sector_industry (
            sector_code TEXT, trade_date TEXT,
            change_percent REAL, sector_name TEXT,
            main_force_net REAL
        );
        DROP TABLE IF EXISTS sector_concept;
        CREATE TABLE sector_concept (
            sector_code TEXT, trade_date TEXT,
            change_percent REAL, sector_name TEXT,
            main_force_net REAL
        );
        DROP TABLE IF EXISTS sector_hot_history;
        CREATE TABLE sector_hot_history (
            sector_code TEXT, trade_date TEXT,
            rank INTEGER
        );
        DROP TABLE IF EXISTS stock_indicators;
        CREATE TABLE stock_indicators (
            stock_code TEXT, trade_date TEXT,
            macd_dif REAL, macd_dea REAL, macd_bar REAL,
            rsi6 REAL, rsi12 REAL, rsi24 REAL,
            kdj_k REAL, kdj_d REAL, kdj_j REAL,
            bb_upper REAL, bb_mid REAL, bb_lower REAL,
            bb_width REAL, bb_pct_b REAL
        );
        DROP TABLE IF EXISTS cls_telegraph;
        CREATE TABLE cls_telegraph (
            trade_date TEXT, ctime TEXT,
            title TEXT, stock_tags TEXT
        );
        DROP TABLE IF EXISTS regulatory_letter;
        CREATE TABLE regulatory_letter (
            stock_code TEXT, trade_date TEXT,
            risk_level INTEGER, risk_type TEXT, title TEXT
        );
        DROP TABLE IF EXISTS limit_pool;
        CREATE TABLE limit_pool (
            stock_code TEXT, trade_date TEXT,
            pool_type TEXT
        );
    """)


def _make_stock_score(code="002371", name="北方华创", **kw) -> StockScore:
    params = {
        "stock_code": code,
        "stock_name": name,
        "trend_mode": "strong",
        "score": 80.0,
        "price": 52.0,
        "change_pct": 2.5,
        "mcap": 500,
        "circ_mcap": 300,
        "turnover_rate": 3.0,
        "volume_ratio": 1.5,
        "ma5": 51.0,
        "ma10": 50.0,
        "ma20": 48.0,
        "ma5_angle": 5.0,
        "industry": "科技",
        "mf_wan": 500,
        "mf_ratio": 8.0,
        "tags": [],
        "scenarios": [],
    }
    params.update(kw)
    return StockScore(**params)


class TestProfileBuilder:
    def test_build_empty_list(self):
        pb = ProfileBuilder(db_path=":memory:")
        result = pb.build([], "2026-06-01")
        assert result == []

    def test_build_missing_tables(self, db_path):
        """表不存在时抛出异常（非静默失败，符合设计）"""
        stocks = [_make_stock_score()]
        pb = ProfileBuilder(db_path=db_path)
        with pytest.raises((ValueError, TypeError)):
            pb.build(stocks, "2026-06-01")

    def test_build_valid_stock(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_profile_tables(conn)

        # 当天行情
        conn.execute(
            """INSERT INTO stock_basic (stock_code, trade_date, price, open, high, low, prev_close,
               change_pct, volume_ratio, amplitude, main_force_net, main_force_ratio, small_net,
               industry, turnover_rate, total_market_cap, pe_ttm, pb_ratio, revenue_growth,
               profit_growth, volume, ma5, ma10, ma20)
               VALUES ('002371', '2026-06-01', 52.0, 50.0, 53.0, 49.5, 50.0,
                       2.5, 1.5, 4.0, 500_0000, 8.0, -100_0000,
                       '科技', 3.0, 500_0000_0000, 30.0, 5.0, 0.2, 0.15, 1000,
                       51.0, 50.0, 48.0)"""
        )

        # 历史数据 (需要 ma5, ma10, ma20, volume, main_force_net 列)
        for i, day in enumerate(["2026-05-20", "2026-05-21", "2026-05-22"]):
            conn.execute(
                """INSERT INTO stock_basic (stock_code, trade_date, price, open, high, low, prev_close,
                   change_pct, volume_ratio, ma5, ma10, ma20, volume, main_force_net)
                   VALUES ('002371', ?, 50.0 + ?, 49.0, 51.0, 48.5, 49.0,
                           2.0, 1.2, 49.0, 48.0, 47.0, 800, 100_0000)""",
                (day, float(i)),
            )

        # 板块
        conn.execute("INSERT INTO sector_stocks (stock_code, sector_code) VALUES ('002371', 'BK0001')")
        conn.execute(
            "INSERT INTO sector_industry (sector_code, trade_date, change_percent, sector_name, main_force_net) "
            "VALUES ('BK0001', '2026-06-01', 2.0, '半导体', 1000_0000)"
        )
        conn.execute(
            "INSERT INTO sector_hot_history (sector_code, trade_date, rank) VALUES ('BK0001', '2026-06-01', 1)"
        )

        # 技术指标
        conn.execute(
            """INSERT INTO stock_indicators (stock_code, trade_date,
               macd_dif, macd_dea, macd_bar, rsi6, rsi12, rsi24,
               kdj_k, kdj_d, kdj_j,
               bb_upper, bb_mid, bb_lower, bb_width, bb_pct_b)
               VALUES ('002371', '2026-06-01',
                       1.5, 1.0, 0.5, 60.0, 55.0, 50.0,
                       70.0, 50.0, 90.0,
                       55.0, 50.0, 45.0, 10.0, 60.0)"""
        )

        conn.commit()
        conn.close()

        pb = ProfileBuilder(db_path=db_path)
        profiles = pb.build(
            [_make_stock_score()],
            "2026-06-01",
            market_state="分化",
            breadth={"up_count": 2000, "down_count": 2000},
        )

        assert len(profiles) == 1
        p = profiles[0]
        assert p.code == "002371"
        assert p.name == "北方华创"
        assert p.score == 80.0
        assert p.trend_mode == "strong"
        assert p.market_state == "分化"
        assert p.snapshot.get("price") == 52.0
        assert p.valuation.get("pe_ttm") == 30.0
        assert len(p.sectors) >= 1
        assert p.sectors[0]["code"] == "BK0001"
        assert p.sector_resonance.get("overall") is True

    def test_build_no_history(self, db_path):
        """无历史数据的股票 → 默认 history 值"""
        conn = sqlite3.connect(db_path)
        _init_profile_tables(conn)
        conn.execute(
            """INSERT INTO stock_basic (stock_code, trade_date, price, open, high, low, prev_close,
               change_pct, volume_ratio, amplitude, main_force_net, main_force_ratio, small_net,
               industry, turnover_rate, total_market_cap, pe_ttm, pb_ratio, revenue_growth,
               profit_growth, volume, ma5, ma10, ma20)
               VALUES ('002371', '2026-06-01', 52.0, 50.0, 53.0, 49.5, 50.0,
                       2.5, 1.5, 4.0, 500_0000, 8.0, -100_0000,
                       '科技', 3.0, 500_0000_0000, 30.0, 5.0, 0.2, 0.15, 1000,
                       51.0, 50.0, 48.0)"""
        )
        conn.commit()
        conn.close()

        pb = ProfileBuilder(db_path=db_path)
        profiles = pb.build([_make_stock_score()], "2026-06-01")
        assert len(profiles) == 1
        p = profiles[0]
        assert p.history.get("consecutive_yang") == 0
        assert p.history.get("daily") == []
        assert p.history.get("ma5") == 51.0
        assert p.history.get("ma10") == 50.0
        assert p.history.get("ma20") == 48.0

    def test_build_with_telegraph(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_profile_tables(conn)
        conn.execute(
            """INSERT INTO stock_basic (stock_code, trade_date, price, open, high, low, prev_close,
               change_pct, volume_ratio, amplitude, main_force_net, main_force_ratio, small_net,
               industry, turnover_rate, total_market_cap, pe_ttm, pb_ratio, revenue_growth,
               profit_growth, volume, ma5, ma10, ma20)
               VALUES ('002371', '2026-06-01', 52.0, 50.0, 53.0, 49.5, 50.0,
                       2.5, 1.5, 4.0, 500_0000, 8.0, -100_0000,
                       '科技', 3.0, 500_0000_0000, 30.0, 5.0, 0.2, 0.15, 1000,
                       51.0, 50.0, 48.0)"""
        )
        conn.execute(
            """INSERT INTO cls_telegraph (trade_date, ctime, title, stock_tags)
               VALUES ('2026-06-01', '09:30:00', '北方华创业绩预喜',
                       '[{"code": "002371", "name": "北方华创"}]')"""
        )
        conn.commit()
        conn.close()

        pb = ProfileBuilder(db_path=db_path)
        profiles = pb.build([_make_stock_score()], "2026-06-01")
        assert len(profiles) == 1
        p = profiles[0]
        assert len(p.telegraphs) == 1
        assert p.telegraphs[0]["summary"] == "北方华创业绩预喜"

    def test_build_with_risks(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_profile_tables(conn)
        conn.execute(
            """INSERT INTO stock_basic (stock_code, trade_date, price, open, high, low, prev_close,
               change_pct, volume_ratio, amplitude, main_force_net, main_force_ratio, small_net,
               industry, turnover_rate, total_market_cap, pe_ttm, pb_ratio, revenue_growth,
               profit_growth, volume, ma5, ma10, ma20)
               VALUES ('002371', '2026-06-01', 52.0, 50.0, 53.0, 49.5, 50.0,
                       2.5, 1.5, 4.0, 500_0000, 8.0, -100_0000,
                       '科技', 3.0, 500_0000_0000, 30.0, 5.0, 0.2, 0.15, 1000,
                       51.0, 50.0, 48.0)"""
        )
        # 监管函：90天内, risk_level >= 2
        conn.execute(
            "INSERT INTO regulatory_letter (stock_code, trade_date, risk_level, risk_type, title) "
            "VALUES ('002371', '2026-05-01', 2, '信披违规', '未及时披露关联交易')"
        )
        conn.commit()
        conn.close()

        pb = ProfileBuilder(db_path=db_path)
        profiles = pb.build([_make_stock_score()], "2026-06-01")
        assert len(profiles) == 1
        p = profiles[0]
        assert any(r["type"] == "监管函" for r in p.risks)

    def test_build_with_zhapan_risk(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_profile_tables(conn)
        conn.execute(
            """INSERT INTO stock_basic (stock_code, trade_date, price, open, high, low, prev_close,
               change_pct, volume_ratio, amplitude, main_force_net, main_force_ratio, small_net,
               industry, turnover_rate, total_market_cap, pe_ttm, pb_ratio, revenue_growth,
               profit_growth, volume, ma5, ma10, ma20)
               VALUES ('002371', '2026-06-01', 52.0, 50.0, 53.0, 49.5, 50.0,
                       2.5, 1.5, 4.0, 500_0000, 8.0, -100_0000,
                       '科技', 3.0, 500_0000_0000, 30.0, 5.0, 0.2, 0.15, 1000,
                       51.0, 50.0, 48.0)"""
        )
        conn.execute(
            "INSERT INTO limit_pool (stock_code, trade_date, pool_type) VALUES ('002371', '2026-06-01', '炸板')"
        )
        conn.commit()
        conn.close()

        pb = ProfileBuilder(db_path=db_path)
        profiles = pb.build([_make_stock_score()], "2026-06-01")
        assert len(profiles) == 1
        p = profiles[0]
        assert any(r["type"] == "炸板未回封" for r in p.risks)

    def test_build_multiple_stocks(self, db_path):
        conn = sqlite3.connect(db_path)
        _init_profile_tables(conn)
        for code, _name in [("002371", "北方华创"), ("000001", "平安银行")]:
            conn.execute(
                """INSERT INTO stock_basic (stock_code, trade_date, price, open, high, low, prev_close,
                   change_pct, volume_ratio, amplitude, main_force_net, main_force_ratio, small_net,
                   industry, turnover_rate, total_market_cap, pe_ttm, pb_ratio, revenue_growth,
                   profit_growth, volume, ma5, ma10, ma20)
                   VALUES (?, '2026-06-01', 50.0, 49.0, 51.0, 48.5, 49.0,
                           1.0, 1.0, 3.0, 100_0000, 2.0, -50_0000,
                           '金融', 2.0, 500_0000_0000, 10.0, 1.0, 0.1, 0.08, 500,
                           49.0, 48.0, 47.0)""",
                (code,),
            )
        conn.commit()
        conn.close()

        stocks = [
            _make_stock_score(code="002371", name="北方华创"),
            _make_stock_score(code="000001", name="平安银行"),
        ]
        pb = ProfileBuilder(db_path=db_path)
        profiles = pb.build(stocks, "2026-06-01")
        assert len(profiles) == 2
        codes = {p.code for p in profiles}
        assert codes == {"002371", "000001"}


# ===================================================================
# ProfileBuilder — 内部方法单独测试
# ===================================================================


class TestProfileBuilderInternals:
    def test_build_snapshot_empty(self, db_path):
        """_build_snapshot 查不到数据时返回空 dict"""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stock_basic (stock_code TEXT, trade_date TEXT, "
            "price REAL, open REAL, high REAL, low REAL, change_pct REAL, volume_ratio REAL, "
            "amplitude REAL, main_force_net REAL, main_force_ratio REAL, small_net REAL, "
            "industry TEXT, turnover_rate REAL)"
        )
        conn.commit()
        pb = ProfileBuilder(db_path=db_path)
        result = pb._build_snapshot(_make_stock_score(), conn, "2026-06-01")
        conn.close()
        assert result == {}

    def test_build_history_empty(self):
        pb = ProfileBuilder()
        s = _make_stock_score()
        result = pb._build_history(s, [])
        assert result["ma5"] == s.ma5
        assert result["ma10"] == s.ma10
        assert result["ma20"] == s.ma20
        assert result["consecutive_yang"] == 0
        assert result["daily"] == []

    def test_build_history_with_data(self):
        pb = ProfileBuilder()
        history = [
            {
                "trade_date": "2026-05-28",
                "price": 49.0,
                "open": 48.5,
                "high": 49.5,
                "low": 48.0,
                "change_pct": 1.0,
                "volume_ratio": 1.2,
                "ma5": 48.5,
                "ma10": 48.0,
                "ma20": 47.0,
                "volume": 800,
                "main_force_net": 100_0000,
            },
            {
                "trade_date": "2026-05-29",
                "price": 50.0,
                "open": 49.0,
                "high": 50.5,
                "low": 49.0,
                "change_pct": 2.0,
                "volume_ratio": 1.3,
                "ma5": 49.0,
                "ma10": 48.5,
                "ma20": 47.5,
                "volume": 900,
                "main_force_net": 200_0000,
            },
            {
                "trade_date": "2026-05-30",
                "price": 51.0,
                "open": 50.0,
                "high": 51.5,
                "low": 50.0,
                "change_pct": 2.0,
                "volume_ratio": 1.4,
                "ma5": 50.0,
                "ma10": 49.0,
                "ma20": 48.0,
                "volume": 1000,
                "main_force_net": 300_0000,
            },
        ]
        s = _make_stock_score(ma5=51.0, ma10=50.0, ma20=48.0)
        result = pb._build_history(s, history)
        assert result["consecutive_yang"] == 3
        assert result["ma_bull_days"] == 3
        assert result["mf_5d_cum"] == 600_0000
        assert result["mf_consec_inflow"] == 3
        assert len(result["daily"]) == 3
        assert result["daily"][0]["close"] == 49.0

    def test_build_resonance_all_cold(self):
        pb = ProfileBuilder()
        result = pb._build_resonance(
            [{"code": "BK001", "name": "A", "change_pct": 1.0}],
            {"BK001": 0},
        )
        assert result["overall"] is False
        assert result["BK001"]["hot"] is False

    def test_build_resonance_some_hot(self):
        pb = ProfileBuilder()
        result = pb._build_resonance(
            [
                {"code": "BK001", "name": "A", "change_pct": 1.0},
                {"code": "BK002", "name": "B", "change_pct": -0.5},
            ],
            {"BK001": 3, "BK002": 0},
        )
        assert result["overall"] is True
        assert result["BK001"]["hot"] is True
        assert result["BK002"]["hot"] is False

    def test_build_resonance_empty(self):
        pb = ProfileBuilder()
        result = pb._build_resonance([], {})
        assert result["overall"] is False

    def test_build_valuation_empty(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stock_basic (stock_code TEXT, trade_date TEXT, "
            "pe_ttm REAL, pb_ratio REAL, total_market_cap REAL, "
            "revenue_growth REAL, profit_growth REAL)"
        )
        conn.commit()
        pb = ProfileBuilder(db_path=db_path)
        result = pb._build_valuation(_make_stock_score(), conn, "2026-06-01")
        conn.close()
        assert result == {}

    def test_build_sector_ref_empty(self):
        pb = ProfileBuilder()
        s = _make_stock_score()
        result = pb._build_sector_ref(s, {}, {}, {})
        assert result == []

    def test_compute_rps_insufficient_history(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stock_basic (stock_code TEXT, trade_date TEXT, price REAL, change_pct REAL)"
        )
        for i in range(10):
            conn.execute(
                "INSERT INTO stock_basic (stock_code, trade_date, price, change_pct) VALUES (?, ?, ?, ?)",
                ("002371", f"2026-05-{20 + i:02d}", 50.0 + i, 1.0),
            )
        conn.commit()

        pb = ProfileBuilder(db_path=db_path)
        result = pb._compute_rps(conn, "002371", "2026-05-30")
        conn.close()
        assert result["rps_20"] == 0
        assert result["rps_60"] == 0
        assert result["rps_120"] == 0

    def test_load_telegraphs_no_match(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cls_telegraph (trade_date TEXT, ctime TEXT, title TEXT, stock_tags TEXT)"
        )
        conn.execute(
            "INSERT INTO cls_telegraph VALUES ('2026-06-01', '10:00', '其他股票消息', '[{\"code\": \"000999\"}]')"
        )
        conn.commit()

        pb = ProfileBuilder(db_path=db_path)
        result = pb._load_telegraphs(conn, _make_stock_score(), "2026-06-01")
        conn.close()
        assert result == []

    def test_load_risks_empty(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS regulatory_letter (stock_code TEXT, trade_date TEXT, "
            "risk_level INTEGER, risk_type TEXT, title TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cls_telegraph (trade_date TEXT, ctime TEXT, title TEXT, stock_tags TEXT)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS limit_pool (stock_code TEXT, trade_date TEXT, pool_type TEXT)")
        conn.commit()

        pb = ProfileBuilder(db_path=db_path)
        result = pb._load_risks(conn, ["002371"], "2026-06-01")
        conn.close()
        assert result == {"002371": []}

    def test_calc_indicators_no_indicators_table(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS stock_basic (stock_code TEXT, trade_date TEXT, price REAL, "
            "open REAL, high REAL, low REAL, prev_close REAL, volume REAL)"
        )
        conn.commit()

        pb = ProfileBuilder(db_path=db_path)
        # _calc_indicators 内部会创建自己的连接去查 stock_indicators
        # 如果 stock_indicators 表不存在会抛出 OperationalError
        with pytest.raises((sqlite3.OperationalError, Exception)):
            pb._calc_indicators(conn, "002371", "2026-06-01", [], {})
        conn.close()
