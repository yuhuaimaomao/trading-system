"""日内熔断规则"""


def check_daily_loss_limit(
    daily_pnl: float, total_value: float, max_loss_pct: float = 0.03
) -> bool:
    """当日累计亏损超过总资金 N% → 熔断"""
    if total_value <= 0:
        return False
    return abs(daily_pnl) / total_value > max_loss_pct


def should_close_losing_positions(
    daily_pnl: float, total_value: float, max_loss_pct: float = 0.03
) -> tuple[bool, str]:
    """检查是否需要清掉浮亏仓位"""
    if not check_daily_loss_limit(daily_pnl, total_value, max_loss_pct):
        return False, ""
    loss_ratio = abs(daily_pnl) / total_value
    return True, f"日内熔断触发 (日亏损 {loss_ratio:.1%} > {max_loss_pct:.1%})"
