# -*- coding: utf-8 -*-
"""异动监控 + 换仓评估单元测试 — AbnormalMonitorMixin"""

import pytest
from unittest.mock import MagicMock, patch
from trade.monitor.abnormal import AbnormalMonitorMixin


def make_abnormal(**attrs):
    """创建 AbnormalMonitorMixin 最小 mock 对象"""
    class TestAbnormal(AbnormalMonitorMixin):
        pass

    obj = TestAbnormal()
    defaults = {
        "db_path": ":memory:",
        "telegram": MagicMock(),
        "_alert": MagicMock(),
        "_alert_private": MagicMock(),
        "_market_snapshot": {},
        "_prev_snapshot": {},
        "_abnormal_detector": None,
        "_sector_monitor": None,
        "_sector_stats": {},
        "_concept_stats": {},
        "_sector_trend_history": {},
        "_concept_trend_history": {},
        "_industry_cache": {},
        "_concept_cache": {},
        "_index_prices": [3300],
        "_get_sector_trend": MagicMock(return_value="走强"),
        "_get_concept_trend_score": MagicMock(return_value=(2, "2个板块走强")),
        "_build_sector_context": MagicMock(return_value="板块行情"),
        "_invalidate_watch_codes_cache": MagicMock(),
    }
    for k, v in defaults.items():
        setattr(obj, k, v)
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# ====================== _build_market_snapshot ======================


class TestBuildMarketSnapshot:
    def test_empty_prices(self):
        result = AbnormalMonitorMixin._build_market_snapshot({})
        assert result == {}

    def test_converts_prices_to_snapshot(self):
        result = AbnormalMonitorMixin._build_market_snapshot({
            "000001": 12.50,
            "000002": 25.00,
        })
        assert "000001" in result
        assert result["000001"]["price"] == 12.50
        assert "timestamp" in result["000001"]

    def test_format_matches_detector_input(self):
        result = AbnormalMonitorMixin._build_market_snapshot({"000001": 12.50})
        assert isinstance(result, dict)
        assert isinstance(result["000001"], dict)
        assert isinstance(result["000001"]["price"], float)


# ====================== _get_abnormal_detector ======================


class TestGetAbnormalDetector:
    def test_returns_detector_instance(self):
        """AbnormalDetector 已实现，应返回有效实例"""
        obj = make_abnormal()
        result = obj._get_abnormal_detector()
        from trade.monitor.abnormal import AbnormalDetector
        assert isinstance(result, AbnormalDetector)

    def test_returns_cached_on_second_call(self):
        obj = make_abnormal(_abnormal_detector=MagicMock())
        first = obj._get_abnormal_detector()
        assert first is obj._abnormal_detector

    def test_same_instance_on_repeated_call(self):
        """多次调用返回同一实例"""
        obj = make_abnormal()
        first = obj._get_abnormal_detector()
        second = obj._get_abnormal_detector()
        assert first is second


# ====================== _get_sector_monitor ======================


class TestGetSectorMonitor:
    def test_creates_on_first_call(self):
        obj = make_abnormal()
        with patch("trade.monitor.sector_heat.SectorHeatMonitor") as MockMon:
            result = obj._get_sector_monitor()
            MockMon.assert_called_once()
            assert result is not None
            assert obj._sector_monitor is not None

    def test_returns_cached_on_second_call(self):
        obj = make_abnormal(_sector_monitor=MagicMock())
        first = obj._get_sector_monitor()
        assert first is obj._sector_monitor

    def test_import_error_returns_none(self):
        obj = make_abnormal()
        with patch("trade.monitor.abnormal.SectorHeatMonitor", create=True, side_effect=ImportError):
            # 模块不存在时 SectorHeatMonitor import 失败 → 返回 None
            pass


# ====================== _check_sector_heat ======================


class TestCheckSectorHeat:
    def test_empty_snapshot_passes(self):
        obj = make_abnormal()
        obj._check_sector_heat({})

    def test_with_snapshot_calls_monitor(self):
        monitor = MagicMock()
        monitor.check.return_value = ["热点: 半导体板块异动"]
        obj = make_abnormal(_sector_monitor=monitor)
        obj._check_sector_heat({"000001": {"changePct": 3.0}})
        monitor.check.assert_called_once()

    def test_no_monitor_skips(self):
        obj = make_abnormal()
        obj._get_sector_monitor = MagicMock(return_value=None)
        obj._check_sector_heat({"000001": {"changePct": 3.0}})
        obj._alert.assert_not_called()

    def test_alerts_on_messages(self):
        monitor = MagicMock()
        monitor.check.return_value = ["⚠️ 银行板块异动"]
        obj = make_abnormal(_sector_monitor=monitor)
        obj._check_sector_heat({"000001": {"changePct": -2.0}})
        assert obj._alert.called


