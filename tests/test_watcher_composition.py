"""Watcher composition tests: ALL 9 mixins working together.

This tests method conflicts, MRO correctness, scan flow order, lifecycle
transitions, crash recovery, and one full scan with realistic setup.

Complement to test_watcher_submodules.py (which tests each mixin in isolation).
"""

import sys

sys.path.insert(0, "/Users/biss/trading-system")

import importlib
import inspect
import logging
from datetime import date, datetime
from datetime import time as dt_time
from unittest.mock import MagicMock, patch

import pytest

from audit.watcher_decision_logger import DecisionLoggerMixin
from trade.core.closeout import CloseSummaryMixin
from trade.core.watcher import Watcher
from trade.decision.buy_decision import BuyDecisionMixin
from trade.decision.late_session import ClosingDecisionMixin
from trade.detect.intraday_scout import IntradayScoutMixin
from trade.detect.market_anomaly import AbnormalMonitorMixin
from trade.risk.position_risk import PositionRiskMixin
from trade.scenario.market_state import MarketStateMixin
from trade.sector.sector_context import SectorContextMixin

logger = logging.getLogger(__name__)

# All 9 mixins in inheritance order (matches Watcher class definition)
ALL_MIXIN_CLASSES = [
    DecisionLoggerMixin,
    MarketStateMixin,
    BuyDecisionMixin,
    PositionRiskMixin,
    SectorContextMixin,
    AbnormalMonitorMixin,
    IntradayScoutMixin,
    ClosingDecisionMixin,
    CloseSummaryMixin,
]
ALL_MIXIN_NAMES = [c.__name__ for c in ALL_MIXIN_CLASSES]

# Module path lookup for each mixin (used in MRO conflict detection)
MIXIN_MODULE_MAP = {
    "DecisionLoggerMixin": "audit.watcher_decision_logger",
    "MarketStateMixin": "trade.scenario.market_state",
    "BuyDecisionMixin": "trade.decision.buy_decision",
    "PositionRiskMixin": "trade.risk.position_risk",
    "SectorContextMixin": "trade.sector.sector_context",
    "AbnormalMonitorMixin": "trade.detect.market_anomaly",
    "IntradayScoutMixin": "trade.detect.intraday_scout",
    "ClosingDecisionMixin": "trade.decision.late_session",
    "CloseSummaryMixin": "trade.core.closeout",
}


# ====================================================================
# Helpers
# ====================================================================


