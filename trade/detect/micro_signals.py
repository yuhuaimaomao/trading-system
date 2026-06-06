"""微观信号提取 — 从日内价格序列+量能+宽度提取情景引擎输入。

纯计算函数，不依赖 Watcher state。
"""

from stock.indicators import calc_rsi
from trade.core.scan_state import MicroSignals


def extract(
    index_prices: list[float],
    index_high: float,
    index_low: float,
    *,
    market_turnovers: list[float] | None = None,
    market_breadth: dict | None = None,
    prev_velocity: float = 0.0,
    prev_breadth: float = 0.5,
    recent_highs: list[float] | None = None,
    key_support: list[float] | None = None,
    key_resistance: list[float] | None = None,
    higher_highs: bool = False,
) -> MicroSignals:
    """从盘面数据提取微观信号 — 情景引擎的输入层。

    Args:
        index_prices: 日内指数价格序列
        index_high/low: 日内最高/最低
        market_turnovers: 全市场成交额序列
        market_breadth: {'up': N, 'down': N} 涨跌家数
        prev_velocity: 上一轮价格速率
        prev_breadth: 上一轮涨家占比
        recent_highs: 近期高点序列（用于高低点结构检测）
        key_support/resistance: 关键支撑/阻力位
        higher_highs: 是否持续创新高

    Returns MicroSignals。调用方自行维护 recent_highs/lows 等状态。
    """
    px = index_prices
    if len(px) < 5:
        return MicroSignals()

    cur = px[-1]
    prev = px[-2] if len(px) >= 2 else cur
    hi, lo = index_high, index_low

    # ── 价格速率 + 加速度 ──
    velocity = (cur - prev) / prev * 100 if prev > 0 else 0
    accel = velocity - prev_velocity

    # ── EMA12 关系 ──
    ema12 = _calc_intraday_ema(px, 12)
    ema12_pos = "above" if cur > ema12 else "below" if cur < ema12 else "on"
    ema12_crossed = ""
    if len(px) >= 3:
        prev_ema12_val = _calc_intraday_ema(px[:-1], 12)
        if px[-2] <= prev_ema12_val and cur > ema12:
            ema12_crossed = "crossed_up"
        elif px[-2] >= prev_ema12_val and cur < ema12:
            ema12_crossed = "crossed_down"

    # ── 量能脉冲 ──
    vols = market_turnovers or []
    vol_pulse = "normal"
    vol_confirm = "neutral"
    if len(vols) >= 6:
        recent_vol = sum(vols[-3:]) / 3 if vols[-3:] else 0
        prev_vol = sum(vols[-6:-3]) / 3 if len(vols) >= 6 and vols[-6:-3] else 0
        if prev_vol > 0:
            vol_ratio = recent_vol / prev_vol
            if vol_ratio > 1.3:
                vol_pulse = "expanding"
            elif vol_ratio < 0.7:
                vol_pulse = "contracting"
        if vol_pulse == "expanding" and abs(velocity) > 0.02:
            vol_confirm = "yes"
        elif vol_pulse == "contracting" and abs(velocity) > 0.02:
            vol_confirm = "no"

    # ── 宽度 ──
    breadth = market_breadth or {}
    up, down = breadth.get("up", 0), breadth.get("down", 0)
    total = up + down
    breadth_pct = up / total if total > 0 else 0.5
    breadth_trend = "stable"
    if prev_breadth > 0:
        delta = breadth_pct - prev_breadth
        if delta > 0.05:
            breadth_trend = "improving"
        elif delta < -0.05:
            breadth_trend = "deteriorating"

    # ── 反弹质量 ──
    bounce_pct = (cur - lo) / lo * 100 if lo > 0 else 0
    bounce_quality = ""
    if bounce_pct > 0 and len(px) >= 5:
        recent_5 = px[-5:]
        up_count = sum(
            1 for i in range(1, len(recent_5)) if recent_5[i] > recent_5[i - 1]
        )
        day_open = px[0] if len(px) > 30 else hi
        day_direction_up = (
            (cur - day_open) / day_open > 0.001 if day_open > 0 else False
        )
        if up_count >= 4 and bounce_pct > 0.3:
            bounce_quality = "strong"
        elif up_count >= 3 and not day_direction_up:
            bounce_quality = "weak"
        elif up_count <= 1 and velocity < 0:
            bounce_quality = "failed"

    # ── 高低点结构 ──
    lower_highs = False
    higher_lows = False
    highs = (recent_highs or []) + [cur]
    if len(highs) > 20:
        highs = highs[-20:]
    if len(highs) >= 10:
        first_half = highs[:5]
        second_half = highs[-5:]
        if max(second_half) < max(first_half) * 0.998:
            lower_highs = True
        if min(second_half) > min(first_half) * 1.002:
            higher_lows = True

    # ── RSI ──
    rsi_signal = ""
    if len(px) >= 30:
        try:
            window = 5
            closes = [px[i + window - 1] for i in range(0, len(px), window)]
            if len(closes) >= 14:
                rsi6 = calc_rsi(closes, 6)
                if rsi6 < 25:
                    rsi_signal = "oversold"
                elif rsi6 > 80:
                    rsi_signal = "overbought"
                if len(closes) >= 20:
                    prev_closes = closes[:-5]
                    prev_rsi = (
                        calc_rsi(prev_closes, 6) if len(prev_closes) >= 14 else 50
                    )
                    if closes[-1] < prev_closes[-1] and rsi6 > prev_rsi:
                        rsi_signal = "divergence_up"
                    elif closes[-1] > prev_closes[-1] and rsi6 < prev_rsi:
                        rsi_signal = "divergence_down"
        except Exception:
            pass

    # ── 振幅变化 ──
    range_expanding = False
    range_contracting = False
    if len(px) >= 20 and hi > lo:
        current_range = (hi - lo) / lo
        mid = len(px) // 2
        early_hi = max(px[:mid])
        early_lo = min(px[:mid])
        if early_hi > early_lo:
            early_range = (early_hi - early_lo) / early_lo
            if current_range > early_range * 1.3:
                range_expanding = True
            elif current_range < early_range * 0.7:
                range_contracting = True

    # ── 关键位测试 ──
    support = key_support or []
    resistance = key_resistance or []
    testing_support = any(abs(cur - s) / s < 0.003 for s in support)
    testing_resistance = any(abs(cur - r) / r < 0.003 for r in resistance)

    return MicroSignals(
        price_velocity=velocity,
        price_accel=accel,
        ema12_pos=ema12_pos,
        ema12_just_crossed=ema12_crossed,
        vol_pulse=vol_pulse,
        vol_price_confirm=vol_confirm,
        breadth_pct=breadth_pct,
        breadth_trend=breadth_trend,
        higher_highs=higher_highs,
        bounce_from_low=bounce_pct,
        bounce_quality=bounce_quality,
        lower_highs=lower_highs,
        higher_lows=higher_lows,
        rsi_signal=rsi_signal,
        testing_support=testing_support,
        testing_resistance=testing_resistance,
        range_expanding=range_expanding,
        range_contracting=range_contracting,
    )


def _calc_intraday_ema(prices: list[float], period: int) -> float:
    """从价格序列计算日内EMA最新值。"""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema
