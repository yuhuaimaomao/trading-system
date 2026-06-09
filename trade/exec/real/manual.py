"""实盘手动执行器 — Telegram 推送 → 用户成交回复 → 记录订单

不下单，只做三件事:
  1. signal 触发时推送 Telegram 通知
  2. 解析用户的成交回复（模拟盘/实盘、成交/未成交）
  3. 记录到 trade_orders 表
"""

import re
from datetime import datetime
from functools import lru_cache
from typing import Optional

from data._base import connect
from data.repo import TradeRepository
from stock.signals import OrderSignal
from system.config import settings
from system.utils.logger import get_trade_logger

logger = get_trade_logger("exec")

# 操作词 + 账户标记（可能被误识别为股票名称的关键词）
_NON_NAME_KEYWORDS = {
    "没成交",
    "未成交",
    "没买到",
    "未买到",
    "没买",
    "买了",
    "成交",
    "买到",
    "买入",
    "已买",
    "模拟盘",
    "模拟",
    "实盘",
    "实际",
    "paper",
    "Paper",
    "real",
    "Real",
}


class ManualExecutor:
    def __init__(self, telegram_bot=None, portfolio=None, db_path: str = None):
        self.repo = TradeRepository(db_path=db_path)
        self.telegram = telegram_bot
        self.portfolio = portfolio
        self._pending_signals: dict[int, dict] = {}

    # ------------------------------------------------------------------
    # 消息解析
    # ------------------------------------------------------------------

    @staticmethod
    @lru_cache(maxsize=settings.NAME_RESOLVE_CACHE_SIZE)
    def _resolve_name(name: str) -> Optional[str]:
        """股票名称 → 代码，从 stock_basic 查最新日期。"""
        from system.config import settings

        try:
            conn = connect(settings.DATABASE_PATH)
            row = conn.execute(
                """SELECT stock_code FROM stock_basic
                   WHERE stock_name=? AND trade_date=(SELECT MAX(trade_date) FROM stock_basic)
                   LIMIT 1""",
                (name,),
            ).fetchone()
            conn.close()
            return row[0] if row else None
        except Exception as e:
            logger.warning(f"名称解析失败 ({name}): {e}")
            return None

    @staticmethod
    def parse_reply(text: str) -> dict:
        """解析用户的成交回复。

        支持格式:
          模拟盘 000001 1000股 12.50      → account='paper', status='filled'
          实盘 000001 800股 12.48         → account='real', status='filled'
          000001 没成交                    → account=None, status='rejected'
          000001 买了 1000股 12.50         → account=None, status='filled'
          拓普集团，72.77 买了500股        → stock_name='拓普集团', status='filled'
          模拟盘 拓普集团 500股 72.77      → name+account

        Returns:
          {stock_code, stock_name, account, status, volume, price}
        """
        result = {
            "stock_code": None,
            "stock_name": None,
            "account": None,
            "status": None,
            "volume": None,
            "price": None,
        }

        # 先尝试六位代码
        code_match = re.search(r"\b(\d{6})\b", text)
        if code_match:
            result["stock_code"] = code_match.group(1)
        else:
            # 先去掉操作词和账户标记再匹配名称，避免"没成交"/"模拟盘"等被误识别
            text_for_name = text
            for kw in _NON_NAME_KEYWORDS:
                text_for_name = text_for_name.replace(kw, "")
            # 匹配 2-4 字中文名称（排除纯数字和标点）
            name_match = re.search(r"([一-鿿]{2,4})", text_for_name)
            if name_match:
                result["stock_name"] = name_match.group(1)

        if any(kw in text for kw in ["模拟盘", "模拟", "paper", "Paper"]):
            result["account"] = "paper"
        elif any(kw in text for kw in ["实盘", "实际", "real", "Real"]):
            result["account"] = "real"

        if any(kw in text for kw in ["没成交", "未成交", "没买到", "未买到", "没买"]):
            result["status"] = "rejected"
            return result

        if any(kw in text for kw in ["买了", "成交", "买到", "买入", "已买"]):
            result["status"] = "filled"

        # 如果啥状态词都没有但有账户标记，默认当成 filled
        if result["account"] and result["status"] is None:
            result["status"] = "filled"

        # 必须先去掉"股"子串，否则"1000股 12.50"会误匹配1000
        text_clean = re.sub(r"\d+\s*股", "", text)
        price_match = re.search(r"(\d+\.?\d{0,2})\s*(?:元|块)", text_clean)
        if price_match:
            result["price"] = float(price_match.group(1))

        vol_match = re.search(r"(\d+)\s*股", text)
        if vol_match:
            result["volume"] = int(vol_match.group(1))

        return result

    def handle_user_reply(self, text: str) -> tuple[str, str] | None:
        """处理用户成交回复，记录到 DB。

        Returns:
            (reply_text, account) 或 None。
        """
        # 只有包含下单相关关键词时才解析，避免把闲聊当指令
        trade_keywords = [
            "模拟盘",
            "实盘",
            "paper",
            "real",
            "买了",
            "成交",
            "买到",
            "买入",
            "没成交",
            "未成交",
        ]
        has_code = bool(re.search(r"\b\d{6}\b", text))
        has_vol = bool(re.search(r"\d+\s*股", text))  # "1000股" 而非 "个股"
        has_keyword = any(kw in text for kw in trade_keywords)
        if not has_code and not has_vol and not has_keyword:
            return None

        parsed = self.parse_reply(text)
        code = parsed["stock_code"]
        name = parsed["stock_name"]
        account = parsed["account"] or "real"  # 默认实盘

        # 名称 → 代码转换
        if not code and name:
            code = self._resolve_name(name)
            if not code:
                logger.warning(f"无法从消息中解析股票: {text}")
                return f"⚠️ 未找到「{name}」对应的股票代码，请用六位代码", account

        if not code:
            logger.warning(f"无法从消息中解析股票代码: {text}")
            return None

        status = parsed["status"]
        price = parsed["price"]
        volume = parsed["volume"]

        now = datetime.now()
        trade_date = now.strftime("%Y-%m-%d")

        if status == "rejected":
            signals = self.repo.get_pending_signals()
            matched = [s for s in signals if s["stock_code"] == code]
            for s in matched:
                self.repo.update_signal_status(s["id"], "rejected")
                logger.info(f"信号 {s['id']} {code} 标记为 rejected（用户未成交）")
            return f"📝 已记录: {code} 未成交", account

        if price is None or volume is None:
            return f"⚠️ 请补充成交信息: {code} X股 X.XX", account

        signals = self.repo.get_pending_signals()
        matched = [s for s in signals if s["stock_code"] == code]
        signal_id = matched[0]["id"] if matched else None
        stock_name = matched[0].get("stock_name", code) if matched else (name or code)

        order_id = self.repo.insert_order(
            {
                "signal_id": signal_id,
                "trade_date": trade_date,
                "order_time": now.isoformat(),
                "stock_code": code,
                "order_type": "buy",
                "order_price": price,
                "order_volume": volume,
                "order_status": "filled",
                "filled_volume": volume,
                "filled_price": price,
                "filled_amount": round(price * volume, 2),
                "strategy_name": "",
                "updated_at": now.isoformat(),
                "account": account,
            }
        )

        logger.info(f"记录 {account} 成交: {code} {volume}股 @{price} order_id={order_id}")

        if signal_id:
            self.repo.update_signal_status(signal_id, "bought")

        return (
            f"✅ 已记录: {code} {stock_name} {account} {volume}股 @{price} | 持续盯盘中",
            account,
        )

    # ------------------------------------------------------------------
    # 信号提交 (Watcher 触发时调用)
    # ------------------------------------------------------------------

    def submit(self, signal: OrderSignal, account: str = "real") -> int:
        """（已废弃 — signal 由 StrategyPipeline 直接入库）
        保留以兼容旧调用方。
        """
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
        self._pending_signals[signal_id] = {
            "stock_code": signal.stock_code,
            "stock_name": signal.stock_name,
            "signal_type": signal.signal_type.name,
        }
        self.notify(signal)
        return signal_id

    def notify(self, signal: OrderSignal):
        if self.telegram is None:
            return
        msg = signal.__repr__()
        self.telegram.send(f"【交易信号】\n{msg}")

    def confirm(self, signal_id: int, price: float, volume: int, code: str = "", name: str = ""):
        """手动确认买入 → 更新状态、记录订单"""
        info = self._pending_signals.get(signal_id, {})
        code = code or info.get("stock_code", "")
        name = name or info.get("stock_name", "")

        self.repo.update_signal_status(signal_id, "executed")

        if self.portfolio is not None and code:
            self.portfolio.open_position(
                stock_code=code,
                stock_name=name,
                volume=volume,
                price=price,
                entry_date=datetime.now().strftime("%Y-%m-%d"),
            )

        order_id = self.repo.insert_order(
            {
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
            }
        )
        return order_id

    def reject(self, signal_id: int):
        self.repo.update_signal_status(signal_id, "rejected")
