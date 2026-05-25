"""组合绩效追踪"""

from typing import List
import math


def calc_max_drawdown(equity_curve: List[float]) -> float:
    """计算最大回撤"""
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calc_sharpe_ratio(daily_returns: List[float], risk_free_rate: float = 0.03) -> float:
    """计算年化夏普比率"""
    if len(daily_returns) < 2:
        return 0.0
    avg = sum(daily_returns) / len(daily_returns)
    if avg == 0:
        return 0.0
    var = sum((r - avg) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    # 年化
    daily_rf = risk_free_rate / 252
    return (avg - daily_rf) / std * math.sqrt(252)


def calc_win_rate(trades: List[dict]) -> float:
    """计算胜率"""
    if not trades:
        return 0.0
    sells = [t for t in trades if t.get("type") == "sell"]
    if not sells:
        return 0.0
    wins = [t for t in sells if t.get("pnl", 0) > 0]
    return len(wins) / len(sells)


def calc_profit_loss_ratio(trades: List[dict]) -> float:
    """计算盈亏比"""
    sells = [t for t in trades if t.get("type") == "sell"]
    if not sells:
        return 0.0
    wins = [t["pnl"] for t in sells if t.get("pnl", 0) > 0]
    losses = [abs(t["pnl"]) for t in sells if t.get("pnl", 0) < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 1
    return avg_win / avg_loss if avg_loss > 0 else 0.0


def calc_metrics(trades: List[dict], equity_curve: List[float]) -> dict:
    """计算综合绩效指标"""
    daily_returns = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i - 1] > 0:
            daily_returns.append(equity_curve[i] / equity_curve[i - 1] - 1)

    total_return = (equity_curve[-1] / equity_curve[0] - 1) if equity_curve else 0

    return {
        "total_return": total_return,
        "max_drawdown": calc_max_drawdown(equity_curve),
        "sharpe_ratio": calc_sharpe_ratio(daily_returns),
        "win_rate": calc_win_rate(trades),
        "profit_loss_ratio": calc_profit_loss_ratio(trades),
        "total_trades": len([t for t in trades if t.get("type") == "sell"]),
        "volatility": (sum((r - sum(daily_returns) / len(daily_returns)) ** 2
                           for r in daily_returns) / len(daily_returns)) ** 0.5
        if daily_returns else 0,
    }
