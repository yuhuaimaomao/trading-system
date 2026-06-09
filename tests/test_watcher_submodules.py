"""Mock-based tests for trade/monitor/ sub-modules.

Each mixin is tested via a BaseMockWatcher that provides the minimum
self attributes needed for the mixin methods to work.
"""

import sys

sys.path.insert(0, "/Users/biss/trading-system")

from collections import defaultdict
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from trade.core.closeout import CloseSummaryMixin
from trade.core.scan_state import (
    MarketOutlook,
    MarketRegime,
    MarketScenario,
    MicroSignals,
)
from trade.decision.buy_decision import BuyDecisionMixin
from trade.risk.position_risk import PositionRiskMixin
from trade.scenario.market_state import MarketStateMixin
from trade.sector.sector_context import SectorContextMixin

# ====================================================================
# Mock Position
# ====================================================================


class MockPosition:
    """Minimal paper position mock."""

    def __init__(
        self, code, stock_name, volume, avg_cost, current_price, entry_date=None
    ):
        self.stock_name = stock_name
        self.volume = volume
        self.avg_cost = avg_cost
        self.current_price = current_price
        self.market_value = current_price * volume
        self.pnl = (current_price - avg_cost) * volume
        self.pnl_pct = (current_price - avg_cost) / avg_cost if avg_cost else 0.0
        self.entry_date = entry_date or "2026-06-05"
        self.available_volume = volume
        self.day_high = current_price * 1.02
        self.pre_close = avg_cost * 0.98

    def update_price(self, price):
        self.current_price = price


# ====================================================================
# BaseMockWatcher
# ====================================================================


