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


def check_time_stop(pos: Position, hold_days: int, max_days: int = 10) -> str:
    """时间止损：持有超 N 天未盈利"""
    if hold_days > max_days and pos.pnl_pct < 0:
        return f"时间止损 (持有 {hold_days} 天，亏损 {pos.pnl_pct:.1%})"
    return ""
