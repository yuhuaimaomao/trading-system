"""止损规则"""

from trade.portfolio.portfolio import Position


def check_stop_loss(pos: Position) -> str:
    """固定止损：价格跌破止损位"""
    if pos.stop_loss <= 0:
        return ""
    if pos.current_price <= pos.stop_loss:
        return f"止损触发 {pos.current_price:.2f} <= {pos.stop_loss:.2f}"
    return ""


def check_ma_stop(pos: Position, ma_value: float) -> str:
    """均线止损：价格跌破关键均线"""
    if ma_value <= 0:
        return ""
    if pos.current_price < ma_value:
        return f"均线止损 {pos.current_price:.2f} < MA {ma_value:.2f}"
    return ""


def should_stop_loss(price: float, avg_cost: float, stop_loss: float,
                     tighten: float = 1.0) -> tuple:
    """止损检查（纯函数，不依赖 Position）。
    返回 (触发: bool, 有效止损价: float)。
    tighten < 1 表示收紧止损（大盘/板块弱时）。
    """
    if stop_loss <= 0 or avg_cost <= 0 or price <= 0:
        return False, stop_loss
    loss_width = avg_cost - stop_loss
    effective_sl = avg_cost - loss_width * tighten
    floor = stop_loss * 0.85  # 不低于原止损 85%，避免过度敏感
    trigger_price = max(effective_sl, floor)
    if price <= trigger_price:
        return True, round(trigger_price, 2)
    return False, round(effective_sl, 2)


def check_time_stop(
    pos: Position, hold_days: int, max_days: int = 5, min_loss: float = -0.03
) -> str:
    """时间止损：持有超 N 天且亏损超过阈值才触发。"""
    if hold_days > max_days and pos.pnl_pct < min_loss:
        return f"时间止损 (持有 {hold_days} 天，亏损 {pos.pnl_pct:.1%})"
    return ""
