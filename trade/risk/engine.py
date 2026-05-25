"""风控引擎：统一编排风控规则，独立于策略层运行"""

from typing import List, Optional
from dataclasses import dataclass

from trade.portfolio.portfolio import Portfolio, Position
from trade.risk.rules.stop_loss import check_stop_loss
from trade.risk.rules.take_profit import check_take_profit, check_trailing_stop
from trade.risk.rules.max_drawdown import check_daily_loss_limit
from trade.risk.rules.concentration import check_concentration
from trade.risk.rules.market_env import get_market_environment, get_max_position
from trade.risk.rules.blacklist import is_blacklisted


@dataclass
class RiskResult:
    allowed: bool
    reason: str = ""
    action: str = ""  # 'reject' | 'close' | 'warn'


class RiskEngine:
    """风控引擎，按优先级执行检查"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.max_single_pct = self.config.get("max_single_pct", 0.20)
        self.max_sector_pct = self.config.get("max_sector_pct", 0.30)
        self.daily_loss_limit = self.config.get("daily_loss_limit", 0.03)
        self.market_env = "swing"

    def update_market_env(self, index_ma20: float, index_price: float):
        self.market_env = get_market_environment(index_price, index_ma20)

    def can_open(self, stock_code: str, target_pct: float,
                 sector_code: str = "", portfolio: Optional[Portfolio] = None) -> RiskResult:
        """开仓前检查（优先级 1-3）"""
        # 优先级 1：黑名单
        if is_blacklisted(stock_code):
            return RiskResult(False, "黑名单标的", "reject")

        # 优先级 2：市场环境
        max_pos = get_max_position(self.market_env)
        if portfolio and portfolio.position_ratio + target_pct > max_pos:
            return RiskResult(
                False,
                f"市场{self.market_env}环境仓位上限{max_pos:.0%}",
                "reject",
            )

        # 优先级 3：集中度
        if portfolio:
            ok, msg = check_concentration(
                stock_code, target_pct, sector_code, portfolio,
                self.max_single_pct, self.max_sector_pct,
            )
            if not ok:
                return RiskResult(False, msg, "reject")

        return RiskResult(True, "通过")

    def check_positions(self, prices: dict, portfolio: Portfolio) -> List[dict]:
        """盘中持仓巡检（优先级 4-7），返回需要平仓的信号列表"""
        close_signals = []

        # 优先级 4：日内熔断
        if portfolio.daily_pnl < 0:
            daily_loss_ratio = abs(portfolio.daily_pnl) / portfolio.total_value
            if daily_loss_ratio > self.daily_loss_limit:
                for code, pos in list(portfolio.positions.items()):
                    if pos.pnl_pct < 0:
                        close_signals.append({
                            "stock_code": code,
                            "reason": f"日内熔断 (日亏损 {daily_loss_ratio:.1%})",
                            "priority": 4,
                        })

        # 优先级 5-7：逐只检查
        for code, pos in list(portfolio.positions.items()):
            price = prices.get(code) or pos.current_price
            pos.update_price(price)

            # 优先级 5：止损
            sl_result = check_stop_loss(pos)
            if sl_result:
                close_signals.append({
                    "stock_code": code,
                    "reason": sl_result,
                    "priority": 5,
                })
                continue

            # 优先级 6：移动止盈
            ts_result = check_trailing_stop(pos)
            if ts_result:
                close_signals.append({
                    "stock_code": code,
                    "reason": ts_result,
                    "priority": 6,
                })
                continue

            # 优先级 7：目标止盈
            tp_result = check_take_profit(pos)
            if tp_result:
                close_signals.append({
                    "stock_code": code,
                    "reason": tp_result,
                    "priority": 7,
                })
                continue

        return close_signals

    def get_risk_status(self, portfolio: Portfolio,
                        prices: Optional[dict] = None) -> dict:
        """获取当前风控状态摘要"""
        if prices:
            portfolio.update_prices(prices)

        sector_exp = portfolio.get_sector_exposure()

        return {
            "market_env": self.market_env,
            "max_position": get_max_position(self.market_env),
            "current_position_ratio": portfolio.position_ratio,
            "total_value": portfolio.total_value,
            "cash": portfolio.cash,
            "daily_pnl": portfolio.daily_pnl,
            "drawdown": portfolio.drawdown,
            "position_count": len(portfolio.positions),
            "sector_exposure": sector_exp,
            "positions": [
                {
                    "code": p.stock_code,
                    "name": p.stock_name,
                    "pnl_pct": p.pnl_pct,
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                }
                for p in portfolio.positions.values()
            ],
        }
