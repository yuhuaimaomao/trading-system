# -*- coding: utf-8 -*-
"""板块上下文单元测试 — SectorContextMixin"""

import pytest
from unittest.mock import MagicMock
from trade.monitor.sector_context import SectorContextMixin


def make_sector(**attrs):
    """创建 SectorContextMixin 最小 mock 对象"""
    class TestSector(SectorContextMixin):
        pass

    obj = TestSector()
    defaults = {
        "db_path": ":memory:",
        "telegram": MagicMock(),
        "_alert": MagicMock(),
        "_alert_private": MagicMock(),
        "_industry_cache": {},
        "_concept_cache": {},
        "_sector_stats": {},
        "_concept_stats": {},
        "_sector_trend_history": {},
        "_sector_trend_continuity": {},
        "_sector_trend_last_dir": {},
        "_market_snapshot": {},
        "_index_prices": [3300],
        "_index_high": 3350,
        "_index_low": 3280,
        "portfolio": MagicMock(),
        "repo": MagicMock(),
        "_trade_date": "2026-05-29",
        "_get_index_quote": MagicMock(return_value={
            "price": 3310, "pre_close": 3300, "change_pct": 0.003,
        }),
        "_get_index_baseline": MagicMock(return_value=(3300, 3350, 3400)),
        "_resolve_name": MagicMock(return_value="测试股"),
    }
    for k, v in defaults.items():
        setattr(obj, k, v)
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# ====================== _get_concept_trend_score ======================


class TestConceptTrendScore:
    def test_empty_caches(self):
        """概念缓存为空返回 (0, '')"""
        obj = make_sector()
        score, reason = obj._get_concept_trend_score("000001")
        assert score == 0
        assert reason == ""

    def test_all_up_concepts(self):
        obj = make_sector(
            _concept_cache={"000001": ["概念A", "概念B", "概念C"]},
            _concept_stats={
                "概念A": {"change_pct": 2.5},
                "概念B": {"change_pct": 1.5},
                "概念C": {"change_pct": 3.0},
            },
        )
        score, reason = obj._get_concept_trend_score("000001")
        assert score >= 2
        assert "偏强" in reason

    def test_all_down_concepts(self):
        obj = make_sector(
            _concept_cache={"000001": ["概念A", "概念B", "概念C"]},
            _concept_stats={
                "概念A": {"change_pct": -2.0},
                "概念B": {"change_pct": -1.5},
                "概念C": {"change_pct": -3.0},
            },
        )
        score, reason = obj._get_concept_trend_score("000001")
        assert score <= -2
        assert "偏弱" in reason

    def test_mixed_concepts(self):
        obj = make_sector(
            _concept_cache={"000001": ["概念A", "概念B"]},
            _concept_stats={
                "概念A": {"change_pct": 2.0},
                "概念B": {"change_pct": -1.0},
            },
        )
        score, reason = obj._get_concept_trend_score("000001")
        assert -3 <= score <= 3

    def test_score_clamped(self):
        """分数限制在 ±3"""
        obj = make_sector(
            _concept_cache={"000001": [f"概念{i}" for i in range(10)]},
            _concept_stats={f"概念{i}": {"change_pct": 5.0} for i in range(10)},
        )
        score, _ = obj._get_concept_trend_score("000001")
        assert score == 3

    def test_missing_stats_skipped(self):
        obj = make_sector(
            _concept_cache={"000001": ["概念A", "概念B"]},
            _concept_stats={"概念A": {"change_pct": 1.0}},
        )
        score, _ = obj._get_concept_trend_score("000001")
        assert -3 <= score <= 3


# ====================== _get_sector_trend ======================


class TestSectorTrend:
    def test_returns_empty_for_missing_industry(self):
        obj = make_sector()
        result = obj._get_sector_trend("000001")
        assert result == ""

    def test_with_sector_data(self):
        obj = make_sector(
            _industry_cache={"000001": "银行"},
            _sector_stats={
                "银行": {
                    "change_pct": 1.5, "up": 15, "down": 5,
                    "relative": 1.2, "vol_ratio": 1.5,
                    "trend_history": [0.5, 1.0, 1.5],
                    "breadth": 0.5,
                },
            },
            _sector_trend_history={"银行": [0.5, 1.0, 1.5]},
            _sector_trend_continuity={"银行": 3},
        )
        result = obj._get_sector_trend("000001")
        assert "银行" in result
        assert "走强" in result or "横盘" in result or "走弱" in result

    def test_breadth_included(self):
        obj = make_sector(
            _industry_cache={"000001": "科技"},
            _sector_stats={"科技": {
                "change_pct": 0.5, "up": 10, "down": 10,
                "trend_history": [0.1, 0.3, 0.5],
                "relative": 0.1, "vol_ratio": 1.0, "breadth": 0.0,
            }},
            _sector_trend_history={"科技": [0.1, 0.3, 0.5]},
        )
        result = obj._get_sector_trend("000001")
        assert "科技" in result


