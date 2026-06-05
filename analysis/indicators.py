"""技术指标计算 — MACD / RSI / KDJ + 形态检测

纯函数，输入 OHLCV 序列（按日期升序），返回当前值。
算法对齐主流看盘软件（同花顺/东方财富），采用 Wilder 平滑和 SMA 初始化。
"""


def _ema(data: list[float], period: int) -> list[float]:
    """EMA — 首值用 SMA 初始化，之后用递归公式"""
    if len(data) < period:
        return [sum(data) / len(data)] * len(data) if data else []

    k = 2 / (period + 1)
    result = []
    # 首值 = SMA(period)
    sma = sum(data[:period]) / period
    result.append(sma)
    for i in range(period, len(data)):
        result.append(data[i] * k + result[-1] * (1 - k))
    # 补齐前 period-1 个位置
    return [sma] * (period - 1) + result


def calc_macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict:
    """MACD — 返回 DIF, DEA, bar (柱值)"""
    if len(closes) < slow + signal:
        return {"dif": 0, "dea": 0, "bar": 0}

    ema12 = _ema(closes, fast)
    ema26 = _ema(closes, slow)
    dif = [ema12[i] - ema26[i] for i in range(len(closes))]
    dea = _ema(dif, signal)
    bar = 2 * (dif[-1] - dea[-1])
    return {"dif": round(dif[-1], 4), "dea": round(dea[-1], 4), "bar": round(bar, 4)}


