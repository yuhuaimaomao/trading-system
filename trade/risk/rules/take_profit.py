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


def check_profit_target_reached(pos: Position, target_pct: float = 0.20) -> str:
    """按收益率止盈"""
    if target_pct <= 0:
        return ""
    if pos.pnl_pct >= target_pct:
        return f"收益目标达到 {pos.pnl_pct:.1%}"
    return ""
