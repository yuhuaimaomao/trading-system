"""买入决策评估 — 纯规则函数，不依赖 Watcher state。

evaluate_buy: 价格在买入区内的多维评估
evaluate_below_zone: 价格低于买入区时的回调评估
"""

from dataclasses import dataclass, field


@dataclass
class BuyEvalInput:
    """买入评估所需的全部输入数据。由调用方（Watcher/CLI）预先收集。"""

    # 基础
    code: str = ""
    price: float = 0.0
    buy_min: float = 0.0
    buy_max: float = 0.0

    # 板块
    sector_trend: str = ""
    sector_chg: float | None = None
    sector_decline: float | None = None
    sector_recovery_risk: float | None = None
    concept_score: int = 0
    concept_reason: str = ""

    # 日线布林/均线（从 stock_indicators）
    daily_bb_upper: float = 0
    daily_bb_mid: float = 0
    daily_bb_lower: float = 0
    daily_bb_pct_b: float | None = None
    daily_ma5: float = 0
    daily_ma10: float = 0
    daily_ma20: float = 0

    # 日线 KDJ/RSI
    daily_rsi6: float | None = None
    daily_rsi12: float | None = None
    daily_kdj_k: float | None = None
    daily_kdj_d: float | None = None
    daily_kdj_j: float | None = None

    # 日内分钟级指标
    intra_available: bool = False
    intra_rsi6: float = 50
    intra_rsi12: float = 50
    intra_macd_direction: str = ""
    intra_macd_bar: float = 0
    intra_kdj_k: float = 50
    intra_kdj_d: float = 50
    intra_kdj_j: float = 50
    intra_price_vs_ma5: float = 0

    # 盘口
    ob_ratio: float = 0.5
    ob_reason: str = ""

    # 大单
    big_ratio: float = 0.5
    big_reason: str = ""

    # 涨跌停
    up_stop: float = 0
    down_stop: float = 0

    # 昨日趋势背景
    yesterday_mf_ratio: float = 0
    ma5_angle: float = 0
    day_position: float | None = None
    daily_macd_dif: float = 0
    daily_macd_dea: float = 0
    daily_macd_bar: float = 0
    daily_kdj_j_daily: float = 50
    bbi_daily: float = 0
    m5_macd_dif: float | None = None
    m5_macd_dea: float | None = None
    m5_macd_bar: float | None = None
    bb_width: float = 0

    # 价格走势
    price_action: str = "no_data"
    price_action_desc: str = ""

    # 板块强弱标记
    sector_strong: bool = False
    sector_very_strong: bool = False
    ai_bias: str = ""       # "" / "focus" / "avoid"
    ai_size_mult: float = 1.0


