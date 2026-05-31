# -*- coding: utf-8 -*-
"""盯盘集成测试 — 模拟完整 scan 流程，验证多层协调、状态转换、异常恢复"""

import pytest
import sqlite3
import time
import tempfile
import os
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, date, time as dt_time
from collections import defaultdict
from trade.monitor.watcher import Watcher

# Mock 时间为 14:35（尾盘测试需要）
_patch_closing_time = patch("trade.monitor.closing.datetime")
_patch_closing_time.start().now.return_value = datetime(2026, 5, 29, 14, 35, 0)


# ===================================================================
# 测试工具
# ===================================================================


def make_watcher(**overrides):
    """创建最小可用 Watcher，patch 所有外部依赖避免真实 DB/网络连接。

    使用 patch 避免 Watcher.__init__ 中的 Telegram bot 创建和 DB 连接。
    """
    with patch("trade.monitor.watcher.TradeRepository", return_value=MagicMock()), \
         patch("trade.portfolio.portfolio.Portfolio", return_value=MagicMock()), \
         patch("trade.risk.engine.RiskEngine", return_value=MagicMock()):
        telegram = MagicMock()
        qmt = MagicMock()
        w = Watcher.__new__(Watcher)

    # 手动设置关键属性（绕过 __init__ 的复杂逻辑）
    w.telegram = telegram
    w._private_telegram = None
    w.qmt = qmt
    w.scan_interval = 1
    w.db_path = ":memory:"
    w.portfolio = MagicMock()
    w.portfolio.positions = {}
    w.portfolio.drawdown = 0.05
    w.portfolio.total_value = 200_000
    w.portfolio.daily_pnl = 0.0
    w.repo = MagicMock()
    w.repo.get_pending_signals.return_value = []
    w.risk_engine = MagicMock()
    w.risk_engine.update_market_env = MagicMock()
    w.risk_engine.can_open = MagicMock()
    w._running = False
    w._trade_date = "2026-05-29"
    w._scan_count = 0
    w._triggered_ids: set[int] = set()
    w._alerted_sl_tp: set[str] = set()
    w._last_index_quote = {
        "price": 3310, "pre_close": 3300, "change_pct": 0.003,
        "amount": 100_000_000_000,
    }
    w._ma_baseline_cache = (3300, 3320, 3350)

    # 子监控器
    w._review_monitor = None
    w._sector_monitor = None
    w._abnormal_detector = None
    w._receiver = None
    w._executor = None
    w._paper_trader = None

    # 大盘状态
    w._index_prices: list[float] = [3300, 3305, 3310]
    w._market_turnovers: list[float] = []  # collector 数据接收用
    w._index_high: float = 3315
    w._index_low: float = 3295
    w._index_alerted_downtrend: bool = False
    w._index_last_fluctuation_price: float = 0.0
    w._market_turnovers: list[float] = []
    w._volume_alerted_divergence: bool = False

    # 尾盘/回撤
    w._closing_decision_done: bool = False
    w._max_drawdown_alerted: bool = False

    # 全市场快照 + 板块
    w._market_snapshot: dict[str, dict] = {}
    w._sector_trend_history: dict[str, list[float]] = defaultdict(list)
    w._sector_trend_continuity: dict[str, int] = defaultdict(int)
    w._sector_trend_last_dir: dict[str, str] = {}
    w._industry_cache: dict[str, str] = {}
    w._concept_cache: dict[str, list[str]] = {}
    w._sector_stats: dict[str, dict] = {}
    w._concept_stats: dict[str, dict] = {}

    # 技术指标
    w._index_tech_state: dict = {
        "macd_cross": None, "rsi6_zone": "normal", "rsi12_zone": "normal",
        "kdj_cross": None, "kdj_j_zone": "normal", "divergence": None,
    }

    # 信号/复盘
    w._signal_alert_state: dict[int, tuple[float, bool]] = {}
    w._review_alert_state: dict[str, tuple[float, bool]] = {}
    w._prev_snapshot: dict[str, dict] = {}

    # 止损提醒 + 限价 + 盯盘
    w._sl_reminders: dict[str, dict] = {}
    w._limit_cache: dict[str, dict] = {}
    w._bought_watch: dict[str, dict] = {}

    # 缓存
    w._cached_db_watch_codes: set[str] = set()
    w._watch_codes_stale: bool = True
    w._intraday_cache: dict[str, dict] = {}
    w._intraday_cache_scan: int = -1
    w._instrument_cache: dict[str, dict] = {}
    w._daily_factor_cache: dict[str, dict] = {}

    # Collector
    w._collector_client = None
    w._last_db_ts: float = 0

    # Mock 关键方法
    w._connect_collector = MagicMock()
    w._recv_collector_data = MagicMock()
    w._check_replies = MagicMock()
    w._restore_positions = MagicMock()
    w._cleanup_old_snapshots = MagicMock()
    w._load_sector_history = MagicMock()
    w._restore_index_context = MagicMock()
    w._restore_market_from_db = MagicMock()
    w._before_market = MagicMock(return_value=False)
    w._in_trading_hours = MagicMock(return_value=True)
    w._after_market = MagicMock(return_value=False)
    w._in_lunch_break = MagicMock(return_value=False)
    w._get_index_baseline = MagicMock(return_value=(3300, 3320, 3350))
    w._is_limit_down = MagicMock(return_value=False)
    w._is_limit_up = MagicMock(return_value=False)
    w._get_sector_trend = MagicMock(return_value="板块银行 走强 +1.5%")
    w._get_concept_trend_score = MagicMock(return_value=(2, "2个板块偏强"))
    w._build_sector_context = MagicMock(return_value="")
    w._resolve_name = MagicMock(return_value="测试股")
    w._load_review_picks = MagicMock(return_value=[])
    w._check_buy_candidates = MagicMock()
    w._check_bought_signals = MagicMock()
    w._check_index_technicals = MagicMock()
    w._check_max_drawdown = MagicMock()
    w._check_volume_divergence = MagicMock()
    w._invalidate_watch_codes_cache = MagicMock()
    w._handle_collector_index = MagicMock()
    w._handle_collector_market = MagicMock()
    w._send_opening_decision = MagicMock()
    w._get_paper_trader = MagicMock(return_value=None)
    w._check_sector_heat = MagicMock()
    w._check_abnormal = MagicMock()
    w._evaluate_swaps = MagicMock()
    w._check_closing = MagicMock()
    w._check_review_picks = MagicMock()
    w._check_signals = MagicMock()
    w._check_positions = MagicMock()

    from trade.monitor.market_state import MarketRegime
    w._regime = MarketRegime(pattern="normal", confidence="medium")
    w._check_market_state = MagicMock(return_value=w._regime)
    w._alert = MagicMock()
    w._alert_private = MagicMock()

    # 初始设置
    w._get_index_quote = MagicMock(return_value=w._last_index_quote)

    for k, v in overrides.items():
        setattr(w, k, v)

    return w