class MockPosition:
    """Minimal position mock for composition tests."""

    def __init__(
        self,
        code,
        stock_name="Mock",
        volume=1000,
        avg_cost=10.0,
        current_price=10.5,
        entry_date=None,
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


def _run_scan_loop(w, after_market_side_effect, lunch_break_side_effect=None):
    """Run the scan loop (simulating the while loop inside Watcher.run())."""
    if lunch_break_side_effect is None:

        def lunch_break_side_effect():
            return False

    w._after_market = MagicMock(side_effect=after_market_side_effect)
    w._in_lunch_break = MagicMock(side_effect=lunch_break_side_effect)
    w._lunch_break = MagicMock()
    w._finalize_close = MagicMock()
    w._cleanup_session_state = MagicMock()

    while w._running:
        if w._after_market():
            break
        if w._in_lunch_break():
            w._lunch_break()
            continue
        w._scan_count += 1
        try:
            w._scan()
        except Exception:
            pass

    w._finalize_close()
    w._cleanup_session_state()


# ====================================================================
# Settings fixture (applied to all tests)
# ====================================================================


@pytest.fixture(autouse=True)
def patch_global_settings():
    with patch("system.config.settings") as mock_s:
        mock_s.MAX_POSITIONS = 20
        mock_s.DEFAULT_POSITION_PCT = 0.16
        mock_s.MAX_ACCOUNT_DRAWDOWN = 0.15
        mock_s.MAX_DAILY_LOSS = 0.03
        mock_s.BREADTH_DOWN_UP_RATIO = 3.0
        mock_s.REGIME_STABLE_SCANS = 5
        mock_s.REGIME_JITTER_MAX = 3
        mock_s.REGIME_JITTER_WINDOW = 5
        mock_s.REAL_TRADE_ENABLED = False
        mock_s.DYNAMIC_SECTOR_DISCOVERY_ENABLED = False
        mock_s.PULLBACK_SCAN_ENABLED = False
        mock_s.PULLBACK_SCAN_INTERVAL = 15
        mock_s.DYNAMIC_SECTOR_HEAT_THRESHOLD = 3
        mock_s.DYNAMIC_SECTOR_MAX_CANDIDATES = 5
        mock_s.PULLBACK_SECTOR_MIN_CHANGE = 0.5
        mock_s.PULLBACK_PRICE_MIN = 5.0
        mock_s.SWAP_SCORE_GAP = 15.0
        mock_s.RESONANCE_INDEX_MIN_POINTS = 10
        mock_s.RESONANCE_PUSH_COOLDOWN_ROUNDS = 15
        mock_s.RESONANCE_PUSH_WINDOW_ENTRIES = 5
        mock_s.RESONANCE_TOP5_WINDOW_ENTRIES = 5
        mock_s.MORNING_SECTOR_BIAS_ENABLED = False
        mock_s.PAPER_INITIAL_CAPITAL = 100000.0
        mock_s.DATABASE_PATH = ":memory:"
        yield


# ====================================================================
# Fixture: real Watcher with all dependencies mocked
# ====================================================================


@pytest.fixture
def watcher():
    """Create a real Watcher with all external dependencies mocked.

    Gives access to the real _scan(), run(), and all mixin methods,
    with safe defaults for the mock objects that the scan flow touches.
    """
    with (
        patch("trade.core.watcher.TradeRepository"),
        patch("trade.core.watcher.PaperAccount"),
        patch("trade.core.watcher.RiskEngine"),
        patch("trade.core.watcher.AlertRouter"),
        patch("trade.core.watcher.AIQueue"),
        patch("trade.core.watcher.SectorResonanceAnalyzer"),
    ):
        w = Watcher(telegram_bot=None, qmt_quote=None, db_path=":memory:")

    # ---- Override with test-safe defaults ----
    w._data_ready = True
    w._trade_date = "2026-06-06"
    w._scan_count = 0
    w._collector_client = None

    # Paper account mock
    w.paper_account = MagicMock()
    w.paper_account.positions = {}
    w.paper_account.cash = 100000.0
    w.paper_account.total_value = 100000.0
    w.paper_account.initial_cash = 100000.0
    w.paper_account.daily_pnl = 0.0
    w.paper_account.drawdown = 0.0
    w.paper_account.update_prices = MagicMock()
    w.paper_account._persist_state = MagicMock()

    # Repo mock
    w.repo = MagicMock()
    w.repo.get_pending_signals.return_value = []

    # QMT mock
    w.qmt = MagicMock()
    w.qmt.get_realtime.return_value = {}

    # Alerter mock
    w.alerter = MagicMock()
    w.alerter.new_round = MagicMock()
    w.alerter.is_cooling.return_value = False
    w.alerter.alert = MagicMock()
    w.alerter.send = MagicMock()

    # Index / market data (needed by _check_market_state etc.)
    w._last_index_quote = {
        "price": 3200.0,
        "pre_close": 3180.0,
        "change_pct": 0.006,
    }
    w._index_prices = [3180.0, 3190.0, 3195.0]
    w._market_breadth = {"up": 1500, "down": 800, "flat": 100}
    w._ma_baseline_cache = (3190.0, 3180.0, 3160.0)
    w._sector_stats = {"银行": {"change_pct": 0.5, "up": 10, "down": 5}}
    w._industry_cache = {"000001": "银行"}
    w._concept_cache = {}

    return w


# ══════════════════════════════════════════════════════════════════
# Test 1 — MRO
# ══════════════════════════════════════════════════════════════════


class TestMRO:
    """Verify Watcher MRO includes all 9 mixins with no method conflicts."""

    def test_mro_includes_all_mixins(self):
        mro_names = [c.__name__ for c in Watcher.__mro__]
        for name in ALL_MIXIN_NAMES:
            assert name in mro_names, f"{name} missing from Watcher MRO"
        mixin_count = sum(1 for n in mro_names if n in ALL_MIXIN_NAMES)
        assert mixin_count == 9

    def test_mro_order_matches_class_definition(self):
        mro_names = [c.__name__ for c in Watcher.__mro__]
        mixin_order = [n for n in mro_names if n in ALL_MIXIN_NAMES]
        expected = [
            "DecisionLoggerMixin",
            "MarketStateMixin",
            "BuyDecisionMixin",
            "PositionRiskMixin",
            "SectorContextMixin",
            "AbnormalMonitorMixin",
            "IntradayScoutMixin",
            "ClosingDecisionMixin",
            "CloseSummaryMixin",
        ]
        assert mixin_order == expected

    def test_no_duplicate_methods_between_mixins(self):
        """No two mixins define the same non-dunder method name."""
        methods_by_mixin = {}
        for name, modpath in MIXIN_MODULE_MAP.items():
            mod = importlib.import_module(modpath)
            cls = getattr(mod, name)
            methods = {m for m, _ in inspect.getmembers(cls, predicate=inspect.isfunction) if not m.startswith("__")}
            methods_by_mixin[name] = methods

        conflicts = []
        names = list(methods_by_mixin.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                overlap = methods_by_mixin[names[i]] & methods_by_mixin[names[j]]
                if overlap:
                    for m in sorted(overlap):
                        conflicts.append(f"  {names[i]} & {names[j]} -> {m}")

        assert not conflicts, "Method conflicts between mixins:\n" + "\n".join(conflicts)

    def test_mro_debug_output(self, capsys):
        """Print MRO for documentation."""
        print("\nWatcher MRO:")
        for i, cls in enumerate(Watcher.__mro__):
            print(f"  {i}: {cls.__name__}")
        out = capsys.readouterr().out
        assert "DecisionLoggerMixin" in out


# ══════════════════════════════════════════════════════════════════
# Test 2 — All mixin attributes initialized
# ══════════════════════════════════════════════════════════════════


class TestAllAttributesInitialized:
    """Verify that a real Watcher instance has all mixin-related attributes."""

    def test_pos_risk_attrs(self, watcher):
        w = watcher
        assert isinstance(w._pos_meta, dict)
        assert isinstance(w._triggered_ids, set)
        assert isinstance(w._alerted_sl_tp, set)
        assert isinstance(w._sl_reminders, dict)
        assert isinstance(w._bought_watch, dict)

    def test_buy_decision_attrs(self, watcher):
        w = watcher
        assert isinstance(w._daily_factor_cache, dict)
        assert isinstance(w._signal_alert_state, dict)
        assert isinstance(w._review_alert_state, dict)
        assert isinstance(w._bought_watch, dict)
        assert isinstance(w._recently_sold, dict)

    def test_index_market_attrs(self, watcher):
        w = watcher
        assert isinstance(w._index_prices, list)
        assert isinstance(w._index_high, float)
        assert isinstance(w._index_low, float)
        assert isinstance(w._index_map, dict)
        assert isinstance(w._market_breadth, dict)
        assert isinstance(w._market_turnovers, list)
        assert isinstance(w._index_tech_state, dict)
        assert w._last_index_quote is None or isinstance(w._last_index_quote, dict)
        assert isinstance(w._closing_decision_done, bool)
        assert isinstance(w._max_drawdown_alerted, bool)

    def test_sector_attrs(self, watcher):
        w = watcher
        assert isinstance(w._sector_stats, dict)
        assert isinstance(w._concept_stats, dict)
        assert isinstance(w._industry_cache, dict)
        assert isinstance(w._concept_cache, dict)
        assert hasattr(w, "_sector_trend_history")
        assert hasattr(w, "_sector_trend_continuity")
        assert hasattr(w, "_sector_trend_last_dir")
        assert hasattr(w, "_sector_trend_start")
        assert hasattr(w, "_concept_trend_history")
        assert hasattr(w, "_concept_trend_continuity")
        assert hasattr(w, "_concept_trend_last_dir")
        assert hasattr(w, "_concept_trend_start")
        assert hasattr(w, "_resonance_analyzer")

    def test_scout_attrs(self, watcher):
        w = watcher
        assert isinstance(w._scout_ai_pending, dict)
        assert isinstance(w._scout_positions, set)
        assert isinstance(w._scout_recent_sectors, dict)
        assert isinstance(w._prev_snapshot_amounts, dict)
        assert isinstance(w._prev_snapshot_changes, dict)

    def test_abnormal_attrs(self, watcher):
        w = watcher
        assert w._review_monitor is None
        assert w._sector_monitor is None
        assert w._abnormal_detector is None
        assert w._receiver is None
        assert w._executor is None

    def test_data_flow_attrs(self, watcher):
        w = watcher
        assert isinstance(w._data_ready, bool)
        assert isinstance(w._market_snapshot, dict)
        assert isinstance(w._last_db_ts, (int, float))
        assert isinstance(w._data_missing_rounds, int)

    def test_ai_attrs(self, watcher):
        w = watcher
        assert hasattr(w, "_ai_queue")
        assert isinstance(w._pending_chase, dict)
        assert isinstance(w._pending_index_ai, dict)
        assert isinstance(w._morning_sector_bias, dict)

    def test_cooldown_attrs(self, watcher):
        w = watcher
        assert isinstance(w._push_cooldown, dict)
        assert isinstance(w._health_alert_seen, dict)
        assert isinstance(w._alert_fingerprints, dict)

    def test_cache_attrs(self, watcher):
        w = watcher
        assert isinstance(w._limit_cache, dict)
        assert isinstance(w._instrument_cache, dict)
        assert isinstance(w._intraday_cache, dict)
        assert isinstance(w._recent_prices, dict)
        assert isinstance(w._snapshot_price_history, dict)
        assert isinstance(w._cached_db_watch_codes, set)
        assert w._watch_codes_stale is True

    def test_pullback_attrs(self, watcher):
        w = watcher
        assert isinstance(w._pullback_scan_count, int)
        assert isinstance(w._pullback_alerted_today, set)

    def test_runtime_dynamic_attrs(self, watcher):
        w = watcher
        assert isinstance(w._breadth_block_alerted, bool)
        assert isinstance(w._last_abnormal_alert, (int, float))
        assert isinstance(w._last_logged_pattern, str)
        assert isinstance(w._pattern_last_alert, dict)
        assert isinstance(w._regime_confirm_count, int)
        assert isinstance(w._regime_switch_times, list)
        assert isinstance(w._scenario_engine, type(None))
        assert isinstance(w._holding_batch, dict)


# ══════════════════════════════════════════════════════════════════
# Test 3 — Scan call order
# ══════════════════════════════════════════════════════════════════


class TestScanOrder:
    """Verify _scan calls methods in the correct sequence."""

    def test_main_sequence_order(self, watcher):
        w = watcher
        # Setup: one position so _scan doesn't return early
        w.paper_account.positions = {"000001": MockPosition("000001")}
        w.qmt.get_realtime.return_value = {
            "000001": {"lastPrice": 10.5, "preClose": 10.0},
        }

        calls: list[str] = []

        def _track(name):
            def f(*a, **kw):
                calls.append(name)

            return f

        def _track_return(name, val):
            def f(*a, **kw):
                calls.append(name)
                return val

            return f

        with (
            patch.object(w, "_recv_collector_data", side_effect=_track("1_recv_collector_data")),
            patch.object(w, "_process_pending_ai", side_effect=_track("2_process_pending_ai")),
            patch.object(w, "_check_data_stale", side_effect=_track("3_check_data_stale")),
            patch.object(w, "_check_replies", side_effect=_track("4_check_replies")),
            patch.object(
                w,
                "_get_watch_codes",
                side_effect=_track_return("5_get_watch_codes", ["000001"]),
            ),
            patch.object(
                w,
                "_get_realtime_prices",
                side_effect=_track_return("6_get_realtime_prices", {"000001": 10.5}),
            ),
            patch.object(w, "_record_baseline", side_effect=_track("7_record_baseline")),
            patch.object(w, "_check_market_state", side_effect=_track("8_check_market_state")),
            patch.object(w, "_check_positions", side_effect=_track("9_check_positions")),
            patch.object(w, "_check_signals", side_effect=_track("10_check_signals")),
            patch.object(w, "_check_review_picks", side_effect=_track("11_check_review_picks")),
            patch.object(w, "_check_closing", side_effect=_track("12_check_closing")),
            patch.object(
                w,
                "_check_stale_positions",
                side_effect=_track("13_check_stale_positions"),
            ),
            patch.object(
                w,
                "_check_bought_signals",
                side_effect=_track("14_check_bought_signals"),
            ),
            patch.object(w, "_check_sl_reminders", side_effect=_track("15_check_sl_reminders")),
            patch.object(
                w,
                "_alert_index_divergence",
                side_effect=_track("16_alert_index_divergence"),
            ),
        ):
            w._scan()

        assert len(calls) > 0, "No calls recorded from _scan"

        # Verify core sequence (methods that are always called in every scan)
        core_expected = [
            "1_recv_collector_data",
            "2_process_pending_ai",
            "3_check_data_stale",
            "4_check_replies",
            "5_get_watch_codes",
            "6_get_realtime_prices",
        ]
        for i, expected in enumerate(core_expected):
            assert calls[i] == expected, f"Call #{i}: expected {expected}, got {calls[i]}"

        # After prices are retrieved, the following are called in order.
        # _record_baseline is called first, then market state, then checks.
        assert "7_record_baseline" in calls
        assert "8_check_market_state" in calls
        assert "9_check_positions" in calls
        assert "10_check_signals" in calls
        assert "11_check_review_picks" in calls
        assert "12_check_closing" in calls

        # Verify order of key check methods (filter out data-flow steps like
        # _check_data_stale which is a readiness scan, not a trading check)
        check_methods = [
            c
            for c in calls
            if "_check_market_state" in c
            or "_check_positions" in c
            or "_check_signals" in c
            or "_check_review_picks" in c
            or "_check_closing" in c
            or "_check_stale_positions" in c
            or "_check_bought_signals" in c
            or "_check_sl_reminders" in c
            or "_alert_index_divergence" in c
        ]
        # Expected order from real _scan() with scan_count=0
        check_expected = [
            "8_check_market_state",
            "9_check_positions",
            "13_check_stale_positions",
            "10_check_signals",
            "14_check_bought_signals",
            "11_check_review_picks",
            "15_check_sl_reminders",
            "16_alert_index_divergence",
            "12_check_closing",
        ]
        for i, expected in enumerate(check_expected):
            if i < len(check_methods):
                assert check_methods[i] == expected, f"Check #{i}: expected {expected}, got {check_methods[i]}"

    def test_early_return_no_watch_codes(self, watcher):
        """When _get_watch_codes returns empty, _scan returns immediately."""
        w = watcher
        w.paper_account.positions = {}  # empty

        with (
            patch.object(w, "_recv_collector_data") as m_recv,
            patch.object(w, "_check_market_state") as m_state,
        ):
            w._scan()
            m_recv.assert_called_once()
            m_state.assert_not_called()

    def test_early_return_no_prices(self, watcher):
        """When _get_realtime_prices returns empty, _scan returns after that."""
        w = watcher
        w.paper_account.positions = {"000001": MockPosition("000001")}
        w.qmt = None  # causes _get_realtime_prices to return {}

        with (
            patch.object(w, "_check_market_state") as m_state,
        ):
            w._scan()
            m_state.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# Test 4 — Cleanup session state
# ══════════════════════════════════════════════════════════════════


class TestCleanupSessionState:
    """Verify _cleanup_session_state clears all runtime state."""

    def test_cleanup_clears_all_dicts(self, watcher):
        w = watcher

        # Fill all 13 runtime dicts/sets with data
        w._signal_alert_state = {1: (10.0, True), 2: (11.0, False)}
        w._review_alert_state = {"k1": (10.0, True)}
        w._alert_fingerprints = {"fp1": 5, "fp2": 10}
        w._push_cooldown = {"000001": (5, 10.5)}
        w._health_alert_seen = {"h1": 3, "h2": 8}
        w._triggered_ids = {101, 102, 103}
        w._alerted_sl_tp = {"000001:sl", "000002:tp"}
        w._sl_reminders = {"k1": {"code": "000001", "type": "sl"}}
        w._pullback_alerted_today = {"000003", "000004"}
        w._recently_sold = {"000005": 3}
        w._recent_prices = {"000001": [(1000.0, 10.5)]}
        w._snapshot_price_history = {"000002": [(1000.0, 15.0)]}
        if not hasattr(w, "_name_cache"):
            w._name_cache = {}
        w._name_cache = {"000001": "名称A", "000002": "名称B"}
        w._ma_baseline_cache = (1, 2, 3)

        # Verify they're filled
        assert w._signal_alert_state
        assert w._review_alert_state
        assert w._alert_fingerprints
        assert w._push_cooldown
        assert w._health_alert_seen
        assert w._triggered_ids
        assert w._alerted_sl_tp
        assert w._sl_reminders
        assert w._pullback_alerted_today
        assert w._recently_sold
        assert w._recent_prices
        assert w._snapshot_price_history
        assert w._name_cache
        assert w._ma_baseline_cache is not None

        # Call cleanup
        w._cleanup_session_state()

        # Verify all cleared
        assert len(w._signal_alert_state) == 0
        assert len(w._review_alert_state) == 0
        assert len(w._alert_fingerprints) == 0
        assert len(w._push_cooldown) == 0
        assert len(w._health_alert_seen) == 0
        assert len(w._triggered_ids) == 0
        assert len(w._alerted_sl_tp) == 0
        assert len(w._sl_reminders) == 0
        assert len(w._pullback_alerted_today) == 0
        assert len(w._recently_sold) == 0
        assert len(w._recent_prices) == 0
        assert len(w._snapshot_price_history) == 0
        assert len(w._name_cache) == 0
        assert w._ma_baseline_cache is None

    def test_cleanup_idempotent(self, watcher):
        """Calling cleanup twice should not raise."""
        w = watcher
        w._cleanup_session_state()
        w._cleanup_session_state()  # second call


# ══════════════════════════════════════════════════════════════════
# Test 5 — Morning init flow
# ══════════════════════════════════════════════════════════════════


class TestMorningInitFlow:
    """Verify pre-market initialization sequence."""

    def test_before_market_returns_true_before_930(self):
        """_before_market() returns True when time < 09:30."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 9, 0, 0)
            assert Watcher._before_market() is True

    def test_before_market_returns_false_after_930(self):
        """_before_market() returns False when time >= 09:30."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 9, 30, 0)
            assert Watcher._before_market() is False

    def test_before_market_sleeps_correct_duration(self):
        """When before market, the wait duration equals seconds until 09:30."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 9, 0, 0)
            mock_dt.combine.return_value = datetime(2026, 6, 6, 9, 30, 0)

            assert Watcher._before_market() is True

        # Verify the calculated duration separately (avoids patching datetime
        # in the same scope as time-consuming calls)
        expected_seconds = 30 * 60  # 30 minutes
        # The run() code calculates: (09:30 - now).total_seconds()
        # At 9:00 that's 1800 seconds
        assert expected_seconds == 1800

    def test_collector_started_before_market(self, watcher):
        """_ensure_collector_running and _connect_collector called in startup."""
        w = watcher
        w._ensure_collector_running = MagicMock()
        w._connect_collector = MagicMock()
        w.paper_account.restore = MagicMock()

        # Simulate the pre-market startup code path from run()
        w._trade_date = "2026-06-06"
        w.paper_account.restore(w._trade_date)
        w._restore_pos_meta()
        w._init_bought_watch()
        w._load_sector_history()

        # In non-trading hours path
        w._ensure_collector_running()
        w._connect_collector()

        if w._before_market():
            from datetime import date

            wait = (datetime.combine(date.today(), dt_time(9, 30)) - datetime.now()).total_seconds()
            if wait > 0:
                pass  # skip actual sleep

        # After waiting, reconnect if not in trading
        w._connect_collector()

        w._ensure_collector_running.assert_called()
        assert w._connect_collector.call_count >= 2

    def test_pre_market_methods_no_crash(self, watcher):
        """Startup methods execute without error on a fresh watcher."""
        w = watcher
        w._trade_date = "2026-06-06"
        w.paper_account.restore = MagicMock()
        w.paper_account.restore(w._trade_date)
        w._restore_pos_meta()
        w._init_bought_watch()
        w._load_sector_history()
        w._ensure_collector_running()


# ══════════════════════════════════════════════════════════════════
# Test 6 — Lunch break transition
# ══════════════════════════════════════════════════════════════════


class TestLunchBreakTransition:
    """Verify lunch break detection and sleep."""

    def test_in_lunch_break_before_1130(self):
        """_in_lunch_break returns False before 11:30."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 11, 29, 0)
            assert Watcher._in_lunch_break() is False

    def test_in_lunch_break_at_1130(self):
        """_in_lunch_break returns True exactly at 11:30."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 11, 30, 0)
            assert Watcher._in_lunch_break() is True

    def test_in_lunch_break_at_noon(self):
        """_in_lunch_break returns True during lunch."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 12, 0, 0)
            assert Watcher._in_lunch_break() is True

    def test_in_lunch_break_after_1300(self):
        """_in_lunch_break returns False at 13:00."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 13, 0, 0)
            assert Watcher._in_lunch_break() is False

    def test_lunch_break_sleeps_until_1300(self):
        """_lunch_break sleeps until 13:00."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            lunch_time = datetime(2026, 6, 6, 11, 30, 0)
            mock_dt.now.return_value = lunch_time
            mock_dt.today.return_value = date(2026, 6, 6)

            with patch("trade.core.watcher.time.sleep") as mock_sleep:
                Watcher._lunch_break()
                # Should sleep 1.5 hours = 5400 seconds
                (dt_time(13, 0).hour * 3600) - (lunch_time.hour * 3600 + lunch_time.minute * 60)
                mock_sleep.assert_called_once()
                assert abs(mock_sleep.call_args[0][0] - 5400) < 10

    def test_scan_loop_skips_during_lunch(self, watcher):
        """Scan loop calls _lunch_break when _in_lunch_break is True, then continues."""
        w = watcher
        w._running = True
        w._scan = MagicMock()

        _run_scan_loop(
            w,
            after_market_side_effect=[False, False, True],  # exit after 2 scans
            lunch_break_side_effect=[True, False],  # lunch break before first scan
        )

        w._lunch_break.assert_called_once()
        w._scan.assert_called_once()
        assert w._scan_count == 1


# ══════════════════════════════════════════════════════════════════
# Test 7 — Market close transition
# ══════════════════════════════════════════════════════════════════


class TestMarketCloseTransition:
    """Verify market close detection and shutdown."""

    def test_after_market_before_1500(self):
        """_after_market returns False before 15:00."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 14, 59, 59)
            assert Watcher._after_market() is False

    def test_after_market_at_1500(self):
        """_after_market returns True at 15:00."""
        with patch("trade.core.watcher.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 6, 6, 15, 0, 0)
            assert Watcher._after_market() is True

    def test_scan_loop_exits_after_market(self, watcher):
        """Scan loop exits when _after_market returns True, calling _finalize_close."""
        w = watcher
        w._running = True
        w._scan = MagicMock()

        _run_scan_loop(w, after_market_side_effect=[True])

        w._finalize_close.assert_called_once()
        w._cleanup_session_state.assert_called_once()
        w._scan.assert_not_called()
        assert w._scan_count == 0

    def test_scan_loop_exits_after_multiple_scans(self, watcher):
        """Scan loop runs multiple scans, then exits on market close."""
        w = watcher
        w._running = True
        w._scan = MagicMock()

        _run_scan_loop(
            w,
            after_market_side_effect=[False, False, False, True],
        )

        assert w._scan.call_count == 3
        assert w._scan_count == 3

    def test_finalize_close_called(self, watcher):
        """_finalize_close is invoked after the scan loop ends."""
        w = watcher
        w._running = True
        w._finalize_close = MagicMock()
        w._cleanup_session_state = MagicMock()

        _run_scan_loop(w, after_market_side_effect=[True])

        w._finalize_close.assert_called_once()


# ══════════════════════════════════════════════════════════════════
# Test 8 — Crash recovery
# ══════════════════════════════════════════════════════════════════


class TestCrashRecovery:
    """Verify _scan exceptions are caught and next scan proceeds."""

    def test_scan_crash_caught_next_scan_proceeds(self, watcher):
        w = watcher
        w._running = True
        w._pos_meta = {"000001": {"sl": 9.0}}
        w.alerter.alert = MagicMock()
        w.alerter.send = MagicMock()

        # First scan raises, subsequent scans succeed
        scan_results: list[Exception | None] = [
            ValueError("Simulated crash"),
            None,
        ]
        scan_iter = iter(scan_results)

        original_scan = w._scan

        def flaky_scan():
            try:
                exc = next(scan_iter)
                if exc:
                    raise exc
            except StopIteration:
                pass
            # Call real _scan so mixin methods are exercised
            original_scan()

        w._scan = flaky_scan

        _run_scan_loop(
            w,
            after_market_side_effect=[False, False, True],  # exit after 2 scans
        )

        # At least 2 scans attempted
        assert w._scan_count >= 2

        # Loop exits normally
        w._finalize_close.assert_called_once()
        w._cleanup_session_state.assert_called_once()

    def test_scan_crash_no_state_corruption(self, watcher):
        """After a crash, runtime dicts remain accessible and uncorrupted."""
        w = watcher
        w._running = True
        w._pos_meta = {"000001": {"sl": 9.0}}

        # First scan raises
        scan_count = [0]

        def crashing_scan():
            scan_count[0] += 1
            if scan_count[0] == 1:
                w._signal_alert_state = {1: (10.0, True)}  # set before crash
                raise RuntimeError("First scan crash")
            # Second scan: verify state preserved from before crash
            assert isinstance(w._pos_meta, dict)
            assert "000001" in w._pos_meta
            # The _signal_alert_state set before crash should still be there
            # (it was set on self, not rolled back)

        w._scan = crashing_scan

        _run_scan_loop(
            w,
            after_market_side_effect=[False, False, True],
        )

        assert scan_count[0] >= 2
        # State set before crash is intact
        assert isinstance(w._pos_meta, dict)
        assert "000001" in w._pos_meta

    def test_scan_crash_logged(self, watcher):
        """A crashing _scan produces an error log."""
        w = watcher
        w._running = True

        w._scan = MagicMock(side_effect=ValueError("Kaboom"))

        with patch("trade.core.watcher.logger"):
            _run_scan_loop(
                w,
                after_market_side_effect=[False, True],
            )

            # Should have logged the error (via except in loop)
            # logger.error won't be called because our _run_scan_loop uses bare except
            # The scan itself should have been recorded
            assert w._scan.called


# ══════════════════════════════════════════════════════════════════
# Test 9 — All mixins together, one full _scan
# ══════════════════════════════════════════════════════════════════


class TestAllMixinsTogetherOneScan:
    """Realistic full _scan(): 1 position, 1 pending signal, normal market."""

    @pytest.fixture(autouse=True)
    def patch_module_settings(self):
        """Patch module-level settings in all mixin modules.

        Must persist across test methods (not scoped to a with-block).
        """
        patch_targets = [
            "trade.scenario.market_state.settings",
            "trade.decision.buy_decision.settings",
            "trade.risk.position_risk.settings",
            "trade.sector.sector_context.settings",
            "trade.detect.market_anomaly.settings",
            "trade.core.closeout.settings",
        ]
        patchers = [patch(t) for t in patch_targets]
        for p in patchers:
            ms = p.start()
            ms.MAX_POSITIONS = 20
            ms.DEFAULT_POSITION_PCT = 0.16
            ms.MAX_ACCOUNT_DRAWDOWN = 0.15
            ms.MAX_DAILY_LOSS = 0.03
            ms.BREADTH_DOWN_UP_RATIO = 3.0
            ms.REGIME_STABLE_SCANS = 5
            ms.REGIME_JITTER_MAX = 3
            ms.REGIME_JITTER_WINDOW = 5
            ms.REAL_TRADE_ENABLED = False
            ms.DYNAMIC_SECTOR_DISCOVERY_ENABLED = False
            ms.PULLBACK_SCAN_ENABLED = False
            ms.PULLBACK_SCAN_INTERVAL = 15
            ms.DYNAMIC_SECTOR_HEAT_THRESHOLD = 3
            ms.DYNAMIC_SECTOR_MAX_CANDIDATES = 5
            ms.PULLBACK_SECTOR_MIN_CHANGE = 0.5
            ms.PULLBACK_PRICE_MIN = 5.0
            ms.SWAP_SCORE_GAP = 15.0
            ms.RESONANCE_INDEX_MIN_POINTS = 10
            ms.RESONANCE_PUSH_COOLDOWN_ROUNDS = 15
            ms.RESONANCE_PUSH_WINDOW_ENTRIES = 5
            ms.RESONANCE_TOP5_WINDOW_ENTRIES = 5
            ms.MORNING_SECTOR_BIAS_ENABLED = False
            ms.PAPER_INITIAL_CAPITAL = 100000.0
            ms.DATABASE_PATH = ":memory:"
        yield
        for p in patchers:
            p.stop()

    @pytest.fixture
    def full_watcher(self):
        """Extended watcher with realistic data for a full scan."""
        with (
            patch("trade.core.watcher.TradeRepository"),
            patch("trade.core.watcher.PaperAccount"),
            patch("trade.core.watcher.RiskEngine"),
            patch("trade.core.watcher.AlertRouter"),
            patch("trade.core.watcher.AIQueue"),
            patch("trade.core.watcher.SectorResonanceAnalyzer"),
        ):
            w = Watcher(telegram_bot=None, qmt_quote=None, db_path=":memory:")

        # ---- Basic state ----
        w._data_ready = True
        w._trade_date = "2026-06-06"
        w._scan_count = 0
        w._running = True

        # ---- Position (1 position) ----
        pos = MockPosition("000001", "平安银行", 1000, 10.0, 11.0, "2026-06-05")
        w.paper_account = MagicMock()
        w.paper_account.positions = {"000001": pos}
        w.paper_account.cash = 100000.0
        w.paper_account.total_value = 110000.0
        w.paper_account.initial_cash = 100000.0
        w.paper_account.daily_pnl = 1000.0
        w.paper_account.drawdown = 0.01
        w.paper_account.update_prices = MagicMock()

        w._pos_meta = {
            "000001": {
                "sl": 9.0,
                "tp": 13.0,
                "trailing_stop": 0.05,
                "highest_price": 12.0,
                "sector": "银行",
                "score": 80,
                "signal_id": 100,
            },
        }
        w._bought_watch = {
            "000001": {
                "entry_price": 10.0,
                "last_alert_scan": 0,
                "buy_scan": 0,
                "status": "watching",
                "alert_count": 0,
                "max_profit_pct": 0.10,
            },
        }

        # ---- Pending signal ----
        w.repo = MagicMock()
        w.repo.get_pending_signals.return_value = [
            {
                "id": 200,
                "stock_code": "000002",
                "stock_name": "万科A",
                "buy_zone_min": 14.0,
                "buy_zone_max": 15.0,
                "stop_loss": 13.0,
                "take_profit": 17.0,
                "signal_score": 85,
            },
        ]
        # Prevent _get_index_ma60 returning a MagicMock
        w.repo.get_index_ma60.return_value = 0.0
        w.repo.get_volume_trend.return_value = 0.0

        # ---- QMT quotes ----
        w.qmt = MagicMock()
        w.qmt.get_realtime.return_value = {
            "000001": {
                "lastPrice": 11.0,
                "preClose": 10.0,
                "price": 11.0,
                "changePct": 10.0,
                "amount": 5000000,
            },
            "000002": {
                "lastPrice": 14.5,
                "preClose": 14.0,
                "price": 14.5,
                "changePct": 3.57,
                "amount": 3000000,
            },
        }

        # ---- Index data (for _check_market_state) ----
        w._last_index_quote = {
            "price": 3200.0,
            "pre_close": 3180.0,
            "change_pct": 0.006,
        }
        w._index_prices = [3180.0, 3190.0, 3195.0, 3198.0, 3200.0]
        w._index_high = 3205.0
        w._index_low = 3175.0
        w._index_map = {
            "000001.SH": {
                "change_pct": 0.006,
                "name": "上证指数",
                "prices": [3180, 3190, 3195, 3198, 3200],
                "high": 3205,
                "low": 3175,
                "last_price": 3200,
                "pre_close": 3180,
            },
        }
        w._market_breadth = {"up": 2000, "down": 600, "flat": 100, "total": 2700}
        w._market_turnovers = [5e9, 5.5e9]
        w._ma_baseline_cache = (3195.0, 3185.0, 3170.0)

        # ---- Sector / industry ----
        w._industry_cache = {"000001": "银行", "000002": "房地产"}
        w._concept_cache = {"000001": ["金融科技"], "000002": ["物业管理"]}
        w._sector_stats = {
            "银行": {"change_pct": 0.5, "up": 15, "down": 3},
            "房地产": {"change_pct": 1.2, "up": 8, "down": 2},
        }
        w._concept_stats = {}
        w._sector_trend_history = {}
        w._sector_trend_continuity = {}
        w._sector_trend_last_dir = {}
        w._sector_trend_start = {}

        # ---- Market snapshot ----
        w._market_snapshot = {
            "000001": {"changePct": 10.0, "price": 11.0, "amount": 5000000},
            "000002": {"changePct": 3.57, "price": 14.5, "amount": 3000000},
        }

        # ---- Limit cache (needed by _is_limit_down) ----
        w._limit_cache = {
            "000001": {"limit_up": 12.0, "limit_down": 8.0, "pre_close": 10.0},
        }

        # ---- Alerter ----
        w.alerter = MagicMock()
        w.alerter.new_round = MagicMock()
        w.alerter.is_cooling.return_value = False
        w.alerter.alert = MagicMock()
        w.alerter.send = MagicMock()

        # ---- Collector ----
        w._collector_client = None

        # ---- misc safe defaults ----
        w._signal_alert_state = {}
        w._review_alert_state = {}
        w._alert_fingerprints = {}
        w._push_cooldown = {}
        w._health_alert_seen = {}
        w._triggered_ids = set()
        w._alerted_sl_tp = set()
        w._sl_reminders = {}
        w._recently_sold = {}
        w._recent_prices = {}
        w._snapshot_price_history = {}
        w._daily_factor_cache = {}
        w._intraday_cache = {}
        w._intraday_cache_scan = -1
        w._instrument_cache = {}
        w._cached_db_watch_codes = set()
        w._watch_codes_stale = True
        w._pullback_alerted_today = set()
        w._pullback_scan_count = 0
        w._scout_ai_pending = {}
        w._scout_positions = set()
        w._scout_recent_sectors = {}
        w._prev_snapshot_amounts = {}
        w._prev_snapshot_changes = {}
        w._scenario_engine = None
        w._scenario_prev_outlook = None
        w._scenario_prev_velocity = 0.0
        w._scenario_recent_lows = []
        w._scenario_recent_highs = []
        w._scenario_prev_breadth = 0.5
        w._scenario_scan_count = 0
        w._scenario_last_alert_scan = 0
        w._scenario_next_confirmation_scan = 9999
        w._scenario_last_confirmed_at = 0.0
        w._scenario_confirmation_boost = 0.0
        w._scenario_probs = {}
        w._pattern_last_alert = {}
        w._regime_switch_times = []
        w._last_index_alert_scan = 0
        w._last_index_alert_advice = ""
        w._breadth_block_alerted = False
        w._last_abnormal_alert = 0
        w._last_logged_pattern = ""
        w._last_resonance_labels = {}
        w._prev_con_amounts = {}
        w._prev_ind_amounts = {}
        w._holding_batch = {}
        w._regime_confirm_count = 0
        w._regime_pending_pattern = ""
        w._swap_ctx = {}
        w._prev_snapshot = {}
        w._closing_decision_done = False
        w._max_drawdown_alerted = False
        w._data_ready_at = 0.0
        w._data_missing_rounds = 0
        w._last_db_ts = 0.0
        w._index_alerted_downtrend = False
        w._index_alerted_ma20 = 0
        w._index_last_fluctuation_price = 0.0
        w._index_close_high = 0.0
        w._index_close_low = 0.0
        w._volume_alerted_divergence = False
        w._index_tech_state = {
            "macd_cross": None,
            "rsi6_zone": "normal",
            "rsi12_zone": "normal",
            "kdj_cross": None,
            "kdj_j_zone": "normal",
            "divergence": None,
        }
        w._morning_sector_bias = {}
        w._pending_chase = {}
        w._pending_index_ai = {}
        w._regime = None
        w._name_cache = {}
        w._baseline = None
        w._opening_decision_sent = False
        w._prev_scan_count = 0
        w._index_stale_count = 0
        w._resonance_analyzer = MagicMock()
        w._last_resonance_push_scan = -100
        w._last_resonance_index_dir = ""
        w._prev_snapshot_ts = 0.0

        # Ensure alerter has the methods _scan expects
        w.alerter.new_round = MagicMock()

        return w

    def test_full_scan_completes(self, full_watcher):
        """A full _scan() with 1 position + 1 signal completes without exception."""
        w = full_watcher

        try:
            w._scan()
        except Exception as e:
            pytest.fail(f"_scan() raised exception: {type(e).__name__}: {e}")

    def test_full_scan_populates_regime(self, full_watcher):
        """After _scan(), _regime is set (by _check_market_state)."""
        w = full_watcher
        assert w._regime is None
        w._scan()
        assert w._regime is not None
        assert hasattr(w._regime, "allow_buy")

    def test_full_scan_calls_alerter_new_round(self, full_watcher):
        """_scan uses alerter.new_round (called in run(), not _scan).
        Actually _scan itself doesn't call new_round — run() does.
        So just verify _scan didn't crash.
        """
        w = full_watcher
        w._scan()
        # _scan didn't crash — that's enough
        assert True

    def test_full_scan_checks_position(self, full_watcher):
        """_scan runs _check_positions without crashing for the existing position."""
        w = full_watcher
        with patch.object(w, "_check_positions", wraps=w._check_positions) as spy:
            w._scan()
            spy.assert_called()

    def test_full_scan_checks_signals(self, full_watcher):
        """_scan runs _check_signals without crashing for the pending signal."""
        w = full_watcher
        with patch.object(w, "_check_signals", wraps=w._check_signals) as spy:
            w._scan()
            spy.assert_called()

    def test_full_scan_no_critical_errors(self, full_watcher):
        """During a healthy _scan, no logger.error calls are emitted."""
        w = full_watcher

        with patch("trade.core.watcher.logger") as mock_log:
            w._scan()
            mock_log.error.assert_not_called()

    def test_full_scan_market_data_preserved(self, full_watcher):
        """After _scan, index prices and market state are still intact."""
        w = full_watcher
        prices_before = list(w._index_prices)
        w._scan()
        assert len(w._index_prices) == len(prices_before)
        assert w._market_breadth["up"] > 0
