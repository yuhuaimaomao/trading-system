"""止盈规则"""

from trade.portfolio.portfolio import Position


def check_take_profit(pos: Position) -> str:
    """目标止盈"""
    if pos.take_profit <= 0:
        return ""
    if pos.current_price >= pos.take_profit:
        return f"目标止盈 {pos.current_price:.2f} >= {pos.take_profit:.2f}"
    return ""


def check_trailing_stop(pos: Position) -> str:
    """移动止盈：从最高点回撤超限"""
    if pos.trailing_stop <= 0:
        return ""
    if pos.highest_price <= 0:
        return ""
    if pos.current_price <= pos.highest_price * (1 - pos.trailing_stop):
        return (
            f"移动止盈 {pos.current_price:.2f} "
            f"从高点 {pos.highest_price:.2f} 回撤 > {pos.trailing_stop:.0%}"
        )
    return ""


def should_take_profit(price: float, avg_cost: float, take_profit: float,
                       tp_lower: float = 1.0) -> tuple:
    """止盈检查（纯函数，不依赖 Position）。
    返回 (触发: bool, 有效止盈价: float)。
    tp_lower < 1 表示下调止盈目标（大盘危险时提前锁定利润）。
    """
    if take_profit <= 0 or avg_cost <= 0:
        return False, take_profit
    if tp_lower < 1.0:
        profit_width = take_profit - avg_cost
        effective_tp = avg_cost + profit_width * tp_lower
        if price >= effective_tp:
            return True, round(effective_tp, 2)
        return False, round(effective_tp, 2)
    if price >= take_profit:
        return True, take_profit
    return False, take_profit


def should_trailing_stop(price: float, highest_price: float, trailing_stop: float,
                         trail_tighten: float = 1.0) -> tuple:
    """移动止盈检查（纯函数）。
    返回 (触发: bool, 触发价: float)。
    trail_tighten < 1 表示缩小回撤容忍（大盘危险时快跑）。
    """
    if trailing_stop <= 0 or highest_price <= 0:
        return False, 0.0
    effective_trail = trailing_stop * trail_tighten
    trail_price = highest_price * (1 - effective_trail)
    if price <= trail_price:
        return True, round(trail_price, 2)
    return False, round(trail_price, 2)


def check_profit_target_reached(pos: Position, target_pct: float = 0.20) -> str:
    """按收益率止盈"""
    if target_pct <= 0:
        return ""
    if pos.pnl_pct >= target_pct:
        return f"收益目标达到 {pos.pnl_pct:.1%}"
    return ""
