"""仓位计算 + 买入区修正 — 纯规则函数，不依赖 Watcher state。"""


def calculate_position_size(
    code: str,
    price: float,
    buy_min: float,
    buy_max: float,
    pattern: str,
    sector_trend: str,
    market_breadth: dict | None = None,
    industry_cache: dict[str, str] | None = None,
    morning_sector_bias: dict[str, dict] | None = None,
) -> tuple[int, str]:
    """根据盘面动态计算买入金额（0-16000），返回 (金额, 决策理由)。"""
    BLOCKED = (
        "panic",
        "one_sided",
        "dead_cat",
        "inverted_v",
        "m_top",
        "gap_up_fade",
        "late_dump",
        "fishing_line",
    )
    if pattern in BLOCKED:
        return 0, f"市场{pattern}模式，暂停买入"

    CAUTIOUS = (
        "v_reversal",
        "w_bottom",
        "melt_up",
        "late_rally",
        "wide_choppy",
        "gap_down_recover",
    )
    if pattern in CAUTIOUS:
        base = 8000
        reason = f"市场{pattern}模式，谨慎参与"
    elif pattern in ("normal", "uptrend"):
        base = 16000
        reason = "大盘正常" if pattern == "normal" else "大盘上行"
    else:
        base = 16000
        reason = ""

    # 市场宽度修正
    breadth = market_breadth or {}
    up, down = breadth.get("up", 0), breadth.get("down", 0)
    if up + down > 0:
        down_ratio = down / (up + down)
        if down_ratio > 0.7:
            base = max(base * 0.3, 5000)
            reason += " 普跌" if reason else "普跌"
        elif down_ratio > 0.6:
            base = max(base * 0.5, 5000)
            reason += " 偏弱" if reason else "偏弱"

    # 板块趋势修正
    if "持续走弱" in sector_trend:
        base = max(base * 0.3, 5000)
        reason += " 板块持续走弱" if reason else "板块持续走弱"
    elif "走弱" in sector_trend:
        base = max(base * 0.6, 5000)
        reason += " 板块走弱" if reason else "板块走弱"
    elif "持续走强" in sector_trend:
        base = min(base * 1.3, 16000)
    elif "走强" in sector_trend:
        base = min(base * 1.2, 16000)

    # 早盘 AI 板块倾向修正
    cache = industry_cache or {}
    industry = cache.get(code, "")
    bias = (morning_sector_bias or {}).get(industry, {}) if industry else {}
    if bias:
        b_mult = bias.get("size_mult", 1.0)
        if bias.get("bias") == "focus":
            base = min(int(base * b_mult), 16000)
            reason += f" AI聚焦({b_mult:.1f}x)" if reason else f"AI聚焦({b_mult:.1f}x)"
        elif bias.get("bias") == "avoid":
            base = max(int(base * b_mult), 3000)
            reason += f" AI回避({b_mult:.1f}x)" if reason else f"AI回避({b_mult:.1f}x)"

    # 买入区位置修正
    zone_range = buy_max - buy_min if buy_max > buy_min else 1
    position_in_zone = (price - buy_min) / zone_range
    if position_in_zone <= 0.33:
        base = min(base * 1.1, 16000)
        reason += " 买入区下沿"
    elif position_in_zone >= 0.67:
        base = max(base * 0.7, 5000)
        reason += " 买入区上沿"

    return int(base // 100 * 100), reason.strip()


def calc_dynamic_buy_zone(
    code: str,
    price: float,
    buy_min: float,
    buy_max: float,
    trend: str = "",
    market_adjustment: dict | None = None,
) -> tuple[float, float, str]:
    """动态买入区修正：市场偏空+板块弱 → 买入区整体下移。返回 (new_min, new_max, reason)。"""
    if not market_adjustment:
        return buy_min, buy_max, ""

    adj = market_adjustment
    shift = adj.get("buy_zone_shift", 0)
    if shift <= 0 or not adj.get("reason"):
        return buy_min, buy_max, ""

    zone_width = buy_max - buy_min
    new_min = round(buy_min * (1 - shift), 2)
    new_max = round(buy_max * (1 - shift), 2)

    if new_max - new_min < zone_width * 0.5:
        new_max = round(new_min + zone_width * 0.5, 2)

    return new_min, new_max, ""


def calc_unified_stop_loss(
    code: str,
    price: float,
    trend: str = "",
    daily_indicators: dict | None = None,
    strategy_type: str = "standard",
) -> float:
    """统一止损计算 — 所有买入来源共用。

    策略:
    1. ATR 动态宽度（默认 2x ATR，波动大的票给更宽的止损）
    2. 策略类型修正：trend=趋势票放宽(1.2x)，chase=追高票收紧(0.8x)
    3. 板块趋势修正：走强放宽(1.1x)，走弱收紧(0.85x)
    4. 支撑位约束：止损不低于最近支撑下方 1%
    5. 硬地板：不低于现价的 93%

    Returns: 止损价（float，保留 2 位小数）
    """
    ind = daily_indicators or {}
    # 1. ATR 宽度（默认 3% 如果无数据）
    atr = ind.get("atr14", price * 0.03) if ind else price * 0.03
    atr_pct = atr / price if price > 0 else 0.03

    # 2. 策略类型修正
    strategy_mult = {"trend": 1.2, "chase": 0.8}.get(strategy_type, 1.0)

    # 3. 板块修正
    sector_mult = 1.0
    if "持续走强" in trend:
        sector_mult = 1.1
    elif "持续走弱" in trend:
        sector_mult = 0.85
    elif "走弱" in trend:
        sector_mult = 0.92

    # 4. 计算原始止损（2x ATR × 策略 × 板块）
    raw_sl = price * (1 - atr_pct * 2 * strategy_mult * sector_mult)

    # 5. 支撑位约束（从 indicators 获取）
    supports = ind.get("_supports", []) if ind else []
    if supports:
        nearest_support = (
            supports[0][0] if isinstance(supports[0], tuple) else supports[0]
        )
        raw_sl = max(raw_sl, nearest_support * 0.99)

    # 6. 硬地板（不低于 93%）
    sl = max(raw_sl, round(price * 0.93, 2))

    return round(sl, 2)


def calc_unified_take_profit(
    code: str,
    price: float,
    trend: str = "",
    daily_indicators: dict | None = None,
    strategy_type: str = "standard",
) -> float:
    """统一止盈计算 — 所有买入来源共用。

    策略:
    1. ATR 动态宽度（默认 3x ATR）
    2. 阻力位约束：止盈不高于最近阻力
    3. 硬天花板：不高于现价的 112%

    Returns: 止盈价（float，保留 2 位小数）
    """
    ind = daily_indicators or {}
    atr = ind.get("atr14", price * 0.03) if ind else price * 0.03
    atr_pct = atr / price if price > 0 else 0.03

    # 策略类型修正
    strategy_mult = {"trend": 1.3, "chase": 0.7}.get(strategy_type, 1.0)

    # 板块修正
    sector_mult = 1.0
    if "持续走强" in trend:
        sector_mult = 1.15
    elif "走强" in trend:
        sector_mult = 1.05
    elif "持续走弱" in trend:
        sector_mult = 0.85

    # 原始止盈
    raw_tp = price * (1 + atr_pct * 3 * strategy_mult * sector_mult)

    # 阻力位约束
    resistances = ind.get("_resistances", []) if ind else []
    if resistances:
        nearest_resistance = (
            resistances[0][0] if isinstance(resistances[0], tuple) else resistances[0]
        )
        raw_tp = min(raw_tp, nearest_resistance)

    # 硬天花板
    tp = min(raw_tp, round(price * 1.12, 2))

    return round(tp, 2)
