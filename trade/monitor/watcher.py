"""盘中盯盘进程 — cron 拉起后自管理生命周期

三层扫描:
  第一层（每轮 60s）: 大盘状态 + 持仓风控 + 信号触发 + 复盘推荐跟踪
  第二层（每50轮 ~50min）: 板块热度排名
  第三层（每3轮 ~3min）: 异动检测 + 板块趋势更新
  额外（每15轮 ~15min）: 主动换仓评估
"""

import logging
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from datetime import time as dt_time
from functools import lru_cache

from data.repo import TradeRepository
from system.config import settings
from trade.monitor.abnormal import AbnormalMonitorMixin
from trade.monitor.audit.decision_logger import DecisionLoggerMixin
from trade.monitor.buy_decision import BuyDecisionMixin
from trade.monitor.close_summary import CloseSummaryMixin
from trade.monitor.closing import ClosingDecisionMixin
from trade.monitor.market_state import MarketRegime, MarketStateMixin
from trade.monitor.position_risk import PositionRiskMixin
from trade.monitor.sector_context import SectorContextMixin
from trade.monitor.sector_resonance import (
    INDEX_VOLATILITY_THRESHOLD,
    SectorResonanceAnalyzer,
)
from trade.paper.account import PaperAccount
from trade.risk.engine import RiskEngine

logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S"
        )
    )
    logger.addHandler(ch)

MORNING_START = dt_time(9, 25)
MORNING_END = dt_time(11, 30)
AFTERNOON_START = dt_time(13, 0)
MARKET_CLOSE = dt_time(15, 0)

# 大盘熔断阈值
INDEX_HALT_PCT = -0.02  # 上证跌幅 > 2%
INDEX_DANGER_PCT = -0.01  # 上证跌破 MA20 且跌幅 > 1%