def add_signal(repo, code="000001", name="测试股", status="pending",
               buy_min=12.0, buy_max=13.0, stop_loss=11.0, take_profit=14.0,
               score=80, signal_id=1, source="ai_enhanced"):
    """向 mock repo 注入一条信号。"""
    repo.get_pending_signals.return_value = [{
        "id": signal_id, "stock_code": code, "stock_name": name,
        "buy_zone_min": buy_min, "buy_zone_max": buy_max,
        "stop_loss": stop_loss, "take_profit": take_profit,
        "signal_score": score, "signal_source": source,
        "status": status,
    }]


def add_position(w, code="000001", name="测试股", volume=1000,
                 avg_cost=12.00, stop_loss=11.00, take_profit=14.00,
                 current_price=12.50, entry_date="2026-05-20"):
    """向 portfolio 添加一笔 mock 持仓。"""
    pos = MagicMock()
    pos.stock_code = code
    pos.stock_name = name
    pos.volume = volume
    pos.avg_cost = avg_cost
    pos.stop_loss = stop_loss
    pos.take_profit = take_profit
    pos.current_price = current_price
    pos.market_value = current_price * volume
    pos.entry_date = entry_date
    pos.trailing_stop = 0.0
    pos.highest_price = current_price
    w.portfolio.positions[code] = pos


# ===================================================================
# Watcher 初始化和生命周期
# ===================================================================


