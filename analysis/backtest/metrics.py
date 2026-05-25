# -*- coding: utf-8 -*-
"""绩效指标计算 — 支持 numpy 加速，兼容纯 Python 列表"""

import math
from datetime import datetime


def calculate_metrics(trades, equity_curve,
                      initial_cash: float = 100_000,
                      risk_free_rate: float = 0.02,
                      trading_days_per_year: int = 252) -> dict:
    """计算标准绩效指标

    Args:
        trades: list[Trade]
        equity_curve: list[dict] with keys date, cash, market_value, total
        initial_cash: 初始资金
        risk_free_rate: 无风险利率（年化）
        trading_days_per_year: 年化交易天数

    Returns:
        dict with:
            total_return, annual_return, sharpe_ratio, max_drawdown,
            win_rate, profit_factor, avg_win, avg_loss,
            total_trades, avg_hold_days
    """
    # --- 基于权益曲线的指标 ---
    if not equity_curve:
        return _empty_result()

    totals = [e["total"] for e in equity_curve]
    final_total = totals[-1]
    total_return = (final_total - initial_cash) / initial_cash if initial_cash > 0 else 0.0

    n_days = len(equity_curve)
    annual_return = ((1 + total_return) ** (trading_days_per_year / n_days) - 1
                     if n_days > 0 else 0.0)

    # 日收益率
    daily_returns = [
        (totals[i] - totals[i - 1]) / totals[i - 1]
        for i in range(1, n_days)
        if totals[i - 1] > 0
    ]

    # 夏普比率
    sharpe = _calc_sharpe(daily_returns, risk_free_rate, trading_days_per_year)

    # 最大回撤
    max_dd = _calc_max_drawdown(totals)

    # --- 基于交易的指标 ---
    total_trades = len(trades)
    if total_trades == 0:
        result = _empty_result()
        result.update({
            "total_return": round(total_return, 4),
            "annual_return": round(annual_return, 4),
            "sharpe_ratio": round(sharpe, 4),
            "max_drawdown": round(max_dd, 4),
            "total_trades": 0,
        })
        return result

    wins = [t for t in trades if t.pnl is not None and t.pnl > 0]
    losses = [t for t in trades if t.pnl is not None and t.pnl <= 0]
    win_rate = len(wins) / total_trades

    sum_wins = sum(t.pnl for t in wins)
    sum_losses = abs(sum(t.pnl for t in losses))
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else (
        float("inf") if sum_wins > 0 else 0.0
    )

    avg_win = sum_wins / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0

    # 平均持仓天数
    avg_hold = _calc_avg_hold_days(trades)

    return {
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "total_trades": total_trades,
        "avg_hold_days": round(avg_hold, 1),
    }


def _calc_sharpe(daily_returns: list[float], risk_free_rate: float,
                 trading_days_per_year: int) -> float:
    """计算夏普比率"""
    n = len(daily_returns)
    if n < 2:
        return 0.0
    rf_daily = risk_free_rate / trading_days_per_year
    excess = [r - rf_daily for r in daily_returns]
    mean_excess = sum(excess) / n
    variance = sum((x - mean_excess) ** 2 for x in excess) / (n - 1)
    std_excess = math.sqrt(variance)
    if std_excess < 1e-12:
        return 0.0
    return (mean_excess / std_excess) * math.sqrt(trading_days_per_year)


def _calc_max_drawdown(totals: list[float]) -> float:
    """计算最大回撤"""
    if len(totals) < 2:
        return 0.0
    peak = totals[0]
    max_dd = 0.0
    for v in totals:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _calc_avg_hold_days(trades) -> float:
    """计算平均持仓天数"""
    hold_days = []
    for t in trades:
        if t.entry_date and t.exit_date:
            try:
                d1 = datetime.strptime(t.entry_date, "%Y-%m-%d")
                d2 = datetime.strptime(t.exit_date, "%Y-%m-%d")
                hold_days.append((d2 - d1).days)
            except ValueError:
                continue
    return sum(hold_days) / len(hold_days) if hold_days else 0.0


def _empty_result() -> dict:
    """返回全零的指标字典"""
    return {
        "total_return": 0.0,
        "annual_return": 0.0,
        "sharpe_ratio": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "total_trades": 0,
        "avg_hold_days": 0.0,
    }
