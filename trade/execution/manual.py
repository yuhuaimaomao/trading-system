# -*- coding: utf-8 -*-
"""实盘手动执行器 — Telegram 推送 → 用户确认/拒绝 → 记录持仓"""

from datetime import datetime
from data.repo import TradeRepository
from analysis.signals import OrderSignal


class ManualExecutor:
    def __init__(self, telegram_bot=None, portfolio=None):
        self.repo = TradeRepository()
        self.telegram = telegram_bot
        self.portfolio = portfolio
        # 缓存 submit 时的信号信息，确认时可免数据库查询
        self._pending_signals: dict[int, dict] = {}

    def submit(self, signal: OrderSignal, account: str = "real") -> int:
        """推送信号到 Telegram，等待用户确认"""
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
            "status": "pending",
        }
        signal_id = self.repo.insert_signal(signal_dict)

        # 缓存信号信息供 confirm 使用
        self._pending_signals[signal_id] = {
            "stock_code": signal.stock_code,
            "stock_name": signal.stock_name,
            "signal_type": signal.signal_type.name,
        }

        # 推送 Telegram 通知
        self.notify(signal)
        return signal_id

    def notify(self, signal: OrderSignal):
        """发送信号摘要到 Telegram"""
        if self.telegram is None:
            return
        msg = signal.__repr__()
        self.telegram.send(f"【交易信号】\n{msg}")

    def confirm(self, signal_id: int, price: float, volume: int,
                code: str = "", name: str = ""):
        """确认执行买入 → 更新状态、建立持仓、记录订单"""
        info = self._pending_signals.get(signal_id, {})
        code = code or info.get("stock_code", "")
        name = name or info.get("stock_name", "")

        # 更新信号状态
        self.repo.update_signal_status(signal_id, "executed")

        # 建立持仓
        if self.portfolio is not None and code:
            self.portfolio.open_position(
                stock_code=code,
                stock_name=name,
                volume=volume,
                price=price,
                entry_date=datetime.now().strftime("%Y-%m-%d"),
            )

        # 记录订单
        order_id = self.repo.insert_order({
            "signal_id": signal_id,
            "trade_date": datetime.now().strftime("%Y-%m-%d"),
            "order_time": datetime.now().isoformat(),
            "stock_code": code,
            "order_type": "buy",
            "order_price": price,
            "order_volume": volume,
            "order_status": "filled",
            "filled_volume": volume,
            "filled_price": price,
            "filled_amount": round(price * volume, 2),
            "strategy_name": info.get("strategy_name", ""),
            "updated_at": datetime.now().isoformat(),
        })
        return order_id

    def reject(self, signal_id: int):
        """拒绝信号 → 标记为 rejected"""
        self.repo.update_signal_status(signal_id, "rejected")
