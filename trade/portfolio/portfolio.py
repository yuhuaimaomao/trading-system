"""组合管理器：持仓、现金、净值追踪"""

import json
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Position:
    """模拟盘持仓 — 只记录买卖执行结果，不存决策数据（止损止盈板块由盯盘 _pos_meta 维护）"""

    stock_code: str
    stock_name: str = ""
    volume: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    entry_date: str = ""  # 买入日期
    locked_volume: int = 0  # T+1 锁定的股数（当日买入部分）

    @property
    def available_volume(self) -> int:
        """今日可卖出的股数 = 总持仓 - T+1 锁定"""
        return max(0, self.volume - self.locked_volume)

    def update_price(self, price: float):
        self.current_price = price
        self.market_value = self.volume * price
        self.pnl = (price - self.avg_cost) * self.volume
        self.pnl_pct = (
            (price - self.avg_cost) / self.avg_cost if self.avg_cost > 0 else 0.0
        )


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

    def get_sector_exposure(
        self, sector_map: Dict[str, str] = None
    ) -> Dict[str, float]:
        """板块曝光。需外部传入 {code: sector_code} 映射（模拟盘不存板块）。"""
        exposure: Dict[str, float] = {}
        if sector_map:
            for p in self.positions:
                sec = sector_map.get(p.stock_code, "")
                if sec:
                    exposure[sec] = exposure.get(sec, 0) + p.market_value
        total = self.market_value or 1
        return {k: v / total for k, v in exposure.items()}

    def to_db_dict(self, account: str = "paper") -> dict:
        from datetime import datetime

        return {
            "trade_date": self.date,
            "total_value": self.total_value,
            "cash": self.cash,
            "market_value": self.market_value,
            "daily_pnl": self.daily_pnl,
            "total_pnl": self.total_pnl,
            "drawdown": self.drawdown,
            "position_count": self.position_count,
            "sector_exposure": json.dumps(
                self.get_sector_exposure(), ensure_ascii=False
            ),
            "account": account,
            "created_at": datetime.now().isoformat(),
        }


class Portfolio:
    """组合管理器"""

    def __init__(self, initial_cash: float = 100000):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: Dict[str, Position] = {}
        self._peak_value = 0.0  # 初始为0，由首轮 update_prices/snapshot 设为首个净值
        self._prev_total = initial_cash
        self.snapshots: List[PortfolioSnapshot] = []
        self.trade_log: List[dict] = []

    @property
    def total_value(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    @property
    def drawdown(self) -> float:
        """日内回撤 = Σ((日内最高 - 现价) × 股数)，由 _persist_state 计算"""
        total = 0.0
        for pos in self.positions.values():
            day_high = getattr(pos, "day_high", 0) or pos.current_price
            dd = (day_high - pos.current_price) * pos.volume
            if dd > 0:
                total += dd
        return total

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

    def get_sector_exposure(
        self, sector_map: Dict[str, str] = None
    ) -> Dict[str, float]:
        """板块曝光。需外部传入 {code: sector_code} 映射。"""
        exposure: Dict[str, float] = {}
        if sector_map:
            base = self.total_value or 1.0
            for code, pos in self.positions.items():
                sec = sector_map.get(code, "")
                if sec:
                    exposure[sec] = exposure.get(sec, 0) + pos.market_value
            return {k: v / base for k, v in exposure.items()}
        return {}

    def can_open_position(
        self,
        stock_code: str,
        target_pct: float,
        max_single_pct: float = 0.20,
        max_sector_pct: float = 0.30,
        sector_exposure: Dict[str, float] = None,
    ) -> tuple[bool, str]:
        """开仓前检查。sector_exposure 由外部（盯盘 _pos_meta）计算传入。"""
        if stock_code in self.positions:
            return True, ""

        if self.position_ratio + target_pct > 1.0:
            return False, "总仓位超限"

        if target_pct > max_single_pct:
            return False, f"单票 {target_pct:.0%} 超上限 {max_single_pct:.0%}"

        if sector_exposure:
            for sec, pct in sector_exposure.items():
                if pct + target_pct > max_sector_pct:
                    return (
                        False,
                        f"板块 {sec} {pct + target_pct:.0%} 超上限 {max_sector_pct:.0%}",
                    )

        return True, ""

    def open_position(
        self,
        stock_code: str,
        stock_name: str,
        volume: int,
        price: float,
        entry_date: str = "",
        commission: float = 0.0,
    ):
        cost = price * volume + commission
        if cost > self.cash:
            return False

        self.cash -= cost

        if stock_code in self.positions:
            # 加仓：合并均价和数量
            old = self.positions[stock_code]
            old_total_cost = old.avg_cost * old.volume
            new_total_cost = price * volume + commission
            old.volume += volume
            old.avg_cost = round(
                (old_total_cost + new_total_cost) / old.volume, 2
                if old.volume > 0
                else price
            )
            old.market_value = old.volume * price
            old.current_price = price
            old.locked_volume += volume  # 当日加仓部分 T+1 锁定
            # entry_date 保持最早的
        else:
            actual_avg_cost = round(
                (price * volume + commission) / volume, 2
            ) if volume > 0 else price
            pos = Position(
                stock_code=stock_code,
                stock_name=stock_name,
                volume=volume,
                avg_cost=actual_avg_cost,
                current_price=price,
                market_value=price * volume,
                entry_date=entry_date,
                locked_volume=volume,  # 当日买入，全部 T+1 锁定
            )
            self.positions[stock_code] = pos

        self.trade_log.append(
            {
                "type": "buy",
                "stock_code": stock_code,
                "volume": volume,
                "price": price,
                "commission": commission,
                "date": entry_date,
            }
        )
        return True

    def close_position(
        self, stock_code: str, price: float, reason: str = "", commission: float = 0.0
    ) -> bool:
        pos = self.positions.get(stock_code)
        if not pos:
            return False

        # 先以实际成交价更新，再取 PnL
        pos.update_price(price)
        proceeds = price * pos.volume - commission
        self.cash += proceeds
        self.trade_log.append(
            {
                "type": "sell",
                "stock_code": stock_code,
                "volume": pos.volume,
                "price": price,
                "commission": commission,
                "reason": reason,
                "pnl": pos.pnl,
                "pnl_pct": pos.pnl_pct,
            }
        )
        del self.positions[stock_code]
        return True

    def update_prices(self, prices: Dict[str, float]):
        for code, pos in self.positions.items():
            if code in prices:
                pos.update_price(prices[code])
        # 盘中实时更新峰值（用于最大回撤保护）
        cur = self.total_value
        if cur > self._peak_value:
            self._peak_value = cur

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