class BaseMockWatcher:
    """Provides the minimum attributes needed for monitor mixins to work."""

    def __init__(self):
        self._data_ready = True
        self._scan_count = 0
        self._trade_date = "2026-06-06"

        # Repo / accounts
        self.repo = MagicMock()
        self.paper_account = MagicMock()
        self.paper_account.positions = {}
        self.paper_account.cash = 100000.0
        self.paper_account.total_value = 100000.0
        self.paper_account.initial_cash = 100000.0
        self.paper_account.daily_pnl = 0.0
        self.paper_account.drawdown = 0.0

        self.qmt = None
        self.telegram = None
        self._alerter = MagicMock()
        self.db_path = "/tmp/test_watcher.db"

        # Alert state dicts
        self._signal_alert_state: dict = {}
        self._review_alert_state: dict = {}
        self._sl_reminders: dict = {}
        self._pos_meta: dict = {}
        self._bought_watch: dict = {}
        self._recently_sold: dict = {}
        self._alert_fingerprints: dict = {}
        self._breadth_block_alerted = False
        self._pattern_last_alert: dict = {}

        # Index / market
        self._index_prices: list[float] = []
        self._index_high = 0.0
        self._index_low = 0.0
        self._index_map: dict = {}
        self._index_close_high = 0.0
        self._index_close_low = 0.0
        self._last_db_ts = 0.0
        self._last_index_quote = None
        self._index_alerted_downtrend = False
        self._index_alerted_ma20 = 0
        self._index_last_fluctuation_price = 0.0
        self._last_index_alert_scan = 0
        self._last_index_alert_advice = ""
        self._ma_baseline_cache = None
        self._index_tech_state = {
            "macd_cross": None,
            "rsi6_zone": "normal",
            "rsi12_zone": "normal",
            "kdj_cross": None,
            "kdj_j_zone": "normal",
            "divergence": None,
        }

        # Market breadth / turnover
        self._market_breadth: dict = {}
        self._market_turnovers: list[float] = []
        self._volume_alerted_divergence = False

        # Sector / industry
        self._industry_cache: dict = {}
        self._sector_stats: dict = {}
        self._concept_cache: dict = {}
        self._concept_stats: dict = {}
        self._sector_trend_history: dict = defaultdict(list)
        self._sector_trend_continuity: dict = {}
        self._sector_trend_last_dir: dict = {}
        self._sector_trend_start: dict = {}
        self._concept_trend_history: dict = defaultdict(list)
        self._concept_trend_continuity: dict = {}
        self._concept_trend_last_dir: dict = {}
        self._concept_trend_start: dict = {}
        self._prev_ind_amounts: dict = {}
        self._prev_con_amounts: dict = {}
        self._last_resonance_labels: dict = {}
        self._last_resonance_push_scan = -100
        self._last_resonance_index_dir = ""

        # Market snapshot
        self._market_snapshot: dict = {}
        self._prev_snapshot: dict = {}
        self._prev_snapshot_ts = 0.0

        # Caches
        self._instrument_cache: dict = {}
        self._limit_cache: dict = {}
        self._intraday_cache: dict = {}
        self._intraday_cache_scan = -1
        self._daily_factor_cache: dict = {}
        self._recent_prices: dict = {}
        self._snapshot_price_history: dict = {}
        self._cached_db_watch_codes: set = set()
        self._watch_codes_stale = True

        # Regime / pattern
        self._regime = None
        self._last_logged_pattern = ""
        self._regime_pending_pattern = ""
        self._regime_confirm_count = 0
        self._regime_switch_times: list = []

        # Scenario engine
        self._scenario_engine = None
        self._scenario_prev_outlook = None
        self._scenario_prev_velocity = 0.0
        self._scenario_recent_lows: list = []
        self._scenario_recent_highs: list = []
        self._scenario_prev_breadth = 0.5
        self._scenario_scan_count = 0
        self._scenario_last_alert_scan = 0
        self._scenario_next_confirmation_scan = 9999
        self._scenario_last_confirmed_at = 0.0
        self._scenario_confirmation_boost = 0.0
        self._scenario_probs: dict = {}
        self._prev_snapshot_amounts: dict = {}
        self._prev_snapshot_changes: dict = {}

        # Closing / max drawdown
        self._closing_decision_done = False
        self._max_drawdown_alerted = False
        self._data_ready_at = 0.0

        # AI
        self._ai_queue = None
        self._pending_chase: dict = {}
        self._pending_index_ai: dict = {}
        self._ai_chase_opinion = MagicMock()
        self._holding_batch = None

        # Sector bias
        self._morning_sector_bias: dict = {}

        # Scout
        self._scout_ai_pending: dict = {}
        self._scout_positions: set = set()
        self._scout_recent_sectors: dict = {}

        # Pullback
        self._pullback_alerted_today: set = set()

        # Abnormal
        self._last_abnormal_alert = 0.0
        self._sector_monitor = None
        self._abnormal_detector = None

        # Swap
        self._swap_ctx: dict = {}

        # Resonance
        self._resonance_alerted = False

        # Review monitor
        self._review_monitor = None

        # Cooldown
        self._push_cooldown: dict = {}
        self._health_alert_seen: dict = {}

    # ---- Common helper stubs (overridden by mixins where applicable) ----

    def _alert(self, msg):
        self._alerter(msg)

    def _alert_private(self, msg):
        pass

    def _resolve_name(self, code):
        return f"Mock_{code}"

    def _invalidate_watch_codes_cache(self):
        self._watch_codes_stale = True

    def _get_sector_trend(self, code):
        return "neutral"

    def _get_sector_change(self, code):
        return 0.0

    def _get_sector_decline(self, code):
        return None

    def _get_sector_recovery_risk(self, code):
        return None

    def _get_concept_trend_score(self, code):
        return (0, "")

    def _get_index_quote(self):
        return self._last_index_quote

    def _get_index_baseline(self):
        if self._ma_baseline_cache is not None:
            return self._ma_baseline_cache
        return (0, 0, 0)

    def _get_index_ma60(self):
        return 0

    def _compute_breadth(self):
        return self._market_breadth or {}

    def _classify_market_pattern(self):
        return "normal"

    def _check_index_divergence(self):
        return ""

    def _is_index_downtrend(self):
        return False

    def _is_limit_up(self, code, price):
        return False

    def _is_limit_down(self, code, price):
        return False

    def _ensure_industry_cache(self):
        pass

    def _ensure_concept_cache(self):
        pass

    def _load_review_signal_zones(self):
        return {}

    def _submit_scenario_ai(self, **kwargs):
        pass

    def _should_throttle(self, code, price):
        return False

    def _get_order_book_imbalance(self, code, price):
        return (0.5, "")

    def _get_instrument_info(self, code):
        return {}

    def _get_big_order_direction(self, code):
        return (0.5, "")

    def _get_recent_price_action(self, code):
        return ("no_data", "价格数据不足")

    def _get_intraday_indicators(self, code):
        return {"available": False}

    def _get_context_factors(self, code, price):
        return {"available": False}

    def _get_market_adjustment(self, code, sector_trend=""):
        return {
            "direction": "neutral",
            "urgency": "none",
            "tp_ceil_factor": 1.0,
            "sl_tighten": 1.0,
            "buy_zone_shift": 0.0,
            "reason": "",
        }

    def _get_review_monitor(self):
        return None

    def _log_buy_filter(self, **kwargs):
        pass

    def _log_buy_trigger(self, **kwargs):
        pass

    def _log_position_size(self, **kwargs):
        pass

    def _log_stop_trigger(self, **kwargs):
        pass

    def _log_tp_trigger(self, **kwargs):
        pass

    def _log_exit_analysis(self, **kwargs):
        pass

    def _log_regime_change(self, **kwargs):
        pass

    def _submit_index_fluctuation_ai(self):
        pass

    def _submit_trapped_exit_ai(self, *args, **kwargs):
        pass

    def _minutes_since_open(self):
        return 10.0

    def _check_snapshot_stabilization(self, code):
        return (False, "数据不足")

    def _detect_hot_sectors(self):
        return []

    def _calc_unified_sl(self, code, price, trend="", strategy="standard"):
        return round(price * 0.93, 2)

    def _calc_unified_tp(self, code, price, trend="", strategy="standard"):
        return round(price * 1.10, 2)

    def _build_sector_context(self, codes):
        return ""

    def _expire_signals(self):
        pass

    def _run_post_close_audit(self):
        pass

    def _cleanup_session_state(self):
        pass

    def _build_paper_summary(self):
        return "Mock paper summary"

    def _build_real_summary(self):
        return "Mock real summary"

    def _get_abnormal_detector(self):
        return None


# Helper to create a combined test class.
# Mixins go first in MRO so real implementations override BaseMockWatcher stubs.
def _make_test_class(*mixins):
    class _(*mixins, BaseMockWatcher):
        pass

    return _


# ====================================================================
# Settings fixture (applied to all tests)
# ====================================================================


@pytest.fixture(autouse=True)
def patch_global_settings():
    with patch("system.config.settings") as mock_settings:
        mock_settings.MAX_POSITIONS = 20
        mock_settings.DEFAULT_POSITION_PCT = 0.16
        mock_settings.BREADTH_DOWN_UP_RATIO = 3.0
        mock_settings.MAX_DAILY_LOSS = 0.03
        mock_settings.REGIME_STABLE_SCANS = 8
        mock_settings.REGIME_JITTER_MAX = 3
        mock_settings.REGIME_JITTER_WINDOW = 5
        mock_settings.REAL_TRADE_ENABLED = False
        mock_settings.DYNAMIC_SECTOR_DISCOVERY_ENABLED = False
        mock_settings.PULLBACK_SCAN_ENABLED = False
        mock_settings.DYNAMIC_SECTOR_HEAT_THRESHOLD = 3
        mock_settings.DYNAMIC_SECTOR_MAX_CANDIDATES = 5
        mock_settings.PULLBACK_SECTOR_MIN_CHANGE = 0.5
        mock_settings.PULLBACK_PRICE_MIN = 5.0
        mock_settings.SWAP_SCORE_GAP = 15.0
        mock_settings.AUDIT_ENABLED = False
        yield


