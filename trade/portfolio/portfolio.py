"""组合管理器：持仓、现金、净值追踪"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import json


@dataclass
class Position:
    stock_code: str
    stock_name: str = ""
    volume: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    sector_code: str = ""
    entry_date: str = ""
    stop_loss: float = 0.0
    take_profit: float = 0.0
    trailing_stop: float = 0.05
    highest_price: float = 0.0

    def update_price(self, price: float):
        self.current_price = price
        self.market_value = self.volume * price
        self.pnl = (price - self.avg_cost) * self.volume
        self.pnl_pct = (price - self.avg_cost) / self.avg_cost if self.avg_cost > 0 else 0.0
        if price > self.highest_price:
            self.highest_price = price


@dataclass
class PortfolioSnapshot:
    date: str
    cash: float = 0.0
    positions: List[Position] = field(default_factory=list)
    total_value: float = 0.0
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    drawdown: float = 0.0

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def market_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    def get_sector_exposure(self) -> Dict[str, float]:
        exposure: Dict[str, float] = {}
        for p in self.positions:
            if p.sector_code:
                exposure[p.sector_code] = exposure.get(p.sector_code, 0) + p.market_value
        total = self.market_value or 1
        return {k: v / total for k, v in exposure.items()}

    def to_db_dict(self) -> dict:
        return {
            "trade_date": self.date,
            "total_value": self.total_value,
            "cash": self.cash,
            "market_value": self.market_value,
            "daily_pnl": self.daily_pnl,
            "total_pnl": self.total_pnl,
            "drawdown": self.drawdown,
            "position_count": self.position_count,
            "sector_exposure": json.dumps(self.get_sector_exposure(), ensure_ascii=False),
        }


class Portfolio:
    """组合管理器"""

    def __init__(self, initial_cash: float = 100000):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: Dict[str, Position] = {}
        self._peak_value = initial_cash
        self._prev_total = initial_cash
        self.snapshots: List[PortfolioSnapshot] = []
        self.trade_log: List[dict] = []

    @property
    def total_value(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    @property
    def drawdown(self) -> float:
        if self._peak_value == 0:
            return 0.0
        return (self._peak_value - self.total_value) / self._peak_value

    @property
    def daily_pnl(self) -> float:
        return self.total_value - self._prev_total

    @property
    def total_pnl(self) -> float:
        return self.total_value - self.initial_cash

    @property
    def position_ratio(self) -> float:
        if self.total_value == 0:
            return 0.0
        return sum(p.market_value for p in self.positions.values()) / self.total_value

    def get_sector_exposure(self) -> Dict[str, float]:
        exposure: Dict[str, float] = {}
        base = self.total_value or 1.0
        for p in self.positions.values():
            if p.sector_code:
                exposure[p.sector_code] = exposure.get(p.sector_code, 0) + p.market_value
        return {k: v / base for k, v in exposure.items()}

    def can_open_position(self, stock_code: str, target_pct: float,
                          sector_code: str = "",
                          max_single_pct: float = 0.20,
                          max_sector_pct: float = 0.30) -> tuple[bool, str]:
        """开仓前检查"""
        if stock_code in self.positions:
            return True, ""

        if self.position_ratio + target_pct > 1.0:
            return False, "总仓位超限"

        if target_pct > max_single_pct:
            return False, f"单票 {target_pct:.0%} 超上限 {max_single_pct:.0%}"

        if sector_code:
            exposure = self.get_sector_exposure()
            current = exposure.get(sector_code, 0)
            if current + target_pct > max_sector_pct:
                return False, f"板块 {sector_code} {current + target_pct:.0%} 超上限 {max_sector_pct:.0%}"

        return True, ""

    def open_position(self, stock_code: str, stock_name: str, volume: int,
                      price: float, sector_code: str = "", entry_date: str = "",
                      stop_loss: float = 0.0, take_profit: float = 0.0,
                      trailing_stop: float = 0.05, commission: float = 0.0):
        cost = price * volume + commission
        if cost > self.cash:
            return False

        self.cash -= cost
        pos = Position(
            stock_code=stock_code,
            stock_name=stock_name,
            volume=volume,
            avg_cost=price,
            current_price=price,
            market_value=price * volume,
            sector_code=sector_code,
            entry_date=entry_date,
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop=trailing_stop,
            highest_price=price,
        )
        self.positions[stock_code] = pos
        self.trade_log.append({
            "type": "buy", "stock_code": stock_code, "volume": volume,
            "price": price, "commission": commission, "date": entry_date,
        })
        return True

    def close_position(self, stock_code: str, price: float,
                       reason: str = "", commission: float = 0.0) -> bool:
        pos = self.positions.get(stock_code)
        if not pos:
            return False

        proceeds = price * pos.volume - commission
        self.cash += proceeds
        self.trade_log.append({
            "type": "sell", "stock_code": stock_code, "volume": pos.volume,
            "price": price, "commission": commission, "reason": reason,
            "pnl": pos.pnl, "pnl_pct": pos.pnl_pct,
        })
        del self.positions[stock_code]
        return True

    def update_prices(self, prices: Dict[str, float]):
        for code, pos in self.positions.items():
            if code in prices:
                pos.update_price(prices[code])

    def snapshot(self, date: str) -> PortfolioSnapshot:
        cur = self.total_value
        if cur > self._peak_value:
            self._peak_value = cur

        snap = PortfolioSnapshot(
            date=date,
            cash=self.cash,
            positions=list(self.positions.values()),
            total_value=cur,
            daily_pnl=cur - self._prev_total,
            total_pnl=cur - self.initial_cash,
            drawdown=self.drawdown,
        )
        self._prev_total = cur
        self.snapshots.append(snap)
        return snap