class Watcher(
    DecisionLoggerMixin,
    MarketStateMixin,
    BuyDecisionMixin,
    PositionRiskMixin,
    SectorContextMixin,
    AbnormalMonitorMixin,
    ClosingDecisionMixin,
    CloseSummaryMixin,
):
    """盘中盯盘进程 — cron 拉起后自管理生命周期"""

    def __init__(self, telegram_bot=None, qmt_quote=None, db_path=None):
        self.telegram = telegram_bot
        self._private_telegram = None
        self._init_private_telegram()
        self.qmt = qmt_quote
        self.db_path = db_path or settings.DATABASE_PATH
        self.repo = TradeRepository(db_path=self.db_path)
        self.paper_account = PaperAccount(
            db_path=self.db_path,
            telegram_bot=self.telegram,
            initial_capital=settings.PAPER_INITIAL_CAPITAL,
        )
        self._pos_meta: dict[
            str, dict
        ] = {}  # {code: {sl, tp, trailing_stop, highest_price, sector, score, signal_id}}
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

        # 指数日内走势追踪
        self._index_prices: list[float] = []  # 近 N 轮上证价格
        self._index_high: float = 0.0  # 日内最高
        self._index_low: float = 0.0  # 日内最低
        self._index_alerted_downtrend: bool = False
        self._index_last_fluctuation_price: float = 0.0  # 上次波动预警时的价格

        # 大盘量能追踪（量价背离检测）
        self._market_turnovers: list[float] = []  # 近 N 轮全市场成交额
        self._volume_alerted_divergence: bool = False  # 量价背离已推送过

        # 当前市场状态（MarketRegime 对象，每次 _scan() 刷新）
        self._regime = None  # type: MarketRegime | None

        # 尾盘决策
        self._closing_decision_done: bool = False  # 14:30 后只推送一次

        # 最大回撤保护
        self._max_drawdown_alerted: bool = False

        # 全市场快照（每3轮刷新）
        self._market_snapshot: dict[str, dict] = {}

        # 板块趋势跟踪（用于买卖信号时附带板块走势）
        self._sector_trend_history: dict[str, list[float]] = defaultdict(list)
        self._sector_trend_continuity: dict[str, int] = defaultdict(int)  # 连续同向轮数
        self._sector_trend_last_dir: dict[str, str] = {}  # 上一轮方向
        self._sector_trend_start: dict[str, str] = {}  # 趋势起点时间 HH:MM
        # 概念趋势跟踪
        self._concept_trend_history: dict[str, list[float]] = defaultdict(list)
        self._concept_trend_continuity: dict[str, int] = defaultdict(int)
        self._concept_trend_last_dir: dict[str, str] = {}
        self._concept_trend_start: dict[str, str] = {}
        # 共振/逆势分析
        self._resonance_analyzer = SectorResonanceAnalyzer()
        self._last_resonance_push_scan: int = -100  # 上次独立推送轮次
        self._last_resonance_index_dir: str = ""  # 上次推送时大盘方向（去重用）
        self._industry_cache: dict[str, str] = {}  # code → industry
        self._concept_cache: dict[str, list[str]] = {}  # code → [concept_names]
        self._sector_stats: dict[
            str, dict
        ] = {}  # sector_name → {change_pct, up, down} 实时
        self._concept_stats: dict[
            str, dict
        ] = {}  # concept_name → {change_pct, up, down} 实时

        # 指数技术指标拐点检测状态
        self._index_tech_state: dict[str, str | None] = {
            "macd_cross": None,
            "rsi6_zone": "normal",
            "rsi12_zone": "normal",
            "kdj_cross": None,
            "kdj_j_zone": "normal",
            "divergence": None,
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

        # watch_codes 缓存（signals + review_picks 查询结果，盘中极少变化）
        self._cached_db_watch_codes: set[str] = set()
        self._watch_codes_stale: bool = True

        # 个股日内技术指标缓存（每轮扫描刷新）
        self._intraday_cache: dict[str, dict] = {}
        self._intraday_cache_scan: int = -1

        # 合约信息缓存（盘中不变，永久缓存）
        self._instrument_cache: dict[str, dict] = {}

        # 日线因子缓存（全天不变）
        self._daily_factor_cache: dict[str, dict] = {}

        # Collector TCP 客户端
        self._collector_client = None
        self._last_index_quote: dict | None = None  # collector 推送的最新指数行情
        self._last_db_ts: float = 0  # 用于盘中重启去重

    def _init_private_telegram(self):
        try:
            from system.config.settings import TELEGRAM_PRIVATE_CHAT_ID

            if TELEGRAM_PRIVATE_CHAT_ID:
                from system.utils.telegram import MessageSender

                self._private_telegram = MessageSender(chat_id=TELEGRAM_PRIVATE_CHAT_ID)
        except Exception:
            pass

    @staticmethod
    @lru_cache(maxsize=settings.NAME_RESOLVE_CACHE_SIZE)
    def _resolve_name(self, code: str) -> str:
        try:
            import sqlite3

            from system.config import settings

            conn = sqlite3.connect(settings.DATABASE_PATH)
            row = conn.execute(
                """SELECT stock_name FROM stock_basic
                   WHERE stock_code=? AND trade_date=(SELECT MAX(trade_date) FROM stock_basic)
                   LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            return row[0] if row else code
        except Exception:
            return code

    # ======================== 生命周期 ========================

    def run(self):
        self._trade_date = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"盯盘进程启动 {self._trade_date}")
        self.paper_account.restore(self._trade_date)
        self._restore_pos_meta()
        self._init_bought_watch()
        self._cleanup_old_snapshots()
        self._load_sector_history()

        # 盘中重启：立即从 QMT 拉最新价格更新持仓市值，避免快照与持仓数据不一致
        if self._in_trading_hours() and self.qmt and self.paper_account.positions:
            codes = list(self.paper_account.positions.keys())
            try:
                quotes = self.qmt.get_realtime(codes)
                for code, pos in self.paper_account.positions.items():
                    item = quotes.get(code)
                    if item:
                        new_price = (
                            item.get("lastPrice")
                            or item.get("last_price")
                            or item.get("price")
                        )
                        if new_price:
                            pos.update_price(float(new_price))
                # 立即落盘，确保快照与当前价格一致
                self.paper_account._persist_state()
                logger.info(
                    f"盘中重启：已刷新 {len(self.paper_account.positions)} 只持仓价格并落盘 "
                    f"总资产 {self.paper_account.total_value:,.0f}"
                )
            except Exception as e:
                logger.warning(f"盘中重启刷新价格失败: {e}")
        # 新交易日重置跨日状态
        self._signal_alert_state.clear()
        self._review_alert_state.clear()
        self._sl_reminders.clear()
        self._alerted_sl_tp.clear()
        self._index_alerted_downtrend = False
        self._max_drawdown_alerted = False
        self._closing_decision_done = False

        in_trading = self._in_trading_hours()
        if in_trading:
            # 盘中重启（容灾路径）：先连 collector socket，再读 DB 恢复历史
            logger.info("检测到盘中重启，进入容灾恢复")
            self._connect_collector()
            self._restore_index_context()  # 从 index_snapshots 读
            self._restore_market_from_db()  # 从 market_snapshots 读
            if self._market_snapshot:
                self._update_sector_trends()  # 立即计算板块趋势
            self._recv_collector_data()  # 处理 socket buffer，去重
        else:
            # 盘前正常启动：直接连 collector，不读 DB
            self._connect_collector()

        if self._before_market():
            wait = (
                datetime.combine(date.today(), MORNING_START) - datetime.now()
            ).total_seconds()
            if wait > 0:
                logger.info(f"距开盘 {wait:.0f} 秒，等待中")
                time.sleep(wait)

        # 如果盘前还没连上，交易时段再试一次
        if not in_trading:
            self._connect_collector()

        self._running = True

        while self._running:
            if self._after_market():
                logger.info("收盘，盯盘结束")
                break

            if self._in_lunch_break():
                logger.info("午休，13:00 恢复")
                self._lunch_break()
                continue

            self._scan_count += 1
            logger.info(f"扫描 #{self._scan_count}")
            try:
                self._scan()
            except Exception as e:
                logger.error(f"扫描异常: {e}", exc_info=True)

        self._finalize_close()
        logger.info("盯盘进程退出")

    # ======================== 时段判断 ========================

    @staticmethod
    def _in_trading_hours() -> bool:
        now = datetime.now().time()
        return (
            MORNING_START <= now < MORNING_END or AFTERNOON_START <= now < MARKET_CLOSE
        )

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
        now = datetime.now()
        afternoon = now.replace(hour=13, minute=0, second=0, microsecond=0)
        wait = (afternoon - now).total_seconds()
        if wait > 0:
            logger.info(f"午休，{wait:.0f}秒后恢复")
            time.sleep(wait)

    # ======================== 主扫描 ========================

    def _scan(self):
        """三层扫描入口。每步骤独立异常保护，单步失败不阻塞后续步骤。"""
        try:
            self._recv_collector_data()
        except Exception as e:
            logger.warning(f"接收collector数据异常: {e}", exc_info=True)

        try:
            self._check_replies()
        except Exception as e:
            logger.warning(f"Telegram回复检查异常: {e}", exc_info=True)

        try:
            watch_codes = self._get_watch_codes()
        except Exception as e:
            logger.warning(f"获取监控列表异常: {e}", exc_info=True)
            return

        if not watch_codes:
            return

        try:
            prices = self._get_realtime_prices(watch_codes)
        except Exception as e:
            logger.warning(f"获取实时价格异常: {e}", exc_info=True)
            return

        if not prices:
            logger.warning("无实时行情，跳过本轮")
            return

        try:
            self.paper_account.update_prices(prices)
        except Exception as e:
            logger.warning(f"更新持仓价格异常: {e}", exc_info=True)

        # 首轮记录基准锚点（用于后续交叉校验）
        try:
            self._record_baseline(prices)
        except Exception as e:
            logger.warning(f"基准记录异常: {e}", exc_info=True)

        try:
            drawdown_halt = self.paper_account.drawdown >= settings.MAX_ACCOUNT_DRAWDOWN
            if drawdown_halt:
                self._check_max_drawdown()
        except Exception as e:
            logger.warning(f"回撤检查异常: {e}", exc_info=True)
            drawdown_halt = False

        try:
            # 全市场快照只能从 collector 获取，Watcher 不直连 QMT 拉全市场数据
            if self._scan_count % 3 == 0 and self._market_snapshot:
                self._update_sector_trends()

            # 全市场快照只能从 collector 获取，Watcher 不直连 QMT 拉全市场数据
        except Exception as e:
            logger.warning(f"板块趋势更新异常: {e}", exc_info=True)

        try:
            self._maybe_push_resonance()
        except Exception as e:
            logger.warning(f"共振分析异常: {e}", exc_info=True)

        try:
            self._regime = self._check_market_state(prices)
            if hasattr(self._regime, "allow_buy"):
                regime_ok = self._regime.allow_buy and not drawdown_halt
            else:
                # 兼容旧版返回 bool
                regime_ok = self._regime and not drawdown_halt
        except Exception as e:
            logger.error(f"大盘状态检查异常，暂停买入: {e}", exc_info=True)
            self._regime = MarketRegime(
                pattern="error", confidence="low", allow_buy=False
            )
            regime_ok = False

        try:
            if self._index_prices:
                ma5, ma10, ma20 = self._get_index_baseline()
                ma60 = self._get_index_ma60()
                vol_trend = self._calc_volume_trend()
                breadth = self._compute_breadth()
                br = (
                    breadth.get("up", 1) / max(breadth.get("down", 1), 1)
                    if breadth
                    else 0
                )
                amp = (
                    (self._index_high - self._index_low) / self._index_low
                    if self._index_low > 0
                    else 0
                )
                active = sum(
                    1
                    for s in self._sector_stats.values()
                    if abs(s.get("change_pct", 0)) > 0.01
                )
                self.risk_engine.update_market_env(
                    ma20,
                    self._index_prices[-1],
                    ma60,
                    vol_trend,
                    br,
                    amp,
                    active,
                )
        except Exception as e:
            logger.warning(f"风控市场环境更新异常: {e}", exc_info=True)

        try:
            self._check_index_technicals()
        except Exception as e:
            logger.warning(f"指数技术分析异常: {e}", exc_info=True)

        try:
            self._check_positions(prices)
        except Exception as e:
            logger.warning(f"持仓检查异常: {e}", exc_info=True)

        try:
            self._check_signals(prices, self._regime)
        except Exception as e:
            logger.warning(f"信号检查异常: {e}", exc_info=True)

        try:
            self._check_bought_signals(prices)
        except Exception as e:
            logger.warning(f"已买入信号检查异常: {e}", exc_info=True)

        try:
            self._check_review_picks(prices, self._regime)
        except Exception as e:
            logger.warning(f"复盘精选检查异常: {e}", exc_info=True)

        try:
            self._check_sl_reminders()
        except Exception as e:
            logger.warning(f"止损提醒异常: {e}", exc_info=True)

        try:
            # 开盘决策：9:25~9:35 内第一轮有数据的扫描发送
            minutes_since_open = (
                datetime.now() - datetime.combine(date.today(), MORNING_START)
            ).total_seconds() / 60
            if (
                not getattr(self, "_opening_decision_sent", False)
                and prices
                and 0 <= minutes_since_open <= 10
            ):
                self._send_opening_decision(prices, regime_ok)
                self._opening_decision_sent = True
        except Exception as e:
            logger.warning(f"开盘决策推送异常: {e}", exc_info=True)

        try:
            if self._scan_count % 50 == 0 and self._market_snapshot:
                # 运行共振分析（长窗口 ~50分钟），注入标签到TOP5
                res_labels = {}
                if len(self._index_prices) >= settings.RESONANCE_INDEX_MIN_POINTS:
                    try:
                        result = self._resonance_analyzer.analyze(
                            index_prices=self._index_prices,
                            sector_histories=dict(self._sector_trend_history),
                            concept_histories=dict(self._concept_trend_history),
                            sector_stats=self._sector_stats,
                            concept_stats=self._concept_stats,
                            market_snapshot=self._market_snapshot,
                            industry_cache=self._industry_cache,
                            concept_cache=self._concept_cache,
                            trend_starts={
                                **self._sector_trend_start,
                                **self._concept_trend_start,
                            },
                            resolve_name=self._resolve_name,
                            window_entries=settings.RESONANCE_TOP5_WINDOW_ENTRIES,
                        )
                        res_labels = self._resonance_analyzer.format_top5_labels(result)
                    except Exception:
                        pass
                self._check_sector_heat(self._market_snapshot, res_labels)
                # 板块热度决策日志
                try:
                    top5 = sorted(
                        [(k, round(v[-1], 2)) for k, v in self._sector_trend_history.items()
                         if len(v) >= 3], key=lambda x: -x[1],
                    )[:5]
                    bottom3 = sorted(
                        [(k, round(v[-1], 2)) for k, v in self._sector_trend_history.items()
                         if len(v) >= 3], key=lambda x: x[1],
                    )[:3]
                    if top5 or bottom3:
                        self._log_sector_alert(
                            top_sectors=[[n, v] for n, v in top5],
                            bottom_sectors=[[n, v] for n, v in bottom3],
                            warnings=[], good=[],
                        )
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"板块热度检查异常: {e}", exc_info=True)

        try:
            if self._scan_count % 10 == 0:
                self.paper_account._persist_state()
        except Exception as e:
            logger.warning(f"定期落盘异常: {e}", exc_info=True)

        try:
            if self._scan_count % 3 == 0:
                self._check_abnormal(prices)
        except Exception as e:
            logger.warning(f"异动检测异常: {e}", exc_info=True)

        try:
            if self._scan_count % 15 == 0:
                self._evaluate_swaps(prices)
        except Exception as e:
            logger.warning(f"换仓评估异常: {e}", exc_info=True)

        try:
            self._check_closing(prices)
        except Exception as e:
            logger.warning(f"尾盘决策异常: {e}", exc_info=True)

        try:
            if self._scan_count % 5 == 0:  # 每 5 轮做一次健康检查
                self._health_check(prices)
        except Exception as e:
            logger.warning(f"健康检查异常: {e}", exc_info=True)

    # ======================== 开机基准 ========================

    def _record_baseline(self, prices: dict[str, float]):
        """首轮记录基准值，后续每轮交叉校验用。"""
        if getattr(self, "_baseline", None) is not None:
            return
        iq = getattr(self, "_last_index_quote", None) or {}
        pre_close = iq.get("pre_close", 0)
        if pre_close <= 0:
            return  # 还没收到 collector 数据，等下一轮
        self._baseline = {
            "pre_close": pre_close,
            "qmt_change_pct": iq.get("change_pct", 0),
            "round": self._scan_count,
        }
        logger.info(
            f"基准锚点: preClose={self._baseline['pre_close']:.2f} "
            f"QMT涨跌幅={self._baseline['qmt_change_pct']:.4f}"
        )

    # ======================== 健康检查 ========================

    def _health_check(self, prices: dict[str, float]):
        """每 5 轮运行所有注册的校验函数，异常推 Telegram。"""
        from trade.monitor.health_checks import CheckContext, run_checks

        pa = self.paper_account
        baseline = getattr(self, "_baseline", None) or {}
        index_quote = getattr(self, "_last_index_quote", None) or {}
        sector_stats = getattr(self, "_sector_stats", None) or {}

        # 指数停更检测（需要跨轮状态，在框架外处理）
        index_stale = False
        if len(self._index_prices) >= 5:
            recent = self._index_prices[-5:]
            if max(recent) - min(recent) < 0.01:
                self._index_stale_count = getattr(self, "_index_stale_count", 0) + 1
                if self._index_stale_count >= 3:
                    index_stale = True
            else:
                self._index_stale_count = 0

        collector_ok = bool(
            getattr(self, "_collector_client", None)
            and getattr(self._collector_client, "connected", False)
        )
        regime = getattr(self, "_regime", None)
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"
        sector_trends = {}
        for code in pa.positions:
            try:
                sector_trends[code] = self._get_sector_trend(code)
            except Exception:
                sector_trends[code] = ""

        # 技术指标快照
        index_tech = {}
        for k in (
            "rsi6",
            "rsi12",
            "rsi24",
            "macd_dif",
            "macd_dea",
            "macd_bar",
            "kdj_k",
            "kdj_d",
            "kdj_j",
        ):
            v = getattr(self, f"_idx_{k}", None)
            if v is not None:
                index_tech[k] = v
        market_env = getattr(self.risk_engine, "market_env", "swing")

        # entry_date 映射
        entry_dates = {}
        for code, pos in pa.positions.items():
            entry_dates[code] = getattr(pos, "entry_date", "")

        # pending 信号数
        try:
            pending = self.repo.get_pending_signals(self._trade_date, account="paper")
            pending_count = len(pending) if pending else 0
        except Exception:
            pending_count = 0

        ctx = CheckContext(
            # 账户
            cash=pa.cash,
            total_value=pa.total_value,
            daily_pnl=pa.daily_pnl,
            positions=pa.positions,
            max_positions=settings.MAX_POSITIONS,
            entry_dates=entry_dates,
            # 行情
            prices=prices,
            limit_cache=getattr(self, "_limit_cache", {}) or {},
            index_prices=list(self._index_prices),
            index_high=self._index_high,
            index_low=self._index_low,
            index_pre_close=index_quote.get("pre_close", 0),
            qmt_change_pct=index_quote.get("change_pct"),
            # 板块
            sector_stats=sector_stats,
            # 盯盘内部
            pos_meta=dict(self._pos_meta),
            bought_watch=getattr(self, "_bought_watch", {}) or {},
            sl_reminder_count=len(getattr(self, "_sl_reminders", {}) or {}),
            alerted_sl_tp_count=len(self._alerted_sl_tp),
            triggered_ids_count=len(self._triggered_ids),
            pending_signal_count=pending_count,
            scan_count=self._scan_count,
            prev_scan_count=getattr(self, "_prev_scan_count", 0),
            # 基准
            baseline_pre_close=baseline.get("pre_close", 0),
            baseline_qmt_pct=baseline.get("qmt_change_pct", 0),
            trade_date=self._trade_date,
            collector_connected=collector_ok,
            # 决策上下文
            risk_level=risk_level,
            regime_pattern=getattr(regime, "pattern", "normal") if regime else "normal",
            sector_trends=sector_trends,
            index_technicals=index_tech,
            market_env=market_env,
            scenario_probs=getattr(self, "_scenario_probs", {}) or {},
            scenario_scan_count=getattr(self, "_scenario_scan_count", 0),
        )
        self._prev_scan_count = self._scan_count

        alerts = run_checks(ctx)
        if index_stale:
            alerts.append("⚠️ 指数停更: 近 15 轮上证波动 < 0.01")

        if alerts:
            msg = "🩺 健康检查\n" + "\n".join(f"  {a}" for a in alerts)
            logger.warning(msg)
            self._alert_private(msg)

    def _restore_pos_meta(self):
        """从 trade_signals 恢复 _pos_meta（止损止盈板块等决策数据）。

        模拟盘只存买卖结果，盯盘决策数据需从信号表重建。
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            for code in self.paper_account.positions:
                sig = conn.execute(
                    """SELECT stop_loss, take_profit, trailing_stop, signal_score,
                              strategy_name, id
                       FROM trade_signals
                       WHERE stock_code=? AND status='bought'
                       ORDER BY id DESC LIMIT 1""",
                    (code,),
                ).fetchone()
                if sig:
                    pos = self.paper_account.positions[code]
                    self._pos_meta[code] = {
                        "sl": sig["stop_loss"] or 0,
                        "tp": sig["take_profit"] or 0,
                        "trailing_stop": sig["trailing_stop"] or 0.05,
                        "highest_price": pos.current_price,
                        "sector": "",
                        "score": sig["signal_score"] or 0,
                        "signal_id": sig["id"],
                    }
                else:
                    pos = self.paper_account.positions[code]
                    self._pos_meta[code] = {
                        "sl": 0,
                        "tp": 0,
                        "trailing_stop": 0.05,
                        "highest_price": pos.current_price,
                        "sector": "",
                        "score": 0,
                        "signal_id": None,
                    }
            conn.close()
            logger.info(f"_pos_meta 恢复: {len(self._pos_meta)} 只")
        except Exception as e:
            logger.warning(f"_pos_meta 恢复失败: {e}")

    def _init_bought_watch(self):
        """初始化 _bought_watch（从 _pos_meta + paper_account 持仓重建）。"""
        for code, pos in self.paper_account.positions.items():
            _meta = self._pos_meta.get(code, {})
            self._bought_watch[code] = {
                "entry_price": pos.avg_cost,
                "last_alert_scan": 0,
                "status": "watching",
                "alert_count": 0,
                "max_profit_pct": (
                    max(0, (pos.current_price - pos.avg_cost) / pos.avg_cost)
                    if pos.avg_cost > 0 and pos.current_price > 0
                    else 0
                ),
            }
        self._invalidate_watch_codes_cache()

    # ======================== 关注清单 ========================

    def _get_watch_codes(self) -> list[str]:
        """获取需要监控的代码列表。positions 来自内存实时，signals+picks 用缓存。"""
        codes: set[str] = set()

        # 持仓来自内存，始终实时
        for code in self.paper_account.positions:
            codes.add(code)

        # signals + review picks 缓存，信号变化时 _invalidate_watch_codes_cache() 触发刷新
        if self._watch_codes_stale:
            try:
                signals = self.repo.get_pending_signals(account="paper")
                for s in signals:
                    codes.add(s["stock_code"])
            except Exception as e:
                logger.warning(f"获取待处理信号异常: {e}")

            try:
                picks = self._load_review_picks()
                for p in picks:
                    codes.add(p["stock_code"])
            except Exception as e:
                logger.warning(f"获取复盘推荐异常: {e}")

            self._cached_db_watch_codes = codes - set(
                self.paper_account.positions.keys()
            )
            self._watch_codes_stale = False
        else:
            # 从缓存恢复（不含持仓，持仓已从内存加入）
            codes |= self._cached_db_watch_codes

        return list(codes)

    def _invalidate_watch_codes_cache(self):
        """模拟盘成交后刷新关注列表缓存。"""
        self._watch_codes_stale = True

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

    def _get_index_ma60(self) -> float:
        """获取上证 MA60（从 DB 缓存）。"""
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT ma60 FROM stock_basic
                   WHERE stock_code='000001'
                   ORDER BY trade_date DESC LIMIT 1"""
            ).fetchone()
            conn.close()
            return (row[0] or 0) if row else 0
        except Exception:
            return 0

    def _calc_volume_trend(self) -> float:
        """计算近5天全市场成交额趋势。正=放量，负=缩量。"""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT amount FROM market_breadth
                   ORDER BY trade_date DESC LIMIT 5"""
            ).fetchall()
            conn.close()
            if len(rows) < 3:
                return 0
            amounts = [r[0] for r in rows if r[0]]
            if len(amounts) < 3:
                return 0
            recent_avg = sum(amounts[:2]) / 2
            prev_avg = sum(amounts[2:]) / (len(amounts) - 2)
            return (recent_avg - prev_avg) / prev_avg if prev_avg > 0 else 0
        except Exception:
            return 0

    # ======================== Collector 数据接收 ========================

    # ======================== 共振/逆势分析 ========================

    def _maybe_push_resonance(self):
        """大盘波动≥0.3%时触发独立共振/逆势推送。去重：≥15轮 + 大盘方向未变不重复。"""
        if len(self._index_prices) < settings.RESONANCE_INDEX_MIN_POINTS:
            return

        # 检查大盘近10分钟波动
        n = min(settings.RESONANCE_PUSH_WINDOW_ENTRIES * 3, len(self._index_prices) - 1)
        recent_change = (
            self._index_prices[-1] - self._index_prices[-(n + 1)]
        ) / self._index_prices[-(n + 1)]
        if abs(recent_change) < INDEX_VOLATILITY_THRESHOLD:
            return

        # 去重：冷却轮数内不重复，大盘方向未变不重复
        index_dir = "up" if recent_change > 0 else "down"
        if (
            self._scan_count - self._last_resonance_push_scan
            < settings.RESONANCE_PUSH_COOLDOWN_ROUNDS
            and index_dir == self._last_resonance_index_dir
        ):
            return

        # 需要足量板块数据
        sector_histories = dict(self._sector_trend_history)
        concept_histories = dict(self._concept_trend_history)
        if not sector_histories:
            return

        result = self._resonance_analyzer.analyze(
            index_prices=self._index_prices,
            sector_histories=sector_histories,
            concept_histories=concept_histories,
            sector_stats=self._sector_stats,
            concept_stats=self._concept_stats,
            market_snapshot=self._market_snapshot,
            industry_cache=self._industry_cache,
            concept_cache=self._concept_cache,
            trend_starts={
                **self._sector_trend_start,
                **self._concept_trend_start,
            },
            resolve_name=self._resolve_name,
            window_entries=settings.RESONANCE_PUSH_WINDOW_ENTRIES,
        )

        msg = self._resonance_analyzer.format_push_message(result)
        if msg:
            self._alert(msg)
            self._last_resonance_push_scan = self._scan_count
            self._last_resonance_index_dir = index_dir
            self._last_resonance_result = result
            # 决策日志
            try:
                self._log_resonance_alert(
                    index_direction=result.get("index_direction", ""),
                    index_change=result.get("index_change", 0),
                    resonance_down=[(n, round(c, 4)) for n, c, *_ in result.get("resonance_down", [])],
                    counter_up=[(n, round(c, 4)) for n, c, *_ in result.get("counter_up", [])],
                )
            except Exception:
                pass

    def _connect_collector(self):
        """连接 QMT Collector。可重复调用，内部有重试节流。"""
        try:
            from data.live.collector_client import DataCollectorClient
        except ImportError:
            logger.warning("DataCollectorClient 模块不可用")
            return
        if self._collector_client is None:
            self._collector_client = DataCollectorClient()
        if not self._collector_client.connected:
            self._collector_client.connect()

    def _recv_collector_data(self):
        """从 collector socket 读取所有待处理消息，更新内存状态。"""
        if self._collector_client is None:
            self._connect_collector()
            return
        if not self._collector_client.connected:
            self._connect_collector()
            return

        try:
            messages = self._collector_client.recv_all()
        except Exception as e:
            logger.warning(f"Collector 数据读取异常: {e}")
            self._collector_client.disconnect()
            return

        for msg in messages:
            msg_ts = msg.get("ts", 0)
            # 去重：跳过 DB 已恢复的旧数据
            if self._last_db_ts > 0 and msg_ts <= self._last_db_ts:
                continue

            msg_type = msg.get("type")
            if msg_type == "index":
                self._handle_collector_index(msg)
            elif msg_type == "market":
                self._handle_collector_market(msg)

    def _handle_collector_index(self, msg: dict):
        """处理 collector 推送的指数行情。"""
        self._last_index_quote = {
            "price": msg["price"],
            "pre_close": msg.get("pre_close", 0),
            "change_pct": msg.get("change_pct", 0),
            "amount": msg.get("amount", 0),
        }
        index_price = msg["price"]

        if self._index_high == 0 or index_price > self._index_high:
            self._index_high = index_price
        if self._index_low == 0 or index_price < self._index_low:
            self._index_low = index_price
        self._index_prices.append(index_price)

        amount = msg.get("amount", 0)
        if amount > 0:
            self._market_turnovers.append(amount)

    def _handle_collector_market(self, msg: dict):
        """处理 collector 推送的全市场快照。"""
        self._market_snapshot = msg.get("stocks", {})
        self._last_db_ts = max(self._last_db_ts, msg.get("ts", 0))

        if self._market_snapshot:
            self._update_sector_trends()

    def _restore_market_from_db(self):
        """从 market_snapshots 恢复最新一批全市场快照（盘中重启用）。"""
        try:
            conn = sqlite3.connect(self.db_path)
            # 先查最新时间戳，再只取该批次数据
            latest = conn.execute(
                """SELECT MAX(ts) FROM market_snapshots WHERE trade_date=?""",
                (self._trade_date,),
            ).fetchone()
            if not latest or not latest[0]:
                conn.close()
                return
            latest_ts = latest[0]
            rows = conn.execute(
                """SELECT ts, code, change_pct, price, amount FROM market_snapshots
                   WHERE trade_date=? AND ts=?""",
                (self._trade_date, latest_ts),
            ).fetchall()
            conn.close()
            if not rows:
                return

            self._market_snapshot = {}
            for _ts, code, chg, price, amount in rows:
                self._market_snapshot[code] = {
                    "changePct": chg,
                    "price": price or 0,
                    "amount": amount or 0,
                }
            # ts 可能是 epoch 浮点或 ISO 字符串，统一尝试转换
            try:
                _parsed = float(latest_ts) if latest_ts else 0
            except (ValueError, TypeError):
                _parsed = 0
            self._last_db_ts = max(self._last_db_ts, _parsed)
            logger.info(
                f"从DB恢复市场快照: {len(self._market_snapshot)}只 ts={latest_ts}"
            )
        except Exception as e:
            logger.warning(f"从DB恢复市场快照失败: {e}")

    def _expire_signals(self):
        """收盘后：仅过期当日的 pending 信号，bought 保留不动。"""
        try:
            conn = sqlite3.connect(self.db_path)
            count = conn.execute(
                "UPDATE trade_signals SET status='expired' WHERE status='pending' AND trade_date=?",
                (self._trade_date,),
            ).rowcount
            conn.commit()
            conn.close()
            logger.info(f"过期信号: {count} 个")
        except Exception as e:
            logger.warning(f"过期信号处理异常: {e}")

    # ======================== Telegram 消息接收 ========================

    def _check_replies(self):
        """拉取用户 Telegram 回复，解析成交信息。实盘回复走私聊。"""
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
                # 先检查是否是 SL 提醒回复（优先级高于成交解析）
                sl_result = self.handle_sl_command(text)
                if sl_result:
                    self._alert(sl_result)
                    continue

                result = executor.handle_user_reply(text)
                if result is not None:
                    reply_text, account = result
                    if account == "real":
                        self._alert_private(reply_text)
                    else:
                        self._alert(reply_text)
                    # 如果消息包含成交/未成交，同时清理对应的 SL 提醒
                    self._resolve_sl_reminders(text)
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

    # ======================== AI 辅助 ========================

    def _ai_chase_opinion(
        self,
        code,
        name,
        price,
        buy_min,
        buy_max,
        sl,
        tp,
        trend,
        above_pct,
        reject_reason: str = "",
    ) -> str:
        """追高/被拒场景调用 AI 给出判断建议。返回空字符串表示 AI 不可用。"""
        try:
            from analysis.review.analyzer import AIAnalyzer

            if not hasattr(self, "_ai_analyzer"):
                self._ai_analyzer = AIAnalyzer()
        except Exception:
            return ""

        above_str = (
            f"超出买入区上限{above_pct:+.1f}%"
            if above_pct > 0
            else f"低于买入区下限{abs(above_pct):.1f}%"
        )
        position = "追高" if above_pct > 0 else "已回落"
        reject_info = (
            f"系统已因「{reject_reason}」拒绝买入，请二判。" if reject_reason else ""
        )

        prompt = (
            f"股票：{code} {name}，现价{price:.2f}，买入区间{buy_min:.2f}~{buy_max:.2f}，"
            f"当前{above_str}。"
            f"止损{sl:.2f}，止盈{tp:.2f}。"
            f"板块趋势：{trend}。{reject_info}"
            f"请根据当前状态判断：同意拒绝 / 可以买入 / 再等等。"
            f"用一句话给出结论和关键理由（不超过50字）。"
        )
        try:
            result = self._ai_analyzer._call_ai(prompt, max_tokens=100)
            if result:
                return result.strip()
        except Exception:
            pass
        return ""

    # ======================== 推送 ========================

    def _alert(self, msg: str):
        if self.telegram:
            try:
                self.telegram.send(msg)
            except Exception as e:
                logger.error(f"Telegram推送失败: {e}")
        logger.info(f"📤 Telegram: {msg}")

    def _alert_private(self, msg: str):
        """推送消息到私聊（实盘交易信息）"""
        if self._private_telegram:
            try:
                self._private_telegram.send(msg)
            except Exception as e:
                logger.error(f"私聊推送失败: {e}")
            logger.info(f"📤 Telegram私聊: {msg}")
        else:
            self._alert(msg)  # fallback