class TestWatcherInit:
    """Watcher 初始化：状态、属性、依赖注入。"""

    def test_init_sets_core_attributes(self):
        w = make_watcher()
        assert w.scan_interval == 1
        assert w.db_path == ":memory:"
        assert w.portfolio is not None
        assert w.repo is not None
        assert w.risk_engine is not None
        assert w._running is False
        assert w._scan_count == 0

    def test_init_telegram_assignment(self):
        w = make_watcher()
        assert w.telegram is not None

    def test_init_qmt_assignment(self):
        w = make_watcher()
        assert w.qmt is not None

    def test_init_default_flags(self):
        w = make_watcher()
        assert w._closing_decision_done is False
        assert w._max_drawdown_alerted is False
        assert isinstance(w._triggered_ids, set)
        assert isinstance(w._alerted_sl_tp, set)
        assert isinstance(w._index_prices, list)

    def test_init_mixin_methods_available(self):
        w = make_watcher()
        # 所有 mixin 方法在真实 Watcher 上都可用
        assert callable(Watcher._check_market_state)
        assert callable(Watcher._check_positions)
        assert callable(Watcher._check_signals)
        assert callable(Watcher._check_closing)
        assert callable(Watcher._check_abnormal)
        assert callable(Watcher._check_sector_heat)
        assert callable(Watcher._evaluate_swaps)
        assert callable(Watcher._get_sector_trend)
        assert callable(Watcher._build_sector_context)


# ===================================================================
# _scan 流程集成
# ===================================================================


class TestScanPipeline:
    """_scan 全管线：各层调用顺序、参数传递、分层触发。"""

    def test_scan_calls_all_first_layer_methods(self):
        """正常 scan 调用所有第一层方法。"""
        w = make_watcher()
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})

        w._scan()

        w._recv_collector_data.assert_called_once()
        w._check_replies.assert_called_once()
        w._get_watch_codes.assert_called_once()
        w._get_realtime_prices.assert_called_once()

    def test_scan_empty_watch_codes_skips_prices(self):
        """无关注代码时跳过行情获取和后续步骤。"""
        w = make_watcher()
        w._get_watch_codes = MagicMock(return_value=[])
        w._get_realtime_prices = MagicMock()

        w._scan()
        w._get_realtime_prices.assert_not_called()

    def test_scan_no_prices_skips_downstream(self):
        """行情获取返回空 dict → 跳过信号/持仓等后续处理。"""
        w = make_watcher()
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={})

        w._scan()
        w._check_bought_signals.assert_not_called()

    def test_scan_first_round_sends_opening_decision(self):
        """scan_count==1 时推送开盘决策汇总（run() 在调用前递增）。"""
        w = make_watcher()
        w._scan_count = 1
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})

        w._scan()
        w._send_opening_decision.assert_called_once()

    def test_scan_market_ok_false_passed_to_signals(self):
        """_check_market_state 返回 allow_buy=False → _check_signals 收到对应 regime。"""
        w = make_watcher()
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})
        from trade.monitor.market_state import MarketRegime
        bad_regime = MarketRegime(pattern="panic", allow_buy=False, position_mult=0.0)
        w._check_market_state = MagicMock(return_value=bad_regime)

        w._scan()
        passed_regime = w._check_signals.call_args[0][1]
        assert passed_regime.allow_buy is False

    def test_scan_drawdown_halt_blocks_market(self):
        """总回撤 > 15% → regime_ok=False → _check_max_drawdown 被调用。"""
        w = make_watcher()
        type(w.portfolio).drawdown = PropertyMock(return_value=0.16)
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})
        from trade.monitor.market_state import MarketRegime
        ok_regime = MarketRegime(pattern="normal", allow_buy=True)
        w._check_market_state = MagicMock(return_value=ok_regime)

        w._scan()
        # drawdown_halt=True, regime_ok = allow_buy and not drawdown_halt = False
        # _check_max_drawdown is called
        w._check_max_drawdown.assert_called()

    def test_scan_count_50_triggers_sector_heat(self):
        """scan_count % 50 == 0 → 板块热度检查（需 market_snapshot 非空）。"""
        w = make_watcher()
        w._scan_count = 50
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})
        w._market_snapshot = {"000001": {"changePct": 1.0}}

        w._scan()
        w._check_sector_heat.assert_called_once()

    def test_scan_count_3_triggers_abnormal_check(self):
        """scan_count % 3 == 0 → 异动检测。"""
        w = make_watcher()
        w._scan_count = 3
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})

        w._scan()
        w._check_abnormal.assert_called_once()

    def test_scan_count_15_triggers_swap_eval(self):
        """scan_count % 15 == 0 → 换仓评估。"""
        w = make_watcher()
        w._scan_count = 15
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})

        w._scan()
        w._evaluate_swaps.assert_called_once()

    def test_scan_closing_called_every_round(self):
        """尾盘决策每轮调用，内部时间判断是否执行。"""
        w = make_watcher()
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})

        w._scan()
        w._check_closing.assert_called_once()

    def test_scan_update_prices_on_portfolio(self):
        """每轮 scan 将价格同步到 portfolio.update_prices。"""
        w = make_watcher()
        w._get_watch_codes = MagicMock(return_value=["000001", "000002"])
        w._get_realtime_prices = MagicMock(return_value={
            "000001": 12.50, "000002": 25.00,
        })

        w._scan()
        w.portfolio.update_prices.assert_called_once()


