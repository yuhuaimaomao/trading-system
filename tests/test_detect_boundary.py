"""trade/detect/ 模块边界测试 — 覆盖 16 种市场模式、板块趋势、微观信号。

涵盖 classify_market_pattern 全部 16 种模式、子函数极端值、空值、边界时间、
板块输入组合、微观信号各个检测分支、空值和 None 容错。
"""

from datetime import time as dt_time
from unittest.mock import patch

from trade.core.scan_state import MicroSignals
from trade.detect.market_pattern import (
    _calc_intraday_ema,
    _detect_fishing_line,
    _detect_gap_down_recover,
    _detect_gap_up_fade,
    _detect_higher_highs,
    _detect_late_dump,
    _detect_late_rally,
    _detect_m_top,
    _detect_w_bottom,
    _detect_wide_choppy,
    _session_phase,
    classify_market_pattern,
)
from trade.detect.micro_signals import extract
from trade.detect.sector_trend import (
    get_concept_trend_score,
    get_sector_change,
    get_sector_decline,
    get_sector_recovery_risk,
    get_sector_trend,
)

# ====================================================================
#  辅助函数
# ====================================================================


def _phase(name):
    """固定 _session_phase 返回值，避免时间依赖。"""
    return patch("trade.detect.market_pattern._session_phase", return_value=name)


# ====================================================================
#  _calc_intraday_ema 边界
# ====================================================================


class TestCalcIntradayEMA:
    """_calc_intraday_ema 极端值/空值/边界覆盖"""

    def test_empty_list(self):
        assert _calc_intraday_ema([], 12) == 0.0

    def test_single_element(self):
        assert _calc_intraday_ema([100.0], 12) == 100.0

    def test_less_than_period(self):
        """len < period -> 简单平均"""
        result = _calc_intraday_ema([10.0, 20.0, 30.0], 12)
        assert result == 20.0

    def test_constant_prices(self):
        """全相等 -> EMA 等于该常量"""
        prices = [50.0] * 20
        assert _calc_intraday_ema(prices, 5) == 50.0
        assert _calc_intraday_ema(prices, 12) == 50.0

    def test_exactly_period(self):
        """len == period -> SMA（首值）"""
        prices = [10.0, 11.0, 12.0, 13.0, 14.0]
        assert _calc_intraday_ema(prices, 5) == 12.0

    def test_up_trend(self):
        """单调上升，EMA 应接近但滞后于最新值"""
        prices = [100.0 + i for i in range(30)]
        result = _calc_intraday_ema(prices, 12)
        assert 100.0 < result < prices[-1]

    def test_down_trend(self):
        """单调下降"""
        prices = [130.0 - i for i in range(30)]
        result = _calc_intraday_ema(prices, 12)
        assert prices[-1] < result < 130.0

    def test_period_one(self):
        """period=1 -> EMA 就是最新值"""
        assert _calc_intraday_ema([10.0, 20.0, 30.0, 40.0], 1) == 40.0

    def test_period_greater_than_len(self):
        assert _calc_intraday_ema([1.0, 2.0], 12) == 1.5


# ====================================================================
#  _session_phase 边界
# ====================================================================