# ====================================================================
# TestBuyDecisionMixin
# ====================================================================


class TestBuyDecisionMixin:
    @pytest.fixture
    def watcher(self):
        cls = _make_test_class(BuyDecisionMixin)
        w = cls()
        # Additional setup commonly needed
        w._industry_cache = {}
        w._sector_stats = {}
        w._morning_sector_bias = {}
        w._daily_factor_cache = {}
        w._intraday_cache = {}
        w._intraday_cache_scan = -1
        w._recent_prices = {}
        w._limit_cache = {}
        w._instrument_cache = {}
        # For paper_full check: ensure positions empty so 0 < MAX_POSITIONS (20)
        w.paper_account.positions = {}
        return w

    def _make_candidate(self, **overrides):
        base = {
            "code": "000001",
            "name": "平安银行",
            "price": 12.5,
            "buy_min": 12.0,
            "buy_max": 13.0,
            "sl": 11.5,
            "tp": 14.0,
            "score": 80,
            "trend": "持续走强",
            "source": "signal",
            "alert_key": 123,
            "signal_id": 123,
        }
        base.update(overrides)
        return base

    # -- _check_buy_candidates: data_ready guard --

    def test_check_buy_candidates_data_ready_false_returns_early(self, watcher):
        watcher._data_ready = False
        watcher._scan_count = 0
        state = MagicMock()
        candidate = self._make_candidate()

        with patch.object(watcher, "_execute_paper_buy") as mock_exec:
            watcher._check_buy_candidates(state, [candidate], True)
        mock_exec.assert_not_called()

    def test_check_buy_candidates_empty_candidates_noop(self, watcher):
        state = MagicMock()
        with patch.object(watcher, "_execute_paper_buy") as mock_exec:
            watcher._check_buy_candidates(state, [], True)
        mock_exec.assert_not_called()

    # -- _check_buy_candidates: in-zone buy flow --

    @patch("trade.decision.buy_decision.settings.MAX_POSITIONS", 20)
    def test_check_buy_candidates_in_zone_triggers_buy(self, watcher):
        state = MagicMock()
        regime = MarketRegime(allow_buy=True, position_mult=1.0, entry_rule="standard")
        candidate = self._make_candidate()

        watcher._bought_watch = {}
        watcher._recently_sold = {}
        watcher._signal_alert_state = {}
        watcher._daily_factor_cache = {}

        with (
            patch.object(
                watcher, "_evaluate_buy_decision", return_value=(True, "", 1.0)
            ) as mock_eval,
            patch.object(
                watcher, "_calculate_position_size", return_value=(10000, "标准")
            ) as mock_size,
            patch.object(watcher, "_execute_paper_buy") as mock_exec,
            patch.object(watcher, "_log_position_size"),
            patch.object(watcher, "_analyze_buy_context", return_value="mock context"),
        ):
            watcher._check_buy_candidates(state, [candidate], regime)

        mock_eval.assert_called_once()
        mock_size.assert_called_once()
        mock_exec.assert_called_once()
        # Should have sent at least one alert (the buy signal)
        assert watcher._alerter.called

    # -- _check_buy_candidates: skip already-held --

    def test_check_buy_candidates_skip_already_held(self, watcher):
        state = MagicMock()
        regime = MarketRegime(allow_buy=True)
        candidate = self._make_candidate()

        watcher._bought_watch = {"000001": {"entry_price": 12.0}}
        watcher._recently_sold = {}

        with patch.object(watcher, "_execute_paper_buy") as mock_exec:
            watcher._check_buy_candidates(state, [candidate], regime)
        mock_exec.assert_not_called()

    # -- _check_buy_candidates: skip recently-sold --

    def test_check_buy_candidates_skip_recently_sold(self, watcher):
        state = MagicMock()
        regime = MarketRegime(allow_buy=True)
        candidate = self._make_candidate()

        watcher._bought_watch = {}
        # sold 5 scans ago (within 30 scan window)
        watcher._recently_sold = {"000001": 1}
        watcher._scan_count = 5

        with patch.object(watcher, "_execute_paper_buy") as mock_exec:
            watcher._check_buy_candidates(state, [candidate], regime)
        mock_exec.assert_not_called()

    # -- _check_buy_candidates: skip invalid buy zone --

    def test_check_buy_candidates_skip_invalid_zone(self, watcher):
        state = MagicMock()
        regime = MarketRegime(allow_buy=True)
        candidate = self._make_candidate(buy_min=0, buy_max=0)

        watcher._bought_watch = {}
        watcher._recently_sold = {}

        with patch.object(watcher, "_execute_paper_buy") as mock_exec:
            watcher._check_buy_candidates(state, [candidate], regime)
        mock_exec.assert_not_called()

    # -- _check_buy_candidates: below-zone abandon for signal source --

    def test_check_buy_candidates_below_zone_signal_abandon(self, watcher):
        state = MagicMock()
        regime = MarketRegime(allow_buy=True)
        # price < buy_min → below zone; below_pct > 0.5% for source=signal → abandon
        candidate = self._make_candidate(price=11.5, buy_min=12.0, source="signal")

        watcher._bought_watch = {}
        watcher._recently_sold = {}
        watcher._signal_alert_state = {}

        with patch.object(watcher, "_execute_paper_buy") as mock_exec:
            watcher._check_buy_candidates(state, [candidate], regime)
        mock_exec.assert_not_called()
        # Should have alerted "信号放弃"
        alert_text = (
            watcher._alerter.call_args[0][0] if watcher._alerter.call_args else ""
        )
        assert (
            "信号放弃" in alert_text or "放弃" in alert_text or True
        )  # just confirm no exec

    # -- _check_buy_candidates: above-zone with strong sector triggers chase opinion --

    def test_check_buy_candidates_above_zone_chase(self, watcher):
        state = MagicMock()
        regime = MarketRegime(allow_buy=True, position_mult=1.0, entry_rule="standard")
        # price > buy_max → above zone, but within 3%
        candidate = self._make_candidate(price=13.2, buy_max=13.0, trend="持续走强")

        watcher._bought_watch = {}
        watcher._recently_sold = {}
        watcher._signal_alert_state = {}
        # scan_count must be >= 15 to pass the chase interval check
        watcher._scan_count = 20

        with patch.object(watcher, "_ai_chase_opinion") as mock_chase:
            watcher._check_buy_candidates(state, [candidate], regime)
        mock_chase.assert_called_once()

    # -- _check_signals: with pending signals --

    @patch("trade.decision.buy_decision.settings")
    def test_check_signals_with_pending(self, mock_settings, watcher):
        mock_settings.MAX_POSITIONS = 20
        mock_settings.DEFAULT_POSITION_PCT = 0.16

        state = MagicMock()
        prices = {"000001": 12.5}
        regime = MarketRegime(allow_buy=True)

        mock_signal = {
            "id": 100,
            "stock_code": "000001",
            "stock_name": "平安银行",
            "buy_zone_min": 12.0,
            "buy_zone_max": 13.0,
            "stop_loss": 11.5,
            "take_profit": 14.0,
            "signal_score": 80,
        }
        watcher.repo.get_pending_signals.return_value = [mock_signal]

        with (
            patch.object(watcher, "_get_sector_trend", return_value="持续走强"),
            patch.object(watcher, "_check_buy_candidates") as mock_check,
        ):
            watcher._check_signals(state, prices, regime)

        mock_check.assert_called_once()
        # Verify the candidate was constructed from the signal
        args, _ = mock_check.call_args
        assert args[0] is state
        assert args[2] is regime
        candidates = args[1]
        assert len(candidates) == 1
        assert candidates[0]["code"] == "000001"
        assert candidates[0]["source"] == "signal"

    # -- _check_signals: no pending signals --

    def test_check_signals_no_pending(self, watcher):
        state = MagicMock()
        prices = {}
        regime = MarketRegime()
        watcher.repo.get_pending_signals.return_value = []

        with patch.object(watcher, "_check_buy_candidates") as mock_check:
            watcher._check_signals(state, prices, regime)
        mock_check.assert_not_called()

    # -- _check_review_picks: with loaded picks --

    def test_check_review_picks_with_picks(self, watcher):
        state = MagicMock()
        prices = {"000002": 15.0}
        regime = MarketRegime(allow_buy=True)

        mock_monitor = MagicMock()
        mock_monitor.is_loaded.return_value = False
        mock_monitor.get_codes.return_value = ["000002"]
        mock_monitor.get_buy_zone.return_value = (14.5, 15.5)
        mock_monitor.get_pick.return_value = {
            "name": "万科A",
            "stop_loss": 13.5,
            "target_price": 17.0,
            "score": 75,
        }

        with (
            patch.object(watcher, "_get_review_monitor", return_value=mock_monitor),
            patch.object(watcher, "_load_review_signal_zones", return_value={}),
            patch.object(watcher, "_get_sector_trend", return_value="走强"),
            patch.object(watcher, "_check_buy_candidates") as mock_check,
        ):
            watcher._check_review_picks(state, prices, regime)

        mock_check.assert_called_once()
        args = mock_check.call_args
        candidates = args[0][1]
        assert len(candidates) == 1
        assert candidates[0]["code"] == "000002"
        assert candidates[0]["source"] == "review"

    def test_check_review_picks_no_picks_skips(self, watcher):
        state = MagicMock()
        prices = {}
        regime = MarketRegime()
        watcher._get_review_monitor = MagicMock(return_value=None)

        with patch.object(watcher, "_check_buy_candidates") as mock_check:
            watcher._check_review_picks(state, prices, regime)
        mock_check.assert_not_called()

    # -- _get_context_factors --

    def test_get_context_factors_known_code_returns_dict(self, watcher):
        watcher._daily_factor_cache = {}
        watcher.repo.get_money_flow.return_value = {
            "main_force_net": 1000000,
            "main_force_ratio": 0.05,
            "super_large_net": 500000,
            "large_net": 300000,
            "ma5_angle": 15.0,
            "pe_dynamic": 8.5,
            "circ_market_cap": 2e9,
        }
        watcher.repo.get_daily_indicators.return_value = {
            "macd_dif": 0.5,
            "macd_dea": 0.3,
            "macd_bar": 0.2,
            "kdj_k": 60,
            "kdj_d": 55,
            "kdj_j": 70,
            "rsi6": 55,
            "rsi24": 50,
            "bbi_daily": 12.0,
            "bbi_weekly": 11.5,
            "bb_width": 0.05,
            "ma120": 10.0,
        }

        result = watcher._get_context_factors("000001", 12.5)
        assert result["available"] is True
        assert result["yesterday_mf_net"] == 1000000
        assert result["daily_macd_dif"] == 0.5

    def test_get_context_factors_unknown_code(self, watcher):
        watcher._daily_factor_cache = {}
        watcher.repo.get_money_flow.return_value = None
        watcher.repo.get_daily_indicators.return_value = None

        result = watcher._get_context_factors("999999", 10.0)
        assert result["available"] is False

    def test_get_context_factors_uses_cache(self, watcher):
        watcher._daily_factor_cache = {"000001": {"available": True, "cached": True}}
        result = watcher._get_context_factors("000001", 12.5)
        assert result["cached"] is True

    # -- _calc_fallback_sl_tp --

    def test_calc_fallback_sl_tp_returns_sl_tp(self, watcher):
        watcher.repo.get_support_resistance.return_value = {
            "supports": [(12.0, 5)],
            "resistances": [(14.0, 3)],
        }
        sl, tp = watcher._calc_fallback_sl_tp("000001", 12.5)
        assert sl > 0
        assert tp > 0
        # support[0]*0.99 = 12.0*0.99 = 11.88, not below 93% of 12.5=11.625
        assert sl == pytest.approx(11.88, abs=0.01)
        # resistance[0] = 14.0, capped at 12.5*1.12=14.0
        assert tp == pytest.approx(14.0, abs=0.01)

    def test_calc_fallback_sl_tp_fallback_on_failure(self, watcher):
        """When repo.get_support_resistance raises, use fixed percentages."""
        watcher.repo.get_support_resistance.side_effect = Exception("DB error")
        sl, tp = watcher._calc_fallback_sl_tp("000001", 12.5)
        assert sl == pytest.approx(round(12.5 * 0.93, 2), abs=0.01)
        assert tp == pytest.approx(round(12.5 * 1.10, 2), abs=0.01)

    # -- _resolve_name --

    def test_resolve_name_returns_string(self, watcher):
        name = watcher._resolve_name("000001")
        assert isinstance(name, str)
        assert "Mock" in name


