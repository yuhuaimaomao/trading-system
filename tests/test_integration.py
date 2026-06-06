"""集成测试 — 验证重构后所有委托方法不崩溃、输出类型正确。

关键约束：周一开盘前所有代码路径必须可执行。本测试构造最小 Watcher 实例，
逐一调用每个方法，验证不抛异常、返回值类型正确。
"""

import pytest
from collections import defaultdict
from trade.monitor.state import MarketRegime, MicroSignals, MarketScenario, MarketOutlook


class FakePaperAccount:
    """最小 PaperAccount 替身，提供各 Mixin 需要的属性。"""
    def __init__(self):
        self.positions = {}
        self.cash = 180000
        self.total_value = 200000
        self.daily_pnl = 0
        self.position_count = 0
        self._peak_value = 200000

    def update_prices(self, prices): pass
    def buy(self, code, name, price, volume, signal_id=None, source=""):
        class R: success = True; cost = price * volume; commission = 5; reason = "ok"
        return R()
    def sell(self, code, price, reason="", signal_id=None):
        class R: success = True; pnl = 0; pnl_pct = 0; proceeds = 0; commission = 5; reason = reason
        return R()


class TestWatcher:
    """模拟完整 Watcher，包含所有 Mixin 需要的最小状态。"""

    def __init__(self):
        from trade.monitor.watcher import Watcher
        from trade.message import AlertRouter
        # 拿真正的 MRO 但不真正初始化
        self.__class__ = Watcher  # 借用 MRO
        self.alerter = AlertRouter()
        self.alerter.new_round(0)
        # 核心状态
        self.paper_account = FakePaperAccount()
        self.db_path = ":memory:"
        self.qmt = None
        self.telegram = None
        self._private_telegram = None
        self.risk_engine = None
        self.repo = None
        self._running = False
        self._trade_date = "2026-06-06"
        self._scan_count = 0
        self._regime = MarketRegime()
        # 缓存
        self._industry_cache = {"000001": "银行"}
        self._concept_cache = {}
        self._sector_stats = {"银行": {"change_pct": 1.5, "trend_history": [0.0, 0.3, 0.5, 0.8, 1.0, 1.2],
                                        "relative": 0.6, "breadth": 0.4, "vol_ratio": 1.2, "continuity": 3}}
        self._concept_stats = {}
        self._intraday_cache = {}
        self._intraday_cache_scan = -1
        self._daily_factor_cache = {}
        self._instrument_cache = {}
        self._limit_cache = {}
        self._ma_baseline_cache = None
        self._market_breadth = {"up": 500, "down": 300, "flat": 100, "total": 900}
        self._market_snapshot = {"000001": {"price": 10.0, "changePct": 2.0, "amount": 50000000}}
        self._market_turnovers = []
        self._index_prices = [3400 + i * 0.5 for i in range(100)]
        self._index_high = max(self._index_prices)
        self._index_low = min(self._index_prices)
        self._index_map = {}
        self._index_close_high = 3450
        self._index_close_low = 3380
        self._index_alerted_downtrend = False
        self._index_alerted_ma20 = 0
        self._index_last_fluctuation_price = 0
        self._index_tech_state = {"macd_cross": None, "rsi6_zone": "normal",
                                  "rsi12_zone": "normal", "kdj_cross": None,
                                  "kdj_j_zone": "normal", "divergence": None}
        self._last_index_quote = {"price": 3420, "pre_close": 3400, "change_pct": 0.005}
        self._volume_alerted_divergence = False
        self._closing_decision_done = False
        self._max_drawdown_alerted = False
        self._data_ready = True
        self._data_ready_at = 0
        self._data_missing_rounds = 0
        # 板块趋势
        self._sector_trend_history = defaultdict(list)
        self._sector_trend_continuity = defaultdict(int)
        self._sector_trend_last_dir = {}
        self._sector_trend_start = {}
        self._concept_trend_history = defaultdict(list)
        self._concept_trend_continuity = defaultdict(int)
        self._concept_trend_last_dir = {}
        self._concept_trend_start = {}
        # 告警/信号
        self._triggered_ids = set()
        self._alerted_sl_tp = set()
        self._alert_fingerprints = {}
        self._signal_alert_state = {}
        self._review_alert_state = {}
        self._prev_snapshot = {}
        self._recently_sold = {}
        # 持仓
        self._pos_meta = {}
        self._bought_watch = {}
        self._recent_prices = {}
        self._snapshot_price_history = {}
        self._sl_reminders = {}
        # 缓存
        self._cached_db_watch_codes = set()
        self._watch_codes_stale = True
        # 回踩
        self._pullback_scan_count = 0
        self._pullback_alerted_today = set()
        # AI
        self._ai_queue = None
        self._pending_chase = {}
        self._pending_index_ai = {}
        self._morning_sector_bias = {}
        self._push_cooldown = {}
        self._health_alert_seen = {}
        self._scenario_engine = None
        self._scenario_prev_velocity = 0
        self._scenario_recent_lows = []
        self._scenario_recent_highs = []
        self._scenario_prev_breadth = 0.5
        self._scenario_prev_outlook = None
        self._review_monitor = None
        self._sector_monitor = None
        self._abnormal_detector = None
        self._receiver = None
        self._executor = None
        self._collector_client = None
        self._last_db_ts = 0
        self._resonance_analyzer = None
        self._breadth_block_alerted = False
        self._last_resonance_push_scan = -100
        self._last_resonance_index_dir = ""
        self._last_resonance_labels = {}
        self._prev_ind_amounts = {}
        self._prev_con_amounts = {}
        self._scenario_probs = {}
        self._scenario_scan_count = 0
        self._scenario_last_alert_scan = 0

    def _alert(self, msg): pass
    def _alert_private(self, msg): pass
    def _resolve_name(self, code): return "测试股票"
    def _get_market_adjustment(self, code, trend): return {}
    def _init_private_telegram(self): pass
    def _compute_breadth(self): return {"up": 500, "down": 300, "flat": 100}
    def _get_index_baseline(self): return (3390, 3395, 3400)
    def _get_index_ma60(self): return 3350
    def _calc_intraday_ema(self, prices, period): return sum(prices[-period:]) / period if len(prices) >= period else sum(prices) / len(prices)
    def _compute_key_levels(self): return ([3380.0], [3450.0])
    def _detect_higher_highs(self, px): return False
    def _get_index_quote(self): return {"price": 3420, "pre_close": 3400}
    def _check_multi_day_downtrend(self): return False
    def _init_scenario_state(self):
        from trade.scenario.scenario_engine import ScenarioEngine
        self._scenario_engine = ScenarioEngine()
        self._scenario_probs = self._scenario_engine.probs
        self._scenario_scan_count = self._scenario_engine.scan_count
    def _should_throttle(self, code, price): return False
    def _is_limit_up(self, code, price): return False
    def _is_limit_down(self, code, price): return False
    def _invalidate_watch_codes_cache(self): pass
    def _log_buy_filter(self, **kw): pass
    def _log_buy_trigger(self, **kw): pass
    def _log_position_size(self, **kw): pass
    def _log_stop_trigger(self, **kw): pass
    def _log_tp_trigger(self, **kw): pass
    def _submit_scenario_ai(self, **kw): pass
    def _get_watch_codes(self): return set()