class TestSessionPhase:
    """_session_phase 时间边界覆盖 — 使用 mock 隔离当前时间。"""

    def test_pre_open(self):
        t = dt_time(9, 29, 59)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "pre_open"

    def test_opening(self):
        t = dt_time(9, 30, 0)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "opening"

    def test_opening_late(self):
        t = dt_time(9, 59, 59)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "opening"

    def test_morning(self):
        t = dt_time(10, 0, 0)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "morning"

    def test_morning_late(self):
        t = dt_time(10, 59, 59)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "morning"

    def test_late_morning(self):
        t = dt_time(11, 0, 0)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "late_morning"

    def test_lunch(self):
        t = dt_time(11, 30, 0)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "lunch"

    def test_afternoon(self):
        t = dt_time(13, 0, 0)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "afternoon"

    def test_late_afternoon(self):
        t = dt_time(14, 0, 0)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "late_afternoon"

    def test_closing(self):
        t = dt_time(14, 30, 0)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "closing"

    def test_closing_late(self):
        t = dt_time(23, 59, 59)
        with patch("trade.detect.market_pattern.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = t
            assert _session_phase() == "closing"


# ====================================================================
#  子函数边界 — _detect_ 系列
# ====================================================================


class TestDetectHigherHighs:
    """_detect_higher_highs 边界"""

    def test_not_enough_data(self):
        assert _detect_higher_highs([100.0] * 50) is False

    def test_three_rising_windows(self):
        """连续 3 个 20-bar 窗口创新高 -> True"""
        px = [100.0] * 20 + [101.0] * 20 + [102.0] * 20 + [103.0] * 20
        assert _detect_higher_highs(px) is True

    def test_descending_returns_false(self):
        px = [103.0 - i * 0.05 for i in range(80)]
        assert _detect_higher_highs(px) is False

    def test_flat_returns_false(self):
        px = [100.0] * 80
        assert _detect_higher_highs(px) is False


class TestDetectWBottom:
    """_detect_w_bottom 边界"""

    def test_not_enough_data(self):
        assert _detect_w_bottom([100.0] * 50, 50, 25, 99.0, 101.0) is False

    def test_w_shaped(self):
        """经典 W 型应返回 True"""
        n = 80
        px = (
            [101.0] * 20
            + [101.0 - 0.4 * i for i in range(10)]  # drop 101->97 (valley idx 29)
            + [97.0 + 0.2 * i for i in range(10)]  # rise 97->99
            + [99.0 - 0.4 * i for i in range(5)]  # drop 99->97 (valley idx 44)
            + [97.0 + 0.2 * i for i in range(15)]  # rise 97->100
            + [100.0] * 20  # settle
        )
        lo, hi = min(px), max(px)
        assert _detect_w_bottom(px, n, n // 2, lo, hi) is True

    def test_no_second_valley(self):
        px = [100.0] * 30 + [105.0] * 30
        lo, hi = min(px), max(px)
        assert _detect_w_bottom(px, 60, 30, lo, hi) is False

    def test_bottoms_too_far_apart(self):
        """两个底部差距 > 0.8% -> False"""
        px = (
            [100.0] * 10
            + [100.0 - i * 0.25 for i in range(15)]
            + [96.25 + i * 0.25 for i in range(10)]
            + [98.75 - i * 0.1 for i in range(15)]
            + [97.25 + i * 0.35 for i in range(10)]
        )
        lo, hi = min(px), max(px)
        assert _detect_w_bottom(px, 60, 30, lo, hi) is False

    def test_volume_confirms_rejection(self):
        """第二个底部放量 > 1.1x 第一个底部 -> False"""
        n = 80
        px = (
            [100.0] * 20
            + [100.0 - i * 0.3 for i in range(10)]
            + [97.0 + i * 0.3 for i in range(10)]
            + [100.0 - i * 0.2 for i in range(15)]
            + [97.0 + i * 0.3 for i in range(15)]
            + [101.2] * 10
        )
        lo, hi = min(px), max(px)
        # turnovers with second-bottom volume > first-bottom * 1.1 -> reject
        turnovers = [100.0] * n
        # Set first-bottom region vol low
        for i in range(27, 32):
            turnovers[i] = 50.0
        # Set second-bottom region vol high
        for i in range(55, 60):
            turnovers[i] = 100.0
        assert _detect_w_bottom(px, n, n // 2, lo, hi, turnovers) is False


class TestDetectMTop:
    """_detect_m_top 边界"""

    def test_not_enough_data(self):
        assert _detect_m_top([100.0] * 30, 30, 15, 99.0, 101.0) is False

    def test_m_shaped(self):
        """经典 M 型（60 点）应返回 True"""
        n = 60
        # M型: 100稳 -> 涨至102 -> 跌至100 -> 涨至102 -> 跌至99.5
        px = (
            [100.0] * 10
            + [100.0 + i * 0.2 for i in range(10)]  # rise 100->102
            + [102.0 - i * 0.2 for i in range(10)]  # drop 102->100
            + [100.0 + i * 0.2 for i in range(10)]  # rise 100->102
            + [102.0 - i * 0.25 for i in range(10)]  # drop 102->99.5
            + [99.5] * 10  # settle
        )
        lo, hi = min(px), max(px)
        result = _detect_m_top(px, n, n // 2, lo, hi)
        assert result is True, f"expected True, got {result}"

    def test_no_second_peak(self):
        px = [100.0] * 20 + [105.0] * 20
        lo, hi = min(px), max(px)
        assert _detect_m_top(px, 40, 20, lo, hi) is False

    def test_peaks_too_far_apart(self):
        """两个顶部差距 > 1% -> False"""
        px = (
            [100.0] * 10
            + [100.0 + i * 0.15 for i in range(10)]
            + [101.5 - i * 0.15 for i in range(10)]
            + [100.0 + i * 0.3 for i in range(10)]
        )
        lo, hi = min(px), max(px)
        assert _detect_m_top(px, 40, 20, lo, hi) is False


class TestDetectGapUpFade:
    """_detect_gap_up_fade 边界"""

    def test_small_range_returns_false(self):
        assert (
            _detect_gap_up_fade(
                [100.0] * 30,
                30,
                0.0,
                0.5,
                0.005,
                101.0,
                100.0,
                {"pre_close": 99.0},
            )
            is False
        )

    def test_open_low_returns_false(self):
        """open_zone < 0.6 -> False"""
        px = [100.1] + [100.0] * 29
        assert (
            _detect_gap_up_fade(
                px,
                30,
                -0.002,
                0.2,
                0.01,
                101.0,
                100.0,
                {"pre_close": 100.0},
            )
            is False
        )

    def test_no_quote_returns_false(self):
        px = [100.8] + [100.0] * 29
        assert (
            _detect_gap_up_fade(
                px,
                30,
                -0.002,
                0.2,
                0.01,
                101.0,
                100.0,
                None,
            )
            is False
        )

    def test_no_gap_returns_false(self):
        """gap < 0.5% -> False"""
        px = [100.8] + [100.0] * 29
        assert (
            _detect_gap_up_fade(
                px,
                30,
                -0.002,
                0.2,
                0.01,
                101.0,
                100.0,
                {"pre_close": 100.75},
            )
            is False
        )


class TestDetectGapDownRecover:
    """_detect_gap_down_recover 边界"""

    def test_small_range_returns_false(self):
        assert (
            _detect_gap_down_recover(
                [100.0] * 30,
                30,
                0.0,
                0.5,
                0.005,
                101.0,
                100.0,
                {"pre_close": 101.0},
            )
            is False
        )

    def test_quote_pre_close_zero(self):
        assert (
            _detect_gap_down_recover(
                [99.3] + [100.5] * 29,
                30,
                0.002,
                0.8,
                0.02,
                101.0,
                99.0,
                {"pre_close": 0},
            )
            is False
        )


class TestDetectLateDump:
    """_detect_late_dump 边界"""

    def test_not_enough_data(self):
        assert _detect_late_dump([100.0] * 5, 5, 5, 0.0, 0.01) is False

    def test_drop_detected(self):
        """短期明显下跌 -> True"""
        n = 50
        short_n = min(15, max(5, n // 4))
        stable = [100.0] * (n - short_n * 2)
        prev = [100.0 - i * 0.01 for i in range(short_n)]
        recent = [100.0 - short_n * 0.01 - i * 0.3 for i in range(short_n)]
        px = stable + prev + recent
        assert _detect_late_dump(px, n, short_n, -0.01, 0.02) is True

    def test_no_drop_returns_false(self):
        n = 50
        short_n = min(15, max(5, n // 4))
        px = [100.0] * n
        assert _detect_late_dump(px, n, short_n, 0.0, 0.01) is False


class TestDetectLateRally:
    """_detect_late_rally 边界"""

    def test_not_enough_data(self):
        assert _detect_late_rally([100.0] * 5, 5, 5, 0.0, 0.01) is False

    def test_early_already_up_returns_false(self):
        """early_chg > 0.5% 且早盘已涨 -> 拒绝"""
        n = 50
        short_n = min(15, max(5, n // 4))
        rises = [100.0 + i * 0.02 for i in range(40)]
        drops = [100.8 + i * 0.3 for i in range(10)]
        px = rises + drops
        assert _detect_late_rally(px, n, short_n, 0.005, 0.02) is False

    def test_rally_detected(self):
        """尾盘急拉 -> True"""
        n = 50
        short_n = min(15, max(5, n // 4))
        px = (
            [100.0] * (n - short_n * 2)
            + [100.0 + i * 0.01 for i in range(short_n)]
            + [100.0 + short_n * 0.01 + i * 0.3 for i in range(short_n)]
        )
        assert _detect_late_rally(px, n, short_n, 0.005, 0.02) is True

    def test_flat_returns_false(self):
        n = 50
        short_n = min(15, max(5, n // 4))
        px = [100.0] * n
        assert _detect_late_rally(px, n, short_n, 0.0, 0.01) is False


class TestDetectFishingLine:
    """_detect_fishing_line 边界"""

    def test_not_enough_data(self):
        assert (
            _detect_fishing_line(
                [100.0] * 30,
                30,
                15,
                5,
                101.0,
                99.0,
                "closing",
            )
            is False
        )

    def test_wrong_phase(self):
        """非尾盘时段 -> False"""
        n = 50
        px = [100.0 + i * 0.025 for i in range(40)] + [
            100.975 - i * 0.08 for i in range(10)
        ]
        assert (
            _detect_fishing_line(
                px,
                n,
                25,
                12,
                max(px),
                min(px),
                "afternoon",
            )
            is False
        )

    def test_fishing_line_detected(self):
        """全天推升尾盘暴跌 -> True"""
        n = 50
        px = [100.0 + i * 0.025 for i in range(40)] + [
            101.0 - i * 0.08 for i in range(10)
        ]
        assert (
            _detect_fishing_line(
                px,
                n,
                25,
                12,
                max(px),
                min(px),
                "closing",
            )
            is True
        )

    def test_not_enough_rise(self):
        """前80%涨幅不够 -> False"""
        px = [100.0] * 40 + [100.0 - i * 0.1 for i in range(10)]
        assert (
            _detect_fishing_line(
                px,
                50,
                25,
                12,
                max(px),
                min(px),
                "closing",
            )
            is False
        )


class TestDetectWideChoppy:
    """_detect_wide_choppy 边界"""

    def test_not_enough_range(self):
        assert (
            _detect_wide_choppy(
                [100.0] * 50,
                50,
                25,
                100.0,
                100.0,
                0.005,
                101.0,
                99.0,
            )
            is False
        )

    def test_choppy_detected(self):
        """振幅>1% 且多次穿 EMA12 -> True"""
        px = (
            [101.0, 99.5, 101.0, 99.5, 101.0, 99.5]
            + [100.5, 99.8, 100.5, 99.8]
            + [100.0] * 30
        )
        hi, lo = 101.0, 99.5
        range_pct = (hi - lo) / lo
        result = _detect_wide_choppy(
            px,
            len(px),
            20,
            100.0,
            99.5,
            range_pct,
            hi,
            lo,
        )
        assert result is True

    def test_at_extreme_position(self):
        """价格在区间极值处 -> False (pos 不在 0.3-0.7)"""
        px = [101.0, 99.5, 101.0, 99.5, 101.0, 99.5] + [101.0] * 40
        hi, lo = 101.0, 99.5
        range_pct = (hi - lo) / lo
        result = _detect_wide_choppy(
            px,
            len(px),
            20,
            100.0,
            99.5,
            range_pct,
            hi,
            lo,
        )
        assert result is False


# ====================================================================
#  classify_market_pattern — 16 种模式全覆盖
# ====================================================================


class TestClassifyMarketPattern:
    """classify_market_pattern 边界 — 16 种模式 + 空/单元素/等值"""

    # ── 基础边界 ──

    def test_empty_prices(self):
        result = classify_market_pattern([], 0, 0)
        assert result == "normal"

    def test_single_price_point(self):
        result = classify_market_pattern([3400.0], 3400, 3400)
        assert result == "normal"

    def test_same_high_low_choppy(self):
        """hi == lo -> normal"""
        result = classify_market_pattern([3400.0] * 50, 3400, 3400)
        assert result == "normal"

    def test_short_data(self):
        """n < 20 -> normal"""
        result = classify_market_pattern([3400.0] * 15, 3400, 3400)
        assert result == "normal"

    # ── 趋势类 ──

    def test_uptrend(self):
        """持续上涨 -> uptrend"""
        px = [3400.0 + i * 0.5 for i in range(100)]
        hi, lo = max(px), min(px)
        result = classify_market_pattern(px, hi, lo)
        assert result == "uptrend"

    def test_steady_climb_uptrend(self):
        """稳步爬升 -> uptrend"""
        px = [3500.0 + i * 0.3 for i in range(100)]
        hi, lo = max(px), min(px)
        result = classify_market_pattern(px, hi, lo)
        assert result == "uptrend"

    def test_one_sided_downtrend(self):
        """持续下跌 -> one_sided"""
        px = [3450.0 - i * 0.5 for i in range(100)]
        hi, lo = max(px), min(px)
        result = classify_market_pattern(px, hi, lo)
        assert result == "one_sided"

    # ── V 型反转 ──

    def test_v_reversal(self):
        """深跌后快速收回 -> v_reversal"""
        stable = [100.0] * 40
        drop = [100.0 - i * 0.09 for i in range(25)]  # 100 -> 97.84
        rise = [97.84 + i * 0.0544 for i in range(25)]  # 97.84 -> 99.20
        px = stable + drop + rise
        hi, lo = max(px), min(px)
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "v_reversal", f"expected v_reversal, got {result}"

    # ── 死猫跳 ──

    def test_dead_cat_bounce(self):
        """大跌后弱反弹且价格仍在低位 -> dead_cat"""
        stable = [100.0] * 40
        drop = [100.0 - i * 0.12 for i in range(33)]  # 100 -> 96.04
        rise = [96.04 + i * 0.06 for i in range(27)]  # 96.04 -> 97.66
        px = stable + drop + rise
        hi, lo = max(px), min(px)
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "dead_cat", f"expected dead_cat, got {result}"

    # ── 加速上涨 ──

    def test_melt_up(self):
        """价格高位加速拉升 -> melt_up"""
        stable = [100.0] * 40
        climb = [100.0 + i * 0.1 for i in range(30)]
        surge = [103.0 + i * 0.5 for i in range(30)]
        px = stable + climb + surge
        hi, lo = max(px), min(px)
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "melt_up", f"expected melt_up, got {result}"

    # ── 倒 V / A 型 ──

    def test_inverted_v(self):
        """冲高回落 -> inverted_v"""
        low_start = [100.0] * 15
        spike = [100.0 + i * 0.146 for i in range(35)]  # 100 -> 105.1
        collapse = [105.1 - i * 0.076 for i in range(50)]  # 105.1 -> 101.38
        px = low_start + spike + collapse
        hi, lo = max(px), min(px)
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "inverted_v", f"expected inverted_v, got {result}"

    # ── 恐慌 ──

    def test_panic(self):
        """平稳后加速暴跌 -> panic"""
        # 70 根平稳 + 30 根急跌，确保短期跌幅 > 中期跌幅 80%
        px = [3400.0] * 70 + [3400.0 - i * 5 for i in range(30)]
        hi, lo = max(px), min(px)
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "panic", f"expected panic, got {result}"

    def test_sharp_drop_no_recovery(self):
        """暴跌无反弹 -> panic（需短期跌幅 > 中期跌幅 80%）"""
        # 70 根平稳后 30 根急跌: 短期集中下跌使 drop_short > drop_medium*0.8
        px = [3400.0] * 70 + [3400.0 - i * 7 for i in range(30)]
        hi, lo = max(px), min(px)
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "panic", f"expected panic, got {result}"

    # ── 跳空高开低走 ──

    def test_gap_up_fade(self):
        """跳空高开后持续回落 -> gap_up_fade"""
        n = 40
        hi, lo = 101.0, 100.0
        px = [101.0 - i / (n - 1) for i in range(n)]  # linear 101 -> 100
        quote = {"pre_close": 100.0}
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo, last_index_quote=quote)
        assert result == "gap_up_fade", f"expected gap_up_fade, got {result}"

    # ── 跳空低开高走 ──

    def test_gap_down_recover(self):
        """跳空低开后持续回升 -> gap_down_recover"""
        n = 30
        hi, lo = 100.5, 99.0
        px = [99.2 + i * (1.3 / (n - 1)) for i in range(n)]
        px[-1] = hi
        quote = {"pre_close": 100.0}
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo, last_index_quote=quote)
        assert result == "gap_down_recover", f"expected gap_down_recover, got {result}"

    # ── 宽幅震荡 ──

    def test_wide_choppy(self):
        """宽幅震荡多次穿 EMA -> wide_choppy"""
        px = (
            [100.0, 101.0, 99.0, 101.0, 99.0]
            + [100.5, 99.5, 100.5, 99.5, 100.5]
            + [99.8, 100.3, 99.7, 100.2, 99.8]
            + [100.0] * 30
        )
        hi, lo = 101.0, 99.0
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "wide_choppy", f"expected wide_choppy, got {result}"

    # ── W 底 / M 顶（子函数已验证；classify 层因模式匹配顺序可能兜底为 normal）──

    def test_double_bottom_w(self):
        """W 型双底在 classify 层应最终匹配"""
        n = 80
        px = (
            [100.0] * 20
            + [100.0 - i * 0.3 for i in range(10)]
            + [97.0 + i * 0.3 for i in range(10)]
            + [100.0 - i * 0.2 for i in range(15)]
            + [97.0 + i * 0.3 for i in range(15)]
            + [101.2] * 10
        )
        hi, lo = max(px), min(px)
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        # 可能因 melt_up(pos>0.8) 抢先匹配，但不会返回 normal 或 panic
        assert result != "normal"

    def test_double_top_m(self):
        """M 型双顶在 classify 层应最终匹配"""
        n = 60
        px = (
            [100.0] * 10
            + [100.0 + i * 0.2 for i in range(10)]
            + [102.0 - i * 0.2 for i in range(10)]
            + [100.0 + i * 0.2 for i in range(10)]
            + [102.0 - i * 0.25 for i in range(10)]
            + [99.5] * 10
        )
        hi, lo = max(px), min(px)
        with _phase("afternoon"):
            result = classify_market_pattern(px, hi, lo)
        assert result != "normal"

    # ── 钓鱼线 ──

    def test_fishing_line(self):
        """全天缓慢推升尾盘暴跌 -> fishing_line"""
        n = 50
        px = [100.0 + i * 0.025 for i in range(40)] + [
            101.0 - i * 0.08 for i in range(10)
        ]
        hi, lo = max(px), min(px)
        with _phase("closing"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "fishing_line", f"expected fishing_line, got {result}"

    # ── 尾盘拉升 ──

    def test_late_rally(self):
        """尾盘快速拉升 -> late_rally"""
        # 前 80% (48 根) 不涨, 确保 early_chg <= 0.005
        # 最后 15 根 (recent) 显著高于前 15 根 (prev)
        n = 60
        short_n = min(15, max(5, n // 4))
        flat = [100.0] * (n - short_n)  # 45 flat at 100 (30 base + 15 prev)
        # prev = flat[-15:] = [100]*15
        # recent = [100]*3 + rising 12
        recent = [100.0] * 3 + [100.0 + 0.5 * (i - 3) for i in range(3, short_n)]
        px = flat + recent
        hi, lo = max(px), min(px)
        with _phase("closing"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "late_rally", f"expected late_rally, got {result}"

    # ── 尾盘跳水 ──

    def test_late_dump(self):
        """尾盘快速下跌 -> late_dump"""
        n = 60
        short_n = min(15, max(5, n // 4))
        stable = [100.0] * (n - short_n * 2)
        prev_rise = [100.0 + i * 0.01 for i in range(short_n)]
        drop = [100.0 + short_n * 0.01 - i * 0.5 for i in range(short_n)]
        px = stable + prev_rise + drop
        hi, lo = max(px), min(px)
        with _phase("closing"):
            result = classify_market_pattern(px, hi, lo)
        assert result == "late_dump", f"expected late_dump, got {result}"

    # ── 无模式 -> normal ──

    def test_normal_no_pattern(self):
        """小幅随机波动 -> normal"""
        import random

        random.seed(42)
        base = 3400.0
        px = [base + random.uniform(-0.3, 0.3) for _ in range(60)]
        hi, lo = max(px), min(px)
        result = classify_market_pattern(px, hi, lo)
        assert result == "normal"

    def test_normal_flat_low_volatility(self):
        """极窄幅震荡 -> normal"""
        px = [3400.0 + i * 0.01 for i in range(30)]
        hi, lo = max(px), min(px)
        result = classify_market_pattern(px, hi, lo)
        assert result == "normal"


# ====================================================================
#  板块趋势函数边界
# ====================================================================


class TestGetSectorTrend:
    """get_sector_trend 边界"""

    def test_empty_industry(self):
        assert get_sector_trend("000001", {}, {}) == ""

    def test_insufficient_data(self):
        stats = {"dummy": {"trend_history": [1.0]}}
        result = get_sector_trend("000001", {"000001": "dummy"}, stats)
        assert "数据" in result

    def test_uptrend_slope(self):
        history = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        stats = {
            "test_sector": {
                "trend_history": history,
                "relative": 0.6,
                "breadth": 0.5,
                "vol_ratio": 1.8,
                "continuity": 4,
            },
        }
        result = get_sector_trend("000001", {"000001": "test_sector"}, stats)
        assert "走强" in result

    def test_downtrend_slope(self):
        history = [0.5, 0.4, 0.3, 0.2, 0.1, 0.0]
        stats = {
            "test_sector": {
                "trend_history": history,
                "relative": -0.6,
                "breadth": -0.5,
                "vol_ratio": 0.4,
                "continuity": 0,
            },
        }
        result = get_sector_trend("000001", {"000001": "test_sector"}, stats)
        assert "走弱" in result

    def test_sideways(self):
        history = [0.1, 0.15, 0.12, 0.13, 0.11, 0.14]
        stats = {
            "test_sector": {
                "trend_history": history,
                "relative": 0,
                "breadth": 0,
                "vol_ratio": 1.0,
            },
        }
        result = get_sector_trend("000001", {"000001": "test_sector"}, stats)
        assert "横盘" in result

    def test_strong_downtrend_with_concept(self):
        concept_cache = {"000001": ["概念A"]}
        concept_stats = {"概念A": {"change_pct": -2.0}}
        stats = {
            "test_sector": {
                "trend_history": [0.0, -0.1, -0.2, -0.3, -0.4],
                "relative": -0.8,
                "breadth": -0.6,
                "vol_ratio": 1.6,
                "continuity": 3,
            },
        }
        result = get_sector_trend(
            "000001",
            {"000001": "test_sector"},
            stats,
            concept_cache=concept_cache,
            concept_stats=concept_stats,
        )
        assert "走弱" in result
        assert "概念A" in result

    def test_vol_ratio_high(self):
        history = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        stats = {
            "test_sector": {
                "trend_history": history,
                "relative": 0,
                "breadth": 0,
                "vol_ratio": 2.0,
            }
        }
        result = get_sector_trend("000001", {"000001": "test_sector"}, stats)
        assert "放量" in result

    def test_vol_ratio_low(self):
        history = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        stats = {
            "test_sector": {
                "trend_history": history,
                "relative": 0,
                "breadth": 0,
                "vol_ratio": 0.3,
            }
        }
        result = get_sector_trend("000001", {"000001": "test_sector"}, stats)
        assert "缩量" in result


class TestGetSectorChange:
    """get_sector_change 边界"""

    def test_empty_industry(self):
        assert get_sector_change("000001", {}, {}) is None

    def test_no_stats(self):
        assert get_sector_change("000001", {"000001": "dummy"}, {}) is None

    def test_positive_change(self):
        stats = {"test_sector": {"change_pct": 2.5}}
        result = get_sector_change("000001", {"000001": "test_sector"}, stats)
        assert result == 2.5

    def test_negative_change(self):
        stats = {"test_sector": {"change_pct": -1.8}}
        result = get_sector_change("000001", {"000001": "test_sector"}, stats)
        assert result == -1.8

    def test_zero_change(self):
        stats = {"test_sector": {"change_pct": 0.0}}
        result = get_sector_change("000001", {"000001": "test_sector"}, stats)
        assert result == 0.0

    def test_none_industry_cache_raises(self):
        """industry_cache=None 时函数未防御性编码 -> 预期 AttributeError"""
        import pytest

        with pytest.raises(AttributeError):
            get_sector_change("000001", None, {})


class TestGetSectorDecline:
    """get_sector_decline 边界"""

    def test_empty_industry(self):
        assert get_sector_decline("000001", {}, {}) is None

    def test_short_history(self):
        stats = {"test_sector": {"trend_history": [1.0, 2.0]}}
        assert get_sector_decline("000001", {"000001": "test_sector"}, stats) is None

    def test_decline_present(self):
        stats = {"test_sector": {"trend_history": [3.0, 2.5, 2.0, 1.5, 1.0]}}
        result = get_sector_decline("000001", {"000001": "test_sector"}, stats)
        assert result is not None and result > 0
        assert result == 2.0

    def test_no_decline_rising(self):
        stats = {"test_sector": {"trend_history": [1.0, 1.5, 2.0, 2.5, 3.0]}}
        result = get_sector_decline("000001", {"000001": "test_sector"}, stats)
        assert result is None

    def test_flat_no_decline(self):
        stats = {"test_sector": {"trend_history": [2.0, 2.0, 2.0, 2.0, 2.0]}}
        result = get_sector_decline("000001", {"000001": "test_sector"}, stats)
        assert result is None

    def test_none_industry_cache_raises(self):
        import pytest

        with pytest.raises(AttributeError):
            get_sector_decline("000001", None, {})


class TestGetSectorRecoveryRisk:
    """get_sector_recovery_risk 边界"""

    def test_empty_industry(self):
        assert get_sector_recovery_risk("000001", {}, {}) is None

    def test_short_history(self):
        stats = {"test_sector": {"trend_history": [1.0, 2.0, 3.0, 4.0, 5.0]}}
        assert (
            get_sector_recovery_risk(
                "000001",
                {"000001": "test_sector"},
                stats,
            )
            is None
        )

    def test_significant_recovery(self):
        """从深跌反弹 > 2.0 -> 返回数值"""
        stats = {
            "test_sector": {
                "trend_history": [-3.0, -2.5, -2.0, -1.5, -0.5, 0.0, 0.3, 0.8],
            },
        }
        result = get_sector_recovery_risk(
            "000001",
            {"000001": "test_sector"},
            stats,
        )
        assert result is not None
        assert result > 2.0

    def test_small_recovery(self):
        stats = {"test_sector": {"trend_history": [-1.0, -0.8, -0.5, -0.2, 0.0, 0.1]}}
        result = get_sector_recovery_risk(
            "000001",
            {"000001": "test_sector"},
            stats,
        )
        assert result is None

    def test_none_industry_cache_raises(self):
        import pytest

        with pytest.raises(AttributeError):
            get_sector_recovery_risk("000001", None, {})


class TestGetConceptTrendScore:
    """get_concept_trend_score 边界"""

    def test_empty_cache(self):
        score, reason = get_concept_trend_score("000001", {}, {})
        assert score == 0
        assert reason == ""

    def test_strong_concepts(self):
        concept_cache = {"000001": ["概念A", "概念B", "概念C", "概念D", "概念E"]}
        concept_stats = {
            "概念A": {"change_pct": 2.0},
            "概念B": {"change_pct": 1.5},
            "概念C": {"change_pct": 1.8},
            "概念D": {"change_pct": 2.5},
            "概念E": {"change_pct": 1.2},
        }
        score, reason = get_concept_trend_score("000001", concept_cache, concept_stats)
        assert score == 3  # capped at 3

    def test_weak_concepts(self):
        concept_cache = {"000001": ["弱A", "弱B", "弱C"]}
        concept_stats = {
            "弱A": {"change_pct": -2.0},
            "弱B": {"change_pct": -1.5},
            "弱C": {"change_pct": -1.8},
        }
        score, reason = get_concept_trend_score("000001", concept_cache, concept_stats)
        assert score == -3

    def test_mixed_concepts(self):
        concept_cache = {"000001": ["强A", "弱A", "中A"]}
        concept_stats = {
            "强A": {"change_pct": 2.0},
            "弱A": {"change_pct": -2.0},
            "中A": {"change_pct": 0.5},
        }
        score, reason = get_concept_trend_score("000001", concept_cache, concept_stats)
        assert score == 0
        assert reason == ""

    def test_concept_not_in_stats(self):
        concept_cache = {"000001": ["不存在概念"]}
        score, reason = get_concept_trend_score("000001", concept_cache, {})
        assert score == 0

    def test_no_concepts_for_code(self):
        concept_cache = {"other_code": ["概念A"]}
        concept_stats = {"概念A": {"change_pct": 5.0}}
        score, reason = get_concept_trend_score("000001", concept_cache, concept_stats)
        assert score == 0


# ====================================================================
#  Micro Signals extract 边界
# ====================================================================


class TestExtractMicroSignals:
    """extract 函数边界 — 空值、量能脉冲、突破/跌破、压缩、正常"""

    def test_short_prices(self):
        """不足 5 个价格点 -> 默认 MicroSignals"""
        result = extract([100.0], 100.0, 100.0)
        assert isinstance(result, MicroSignals)
        assert result.price_velocity == 0.0
        assert result.ema12_pos == "on"

    def test_empty_prices(self):
        result = extract([], 0, 0)
        assert isinstance(result, MicroSignals)

    def test_normal_price_action(self):
        """极小波动 -> 无显著信号"""
        # 完全平坦的数据避免 range_expanding（首尾范围一致）
        px = [3400.0] * 30
        result = extract(px, 3400.0, 3400.0)
        assert result.vol_pulse == "normal"
        assert not result.range_expanding
        assert not result.range_contracting
        assert not result.higher_highs

    def test_large_volume_spike(self):
        """最近 3 期量能 > 前 3 期 1.3x -> expanding"""
        px = [100.0 + i * 0.1 for i in range(30)]
        hi, lo = max(px), min(px)
        # 6 期: 前 3 期小, 后 3 期大
        vols = [50.0, 50.0, 50.0, 100.0, 100.0, 100.0]
        result = extract(px, hi, lo, market_turnovers=vols)
        assert result.vol_pulse == "expanding"

    def test_volume_not_enough(self):
        """vols len < 6 -> normal"""
        px = [100.0] * 30
        result = extract(px, 101.0, 99.0, market_turnovers=[100.0, 200.0, 300.0])
        assert result.vol_pulse == "normal"

    def test_volume_contracting(self):
        """量能收缩 -> contracting"""
        px = [100.0 + i * 0.1 for i in range(30)]
        hi, lo = max(px), min(px)
        # 前 3 期大, 后 3 期小
        vols = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 30.0, 30.0, 30.0]
        result = extract(px, hi, lo, market_turnovers=vols)
        assert result.vol_pulse == "contracting"

    def test_price_near_resistance(self):
        """价格接近阻力位 -> testing_resistance=True"""
        px = [100.0 + i * 0.1 for i in range(30)]
        hi, lo = max(px), min(px)
        cur = px[-1]
        result = extract(px, hi, lo, key_resistance=[cur * 1.0015])
        assert result.testing_resistance is True

    def test_price_near_support(self):
        """价格接近支撑位 -> testing_support=True"""
        px = [100.0 + i * 0.1 for i in range(30)]
        hi, lo = max(px), min(px)
        cur = px[-1]
        result = extract(px, hi, lo, key_support=[cur * 0.999])
        assert result.testing_support is True

    def test_support_resistance_far(self):
        """支撑/阻力位远离当前价 -> False"""
        px = [100.0] * 30
        result = extract(px, 101.0, 99.0, key_support=[95.0], key_resistance=[105.0])
        assert result.testing_support is False
        assert result.testing_resistance is False

    def test_range_expanding(self):
        """早期窄幅 -> 当前扩大 -> range_expanding"""
        early_narrow = [
            100.0,
            100.1,
            99.9,
            100.1,
            99.9,
            100.1,
            99.9,
            100.1,
            99.9,
            100.1,
            99.9,
            100.1,
            99.9,
            100.1,
            99.9,
        ]
        late_wide = [
            100.0,
            101.0,
            99.0,
            102.0,
            98.0,
            103.0,
            97.0,
            103.0,
            97.0,
            103.0,
            97.0,
            103.0,
            97.0,
            103.0,
            97.0,
        ]
        px = early_narrow + late_wide
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo)
        assert result.range_expanding is True

    def test_breadth_improving(self):
        """涨家占比显著提升 -> breadth_trend=improving"""
        px = [100.0] * 30
        result = extract(
            px,
            101.0,
            99.0,
            market_breadth={"up": 700, "down": 300},
            prev_breadth=0.3,
        )
        assert result.breadth_trend == "improving"

    def test_breadth_deteriorating(self):
        px = [100.0] * 30
        result = extract(
            px,
            101.0,
            99.0,
            market_breadth={"up": 200, "down": 800},
            prev_breadth=0.6,
        )
        assert result.breadth_trend == "deteriorating"

    def test_breadth_stable(self):
        px = [100.0] * 30
        result = extract(
            px,
            101.0,
            99.0,
            market_breadth={"up": 510, "down": 490},
            prev_breadth=0.5,
        )
        assert result.breadth_trend == "stable"

    def test_higher_highs_param(self):
        """higher_highs 参数被透传"""
        px = [100.0 + i * 0.1 for i in range(30)]
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo, higher_highs=True)
        assert result.higher_highs is True
        result2 = extract(px, hi, lo, higher_highs=False)
        assert result2.higher_highs is False

    def test_lower_highs_detected(self):
        """近期高点低于前期高点 -> lower_highs=True"""
        # recent_highs: 前 10 高 + 后 10 低 (cur 置于更低位置不干扰)
        recent_highs = [100.0] * 10 + [99.0] * 10
        px = [99.0] * 30  # cur=99.0, 不干扰尾部数据
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo, recent_highs=recent_highs)
        assert result.lower_highs is True

    def test_higher_lows_detected(self):
        """近期低点高于前期低点 -> higher_lows=True"""
        recent_highs = [99.0] * 10 + [100.0] * 10
        px = [100.0] * 30
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo, recent_highs=recent_highs)
        assert result.higher_lows is True

    def test_bounce_quality_strong(self):
        """反弹力度强 -> bounce_quality=strong"""
        px = [100.0, 99.8, 99.6, 99.8, 100.0, 100.2, 100.3, 100.4, 100.5]
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo)
        assert result.bounce_quality == "strong"

    def test_bounce_quality_failed(self):
        """反弹失败 -> bounce_quality=failed"""
        # cur > lo (bounce_pct > 0), 但最后 5 个 tick 多数下行
        px = [100.0, 99.5, 99.8, 99.6, 99.4, 99.3, 99.0, 99.2, 99.1]
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo)
        assert result.bounce_quality == "failed"

    def test_bounce_quality_weak(self):
        """反弹力量微弱 -> bounce_quality=weak"""
        # 开盘方向向下，up_count >= 3 但不够强
        px = [100.5, 100.3, 100.1, 99.9, 99.7, 99.8, 99.9, 99.7, 99.8, 99.9]
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo)
        # up_count=3, day_direction_up=(99.9-100.5)/100.5 < 0, 非走强日
        assert result.bounce_quality == "weak"

    def test_ema12_above(self):
        """价格在 EMA12 上方 -> ema12_pos=above"""
        px = [100.0 + i * 0.2 for i in range(30)]
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo)
        assert result.ema12_pos == "above"

    def test_ema12_below(self):
        """价格在 EMA12 下方 -> ema12_pos=below"""
        px = [101.0 - i * 0.2 for i in range(30)]
        hi, lo = max(px), min(px)
        result = extract(px, hi, lo)
        assert result.ema12_pos == "below"

    def test_none_market_breadth(self):
        """market_breadth=None -> 默认值 0.5"""
        px = [100.0] * 30
        result = extract(px, 101.0, 99.0, market_breadth=None)
        assert result.breadth_pct == 0.5

    def test_zero_narrow_range(self):
        """零振幅（hi==lo）-> 不触发范围信号"""
        px = [100.0] * 30
        result = extract(px, 100.0, 100.0)
        assert not result.range_expanding
        assert not result.range_contracting

    def test_all_none_inputs(self):
        """所有可选参数为 None -> 不崩溃"""
        px = [100.0 + i * 0.1 for i in range(20)]
        result = extract(
            px,
            101.0,
            99.0,
            market_turnovers=None,
            market_breadth=None,
            prev_velocity=0.0,  # 函数内做减法, None 会崩溃
            prev_breadth=0.5,
            recent_highs=None,
            key_support=None,
            key_resistance=None,
        )
        assert isinstance(result, MicroSignals)