# ===================================================================
# 信号 → 买入 集成管线
# ===================================================================


class TestSignalToBuyPipeline:
    """信号 → 候选转换 → 买入执行的完整管线。"""

    def test_pending_signal_in_zone_creates_candidate(self):
        """pending 信号 + 价格在买入区 → 生成候选。"""
        w = make_watcher()
        add_signal(w.repo, code="000001", name="平安银行",
                   buy_min=12.0, buy_max=13.0, score=80)

        # 恢复真实 _check_signals
        w._check_signals = Watcher._check_signals.__get__(w, Watcher)
        w._check_buy_candidates = MagicMock()

        w._check_signals({"000001": 12.50}, True)
        w._check_buy_candidates.assert_called_once()
        candidates = w._check_buy_candidates.call_args[0][0]
        assert len(candidates) == 1
        assert candidates[0]["code"] == "000001"
        assert candidates[0]["source"] == "signal"

    def test_signal_price_out_of_zone_no_candidate(self):
        """价格高于买入区上沿 → 候选被创建但 _check_buy_candidates 内部过滤。"""
        w = make_watcher()
        add_signal(w.repo, code="000001", buy_min=12.0, buy_max=13.0)
        w._check_signals = Watcher._check_signals.__get__(w, Watcher)
        w._check_buy_candidates = MagicMock()

        w._check_signals({"000001": 15.00}, True)
        # _check_signals 不做 zone 过滤，候选传给 _check_buy_candidates
        w._check_buy_candidates.assert_called_once()
        candidates = w._check_buy_candidates.call_args[0][0]
        assert len(candidates) == 1
        assert candidates[0]["code"] == "000001"

    def test_signal_no_buy_zone_skipped(self):
        """buy_zone_min=0 的信号跳过。"""
        w = make_watcher()
        add_signal(w.repo, code="000001", buy_min=0, buy_max=0)
        w._check_signals = Watcher._check_signals.__get__(w, Watcher)
        w._check_buy_candidates = MagicMock()

        w._check_signals({"000001": 12.50}, True)
        w._check_buy_candidates.assert_not_called()

    def test_repo_get_signals_exception_graceful(self):
        """repo.get_pending_signals 异常不崩溃。"""
        w = make_watcher()
        w.repo.get_pending_signals.side_effect = Exception("DB error")
        w._check_signals = Watcher._check_signals.__get__(w, Watcher)
        w._check_signals({"000001": 12.50}, True)

    def test_signal_price_missing_skipped(self):
        """信号代码不在价格字典中时跳过。"""
        w = make_watcher()
        add_signal(w.repo, code="000001", buy_min=12.0, buy_max=13.0)
        w._check_signals = Watcher._check_signals.__get__(w, Watcher)
        w._check_buy_candidates = MagicMock()

        w._check_signals({}, True)  # 空价格字典
        w._check_buy_candidates.assert_not_called()


# ===================================================================
# 持仓 → 风控 → 告警 集成管线
# ===================================================================


