# -*- coding: utf-8 -*-
"""盘中盯盘进程 — cron 拉起后自管理生命周期"""

import logging
import sqlite3
import time
from datetime import datetime, date, time as dt_time

from system.config import settings
from data.repo import TradeRepository
from trade.portfolio.portfolio import Portfolio

logger = logging.getLogger(__name__)

MORNING_START = dt_time(9, 25)
MORNING_END = dt_time(11, 30)
AFTERNOON_START = dt_time(13, 0)
MARKET_CLOSE = dt_time(15, 0)


class Watcher:
    """盘中盯盘进程 — cron 拉起后自管理生命周期"""

    def __init__(self, telegram_bot=None, qmt_quote=None,
                 scan_interval=60, db_path=None):
        self.telegram = telegram_bot
        self.qmt = qmt_quote  # QMT QuoteClient (optional)
        self.scan_interval = scan_interval
        self.db_path = db_path or settings.DATABASE_PATH
        self.portfolio = Portfolio()
        self.repo = TradeRepository()
        self._running = False
        self._trade_date = ""
        self._alerted_signal_ids: set[int] = set()

    # ========================  生命周期  ========================

    def run(self):
        """Main entry point. 开市前等待 -> 盘中循环 -> 收盘保存快照."""
        self._trade_date = datetime.now().strftime('%Y-%m-%d')
        logger.info(f"盯盘进程启动 {self._trade_date}")

        # 9:25 前等待
        while self._before_market():
            wait_sec = min(
                (datetime.combine(date.today(), MORNING_START) - datetime.now()).total_seconds(),
                30,
            )
            if wait_sec > 0:
                time.sleep(wait_sec)

        self._running = True
        scan_count = 0

        while self._running:
            now = datetime.now().time()

            if self._after_market():
                logger.info("收盘，盯盘结束")
                break

            if self._in_lunch_break():
                logger.info("午休，13:00 恢复")
                self._lunch_break()
                continue

            if self._in_trading_hours():
                scan_count += 1
                logger.info(f"扫描 #{scan_count}")
                try:
                    self._scan()
                except Exception as e:
                    logger.error(f"扫描异常: {e}")
                time.sleep(self.scan_interval)
            else:
                time.sleep(5)

        self.portfolio.snapshot(self._trade_date)
        logger.info("盯盘进程退出")

    # ========================  时段判断  ========================

    @staticmethod
    def _in_trading_hours() -> bool:
        now = datetime.now().time()
        if MORNING_START <= now < MORNING_END:
            return True
        if AFTERNOON_START <= now < MARKET_CLOSE:
            return True
        return False

    @staticmethod
    def _in_lunch_break() -> bool:
        now = datetime.now().time()
        return MORNING_END <= now < AFTERNOON_START

    @staticmethod
    def _before_market() -> bool:
        return datetime.now().time() < MORNING_START

    @staticmethod
    def _after_market() -> bool:
        return datetime.now().time() >= MARKET_CLOSE

    @staticmethod
    def _lunch_break():
        """Sleep until 13:00 (called only inside lunch break)."""
        while Watcher._in_lunch_break():
            time.sleep(30)

    # ========================  扫描循环  ========================

    def _scan(self):
        """One scan cycle: fetch prices -> check signals -> check positions."""
        all_codes = self._get_watch_codes()
        if not all_codes:
            return
        prices = self._get_prices(all_codes)
        if not prices:
            logger.warning("无行情数据，跳过本次扫描")
            return
        self._check_signals(prices)
        self._check_positions(prices)

    def _get_watch_codes(self) -> list[str]:
        """Get all stock codes that need watching (signals + positions)."""
        codes: set[str] = set()
        try:
            signals = self.repo.get_pending_signals(self._trade_date)
            for s in signals:
                codes.add(s["stock_code"])
        except Exception as e:
            logger.warning(f"获取待处理信号异常: {e}")
        for code in self.portfolio.positions:
            codes.add(code)
        return list(codes)

    # ========================  行情获取  ========================

    def _get_prices(self, stock_codes: list[str]) -> dict[str, float]:
        """Get current prices. Try QMT first, fallback to DB latest close."""
        if not stock_codes:
            return {}
        prices: dict[str, float] = {}
        if self.qmt:
            try:
                quotes = self.qmt.get_realtime(stock_codes)
                for code in stock_codes:
                    item = quotes.get(code)
                    if item:
                        price = (item.get("last_price") or
                                 item.get("lastPrice") or
                                 item.get("price"))
                        if price is not None:
                            prices[code] = float(price)
            except Exception as e:
                logger.warning(f"QMT获取行情失败: {e}")
        if not prices:
            prices = self._get_close_prices(stock_codes)
        return prices

    def _get_close_prices(self, stock_codes: list[str]) -> dict[str, float]:
        """Fallback: query latest close from stock_basic table."""
        if not stock_codes:
            return {}
        try:
            conn = sqlite3.connect(self.db_path)
            placeholders = ",".join("?" for _ in stock_codes)
            rows = conn.execute(
                f"""SELECT code, close FROM stock_basic
                    WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)
                    AND code IN ({placeholders})""",
                stock_codes,
            ).fetchall()
            conn.close()
            return {row[0]: row[1] for row in rows if row[1] is not None}
        except Exception as e:
            logger.warning(f"DB获取收盘价失败: {e}")
            return {}

    # ========================  信号检查  ========================

    def _check_signals(self, prices: dict[str, float]):
        """Check pending BUY signals against current prices."""
        try:
            signals = self.repo.get_pending_signals(self._trade_date)
        except Exception as e:
            logger.warning(f"获取待处理信号异常: {e}")
            return

        for s in signals:
            sid = s["id"]
            if sid in self._alerted_signal_ids:
                continue

            code = s["stock_code"]
            price = prices.get(code)
            if price is None:
                continue

            buy_min = s.get("buy_zone_min")
            buy_max = s.get("buy_zone_max")
            if buy_min is not None and buy_max is not None:
                if buy_min <= price <= buy_max:
                    name = s.get("stock_name", code)
                    sl = s.get("stop_loss", 0) or 0
                    tp = s.get("take_profit", 0) or 0
                    msg = (
                        f"\U0001f514 买入提醒: {code} {name} "
                        f"现价{price:.2f} 进入买入区间{buy_min:.2f}-{buy_max:.2f} "
                        f"| 止损{sl:.2f} 止盈{tp:.2f}"
                    )
                    self._alert(msg)
                    self._alerted_signal_ids.add(sid)

    # ========================  持仓检查  ========================

    def _check_positions(self, prices: dict[str, float]):
        """Check portfolio positions for SL/TP/trailing stop triggers."""
        for code, pos in list(self.portfolio.positions.items()):
            price = prices.get(code)
            if price is None:
                continue

            # 止损
            if pos.stop_loss > 0 and price <= pos.stop_loss:
                msg = (
                    f"⚠️ 止损触发: {code} {pos.stock_name} "
                    f"现价{price:.2f} 止损价{pos.stop_loss:.2f}"
                )
                self._alert(msg)
                continue

            # 止盈
            if pos.take_profit > 0 and price >= pos.take_profit:
                msg = (
                    f"✅ 止盈触发: {code} {pos.stock_name} "
                    f"现价{price:.2f} 止盈价{pos.take_profit:.2f}"
                )
                self._alert(msg)
                continue

            # 移动止盈 (use previous highest_price before update)
            if pos.trailing_stop > 0 and pos.highest_price > 0:
                trail_price = pos.highest_price * (1 - pos.trailing_stop)
                if price <= trail_price:
                    msg = (
                        f"⚠️ 移动止盈触发: {code} {pos.stock_name} "
                        f"现价{price:.2f} 最高{pos.highest_price:.2f} "
                        f"触发价{trail_price:.2f}"
                    )
                    self._alert(msg)
                    continue

            # Update price and track highest
            pos.update_price(price)

    # ========================  推送  ========================

    def _alert(self, msg: str):
        """Send alert to Telegram, fallback to log."""
        if self.telegram:
            try:
                self.telegram.send(msg)
            except Exception as e:
                logger.error(f"Telegram推送失败: {e}")
        logger.info(f"盯盘提醒: {msg}")
