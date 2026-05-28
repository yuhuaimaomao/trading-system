# -*- coding: utf-8 -*-
"""ReviewPickMonitor 行为模式盯盘单元测试"""

import sqlite3
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from trade.monitor.review_picks import (
    PickState,
    ReviewPickMonitor,
    NEAR_MA_RATIO,
    NEAR_TARGET_RATIO,
    NEAR_STOP_RATIO,
    ZONE_DWELL_WARN,
    RANGE_NARROW_RATIO,
)


# =====================  Fixtures  =====================


@pytest.fixture
def mock_telegram():
    return MagicMock()


@pytest.fixture
def db_path():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    conn = sqlite3.connect(path)

    conn.execute("""
        CREATE TABLE stock_tracker (
            id INTEGER, push_date TEXT, stock_code TEXT, stock_name TEXT,
            plate TEXT, star_rating INTEGER, market_cap REAL,
            reason_keywords TEXT, source TEXT, sector_code TEXT,
            abandon_condition TEXT, stop_loss REAL, target_price REAL
        )
    """)
    conn.execute("""
        CREATE TABLE stock_basic (
            stock_code TEXT, stock_name TEXT, trade_date TEXT,
            ma5 REAL, ma10 REAL, ma20 REAL, prev_close REAL,
            industry TEXT
        )
    """)

    # 复盘推荐数据
    conn.execute("""
        INSERT INTO stock_tracker VALUES
        (1, '2026-05-26', '000001', '平安银行', '银行', 85, 200000000000.0,
         'MA20回踩', '复盘', 'bank', '', 10.80, 13.50)
    """)
    conn.execute("""
        INSERT INTO stock_tracker VALUES
        (2, '2026-05-26', '000002', '万科A', '房地产', 60, 150000000000.0,
         '突破回踩', '复盘', 'estate', '', 7.50, 10.00)
    """)

    # 参考位数据
    conn.execute("""
        INSERT INTO stock_basic VALUES
        ('000001', '平安银行', '2026-05-26', 11.80, 11.50, 11.20, 12.00, '银行')
    """)
    conn.execute("""
        INSERT INTO stock_basic VALUES
        ('000002', '万科A', '2026-05-26', 8.30, 8.10, 8.00, 8.50, '房地产')
    """)

    conn.commit()
    conn.close()
    yield path
    import os
    os.unlink(path)


@pytest.fixture
def monitor(db_path, mock_telegram):
    return ReviewPickMonitor(db_path=db_path, telegram_bot=mock_telegram)


@pytest.fixture
def loaded_monitor(monitor):
    monitor.load_picks()
    return monitor


# =====================  PickState 单元测试  =====================


class TestPickState:
    def test_init_defaults(self):
        s = PickState()
        assert s.code == ""
        assert s.name == ""
        assert s.stars == "★★★"
        assert s.ma5 == 0.0
        assert s.current_zone == ""

    def test_update_price_updates_ohlc(self):
        s = PickState()
        s.update_price(12.00)
        assert s.open_price == 12.00
        assert s.high_price == 12.00
        assert s.low_price == 12.00
        assert s.scan_count == 1

        s.update_price(12.50)
        assert s.open_price == 12.00  # 不变
        assert s.high_price == 12.50
        assert s.low_price == 12.00

        s.update_price(11.80)
        assert s.low_price == 11.80
        assert s.high_price == 12.50
        assert s.scan_count == 3

    def test_price_history_capped_at_30(self):
        s = PickState()
        for i in range(40):
            s.update_price(10.0 + i * 0.1)
        assert len(s.price_history) == 30
        assert s.price_history[-1] == 10.0 + 39 * 0.1

    def test_zone_duration_returns_zero_when_not_entered(self):
        s = PickState()
        assert s.zone_duration == 0

    def test_zone_duration_tracks_elapsed(self):
        s = PickState()
        s.zone_entered_at = time.time() - 120
        assert 119 <= s.zone_duration <= 121

    def test_zone_range_zero_when_no_low(self):
        s = PickState()
        assert s.zone_range == 0

    def test_zone_range_calculation(self):
        s = PickState()
        s.zone_low = 10.00
        s.zone_high = 10.20
        assert abs(s.zone_range - 2.0) < 0.01


# =====================  区域分类测试  =====================