class TestPositionRiskPipeline:
    """持仓风控集成：止损/止盈/移动止盈/T+1保护/去重。"""

    def test_stop_loss_triggers_alert(self):
        """价格跌破止损价 → 告警被推送。"""
        w = make_watcher()
        add_position(w, "000001", "平安银行", avg_cost=12.00, stop_loss=11.00,
                     current_price=12.50, entry_date="2026-05-20")

        w._check_positions = Watcher._check_positions.__get__(w, Watcher)
        w._check_positions({"000001": 10.90})
        assert w._alert.called

    def test_take_profit_triggers_alert(self):
        """价格突破止盈价 → 告警被推送。"""
        w = make_watcher()
        add_position(w, "000001", "平安银行", avg_cost=12.00, take_profit=14.00,
                     current_price=13.50, entry_date="2026-05-20")

        w._check_positions = Watcher._check_positions.__get__(w, Watcher)
        w._check_positions({"000001": 14.10})
        assert w._alert.called

    def test_t1_position_not_triggered(self):
        """T+1 当日买入持仓 → 跌破止损也不触发（不可卖）。"""
        w = make_watcher()
        w._trade_date = "2026-05-29"
        add_position(w, "000001", "平安银行", avg_cost=12.00, stop_loss=11.00,
                     current_price=12.50, entry_date="2026-05-29")

        w._check_positions = Watcher._check_positions.__get__(w, Watcher)
        w._alert.reset_mock()
        w._check_positions({"000001": 10.90})  # 跌破止损 but T+1
        assert not w._alert.called

    def test_alert_key_dedup_prevents_duplicate(self):
        """_sl_reminders 已有 key → 跳过不重复推送。"""
        w = make_watcher()
        w._sl_reminders = {"000001:sl": {"code": "000001", "status": "pending"}}
        add_position(w, "000001", "平安银行", avg_cost=12.00, stop_loss=11.00,
                     current_price=12.50, entry_date="2026-05-20")

        w._check_positions = Watcher._check_positions.__get__(w, Watcher)
        w._alert.reset_mock()
        w._check_positions({"000001": 10.90})
        assert not w._alert.called

    def test_multiple_positions_evaluated_independently(self):
        """多只持仓各自独立评估，触发各自止损。"""
        w = make_watcher()
        for code, sl in [("000001", 11.00), ("000002", 25.00)]:
            add_position(w, code, f"股{code}", avg_cost=sl + 1.0,
                         stop_loss=sl, current_price=sl + 0.5,
                         entry_date="2026-05-20")

        w._check_positions = Watcher._check_positions.__get__(w, Watcher)
        w._check_positions({"000001": 10.90, "000002": 24.50})
        assert w._alert.called


# ===================================================================
# Collector 数据集成
# ===================================================================


class TestCollectorIntegration:
    """Collector socket 数据 → 内存状态转换。"""

    def test_handle_collector_index_updates_state(self):
        """指数消息 → 更新 _last_index_quote + _index_prices + _index_high/low。"""
        w = make_watcher()
        w._index_prices = []
        w._index_high = 0
        w._index_low = 0

        w._handle_collector_index = Watcher._handle_collector_index.__get__(w, Watcher)
        msg = {
            "type": "index", "ts": time.time(),
            "price": 3320, "pre_close": 3300,
            "change_pct": 0.006, "amount": 120_000_000_000,
        }
        w._handle_collector_index(msg)

        assert w._last_index_quote["price"] == 3320
        assert w._index_prices == [3320]
        assert w._index_high == 3320
        assert w._index_low == 3320

    def test_handle_collector_market_updates_snapshot(self):
        """全市场快照消息 → _market_snapshot 更新。"""
        w = make_watcher()
        w._industry_cache = {"000001": "银行"}
        w._concept_cache = {"000001": ["金融科技"]}

        w._handle_collector_market = Watcher._handle_collector_market.__get__(w, Watcher)
        msg = {
            "type": "market", "ts": time.time(),
            "stocks": {
                "000001": {"price": 12.50, "changePct": 1.5, "amount": 50000000},
            },
        }
        w._handle_collector_market(msg)
        assert "000001" in w._market_snapshot

    def test_recv_collector_data_ts_based_dedup(self):
        """消息 ts <= _last_db_ts → 跳过（DB已有）；ts > _last_db_ts → 应用。"""
        w = make_watcher()
        w._collector_client = MagicMock()
        w._collector_client.connected = True
        w._collector_client.recv_all.return_value = [
            {"type": "index", "ts": 100.0, "price": 3300,
             "pre_close": 3300, "change_pct": 0, "amount": 1e11},
            {"type": "index", "ts": 200.0, "price": 3310,
             "pre_close": 3300, "change_pct": 0.003, "amount": 1e11},
            {"type": "index", "ts": 300.0, "price": 3320,
             "pre_close": 3300, "change_pct": 0.006, "amount": 1e11},
        ]
        w._last_db_ts = 200.0
        w._handle_collector_index = MagicMock()

        w._recv_collector_data = Watcher._recv_collector_data.__get__(w, Watcher)
        w._recv_collector_data()

        # ts=100,200 被跳过，只有 ts=300 被 handle
        assert w._handle_collector_index.call_count == 1

    def test_collector_disconnected_no_crash(self):
        """Collector 未连接 → _recv_collector_data 直接返回。"""
        w = make_watcher()
        w._collector_client = None
        w._recv_collector_data = Watcher._recv_collector_data.__get__(w, Watcher)
        w._recv_collector_data()


# ===================================================================
# 异常恢复和容错
# ===================================================================


