"""日内熔断规则"""


def check_daily_loss_limit(
    daily_pnl: float, total_value: float, max_loss_pct: float = 0.03
) -> bool:
    """当日累计亏损超过总资金 N% → 熔断"""
    if total_value <= 0:
        return False
    return abs(daily_pnl) / total_value > max_loss_pct


