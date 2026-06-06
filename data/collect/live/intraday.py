"""日内分钟级技术指标快照 — 从 QMT 取分钟K线，计算 RSI/MACD/KDJ。

纯数据获取+计算，不依赖 Watcher state。
"""

from stock.indicators import calc_kdj, calc_macd, calc_rsi


def intraday_snapshot(
    code: str, qmt, cache: dict, cache_scan: int, scan_count: int
) -> dict:
    """获取个股日内分钟级技术指标。缓存同一扫描轮内复用。

    Args:
        code: 股票代码
        qmt: QMT QuoteClient 实例（有 get_minute_kline 方法）
        cache: 缓存 dict，通常来自 IntradayCache
        cache_scan: 缓存对应的 scan_count
        scan_count: 当前扫描轮次

    Returns:
        dict with keys: rsi6, rsi12, macd_dif, macd_dea, macd_bar,
        macd_direction, kdj_k, kdj_d, kdj_j, price_vs_ma5, available
    """
    if cache_scan == scan_count and code in cache:
        return cache[code]

    result = {
        "rsi6": 50,
        "rsi12": 50,
        "macd_dif": 0,
        "macd_dea": 0,
        "macd_bar": 0,
        "macd_direction": "",
        "kdj_k": 50,
        "kdj_d": 50,
        "kdj_j": 50,
        "price_vs_ma5": 0,
        "available": False,
    }

    if not qmt:
        return result

    try:
        raw = qmt.get_minute_kline(code, count=240)
        if not raw or len(raw) < 26:
            return result

        closes = [float(k.get("close", 0)) for k in raw if k.get("close")]
        highs = [float(k.get("high", 0)) for k in raw if k.get("high")]
        lows = [float(k.get("low", 0)) for k in raw if k.get("low")]

        if len(closes) < 26:
            return result

        result["available"] = True
        result["rsi6"] = calc_rsi(closes, 6)
        result["rsi12"] = calc_rsi(closes, 12)
        macd = calc_macd(closes)
        result["macd_dif"] = macd["dif"]
        result["macd_dea"] = macd["dea"]
        result["macd_bar"] = macd["bar"]
        kdj = calc_kdj(highs, lows, closes)
        result["kdj_k"] = kdj["k"]
        result["kdj_d"] = kdj["d"]
        result["kdj_j"] = kdj["j"]

        # MACD 方向
        if macd["dif"] > macd["dea"]:
            result["macd_direction"] = "bullish"
        elif macd["dif"] < macd["dea"]:
            result["macd_direction"] = "bearish"

        # 价格相对 MA5
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else closes[-1]
        if ma5 > 0:
            result["price_vs_ma5"] = (closes[-1] - ma5) / ma5 * 100

    except Exception:
        pass

    cache[code] = result
    return result