class TestZoneClassification:
    """_classify_zone 的 7 个区域。优先级: target > stop > below_ma20 > near_ma20 > near_ma10 > above_ma5 > ma5_ma20"""

    def test_near_target(self):
        s = PickState(target_price=13.50, ma5=12.0, ma10=11.5, ma20=11.2)
        near = 13.50 * (1 + NEAR_TARGET_RATIO * 0.5)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._classify_zone(s, near) == "near_target"

    def test_near_stop(self):
        s = PickState(stop_loss=10.80, target_price=0, ma5=11.80, ma10=11.50, ma20=11.20)
        near_stop = 10.80 * 1.01  # +1%
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._classify_zone(s, near_stop) == "near_stop"

    def test_below_ma20(self):
        s = PickState(ma20=11.20, ma5=11.80, target_price=0, stop_loss=0)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._classify_zone(s, 11.00) == "below_ma20"

    def test_near_ma20(self):
        s = PickState(ma20=11.20, ma5=11.80, target_price=0, stop_loss=0)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._classify_zone(s, 11.21) == "near_ma20"

    def test_near_ma10(self):
        s = PickState(ma10=11.50, ma20=11.20, ma5=11.80, target_price=0, stop_loss=0)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._classify_zone(s, 11.52) == "near_ma10"

    def test_above_ma5(self):
        s = PickState(ma5=11.80, ma10=11.50, ma20=11.20, target_price=0, stop_loss=0)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._classify_zone(s, 12.00) == "above_ma5"

    def test_ma5_ma20_between(self):
        s = PickState(ma5=11.80, ma20=11.20, ma10=11.50, target_price=0, stop_loss=0)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._classify_zone(s, 11.75) == "ma5_ma20"

    def test_unknown_when_no_ma(self):
        s = PickState(ma5=0, ma10=0, ma20=0, target_price=0, stop_loss=0)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._classify_zone(s, 10.00) == "unknown"


# =====================  开盘验证测试  =====================


class TestOpeningCheck:
    def test_high_open_triggers_alert(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      prev_close=12.00, open_price=12.50)
        s.scan_count = 1
        s.price_history = [12.50]
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msg = m._check_opening(s, {})
        assert msg is not None
        assert "平安银行" in msg
        assert "+4.2%" in msg

    def test_low_open_triggers_warning(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      prev_close=12.00, open_price=11.50)
        s.scan_count = 1
        s.price_history = [11.50]
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msg = m._check_opening(s, {})
        assert msg is not None
        assert "⚠️" in msg

    def test_normal_open_no_alert(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      prev_close=12.00, open_price=12.10)
        s.scan_count = 1
        s.price_history = [12.10]
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msg = m._check_opening(s, {})
        assert msg is None

    def test_after_first_scan_no_alert(self):
        s = PickState(code="000001", name="平安银行", prev_close=12.00)
        s.scan_count = 2
        s.price_history = [12.50]
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msg = m._check_opening(s, {})
        assert msg is None

    def test_no_prev_close_skip(self):
        s = PickState(code="000001", name="平安银行", prev_close=0)
        s.scan_count = 1
        s.price_history = [12.50]
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msg = m._check_opening(s, {})
        assert msg is None

    def test_alerted_only_once(self):
        s = PickState(code="000001", name="平安银行", prev_close=12.00,
                      open_price=12.50, alerted_zones={"000001:open"})
        s.scan_count = 1
        s.price_history = [12.50]
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msg = m._check_opening(s, {})
        assert msg is None


# =====================  区域转换测试  =====================


class TestZoneTransition:
    def test_enter_near_ma20(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      ma20=11.20, ma5=11.80, ma10=11.50, price_history=[11.22])
        s.current_zone = "ma5_ma20"
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._zone_transition_messages(s, "ma5_ma20", "near_ma20", 11.22)
        assert any("MA20 回踩" in msg for msg in msgs)

    def test_bounce_from_ma20(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      ma20=11.20, ma5=11.80, ma10=11.50, zone_entered_price=11.20)
        s.current_zone = "near_ma20"
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._zone_transition_messages(s, "near_ma20", "ma5_ma20", 11.50)
        assert any("MA20 回踩确认" in msg for msg in msgs)

    def test_break_below_ma20(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      ma20=11.20, ma5=11.80, ma10=11.50)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._zone_transition_messages(s, "ma5_ma20", "below_ma20", 11.00)
        assert any("跌破 MA20" in msg for msg in msgs)

    def test_approach_target(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      target_price=13.50)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._zone_transition_messages(s, "above_ma5", "near_target", 13.20)
        assert any("接近目标价" in msg for msg in msgs)

    def test_approach_stop(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      stop_loss=10.80)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._zone_transition_messages(s, "near_ma20", "near_stop", 10.90)
        assert any("止损位" in msg for msg in msgs)

    def test_ma10_support_bounce(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      ma10=11.50)
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._zone_transition_messages(s, "near_ma10", "above_ma5", 11.80)
        assert any("MA10 支撑有效" in msg for msg in msgs)

    def test_duplicate_zone_alert_suppressed(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★",
                      ma20=11.20, alerted_zones={"000001:zone:near_ma20"})
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._zone_transition_messages(s, "ma5_ma20", "near_ma20", 11.22)
        assert len(msgs) == 0