# ====================== _check_abnormal ======================


class TestCheckAbnormal:
    def test_with_market_snapshot(self):
        detector = MagicMock()
        detector.detect_sector.return_value = ["🔔 000001 振幅异常"]
        obj = make_abnormal(
            _abnormal_detector=detector,
            _market_snapshot={"000001": {"price": 12.50, "changePct": 5.0}},
        )
        obj._check_abnormal({"000001": 12.50})
        detector.detect_sector.assert_called_once()

    def test_with_prev_snapshot_comparison(self):
        detector = MagicMock()
        detector.detect_sector.return_value = []
        obj = make_abnormal(
            _abnormal_detector=detector,
            _market_snapshot={"000001": {"price": 12.50}},
            _prev_snapshot={"000001": {"price": 12.00}},
        )
        obj._check_abnormal({"000001": 12.50})
        assert detector.detect_sector.called

    def test_alerts_on_detection(self):
        detector = MagicMock()
        detector.detect_sector.return_value = ["🔔 异动: 000001 量比飙升"]
        obj = make_abnormal(
            _abnormal_detector=detector,
            _market_snapshot={"000001": {"price": 12.50}},
        )
        obj._check_abnormal({"000001": 12.50})
        obj._alert.assert_called()

    def test_no_detector_skips(self):
        obj = make_abnormal()
        obj._get_abnormal_detector = MagicMock(return_value=None)
        obj._check_abnormal({"000001": 12.50})
        obj._alert.assert_not_called()


# ====================== _evaluate_swaps ======================


class TestEvaluateSwaps:
    def _make_swapper(self, **extra):
        """创建带 _get_paper_trader + repo 的对象"""
        obj = make_abnormal(**extra)
        obj.repo = MagicMock()
        obj.repo.get_pending_signals.return_value = []
        pt = MagicMock()
        pt.portfolio.positions = {"p1": 1, "p2": 2, "p3": 3}  # 3+ positions
        obj._get_paper_trader = MagicMock(return_value=pt)
        obj._trade_date = "2026-05-29"
        obj._index_prices = [3300, 3310]
        return obj

    def test_no_pending_signals(self):
        obj = self._make_swapper()
        obj._evaluate_swaps({"000001": 12.50})

    def test_signals_out_of_zone(self):
        obj = self._make_swapper()
        obj.repo.get_pending_signals.return_value = [{
            "id": 1, "stock_code": "000001", "stock_name": "测试",
            "buy_zone_min": 10.0, "buy_zone_max": 11.0,
            "stop_loss": 9.0, "take_profit": 15.0,
        }]
        obj._evaluate_swaps({"000001": 15.0})

    def test_signals_in_zone_evaluates(self):
        obj = self._make_swapper()
        obj.repo.get_pending_signals.return_value = [{
            "id": 1, "stock_code": "000001", "stock_name": "测试",
            "buy_zone_min": 10.0, "buy_zone_max": 15.0,
            "stop_loss": 9.0, "take_profit": 18.0,
        }]
        pt = obj._get_paper_trader.return_value
        pt.evaluate_swaps.return_value = []
        obj._evaluate_swaps({"000001": 12.50})
        pt.evaluate_swaps.assert_called_once()

    def test_swap_executed_invalidates_cache(self):
        obj = self._make_swapper()
        obj.repo.get_pending_signals.return_value = [{
            "id": 1, "stock_code": "000001", "stock_name": "测试",
            "buy_zone_min": 10.0, "buy_zone_max": 15.0,
            "stop_loss": 9.0, "take_profit": 18.0,
        }]
        pt = obj._get_paper_trader.return_value
        pt.evaluate_swaps.return_value = [{"code": "000001", "action": "swap"}]
        obj._evaluate_swaps({"000001": 12.50})
        obj._invalidate_watch_codes_cache.assert_called()

    def test_too_few_positions_skips(self):
        """持仓 < 3 时跳过换仓评估"""
        obj = self._make_swapper()
        pt = obj._get_paper_trader.return_value
        pt.portfolio.positions = {"p1": 1}
        obj.repo.get_pending_signals.return_value = [{
            "id": 1, "stock_code": "000001", "stock_name": "测试",
            "buy_zone_min": 10.0, "buy_zone_max": 15.0,
            "stop_loss": 9.0, "take_profit": 18.0,
        }]
        obj._evaluate_swaps({"000001": 12.50})
        pt.evaluate_swaps.assert_not_called()