def evaluate_buy(ctx: BuyEvalInput) -> tuple[bool, str, float]:
    """多维买入决策评估。返回 (allowed, reason, size_multiplier)。"""
    reject_reasons = []
    warn_reasons = []
    size_mul = 1.0

    sector_chg = ctx.sector_chg
    trend = ctx.sector_trend
    decline = ctx.sector_decline
    recovery_risk = ctx.sector_recovery_risk
    sector_strong = ctx.sector_strong
    sector_very_strong = ctx.sector_very_strong

    # 1. 板块趋势
    SECTOR_REJECT_PCT = -1.0
    if not trend or "数据不足" in trend or "数据积累中" in trend:
        reject_reasons.append(f"板块数据不足，开盘初期暂不买入{trend}")
        size_mul = 0.0
    elif sector_chg is not None and sector_chg <= SECTOR_REJECT_PCT:
        reject_reasons.append(f"板块跌幅 {sector_chg:+.1f}%，拒绝买入")
        size_mul = 0.0
    elif decline is not None and decline >= 1.5:
        reject_reasons.append(f"板块冲高回落 {decline:+.1f}%，拒绝追入")
        size_mul = 0.0
    elif recovery_risk is not None:
        reject_reasons.append(f"板块从日内低点反弹 {recovery_risk:+.1f}%，疑似死猫跳不追")
        size_mul = 0.0
    elif "持续走弱" in trend:
        reject_reasons.append(f"板块持续走弱，不买入{trend}")
        size_mul = 0.0
    elif "走弱" in trend:
        warn_reasons.append(f"板块偏弱{trend}")
        size_mul *= 0.5
    elif "持续走强" in trend:
        size_mul = min(1.0, size_mul * 1.2)

    # 1b. 概念板块趋势
    if ctx.concept_score <= -2:
        reject_reasons.append(f"多数概念板块走弱{ctx.concept_reason}")
        size_mul = 0.0
    elif ctx.concept_score < 0:
        warn_reasons.append(f"概念板块偏弱{ctx.concept_reason}")
        size_mul *= 0.6

    # AI 板块倾向
    if ctx.ai_bias == "focus":
        sector_very_strong = True
        size_mul = min(1.0, size_mul * ctx.ai_size_mult)
    elif ctx.ai_bias == "avoid":
        if "持续走弱" in trend:
            reject_reasons.append(f"AI回避+板块持续走弱")
            size_mul = 0.0
        else:
            size_mul *= ctx.ai_size_mult
            warn_reasons.append("AI建议回避")

    # 2. 买入区位置
    zone_range = ctx.buy_max - ctx.buy_min if ctx.buy_max > ctx.buy_min else 1
    zone_pos = (ctx.price - ctx.buy_min) / zone_range
    if zone_pos >= 0.85:
        reject_reasons.append(f"买入区顶部({zone_pos:.0%})，不追高")
    elif zone_pos >= 0.65:
        warn_reasons.append(f"买入区偏上({zone_pos:.0%})")
        size_mul *= 0.7

    # 3. 布林带 + 均线
    pct_b = ctx.daily_bb_pct_b
    b_reject = 95 if sector_very_strong else 90
    b_warn = 85 if sector_very_strong else 75
    if pct_b is not None and pct_b >= b_reject:
        reject_reasons.append(f"布林带超买(%B={pct_b:.0f})，回调风险高")
    elif pct_b is not None and pct_b >= b_warn:
        warn_reasons.append(f"布林带偏上(%B={pct_b:.0f})")
        size_mul *= 0.8

    ma5, ma10, ma20 = ctx.daily_ma5, ctx.daily_ma10, ctx.daily_ma20
    price = ctx.price
    if ma5 and ma10 and ma20 and ma5 > 0 and ma10 > 0 and ma20 > 0:
        below_all = price < ma5 and price < ma10 and price < ma20
        bearish_alignment = ma5 < ma10 < ma20
        if below_all and bearish_alignment:
            reject_reasons.append("均线空头排列+价格破位，疑似接飞刀")
        elif below_all:
            warn_reasons.append("价格低于所有均线，趋势偏弱")
            size_mul *= 0.7

    bb_lower = ctx.daily_bb_lower
    near_support = False
    if bb_lower and abs(price - bb_lower) / bb_lower < 0.03:
        near_support = True
    if ma20 and abs(price - ma20) / ma20 < 0.03:
        near_support = True
    if near_support and not reject_reasons:
        size_mul = min(1.0, size_mul * 1.2)

    # 3b. 日线 KDJ/RSI
    d_j = ctx.daily_kdj_j
    d_rsi6 = ctx.daily_rsi6
    if d_j is not None and d_j > 100:
        reject_reasons.append(f"日线KDJ极度超买(J={d_j:.0f})")
    elif d_j is not None and d_j > 85:
        warn_reasons.append(f"日线KDJ超买(J={d_j:.0f})")
        size_mul *= 0.6
    if d_rsi6 is not None and d_rsi6 >= 80:
        reject_reasons.append(f"日线RSI6超买({d_rsi6:.0f})，不宜追高")
    elif d_rsi6 is not None and d_rsi6 >= 70:
        warn_reasons.append(f"日线RSI6偏高({d_rsi6:.0f})")
        size_mul *= 0.7

    # 4. 日内分钟级指标
    if ctx.intra_available:
        r6 = ctx.intra_rsi6
        if r6 >= 85:
            reject_reasons.append(f"日内RSI6极度超买({r6:.0f})，追高风险极大")
        elif r6 >= 75:
            warn_reasons.append(f"日内RSI6超买({r6:.0f})")
            size_mul *= 0.7 if not sector_strong else 0.85
        elif r6 <= 20:
            size_mul = min(1.0, size_mul * 1.1)

        macd_reject_bar = -0.8 if sector_very_strong else -0.5
        macd_warn_bar = -0.3 if sector_very_strong else -0.1
        if ctx.intra_macd_direction == "bearish" and ctx.intra_macd_bar < macd_reject_bar:
            reject_reasons.append("日内MACD强烈空头，下跌动能未衰竭")
        elif ctx.intra_macd_direction == "bearish" and ctx.intra_macd_bar < macd_warn_bar:
            warn_reasons.append(f"日内MACD空头(bar={ctx.intra_macd_bar:.2f})")
            size_mul *= 0.8 if not sector_strong else 0.9
        elif ctx.intra_macd_direction == "bullish" and ctx.intra_macd_bar > 0.2:
            size_mul = min(1.0, size_mul * 1.1)

        j = ctx.intra_kdj_j
        k, d = ctx.intra_kdj_k, ctx.intra_kdj_d
        if j > 100:
            reject_reasons.append(f"日内KDJ极度超买(J={j:.0f})")
        elif j > 85:
            warn_reasons.append(f"日内KDJ超买(J={j:.0f})")
            size_mul *= 0.7 if not sector_strong else 0.85
        elif j < 0:
            size_mul = min(1.0, size_mul * 1.1)
        if k < d and j < 50 and j >= 0:
            warn_reasons.append("日内KDJ死叉")
            size_mul *= 0.85 if not sector_strong else 0.95

        vs_ma5 = ctx.intra_price_vs_ma5
        if vs_ma5 < -3:
            reject_reasons.append(f"价格远离日内MA5({vs_ma5:+.1f}%)，短期急跌接飞刀")
        elif vs_ma5 < -1.5:
            warn_reasons.append(f"价格低于日内MA5({vs_ma5:+.1f}%)")

    # 5. 盘口
    if ctx.ob_ratio <= 0.3 and ctx.ob_reason:
        reject_reasons.append(f"盘口卖盘沉重(买盘{ctx.ob_ratio:.0%})")
    elif ctx.ob_ratio <= 0.42 and ctx.ob_reason:
        warn_reasons.append(f"盘口卖压偏大(买盘{ctx.ob_ratio:.0%})")
        size_mul *= 0.85
    elif ctx.ob_ratio >= 0.7:
        size_mul = min(1.0, size_mul * 1.1)

    # 6. 大单
    if ctx.big_ratio <= 0.35 and ctx.big_reason:
        reject_reasons.append(ctx.big_reason)
    elif ctx.big_ratio <= 0.45 and ctx.big_reason:
        warn_reasons.append(ctx.big_reason)
        size_mul *= 0.8
    elif ctx.big_ratio >= 0.65 and ctx.big_reason:
        size_mul = min(1.0, size_mul * 1.1)

    # 7. 涨跌停空间
    if ctx.up_stop > 0 and price > 0:
        room_pct = (ctx.up_stop - price) / price * 100
        if room_pct < 2:
            reject_reasons.append(f"距涨停仅{room_pct:.1f}%，追板风险极高")
        elif room_pct < 4:
            warn_reasons.append(f"距涨停{room_pct:.1f}%，上行空间有限")
            size_mul *= 0.8
    if ctx.down_stop > 0 and price > 0:
        risk_pct = (price - ctx.down_stop) / price * 100
        if risk_pct > 15:
            reject_reasons.append(f"距跌停{risk_pct:.0f}%，下方风险空间过大")

    # 8. 昨日趋势背景
    mf_ratio = ctx.yesterday_mf_ratio
    if mf_ratio > 5:
        size_mul = min(1.0, size_mul * 1.1)
    elif mf_ratio < -5:
        reject_reasons.append(f"昨日主力大幅流出({mf_ratio:.1f}%)，今日承压")
    elif mf_ratio < -2:
        warn_reasons.append(f"昨日主力流出({mf_ratio:.1f}%)")
        size_mul *= 0.85

    ma5_ang = ctx.ma5_angle
    if ma5_ang < -2:
        reject_reasons.append(f"MA5加速下行(角{ma5_ang:.1f})，趋势向空")
    elif ma5_ang < 0:
        warn_reasons.append(f"MA5拐头向下(角{ma5_ang:.1f})")
        size_mul *= 0.85

    day_pos = ctx.day_position
    if day_pos is not None:
        if day_pos < 0.15 and ma5_ang > 1:
            size_mul = min(1.0, size_mul * 1.1)
        elif day_pos > 0.9:
            warn_reasons.append("价格接近日内高点")
            size_mul *= 0.85

    dm_dif, dm_dea, dm_bar = ctx.daily_macd_dif, ctx.daily_macd_dea, ctx.daily_macd_bar
    if dm_dif < dm_dea and dm_bar < -0.3:
        warn_reasons.append("日线MACD空头")
        size_mul *= 0.85
    elif dm_dif > dm_dea and dm_bar > 0.2:
        size_mul = min(1.0, size_mul * 1.05)

    dk_j = ctx.daily_kdj_j_daily
    if dk_j > 100:
        reject_reasons.append(f"日线KDJ极度超买(J={dk_j:.0f})")
    elif dk_j > 85:
        warn_reasons.append(f"日线KDJ超买(J={dk_j:.0f})")
        size_mul *= 0.8
    elif dk_j < 0:
        size_mul = min(1.0, size_mul * 1.1)

    bbi = ctx.bbi_daily
    if bbi > 0 and price < bbi * 0.95:
        warn_reasons.append("价格低于BBI多空线")
        size_mul *= 0.85

    if ctx.m5_macd_dif is not None:
        m5_dif, m5_dea, m5_bar = ctx.m5_macd_dif, ctx.m5_macd_dea, ctx.m5_macd_bar or 0
        if m5_dif < m5_dea and m5_bar < -0.2:
            warn_reasons.append("5min MACD空头")
            size_mul *= 0.85
        elif m5_dif > m5_dea and m5_bar > 0.1:
            size_mul = min(1.0, size_mul * 1.05)

    bb_w = ctx.bb_width
    bb_warn = 70 if sector_very_strong else 40
    if bb_w > bb_warn:
        warn_reasons.append(f"布林带宽({bb_w:.0f})，波动剧烈")
        size_mul *= 0.85 if sector_strong else 0.8

    # 9. 价格走势
    if ctx.price_action == "declining":
        reject_reasons.append(f"10分钟内{ctx.price_action_desc}，等待止跌再买")
    elif ctx.price_action == "reversing":
        size_mul = min(1.0, size_mul * 1.15)

    # 10. 汇总
    if reject_reasons:
        return False, "; ".join(reject_reasons), 0
    if warn_reasons:
        return True, "; ".join(warn_reasons), max(0.5, size_mul)
    return True, "条件符合", size_mul