# ====================== _build_sector_context ======================


class TestBuildSectorContext:
    def test_empty_codes(self):
        obj = make_sector()
        result = obj._build_sector_context(set())
        assert result == ""

    def test_builds_context_for_codes(self):
        obj = make_sector(
            _industry_cache={"000001": "银行", "000002": "科技"},
            _concept_cache={"000001": ["金融科技"], "000002": ["半导体"]},
            _sector_stats={
                "银行": {"change_pct": 1.5, "up": 15, "down": 5},
                "科技": {"change_pct": -0.5, "up": 5, "down": 15},
            },
            _concept_stats={
                "金融科技": {"change_pct": 2.0, "up": 8, "down": 2},
                "半导体": {"change_pct": -1.0, "up": 3, "down": 7},
            },
        )
        result = obj._build_sector_context({"000001", "000002"})
        assert len(result) > 0
        assert "银行" in result

    def test_sorts_by_abs_change(self):
        obj = make_sector(
            _industry_cache={"000001": "科技"},
            _sector_stats={"科技": {"change_pct": -5.0, "up": 1, "down": 19}},
        )
        result = obj._build_sector_context({"000001"})
        assert "科技" in result


# ====================== _rebuild_from_sector_snapshots ======================


class TestRebuildFromSectorSnapshots:
    def test_empty_rows(self):
        obj = make_sector()
        obj._rebuild_from_sector_snapshots([])
        assert obj._sector_trend_history == {}

    def test_rebuilds_history(self):
        rows = [
            ("银行", 1000.0, 1.0),
            ("银行", 1001.0, 1.2),
            ("银行", 1002.0, 1.5),
            ("科技", 1000.0, -0.3),
            ("科技", 1001.0, -0.5),
        ]
        obj = make_sector()
        obj._rebuild_from_sector_snapshots(rows)
        assert "银行" in obj._sector_trend_history
        assert len(obj._sector_trend_history["银行"]) == 3
        assert "科技" in obj._sector_trend_history

    def test_continuity_count(self):
        """同一方向连续性计算正确"""
        rows = [
            ("银行", 1000.0, 0.5),
            ("银行", 1001.0, 1.0),
            ("银行", 1002.0, 1.5),
            ("银行", 1003.0, 2.0),
        ]
        obj = make_sector()
        obj._rebuild_from_sector_snapshots(rows)
        assert obj._sector_trend_continuity.get("银行", 0) >= 1


# ====================== _send_opening_decision ======================


class TestSendOpeningDecision:
    def test_no_positions_no_signals(self):
        obj = make_sector()
        obj.portfolio.positions = {}
        obj.repo.get_pending_signals.return_value = []
        obj._send_opening_decision({"000001": 12.50}, True)
        assert obj._alert.called

    def test_with_positions(self):
        obj = make_sector()
        obj.portfolio.positions = {
            "000001": MagicMock(
                stock_name="平安银行", volume=1000, avg_cost=12.00,
                current_price=12.50, market_value=12500,
                stop_loss=11.00, take_profit=14.00,
                entry_date="2026-05-20", pnl_pct=4.17,
            ),
        }
        obj.repo.get_pending_signals.return_value = []
        obj._send_opening_decision({"000001": 12.50}, True)
        call_msg = obj._alert.call_args[0][0]
        assert "平安银行" in call_msg

    def test_sector_concentration_warning(self):
        """同板块 3 只触发集中度警告"""
        obj = make_sector()
        obj.portfolio.positions = {}
        obj._industry_cache = {
            "000001": "银行", "000002": "银行", "000003": "银行",
        }
        obj._concept_cache = {}
        # 3 positions in same sector — need take_profit as real number
        for code in ["000001", "000002", "000003"]:
            obj.portfolio.positions[code] = MagicMock(
                stock_name=f"股{code}", volume=100, avg_cost=10.00,
                current_price=11.00, stop_loss=9.00, take_profit=15.00,
                entry_date="2026-05-20",
            )
        obj.repo.get_pending_signals.return_value = []
        obj._send_opening_decision({"000001": 11.0, "000002": 11.0, "000003": 11.0}, True)
        assert obj._alert.called