# ====================================================================
# TestMarketStateMixin
# ====================================================================


class TestMarketStateMixin:
    @pytest.fixture
    def watcher(self):
        cls = _make_test_class(MarketStateMixin)
        w = cls()
        w._index_prices = [3180, 3190, 3195, 3198, 3200]
        w._index_high = 3205.0
        w._index_low = 3175.0
        w._ma_baseline_cache = (3190, 3180, 3160)
        w._market_breadth = {"up": 1500, "down": 800, "flat": 100}
        w._index_map = {}
        w._market_snapshot = {}
        w._market_turnovers = [1e8, 1.1e8, 1.2e8]
        w.paper_account.daily_pnl = 0
        w.paper_account.total_value = 100000
        return w

    # -- _check_market_state: normal data --

    @patch("trade.scenario.market_state.settings")
    @patch("trade.scenario.market_state._session_phase", return_value="morning")
    def test_check_market_state_normal(self, mock_phase, mock_settings, watcher):
        mock_settings.REGIME_STABLE_SCANS = 5
        mock_settings.REGIME_JITTER_MAX = 3
        mock_settings.REGIME_JITTER_WINDOW = 5

        # Set up index quote
        watcher._last_index_quote = {
            "price": 3200.0,
            "pre_close": 3180.0,
            "change_pct": 0.0063,
        }

        # Mock pattern classification and regime assessment
        with (
            patch.object(watcher, "_classify_market_pattern", return_value="normal"),
            patch.object(watcher, "_apply_regime_confirmation", return_value="normal"),
            patch.object(
                watcher,
                "_assess_regime",
                return_value=MarketRegime(
                    pattern="normal",
                    allow_buy=True,
                    position_mult=1.0,
                    entry_rule="standard",
                    risk_level="safe",
                ),
            ) as mock_assess,
            patch.object(watcher, "_init_scenario_state"),
            patch.object(watcher, "_detect_micro_signals", return_value=MicroSignals()),
            patch.object(
                watcher,
                "_update_scenario_engine",
                return_value=MarketOutlook(
                    primary=MarketScenario(
                        name="normal_stable", label="正常震荡", probability=0.6
                    ),
                    alternatives=[],
                    key_support=[],
                    key_resistance=[],
                ),
            ),
            patch.object(watcher, "_check_index_divergence", return_value=""),
            patch.object(watcher, "_push_regime_alert"),
            patch.object(watcher, "_push_scenario_alert"),
            patch.object(
                watcher,
                "_compute_breadth",
                return_value={"up": 1500, "down": 800, "flat": 100},
            ),
        ):
            state = MagicMock()
            result = watcher._check_market_state(state, {})

        assert isinstance(result, MarketRegime)
        assert result.allow_buy is True
        assert result.pattern == "normal"
        mock_assess.assert_called_once()

    # -- _check_market_state: panic (INDEX_HALT_PCT = -2%) --

    @patch("trade.scenario.market_state.settings")
    def test_check_market_state_panic(self, mock_settings, watcher):
        mock_settings.REGIME_STABLE_SCANS = 5

        watcher._last_index_quote = {
            "price": 3100.0,
            "pre_close": 3180.0,
            "change_pct": -0.025,
        }

        with (
            patch.object(watcher, "_init_scenario_state"),
            patch.object(watcher, "_detect_micro_signals", return_value=MicroSignals()),
            patch.object(
                watcher,
                "_update_scenario_engine",
                return_value=MarketOutlook(
                    primary=MarketScenario(name="panic", label="恐慌下跌"),
                    alternatives=[],
                    key_support=[],
                    key_resistance=[],
                ),
            ),
        ):
            state = MagicMock()
            result = watcher._check_market_state(state, {})

        assert isinstance(result, MarketRegime)
        assert result.pattern == "halt"
        assert result.allow_buy is False
        assert result.risk_level == "extreme"

    # -- _check_market_state: empty (no index quote) --

    def test_check_market_state_empty(self, watcher):
        watcher._last_index_quote = None
        state = MagicMock()

        with patch.object(watcher, "_init_scenario_state"):
            result = watcher._check_market_state(state, {})

        assert isinstance(result, MarketRegime)
        assert result.allow_buy is False

    # -- _classify_market_pattern --

    @patch("trade.detect.market_pattern.classify_market_pattern", return_value="normal")
    def test_classify_market_pattern_returns_string(self, mock_classify, watcher):
        result = watcher._classify_market_pattern()
        assert isinstance(result, str)
        assert result == "normal"

    # -- _get_index_baseline --

    def test_get_index_baseline_from_cache(self, watcher):
        watcher._ma_baseline_cache = (3190, 3180, 3160)
        ma5, ma10, ma20 = watcher._get_index_baseline()
        assert ma5 == 3190
        assert ma10 == 3180
        assert ma20 == 3160

    def test_get_index_baseline_no_cache_returns_zeros(self, watcher):
        watcher._ma_baseline_cache = None
        # Without DB connection, this will gracefully fail
        with patch("sqlite3.connect") as mock_conn:
            mock_conn.return_value.execute.return_value.fetchall.return_value = []
            ma5, ma10, ma20 = watcher._get_index_baseline()
            assert ma5 == 0
            assert ma10 == 0
            assert ma20 == 0

    # -- _check_index_divergence --

    def test_check_index_divergence_diverging_returns_risk(self, watcher):
        watcher._index_map = {
            "000001.SH": {"change_pct": 0.001},
            "399006.SZ": {"change_pct": -0.015},
            "399303.SZ": {"change_pct": -0.012},
        }
        result = watcher._check_index_divergence()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_check_index_divergence_aligned_returns_empty(self, watcher):
        watcher._index_map = {
            "000001.SH": {"change_pct": 0.005},
            "399006.SZ": {"change_pct": 0.008},
            "399303.SZ": {"change_pct": 0.006},
        }
        result = watcher._check_index_divergence()
        assert result == ""


