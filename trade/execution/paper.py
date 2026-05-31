"""模拟盘自动执行器 — 模拟成交+滑点+佣金+仓位追踪"""

from datetime import datetime

from analysis.signals import OrderSignal
from data.repo import TradeRepository
from system.config.settings import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_SLIPPAGE,
    MIN_COMMISSION,
    STAMP_TAX_RATE,
)
from system.utils.logger import get_system_logger

logger = get_system_logger("paper_executor")


class PaperExecutor:
    def __init__(self, portfolio=None, slippage=None, commission_rate=None, db_path: str = None):
        self.repo = TradeRepository(db_path=db_path)
        self.portfolio = portfolio
        self.slippage = slippage if slippage is not None else DEFAULT_SLIPPAGE
        self.commission_rate = (
            commission_rate if commission_rate is not None else DEFAULT_COMMISSION_RATE
        )

    def execute_buy(
        self,
        signal: OrderSignal,
        current_price: float,
        volume: int = None,
        account: str = "paper",
    ) -> int | None:
        """模拟买入：计算成交价、股数、佣金，执行持仓更新并记录"""
        fill_price = current_price * (1 + self.slippage)
        fill_price = round(fill_price, 2)

        # 计算股数
        if volume is None:
            volume = self._calc_shares(signal, fill_price)
        if volume <= 0:
            logger.warning(f"[Paper] 股数 <= 0，跳过买入 {signal.stock_code}")
            return None

        # 对为100的整数倍
        volume = (volume // 100) * 100
        if volume <= 0:
            return None

        amount = round(fill_price * volume, 2)
        commission = self._calc_commission(amount, is_sell=False)
        total_cost = amount + commission

        # 检查现金
        if self.portfolio is not None:
            if total_cost > self.portfolio.cash:
                logger.warning(
                    f"[Paper] 现金不足 {signal.stock_code}: "
                    f"需 {total_cost:.2f} 仅 {self.portfolio.cash:.2f}"
                )
                return None

        # 记录信号
        signal_dict = {
            "trade_date": datetime.now().strftime("%Y-%m-%d"),
            "created_at": datetime.now().isoformat(),
            "signal_type": signal.signal_type.name,
            "signal_source": signal.source.name,
            "stock_code": signal.stock_code,
            "stock_name": signal.stock_name,
            "buy_zone_min": signal.buy_zone_min,
            "buy_zone_max": signal.buy_zone_max,
            "target_position": signal.target_position,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "trailing_stop": signal.trailing_stop,
            "signal_score": signal.signal_score,
            "strategy_name": signal.strategy_name,
            "reason": signal.reason,
            "status": "executed",
        }
        signal_id = self.repo.insert_signal(signal_dict)

        # 执行组合开仓
        if self.portfolio is not None:
            self.portfolio.open_position(
                stock_code=signal.stock_code,
                stock_name=signal.stock_name,
                volume=volume,
                price=fill_price,
                entry_date=datetime.now().strftime("%Y-%m-%d"),
                stop_loss=signal.stop_loss or 0.0,
                take_profit=signal.take_profit or 0.0,
                trailing_stop=signal.trailing_stop or 0.05,
                commission=round(commission, 2),
            )

        # 记录订单
        order_id = self.repo.insert_order(
            {
                "signal_id": signal_id,
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
                "order_time": datetime.now().isoformat(),
                "stock_code": signal.stock_code,
                "order_type": "buy",
                "order_price": fill_price,
                "order_volume": volume,
                "order_status": "filled",
                "filled_volume": volume,
                "filled_price": fill_price,
                "filled_amount": amount,
                "commission": round(commission, 2),
                "strategy_name": signal.strategy_name,
                "updated_at": datetime.now().isoformat(),
            }
        )
        logger.info(
            f"[Paper] 买入 {signal.stock_code} {volume}股 @{fill_price} 佣金{commission:.2f}"
        )
        return order_id

    def execute_sell(
        self,
        stock_code: str,
        current_price: float,
        volume: int = None,
        reason: str = "",
        account: str = "paper",
    ) -> int | None:
        """模拟卖出：计算成交价、佣金，执行持仓更新并记录"""
        fill_price = current_price * (1 - self.slippage)
        fill_price = round(fill_price, 2)

        pos = None
        if self.portfolio is not None:
            pos = self.portfolio.positions.get(stock_code)
            if pos is None:
                logger.warning(f"[Paper] 无持仓可卖 {stock_code}")
                return None
            if pos.entry_date == datetime.now().strftime("%Y-%m-%d"):
                logger.warning(f"[Paper] T+1 保护，拒绝卖出当日买入的 {stock_code}")
                return None

        if volume is None:
            if pos is not None:
                volume = pos.volume
            else:
                volume = 0
        else:
            if pos is not None:
                volume = min(volume, pos.volume)

        if volume <= 0:
            return None

        amount = round(fill_price * volume, 2)
        commission = self._calc_commission(amount, is_sell=True)

        # 执行组合平仓
        if self.portfolio is not None:
            self.portfolio.close_position(
                stock_code=stock_code,
                price=fill_price,
                reason=reason,
                commission=round(commission, 2),
            )

        # 记录订单（没有 signal_id 时传 0）
        order_id = self.repo.insert_order(
            {
                "signal_id": 0,
                "trade_date": datetime.now().strftime("%Y-%m-%d"),
                "order_time": datetime.now().isoformat(),
                "stock_code": stock_code,
                "order_type": "sell",
                "order_price": fill_price,
                "order_volume": volume,
                "order_status": "filled",
                "filled_volume": volume,
                "filled_price": fill_price,
                "filled_amount": amount,
                "commission": round(commission, 2),
                "strategy_name": "",
                "updated_at": datetime.now().isoformat(),
            }
        )
        logger.info(
            f"[Paper] 卖出 {stock_code} {volume}股 @{fill_price} 佣金{commission:.2f}"
        )
        return order_id

    # ---- 辅助方法 ----

    def _calc_commission(self, amount: float, is_sell: bool = False) -> float:
        """计算手续费：佣金（最低5元）+ 卖出印花税"""
        fee = amount * self.commission_rate
        if is_sell:
            fee += amount * STAMP_TAX_RATE
        return max(fee, MIN_COMMISSION)

    def _calc_shares(self, signal: OrderSignal, price: float) -> int:
        """根据 target_position% 计算买入股数（取整到 100 股）"""
        if self.portfolio is None:
            return 0
        target_pct = signal.target_position or 0.1  # 默认 10%
        target_value = self.portfolio.total_value * target_pct
        shares = int(target_value / price / 100) * 100
        return max(shares, 100)  # 至少 1 手
