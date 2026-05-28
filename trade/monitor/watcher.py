# -*- coding: utf-8 -*-
"""盘中盯盘进程 — cron 拉起后自管理生命周期

三层扫描:
  第一层（每轮）: 大盘状态 + 持仓风控 + 信号触发 + 复盘推荐跟踪
  第二层（每5轮）: 板块热度
  第三层（每5轮）: 异动检测
"""

import logging
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, date, time as dt_time

from system.config import settings
from data.repo import TradeRepository
from trade.portfolio.portfolio import Portfolio
from trade.risk.engine import RiskEngine

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(ch)

MORNING_START = dt_time(9, 25)
MORNING_END = dt_time(11, 30)
AFTERNOON_START = dt_time(13, 0)
MARKET_CLOSE = dt_time(15, 0)

# 大盘熔断阈值
INDEX_HALT_PCT = -0.02       # 上证跌幅 > 2%
INDEX_DANGER_PCT = -0.01     # 上证跌破 MA20 且跌幅 > 1%


class Watcher:
    """盘中盯盘进程 — cron 拉起后自管理生命周期"""

    def __init__(self, telegram_bot=None, qmt_quote=None,
                 scan_interval=60, db_path=None):
        self.telegram = telegram_bot
        self.qmt = qmt_quote
        self.scan_interval = scan_interval
        self.db_path = db_path or settings.DATABASE_PATH
        self.portfolio = Portfolio()
        self.repo = TradeRepository()
        self.risk_engine = RiskEngine()
        self._running = False
        self._trade_date = ""
        self._scan_count = 0
        self._triggered_ids: set[int] = set()
        self._alerted_sl_tp: set[str] = set()  # "code:type" 防重复推送

        # 子监控器（懒加载）
        self._review_monitor = None
        self._sector_monitor = None
        self._abnormal_detector = None
        self._receiver = None
        self._executor = None
        self._paper_trader = None

        # 指数日内走势追踪
        self._index_prices: list[float] = []   # 近 N 轮上证价格
        self._index_high: float = 0.0          # 日内最高
        self._index_low: float = 0.0           # 日内最低
        self._index_alerted_downtrend: bool = False
        self._index_last_fluctuation_price: float = 0.0  # 上次波动预警时的价格

        # 全市场快照（每3轮刷新）
        self._market_snapshot: dict[str, dict] = {}

        # 板块趋势跟踪（用于买卖信号时附带板块走势）
        self._sector_trend_history: dict[str, list[float]] = defaultdict(list)
        self._industry_cache: dict[str, str] = {}  # code → industry

        # 指数技术指标拐点检测状态
        self._index_tech_state: dict[str, str | None] = {
            "macd_cross": None, "rsi6_zone": "normal", "rsi12_zone": "normal",
            "kdj_cross": None, "kdj_j_zone": "normal", "divergence": None,
        }

        # 信号/复盘提醒状态（防重复推送）
        self._signal_alert_state: dict[int, tuple[float, bool]] = {}
        self._review_alert_state: dict[str, tuple[float, bool]] = {}
        self._prev_snapshot: dict[str, dict] = {}

        # 缓存（盘中不变化）
        self._ma_baseline_cache: tuple | None = None

        # 止损提醒循环：key → {code, name, type, trigger_price, last_push, status, wake_at}
        self._sl_reminders: dict[str, dict] = {}

        # 涨跌停缓存：code → {limit_up, limit_down, pre_close}
        self._limit_cache: dict[str, dict] = {}

        # 买入后盯盘状态：code → {entry_price, last_alert_scan, status, alert_count}
        self._bought_watch: dict[str, dict] = {}

    # ======================== 生命周期 ========================

    def run(self):
        self._trade_date = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"盯盘进程启动 {self._trade_date}")
        self._restore_positions()

        while self._before_market():
            wait_sec = min(
                (datetime.combine(date.today(), MORNING_START) - datetime.now()).total_seconds(),
                30,
            )
            if wait_sec > 0:
                time.sleep(wait_sec)

        self._running = True

        while self._running:
            if self._after_market():
                logger.info("收盘，盯盘结束")
                break

            if self._in_lunch_break():
                logger.info("午休，13:00 恢复")
                self._lunch_break()
                continue

            if self._in_trading_hours():
                self._scan_count += 1
                logger.info(f"扫描 #{self._scan_count}")
                try:
                    self._scan()
                except Exception as e:
                    logger.error(f"扫描异常: {e}", exc_info=True)
                time.sleep(self.scan_interval)
            else:
                time.sleep(5)

        self.portfolio.snapshot(self._trade_date)
        self._expire_signals()
        logger.info("盯盘进程退出")

    # ======================== 时段判断 ========================

    @staticmethod
    def _in_trading_hours() -> bool:
        now = datetime.now().time()
        return (MORNING_START <= now < MORNING_END or
                AFTERNOON_START <= now < MARKET_CLOSE)

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
        while Watcher._in_lunch_break():
            time.sleep(30)

    # ======================== 主扫描 ========================

    def _scan(self):
        """三层扫描入口。"""
        # 接收用户 Telegram 回复（不依赖行情）
        self._check_replies()

        watch_codes = self._get_watch_codes()
        if not watch_codes:
            return

        prices = self._get_realtime_prices(watch_codes)
        if not prices:
            logger.warning("无实时行情，跳过本轮")
            return

        self.portfolio.update_prices(prices)

        # 每 3 轮拉一次全市场快照
        if self._scan_count % 3 == 0:
            self._refresh_market_snapshot()
            self._update_sector_trends()

        # --- 第一层：每轮必扫 ---
        market_ok = self._check_market_state(prices)
        # 更新风控引擎的市场环境
        if self._index_prices:
            _, _, ma20 = self._get_index_baseline()
            self.risk_engine.update_market_env(ma20, self._index_prices[-1])
        self._check_index_technicals()
        self._check_positions(prices)
        self._check_signals(prices, market_ok)
        self._check_bought_signals(prices)
        self._check_review_picks(prices, market_ok)
        self._check_sl_reminders()  # 止损提醒循环

        # 集合竞价后第一轮：推送汇总决策
        if self._scan_count == 1:
            self._send_opening_decision(prices, market_ok)

        # --- 第二层：每 50 轮（约10分钟）---
        if self._scan_count % 50 == 0 and self._market_snapshot:
            self._check_sector_heat(self._market_snapshot)

        # --- 第三层：每 3 轮 ---
        if self._scan_count % 3 == 0:
            self._check_abnormal(prices)

    # ======================== 持仓恢复 ========================

    def _restore_positions(self):
        """从 trade_orders 恢复持仓（进程重启不丢）。"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            # 取每只票的最新买入记录汇总
            rows = conn.execute(
                """SELECT stock_code, SUM(filled_volume) as total_vol,
                          SUM(filled_price * filled_volume) / SUM(filled_volume) as avg_price
                   FROM trade_orders
                   WHERE order_type='buy' AND order_status='filled'
                     AND filled_volume > 0
                   GROUP BY stock_code"""
            ).fetchall()
            if not rows:
                conn.close()
                return

            for row in rows:
                code = row["stock_code"]
                vol = row["total_vol"]
                price = row["avg_price"]
                if vol <= 0:
                    continue

                # 从 signal 取止盈止损
                sig = conn.execute(
                    """SELECT stock_name, stop_loss, take_profit
                       FROM trade_signals WHERE stock_code=? AND status='bought'
                       ORDER BY id DESC LIMIT 1""",
                    (code,),
                ).fetchone()

                name = sig["stock_name"] if sig else code
                sl = sig["stop_loss"] if sig else 0
                tp = sig["take_profit"] if sig else 0

                self.portfolio.open_position(
                    stock_code=code,
                    stock_name=name,
                    volume=vol,
                    price=price,
                    entry_date=self._trade_date,
                    stop_loss=sl or 0,
                    take_profit=tp or 0,
                )
                logger.info(f"恢复持仓: {code} {name} {vol}股 @{price:.2f}")

            conn.close()
        except Exception as e:
            logger.warning(f"恢复持仓失败: {e}")

    # ======================== 关注清单 ========================

    def _get_watch_codes(self) -> list[str]:
        codes: set[str] = set()

        try:
            signals = self.repo.get_pending_signals()
            for s in signals:
                codes.add(s["stock_code"])
        except Exception as e:
            logger.warning(f"获取待处理信号异常: {e}")

        for code in self.portfolio.positions:
            codes.add(code)

        # 复盘推荐标的
        try:
            picks = self._load_review_picks()
            for p in picks:
                codes.add(p["stock_code"])
        except Exception as e:
            logger.warning(f"获取复盘推荐异常: {e}")

        return list(codes)

    # ======================== 行情获取 ========================

    def _get_realtime_prices(self, stock_codes: list[str]) -> dict[str, float]:
        """获取实时价格。QuoteClient 自动处理代码后缀（.SH/.SZ）。"""
        if not self.qmt:
            return {}

        try:
            quotes = self.qmt.get_realtime(stock_codes)
        except Exception as e:
            logger.warning(f"QMT 行情获取失败: {e}")
            return {}

        prices: dict[str, float] = {}
        for code in stock_codes:
            item = quotes.get(code)
            if item:
                price = item.get("lastPrice")
                if price is None:
                    price = item.get("last_price")
                if price is None:
                    price = item.get("price")
                if price is not None:
                    prices[code] = float(price)

                # 涨跌停价
                pre_close = item.get("preClose") or item.get("pre_close") or 0
                if pre_close > 0:
                    limit_pct = 0.20 if code.startswith(("688", "300")) else 0.10
                    self._limit_cache[code] = {
                        "limit_up": round(pre_close * (1 + limit_pct), 2),
                        "limit_down": round(pre_close * (1 - limit_pct), 2),
                        "pre_close": pre_close,
                    }
        return prices

    @staticmethod
    def _get_limit_pct(code: str) -> float:
        """涨跌停幅度：科创/创业板20%，其余10%。"""
        return 0.20 if code.startswith(("688", "300")) else 0.10

    def _is_limit_up(self, code: str, price: float) -> bool:
        """判断是否涨停。"""
        info = self._limit_cache.get(code)
        if not info:
            return False
        return price >= info["limit_up"] * 0.995  # 留0.5%容差

    def _is_limit_down(self, code: str, price: float) -> bool:
        """判断是否跌停。"""
        info = self._limit_cache.get(code)
        if not info:
            return False
        return price <= info["limit_down"] * 1.005

    # ======================== 全市场快照 ========================

    def _refresh_market_snapshot(self):
        """每 5 轮拉一次全市场行情，只提取用到的字段（price/timestamp/changePct）。"""
        if self.qmt is None:
            return
        try:
            from data.live.quotes import QuoteClient
            if hasattr(QuoteClient, 'get_all_quotes'):
                raw = self.qmt.get_all_quotes()
                if raw:
                    ts = time.time()
                    self._market_snapshot = {}
                    for code, item in raw.items():
                        price = item.get("lastPrice") or item.get("last_price") or item.get("price")
                        if price is None:
                            continue
                        self._market_snapshot[code] = {
                            "price": float(price),
                            "timestamp": ts,
                            "changePct": item.get("changePct") or item.get("change_pct") or 0,
                        }
        except Exception as e:
            logger.warning(f"全市场快照获取失败: {e}")

    def _compute_breadth(self) -> dict:
        """从全市场快照计算涨跌家数。"""
        if not self._market_snapshot:
            return {}
        up = down = flat = 0
        for code, item in self._market_snapshot.items():
            chg = item.get("changePct", 0)
            try:
                chg = float(chg)
            except (ValueError, TypeError):
                continue
            if chg > 0:
                up += 1
            elif chg < 0:
                down += 1
            else:
                flat += 1
        return {"up": up, "down": down, "flat": flat}

    # ======================== 第一层：大盘状态 ========================

    def _get_index_quote(self) -> dict | None:
        """单独获取上证指数实时行情（避免 000001 与平安银行冲突）。"""
        if not self.qmt:
            return None
        try:
            result = self.qmt._client.quote("000001.SH")
            data = result.get("data", {})
            price = data.get("lastPrice") or data.get("last_price")
            if not price:
                return None
            return {
                "price": float(price),
                "pre_close": float(data.get("preClose") or 0),
                "change_pct": float(data.get("changePct") or 0) / 100,
            }
        except Exception:
            return None

    def _classify_market_pattern(self) -> str:
        """识别市场模式：normal / v_reversal / dead_cat / one_sided / panic。

        基于日内价格轨迹三段分析，而非机械阈值。
        - panic: 加速下跌+价格逼近日内低点
        - v_reversal: 前跌后涨，价格回升到50%分位以上
        - dead_cat: 反弹但没回到50%分位，且均价仍在低位
        - one_sided: 三段均价逐段走低
        """
        px = self._index_prices
        if len(px) < 30:
            return "normal"

        n = len(px)
        seg_size = max(n // 3, 10)
        seg1 = px[-3 * seg_size : -2 * seg_size]
        seg2 = px[-2 * seg_size : -seg_size]
        seg3 = px[-seg_size:]

        avg1, avg2, avg3 = (sum(s) / len(s) for s in (seg1, seg2, seg3))
        hi, lo = self._index_high, self._index_low
        if hi <= lo:
            return "normal"

        cur = px[-1]
        range_pct = (hi - lo) / lo

        # 恐慌：加速下跌 + 价格在低位
        if range_pct > 0.015 and cur < lo + (hi - lo) * 0.1:
            drop1 = max(0, (seg1[0] - seg1[-1]) / seg1[0]) if seg1[0] > 0 else 0
            drop3 = max(0, (seg3[0] - seg3[-1]) / seg3[0]) if seg3[0] > 0 else 0
            if drop3 > drop1 * 1.5 and drop3 > 0.005:
                return "panic"

        # V 型反转：中段低谷 + 后段回升 + 价格在50%分位以上
        if avg1 > avg2 < avg3:
            recovery = (avg3 - avg2) / avg2 if avg2 > 0 else 0
            if recovery > 0.003 and cur > lo + (hi - lo) * 0.5:
                return "v_reversal"

        # 死猫跳：有反弹但未突破50%分位，且均价仍低于前段
        if avg1 > avg2 < avg3:
            recovery = (avg3 - avg2) / avg2 if avg2 > 0 else 0
            if recovery > 0.002 and cur <= lo + (hi - lo) * 0.5 and avg3 < avg1:
                return "dead_cat"

        # 单边下跌：三段逐次走低
        if avg1 > avg2 > avg3:
            decline = (avg1 - avg3) / avg1 if avg1 > 0 else 0
            if decline > 0.005:
                return "one_sided"

        return "normal"

    def _check_market_state(self, prices: dict[str, float]) -> bool:
        """检测上证指数，返回 True=正常买入，False=暂停买入。

        智能分层：
        - 正常: 满额买入
        - V型反转: 允许买入（反转确认中）
        - 死猫跳/单边下跌: 暂停
        - 恐慌: 全部停止 + 建议减仓
        """
        pattern = self._classify_market_pattern()
        idx = self._get_index_quote()
        if idx is None:
            return True
        index_price = idx["price"]

        # 更新日内高低点
        if self._index_high == 0 or index_price > self._index_high:
            self._index_high = index_price
        if self._index_low == 0 or index_price < self._index_low:
            self._index_low = index_price
        self._index_prices.append(index_price)
        if len(self._index_prices) > 60:
            self._index_prices = self._index_prices[-60:]

        prev_close = idx["pre_close"]
        change_pct = idx["change_pct"]
        if prev_close <= 0:
            return True

        # MA 均线从 DB 获取
        _, _, ma20 = self._get_index_baseline()

        if change_pct < INDEX_HALT_PCT:
            self._alert(
                f"🚨 大盘熔断: 上证跌幅 {change_pct:.1%}，暂停所有买入信号"
            )
            return False

        # 智能模式分层
        if pattern == "panic":
            self._alert(
                f"🚨 恐慌下跌: 上证 {index_price:.2f} 加速下探，"
                f"日内振幅{((self._index_high-self._index_low)/self._index_low*100):.1f}%，"
                f"建议暂停所有买入，考虑减仓"
            )
            return False

        if pattern == "one_sided":
            if not self._index_alerted_downtrend:
                self._index_alerted_downtrend = True
                self._alert(
                    f"⚠️ 单边下跌: 上证 {index_price:.2f}，重心持续下移，暂停买入"
                )
            return False

        if pattern == "dead_cat":
            if not self._index_alerted_downtrend:
                self._index_alerted_downtrend = True
                self._alert(
                    f"⚠️ 弱势反弹(疑似死猫跳): 上证 {index_price:.2f}，"
                    f"反弹未过50%分位，暂不跟进"
                )
            return False

        if pattern == "v_reversal":
            if self._index_alerted_downtrend:
                self._index_alerted_downtrend = False
                self._alert(
                    f"🔄 V型反转迹象: 上证 {index_price:.2f} 深跌后回升至50%分位以上，"
                    f"恢复买入信号"
                )
            # 允许买入，但标记谨慎
            return True

        # 传统阈值补充判断
        if index_price < ma20 and change_pct < INDEX_DANGER_PCT:
            self._alert(
                f"⚠️ 大盘偏弱: 上证 {index_price:.2f} 跌破 MA20({ma20:.2f})，"
                f"跌幅 {change_pct:.1%}，暂停买入"
            )
            return False

        if self._is_index_downtrend():
            if not self._index_alerted_downtrend:
                self._index_alerted_downtrend = True
                self._alert(
                    f"⚠️ 大盘单边下跌: 上证 {index_price:.2f}，"
                    f"日内高{self._index_high:.2f} 低{self._index_low:.2f}，"
                    f"重心持续下移，暂停买入"
                )
            return False

        # 三轮扫描波动预警（≥0.5%）+ AI 技术分析
        if len(self._index_prices) >= 4:
            price_3_ago = self._index_prices[-4]
            fluctuation = (index_price - price_3_ago) / price_3_ago
            if abs(fluctuation) >= 0.005:
                # 价格偏离上次预警超过 0.3% 才重新推送，避免重复
                last = self._index_last_fluctuation_price
                if last == 0 or abs((index_price - last) / last) >= 0.003:
                    self._index_last_fluctuation_price = index_price
                    direction = "急拉" if fluctuation > 0 else "急跌"
                    base_msg = (
                        f"⚡ 大盘波动预警: 上证 {index_price:.2f} 近3轮{direction} {fluctuation:+.2%}\n"
                        f"当前涨幅 {change_pct:+.2%}"
                    )
                    # AI 技术分析
                    ai_analysis = self._analyze_index_fluctuation()
                    if ai_analysis:
                        self._alert(f"{base_msg}\n\n🤖 AI技术研判:\n{ai_analysis}")
                    else:
                        self._alert(base_msg)

        return True

    def _is_index_downtrend(self) -> bool:
        """结构性判断单边下跌:
        1. 当前价在日内区间下 1/3
        2. 重心持续下移（近10轮均价 < 前10轮均价）
        3. 跌家数 > 2 × 涨家数（全市场普跌确认）
        """
        prices = self._index_prices
        if len(prices) < 20:
            return False

        hi = self._index_high
        lo = self._index_low
        if hi <= lo:
            return False

        cur = prices[-1]
        if cur > lo + (hi - lo) / 3:
            self._index_alerted_downtrend = False
            return False

        first_avg = sum(prices[-20:-10]) / 10
        second_avg = sum(prices[-10:]) / 10
        if second_avg >= first_avg:
            self._index_alerted_downtrend = False
            return False

        # 涨跌比确认：跌家数必须显著多于涨家数
        # up=0 全市场普跌，无需比例检查直接确认为单边下跌
        breadth = self._compute_breadth()
        if breadth:
            up, down = breadth.get("up", 0), breadth.get("down", 0)
            if up > 0 and down <= up * 2:
                return False  # 跌家不够多，不是全面下跌

        return True

    def _check_index_technicals(self):
        """检测指数分钟级技术指标拐点（MACD交叉/RSI极值/KDJ交叉/背离）。"""
        prices = self._index_prices
        if len(prices) < 30:
            return

        # 构建分钟级K线
        window = 5
        closes, highs, lows = [], [], []
        for i in range(0, len(prices) - window + 1, window):
            chunk = prices[i:i + window]
            closes.append(chunk[-1])
            highs.append(max(chunk))
            lows.append(min(chunk))
        if len(closes) < 26:
            return

        from analysis.screening.indicators import (
            calc_macd, calc_rsi, calc_kdj,
            calc_macd_series, detect_macd_cross, detect_divergence,
        )

        macd = calc_macd(closes)
        rsi6 = calc_rsi(closes, 6)
        rsi12 = calc_rsi(closes, 12)
        kdj = calc_kdj(highs, lows, closes)

        macd_series = calc_macd_series(closes)
        crosses = detect_macd_cross(macd_series["dif"], macd_series["dea"], lookback=5)
        divergences = detect_divergence(closes, macd_series["dif"], lookback=30)

        st = self._index_tech_state
        alerts = []

        # --- MACD 交叉 ---
        recent_cross = crosses[-1] if crosses else None
        if recent_cross:
            cross_type = "golden" if "金叉" in recent_cross["type"] else "death"
            if st["macd_cross"] != cross_type:
                st["macd_cross"] = cross_type
                days = recent_cross["days_ago"]
                label = "金叉" if cross_type == "golden" else "死叉"
                alerts.append(f"MACD{label}({days}根前) DIF={macd['dif']:.2f} DEA={macd['dea']:.2f}")

        # --- RSI 极值 ---
        for period, val, key in [(6, rsi6, "rsi6_zone"), (12, rsi12, "rsi12_zone")]:
            if val < 20:
                zone = "oversold"
                label = f"RSI{period}超卖({val:.1f})"
            elif val > 80:
                zone = "overbought"
                label = f"RSI{period}超买({val:.1f})"
            else:
                zone = "normal"
            if st[key] != zone and zone != "normal":
                st[key] = zone
                alerts.append(label)
            elif zone == "normal":
                st[key] = "normal"

        # --- KDJ ---
        # J值极值
        if kdj["j"] < 0:
            j_zone = "oversold"
            j_label = f"KDJ J值超卖(K={kdj['k']:.1f} D={kdj['d']:.1f} J={kdj['j']:.1f})"
        elif kdj["j"] > 100:
            j_zone = "overbought"
            j_label = f"KDJ J值超买(K={kdj['k']:.1f} D={kdj['d']:.1f} J={kdj['j']:.1f})"
        else:
            j_zone = "normal"
        if st["kdj_j_zone"] != j_zone and j_zone != "normal":
            st["kdj_j_zone"] = j_zone
            alerts.append(j_label)
        elif j_zone == "normal":
            st["kdj_j_zone"] = "normal"

        # KDJ K/D交叉
        # 用最近2个值判断
        if len(closes) >= 2:
            k_now, d_now = kdj["k"], kdj["d"]
            # 简单判断：K在D上方/下方
            if k_now > d_now:
                kd_cross = "golden"
            elif k_now < d_now:
                kd_cross = "death"
            else:
                kd_cross = None
            if kd_cross and st["kdj_cross"] != kd_cross:
                st["kdj_cross"] = kd_cross
                label = "KDJ金叉" if kd_cross == "golden" else "KDJ死叉"
                alerts.append(f"{label} K={kdj['k']:.1f} D={kdj['d']:.1f} J={kdj['j']:.1f}")

        # --- 背离 ---
        recent_div = divergences[-1] if divergences else None
        if recent_div:
            div_type = "top" if "顶背离" in recent_div["type"] else "bottom"
            if st["divergence"] != div_type:
                st["divergence"] = div_type
                alerts.append(f"{recent_div['type']}: {recent_div['desc']}")

        if alerts:
            current = prices[-1]
            trend_desc = self._index_trend_desc(prices)
            self._alert(
                f"📈 指数技术拐点 上证{current:.2f}\n"
                + "\n".join(f"  • {a}" for a in alerts)
                + f"\n走势: {trend_desc}"
            )

    def _index_trend_desc(self, prices: list[float]) -> str:
        """描述近期走势方向。"""
        if len(prices) < 10:
            return "数据不足"
        half = len(prices) // 2
        first_half = sum(prices[:half]) / half
        second_half = sum(prices[-half:]) / half
        chg = (prices[-1] - prices[0]) / prices[0] * 100
        if second_half > first_half * 1.001:
            direction = "持续上行"
        elif second_half < first_half * 0.999:
            direction = "持续下行"
        else:
            direction = "横盘震荡"
        return f"{direction} 变幅{chg:+.2f}%"

    def _get_index_baseline(self) -> tuple:
        """获取上证指数 MA5/MA10/MA20（盘中不变，首次查询后缓存）。"""
        if self._ma_baseline_cache is not None:
            return self._ma_baseline_cache
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT ma5, ma10, ma20 FROM stock_basic
                   WHERE stock_code='000001'
                   ORDER BY trade_date DESC LIMIT 1"""
            ).fetchone()
            conn.close()
            if row:
                self._ma_baseline_cache = (row[0] or 0, row[1] or 0, row[2] or 0)
                return self._ma_baseline_cache
        except Exception:
            pass
        return (0, 0, 0)

    def _analyze_index_fluctuation(self) -> str | None:
        """大盘波动≥0.5%时，计算分钟级技术指标并调用AI预判走势和企稳点位。"""
        prices = self._index_prices
        if len(prices) < 30:
            return None

        # 每5个扫描数据点≈1分钟，构建分钟K线
        window = 5
        closes = []
        highs = []
        lows = []
        for i in range(0, len(prices) - window + 1, window):
            chunk = prices[i:i + window]
            closes.append(chunk[-1])
            highs.append(max(chunk))
            lows.append(min(chunk))

        if len(closes) < 26:
            return None

        from analysis.screening.indicators import (
            calc_macd, calc_rsi, calc_kdj,
            calc_macd_series, detect_macd_cross, detect_divergence,
        )

        macd = calc_macd(closes)
        rsi6 = calc_rsi(closes, 6)
        rsi12 = calc_rsi(closes, 12)
        rsi24 = calc_rsi(closes, 24)
        kdj = calc_kdj(highs, lows, closes)

        macd_series = calc_macd_series(closes)
        crosses = detect_macd_cross(macd_series["dif"], macd_series["dea"])
        divergences = detect_divergence(closes, macd_series["dif"])

        ma5, ma10, ma20 = self._get_index_baseline()

        current = prices[-1]
        change_from_first = (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] else 0

        # 近10分钟走势
        bar_count = min(10, len(closes))
        recent_bars = []
        for i in range(len(closes) - bar_count + 1, len(closes)):
            chg = (closes[i] - closes[i - 1]) / closes[i - 1] * 100 if closes[i - 1] else 0
            recent_bars.append(f"{closes[i]:.2f}({chg:+.1f}%)")

        # 均线位置
        ma_parts = []
        for label, ma_val in [("MA5", ma5), ("MA10", ma10), ("MA20", ma20)]:
            if ma_val and ma_val > 0:
                pos = "上方" if current > ma_val else "下方"
                ma_parts.append(f"{label}={ma_val:.0f}({pos}{abs(current - ma_val):.0f})")

        cross_info = ", ".join(
            [f"{c['days_ago']}根前{c['type']}" for c in crosses]
        ) if crosses else "近期无交叉"
        div_info = ", ".join([d['type'] for d in divergences]) if divergences else "无背离"

        prompt = f"""分析上证指数当前走势，预判方向和企稳点位。

## 当前状态
指数现价: {current:.2f}
近{len(prices)}轮(约{len(closes)}分钟)总变动: {change_from_first:+.2f}%
日线均线: {', '.join(ma_parts) if ma_parts else '无数据'}

## 分钟级技术指标
MACD: DIF={macd['dif']:.2f} DEA={macd['dea']:.2f} BAR={macd['bar']:.2f}
RSI(6/12/24): {rsi6:.1f}/{rsi12:.1f}/{rsi24:.1f}
KDJ: K={kdj['k']:.1f} D={kdj['d']:.1f} J={kdj['j']:.1f}
交叉: {cross_info}
背离: {div_info}

## 近{bar_count}分钟走势
{', '.join(recent_bars)}

请分析:
1. 这波急跌/急拉会继续还是会反转?
2. 如果继续，到什么点位可能企稳?
3. 当前应该追/等/减/守?

用中文简洁回复，不超过150字。格式:
方向: [继续下跌/继续上涨/即将反弹/即将回调]
企稳点位: [具体点位或区间]
建议: [追/等/减/守]
理由: [一句话]"""

        try:
            from analysis.review.analyzer import AIAnalyzer
            ai = AIAnalyzer()
            result = ai._call_ai(
                prompt,
                system_prompt="你是A股大盘技术分析专家，基于MACD/RSI/KDJ和均线系统做短线预判。简洁、准确、可操作。",
                max_tokens=300,
            )
            if result:
                return result.strip()
        except Exception as e:
            logger.warning(f"AI指数分析失败: {e}")

        return None

    # ======================== 第一层：持仓风控 ========================

    def _check_positions(self, prices: dict[str, float]):
        for code, pos in list(self.portfolio.positions.items()):
            price = prices.get(code)
            if price is None:
                continue

            is_today_buy = pos.entry_date == self._trade_date
            trend = self._get_sector_trend(code)
            limit_down = self._is_limit_down(code, price)

            # T+1 前不触发止损止盈
            if not is_today_buy:
                # 止损
                if pos.stop_loss > 0 and price <= pos.stop_loss:
                    key = f"{code}:sl"
                    self._handle_stop_signal(key, code, pos.stock_name, "止损",
                        price, pos.stop_loss, pos.avg_cost, trend, limit_down)
                    continue

                # 止盈
                if pos.take_profit > 0 and price >= pos.take_profit:
                    key = f"{code}:tp"
                    self._handle_stop_signal(key, code, pos.stock_name, "止盈",
                        price, pos.take_profit, pos.avg_cost, trend, limit_down)
                    continue

                # 移动止盈（T+1 保护，今天买的不可卖）
                if pos.trailing_stop > 0 and pos.highest_price > 0:
                    trail_price = pos.highest_price * (1 - pos.trailing_stop)
                    if price <= trail_price:
                        key = f"{code}:trail"
                        self._handle_stop_signal(key, code, pos.stock_name, "移动止盈",
                            price, trail_price, pos.highest_price, trend, limit_down,
                            extra=f"最高{pos.highest_price:.2f}")
                        continue

                # 利润回撤止盈（分级）
                retrace_key, retrace_signal = self._check_retracement_stop(
                    code, pos.stock_name, price, pos.avg_cost, trend, limit_down)
                if retrace_signal:
                    self._handle_stop_signal(**retrace_signal)
                    continue

            # 更新最高浮盈（即使 T+1 锁定也记录）
            if pos.avg_cost > 0:
                cur_pct = (price - pos.avg_cost) / pos.avg_cost
                watch = self._bought_watch.setdefault(code, {"max_profit_pct": 0})
                if cur_pct > watch.get("max_profit_pct", 0):
                    watch["max_profit_pct"] = cur_pct

            pos.update_price(price)

    def _check_retracement_stop(self, code: str, name: str, price: float,
                                 entry_price: float, trend: str, limit_down: bool):
        """分级利润回撤止盈。

        分级阈值（从 _bought_watch 读取历史最高浮盈）：
        - 最高浮盈 ≥ 15%: 回撤容忍 40%（即保留 60% 利润），触发线 = max * 0.60
        - 最高浮盈 ≥ 10%: 回撤容忍 45%（即保留 55% 利润），触发线 = max * 0.55
        - 最高浮盈 ≥ 5%:  回撤容忍 50%（即保留 50% 利润），触发线 = max * 0.50
        返回 (key, kwargs) 或 (None, None) 表示未触发。
        """
        if entry_price <= 0:
            return None, None

        watch = self._bought_watch.get(code, {})
        max_profit = watch.get("max_profit_pct", 0)
        if max_profit < 0.05:
            return None, None

        current_profit = (price - entry_price) / entry_price

        if max_profit >= 0.15:
            keep_ratio = 0.60
        elif max_profit >= 0.10:
            keep_ratio = 0.55
        else:
            keep_ratio = 0.50

        threshold = max_profit * keep_ratio
        if current_profit >= threshold:
            return None, None

        tier_label = "T1" if max_profit >= 0.15 else "T2" if max_profit >= 0.10 else "T3"
        key = f"{code}:retrace"
        extra = (
            f"{tier_label} 最高浮盈{max_profit*100:.1f}% → 当前{current_profit*100:.1f}%"
            f"（保留{keep_ratio*100:.0f}%利润触发）"
        )
        trigger_price = entry_price * (1 + threshold)
        return key, {
            "key": key, "code": code, "name": name,
            "stype": "利润回撤止盈",
            "price": price, "trigger": trigger_price,
            "ref_price": entry_price,
            "trend": trend, "limit_down": limit_down, "extra": extra,
        }

    def _handle_stop_signal(self, key: str, code: str, name: str, stype: str,
                            price: float, trigger: float, ref_price: float,
                            trend: str, limit_down: bool, extra: str = ""):
        """止损/止盈触发时的统一处理：推送提醒 + 模拟盘执行（实盘等用户确认）。"""
        now = datetime.now()

        # 已在提醒队列中，跳过
        if key in self._sl_reminders:
            return

        chg = (price - ref_price) / ref_price * 100 if ref_price else 0

        if limit_down:
            self._alert(
                f"🚫 {stype}信号但跌停无法卖出: {code} {name}\n"
                f"现价{price:.2f} 触发价{trigger:.2f} 盈亏{chg:+.1f}%{trend}\n"
                f"→ 跌停封单，下一轮继续监控"
            )
            return

        emoji = "⚠️" if stype != "止盈" else "✅"
        extra_str = f" {extra}" if extra else ""
        self._alert(
            f"{emoji} {stype}触发: {code} {name}\n"
            f"现价{price:.2f} 触发价{trigger:.2f} 盈亏{chg:+.1f}%{trend}{extra_str}\n"
            f"┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n"
            f"请确认是否已执行：回复「成交 {code}」\n"
            f"暂不执行：回复「再等 N {code}」（N=分钟数）"
        )

        # 加入提醒队列（5分钟后未确认则再推）
        self._sl_reminders[key] = {
            "code": code, "name": name, "type": stype,
            "price": price, "trigger": trigger, "ref_price": ref_price,
            "last_push": now, "status": "pending",
        }

        # 模拟盘直接执行（实盘等用户确认）
        pt = self._get_paper_trader()
        if pt:
            pt.close(code, price, stype)

    def _check_sl_reminders(self):
        """止损提醒循环：5分钟未确认则重新推送。"""
        now = datetime.now()
        for key, rem in list(self._sl_reminders.items()):
            elapsed = (now - rem["last_push"]).total_seconds()

            if rem["status"] == "waiting":
                if now < rem.get("wake_at", now):
                    continue
                # 等待时间到，恢复提醒
                rem["status"] = "pending"

            if rem["status"] == "pending" and elapsed > 300:
                rem["last_push"] = now
                code = rem["code"]
                name = rem["name"]
                stype = rem["type"]
                price = rem["price"]
                trigger = rem["trigger"]
                self._alert(
                    f"⏰ 再次提醒: {stype}信号 — {code} {name}\n"
                    f"触发价 {trigger:.2f}  上次提醒 {elapsed/60:.0f} 分钟前\n"
                    f"确认已执行 → 回复「成交 {code}」\n"
                    f"暂不执行 → 回复「再等 N {code}」（N=分钟数）"
                )

    def handle_sl_command(self, text: str) -> str:
        """处理用户对止损提醒的回复。

        返回确认消息或空字符串。
        格式：
          成交 CODE — 已手动执行
          再等 N CODE — 等待N分钟后再提醒
        """
        import re
        text = text.strip()

        # 成交确认
        m_done = re.search(r"成交\s*(\d{6})", text)
        if m_done:
            code = m_done.group(1)
            removed = [k for k, v in self._sl_reminders.items() if v["code"] == code]
            for k in removed:
                del self._sl_reminders[k]
            if removed:
                return f"✅ 已确认 {code} 成交，停止提醒"
            return ""

        # 延迟提醒
        m_wait = re.search(r"再等\s*(\d+)\s*(\d{6})?", text)
        if m_wait:
            minutes = int(m_wait.group(1))
            code = m_wait.group(2)
            if code:
                keys = [k for k, v in self._sl_reminders.items() if v["code"] == code]
            else:
                keys = list(self._sl_reminders.keys())

            from datetime import timedelta
            wake = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=minutes)
            for k in keys:
                self._sl_reminders[k]["status"] = "waiting"
                self._sl_reminders[k]["wake_at"] = wake
            return f"⏰ 延迟 {minutes} 分钟后再提醒"

        return ""

    # ======================== 智能仓位计算 ========================

    def _calculate_position_size(self, code: str, price: float,
                                  buy_min: float, buy_max: float,
                                  pattern: str, sector_trend: str) -> tuple[int, str]:
        """根据盘面动态计算买入金额（0-20000），返回 (金额, 决策理由)。

        大盘正常 + 板块走强 + 价格在买入区下沿 → 满额 20000
        大盘谨慎 + 板块走弱 + 价格在买入区上沿 → 减额 5000 或不买
        """
        if pattern in ("panic", "one_sided", "dead_cat"):
            return 0, f"市场{pattern}模式，暂停买入"

        # 基础额度
        if pattern == "v_reversal":
            base = 10000
            reason = "V型反转确认中"
        elif pattern == "normal":
            base = 20000
            reason = "大盘正常"
        else:
            base = 20000
            reason = ""

        # 板块趋势修正
        if "走强" in sector_trend:
            base = min(base * 1.2, 20000)
        elif "走弱" in sector_trend:
            base = max(base * 0.6, 5000)
            reason += " 板块走弱" if reason else "板块走弱"

        # 买入区位置修正（下沿1/3 → 激进，上沿1/3 → 保守）
        zone_range = buy_max - buy_min if buy_max > buy_min else 1
        position_in_zone = (price - buy_min) / zone_range
        if position_in_zone <= 0.33:
            # 价格在买入区下沿，可更激进
            base = min(base * 1.1, 20000)
            reason += " 买入区下沿"
        elif position_in_zone >= 0.67:
            # 价格在买入区上沿，偏保守
            base = max(base * 0.7, 5000)
            reason += " 买入区上沿"

        return int(base // 100 * 100), reason.strip()

    def _analyze_buy_context(self, code: str, price: float,
                              buy_min: float, buy_max: float) -> str:
        """分析买入时的盘面上下文，返回人性化的决策提示。

        结合：趋势方向、买入区位置、布林带位置、是否回踩支撑
        """
        parts = []

        # 1. 买入区位置
        zone_range = buy_max - buy_min if buy_max > buy_min else 1
        zone_pos = (price - buy_min) / zone_range
        if zone_pos <= 0.2:
            parts.append("📍 价格在买入区下沿，安全边际较高")
        elif zone_pos <= 0.5:
            parts.append("📍 价格在买入区中段")
        elif zone_pos <= 0.8:
            parts.append("📍 价格接近买入区上沿，注意追高风险")
        else:
            parts.append("⚠️ 价格在买入区顶部，追高需谨慎")

        # 2. 布林带位置（从数据库获取最近指标）
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_upper, bb_mid, bb_lower, bb_pct_b, ma5, ma10, ma20
                   FROM stock_indicators WHERE stock_code=? AND bb_mid > 0
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            if row:
                bb_upper, bb_mid, bb_lower, pct_b, ma5, ma10, ma20 = row
                if pct_b is not None:
                    if pct_b <= 10:
                        parts.append("📊 布林带：触及下轨（超卖区域，可能反弹）")
                    elif pct_b <= 30:
                        parts.append("📊 布林带：偏下部运行，接近支撑")
                    elif pct_b <= 70:
                        parts.append("📊 布林带：中轨附近运行")
                    elif pct_b <= 90:
                        parts.append("📊 布林带：偏上部运行，接近压力")
                    else:
                        parts.append("📊 布林带：触及上轨，注意回调风险")

                # 3. 均线位置
                ma_parts = []
                for label, ma in [("MA5", ma5), ("MA10", ma10), ("MA20", ma20)]:
                    if ma and ma > 0:
                        pct = (price - ma) / ma * 100
                        side = "上" if pct > 0 else "下"
                        ma_parts.append(f"{label}={ma:.2f}({side}{abs(pct):.1f}%)")
                if ma_parts:
                    parts.append(f"📈 均线: {', '.join(ma_parts)}")

                # 4. 判断回踩支撑
                if bb_lower and price > bb_lower * 0.98 and price < bb_lower * 1.03:
                    parts.append("🟢 回踩布林下轨支撑，反弹概率较高")
                elif ma20 and price > ma20 * 0.98 and price < ma20 * 1.03:
                    parts.append("🟡 回踩MA20支撑位，关注是否站稳")
        except Exception:
            pass

        # 5. 板块叠加
        trend = self._get_sector_trend(code)
        if "走弱" in trend:
            parts.append("⚠️ 板块走弱中，逆势买入需注意风险")
        elif "走强" in trend:
            parts.append("✅ 板块走强，顺势买入")

        return "\n".join(parts)

    # ======================== 第一层：信号触发 ========================

    def _check_signals(self, prices: dict[str, float], market_ok: bool):
        """检查 pending 信号是否进入买入区间。智能仓位 + 盘面上下文分析。"""
        try:
            signals = self.repo.get_pending_signals()
        except Exception as e:
            logger.warning(f"获取待处理信号异常: {e}")
            return

        pattern = self._classify_market_pattern() if market_ok else "halt"

        for s in signals:
            sid = s["id"]
            code = s["stock_code"]
            name = s.get("stock_name", "")
            price = prices.get(code)
            if price is None:
                continue

            buy_min = s.get("buy_zone_min") or 0
            buy_max = s.get("buy_zone_max") or 0
            if buy_min <= 0 or buy_max <= 0:
                continue

            in_zone = buy_min <= price <= buy_max
            prev_state = self._signal_alert_state.get(sid)  # (last_price, was_in_zone)

            # 首次进入买入区，或离开后重新进入
            if in_zone and (prev_state is None or not prev_state[1]):
                if not market_ok:
                    if prev_state is None:
                        self._alert(
                            f"⏸️ 大盘危险，暂停: {code} {name} 现价{price:.2f}"
                        )
                    self._signal_alert_state[sid] = (price, in_zone)
                    continue

                sl = s.get("stop_loss", 0) or 0
                tp = s.get("take_profit", 0) or 0
                trend = self._get_sector_trend(code)

                if self._is_limit_up(code, price):
                    self._alert(
                        f"🚫 涨停无法买入: {code} {name}\n"
                        f"涨停价 {self._limit_cache.get(code, {}).get('limit_up', 0):.2f}{trend}"
                    )
                    self._signal_alert_state[sid] = (price, in_zone)
                    continue

                # 智能仓位计算
                max_amount, size_reason = self._calculate_position_size(
                    code, price, buy_min, buy_max, pattern, trend,
                )
                if max_amount <= 0:
                    self._alert(
                        f"⏸️ 暂不买入: {code} {name}\n"
                        f"原因: {size_reason}  现价{price:.2f} 买入区{buy_min:.2f}-{buy_max:.2f}"
                    )
                    self._signal_alert_state[sid] = (price, in_zone)
                    continue

                # 风控检查（黑名单、市场环境、集中度）
                target_pct = max_amount / self.portfolio.total_value if self.portfolio.total_value > 0 else 0.10
                risk_result = self.risk_engine.can_open(
                    code, target_pct, portfolio=self.portfolio,
                )
                if not risk_result.allowed:
                    self._alert(
                        f"🚫 风控拦截: {code} {name}\n"
                        f"原因: {risk_result.reason}  现价{price:.2f}{trend}"
                    )
                    self._signal_alert_state[sid] = (price, in_zone)
                    continue

                # 买入上下文分析
                context = self._analyze_buy_context(code, price, buy_min, buy_max)

                # 仓位决策理由
                size_info = f"\n💰 仓位: {max_amount}元 ({size_reason})" if size_reason else f"\n💰 仓位: {max_amount}元"

                self._alert(
                    f"🔴 买入信号: {code} {name}\n"
                    f"现价 {price:.2f} 进入买入区间 {buy_min:.2f}-{buy_max:.2f}\n"
                    f"止损 {sl:.2f}  止盈 {tp:.2f}{size_info}{trend}\n"
                    f"┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉\n"
                    f"{context}"
                )
                pt = self._get_paper_trader()
                if pt:
                    bought = pt.try_buy(code, name, price,
                               buy_min, buy_max, sl, tp,
                               score=s.get('signal_score', 0), source='signal',
                               max_amount=max_amount)
                    if bought:
                        # 加入买入后盯盘
                        self._bought_watch[code] = {
                            "entry_price": price,
                            "last_alert_scan": self._scan_count,
                            "status": "watching",
                            "alert_count": 0,
                        }

            self._signal_alert_state[sid] = (price, in_zone)

    def _check_bought_signals(self, prices: dict[str, float]):
        """监控已买入持仓：止损止盈 + 盯盘状态 + 补仓信号。"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT ts.*, buy_info.entry_price, buy_info.buy_time
                   FROM trade_signals ts
                   JOIN (
                       SELECT signal_id,
                              SUM(filled_price * filled_volume) / SUM(filled_volume) as entry_price,
                              MAX(order_time) as buy_time
                       FROM trade_orders
                       WHERE order_type='buy' AND order_status='filled'
                         AND filled_volume > 0
                       GROUP BY signal_id
                   ) buy_info ON buy_info.signal_id = ts.id
                   WHERE ts.status='bought'""",
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.warning(f"获取已买入信号异常: {e}")
            return

        for row in rows:
            s = dict(row)
            code = s["stock_code"]
            name = s.get("stock_name", "")
            price = prices.get(code)
            if price is None:
                continue

            sl = s.get("stop_loss") or 0
            tp = s.get("take_profit") or 0
            entry_price = s.get("entry_price") or 0
            buy_time = s.get("buy_time", "")
            is_today_buy = str(buy_time).startswith(self._trade_date)

            trend = self._get_sector_trend(code)
            limit_down = self._is_limit_down(code, price)

            # T+1 前不触发止损止盈（用与 _check_positions 相同的 key 防重复）
            if not is_today_buy:
                if sl > 0 and price <= sl:
                    self._handle_stop_signal(f"{code}:sl", code, name,
                        "止损", price, sl, entry_price, trend, limit_down)
                    continue
                if tp > 0 and price >= tp:
                    self._handle_stop_signal(f"{code}:tp", code, name,
                        "止盈", price, tp, entry_price, trend, limit_down)
                    continue

                # 利润回撤止盈（分级）
                retrace_key, retrace_signal = self._check_retracement_stop(
                    code, name, price, entry_price, trend, limit_down)
                if retrace_signal:
                    self._handle_stop_signal(**retrace_signal)
                    continue

            # === 买入后盯盘（每50轮~10分钟推送一次状态） ===
            watch = self._bought_watch.setdefault(code, {
                "entry_price": entry_price,
                "last_alert_scan": 0,
                "status": "watching",
                "alert_count": 0,
                "max_profit_pct": 0,
            })
            # 更新入场价（可能从DB恢复）
            if entry_price and not watch["entry_price"]:
                watch["entry_price"] = entry_price

            # 更新最高浮盈
            if entry_price > 0:
                cur_pct = (price - entry_price) / entry_price
                if cur_pct > watch.get("max_profit_pct", 0):
                    watch["max_profit_pct"] = cur_pct

            # 每50轮检查一次，或状态变化时立即推送
            scans_since = self._scan_count - watch["last_alert_scan"]
            pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0

            # 判断当前状态
            new_status = self._classify_holding_status(code, price, entry_price, sl, tp, is_today_buy)
            status_changed = new_status != watch["status"]
            should_alert = scans_since >= 50 or status_changed

            if should_alert and entry_price > 0:
                watch["last_alert_scan"] = self._scan_count
                watch["alert_count"] += 1
                if status_changed:
                    watch["status"] = new_status

                emoji = {"healthy": "✅", "watching": "👀", "trapped": "🔴", "add_opportunity": "🟡"}
                status_labels = {
                    "healthy": "持仓健康",
                    "watching": "持续观察",
                    "trapped": "深度被套",
                    "add_opportunity": "补仓机会",
                }

                day_label = "今日买入" if is_today_buy else f"成本{entry_price:.2f}"
                lines = [
                    f"{emoji.get(new_status, '👀')} 持仓盯盘: {code} {name}",
                    f"现价{price:.2f}  {day_label}  盈亏{pnl_pct:+.1f}%",
                    f"状态: {status_labels.get(new_status, new_status)}  "
                    f"止损{sl:.2f}  止盈{tp:.2f}{trend}",
                ]

                if new_status == "trapped":
                    lines.append("⚠️ 已深度被套，关注是否止损或等待反弹减仓")
                elif new_status == "add_opportunity":
                    # 给出补仓建议
                    add_context = self._analyze_add_context(code, price, entry_price)
                    if add_context:
                        lines.append(add_context)

                self._alert("\n".join(lines))

    def _classify_holding_status(self, code: str, price: float, entry_price: float,
                                   sl: float, tp: float, is_today_buy: bool) -> str:
        """分类持仓状态：healthy / watching / trapped / add_opportunity。

        - healthy: 盈利 > 2% 或在成本附近横盘
        - watching: 小亏 < 2%，正常波动范围内
        - trapped: 亏损超过止损一半以上或深度亏损
        - add_opportunity: 回踩支撑未破，出现反弹迹象
        """
        if entry_price <= 0:
            return "watching"

        pnl_pct = (price - entry_price) / entry_price * 100

        # 深度被套：距离止损不到一半空间
        if sl > 0 and price <= sl * 1.03:
            return "trapped"
        if pnl_pct <= -5:
            return "trapped"

        # 补仓机会：亏损但出现反弹迹象
        if pnl_pct <= -2:
            # 检查是否有反弹迹象：从布林下轨反弹、RSI从超卖回升
            try:
                conn = sqlite3.connect(self.db_path)
                row = conn.execute(
                    """SELECT bb_pct_b, rsi12 FROM stock_indicators
                       WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1""",
                    (code,),
                ).fetchone()
                conn.close()
                if row:
                    pct_b, rsi12 = row[0], row[1]
                    # 从下轨反弹或RSI从超卖区回升
                    if pct_b is not None and 5 <= pct_b <= 30:
                        if rsi12 is not None and rsi12 < 40:
                            return "add_opportunity"
            except Exception:
                pass

        # 健康
        if pnl_pct > 2:
            return "healthy"

        return "watching"

    def _analyze_add_context(self, code: str, price: float, entry_price: float) -> str:
        """分析补仓时机，返回建议文本。"""
        pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        parts = [f"当前亏损{pnl_pct:+.1f}%，成本{entry_price:.2f}"]

        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_lower, bb_mid, ma20, rsi12
                   FROM stock_indicators WHERE stock_code=? AND bb_lower > 0
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            if row:
                bb_lower, bb_mid, ma20, rsi12 = row
                if bb_lower and price <= bb_lower * 1.05:
                    parts.append("📍 价格已触及布林下轨，技术性超卖")
                if ma20 and price < ma20:
                    pct = (ma20 - price) / ma20 * 100
                    parts.append(f"📉 低于MA20={ma20:.2f}约{pct:.1f}%，均线压制中")
                if rsi12 and rsi12 < 35:
                    parts.append(f"📊 RSI(12)={rsi12:.1f}，接近超卖区域")
        except Exception:
            pass

        parts.append("→ 补仓需确认盘面企稳，建议等反弹确认后再操作")
        return "\n".join(parts)

    # ======================== 第一层：复盘推荐跟踪 ========================

    def _check_review_picks(self, prices: dict[str, float], market_ok: bool):
        """复盘推荐盯盘 — 优先级：trade_signals 结构化买入区间 > MA 动态计算。"""
        monitor = self._get_review_monitor()
        if monitor is None:
            return
        if not monitor.is_loaded():
            monitor.load_picks()

        # 加载 REVIEW 信号的结构化买入区间（策略管线入库的，优于 MA 动态计算）
        review_zones = self._load_review_signal_zones()

        for code in monitor.get_codes():
            price = prices.get(code)
            if price is None:
                continue

            # 如果 trade_signals 里已有同 code 的 REVIEW 信号，_check_signals 会处理，这里跳过
            if code in review_zones:
                continue

            buy_min, buy_max = monitor.get_buy_zone(code)
            if buy_min <= 0 or buy_max <= 0:
                continue

            in_zone = buy_min <= price <= buy_max
            prev_state = self._review_alert_state.get(code)

            if in_zone and (prev_state is None or not prev_state[1]):
                if not market_ok:
                    if prev_state is None:
                        pick = monitor.get_pick(code)
                        self._alert(
                            f"⏸️ 大盘危险，暂停: {code} {pick.get('name', '')} 现价{price:.2f}"
                        )
                    self._review_alert_state[code] = (price, in_zone)
                    continue

                pick = monitor.get_pick(code)
                sl = pick.get("stop_loss", 0) or 0
                tp = pick.get("target_price", 0) or 0
                trend = self._get_sector_trend(code)
                self._alert(
                    f"🔴 复盘买入信号: {code} {pick.get('name', '')}\n"
                    f"现价 {price:.2f} 进入买入区间 {buy_min:.2f}-{buy_max:.2f}\n"
                    f"止损 {sl:.2f}  止盈 {tp:.2f}{trend}"
                )
                pt = self._get_paper_trader()
                if pt:
                    pt.try_buy(code, pick.get('name', ''), price,
                               buy_min, buy_max, sl, tp,
                               score=pick.get('score', 0), source='review')

            self._review_alert_state[code] = (price, in_zone)

    # ------------------------------------------------------------------
    # 结构化买入区间加载（优先于 MA 动态计算）
    # ------------------------------------------------------------------

    def _load_review_signal_zones(self) -> dict[str, tuple[float, float, float, float]]:
        """从 trade_signals 加载 REVIEW 信号的结构化买入区间。
        返回 {code: (buy_min, buy_max, sl, tp)}。
        """
        try:
            rows = sqlite3.connect(self.db_path).execute(
                """SELECT stock_code, buy_zone_min, buy_zone_max, stop_loss, take_profit
                   FROM trade_signals
                   WHERE trade_date=? AND signal_source='REVIEW' AND status='pending'""",
                (self._trade_date,),
            ).fetchall()
            return {
                r[0]: (r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0)
                for r in rows if r[1] and r[2]
            }
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 开盘决策汇总（集合竞价后第一轮，替代之前的两个开盘参考）
    # ------------------------------------------------------------------

    def _send_opening_decision(self, prices: dict[str, float], market_ok: bool):
        """集合竞价后推送一条汇总决策：持仓状态 + 买入区信号 + 待观察。"""
        self._ensure_industry_cache()
        idx = self._get_index_quote()
        idx_price = idx["price"] if idx else 0
        chg_pct = idx["change_pct"] if idx else 0
        _, _, ma20 = self._get_index_baseline()
        vs_ma20 = "MA20上方" if ma20 and idx_price >= ma20 else "MA20下方" if ma20 else ""
        market_label = "⚠️大盘危险" if not market_ok else "正常"

        header = f"📋 开盘决策 {self._trade_date}"
        if idx_price:
            header += f" | 上证 {idx_price:.2f} {chg_pct:+.2%} | {vs_ma20} | {market_label}"

        lines = [header, ""]

        # ━━━ 当前持仓 ━━━
        if self.portfolio.positions:
            lines.append("━━━ 当前持仓 ━━━")
            for code, pos in self.portfolio.positions.items():
                price = prices.get(code)
                if price is None:
                    continue
                pnl_pct = (price - pos.avg_cost) / pos.avg_cost * 100 if pos.avg_cost else 0
                pnl_emoji = "🟢" if pnl_pct > 2 else "🟡" if pnl_pct > -2 else "🔴"
                is_today = " 🔒T+1" if pos.entry_date == self._trade_date else ""
                # 检查是否触发止损/止盈
                triggered = ""
                if pos.stop_loss > 0 and price <= pos.stop_loss:
                    triggered = " ⚠️触发止损"
                elif pos.take_profit > 0 and price >= pos.take_profit:
                    triggered = " ✅触发止盈"
                lines.append(
                    f"  {pnl_emoji} {code} {pos.stock_name} 成本{pos.avg_cost:.2f} "
                    f"现价{price:.2f} {pnl_pct:+.1f}% | "
                    f"止损{pos.stop_loss:.2f} 止盈{pos.take_profit:.2f}{is_today}{triggered}"
                )
            lines.append("")

        # ━━━ 信号列表（来自 trade_signals）━━━
        try:
            signals = self.repo.get_pending_signals()
        except Exception:
            signals = []

        buy_list = []    # 已在买入区
        watch_list = []  # 未进入买入区

        for s in signals:
            code = s["stock_code"]
            price = prices.get(code)
            if price is None:
                continue
            buy_min = s.get("buy_zone_min") or 0
            buy_max = s.get("buy_zone_max") or 0
            if buy_min <= 0:
                continue

            in_zone = buy_min <= price <= buy_max
            entry = (code, s.get("stock_name", code), price, buy_min, buy_max,
                     s.get("stop_loss") or 0, s.get("take_profit") or 0,
                     s.get("signal_source", ""), s.get("signal_score", 0))

            if in_zone:
                buy_list.append(entry)
            else:
                watch_list.append(entry)

        if buy_list:
            lines.append("━━━ 买入区信号 ━━━")
            for code, name, price, buy_min, buy_max, sl, tp, source, score in buy_list:
                src_tag = "复盘" if source == "REVIEW" else "AI"
                lines.append(
                    f"  🔴 {code} {name} 现价{price:.2f} "
                    f"买入{buy_min:.2f}-{buy_max:.2f} | "
                    f"止损{sl:.2f} 止盈{tp:.2f} | {src_tag} 评分{score:.0f}"
                )
            lines.append("")

        if watch_list:
            lines.append("━━━ 待观察（未进入买入区）━━━")
            for code, name, price, buy_min, buy_max, sl, tp, source, score in watch_list:
                src_tag = "复盘" if source == "REVIEW" else "AI"
                status = "高于" if price > buy_max else "低于"
                lines.append(
                    f"  👀 {code} {name} 现价{price:.2f} "
                    f"{status}买入区({buy_min:.2f}-{buy_max:.2f}) | {src_tag}"
                )
            lines.append("")

        if not buy_list and not watch_list:
            lines.append("━━━ 今日无待处理信号 ━━━")
            lines.append("")

        # ━━━ 板块集中度提示 ━━━
        from collections import Counter
        industries = []
        for code, _, _, _, _, _, _, _, _ in buy_list + watch_list:
            ind = self._industry_cache.get(code, "")
            if ind:
                industries.append(ind)
        for ind, cnt in Counter(industries).items():
            if cnt >= 3:
                lines.append(f"⚠️ 板块集中度: {ind} {cnt} 只信号，注意分散风险")

        self._alert("\n".join(lines))

    def _update_sector_trends(self):
        """用当前全市场快照更新板块趋势历史（用于买卖信号时附带板块走势）。"""
        if not self._market_snapshot:
            return
        self._ensure_industry_cache()
        sec_changes: dict[str, list[float]] = defaultdict(list)
        for code, item in self._market_snapshot.items():
            industry = self._industry_cache.get(code)
            if not industry:
                continue
            chg = item.get("changePct", 0)
            try:
                chg = float(chg)
            except (ValueError, TypeError):
                chg = 0
            sec_changes[industry].append(chg)
        for ind, changes in sec_changes.items():
            if len(changes) < 3:
                continue
            avg = sum(changes) / len(changes)
            self._sector_trend_history[ind].append(avg)
            if len(self._sector_trend_history[ind]) > 3:
                self._sector_trend_history[ind] = self._sector_trend_history[ind][-3:]

    def _ensure_industry_cache(self):
        """加载代码→行业映射（懒加载一次）。"""
        if self._industry_cache:
            return
        try:
            import sqlite3 as _sql
            conn = _sql.connect(self.db_path)
            rows = conn.execute(
                """SELECT stock_code, industry FROM stock_basic
                   WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
            conn.close()
            self._industry_cache = {r[0]: (r[1] or "") for r in rows}
        except Exception:
            pass

    def _get_sector_trend(self, code: str) -> str:
        """返回股票所在板块的日内趋势描述，用于买卖信号。"""
        self._ensure_industry_cache()
        industry = self._industry_cache.get(code, "")
        if not industry:
            return ""
        history = self._sector_trend_history.get(industry, [])
        if len(history) < 2:
            return ""
        cumulative = history[-1] - history[0]
        if abs(cumulative) < 0.5:
            return f" 板块{industry} 走势平稳"
        direction = "走强" if cumulative > 0 else "走弱"
        arrow = "↑" if cumulative > 0 else "↓"
        return f" 板块{industry} {direction} {arrow}{abs(cumulative):.1f}%"

    def _get_review_monitor(self):
        if self._review_monitor is None:
            try:
                from trade.monitor.review_picks import ReviewPickMonitor
                self._review_monitor = ReviewPickMonitor(
                    db_path=self.db_path,
                    telegram_bot=self.telegram,
                )
            except Exception as e:
                logger.warning(f"复盘推荐监控器初始化失败: {e}")
        return self._review_monitor

    def _load_review_picks(self) -> list[dict]:
        """从 stock_tracker 读最新复盘推荐（含止损/目标价）。"""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT stock_code, stock_name, stop_loss, target_price, abandon_condition
                   FROM stock_tracker
                   WHERE push_date = (
                       SELECT MAX(push_date) FROM stock_tracker WHERE source='复盘'
                   )"""
            ).fetchall()
            conn.close()
            return [{"stock_code": r[0], "stock_name": r[1],
                     "stop_loss": r[2] or 0, "target_price": r[3] or 0,
                     "abandon_condition": r[4] or ""} for r in rows]
        except Exception as e:
            logger.warning(f"加载复盘推荐失败: {e}")
            return []

    # ======================== 第二层：板块热度 ========================

    def _check_sector_heat(self, snapshot: dict[str, dict]):
        monitor = self._get_sector_monitor()
        if monitor is None:
            return
        try:
            messages = monitor.check(snapshot)
            for msg in messages:
                self._alert(msg)
        except Exception as e:
            logger.warning(f"板块热度检查异常: {e}")

    def _get_sector_monitor(self):
        if self._sector_monitor is None:
            try:
                from trade.monitor.sector_heat import SectorHeatMonitor
                self._sector_monitor = SectorHeatMonitor(
                    db_path=self.db_path,
                    telegram_bot=self.telegram,
                )
            except Exception as e:
                logger.warning(f"板块热度监控器初始化失败: {e}")
        return self._sector_monitor

    # ======================== 第三层：异动检测 ========================

    def _check_abnormal(self, prices: dict[str, float]):
        detector = self._get_abnormal_detector()
        if detector is None:
            return
        try:
            if self._market_snapshot:
                current_snapshot = self._market_snapshot  # 已提取好 {price, timestamp, changePct}
            else:
                current_snapshot = self._build_market_snapshot(prices)
            messages = detector.detect_sector(current_snapshot, self._prev_snapshot)
            self._prev_snapshot = current_snapshot
            if messages:
                self._alert("\n".join(messages))
        except Exception as e:
            logger.warning(f"异动检测异常: {e}")

    def _get_abnormal_detector(self):
        if self._abnormal_detector is None:
            try:
                from trade.monitor.abnormal import AbnormalDetector
                self._abnormal_detector = AbnormalDetector(
                    db_path=self.db_path,
                    telegram_bot=self.telegram,
                )
            except Exception as e:
                logger.warning(f"异动检测器初始化失败: {e}")
        return self._abnormal_detector

    @staticmethod
    def _build_market_snapshot(prices: dict[str, float]) -> dict:
        """将当前价格字典转为 snapshot 格式供异动检测器使用。"""
        return {code: {"price": p, "timestamp": time.time()} for code, p in prices.items()}

    # ======================== 收盘收尾 ========================

    def _expire_signals(self):
        """收盘后：pending→expired，bought 保留不动。"""
        try:
            conn = sqlite3.connect(self.db_path)
            count = conn.execute(
                "UPDATE trade_signals SET status='expired' WHERE status='pending'"
            ).rowcount
            conn.commit()
            conn.close()
            logger.info(f"过期信号: {count} 个")
        except Exception as e:
            logger.warning(f"过期信号处理异常: {e}")

    # ======================== Telegram 消息接收 ========================

    def _check_replies(self):
        """拉取用户 Telegram 回复，解析成交信息。"""
        receiver = self._get_receiver()
        executor = self._get_executor()
        if receiver is None or executor is None:
            return
        try:
            updates = receiver.fetch_updates()
            for msg in updates:
                text = msg.get("text", "")
                if not text:
                    continue
                logger.info(f"收到 Telegram 消息: {msg['user']}: {text}")
                reply = executor.handle_user_reply(text)
                if reply:
                    self._alert(reply)
        except Exception as e:
            logger.warning(f"消息接收异常: {e}")

    def _get_receiver(self):
        if self._receiver is None:
            try:
                from system.utils.telegram import MessageReceiver
                self._receiver = MessageReceiver()
            except Exception as e:
                logger.warning(f"消息接收器初始化失败: {e}")
        return self._receiver

    def _get_executor(self):
        if self._executor is None:
            try:
                from trade.execution.manual import ManualExecutor
                self._executor = ManualExecutor()
            except Exception as e:
                logger.warning(f"执行器初始化失败: {e}")
        return self._executor

    def _get_paper_trader(self):
        if self._paper_trader is None:
            try:
                from trade.paper.trader import PaperTrader
                self._paper_trader = PaperTrader(
                    db_path=self.db_path, telegram_bot=self.telegram,
                )
            except Exception as e:
                logger.warning(f"模拟盘初始化失败: {e}")
        return self._paper_trader

    # ======================== 推送 ========================

    def _alert(self, msg: str):
        if self.telegram:
            try:
                self.telegram.send(msg)
            except Exception as e:
                logger.error(f"Telegram推送失败: {e}")
        logger.debug(f"盯盘提醒: {msg}")
