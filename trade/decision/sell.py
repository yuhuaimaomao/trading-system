"""卖出/离场决策 — 纯规则函数。"""


def analyze_exit_signals(
    price: float, entry_price: float, trend: str,
    risk_level: str = "safe", pattern: str = "normal",
    bb_mid: float | None = None, ma60: float | None = None,
    macd_bar: float | None = None, macd_dif: float | None = None,
    bbi_daily: float | None = None, rsi12: float | None = None,
    rsi6: float | None = None, bb_lower: float | None = None,
    kdj_j: float | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """分析持仓的离场时机。返回 (exit_signals, wait_signals, env_parts)。

    三层视角：大盘环境 → 板块趋势 → 个股技术。
    """
    is_sector_weak = any(w in trend for w in ("持续走弱", "弱于大盘", "普跌"))
    is_market_extreme = risk_level in ("extreme",)
    is_market_dangerous = risk_level in ("dangerous",)
    is_panic = pattern in ("panic", "one_sided")

    exit_signals = []
    wait_signals = []
    env_parts = []

    # 大盘环境
    if is_market_extreme or is_panic:
        env_parts.append("🌐 大盘恐慌/极端 → 反弹不可靠，不建议等待，任何反弹都应减仓")
    elif is_market_dangerous:
        env_parts.append("🌐 大盘危险 → 反弹空间受限，降低等待预期")
    elif risk_level == "cautious":
        env_parts.append("🌐 大盘谨慎 → 正常等待技术反弹")

    # 板块走势
    if is_sector_weak and "加速" in trend:
        env_parts.append("📊 板块加速走弱 → 拖累个股，反弹力度有限，优先减仓")
    elif is_sector_weak:
        env_parts.append("📊 板块走弱 → 个股反弹可能受压制，不宜等太高")

    # 个股技术
    if bb_mid is not None and price >= bb_mid * 0.97:
        exit_signals.append(f"接近布林中轨{bb_mid:.2f}阻力位")
    if ma60 is not None and price >= ma60 * 0.97:
        exit_signals.append(f"接近MA60={ma60:.2f}压力位")
    if macd_bar is not None and macd_dif is not None and macd_bar < 0 and macd_dif < 0:
        exit_signals.append("MACD空头排列，下跌趋势未止")
    if bbi_daily is not None and price < bbi_daily:
        below_pct = (bbi_daily - price) / price * 100
        if below_pct > 5:
            exit_signals.append(f"远低于BBI{bbi_daily:.2f}，弱反弹即为减仓窗口")

    # 超卖判断
    if is_market_extreme or is_panic:
        if rsi12 is not None and rsi12 < 30:
            exit_signals.append(f"RSI虽超卖({rsi12:.0f})，但大盘弱势，反弹不可靠")
    else:
        if rsi12 is not None and rsi12 < 30:
            wait_signals.append(f"RSI(12)={rsi12:.0f}深度超卖，短期反弹概率高")
        elif rsi6 is not None and rsi6 < 25:
            wait_signals.append(f"RSI(6)={rsi6:.0f}极度超卖，反弹临近")

    if bb_lower is not None and price <= bb_lower * 1.03:
        if is_market_extreme or is_panic:
            exit_signals.append(f"触及布林下轨{bb_lower:.2f}，但大盘恐慌不宜等反弹")
        else:
            wait_signals.append(f"触及布林下轨{bb_lower:.2f}支撑，有技术反弹需求")

    if kdj_j is not None and kdj_j < 0:
        if is_market_extreme or is_panic:
            exit_signals.append("KDJ虽超卖，但大盘弱势不建议等")
        else:
            wait_signals.append(f"KDJ J={kdj_j:.0f}极度超卖，反弹可能启动")

    return exit_signals, wait_signals, env_parts


def classify_holding_status(
    price: float, entry_price: float, sl: float, tp: float = 0,
    is_today_buy: bool = False, pct_b: float | None = None,
    rsi12: float | None = None,
) -> str:
    """分类持仓状态：healthy / watching / at_risk / trapped / deep_trapped / add_opportunity。"""
    if entry_price <= 0:
        return "watching"
    pnl_pct = (price - entry_price) / entry_price * 100

    if pnl_pct <= -10:
        return "deep_trapped"
    if pnl_pct <= -5:
        return "trapped"
    if pnl_pct <= -2 and sl > 0 and entry_price > sl:
        loss_used = (entry_price - price) / (entry_price - sl)
        if loss_used >= 0.85:
            return "at_risk"
        if loss_used < 0.5 and pct_b is not None and 5 <= pct_b <= 30 and rsi12 is not None and rsi12 < 40:
            return "add_opportunity"
        return "watching"
    if pnl_pct > 2:
        return "healthy"
    return "watching"
