"""风控引擎：统一编排风控规则，独立于策略层运行"""

from dataclasses import dataclass
from typing import List, Optional

from trade.paper.portfolio import Portfolio
from trade.risk.rules.blacklist import is_blacklisted, is_risk_suspect
from trade.risk.rules.concentration import check_concentration
from trade.risk.rules.market_env import get_market_environment, get_max_position
from trade.risk.rules.max_drawdown import check_daily_loss_limit
from trade.risk.rules.stop_loss import check_stop_loss, check_time_stop
from trade.risk.rules.take_profit import check_take_profit, check_trailing_stop


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
        self.max_sector_pct = self.config.get("max_sector_pct", 0.70)
        self.daily_loss_limit = self.config.get("daily_loss_limit", 0.03)
        self.market_env = "swing"
        self._regime = None  # MarketRegime | None
        self._halted = False  # 日内熔断标志，阻止当日新建仓

    def update_market_env(
        self,
        index_ma20: float,
        index_price: float,
        index_ma60: float = 0,
        volume_trend: float = 0,
        breadth_ratio: float = 0,
        daily_amplitude: float = 0,
        active_sectors: int = 0,
    ):
        self.market_env = get_market_environment(
            index_price,
            index_ma20,
            index_ma60,
            volume_trend,
            breadth_ratio,
            daily_amplitude,
            active_sectors,
        )

    def set_regime(self, regime):
        """注入 MarketRegime，供 can_open / adjust_stops 使用。"""
        self._regime = regime

    def can_open(
        self,
        stock_code: str,
        target_pct: float,
        sector_code: str = "",
        portfolio: Optional[Portfolio] = None,
        stock_name: str = "",
    ) -> RiskResult:
        """开仓前检查（优先级 1-3）"""
        # 优先级 0：日内熔断
        if self._halted:
            return RiskResult(False, "日内熔断已触发，暂停新建仓", "reject")

        # 优先级 1：黑名单 + ST/风险标的
        if is_blacklisted(stock_code):
            return RiskResult(False, "黑名单标的", "reject")
        if stock_name and is_risk_suspect(stock_name):
            return RiskResult(False, f"风险标的: {stock_name}", "reject")

        # 优先级 2：市场环境（优先用 MarketRegime 的 position_mult）
        if self._regime is not None:
            regime_mult = self._regime.position_mult
            if regime_mult <= 0:
                return RiskResult(
                    False, f"市场{self._regime.pattern}模式禁止开仓", "reject"
                )
            max_pos = get_max_position(self.market_env) * regime_mult
        else:
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
                stock_code,
                target_pct,
                sector_code,
                portfolio,
                self.max_single_pct,
                self.max_sector_pct,
            )
            if not ok:
                return RiskResult(False, msg, "reject")

        return RiskResult(True, "通过")

    def check_positions(
        self, prices: dict, portfolio: Portfolio, trade_date: str = ""
    ) -> List[dict]:
        """盘中持仓巡检（优先级 4-8），返回需要平仓的信号列表"""
        close_signals = []

        # 优先级 4：日内熔断
        if check_daily_loss_limit(
            portfolio.daily_pnl, portfolio.total_value, self.daily_loss_limit
        ):
            self._halted = True
            loss_ratio = abs(portfolio.daily_pnl) / portfolio.total_value
            for code, pos in list(portfolio.positions.items()):
                if pos.pnl_pct < 0:
                    close_signals.append(
                        {
                            "stock_code": code,
                            "reason": f"日内熔断 (日亏损 {loss_ratio:.1%})",
                            "priority": 4,
                        }
                    )

        # 优先级 5-8：逐只检查
        for code, pos in list(portfolio.positions.items()):
            price = prices.get(code) or pos.current_price
            pos.update_price(price)

            # 优先级 5：止损
            sl_result = check_stop_loss(pos)
            if sl_result:
                close_signals.append(
                    {
                        "stock_code": code,
                        "reason": sl_result,
                        "priority": 5,
                    }
                )
                continue

            # 优先级 6：移动止盈
            ts_result = check_trailing_stop(pos)
            if ts_result:
                close_signals.append(
                    {
                        "stock_code": code,
                        "reason": ts_result,
                        "priority": 6,
                    }
                )
                continue

            # 优先级 7：目标止盈
            tp_result = check_take_profit(pos)
            if tp_result:
                close_signals.append(
                    {
                        "stock_code": code,
                        "reason": tp_result,
                        "priority": 7,
                    }
                )
                continue

            # 优先级 8：时间止损（持有超 5 天仍在亏损）
            if trade_date and pos.entry_date:
                try:
                    from datetime import date

                    ed = date.fromisoformat(pos.entry_date)
                    td = date.fromisoformat(trade_date)
                    hold_days = (td - ed).days
                    tstop = check_time_stop(pos, hold_days)
                    if tstop:
                        close_signals.append(
                            {
                                "stock_code": code,
                                "reason": tstop,
                                "priority": 8,
                            }
                        )
                        continue
                except (ValueError, TypeError):
                    pass

        return close_signals

    def adjust_stops(self, portfolio, prices: dict, pos_meta: dict = None):
        """按 MarketRegime 的 stop_mult 动态调整持仓止损。

        stop_mult > 1 → 放宽止损（宽幅震荡/恐慌中避免被震出）
        stop_mult < 1 → 收紧止损（死猫跳/倒V中快跑）

        pos_meta: {code: {sl, tp, ...}} 盯盘决策数据，止损调整直接写入 pos_meta["sl"]。
        """
        if self._regime is None:
            return
        if pos_meta is None:
            return
        mult = self._regime.stop_mult
        if mult == 1.0:
            return
        for code, pos in list(portfolio.positions.items()):
            price = prices.get(code) or pos.current_price
            if price <= 0:
                continue
            meta = pos_meta.get(code, {})
            orig_sl = meta.get("sl", 0)
            if orig_sl <= 0:
                continue
            base_distance = abs(price - orig_sl) / price
            new_distance = base_distance * mult
            new_sl = price * (1 - new_distance)
            pos_meta[code]["sl"] = round(new_sl, 2)

    def evaluate_existing(self, portfolio: Portfolio, prices: dict) -> list[dict]:
        """按 MarketRegime 的 urgent_action 评估持仓处置。

        返回需要执行的处置列表：{code, action, reason}
        """
        if self._regime is None:
            return []
        action = self._regime.urgent_action
        if not action:
            return []
        results = []
        for code, pos in list(portfolio.positions.items()):
            price = prices.get(code) or pos.current_price
            if action == "emergency_exit":
                results.append(
                    {
                        "stock_code": code,
                        "action": "emergency_close",
                        "reason": f"市场{self._regime.pattern}触发紧急平仓",
                        "price": price,
                    }
                )
            elif action == "reduce_positions" and pos.pnl_pct < 0:
                results.append(
                    {
                        "stock_code": code,
                        "action": "reduce",
                        "reason": f"市场{self._regime.pattern}建议减仓",
                        "price": price,
                    }
                )
            elif action == "tighten_stops" and pos.pnl_pct > 0.03:
                results.append(
                    {
                        "stock_code": code,
                        "action": "tighten_stop",
                        "reason": f"市场{self._regime.pattern}收紧止盈",
                        "price": price,
                    }
                )
        return results

    def get_risk_status(
        self, portfolio, prices: Optional[dict] = None, pos_meta: dict = None
    ) -> dict:
        """获取当前风控状态摘要。pos_meta: {code: {sl, tp, ...}}。"""
        if prices:
            portfolio.update_prices(prices)

        pos_meta = pos_meta or {}
        positions = []
        for p in portfolio.positions.values():
            meta = pos_meta.get(p.stock_code, {})
            positions.append(
                {
                    "code": p.stock_code,
                    "name": p.stock_name,
                    "pnl_pct": p.pnl_pct,
                    "stop_loss": meta.get("sl", 0),
                    "take_profit": meta.get("tp", 0),
                }
            )

        return {
            "market_env": self.market_env,
            "max_position": get_max_position(self.market_env),
            "current_position_ratio": portfolio.position_ratio,
            "total_value": portfolio.total_value,
            "cash": portfolio.cash,
            "daily_pnl": portfolio.daily_pnl,
            "drawdown": portfolio.drawdown,
            "position_count": len(portfolio.positions),
            "sector_exposure": portfolio.get_sector_exposure(),
            "positions": positions,
        }