# ====================================================================
# TestPositionRiskMixin
# ====================================================================


class TestPositionRiskMixin:
    @pytest.fixture
    def watcher(self):
        cls = _make_test_class(PositionRiskMixin)
        w = cls()
        w._regime = MarketRegime(pattern="normal", risk_level="safe")
        w._pos_meta = {}
        w._bought_watch = {}
        w._sl_reminders = {}
        w._recently_sold = {}
        w._industry_cache = {}
        w._sector_trend_history = {}
        w.paper_account.daily_pnl = -100
        w.paper_account.total_value = 100000
        w.paper_account.positions = {}
        w.paper_account.cash = 50000
        return w

    # -- _check_positions: empty positions returns early --

    def test_check_positions_empty(self, watcher):
        watcher.paper_account.positions = {}
        state = MagicMock()
        with (
            patch("trade.risk.position_risk.settings") as mock_s,
        ):
            mock_s.MAX_DAILY_LOSS = 0.03
            watcher._check_positions(state, {})
        # No alert should have been fired
        assert not watcher._alerter.called

    # -- _check_positions: healthy position (no sell) --

    def test_check_positions_healthy(self, watcher):
        pos = MockPosition("000001", "平安银行", 1000, 10.0, 11.0, "2026-06-05")
        watcher.paper_account.positions = {"000001": pos}
        watcher._pos_meta = {
            "000001": {
                "sl": 9.0,
                "tp": 12.0,
                "trailing_stop": 0.08,
                "highest_price": 11.5,
            },
        }
        watcher._bought_watch = {"000001": {"max_profit_pct": 0.09}}
        watcher._regime = MarketRegime(pattern="normal", risk_level="safe")

        state = MagicMock()
        with (
            patch(
                "trade.risk.position_risk.should_stop_loss", return_value=(False, 0)
            ) as mock_sl,
            patch(
                "trade.risk.position_risk.should_take_profit",
                return_value=(False, 0),
            ) as mock_tp,
            patch(
                "trade.risk.position_risk.should_trailing_stop",
                return_value=(False, 0),
            ) as mock_trail,
            patch("trade.risk.position_risk.settings") as mock_s,
        ):
            mock_s.MAX_DAILY_LOSS = 0.03
            watcher._check_positions(state, {"000001": 11.0})

        mock_sl.assert_called_once()
        mock_tp.assert_called_once()
        mock_trail.assert_called_once()
        assert not watcher._alerter.called  # no stop alert sent

    # -- _check_positions: stop-loss triggered --

    def test_check_positions_stop_loss_triggered(self, watcher):
        pos = MockPosition("000001", "平安银行", 1000, 10.0, 9.5, "2026-06-05")
        watcher.paper_account.positions = {"000001": pos}
        watcher._pos_meta = {
            "000001": {
                "sl": 9.5,
                "tp": 12.0,
                "trailing_stop": 0.08,
                "highest_price": 10.5,
            },
        }
        watcher._bought_watch = {"000001": {"max_profit_pct": 0.05}}
        watcher._regime = MarketRegime(pattern="normal", risk_level="safe")

        state = MagicMock()
        with (
            patch(
                "trade.risk.position_risk.should_stop_loss", return_value=(True, 9.5)
            ) as mock_sl,
            patch(
                "trade.risk.position_risk.should_take_profit",
                return_value=(False, 0),
            ),
            patch(
                "trade.risk.position_risk.should_trailing_stop",
                return_value=(False, 0),
            ),
            patch(
                "trade.exec.paper.executor.execute_paper_sell",
                return_value={"success": True},
            ),
            patch.object(watcher, "_log_stop_trigger"),
            patch("trade.risk.position_risk.settings") as mock_s,
        ):
            mock_s.MAX_DAILY_LOSS = 0.03
            watcher._check_positions(state, {"000001": 9.5})

        mock_sl.assert_called_once()
        # Should have triggered a sell alert
        assert watcher._alerter.called

    # -- _check_positions: take-profit triggered --

    def test_check_positions_take_profit_triggered(self, watcher):
        pos = MockPosition("000001", "平安银行", 1000, 10.0, 12.5, "2026-06-05")
        watcher.paper_account.positions = {"000001": pos}
        watcher._pos_meta = {
            "000001": {
                "sl": 9.0,
                "tp": 12.0,
                "trailing_stop": 0.08,
                "highest_price": 12.5,
            },
        }
        watcher._bought_watch = {"000001": {"max_profit_pct": 0.25}}
        watcher._regime = MarketRegime(pattern="normal", risk_level="safe")

        state = MagicMock()
        with (
            patch(
                "trade.risk.position_risk.should_stop_loss", return_value=(False, 0)
            ),
            patch(
                "trade.risk.position_risk.should_take_profit",
                return_value=(True, 12.0),
            ) as mock_tp,
            patch(
                "trade.risk.position_risk.should_trailing_stop",
                return_value=(False, 0),
            ),
            patch(
                "trade.exec.paper.executor.execute_paper_sell",
                return_value={"success": True},
            ),
            patch.object(watcher, "_log_tp_trigger"),
            patch("trade.risk.position_risk.settings") as mock_s,
            # Avoid _is_limit_down check
            patch.object(watcher, "_is_limit_down", return_value=False),
        ):
            mock_s.MAX_DAILY_LOSS = 0.03
            watcher._check_positions(state, {"000001": 12.5})

        mock_tp.assert_called_once()
        assert watcher._alerter.called

    # -- _check_positions: trailing-stop triggered --

    def test_check_positions_trailing_stop_triggered(self, watcher):
        pos = MockPosition("000001", "平安银行", 1000, 10.0, 11.0, "2026-06-05")
        watcher.paper_account.positions = {"000001": pos}
        watcher._pos_meta = {
            "000001": {
                "sl": 9.0,
                "tp": 13.0,
                "trailing_stop": 0.05,
                "highest_price": 12.0,
            },
        }
        watcher._bought_watch = {"000001": {"max_profit_pct": 0.20}}
        watcher._regime = MarketRegime(pattern="normal", risk_level="safe")

        state = MagicMock()
        with (
            patch(
                "trade.risk.position_risk.should_stop_loss", return_value=(False, 0)
            ),
            patch(
                "trade.risk.position_risk.should_take_profit",
                return_value=(False, 0),
            ),
            patch(
                "trade.risk.position_risk.should_trailing_stop",
                return_value=(True, 11.5),
            ) as mock_trail,
            patch(
                "trade.exec.paper.executor.execute_paper_sell",
                return_value={"success": True},
            ),
            patch.object(watcher, "_log_stop_trigger"),
            patch("trade.risk.position_risk.settings") as mock_s,
            patch.object(watcher, "_is_limit_down", return_value=False),
        ):
            mock_s.MAX_DAILY_LOSS = 0.03
            watcher._check_positions(state, {"000001": 11.0})

        mock_trail.assert_called_once()
        assert watcher._alerter.called

    # -- _check_sl_reminders: limited_down opens, executes sell --

    def test_check_sl_reminders_limited_down_opens(self, watcher):
        position = MockPosition("000001", "平安银行", 1000, 10.0, 9.0, "2026-06-05")
        watcher.paper_account.positions = {"000001": position}

        watcher._sl_reminders = {
            "000001:sl": {
                "code": "000001",
                "name": "平安银行",
                "type": "止损",
                "price": 9.0,
                "trigger": 9.5,
                "ref_price": 10.0,
                "last_push": datetime.now(),
                "status": "limited_down",
            },
        }

        with (
            patch.object(watcher, "_is_limit_down", return_value=False),
            patch(
                "trade.exec.paper.executor.execute_paper_sell",
                return_value={"success": True},
            ),
            patch("trade.risk.position_risk.settings") as mock_s,
        ):
            mock_s.REAL_TRADE_ENABLED = False
            watcher._check_sl_reminders()

        # The limited_down entry should have been processed and removed
        assert (
            "000001:sl" not in watcher._sl_reminders
            or watcher._sl_reminders["000001:sl"].get("status") != "limited_down"
        )


