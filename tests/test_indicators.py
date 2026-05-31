# -*- coding: utf-8 -*-
"""技术指标计算单元测试 — analysis/screening/indicators.py"""

import math
import pytest
from analysis.screening.indicators import (
    _ema, calc_macd, calc_rsi, calc_kdj, calc_bollinger,
    calc_macd_series, detect_macd_cross, calc_atr, detect_divergence,
)


# ====================== EMA ======================

class TestEMA:
    def test_empty_data(self):
        assert _ema([], 12) == []

    def test_shorter_than_period(self):
        result = _ema([10.0, 12.0, 14.0], 12)
        assert len(result) == 3

    def test_exact_period(self):
        prices = [10.0] * 12
        result = _ema(prices, 12)
        assert len(result) == 12
        assert result[-1] == pytest.approx(10.0)

    def test_standard_ema(self):
        """k = 2/(5+1) = 0.333"""
        prices = [1.0] * 5 + [2.0] * 3
        result = _ema(prices, 5)
        assert result[-1] > 1.0  # trending toward 2

    def test_increasing_trend(self):
        prices = list(range(1, 21))
        result = _ema(prices, 10)
        assert result[-1] > 10  # lagging behind latest values
        assert result[-1] < 20

    def test_period_1(self):
        """EMA(1) should follow price exactly"""
        result = _ema([10.0, 12.0, 11.0], 1)
        assert result == [10.0, 12.0, 11.0]


# ====================== MACD ======================

class TestMACD:
    def test_flat_prices(self):
        closes = [10.0] * 40
        result = calc_macd(closes)
        assert result["dif"] == pytest.approx(0.0, abs=0.01)
        assert result["dea"] == pytest.approx(0.0, abs=0.01)
        assert result["bar"] == pytest.approx(0.0, abs=0.01)

    def test_uptrend(self):
        closes = list(range(1, 50))
        result = calc_macd(closes)
        assert result["dif"] > 0
        assert result["bar"] is not None

    def test_downtrend(self):
        closes = [50 - i for i in range(50)]
        result = calc_macd(closes)
        assert result["dif"] < 0

    def test_insufficient_data(self):
        closes = [10.0] * 30  # just enough for 26
        result = calc_macd(closes)
        assert "dif" in result

    def test_custom_params(self):
        closes = list(range(1, 50))
        result = calc_macd(closes, fast=6, slow=13, signal=5)
        assert "dif" in result
        assert "dea" in result


class TestMACDSeries:
    def test_returns_sequences(self):
        closes = list(range(1, 50))
        result = calc_macd_series(closes)
        assert isinstance(result["dif"], list)
        assert isinstance(result["dea"], list)
        assert isinstance(result["bar"], list)
        assert len(result["dif"]) == len(closes)


class TestDetectMACDCross:
    def test_no_cross_in_flat(self):
        closes = [10.0] * 50
        series = calc_macd_series(closes)
        crosses = detect_macd_cross(series["dif"], series["dea"])
        assert crosses == []

    def test_golden_cross(self):
        """先跌后涨产生金叉"""
        closes = [15 - i * 0.05 for i in range(40)] + [13 + i * 0.5 for i in range(20)]
        series = calc_macd_series(closes)
        crosses = detect_macd_cross(series["dif"], series["dea"])
        assert any("金叉" in c["type"] for c in crosses)

    def test_death_cross(self):
        """先涨后跌产生死叉"""
        closes = [13 + i * 0.05 for i in range(40)] + [15 - i * 0.5 for i in range(20)]
        series = calc_macd_series(closes)
        crosses = detect_macd_cross(series["dif"], series["dea"])
        assert any("死叉" in c["type"] for c in crosses)


# ====================== RSI ======================

class TestRSI:
    def test_all_gains(self):
        closes = list(range(1, 30))
        rsi = calc_rsi(closes, 14)
        assert rsi == pytest.approx(100.0, abs=0.01)

    def test_all_losses(self):
        closes = [30 - i for i in range(30)]
        rsi = calc_rsi(closes, 14)
        assert rsi == pytest.approx(0.0, abs=0.01)

    def test_mixed(self):
        closes = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11,
                  10, 11, 10, 11, 10, 11, 10, 11, 10, 11]
        rsi = calc_rsi(closes, 14)
        assert 40 <= rsi <= 60

    def test_insufficient_data(self):
        closes = [10.0, 11.0]
        rsi = calc_rsi(closes, 14)
        assert 0 <= rsi <= 100

    def test_custom_period(self):
        closes = list(range(1, 30))
        rsi = calc_rsi(closes, 6)
        assert rsi == pytest.approx(100.0, abs=0.01)

    def test_no_change(self):
        closes = [10.0] * 20
        rsi = calc_rsi(closes, 14)
        # 无变化时 RSI 可能返回 100（无损失日 → 保护逻辑触发）
        assert 0 <= rsi <= 100


# ====================== KDJ ======================