# =====================  check_zone 流程测试  =====================


class TestCheckZone:
    def test_same_zone_updates_high_low(self):
        s = PickState(code="000001", ma5=12.0, ma20=11.2, ma10=11.5)
        s.current_zone = "above_ma5"
        s.zone_high = 12.30
        s.zone_low = 12.10
        s.price_history = [12.50]
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._check_zone(s)
        assert len(msgs) == 0
        assert s.zone_high == 12.50
        assert s.zone_low == 12.10

    def test_zone_change_resets_range(self):
        s = PickState(code="000001", ma5=11.80, ma20=11.20, ma10=11.50)
        s.current_zone = "above_ma5"
        s.zone_high = 13.00
        s.zone_low = 12.00
        s.price_history = [11.22]
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        m._check_zone(s)
        assert s.current_zone == "near_ma20"
        assert s.zone_high == 11.22
        assert s.zone_low == 11.22


# =====================  行为模式测试  =====================


class TestBehavior:
    def test_dwell_too_long_warns(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★")
        s.current_zone = "near_ma20"
        s.zone_entered_at = time.time() - ZONE_DWELL_WARN * 60 - 10
        s.zone_high = 11.30
        s.zone_low = 11.20
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._check_behavior(s)
        assert any("方向待选" in msg for msg in msgs)

    def test_range_narrow_warns(self):
        s = PickState(code="000001", name="平安银行", stars="★★★★★")
        s.current_zone = "near_ma20"
        s.zone_entered_at = time.time() - 700  # >10 min
        s.zone_low = 11.20
        s.zone_high = 11.21  # 极窄振幅
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._check_behavior(s)
        assert any("振幅收窄" in msg for msg in msgs)

    def test_no_behavior_check_outside_key_zones(self):
        s = PickState(code="000001")
        s.current_zone = "above_ma5"
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._check_behavior(s)
        assert len(msgs) == 0

    def test_dwell_alerted_only_once(self):
        s = PickState(code="000001", name="平安银行",
                      alerted_behaviors={"000001:dwell"})
        s.current_zone = "near_ma20"
        s.zone_entered_at = time.time() - ZONE_DWELL_WARN * 60 - 10
        s.zone_high = 11.30
        s.zone_low = 11.20
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        msgs = m._check_behavior(s)
        assert not any("方向待选" in msg for msg in msgs)


# =====================  评估测试  =====================


class TestAssess:
    def test_above_ma5_green(self):
        s = PickState(current_zone="above_ma5")
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert "🟢" in m._assess(s)

    def test_ma20_pullback_early_yellow(self):
        s = PickState(current_zone="near_ma20")
        s.zone_entered_at = time.time() - 100  # < 600s
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._assess(s) == "🟡 回踩观察中"

    def test_ma20_dwell_long_yellow(self):
        s = PickState(current_zone="near_ma20")
        s.zone_entered_at = time.time() - 700  # > 600s
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._assess(s) == "🟡 方向待选"

    def test_below_ma20_red(self):
        s = PickState(current_zone="below_ma20")
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert "🔴" in m._assess(s)

    def test_near_stop_red(self):
        s = PickState(current_zone="near_stop")
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert "🔴" in m._assess(s)

    def test_near_target_green(self):
        s = PickState(current_zone="near_target")
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert "🟢" in m._assess(s)

    def test_ma5_ma20_yellow(self):
        s = PickState(current_zone="ma5_ma20")
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert "🟡" in m._assess(s)


# =====================  评估摘要测试  =====================


class TestAssessmentSummary:
    def test_groups_by_status(self):
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assessments = [
            ("000001", "平安银行", "🟢 符合预期"),
            ("000002", "万科A", "🟡 回踩观察中"),
            ("000003", "招商银行", "🔴 不及预期"),
        ]
        summary = m._build_assessment_summary(assessments)
        assert "🟢" in summary
        assert "🟡" in summary
        assert "🔴" in summary
        assert "平安银行" in summary
        assert "万科A" in summary

    def test_empty_assessments(self):
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assert m._build_assessment_summary([]) == ""

    def test_only_green(self):
        m = ReviewPickMonitor.__new__(ReviewPickMonitor)
        assessments = [("000001", "平安银行", "🟢 符合预期")]
        summary = m._build_assessment_summary(assessments)
        assert "🟢" in summary
        assert "🟡" not in summary
        assert "🔴" not in summary


# =====================  load_picks 集成测试  =====================


class TestLoadPicks:
    def test_loads_from_tracker(self, loaded_monitor):
        assert loaded_monitor._loaded
        assert len(loaded_monitor._picks) == 2

    def test_state_initialized_with_ma(self, loaded_monitor):
        state = loaded_monitor._states.get("000001")
        assert state is not None
        assert state.name == "平安银行"
        assert state.ma5 == 11.80
        assert state.ma20 == 11.20
        assert state.prev_close == 12.00
        assert state.target_price == 13.50
        assert state.stop_loss == 10.80

    def test_stars_assignment(self, loaded_monitor):
        state_85 = loaded_monitor._states.get("000001")
        state_60 = loaded_monitor._states.get("000002")
        assert state_85.stars == "★★★★★"
        assert state_60.stars == "★★★★"

    def test_empty_db_loads_gracefully(self, mock_telegram):
        import os
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = tmp.name
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE stock_tracker (id INTEGER)")
        conn.commit()
        conn.close()
        try:
            m = ReviewPickMonitor(db_path=path, telegram_bot=mock_telegram)
            m.load_picks()
            assert m._loaded
            assert len(m._picks) == 0
        finally:
            os.unlink(path)


# =====================  check 集成测试  =====================


class TestCheckIntegration:
    def test_check_updates_states(self, loaded_monitor):
        prices = {"000001": 12.50, "000002": 8.40}
        msgs = loaded_monitor.check(prices)
        # 两只都在 above_ma5
        assert loaded_monitor._states["000001"].current_zone == "above_ma5"
        assert loaded_monitor._states["000002"].current_zone == "above_ma5"

    def test_check_skips_unknown_codes(self, loaded_monitor):
        prices = {"999999": 50.00}
        msgs = loaded_monitor.check(prices)
        assert len(msgs) == 0

    def test_check_autoloads_if_not_loaded(self, monitor):
        prices = {"000001": 12.50}
        msgs = monitor.check(prices)
        assert monitor._loaded

    def test_empty_prices_no_crash(self, loaded_monitor):
        msgs = loaded_monitor.check({})
        assert msgs == []

    def test_ma20_zone_entry_generates_message(self, loaded_monitor):
        prices = {"000001": 11.22, "000002": 8.40}
        msgs = loaded_monitor.check(prices)
        # 000001 进入 near_ma20
        assert any("MA20 回踩" in m for m in msgs)

    def test_below_ma20_generates_warning(self, loaded_monitor):
        # MA20=11.20, stop=10.80, 11.15 < MA20 且 (11.15-10.80)/10.80=3.24% > 3% 避开near_stop
        prices = {"000001": 11.15, "000002": 8.40}
        msgs = loaded_monitor.check(prices)
        assert any("跌破 MA20" in m for m in msgs)

    def test_assessment_summary_every_5_scans(self, loaded_monitor):
        """第5轮扫描输出评估摘要。check() 内 update_price 会 +1，所以设 4。"""
        for code in ["000001", "000002"]:
            state = loaded_monitor._states[code]
            state.scan_count = 4
            state.current_zone = "above_ma5"
            state.price_history = [12.50]
        prices = {"000001": 12.50, "000002": 8.40}
        msgs = loaded_monitor.check(prices)
        assert any("复盘标的评估" in m for m in msgs)


# =====================  辅助函数测试  =====================


class TestHelpers:
    def test_zone_label(self):
        assert ReviewPickMonitor._zone_label("above_ma5") == "MA5上方"
        assert ReviewPickMonitor._zone_label("near_ma20") == "MA20附近"
        assert ReviewPickMonitor._zone_label("unknown_zone") == "unknown_zone"

    def test_score_to_stars(self):
        assert ReviewPickMonitor._score_to_stars(90) == "★★★★★"
        assert ReviewPickMonitor._score_to_stars(80) == "★★★★★"
        assert ReviewPickMonitor._score_to_stars(70) == "★★★★"
        assert ReviewPickMonitor._score_to_stars(60) == "★★★★"
        assert ReviewPickMonitor._score_to_stars(30) == "★★★"
        assert ReviewPickMonitor._score_to_stars(None) == "★★★"