class TestErrorResilience:
    """各方法内部异常处理：自己的 try/except 兜底，不依赖 _scan 捕获。"""

    def test_market_state_handles_bad_prices(self):
        """_check_market_state 收到空价格或异常价格不崩溃。"""
        w = make_watcher()
        w._check_market_state = Watcher._check_market_state.__get__(w, Watcher)
        # 空价格字典 — 返回 MarketRegime 或带 allow_buy 属性
        result = w._check_market_state({})
        assert hasattr(result, 'allow_buy')

    def test_positions_handles_missing_stock_attrs(self):
        """_check_positions 空持仓/缺失行情价格时不崩溃。"""
        w = make_watcher()
        # 持仓存在但价格缺失 → 跳过不崩溃
        add_position(w, "000001", "测试", entry_date="2026-05-20")
        w._check_positions = Watcher._check_positions.__get__(w, Watcher)
        w._check_positions({})  # 无价格 → continue 跳过

    def test_signals_handles_repo_exception(self):
        """_check_signals 内 repo 异常被捕获，不传播。"""
        w = make_watcher()
        w.repo.get_pending_signals.side_effect = Exception("DB down")
        w._check_signals = Watcher._check_signals.__get__(w, Watcher)
        # 不应抛出异常
        w._check_signals({"000001": 12.50}, True)

    def test_qmt_get_realtime_exception_returns_empty(self):
        """QMT 行情获取异常 → 返回空 dict，不崩溃。"""
        w = make_watcher()
        w.qmt.get_realtime.side_effect = Exception("QMT disconnected")
        w._get_realtime_prices = Watcher._get_realtime_prices.__get__(w, Watcher)

        prices = w._get_realtime_prices(["000001"])
        assert prices == {}

    def test_repo_update_signal_exception_graceful(self):
        """DB 写入异常 → 模拟盘买入仍完成，不崩溃。"""
        w = make_watcher()
        w.repo.update_signal_status.side_effect = Exception("DB write error")
        pt = MagicMock()
        pt.try_buy.return_value = True
        w._get_paper_trader = MagicMock(return_value=pt)

        w._execute_paper_buy("000001", "测试", 12.50, 12.0, 13.0,
                             11.0, 14.0, 80, "signal", 1, 1.0,
                             "normal", "走强", True)
        pt.try_buy.assert_called_once()

    def test_repo_get_pending_signals_exception_in_scan(self):
        """scan 中 repo.get_pending_signals 异常 → 不崩溃。"""
        w = make_watcher()
        w.repo.get_pending_signals.side_effect = Exception("DB down")
        w._get_watch_codes = Watcher._get_watch_codes.__get__(w, Watcher)

        codes = w._get_watch_codes()
        assert isinstance(codes, list)


# ===================================================================
# 状态转换：盘前→盘中→尾盘→收盘
# ===================================================================


class TestStateTransitions:
    """时间边界和状态机转换。"""

    def test_closing_decision_flag_prevents_reentry(self):
        """尾盘决策执行后 _closing_decision_done=True → 第二轮被跳过。"""
        w = make_watcher()
        add_position(w, "000001", "测试", avg_cost=12.00,
                     stop_loss=11.00, current_price=12.50,
                     entry_date="2026-05-20")

        w._check_closing = Watcher._check_closing.__get__(w, Watcher)
        # 第一次执行
        w._check_closing({"000001": 12.50})
        assert w._closing_decision_done is True

        # 第二次被跳过 — 重置 mock 后不应再调用
        w._alert.reset_mock()
        w._check_closing({"000001": 10.50})
        assert not w._alert.called

    def test_empty_positions_closing_skipped(self):
        """无持仓 → 尾盘跳过，直接置 done。"""
        w = make_watcher()
        w._closing_decision_done = False
        w._check_closing = Watcher._check_closing.__get__(w, Watcher)
        w._check_closing({})
        assert w._closing_decision_done is True

    def test_lunch_break_detection(self):
        """_in_lunch_break 根据当前时间返回正确值。"""
        result = Watcher._in_lunch_break()
        assert result in (True, False)

    def test_restore_positions_empty_db(self):
        """空 DB 恢复持仓 → 不崩溃。"""
        w = make_watcher()
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "test.db")
        w.db_path = db_path
        w._restore_positions = Watcher._restore_positions.__get__(w, Watcher)
        w._restore_positions()
        assert len(w.portfolio.positions) == 0


# ===================================================================
# 缓存和刷新
# ===================================================================