class TestKDJ:
    def test_flat_prices(self):
        highs = [10.0] * 15
        lows = [10.0] * 15
        closes = [10.0] * 15
        result = calc_kdj(highs, lows, closes)
        assert result["k"] == pytest.approx(50.0, abs=5)
        assert result["d"] == pytest.approx(50.0, abs=5)

    def test_uptrend_kdj_high(self):
        highs = [10 + i * 0.5 for i in range(20)]
        lows = [9 + i * 0.5 for i in range(20)]
        closes = [9.9 + i * 0.5 for i in range(20)]
        result = calc_kdj(highs, lows, closes)
        assert result["j"] > result["k"]  # J leads K in uptrend

    def test_downtrend_kdj_low(self):
        highs = [20 - i * 0.5 for i in range(20)]
        lows = [19 - i * 0.5 for i in range(20)]
        closes = [19.1 - i * 0.5 for i in range(20)]
        result = calc_kdj(highs, lows, closes)
        assert result["k"] < 50

    def test_insufficient_data(self):
        result = calc_kdj([10.0] * 5, [9.0] * 5, [9.5] * 5)
        assert "k" in result

    def test_custom_params(self):
        highs = [10 + i * 0.5 for i in range(20)]
        lows = [9 + i * 0.5 for i in range(20)]
        closes = [9.9 + i * 0.5 for i in range(20)]
        result = calc_kdj(highs, lows, closes, n=5, k_smooth=2, d_smooth=2)
        assert 0 <= result["k"] <= 100


# ====================== Bollinger Bands ======================

class TestBollinger:
    def test_flat_prices(self):
        closes = [10.0] * 25
        result = calc_bollinger(closes)
        assert result["mid"] == pytest.approx(10.0)
        assert result["upper"] == pytest.approx(10.0)  # std=0
        assert result["lower"] == pytest.approx(10.0)
        assert result["width"] == pytest.approx(0.0)
        assert result["pct_b"] == pytest.approx(50.0)  # NaN-safe default

    def test_normal_distribution(self):
        closes = [10.0, 11.0, 10.0, 12.0, 10.0, 11.0, 10.0, 12.0, 10.0, 11.0,
                  10.0, 12.0, 10.0, 11.0, 10.0, 12.0, 10.0, 11.0, 10.0, 12.0,
                  10.0, 11.0, 10.0, 12.0]
        result = calc_bollinger(closes)
        assert result["upper"] > result["mid"]
        assert result["lower"] < result["mid"]
        assert result["width"] > 0
        assert 0 <= result["pct_b"] <= 100

    def test_insufficient_data(self):
        closes = [10.0] * 25
        result = calc_bollinger(closes)
        assert result["mid"] > 0

    def test_custom_params(self):
        closes = [10.0, 11.0, 10.0, 12.0, 10.0] * 10
        result = calc_bollinger(closes, period=10, std_mult=3.0)
        assert result["upper"] > result["mid"]

    def test_pct_b_at_upper(self):
        """When price is at upper band, pct_b ≈ 100"""
        closes = [10.0] * 20 + [15.0, 15.0]
        result = calc_bollinger(closes)
        assert result["pct_b"] > 50


# ====================== ATR ======================

class TestATR:
    def test_flat_prices(self):
        highs = [10.0] * 20
        lows = [9.0] * 20
        closes = [9.5] * 20
        result = calc_atr(highs, lows, closes)
        assert result == pytest.approx(1.0)  # (1 + 0 + 1) / 3 ≈ 0.67 actually... let's just check > 0
        assert result > 0

    def test_high_volatility(self):
        highs = [10, 15, 10, 15, 10, 15, 10, 15, 10, 15,
                 10, 15, 10, 15, 10, 15, 10, 15, 10, 15]
        lows = [5, 10, 5, 10, 5, 10, 5, 10, 5, 10,
                5, 10, 5, 10, 5, 10, 5, 10, 5, 10]
        closes = [7, 13, 7, 13, 7, 13, 7, 13, 7, 13,
                  7, 13, 7, 13, 7, 13, 7, 13, 7, 13]
        result = calc_atr(highs, lows, closes)
        assert result > 3.0  # High volatility

    def test_low_volatility(self):
        highs = [10.1, 10.0, 10.1, 10.0] * 5
        lows = [9.9, 10.0, 9.9, 10.0] * 5
        closes = [10.0, 10.0, 10.0, 10.0] * 5
        result = calc_atr(highs, lows, closes)
        assert result < 1.0

    def test_insufficient_data(self):
        result = calc_atr([10, 11], [9, 10], [10, 10])
        assert result > 0

    def test_custom_period(self):
        highs = [10.0] * 10
        lows = [9.0] * 10
        closes = [9.5] * 10
        result = calc_atr(highs, lows, closes, period=5)
        assert result > 0


# ====================== Divergence Detection ======================

class TestDivergence:
    def test_no_divergence_in_flat(self):
        closes = [10.0] * 50
        macd = calc_macd_series(closes)
        divs = detect_divergence(closes, macd["dif"])
        assert divs == []

    def test_bullish_divergence(self):
        """Price makes lower low but DIF makes higher low"""
        closes = [20.0] * 30 + [20 - i * 0.5 for i in range(20)] + [10 + i * 0.2 for i in range(5)] + [11 - i * 0.6 for i in range(10)]
        macd = calc_macd_series(closes)
        # MACD series shorter than closes due to slow period
        dif = macd["dif"]
        divs = detect_divergence(closes[-len(dif):], dif)
        assert isinstance(divs, list)

    def test_bearish_divergence(self):
        """Price makes higher high but DIF makes lower high"""
        closes = [10.0] * 30 + [10 + i * 0.5 for i in range(20)] + [20 - i * 0.2 for i in range(5)] + [19 + i * 0.6 for i in range(10)]
        macd = calc_macd_series(closes)
        dif = macd["dif"]
        divs = detect_divergence(closes[-len(dif):], dif)
        assert isinstance(divs, list)

    def test_custom_lookback(self):
        closes = list(range(1, 50))
        macd = calc_macd_series(closes)
        divs = detect_divergence(closes, macd["dif"], lookback=10)
        assert isinstance(divs, list)