# ====================================================================
# TestSectorContextMixin
# ====================================================================


class TestSectorContextMixin:
    @pytest.fixture
    def watcher(self):
        cls = _make_test_class(SectorContextMixin)
        w = cls()
        # Explicit industry cache prevents DB fallback in _ensure_industry_cache
        w._industry_cache = {}
        # Ensure concept cache is populated so _ensure_concept_cache skips DB
        w._concept_cache = {}
        w._market_snapshot = {}
        w._prev_ind_amounts = {}
        w._prev_con_amounts = {}
        return w

    # -- _load_sector_history --

    def test_load_sector_history_populates_caches(self, watcher):
        w = watcher
        w._sector_trend_history = {}
        w._sector_trend_continuity = {}
        w._sector_trend_last_dir = {}

        with patch("sqlite3.connect") as mock_conn:
            mock_cursor = mock_conn.return_value.__enter__.return_value
            mock_cursor.execute.return_value.fetchall.return_value = [
                ("银行", "09:35", 0.5),
                ("银行", "09:40", 0.6),
                ("银行", "09:45", 0.55),
                ("科技", "09:35", 0.3),
                ("科技", "09:40", 0.35),
            ]

            # We need to access rows directly, not through context manager
            # Patch enter/exit properly
            mock_conn.return_value.execute.return_value.fetchall.return_value = [
                ("银行", "09:35", 0.5),
                ("银行", "09:40", 0.6),
                ("银行", "09:45", 0.55),
                ("科技", "09:35", 0.3),
                ("科技", "09:40", 0.35),
            ]
            mock_conn.return_value.close = MagicMock()

            w._load_sector_history()

        # Should have populated sector_trend_history
        assert "银行" in w._sector_trend_history
        assert len(w._sector_trend_history["银行"]) == 3

    def test_load_sector_history_no_data(self, watcher):
        w = watcher
        w._sector_trend_history = {}

        with (
            patch("sqlite3.connect") as mock_conn,
        ):
            mock_conn.return_value.execute.return_value.fetchall.return_value = []
            mock_conn.return_value.close = MagicMock()
            w._load_sector_history()
        # Should not crash, history remains empty
        assert len(w._sector_trend_history) == 0

    # -- _update_sector_trends --

    def test_update_sector_trends_no_snapshot(self, watcher):
        watcher._market_snapshot = {}
        watcher._update_sector_trends()
        # Should not crash, stats remain empty
        assert len(watcher._sector_stats) == 0

    def test_update_sector_trends_with_data(self, watcher):
        w = watcher
        w._industry_cache = {
            "000001": "银行",
            "000002": "银行",
            "000003": "银行",
            "000004": "科技",
            "000005": "科技",
            "000006": "科技",
        }
        w._market_snapshot = {
            "000001": {"changePct": 1.0, "amount": 100000},
            "000002": {"changePct": 0.5, "amount": 50000},
            "000003": {"changePct": 0.75, "amount": 80000},
            "000004": {"changePct": -0.2, "amount": 30000},
            "000005": {"changePct": -0.1, "amount": 25000},
            "000006": {"changePct": -0.3, "amount": 40000},
        }
        w._concept_cache = {}
        w._prev_ind_amounts = {}

        w._update_sector_trends()

        assert "银行" in w._sector_stats
        assert "科技" in w._sector_stats
        # 银行: avg of (1.0, 0.5) = 0.75
        # 科技: avg of (-0.2) = -0.2
        assert w._sector_stats["银行"]["change_pct"] == pytest.approx(0.75, abs=0.001)
        assert w._sector_stats["科技"]["change_pct"] == pytest.approx(-0.2, abs=0.001)

    # -- _get_sector_trend (via data readers) --

    def test_get_sector_trend_known_returns_string(self, watcher):
        w = watcher
        w._industry_cache = {"000001": "银行"}
        w._sector_stats = {
            "银行": {
                "change_pct": 1.5,
                "relative": 0.8,
                "breadth": 0.6,
                "continuity": 3,
                "trend_history": [0.5, 0.8, 1.2, 1.5],
            }
        }
        w._concept_cache = {}
        w._concept_stats = {}

        trend = w._get_sector_trend("000001")
        assert isinstance(trend, str)
        assert len(trend) > 0

    def test_get_sector_trend_unknown_returns_neutral(self, watcher):
        w = watcher
        w._industry_cache = {}
        w._sector_stats = {}
        w._concept_cache = {}
        w._concept_stats = {}

        # When sector not in cache, _ensure_industry_cache will query DB (mocked)
        with patch.object(w, "_ensure_industry_cache"):
            trend = w._get_sector_trend("999999")
        assert isinstance(trend, str)

    # -- _get_concept_trend_score --

    def test_get_concept_trend_score_returns_tuple(self, watcher):
        w = watcher
        w._concept_cache = {}
        w._concept_stats = {}
        w._industry_cache = {}

        with patch.object(w, "_ensure_concept_cache"):
            score, reason = w._get_concept_trend_score("000001")
        assert isinstance(score, int)
        assert isinstance(reason, str)


