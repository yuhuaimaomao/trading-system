# -*- coding: utf-8 -*-
"""简单回测引擎骨架 — 信号驱动，逐日模拟"""

from dataclasses import dataclass, field

from system.config.settings import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_SLIPPAGE,
    MIN_COMMISSION,
    STAMP_TAX_RATE,
    MAX_SINGLE_STOCK_PCT,
)


@dataclass
class BacktestConfig:
    """回测参数"""
    initial_cash: float = 100_000
    commission_rate: float = DEFAULT_COMMISSION_RATE
    slippage: float = DEFAULT_SLIPPAGE
    stamp_tax_rate: float = STAMP_TAX_RATE  # sell only
    min_commission: float = MIN_COMMISSION
    max_position_pct: float = MAX_SINGLE_STOCK_PCT
    max_total_position: float = 0.80


@dataclass
class Trade:
    """单笔交易记录"""
    stock_code: str
    entry_date: str
    exit_date: str | None = None
    entry_price: float = 0.0
    exit_price: float | None = None
    shares: int = 0
    pnl: float | None = None
    pnl_pct: float | None = None
    exit_reason: str = ""


@dataclass
class OrderSignal:
    """回测用交易信号"""
    stock_code: str
    signal_date: str
    stop_loss: float | None = None
    take_profit: float | None = None
    position_pct: float = MAX_SINGLE_STOCK_PCT
    reason: str = ""


class BacktestEngine:
    """简单回测引擎 — 信号驱动，逐日模拟"""

    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.trades: list[Trade] = []
        self.equity_curve: list[dict] = []

    def run(self, signals: list[OrderSignal], data) -> dict:
        """执行回测

        Args:
            signals: OrderSignal 列表，含买入信号和止损止盈
            data: DataLoader.load_daily() 返回的 DataFrame

        Returns:
            get_metrics() 返回的绩效指标
        """
        self.trades.clear()
        self.equity_curve.clear()

        import pandas as pd

        dates = sorted(data["trade_date"].unique())

        # 构建按股票索引的数据
        stock_data: dict[str, pd.DataFrame] = {}
        for sc in data["stock_code"].unique():
            sdf = data[data["stock_code"] == sc].set_index("trade_date").sort_index()
            stock_data[sc] = sdf

        # 为每个信号确定入场日和入场价（次交易日开盘价）
        entry_map: dict[str, list[OrderSignal]] = {}
        for sig in signals:
            entry_date = self._find_entry_date(sig.signal_date, dates, sig.stock_code, stock_data)
            if entry_date is None:
                continue
            sig.entry_date = entry_date
            sig.entry_price = float(stock_data[sig.stock_code].loc[entry_date, "open"])
            entry_map.setdefault(entry_date, []).append(sig)

        # 逐日模拟
        cash = self.config.initial_cash
        positions: dict[str, Trade] = {}

        for i, d in enumerate(dates):
            # --- 入场 ---
            for sig in entry_map.get(d, []):
                if sig.stock_code in positions:
                    continue
                price = sig.entry_price
                allocated = cash * sig.position_pct
                shares = int(allocated / price / 100) * 100
                if shares <= 0:
                    continue
                cost = shares * price
                commission = max(cost * self.config.commission_rate,
                                 self.config.min_commission)
                total_cost = cost + commission
                if total_cost > cash:
                    shares = int((cash - self.config.min_commission) / price / 100) * 100
                    if shares <= 0:
                        continue
                    cost = shares * price
                    commission = max(cost * self.config.commission_rate,
                                     self.config.min_commission)
                    total_cost = cost + commission
                cash -= total_cost
                trade = Trade(
                    stock_code=sig.stock_code,
                    entry_date=d,
                    entry_price=price,
                    shares=shares,
                )
                positions[sig.stock_code] = trade

            # --- 离场 ---
            to_close: list[str] = []
            for sc, trade in list(positions.items()):
                if sc not in stock_data or d not in stock_data[sc].index:
                    continue
                row = stock_data[sc].loc[d]
                low, high, close = float(row["low"]), float(row["high"]), float(row["close"])

                # 找到信号获取止损止盈
                sig = next((s for s in signals if s.stock_code == sc), None)
                sl = sig.stop_loss if sig else None
                tp = sig.take_profit if sig else None

                exit_price = None
                reason = ""
                if sl is not None and low <= sl:
                    exit_price = sl
                    reason = "stop_loss"
                elif tp is not None and high >= tp:
                    exit_price = tp
                    reason = "take_profit"
                elif i == len(dates) - 1:
                    exit_price = close
                    reason = "end_of_period"

                if exit_price is not None:
                    sell_value = trade.shares * exit_price
                    commission = max(sell_value * self.config.commission_rate,
                                     self.config.min_commission)
                    stamp_tax = sell_value * self.config.stamp_tax_rate
                    net_sell = sell_value - commission - stamp_tax
                    buy_commission = max(
                        trade.shares * trade.entry_price * self.config.commission_rate,
                        self.config.min_commission,
                    )
                    trade.exit_date = d
                    trade.exit_price = exit_price
                    trade.exit_reason = reason
                    trade.pnl = net_sell - trade.shares * trade.entry_price - buy_commission
                    trade.pnl_pct = trade.pnl / (trade.shares * trade.entry_price + buy_commission) * 100
                    cash += net_sell
                    to_close.append(sc)

            for sc in to_close:
                self.trades.append(positions.pop(sc))

            # --- 每日权益 ---
            market_value = sum(
                float(stock_data[t.stock_code].loc[d, "close"]) * t.shares
                for t in positions.values()
                if t.stock_code in stock_data and d in stock_data[t.stock_code].index
            )
            self.equity_curve.append({
                "date": d,
                "cash": cash,
                "market_value": market_value,
                "total": cash + market_value,
            })

        return self.get_metrics()

    def _find_entry_date(self, signal_date: str, dates: list,
                         stock_code: str, stock_data: dict):
        """找到信号日之后的首个交易日"""
        for d in dates:
            if d > signal_date and stock_code in stock_data and d in stock_data[stock_code].index:
                return d
        return None

    def get_metrics(self) -> dict:
        """计算并返回绩效指标"""
        if not self.equity_curve:
            return {
                "total_return": 0.0, "annual_return": 0.0,
                "sharpe_ratio": 0.0, "max_drawdown": 0.0,
                "win_rate": 0.0, "profit_factor": 0.0,
                "avg_win": 0.0, "avg_loss": 0.0,
                "total_trades": 0, "avg_hold_days": 0.0,
            }
        from .metrics import calculate_metrics
        return calculate_metrics(
            self.trades, self.equity_curve,
            initial_cash=self.config.initial_cash,
        )
