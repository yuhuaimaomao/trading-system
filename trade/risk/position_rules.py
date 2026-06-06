"""持仓风控决策 — 单只持仓的止损/止盈/移动止盈/回撤止盈判断。

纯规则函数，不依赖 Watcher state。由 position_risk._check_positions 调用。
"""

from trade.risk.rules.stop_loss import should_stop_loss
from trade.risk.rules.take_profit import should_take_profit, should_trailing_stop


def adjust_tightening(risk_level: str, sector_trend: str) -> tuple[float, float, float]:
    """根据大盘+板块计算止损/止盈/移动止盈的收紧系数。"""
    if risk_level == "extreme":
        sl, tp, trail = 0.70, 0.80, 0.70
    elif risk_level == "dangerous":
        sl, tp, trail = 0.85, 0.90, 0.85
    elif risk_level == "cautious":
        sl, tp, trail = 0.92, 1.0, 0.92
    else:
        sl, tp, trail = 1.0, 1.0, 1.0

    is_weak = any(w in sector_trend for w in ("持续走弱", "弱于大盘", "普跌"))
    is_accel = "持续走弱" in sector_trend and "加速" in sector_trend

    if is_accel:
        sl *= 0.90; tp *= 0.90; trail *= 0.90
    elif is_weak:
        sl *= 0.95; tp *= 0.95; trail *= 0.95

    return sl, tp, trail


def check_position_stop_loss(
    price: float, avg_cost: float, sl: float, sl_tighten: float,
) -> tuple[bool, float]:
    """检查止损。返回 (triggered, effective_sl)。"""
    return should_stop_loss(price, avg_cost, sl, sl_tighten)


def check_position_take_profit(
    price: float, avg_cost: float, tp: float, tp_lower: float,
) -> bool:
    """检查目标止盈。"""
    return should_take_profit(price, avg_cost, tp, tp_lower)


def check_trailing_stop(
    price: float, highest_price: float, trailing_stop: float, trail_tighten: float,
) -> bool:
    """检查移动止盈。"""
    return should_trailing_stop(price, highest_price, trailing_stop, trail_tighten)


def check_retracement_stop(price: float, highest_price: float,
                           avg_cost: float, risk_level: str) -> bool:
    """检查利润回撤止盈 — 三级分级保护。

    Returns True 表示应触发。
    """
    if highest_price <= 0 or avg_cost <= 0:
        return False

    max_profit_pct = (highest_price - avg_cost) / avg_cost * 100
    if max_profit_pct < 5:
        return False

    current_profit = (price - avg_cost) / avg_cost * 100

    # 大盘加成
    if risk_level == "extreme":
        bonus = 0.10
    elif risk_level == "dangerous":
        bonus = 0.05
    else:
        bonus = 0.0

    if max_profit_pct >= 15:
        keep = 0.60 + bonus
    elif max_profit_pct >= 10:
        keep = 0.55 + bonus
    elif max_profit_pct >= 5:
        keep = 0.50 + bonus
    else:
        return False

    return current_profit < max_profit_pct * keep