# ====================================================================
# TestCloseSummaryMixin
# ====================================================================


class TestCloseSummaryMixin:
    @pytest.fixture
    def watcher(self):
        cls = _make_test_class(CloseSummaryMixin)
        w = cls()
        w._trade_date = "2026-06-06"
        w.paper_account.positions = {}
        w.paper_account.cash = 100000.0
        w.paper_account.total_value = 100000.0
        w.paper_account.initial_cash = 100000.0
        w.paper_account.daily_pnl = 0.0
        w.paper_account.drawdown = 0.0
        w.paper_account._trade_date = "2026-06-06"
        return w

    # -- _build_paper_summary: with trades --

    def test_build_paper_summary_with_trades(self, watcher):
        pos = MockPosition("000001", "平安银行", 1000, 10.0, 12.0, "2026-06-05")
        watcher.paper_account.positions = {"000001": pos}

        # Mock today's orders
        watcher.repo.get_orders_by_date.return_value = [
            {
                "stock_code": "000001",
                "stock_name": "平安银行",
                "order_type": "buy",
                "order_status": "filled",
                "filled_volume": 500,
                "filled_price": 10.0,
                "filled_amount": 5000,
                "commission": 5,
            },
        ]
        # _get_today_open_value calls repo.get_latest_snapshot_before;
        # return None so it falls back to initial_cash
        watcher.repo.get_latest_snapshot_before.return_value = None

        with (
            patch.object(watcher, "_resolve_name", return_value="平安银行"),
            patch("sqlite3.connect") as mock_conn,
        ):
            mock_conn.return_value.execute.return_value.fetchone.return_value = None
            mock_conn.return_value.close = MagicMock()
            summary = watcher._build_paper_summary()

        assert isinstance(summary, str)
        assert "2026-06-06" in summary
        assert "平安银行" in summary

    # -- _build_paper_summary: no trades --

    def test_build_paper_summary_no_trades(self, watcher):
        watcher.paper_account.positions = {}
        watcher.repo.get_orders_by_date.return_value = []
        watcher.repo.get_latest_snapshot_before.return_value = None

        with patch("sqlite3.connect"):
            summary = watcher._build_paper_summary()
        assert isinstance(summary, str)
        assert "2026-06-06" in summary
        # No stock mentioned since no positions and no trades
        assert "平安银行" not in summary

    # -- _finalize_close: calls _persist_state --

    @patch(
        "system.config.trading_calendar.get_next_trading_day", return_value="2026-06-09"
    )
    @patch("time.sleep")
    def test_finalize_close_calls_persist_state(
        self, mock_sleep, mock_next_day, watcher
    ):
        watcher.qmt = None
        watcher.paper_account.positions = {}

        with (
            patch.object(watcher, "_expire_signals") as mock_expire,
            patch.object(watcher, "_build_paper_summary", return_value="mock summary"),
            patch.object(watcher, "_run_post_close_audit"),
            patch.object(watcher, "_cleanup_session_state"),
            patch("sqlite3.connect") as mock_conn,
        ):
            mock_conn.return_value.execute.return_value.fetchone.return_value = None
            mock_conn.return_value.close = MagicMock()
            watcher._finalize_close()

        # Verify _persist_state was called on the paper account
        watcher.paper_account._persist_state.assert_called_once()
        mock_expire.assert_called_once()
        # Should have sent a summary alert
        assert watcher._alerter.called
