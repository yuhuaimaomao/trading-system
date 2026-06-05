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
from trade.monitor.ai_queue import AIQueue
from trade.monitor.audit.decision_logger import DecisionLoggerMixin
from trade.monitor.buy_decision import BuyDecisionMixin
from trade.monitor.close_summary import CloseSummaryMixin
from trade.monitor.closing import ClosingDecisionMixin
from trade.monitor.intraday_scout import IntradayScoutMixin
from trade.monitor.market_state import MarketStateMixin
from trade.monitor.state import MarketRegime
from trade.monitor.position_risk import PositionRiskMixin
from trade.monitor.sector_context import SectorContextMixin
from trade.monitor.sector_resonance import (
    INDEX_VOLATILITY_THRESHOLD,
    SectorResonanceAnalyzer,
)
from trade.monitor.state import ScanState
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

MORNING_START = dt_time(9, 30)
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
    IntradayScoutMixin,
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
        self._alert_fingerprints: dict[str, int] = {}  # 消息指纹→上次推送scan_count

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
        # 多指数追踪: {code: {name, prices, high, low, last_price, change_pct}}
        self._index_map: dict[str, dict] = {}
        # 市场宽度
        self._market_breadth: dict = {"up": 0, "down": 0, "flat": 0, "total": 0}
        self._index_close_high: float = 0.0  # 收盘价序列最大值（健康检查用）
        self._index_close_low: float = 0.0  # 收盘价序列最小值（健康检查用）
        self._index_alerted_downtrend: bool = False
        self._index_alerted_ma20: int = 0  # 大盘偏弱告警去重（scan_count）
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

        # 数据就绪标志：首轮 sector 数据到达前不交易
        self._data_ready: bool = False
        self._data_ready_at: float = 0  # 就绪时间戳
        self._data_missing_rounds: int = 0  # 连续缺数据轮数

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

        # 卖出冷却：当日卖出后 N 轮内不重新买入
        self._recently_sold: dict[str, int] = {}  # code → 卖出时的 scan_count

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

        # 个股价格追踪（最近10分钟，用于止跌确认）
        self._recent_prices: dict[
            str, list[tuple[float, float]]
        ] = {}  # {code: [(ts, price), ...]}

        # 全市场快照价格追踪（用于回踩机会扫描，覆盖所有5000+只）
        self._snapshot_price_history: dict[str, list[tuple[float, float]]] = {}
        # {code: [(ts, price), ...]}  每次 _handle_collector_market 更新

        # 回踩机会发现状态
        self._pullback_scan_count: int = 0
        self._pullback_alerted_today: set[str] = set()  # 已推送过的股票（防重复）

        # Collector TCP 客户端
        self._collector_client = None
        self._last_index_quote: dict | None = None  # collector 推送的最新指数行情
        self._last_db_ts: float = 0  # 用于盘中重启去重

        # AI 异步队列（后台线程，不阻塞扫描）
        self._ai_queue = AIQueue()
        # 追高/二判 AI 待处理：{key: {code, name, price, ...}} 结果就绪后发送提醒
        self._pending_chase: dict[str, dict] = {}
        # 指数波动 AI 待处理
        self._pending_index_ai: dict = {}
        # 早盘 AI 板块倾向：{sector_name: {bias, priority, size_mult, stock_codes, reason}}
        self._morning_sector_bias: dict[str, dict] = {}

        # 推送冷却：{code: (scan_count, price)} 抑制重复推送
        self._push_cooldown: dict[str, tuple[int, float]] = {}
        # 健康检查告警指纹：{fingerprint: last_scan_count} 抑制重复告警
        self._health_alert_seen: dict[str, int] = {}

    def build_state(self) -> ScanState:
        """构建当前运行时状态快照，传递给各领域模块。"""
        return ScanState(
            running=self._running,
            trade_date=self._trade_date,
            scan_count=self._scan_count,
            # 指数
            index_prices=list(self._index_prices),
            index_high=self._index_high,
            index_low=self._index_low,
            index_map=dict(self._index_map),
            index_close_high=self._index_close_high,
            index_close_low=self._index_close_low,
            index_alerted_downtrend=self._index_alerted_downtrend,
            index_alerted_ma20=self._index_alerted_ma20,
            index_last_fluctuation_price=self._index_last_fluctuation_price,
            index_tech_state=dict(self._index_tech_state),
            # 市场宽度
            market_breadth=dict(self._market_breadth),
            market_turnovers=list(self._market_turnovers),
            volume_alerted_divergence=self._volume_alerted_divergence,
            # 市场状态
            regime=self._regime,
            closing_decision_done=self._closing_decision_done,
            max_drawdown_alerted=self._max_drawdown_alerted,
            # 数据就绪
            data_ready=self._data_ready,
            data_ready_at=self._data_ready_at,
            data_missing_rounds=self._data_missing_rounds,
            market_snapshot=dict(self._market_snapshot),
            last_index_quote=dict(self._last_index_quote) if self._last_index_quote else None,
            last_db_ts=self._last_db_ts,
            # 板块趋势
            sector_stats=dict(self._sector_stats),
            concept_stats=dict(self._concept_stats),
            industry_cache=dict(self._industry_cache),
            concept_cache=dict(self._concept_cache),
            last_resonance_push_scan=self._last_resonance_push_scan,
            last_resonance_index_dir=self._last_resonance_index_dir,
            # 告警
            triggered_ids=set(self._triggered_ids),
            alerted_sl_tp=set(self._alerted_sl_tp),
            alert_fingerprints=dict(self._alert_fingerprints),
            signal_alert_state=dict(self._signal_alert_state),
            review_alert_state=dict(self._review_alert_state),
            prev_snapshot=dict(self._prev_snapshot),
            # 卖出冷却
            recently_sold=dict(self._recently_sold),
            # 持仓
            pos_meta=dict(self._pos_meta),
            bought_watch=dict(self._bought_watch),
            recent_prices=dict(self._recent_prices),
            snapshot_price_history=dict(self._snapshot_price_history),
            sl_reminders=dict(self._sl_reminders),
            # 缓存
            ma_baseline_cache=self._ma_baseline_cache,
            limit_cache=dict(self._limit_cache),
            instrument_cache=dict(self._instrument_cache),
            intraday_cache=dict(self._intraday_cache),
            intraday_cache_scan=self._intraday_cache_scan,
            daily_factor_cache=dict(self._daily_factor_cache),
            cached_db_watch_codes=set(self._cached_db_watch_codes),
            watch_codes_stale=self._watch_codes_stale,
            # 回踩
            pullback_scan_count=self._pullback_scan_count,
            pullback_alerted_today=set(self._pullback_alerted_today),
            # AI
            pending_chase=dict(self._pending_chase),
            pending_index_ai=dict(self._pending_index_ai),
            morning_sector_bias=dict(self._morning_sector_bias),
            # 推送
            push_cooldown=dict(self._push_cooldown),
            health_alert_seen=dict(self._health_alert_seen),
        )

    def _init_private_telegram(self):
        try:
            from system.config.settings import TELEGRAM_PRIVATE_CHAT_ID

            if TELEGRAM_PRIVATE_CHAT_ID:
                from system.utils.telegram import MessageSender

                self._private_telegram = MessageSender(chat_id=TELEGRAM_PRIVATE_CHAT_ID)
        except Exception:
            pass

    def _load_morning_sector_bias(self):
        """加载今日早盘 AI 板块倾向到内存。"""
        if not settings.MORNING_SECTOR_BIAS_ENABLED:
            return
        try:
            import json

            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT sector_name, bias, priority, size_multiplier, stock_codes, reason
                   FROM morning_sector_bias WHERE trade_date=?""",
                (self._trade_date,),
            ).fetchall()
            conn.close()
            self._morning_sector_bias = {}
            for r in rows:
                name, bias, priority, size_mult, stock_codes_json, reason = r
                self._morning_sector_bias[name] = {
                    "bias": bias,
                    "priority": priority,
                    "size_mult": size_mult,
                    "stock_codes": json.loads(stock_codes_json)
                    if stock_codes_json
                    else [],
                    "reason": reason,
                }
            if self._morning_sector_bias:
                focus = [
                    n
                    for n, i in self._morning_sector_bias.items()
                    if i["bias"] == "focus"
                ]
                avoid = [
                    n
                    for n, i in self._morning_sector_bias.items()
                    if i["bias"] == "avoid"
                ]
                parts = []
                if focus:
                    parts.append(f"聚焦: {', '.join(focus)}")
                if avoid:
                    parts.append(f"回避: {', '.join(avoid)}")
                logger.info(
                    f"早盘板块倾向已加载 ({len(self._morning_sector_bias)}条) {' | '.join(parts)}"
                )
        except Exception as e:
            logger.warning(f"加载早盘板块倾向失败: {e}")

    @staticmethod
    @lru_cache(maxsize=settings.NAME_RESOLVE_CACHE_SIZE)
    def _resolve_name(code: str) -> str:
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
        self._index_alerted_ma20 = 0
        self._alert_fingerprints.clear()
        self._max_drawdown_alerted = False
        self._closing_decision_done = False

        in_trading = self._in_trading_hours()
        if in_trading:
            # 确保 QMT Collector 在运行（不在则自动拉起）
            self._ensure_collector_running()
            # 盘中重启（容灾路径）：先连 collector socket，再读 DB 恢复历史
            logger.info("检测到盘中重启，进入容灾恢复")
            self._connect_collector()
            self._restore_index_context()  # 从 index_snapshots 读
            self._restore_market_from_db()  # 从 market_snapshots 读
            if self._market_snapshot:
                self._update_sector_trends()  # 立即计算板块趋势
            self._recv_collector_data()  # 处理 socket buffer，去重
        else:
            # 盘前正常启动：确保 collector 在运行，再连
            self._ensure_collector_running()
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
        self._ai_queue.start()  # 启动后台 AI 调用线程
        self._load_morning_sector_bias()  # 加载早盘 AI 板块倾向

        # 等数据就绪：首轮 sector 数据到达才允许交易，最长等 3 分钟
        if in_trading:
            deadline = time.time() + 180
            while not self._data_ready and time.time() < deadline:
                self._connect_collector()
                self._recv_collector_data()
                if self._market_snapshot and self._sector_stats:
                    self._data_ready = True
                    self._data_ready_at = time.time()
                    logger.info("数据就绪，开始交易监控")
                    break
                time.sleep(2)
            if not self._data_ready:
                logger.warning("等待数据超时(3分钟)，将以无板块数据模式运行")

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

        # 处理已完成的异步 AI 结果（不阻塞）
        try:
            self._process_pending_ai()
        except Exception as e:
            logger.warning(f"处理异步AI结果异常: {e}", exc_info=True)

        # 每轮检测数据新鲜度
        try:
            self._check_data_stale()
        except Exception as e:
            logger.warning(f"数据新鲜度检查异常: {e}")

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

        # 主动退出检查（硬止损前识别该走的仓位）
        try:
            self._check_stale_positions(prices)
        except Exception as e:
            logger.warning(f"主动退出检查异常: {e}", exc_info=True)

        # 引擎2：盘中机会发现（每 SCOUT_INTERVAL 轮触发）
        if self._scan_count % IntradayScoutMixin.SCOUT_INTERVAL == 0:
            try:
                self._scout_intraday()
            except Exception as e:
                logger.warning(f"盘中机会扫描异常: {e}", exc_info=True)

        try:
            self._check_signals(prices, self._regime)
        except Exception as e:
            logger.warning(f"信号检查异常: {e}", exc_info=True)

        try:
            self._check_bought_signals(prices)
        except Exception as e:
            logger.warning(f"已买入信号检查异常: {e}", exc_info=True)

        # 引擎2 定期清理过期状态
        if self._scan_count % 30 == 0:
            self._scout_cleanup_stale()

        try:
            self._check_review_picks(prices, self._regime)
        except Exception as e:
            logger.warning(f"复盘精选检查异常: {e}", exc_info=True)

        # 盘中动态板块发现（每 3 轮，数据就绪后）
        try:
            if self._data_ready and self._scan_count % 3 == 0:
                dynamic = self._generate_hot_sector_candidates(prices)
                if dynamic:
                    self._check_buy_candidates(dynamic, self._regime)
        except Exception as e:
            logger.warning(f"动态板块发现异常: {e}", exc_info=True)

        # 盘中回踩机会发现（每 N 轮，走完整买入管线）
        try:
            if (
                self._data_ready
                and self._scan_count % settings.PULLBACK_SCAN_INTERVAL == 0
            ):
                opps = self._scan_pullback_opportunities(prices)
                if opps:
                    self._check_buy_candidates(opps, self._regime)
        except Exception as e:
            logger.warning(f"回踩机会扫描异常: {e}", exc_info=True)

        try:
            self._check_sl_reminders()
        except Exception as e:
            logger.warning(f"止损提醒异常: {e}", exc_info=True)

        try:
            # 开盘决策：9:30~9:40 内第一轮有数据的扫描发送
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
                        [
                            (k, round(v[-1], 2))
                            for k, v in self._sector_trend_history.items()
                            if len(v) >= 3
                        ],
                        key=lambda x: -x[1],
                    )[:5]
                    bottom3 = sorted(
                        [
                            (k, round(v[-1], 2))
                            for k, v in self._sector_trend_history.items()
                            if len(v) >= 3
                        ],
                        key=lambda x: x[1],
                    )[:3]
                    if top5 or bottom3:
                        self._log_sector_alert(
                            top_sectors=[[n, v] for n, v in top5],
                            bottom_sectors=[[n, v] for n, v in bottom3],
                            warnings=[],
                            good=[],
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
            self._check_index_divergence()
        except Exception as e:
            logger.warning(f"指数背离检查异常: {e}", exc_info=True)

        try:
            if self._scan_count % 3 == 0:
                self._check_sector_sharp_move()
        except Exception as e:
            logger.warning(f"板块异动检查异常: {e}", exc_info=True)

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

        # 指数停更检测：collector 在线 + 价格长时间不变才报警
        index_stale = False
        collector_ok = bool(
            getattr(self, "_collector_client", None)
            and getattr(self._collector_client, "connected", False)
        )
        if len(self._index_prices) >= 5:
            recent = self._index_prices[-5:]
            if max(recent) - min(recent) < 0.5:
                stale_count = getattr(self, "_index_stale_count", 0) + 1
                self._index_stale_count = stale_count
                # collector 不在线 + 连续 5 轮 → 真停更
                if not collector_ok and stale_count >= 5:
                    index_stale = True
            else:
                self._index_stale_count = 0

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
            index_close_high=getattr(self, "_index_close_high", 0) or self._index_high,
            index_close_low=getattr(self, "_index_close_low", 0) or self._index_low,
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
            scenario_probs=getattr(self, "_scenario_engine", None) and self._scenario_engine.probs or {},
            scenario_scan_count=getattr(self, "_scenario_engine", None) and self._scenario_engine.scan_count or 0,
        )
        self._prev_scan_count = self._scan_count

        alerts = run_checks(ctx)
        if index_stale:
            alerts.append("⚠️ 指数停更: 近 15 轮上证波动 < 0.01")

        if alerts:
            # 去重：同一告警指纹至少隔 10 轮再推
            new_alerts = []
            for a in alerts:
                fp = a[:40]  # 取前 40 字符做指纹（code+类型足够唯一）
                last_seen = self._health_alert_seen.get(fp, -999)
                if self._scan_count - last_seen >= 10:
                    new_alerts.append(a)
                    self._health_alert_seen[fp] = self._scan_count
            if new_alerts:
                msg = "🩺 健康检查\n" + "\n".join(f"  {a}" for a in new_alerts)
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
        # 重建时查 DB 获取每只持仓的真实买入日期
        buy_dates = {}
        try:
            rows = (
                sqlite3.connect(self.db_path)
                .execute(
                    """SELECT stock_code, MIN(date(order_time)) as buy_date
                   FROM trade_orders WHERE order_type='buy' AND order_status='filled'
                     AND account='paper'
                   GROUP BY stock_code"""
                )
                .fetchall()
            )
            buy_dates = {r[0]: r[1] for r in rows}
        except Exception:
            pass

        for code, pos in self.paper_account.positions.items():
            _meta = self._pos_meta.get(code, {})
            self._bought_watch[code] = {
                "entry_price": pos.avg_cost,
                "last_alert_scan": 0,
                "buy_scan": self._scan_count,
                "buy_trade_date": buy_dates.get(code, self._trade_date),
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

        now_ts = time.time()
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
                    price_f = float(price)
                    prices[code] = price_f
                    # 追踪最近10分钟价格（用于止跌确认）
                    if price_f > 0:
                        if code not in self._recent_prices:
                            self._recent_prices[code] = []
                        hist = self._recent_prices[code]
                        # 去重：价格未变化不重复记录
                        if not hist or hist[-1][1] != price_f:
                            hist.append((now_ts, price_f))
                        # 只保留最近10分钟
                        cutoff = now_ts - 600
                        self._recent_prices[code] = [
                            (t, p) for t, p in hist if t > cutoff
                        ]

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

        msg = self._resonance_analyzer.format_push_message(
            result, my_codes=set(self._bought_watch.keys())
        )
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
                    resonance_down=[
                        (n, round(c, 4))
                        for n, c, *_ in result.get("resonance_down", [])
                    ],
                    counter_up=[
                        (n, round(c, 4)) for n, c, *_ in result.get("counter_up", [])
                    ],
                )
            except Exception:
                pass

    def _ensure_collector_running(self):
        """确保 QMT Collector 进程在运行，不在则自动拉起。"""
        import socket
        import subprocess
        import time as _time

        # 快速检测：端口 15555 是否已被监听
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", 15555))
            s.close()
            return  # 已在运行
        except (ConnectionRefusedError, OSError):
            pass
        finally:
            s.close()

        logger.info("QMT Collector 未运行，自动拉起")
        try:
            import os as _os

            subprocess.Popen(
                [self._python_bin(), "main.py", "qmt-collect"],
                cwd=str(self._project_root()),
                env={**_os.environ, "PYTHONPATH": str(self._project_root())},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # 等它启动并开始采集
            _time.sleep(3)
        except Exception as e:
            logger.warning(f"拉起 QMT Collector 失败: {e}")

    @staticmethod
    def _python_bin() -> str:
        import sys

        return sys.executable

    @staticmethod
    def _project_root():
        from pathlib import Path

        return Path(__file__).resolve().parent.parent.parent

    def _check_data_stale(self):
        """检测数据断连：last_db_ts 超 3 分钟 → 私聊告警 + 暂停交易。"""
        if not self._data_ready:
            return
        if self._last_db_ts <= 0:
            return
        stale_sec = time.time() - self._last_db_ts
        if stale_sec > 180:  # 3 分钟
            self._data_ready = False
            self._alert_private(
                f"🚨 数据断连\n"
                f"   距离上次 Collector 数据已 {stale_sec:.0f} 秒\n"
                f"   暂停所有买入信号，恢复后自动重启"
            )
            logger.warning(f"数据断连: {stale_sec:.0f} 秒无数据，暂停交易")

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
        """处理 collector 推送的指数行情（支持多指数）。"""
        code = msg.get("code", "000001.SH")  # 兼容旧格式
        name = msg.get("name", "")
        index_price = msg["price"]

        # 多指数追踪
        if code not in self._index_map:
            self._index_map[code] = {
                "name": name,
                "prices": [],
                "high": index_price,
                "low": index_price,
                "last_price": index_price,
                "change_pct": 0,
                "pre_close": 0,
            }
        im = self._index_map[code]
        im["last_price"] = index_price
        # 优先使用 collector 计算好的 change_pct，如果为 0 且 pre_close 有效则自行计算
        raw_chg = msg.get("change_pct", 0)
        pre_close = msg.get("pre_close", 0)
        if pre_close > 0:
            im["pre_close"] = pre_close
            if raw_chg == 0:
                raw_chg = (index_price - pre_close) / pre_close * 100
        im["change_pct"] = raw_chg
        im["prices"].append(index_price)
        if index_price > im["high"]:
            im["high"] = index_price
        if index_price < im["low"]:
            im["low"] = index_price

        # 上证仍作为主指数（兼容现有逻辑）
        if code == "000001.SH":
            self._last_db_ts = max(self._last_db_ts, msg.get("ts", 0))
            self._last_index_quote = {
                "price": index_price,
                "pre_close": msg.get("pre_close", 0),
                "change_pct": msg.get("change_pct", 0),
                "amount": msg.get("amount", 0),
            }
            if self._index_high == 0 or index_price > self._index_high:
                self._index_high = index_price
            if self._index_low == 0 or index_price < self._index_low:
                self._index_low = index_price
            if self._index_close_high == 0 or index_price > self._index_close_high:
                self._index_close_high = index_price
            if self._index_close_low == 0 or index_price < self._index_close_low:
                self._index_close_low = index_price
            # 去重：collector 可能推送重复值（QMT 采样延迟），连续相同值会污染指标
            if not self._index_prices or self._index_prices[-1] != index_price:
                self._index_prices.append(index_price)

        amount = msg.get("amount", 0)
        if amount > 0:
            self._market_turnovers.append(amount)

    def _handle_collector_market(self, msg: dict):
        """处理 collector 推送的全市场快照。"""
        self._market_snapshot = msg.get("stocks", {})
        self._last_db_ts = max(self._last_db_ts, msg.get("ts", 0))

        if self._market_snapshot:
            # 实时市场宽度：涨跌家数
            up = down = flat = 0
            for item in self._market_snapshot.values():
                chg = item.get("changePct", 0)
                try:
                    chg = float(chg)
                except (ValueError, TypeError):
                    chg = 0
                if chg > 0:
                    up += 1
                elif chg < 0:
                    down += 1
                else:
                    flat += 1
            self._market_breadth = {
                "up": up,
                "down": down,
                "flat": flat,
                "total": up + down + flat,
            }

            # 全市场价格追踪（用于回踩机会发现的止跌判断）
            now_ts = time.time()
            cutoff = now_ts - 600
            for code, item in self._market_snapshot.items():
                try:
                    price_f = float(item.get("price", 0))
                except (ValueError, TypeError):
                    continue
                if price_f <= 0:
                    continue
                if code not in self._snapshot_price_history:
                    self._snapshot_price_history[code] = []
                hist = self._snapshot_price_history[code]
                if not hist or hist[-1][1] != price_f:
                    hist.append((now_ts, price_f))
                self._snapshot_price_history[code] = [
                    (t, p) for t, p in hist if t > cutoff
                ]

            self._update_sector_trends()
            if not self._data_ready and self._sector_stats:
                was_previously_ready = self._data_ready_at > 0
                self._data_ready = True
                self._data_ready_at = time.time()
                if was_previously_ready:
                    logger.info("数据恢复，重启交易监控")
                    self._alert_private("✅ 数据恢复\n   板块数据已恢复，重启交易监控")
                else:
                    logger.info("首轮板块数据到达，交易监控就绪")

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
            # 盘中重启：给回踩扫描器播种初始价格点，避免等10分钟才有数据
            now_ts = time.time()
            for code, item in self._market_snapshot.items():
                p = item.get("price", 0)
                if p and float(p) > 0:
                    self._snapshot_price_history[code] = [(now_ts, float(p))]
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

    def _submit_scenario_ai(self, key: str, scenario: str, **fields) -> bool:
        """使用场景模板提交 AI 异步评估。返回 True 表示已入队。"""
        from trade.monitor.prompts import build_prompt, get_template

        try:
            system_prompt, user_prompt, max_tokens = build_prompt(scenario, **fields)
        except (KeyError, ValueError) as e:
            logger.warning(f"场景AI模板构建失败 [{scenario}] {key}: {e}")
            return False

        tmpl = get_template(scenario)
        dedupe = tmpl.dedupe if tmpl else True
        return self._ai_queue.submit(
            key,
            user_prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            dedupe=dedupe,
        )

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
        *,
        intra_str: str = "",
        alert_key: str = "",
        chase_key: str = "",
    ) -> str:
        """追高/被拒场景提交 AI 异步评估。返回空字符串（异步，不阻塞扫描）。"""
        above_str = (
            f"超出买入区上限{above_pct:+.1f}%"
            if above_pct > 0
            else f"低于买入区下限{abs(above_pct):.1f}%"
        )
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

        akey = f"chase:{code}"
        ok = self._ai_queue.submit(akey, prompt, max_tokens=100, dedupe=True)
        if ok:
            # 保存上下文，等 AI 完成后重建提醒
            self._pending_chase[akey] = {
                "code": code,
                "name": name,
                "price": price,
                "buy_min": buy_min,
                "buy_max": buy_max,
                "sl": sl,
                "tp": tp,
                "trend": trend,
                "above_pct": above_pct,
                "reject_reason": reject_reason,
                "intra_str": intra_str,
                "alert_key": alert_key,
                "chase_key": chase_key,
                "submitted_at": time.time(),
            }
        return ""

    def _process_pending_ai(self):
        """处理已完成的异步 AI 结果，发送延迟提醒。"""
        # 1. 追高/二判 AI — 收集后按板块合并推送
        chase_items = []  # 本轮收集的所有追高/二判结果
        for akey in list(self._pending_chase.keys()):
            result = self._ai_queue.pop_result(akey)
            if result is None:
                ctx = self._pending_chase[akey]
                if time.time() - ctx["submitted_at"] > 60:
                    del self._pending_chase[akey]
                continue

            ctx = self._pending_chase.pop(akey)
            if not result:
                continue  # AI 返回空，不推送

            code, name = ctx["code"], ctx["name"]
            # 缓存 AI 结果
            if not hasattr(self, "_ai_chase_cache"):
                self._ai_chase_cache = {}
            self._ai_chase_cache[code] = result

            # 更新信号提醒状态
            signal_alert = getattr(self, "_signal_alert_state", {})
            review_alert = getattr(self, "_review_alert_state", {})
            alert_dict = (
                signal_alert if ctx["alert_key"] in signal_alert else review_alert
            )
            alert_dict[ctx["alert_key"]] = (ctx["price"], True)
            if ctx["chase_key"]:
                alert_dict[ctx["chase_key"]] = self._scan_count

            above_pct = ctx["above_pct"]
            title = "追高提醒" if above_pct > 0 else "暂不买入"

            # 冷却检查：同票 15 轮内且价格变化 < 2% → 跳过
            if self._should_throttle(code, ctx["price"]):
                continue

            # 提取板块行业名
            industry = self._industry_cache.get(code, "")

            chase_items.append(
                {
                    "code": code,
                    "name": name,
                    "price": ctx["price"],
                    "buy_min": ctx["buy_min"],
                    "buy_max": ctx["buy_max"],
                    "sl": ctx["sl"],
                    "tp": ctx["tp"],
                    "trend": ctx["trend"],
                    "above_pct": above_pct,
                    "reject_reason": ctx["reject_reason"],
                    "intra_str": ctx["intra_str"],
                    "result": result,
                    "title": title,
                    "industry": industry,
                }
            )

        # ── 合并推送：同板块 ≥3 只追高提醒 → 合并为一条 ──
        if chase_items:
            # 分组：按 (title, industry)
            groups: dict[tuple, list[dict]] = {}
            for item in chase_items:
                key = (
                    (item["title"], item["industry"])
                    if item["industry"]
                    else (item["title"], item["code"])
                )
                groups.setdefault(key, []).append(item)

            for (title, sector), items in groups.items():
                if len(items) >= 3 and title == "追高提醒":
                    # 合并推送
                    emoji = "📈"
                    lines = [f"{emoji} {title} — {sector}板块 {len(items)}只"]
                    for it in items[:8]:  # 最多展示8只
                        lines.append(
                            f"   {it['code']} {it['name']}  {it['price']:.2f}  超出{it['above_pct']:+.1f}%"
                        )
                    # 取第一只的 AI 分析作为板块级建议
                    ai_text = items[0]["result"]
                    lines.append(f"   ─────────────────────────\n   🤖 {ai_text}")
                    self._alert("\n".join(lines))
                else:
                    # 单只推送
                    for it in items:
                        emoji = "📈" if it["above_pct"] > 0 else "⏸️"
                        msg = (
                            f"{emoji} {it['title']} — {it['code']} {it['name']}\n"
                            f"   现价 {it['price']:.2f}  买入区 {it['buy_min']:.2f}~{it['buy_max']:.2f}"
                            f"  超出 {it['above_pct']:+.1f}%\n"
                            f"   止损 {it['sl']:.2f}  止盈 {it['tp']:.2f}\n"
                            f"   板块: {it['trend']}"
                        )
                        if it["intra_str"]:
                            msg += it["intra_str"]
                        if it["reject_reason"]:
                            msg += f"\n   ⛔ {it['reject_reason']}"
                        msg += f"\n   ─────────────────────────\n   🤖 {it['result']}"
                        self._alert(msg)

        # 2. 指数波动 AI
        index_result = self._ai_queue.pop_result("index_fluctuation")
        if index_result is not None and self._pending_index_ai:
            ctx = self._pending_index_ai.pop("index_fluctuation", None)
            if ctx and index_result:
                change_pct = ctx.get("change_pct", 0)
                direction = "急拉" if change_pct > 0 else "急跌"
                self._alert(f"🤖 指数{direction}AI分析\n{index_result}")

        # 3. 换仓评估 AI — 异步处理
        swap_result = self._ai_queue.pop_result("swap_eval")
        if swap_result is not None:
            self._handle_swap_ai_result(swap_result)

        # 4. 被套离场 AI — 每 10 轮检查一次异步结果
        if self._scan_count % 10 == 0:
            self._process_trapped_ai_results()

    def _handle_swap_ai_result(self, ai_text: str):
        """处理异步换仓 AI 结果，解析并执行换仓。"""
        ctx = getattr(self, "_swap_ctx", None)
        if not ctx or not ai_text:
            return
        del self._swap_ctx

        import json
        import re

        try:
            content = re.sub(r"```\w*\n?|```", "", ai_text).strip()
            result = json.loads(content)
            sell_code = result.get("sell")
            buy_code = result.get("buy")
        except Exception:
            logger.warning("AI 换仓结果 JSON 解析失败")
            return

        if not sell_code or not buy_code:
            logger.info("AI 换仓决策: 不换仓")
            return

        candidates = ctx["candidates"]
        pos_codes = set(self.paper_account.positions.keys())
        cand_codes = {c["code"] for c in candidates}

        if sell_code not in pos_codes or buy_code not in cand_codes:
            logger.warning(f"AI 换仓返回无效代码: sell={sell_code} buy={buy_code}")
            return

        logger.info(f"AI 换仓决策(异步): 卖{sell_code} 买{buy_code}")
        self._alert(f"🤖 AI 换仓建议: 卖 {sell_code} → 买 {buy_code}")

        # 执行换仓
        buy_cand = next((c for c in candidates if c["code"] == buy_code), None)
        if not buy_cand:
            return

        sell_pos = self.paper_account.positions.get(sell_code)
        sell_price = sell_pos.current_price if sell_pos else (buy_cand.get("price", 0))

        sell_meta = self._pos_meta.get(sell_code, {})
        sell_result = self.paper_account.sell(
            sell_code,
            sell_price,
            f"AI异步换仓→{buy_code}",
            signal_id=sell_meta.get("signal_id"),
        )
        if sell_result.success:
            self._pos_meta.pop(sell_code, None)
            self._bought_watch.pop(sell_code, None)
        else:
            return

        price = buy_cand["price"]
        max_affordable = int(self.paper_account.cash * 0.9 / price / 100) * 100
        volume = min(
            int(
                self.paper_account.total_value
                * settings.DEFAULT_POSITION_PCT
                / price
                / 100
            )
            * 100,
            max_affordable,
        )
        if volume < 100:
            return

        buy_result = self.paper_account.buy(
            buy_cand["code"],
            buy_cand.get("name", ""),
            price,
            volume,
            source="swap_ai_async",
        )
        if buy_result.success:
            self._pos_meta[buy_code] = {
                "sl": buy_cand.get("sl", 0),
                "tp": buy_cand.get("tp", 0),
                "trailing_stop": 0.05,
                "highest_price": price,
                "sector": buy_cand.get("sector", ""),
                "score": buy_cand.get("score", 0),
                "signal_id": None,
            }
            logger.info(f"AI 异步换仓完成: {sell_code}→{buy_code}")

    # ======================== 指数背离 / 板块急跌 ========================

    def _check_index_divergence(self):
        """检测多指数背离 + 日内高位回落。"""
        if len(self._index_map) < 2:
            return

        sh = self._index_map.get("000001.SH", {})
        gem = self._index_map.get("399006.SZ", {})
        kc = self._index_map.get("000688.SH", {})

        sh_chg = sh.get("change_pct", 0)
        gem_chg = gem.get("change_pct", 0)
        kc_chg = kc.get("change_pct", 0)

        if not hasattr(self, "_divergence_alerted"):
            self._divergence_alerted = {}

        # 创业板日内高位回落 ≥ 1.5%（即使绝对涨幅仍为正）
        gem_high = gem.get("high", 0)
        gem_price = gem.get("last_price", 0)
        if gem_high > 0 and gem_price > 0:
            drop_from_high = (gem_high - gem_price) / gem_high * 100
            if drop_from_high > 1.0:
                key4 = f"gem_peak_drop:{self._scan_count // 40}"
                if key4 not in self._divergence_alerted:
                    self._divergence_alerted[key4] = True
                    self._alert(
                        f"⚠️ 创业板高位回落\n"
                        f"   当前: {gem_price:.1f} ({gem_chg:+.2f}%)  "
                        f"日内高: {gem_high:.1f}  回落: -{drop_from_high:.1f}%\n"
                        f"   → 强势板块大面积回吐，注意科技股风险"
                    )

        # 上证涨 + 创业板已经跌（不只是回落，是逆转）
        if sh_chg > 0.1 and gem_chg < -0.5:
            key = f"diverge_gem:{self._scan_count // 20}"
            if key not in self._divergence_alerted:
                self._divergence_alerted[key] = True
                self._alert(
                    f"⚠️ 指数背离\n"
                    f"   上证: {sh_chg:+.2f}%  |  创业板: {gem_chg:+.2f}%  |  科创50: {kc_chg:+.2f}%\n"
                    f"   → 权重护盘小票出货，注意科技/小盘股风险"
                )

        # 创业板急跌（跌到负值）
        if gem_chg < -1.0:
            key2 = f"gem_selloff:{self._scan_count // 30}"
            if key2 not in self._divergence_alerted:
                self._divergence_alerted[key2] = True
                self._alert(
                    f"🔴 创业板急跌\n"
                    f"   创业板: {gem_chg:+.2f}%  现价: {gem.get('last_price', 0):.2f}\n"
                    f"   → 科技/成长股大面积回调，检查相关持仓"
                )

        # 科创50 急跌
        if kc_chg < -1.5:
            key3 = f"kc_selloff:{self._scan_count // 40}"
            if key3 not in self._divergence_alerted:
                self._divergence_alerted[key3] = True
                self._alert(
                    f"🔴 科创50急跌\n"
                    f"   科创50: {kc_chg:+.2f}%  现价: {kc.get('last_price', 0):.2f}\n"
                    f"   → 半导体/AI硬件大规模回调"
                )

    def _check_sector_sharp_move(self):
        """检测板块级急涨急跌，附带领涨/领跌个股。"""
        stats = getattr(self, "_sector_stats", {})
        if not stats:
            return

        held_sectors = set()
        for code, meta in self._pos_meta.items():
            s = meta.get("sector", "")
            if s:
                held_sectors.add(s)

        # 查领涨/领跌股（复用 market_snapshot 的实时数据）
        leaders = (
            self._get_sector_leaders() if hasattr(self, "_get_sector_leaders") else {}
        )

        alerts = []
        for name, s in stats.items():
            chg = s.get("change_pct", 0)
            leaders_in_sector = leaders.get(name, [])
            leader_str = ""
            if leaders_in_sector:
                stocks = [f"{c[0]} {c[1]:+.1f}%" for c in leaders_in_sector[:3]]
                leader_str = (
                    "\n     领" + ("涨" if chg > 0 else "跌") + ": " + ", ".join(stocks)
                )

            if chg < -1.5:
                marker = "🔴" if name in held_sectors else "⬇"
                alerts.append(
                    f"{marker} {name}: {chg:+.1f}%  "
                    f"涨跌{stats.get('up', 0)}/{stats.get('down', 0)}"
                    f"{leader_str}"
                )
            elif chg > 2.5:
                alerts.append(
                    f"🟢 {name}: {chg:+.1f}%  "
                    f"涨跌{stats.get('up', 0)}/{stats.get('down', 0)}"
                    f"{leader_str}"
                )

        if alerts:
            # 全局冷却：至少 40 轮不重复推（~7分钟），防止尾盘消息轰炸
            last_scan = getattr(self, "_last_sector_alert_scan", 0)
            if self._scan_count - last_scan < 40:
                return
            if not hasattr(self, "_sector_alerted"):
                self._sector_alerted: dict[str, int] = {}
            # 每个板块独立去重：至少 90 轮（~15分钟）才重复告警
            fresh = []
            for a in alerts:
                key = a[:12]  # 板块名前几位
                last = self._sector_alerted.get(key, 0)
                if self._scan_count - last > 90:
                    self._sector_alerted[key] = self._scan_count
                    fresh.append(a)
            if not fresh:
                return
            self._last_sector_alert_scan = self._scan_count
            up = any("🟢" in a for a in fresh)
            down = any("🔴" in a or "⬇" in a for a in fresh)
            title = "📊 板块急涨" if up and not down else ("⚠️ 板块急跌" if down else "")
            self._alert(title + "\n" + "\n".join(fresh[:5]))

    def _get_sector_leaders(self) -> dict[str, list[tuple]]:
        """从 market_snapshot 中提取每个板块的领涨/领跌股。"""
        snapshot = getattr(self, "_market_snapshot", {})
        if not snapshot:
            return {}
        # 确保行业缓存已加载
        self._ensure_industry_cache()

        by_sector: dict[str, list[tuple]] = {}
        for code, item in snapshot.items():
            industry = self._industry_cache.get(code, "")
            if not industry:
                continue
            chg = item.get("changePct", 0)
            if abs(chg) > 3:  # 只取涨跌 >3% 的
                if industry not in by_sector:
                    by_sector[industry] = []
                by_sector[industry].append((code, chg))

        # 每板块取 top 5 涨/跌
        result = {}
        for ind, stocks in by_sector.items():
            stocks.sort(key=lambda x: abs(x[1]), reverse=True)
            result[ind] = stocks[:5]
        return result

    # ======================== 推送 ========================

    def _should_throttle(
        self, code: str, price: float, cooldown_scans: int = 15
    ) -> bool:
        """推送冷却：同票同价区间内抑制重复推送。返回 True 表示应跳过。"""
        if code in self._push_cooldown:
            last_scan, last_price = self._push_cooldown[code]
            price_chg = abs(price - last_price) / last_price if last_price > 0 else 999
            if self._scan_count - last_scan < cooldown_scans and price_chg < 0.02:
                return True
        self._push_cooldown[code] = (self._scan_count, price)
        return False

    def _alert(self, msg: str):
        # ── 消息去重：同指纹 N 轮内不重复推送 ──
        fp = self._alert_fingerprint(msg)
        if fp:
            last = self._alert_fingerprints.get(fp, -999)
            cooldown = self._alert_cooldown(msg)
            if self._scan_count - last < cooldown:
                return  # 冷却中，跳过
            self._alert_fingerprints[fp] = self._scan_count
            # 定期清理过期指纹（每100轮）
            if self._scan_count % 100 == 0:
                stale = [
                    k
                    for k, v in self._alert_fingerprints.items()
                    if self._scan_count - v > 200
                ]
                for k in stale:
                    del self._alert_fingerprints[k]

        if self.telegram:
            try:
                self.telegram.send(msg)
            except Exception as e:
                logger.error(f"Telegram推送失败: {e}")
        logger.info(f"📤 Telegram: {msg}")

    def _alert_fingerprint(self, msg: str) -> str:
        """提取消息指纹：首行类型 + 股票代码（如有）"""
        import re

        first_line = msg.split("\n")[0] if msg else ""
        # 提取股票代码（6位数字）
        m = re.search(r"\b(\d{6})\b", first_line)
        if m:
            # 股票相关消息：类型+代码去重（不同股票不互斥）
            prefix = re.sub(r"\d{6}", "XXXXXX", first_line)
            return f"{prefix}:{m.group(1)}"
        # 大盘/板块消息：首行前40字符作为指纹
        return first_line[:40]

    def _alert_cooldown(self, msg: str) -> int:
        """不同类型消息的冷却轮数（~15s/轮）"""
        first_line = msg.split("\n")[0] if msg else ""
        if "大盘偏弱" in first_line:
            return 30  # 7.5分钟
        if "暂停买入" in first_line:
            return 20  # 5分钟
        if "追高提醒" in first_line:
            return 15  # 同只股票3.75分钟内不重复
        if "暂不买入" in first_line or "暂缓买入" in first_line:
            return 10
        if "逼近涨停" in first_line:
            return 12  # 3分钟
        if "板块热度" in first_line or "板块急" in first_line:
            return 20  # 5分钟
        if "止损未触发" in first_line:
            return 40  # 10分钟（健康检查已有自己的去重）
        return 3  # 默认45秒

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