@pytest.fixture
def w():
    return TestWatcher()


class TestMarketStateMethods:
    """market_state.py 委托方法"""

    def test_classify_market_pattern(self, w):
        result = w._classify_market_pattern()
        assert isinstance(result, str)
        assert result in ("normal", "uptrend", "one_sided", "panic", "v_reversal",
                          "dead_cat", "melt_up", "inverted_v", "w_bottom", "m_top",
                          "gap_up_fade", "gap_down_recover", "late_rally", "late_dump",
                          "fishing_line", "wide_choppy")

    def test_assess_regime(self, w):
        result = w._assess_regime("normal", 3420, 3400, 0.005)
        assert isinstance(result, MarketRegime)
        assert result.pattern == "normal"

    def test_check_market_state(self, w):
        result = w._check_market_state({"000001": 10.0})
        assert isinstance(result, MarketRegime)

    def test_check_index_technicals(self, w):
        # 不应抛异常
        try:
            w._check_index_technicals()
        except Exception as e:
            pytest.fail(f"_check_index_technicals raised: {e}")


class TestBuyDecisionMethods:
    """buy_decision.py 委托方法"""

    def test_evaluate_buy_decision(self, w):
        ok, reason, mul = w._evaluate_buy_decision("000001", 10.0, 9.5, 10.5)
        assert isinstance(ok, bool)
        assert isinstance(reason, str)
        assert isinstance(mul, float)

    def test_evaluate_below_zone(self, w):
        action, reason, mul = w._evaluate_below_zone("000001", 9.0, 9.5, 10.5)
        assert isinstance(action, str)
        assert action in ("opportunity", "watching", "abandon")

    def test_calculate_position_size(self, w):
        amount, reason = w._calculate_position_size("000001", 10.0, 9.5, 10.5, "normal", "走强")
        assert isinstance(amount, int)
        assert amount >= 0


class TestPositionRiskMethods:
    """position_risk.py 委托方法"""

    def test_find_resistance_ceiling(self, w):
        result = w._find_resistance_ceiling("000001", 10.0)
        assert result is None or isinstance(result, float)

    def test_find_support_floor(self, w):
        result = w._find_support_floor("000001", 10.0)
        assert result is None or isinstance(result, float)


class TestSectorContextMethods:
    """sector_context.py 委托方法"""

    def test_get_sector_trend(self, w):
        w._ensure_industry_cache()
        result = w._get_sector_trend("000001")
        assert isinstance(result, str)

    def test_get_concept_trend_score(self, w):
        w._ensure_concept_cache()
        score, reason = w._get_concept_trend_score("000001")
        assert isinstance(score, int)
        assert -3 <= score <= 3


class TestScenarioMethods:
    """scenario 模块方法"""

    def test_scenario_engine(self, w):
        w._init_scenario_state()
        assert w._scenario_engine is not None
        micro = w._detect_micro_signals()
        assert isinstance(micro, MicroSignals)
        outlook = w._update_scenario_engine(micro)
        assert isinstance(outlook, MarketOutlook)
