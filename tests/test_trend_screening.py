"""TrendScreener 单元测试"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from analysis.screening import TrendScreener
from analysis.signals import StockScore


# =====================  Fixtures  =====================


@pytest.fixture
def db_path():
    """创建内存 SQLite 数据库并写入模拟数据，返回数据库路径。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE stock_basic (
            stock_code TEXT, stock_name TEXT, trade_date TEXT,
            change_pct REAL, total_market_cap REAL, circ_market_cap REAL,
            turnover_rate REAL, volume_ratio REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma5_angle REAL,
            industry TEXT, price REAL,
            main_force_net REAL, main_force_ratio REAL,
            avg_vol_5d REAL, avg_vol_20d REAL,
            amplitude REAL, super_large_net REAL,
            large_net REAL, medium_net REAL, small_net REAL
        )
    """)

    rows = [
        # (code, name, change, tmc, cmc, turovr, volr, ma5, ma10, ma20, angle, ind, price,
        #  mf_net, mf_ratio, avg5, avg20, amplitude)
        #
        # Row 1 — strong, spread=15%, score=55.0
        ("000001", "强趋势A", 3.5,
         100_0000_0000, 50_0000_0000,
         5.0, 1.2,
         11.50, 10.80, 10.00, 8.0,
         "科技", 11.80,
         500_0000, 0.05,
         100_0000, 100_0000, 3.0),

        # Row 2 — strong, spread=20%, score=60.0
        ("000002", "强趋势B", 2.0,
         80_0000_0000, 40_0000_0000,
         4.0, 1.5,
         12.00, 11.00, 10.00, 6.0,
         "金融", 12.30,
         300_0000, 0.03,
         100_0000, 100_0000, 4.0),

        # Row 3 — normal, bias_ma20=7.14%, score≈66.4
        ("000003", "稳健C", 0.5,
         70_0000_0000, 35_0000_0000,
         6.0, 0.8,
         10.00, 10.20, 9.80, 3.0,
         "医药", 10.50,
         200_0000, 0.02,
         100_0000, 100_0000, 2.5),

        # Row 4 — normal, bias_ma20=9.0%, score=65.5
        ("000004", "稳健D", 0.8,
         90_0000_0000, 45_0000_0000,
         7.0, 0.9,
         10.50, 11.00, 10.00, 2.0,
         "消费", 10.90,
         150_0000, 0.015,
         100_0000, 100_0000, 3.5),

        # Row 5 — barely strong, score=44 (< min_score 默认50), 应被过滤
        ("000005", "弱趋势E", 1.0,
         60_0000_0000, 30_0000_0000,
         3.5, 1.0,
         10.40, 10.10, 10.00, 2.0,
         "消费", 10.50,
         100_0000, 0.01,
         100_0000, 100_0000, 1.5),

        # Row 6 — bias_ma20=11%, 不满足 normal 条件
        ("000006", "边界F", 1.5,
         55_0000_0000, 28_0000_0000,
         5.5, 0.7,
         10.30, 10.60, 10.00, 1.0,
         "地产", 11.10,
         50_0000, 0.005,
         100_0000, 100_0000, 2.0),
    ]

    for r in rows:
        conn.execute("""
            INSERT INTO stock_basic
                (stock_code, stock_name, trade_date, change_pct,
                 total_market_cap, circ_market_cap,
                 turnover_rate, volume_ratio,
                 ma5, ma10, ma20, ma5_angle,
                 industry, price,
                 main_force_net, main_force_ratio,
                 avg_vol_5d, avg_vol_20d,
                 amplitude, super_large_net, large_net, medium_net, small_net)
            VALUES (?, ?, '2025-01-15',
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    ?, ?,
                    ?, ?,
                    0, 0, 0, 0)
        """, r)

    conn.commit()
    conn.close()
    return path


@pytest.fixture
def screener(db_path):
    return TrendScreener(db_path=db_path, min_score=50, top_n=10)


# =====================  Tests  =====================


class TestTrendScreener:

    def test_instantiation(self, db_path):
        """验证 TrendScreener 可实例化。"""
        s = TrendScreener(db_path=db_path)
        assert s.db_path == db_path
        assert s.min_score == 50
        assert s.top_n == 10

    def test_screen_returns_stock_scores(self, screener, db_path):
        """验证 screen() 返回 list[StockScore]。"""
        results = screener.screen("2025-01-15")
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, StockScore) for r in results)

    def test_strong_detection(self, screener):
        """验证强趋势检测逻辑：000001 和 000002 应被识别。"""
        results = screener.screen("2025-01-15")
        strong_codes = {r.stock_code for r in results if r.trend_mode == "strong"}
        assert "000001" in strong_codes
        assert "000002" in strong_codes

        r1 = next(r for r in results if r.stock_code == "000001")
        assert r1.trend_mode == "strong"
        assert r1.score == 55.0
        assert r1.bias_ma5 == pytest.approx(2.61, abs=0.01)  # (11.8-11.5)/11.5 * 100

        r2 = next(r for r in results if r.stock_code == "000002")
        assert r2.trend_mode == "strong"
        assert r2.score == 60.0
        assert r2.bias_ma5 == pytest.approx(2.5, abs=0.01)

    def test_normal_detection(self, screener):
        """验证稳健趋势检测逻辑：000003 和 000004 应被识别。"""
        results = screener.screen("2025-01-15")
        normal_codes = {r.stock_code for r in results if r.trend_mode == "normal"}
        assert "000003" in normal_codes
        assert "000004" in normal_codes

        r3 = next(r for r in results if r.stock_code == "000003")
        assert r3.trend_mode == "normal"
        assert r3.score == pytest.approx(66.4, abs=0.1)
        assert r3.bias_ma20 == pytest.approx(7.14, abs=0.01)

        r4 = next(r for r in results if r.stock_code == "000004")
        assert r4.trend_mode == "normal"
        assert r4.score == 65.5
        assert r4.bias_ma20 == 9.0

    def test_excludes_boundary_normal(self, screener):
        """验证排除边界股票：000006 不应出现在结果中（bias_ma20=11% > 10%）。"""
        results = screener.screen("2025-01-15")
        codes = {r.stock_code for r in results}
        assert "000006" not in codes

    def test_excludes_low_score_strong(self, screener):
        """验证低分强趋势被过滤：000005 (score=44) 不应出现。"""
        results = screener.screen("2025-01-15")
        codes = {r.stock_code for r in results}
        assert "000005" not in codes

    def test_min_score_filter(self, db_path):
        """min_score=60 时只保留 >=60 的候选。"""
        s = TrendScreener(db_path=db_path, min_score=60, top_n=10)
        results = s.screen("2025-01-15")
        codes = {r.stock_code for r in results}
        # 000002 strong score=60 >=60 ✓
        assert "000002" in codes
        # 000001 strong score=55 < 60 ✗
        assert "000001" not in codes
        # normal stocks all ~65+ >=60 ✓
        assert "000003" in codes
        assert "000004" in codes
        # 所有结果 score >= min_score
        assert all(r.score >= 60 for r in results)

    def test_result_order(self, screener):
        """验证结果按 score 降序排列。"""
        results = screener.screen("2025-01-15")
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_top_n_limits(self, db_path):
        """验证 top_n=1 时每种模式只返回 1 只。"""
        s = TrendScreener(db_path=db_path, min_score=0, top_n=1)
        results = s.screen("2025-01-15")
        strong_count = sum(1 for r in results if r.trend_mode == "strong")
        normal_count = sum(1 for r in results if r.trend_mode == "normal")
        assert strong_count <= 1
        assert normal_count <= 1

    def test_no_matching_date(self, screener):
        """验证不存在的交易日返回空列表。"""
        results = screener.screen("2099-12-31")
        assert results == []