def calc_rsi(closes: list[float], period: int = 14) -> float:
    """RSI — Wilder 平滑（对齐同花顺/东方财富）"""
    if len(closes) < period + 1:
        return 50.0

    gains, losses = [], []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0))
        losses.append(max(-chg, 0))

    # 首次 SMA
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder 平滑递推
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def calc_kdj(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    n: int = 9,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> dict:
    """KDJ — 随机指标，返回 K, D, J"""
    if len(closes) < n:
        return {"k": 50.0, "d": 50.0, "j": 50.0}

    k_vals, d_vals = [], []
    prev_k, prev_d = 50.0, 50.0
    for i in range(n - 1, len(closes)):
        hh = max(highs[i - n + 1 : i + 1])
        ll = min(lows[i - n + 1 : i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50.0
        k = (prev_k * (k_smooth - 1) + rsv) / k_smooth
        d = (prev_d * (d_smooth - 1) + k) / d_smooth
        k_vals.append(k)
        d_vals.append(d)
        prev_k, prev_d = k, d

    k_val = k_vals[-1] if k_vals else 50.0
    d_val = d_vals[-1] if d_vals else 50.0
    j_val = 3 * k_val - 2 * d_val
    return {"k": round(k_val, 2), "d": round(d_val, 2), "j": round(j_val, 2)}


# ---- 形态检测 -------------------------------------------------------------


def calc_bollinger(
    closes: list[float], period: int = 20, std_mult: float = 2.0
) -> dict:
    """布林带 — 返回 upper, mid, lower, width(带宽%), pct_b(价格在带内位置%)"""
    if len(closes) < period:
        return {"upper": 0, "mid": 0, "lower": 0, "width": 0, "pct_b": 0}

    mid = sum(closes[-period:]) / period
    variance = sum((x - mid) ** 2 for x in closes[-period:]) / period
    std = variance**0.5

    upper = mid + std_mult * std
    lower = mid - std_mult * std
    # 带宽百分比：带子宽度相对于中轨的比例
    width = (upper - lower) / mid * 100 if mid > 0 else 0
    # %b：当前价在带内的相对位置 (0=下轨, 50=中轨, 100=上轨)
    last = closes[-1]
    pct_b = (last - lower) / (upper - lower) * 100 if upper != lower else 50.0

    return {
        "upper": round(upper, 2),
        "mid": round(mid, 2),
        "lower": round(lower, 2),
        "width": round(width, 2),
        "pct_b": round(pct_b, 1),
    }


def calc_macd_series(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict:
    """返回 MACD DIF/DEA/BAR 完整序列（用于形态检测）"""
    if len(closes) < slow + signal:
        return {"dif": [], "dea": [], "bar": []}
    ema12 = _ema(closes, fast)
    ema26 = _ema(closes, slow)
    dif = [ema12[i] - ema26[i] for i in range(len(closes))]
    dea = _ema(dif, signal)
    bar = [2 * (dif[i] - dea[i]) for i in range(len(closes))]
    return {"dif": dif, "dea": dea, "bar": bar}


def detect_macd_cross(
    dif: list[float], dea: list[float], lookback: int = 20
) -> list[dict]:
    """检测近期金叉/死叉。返回 [(days_ago, type), ...]"""
    crosses = []
    if len(dif) < 2:
        return crosses
    start = max(0, len(dif) - lookback - 1)
    for i in range(start + 1, len(dif)):
        if dif[i - 1] <= dea[i - 1] and dif[i] > dea[i]:
            crosses.append({"days_ago": len(dif) - 1 - i, "type": "金叉(DIF上穿DEA)"})
        elif dif[i - 1] >= dea[i - 1] and dif[i] < dea[i]:
            crosses.append({"days_ago": len(dif) - 1 - i, "type": "死叉(DIF下穿DEA)"})
    return crosses


def calc_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> float:
    """ATR — Average True Range，Wilder 平滑"""
    if len(closes) < 2:
        return 0.0

    trs = []
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i - 1])
        low_close = abs(lows[i] - closes[i - 1])
        trs.append(max(high_low, high_close, low_close))

    if len(trs) < period:
        return round(sum(trs) / len(trs), 4)

    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return round(atr, 4)


def detect_divergence(
    closes: list[float], dif: list[float], lookback: int = 30
) -> list[dict]:
    """检测顶背离/底背离。比较近 30 天内价格与 DIF 的局部高/低点"""
    divergences = []
    if len(closes) < lookback:
        return divergences

    c = closes[-lookback:]
    d = dif[-lookback:]
    # 确保 c 和 d 等长（MACD 序列可能比价格序列短）
    min_len = min(len(c), len(d))
    c = c[:min_len]
    d = d[:min_len]

    peaks = []  # (idx, price, dif)
    troughs = []  # (idx, price, dif)
    for i in range(2, min_len - 2):
        if c[i] > c[i - 1] and c[i] > c[i - 2] and c[i] > c[i + 1] and c[i] > c[i + 2]:
            peaks.append((i, c[i], d[i]))
        if c[i] < c[i - 1] and c[i] < c[i - 2] and c[i] < c[i + 1] and c[i] < c[i + 2]:
            troughs.append((i, c[i], d[i]))

    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        if p2[1] > p1[1] and p2[2] < p1[2]:
            divergences.append(
                {"type": "顶背离", "desc": "股价创新高但DIF走弱，上涨动能衰减"}
            )

    if len(troughs) >= 2:
        t1, t2 = troughs[-2], troughs[-1]
        if t2[1] < t1[1] and t2[2] > t1[2]:
            divergences.append(
                {"type": "底背离", "desc": "股价创新低但DIF拒绝跟随，下跌动能衰减"}
            )

    return divergences


# ---- 均线 -------------------------------------------------------------


def calc_ma(prices: list[float], period: int) -> float:
    """简单移动平均"""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0.0
    return sum(prices[-period:]) / period


def calc_ma_angle(prices: list[float], period: int = 5) -> float:
    """MA 斜率（度）— 用最近 period 天的值做线性回归，返回角度"""
    if len(prices) < period + 1:
        return 0.0
    import math

    n = period
    x_mean = (n - 1) / 2
    y_mean = sum(prices[-n:]) / n
    num = sum((i - x_mean) * (prices[-n + i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0 or y_mean == 0:
        return 0.0
    slope = num / den
    angle = math.degrees(math.atan(slope / y_mean * 100))
    return round(angle, 2)


# ---- 金叉/死叉便捷函数 --------------------------------------------------


def detect_golden_cross(dif: list[float], dea: list[float]) -> bool:
    """最近一次 DIF/DEA 交叉是否为金叉"""
    crosses = detect_macd_cross(dif, dea, lookback=5)
    if not crosses:
        return False
    return crosses[-1]["type"].startswith("金叉")


def detect_death_cross(dif: list[float], dea: list[float]) -> bool:
    """最近一次 DIF/DEA 交叉是否为死叉"""
    crosses = detect_macd_cross(dif, dea, lookback=5)
    if not crosses:
        return False
    return crosses[-1]["type"].startswith("死叉")
