"""analysis/indicators.py 纯函数测试"""

from stock.indicators import (
    calc_atr,
    calc_bollinger,
    calc_kdj,
    calc_ma,
    calc_ma_angle,
    calc_macd,
    calc_macd_series,
    calc_rsi,
    detect_death_cross,
    detect_divergence,
    detect_golden_cross,
    detect_macd_cross,
)


class TestRSI:
    def test_normal(self):
        # 带波动的上涨：有涨有跌
        prices = [
            10.0,
            10.2,
            9.9,
            10.3,
            10.1,
            10.5,
            10.2,
            10.6,
            10.3,
            10.8,
            10.5,
            10.9,
            10.6,
            11.0,
            10.8,
            11.2,
        ]
        rsi = calc_rsi(prices, 14)
        assert 40 < rsi < 80

    def test_flat(self):
        # 全相等 → avg_loss=0 → RSI=100（无误：确实没有任何下跌）
        prices = [10.0] * 30
        rsi = calc_rsi(prices, 14)
        assert rsi == 100.0

    def test_short_data(self):
        assert calc_rsi([10.0, 10.1], 14) == 50.0


class TestMACD:
    def test_uptrend(self):
        prices = [10.0 + i * 0.1 for i in range(50)]
        result = calc_macd(prices)
        assert result["dif"] > result["dea"]
        assert result["bar"] > 0

    def test_downtrend(self):
        prices = [20.0 - i * 0.1 for i in range(50)]
        result = calc_macd(prices)
        assert result["dif"] < result["dea"]

    def test_series(self):
        prices = [10.0 + i * 0.1 for i in range(50)]
        series = calc_macd_series(prices)
        assert len(series["dif"]) == 50
        assert len(series["dea"]) == 50


class TestKDJ:
    def test_normal(self):
        prices = [10.0 + i * 0.1 for i in range(30)]
        highs = [p + 0.2 for p in prices]
        lows = [p - 0.2 for p in prices]
        result = calc_kdj(highs, lows, prices)
        assert 0 <= result["k"] <= 100
        assert result["j"] > result["k"]


class TestBollinger:
    def test_normal(self):
        prices = [10.0] * 15 + [10.5] * 10
        result = calc_bollinger(prices)
        assert result["upper"] > result["mid"] > result["lower"]
        assert 0 < result["width"] < 20


class TestATR:
    def test_volatile(self):
        highs = [10.0, 10.5, 10.2, 10.8, 10.3]
        lows = [9.5, 9.8, 9.7, 10.0, 9.9]
        closes = [9.8, 10.2, 10.0, 10.5, 10.1]
        atr = calc_atr(highs, lows, closes, 3)
        assert atr > 0


class TestDivergence:
    def test_no_divergence_on_trend(self):
        prices = [10.0 + i * 0.1 for i in range(60)]
        dif = [i * 0.01 for i in range(60)]
        result = detect_divergence(prices, dif)
        assert len(result) == 0


class TestMACDCross:
    def test_golden_cross(self):
        dif = [0.0, -0.1, 0.1, 0.3]
        dea = [0.1, 0.0, 0.05, 0.1]
        assert detect_golden_cross(dif, dea)

    def test_death_cross(self):
        dif = [0.3, 0.1, -0.1, -0.3]
        dea = [0.1, 0.15, 0.05, -0.05]
        assert detect_death_cross(dif, dea)

    def test_no_cross(self):
        dif = [0.3, 0.4, 0.5, 0.6]
        dea = [0.1, 0.2, 0.3, 0.4]
        crosses = detect_macd_cross(dif, dea, lookback=10)
        assert len(crosses) == 0


class TestMA:
    def test_calc_ma(self):
        prices = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert calc_ma(prices, 3) == 4.0

    def test_calc_ma_angle_up(self):
        prices = [10.0 + i * 0.5 for i in range(30)]
        angle = calc_ma_angle(prices, 5)
        assert angle > 0

    def test_calc_ma_angle_flat(self):
        prices = [10.0] * 30
        angle = calc_ma_angle(prices, 5)
        assert angle == 0.0
