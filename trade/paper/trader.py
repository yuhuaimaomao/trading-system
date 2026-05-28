# -*- coding: utf-8 -*-
"""模拟盘自动交易 — 信号触发时自动买卖，20万初始资金

费率（A股实际标准）：
  佣金: 万0.85, 最低5元
  印花税: 单边千分之一（卖出征收）
"""

import logging
from datetime import datetime

from data.repo import TradeRepository
from system.config import settings
from trade.portfolio.portfolio import Portfolio

logger = logging.getLogger(__name__)

INITIAL_CAPITAL = 200_000
POSITION_PCT = 0.10  # 每只票占仓位 10%
MAX_POSITIONS = 8
COMMISSION_RATE = 0.000085  # 万0.85
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.001  # 单边千分之一（卖出）


class PaperTrader:
    """模拟盘自动交易器。信号买点触发自动买，止损止盈自动卖。"""

    def __init__(self, db_path: str, telegram_bot=None):
        self.portfolio = Portfolio(initial_cash=INITIAL_CAPITAL)
        self.db_path = db_path
        self.telegram = telegram_bot
        self.trade_date = datetime.now().strftime("%Y-%m-%d")
        self.repo = TradeRepository()

    # ------------------------------------------------------------------
    # 买入
    # ------------------------------------------------------------------

    def try_buy(self, code: str, name: str, price: float,
                buy_min: float, buy_max: float, sl: float, tp: float,
                score: float = 0, source: str = "signal",
                max_amount: float | None = None) -> bool:
        """信号进入买入区间时尝试模拟买入。

        max_amount: 最大买入金额，None 表示用默认仓位比例。
        """
        if code in self.portfolio.positions:
            return False
        if len(self.portfolio.positions) >= MAX_POSITIONS:
            logger.info(f"模拟盘已达最大持仓数 {MAX_POSITIONS}")
            return False

        # 动态仓位：max_amount 优先，否则用默认比例
        if max_amount is not None:
            capital = min(max_amount, self.portfolio.total_value * POSITION_PCT)
        else:
            capital = self.portfolio.total_value * POSITION_PCT

        volume = int(capital / price / 100) * 100
        if volume < 100:
            logger.info(f"模拟盘资金不足买入 {code}")
            return False

        # 买入佣金
        cost = volume * price
        commission = max(cost * COMMISSION_RATE, MIN_COMMISSION)
        total_cost = cost + commission
        if total_cost > self.portfolio.cash:
            volume = int((self.portfolio.cash * 0.9 - commission) / price / 100) * 100
            if volume < 100:
                return False

        ok = self.portfolio.open_position(
            stock_code=code, stock_name=name, volume=volume, price=price,
            entry_date=self.trade_date, stop_loss=sl, take_profit=tp,
            commission=commission,
        )
        if not ok:
            return False

        self._record_order(code, name, "buy", volume, price, source, score,
                           commission=commission)

        pos_count = len(self.portfolio.positions)
        pnl_str = self._portfolio_summary()
        if self.telegram:
            self.telegram.send(
                f"📝 模拟盘买入: {code} {name}\n"
                f"价格 {price:.2f}  {volume}股  金额 {cost:.0f}  佣金 {commission:.1f}\n"
                f"止损 {sl:.2f}  止盈 {tp:.2f}  评分 {score:.0f}\n"
                f"持仓 {pos_count}/{MAX_POSITIONS}  {pnl_str}"
            )
        logger.info(f"模拟盘买入: {code} {name} {volume}股 @{price:.2f}")
        return True

    # ------------------------------------------------------------------
    # 卖出
    # ------------------------------------------------------------------

    def close(self, code: str, price: float, reason: str):
        """止损/止盈触发时平仓。"""
        pos = self.portfolio.positions.get(code)
        if not pos:
            return

        # 卖出佣金 + 印花税
        amount = price * pos.volume
        commission = max(amount * COMMISSION_RATE, MIN_COMMISSION) + amount * STAMP_TAX_RATE

        self.portfolio.close_position(code, price, reason, commission=commission)
        self._record_order(code, pos.stock_name, "sell", pos.volume, price, reason,
                           commission=commission)

        pnl = (price - pos.avg_cost) * pos.volume - commission
        pnl_pct = (price - pos.avg_cost) / pos.avg_cost * 100 if pos.avg_cost else 0
        pnl_str = self._portfolio_summary()
        emoji = "✅" if pnl > 0 else "⚠️"
        if self.telegram:
            self.telegram.send(
                f"{emoji} 模拟盘卖出: {code} {pos.stock_name}\n"
                f"价格 {price:.2f}  {pos.volume}股\n"
                f"成本 {pos.avg_cost:.2f}  盈亏 {pnl:+.0f}({pnl_pct:+.1f}%)\n"
                f"费用 {commission:.1f}  原因: {reason}  {pnl_str}"
            )
        logger.info(f"模拟盘卖出: {code} {pos.stock_name} 盈亏{pnl:+.0f}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_position_summary(self) -> list[str]:
        lines = []
        for code, pos in self.portfolio.positions.items():
            lines.append(
                f"  {code} {pos.stock_name} {pos.volume}股 "
                f"成本{pos.avg_cost:.2f} 现价{pos.current_price:.2f} "
                f"盈亏{pos.pnl:+.0f}({pos.pnl_pct:+.1f}%)"
            )
        return lines

    def _portfolio_summary(self) -> str:
        p = self.portfolio
        return (
            f"总资产 {p.total_value:.0f}  "
            f"现金 {p.cash:.0f}  "
            f"总盈亏 {p.total_pnl:+.0f}({p.total_pnl / INITIAL_CAPITAL * 100:+.1f}%)"
        )

    # ------------------------------------------------------------------
    # 订单记录
    # ------------------------------------------------------------------

    def _record_order(self, code: str, name: str, order_type: str,
                      volume: int, price: float, source: str = "",
                      score: float = 0, commission: float = 0):
        try:
            self.repo.insert_order({
                "trade_date": self.trade_date,
                "order_time": datetime.now().isoformat(),
                "stock_code": code,
                "stock_name": name,
                "order_type": order_type,
                "order_status": "filled",
                "filled_volume": volume,
                "filled_price": price,
                "commission": commission,
                "order_source": f"paper_{source}",
                "signal_id": None,
                "account": "paper",
            })
        except Exception as e:
            logger.warning(f"模拟盘订单记录失败: {e}")