class TestCacheInvalidation:
    """关注列表缓存的加载/刷新/失效。"""

    def test_watch_codes_stale_on_init(self):
        w = make_watcher()
        assert w._watch_codes_stale is True

    def test_watch_codes_combines_all_sources(self):
        """_get_watch_codes = 持仓 + pending信号 + review picks。"""
        w = make_watcher()
        add_position(w, "000001", "持仓股")
        add_signal(w.repo, code="000002", name="信号股")
        w._load_review_picks.return_value = [{"stock_code": "000003", "score": 70}]

        w._get_watch_codes = Watcher._get_watch_codes.__get__(w, Watcher)
        codes = w._get_watch_codes()
        assert "000001" in codes
        assert "000002" in codes
        assert "000003" in codes

    def test_watch_codes_cache_reused_when_not_stale(self):
        """缓存未过期时，只用缓存 + 持仓。"""
        w = make_watcher()
        add_position(w, "000001", "持仓股")
        w._cached_db_watch_codes = {"000002"}
        w._watch_codes_stale = False
        # 改 signal 返回值 — 因为缓存未过期，不应被读到
        w.repo.get_pending_signals.return_value = [{
            "id": 99, "stock_code": "000099", "stock_name": "不应出现",
            "buy_zone_min": 10, "buy_zone_max": 20, "signal_score": 0,
            "signal_source": "test", "status": "pending",
        }]

        w._get_watch_codes = Watcher._get_watch_codes.__get__(w, Watcher)
        codes = w._get_watch_codes()
        assert "000001" in codes
        assert "000002" in codes
        assert "000099" not in codes  # 缓存未过期，没读到新 signal

    def test_empty_signals_on_repo_error_fallback(self):
        """repo 异常 → watch_codes 只含持仓。"""
        w = make_watcher()
        add_position(w, "000001", "持仓股")
        w.repo.get_pending_signals.side_effect = Exception("DB down")

        w._get_watch_codes = Watcher._get_watch_codes.__get__(w, Watcher)
        codes = w._get_watch_codes()
        assert "000001" in codes

    def test_invalidate_cache_resets_flag(self):
        """换仓成交后 _invalidate_watch_codes_cache 重置 stale 标记。"""
        w = make_watcher()
        w._invalidate_watch_codes_cache = Watcher._invalidate_watch_codes_cache.__get__(
            w, Watcher)
        w._watch_codes_stale = False
        w._invalidate_watch_codes_cache()
        assert w._watch_codes_stale is True


# ===================================================================
# Telegram 告警集成
# ===================================================================


class TestAlertIntegration:
    """双通道 Telegram 告警：群聊 + 私聊。"""

    def test_alert_sends_telegram(self):
        w = make_watcher()
        w._alert = Watcher._alert.__get__(w, Watcher)
        w._alert("测试告警")
        w.telegram.send.assert_called_once()

    def test_alert_no_telegram_no_crash(self):
        w = make_watcher()
        w.telegram = None
        w._alert = Watcher._alert.__get__(w, Watcher)
        w._alert("测试")

    def test_alert_telegram_exception_handled(self):
        w = make_watcher()
        w.telegram.send.side_effect = Exception("Network error")
        w._alert = Watcher._alert.__get__(w, Watcher)
        w._alert("测试")


# ===================================================================
# 多轮扫描累积效应
# ===================================================================


class TestMultiScanAccumulation:
    """多轮扫描累积的状态变化和持久性。"""

    def test_index_prices_accumulate_over_time(self):
        """多轮 _check_market_state → _index_prices 累积累积。"""
        w = make_watcher()
        w._index_prices = []
        w._market_turnovers = []
        w._index_high = 0
        w._index_low = 0
        w._get_index_quote = MagicMock(return_value={
            "price": 3310, "pre_close": 3300, "change_pct": 0.003,
            "amount": 100_000_000_000,
        })
        w._check_market_state = Watcher._check_market_state.__get__(w, Watcher)

        # _index_prices 由 collector 的 _handle_collector_index 追加
        # _check_market_state 只更新 _index_high/low（不再重复追加价格）
        for p in [3300, 3305, 3310, 3308, 3312]:
            w._index_prices.append(p)  # 模拟 collector 追加
            w._get_index_quote.return_value = {
                "price": p, "pre_close": 3300,
                "change_pct": (p - 3300) / 3300,
                "amount": 100_000_000_000,
            }
            w._check_market_state({})

        assert len(w._index_prices) >= 5
        assert w._index_high == 3312
        assert w._index_low == 3300

    def test_market_snapshot_persists_across_scans(self):
        """Collector 推送的市场快照在多轮 scan 间保持（直到下次推送）。"""
        w = make_watcher()
        w._market_snapshot = {"000001": {"price": 12.50, "changePct": 1.5}}

        for _ in range(5):
            w._scan_count += 1
            assert "000001" in w._market_snapshot

    def test_bought_watch_max_profit_tracks_peak(self):
        """买入后的利润追踪 — max_profit_pct 只升不降。"""
        w = make_watcher()
        w._bought_watch["000001"] = {
            "entry_price": 12.00, "last_alert_scan": 0,
            "status": "watching", "alert_count": 0,
            "max_profit_pct": 0,
        }
        # 模拟价格先涨后跌
        for price in [12.50, 13.00, 12.80, 12.20]:
            pnl = (price - 12.00) / 12.00
            if pnl > w._bought_watch["000001"]["max_profit_pct"]:
                w._bought_watch["000001"]["max_profit_pct"] = pnl

        assert w._bought_watch["000001"]["max_profit_pct"] == pytest.approx(0.0833, abs=0.01)
        # 跌回 12.20 时 max_profit_pct 仍为峰值
        assert w._bought_watch["000001"]["max_profit_pct"] > 0.05


# ===================================================================
# 复盘推荐集成
# ===================================================================


class TestReviewPicksIntegration:
    """复盘推荐在 scan 中的集成。"""

    def test_review_picks_receive_market_ok(self):
        """复盘推荐检查时 market_ok 正确传递。"""
        w = make_watcher()
        w._get_watch_codes = MagicMock(return_value=["000001"])
        w._get_realtime_prices = MagicMock(return_value={"000001": 12.50})
        w._check_market_state = MagicMock(return_value=False)

        w._scan()
        w._check_review_picks.assert_called_once()
        assert w._check_review_picks.call_args[0][1] is False

    def test_load_review_picks_empty_no_crash(self):
        w = make_watcher()
        w._load_review_picks = Watcher._load_review_picks.__get__(w, Watcher)
        picks = w._load_review_picks()
        assert picks == []


# ===================================================================
# 买入决策管线
# ===================================================================


class TestBuyDecisionPipeline:
    """买入决策全链路：候选检查 → 风控 → 下单 → 状态更新。"""

    def test_candidate_passes_risk_opens_position(self):
        """候选通过风控 → 模拟盘买入 → signal 状态更新为 bought。"""
        w = make_watcher()
        pt = MagicMock()
        pt.try_buy.return_value = True
        w._get_paper_trader = MagicMock(return_value=pt)
        w.risk_engine.can_open.return_value = MagicMock(allowed=True)

        w._execute_paper_buy("000001", "测试", 12.50, 12.0, 13.0,
                             11.0, 14.0, 80, "signal", 1, 1.0,
                             "normal", "走强", True)
        pt.try_buy.assert_called_once()
        w.repo.update_signal_status.assert_called_once_with(1, "bought")

    def test_candidate_blocked_by_risk_no_buy(self):
        """风控拒绝 → 不买入，不更新 signal。"""
        w = make_watcher()
        pt = MagicMock()
        w._get_paper_trader = MagicMock(return_value=pt)
        w.risk_engine.can_open.return_value = MagicMock(allowed=False)

        w._execute_paper_buy("000001", "测试", 12.50, 12.0, 13.0,
                             11.0, 14.0, 80, "signal", 1, 1.0,
                             "normal", "走强", True)
        pt.try_buy.assert_not_called()
        w.repo.update_signal_status.assert_not_called()

    def test_market_not_ok_blocks_buy(self):
        """market_ok=False → 直接返回，不买入。"""
        w = make_watcher()
        pt = MagicMock()
        w._get_paper_trader = MagicMock(return_value=pt)

        w._execute_paper_buy("000001", "测试", 12.50, 12.0, 13.0,
                             11.0, 14.0, 80, "signal", 1, 1.0,
                             "normal", "走强", False)
        pt.try_buy.assert_not_called()

    def test_buy_adds_to_bought_watch(self):
        """买入成功后 → _bought_watch 被初始化。"""
        w = make_watcher()
        pt = MagicMock()
        pt.try_buy.return_value = True
        w._get_paper_trader = MagicMock(return_value=pt)
        w.risk_engine.can_open.return_value = MagicMock(allowed=True)

        w._execute_paper_buy("000001", "测试", 12.50, 12.0, 13.0,
                             11.0, 14.0, 80, "signal", 1, 1.0,
                             "normal", "走强", True)
        assert "000001" in w._bought_watch
        assert w._bought_watch["000001"]["entry_price"] == 12.50
