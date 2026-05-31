"""TrendScreener 单元测试 — 新多因子筛选器"""

import sqlite3
import tempfile

import pytest

from analysis.screening.trend import TrendScreener
from analysis.signals import StockScore

# =====================  Fixtures  =====================


@pytest.fixture
def db_path():
    """创建临时 SQLite 数据库并写入模拟数据。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_indicators (
            stock_code TEXT, trade_date TEXT,
            bbi_weekly REAL,
            PRIMARY KEY (stock_code, trade_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS regulatory_letter (
            stock_code TEXT, trade_date TEXT, risk_type TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cls_telegraph (
            trade_date TEXT, ai_stocks TEXT, ai_summary TEXT,
            ai_status TEXT, ai_sentiment TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE stock_basic (
            stock_code TEXT, stock_name TEXT, trade_date TEXT,
            change_pct REAL, total_market_cap REAL, circ_market_cap REAL,
            turnover_rate REAL, volume_ratio REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma5_angle REAL,
            industry TEXT, price REAL, open REAL, high REAL, low REAL,
            prev_close REAL, main_force_net REAL, main_force_ratio REAL,
            avg_vol_5d REAL, avg_vol_20d REAL,
            amplitude REAL, super_large_net REAL,
            large_net REAL, medium_net REAL, small_net REAL,
            pe_ttm REAL, pb_ratio REAL, revenue_growth REAL, profit_growth REAL
        )
    """)

    # 写入历史数据(前20天)让多日因子可以工作
    history_rows = []
    for day_offset in range(1, 21):
        d = (
            f"2025-01-{15 - day_offset:02d}"
            if day_offset <= 14
            else f"2024-12-{31 - (day_offset - 15):02d}"
        )
        for code, name in [("000001", "强趋势A"), ("000002", "强趋势B")]:
            history_rows.append(
                (
                    code,
                    name,
                    d,
                    1.0,
                    80_0000_0000,
                    40_0000_0000,
                    4.0,
                    1.3,
                    11.0,
                    10.5,
                    10.0,
                    5.0,
                    "科技",
                    11.5,
                    11.0,
                    11.8,
                    10.8,
                    11.2,
                    300_0000,
                    0.03,
                    100_0000,
                    100_0000,
                    3.0,
                    0,
                    0,
                    0,
                    0,
                    15.0,
                    1.5,
                    10.0,
                    8.0,
                )
            )

    # 当日数据
    rows = [
        # 000001: 放量启动(vol_ratio>=1.5) + 主力介入(mf_ratio>3) → 2 tags → 场景命中
        (
            "000001",
            "强趋势A",
            3.5,
            100_0000_0000,
            50_0000_0000,
            5.0,
            1.5,
            11.50,
            10.80,
            10.00,
            8.0,
            "科技",
            11.80,
            11.30,
            11.90,
            10.70,
            11.50,
            500_0000,
            5.0,
            130_0000,
            100_0000,
            3.0,
            0,
            0,
            0,
            0,
            15.0,
            1.5,
            10.0,
            8.0,
        ),
        # 000002: 放量启动 + 主力介入
        (
            "000002",
            "强趋势B",
            2.0,
            80_0000_0000,
            40_0000_0000,
            4.0,
            1.6,
            12.00,
            11.00,
            10.00,
            6.0,
            "金融",
            12.30,
            11.80,
            12.50,
            11.20,
            12.00,
            300_0000,
            4.0,
            100_0000,
            100_0000,
            4.0,
            0,
            0,
            0,
            0,
            12.0,
            1.0,
            5.0,
            3.0,
        ),
        # 000003: 缩量回调(vol_ratio<=0.7, chg<=0) 不满足
        (
            "000003",
            "弱票C",
            0.5,
            70_0000_0000,
            35_0000_0000,
            6.0,
            0.8,
            10.00,
            10.20,
            9.80,
            3.0,
            "医药",
            10.50,
            10.30,
            10.80,
            10.0,
            10.40,
            200_0000,
            2.0,
            100_0000,
            100_0000,
            2.5,
            0,
            0,
            0,
            0,
            20.0,
            2.0,
            3.0,
            1.0,
        ),
        # 000004: ST 股，hard_gates 直接过滤
        (
            "000004",
            "*ST失败",
            0.8,
            90_0000_0000,
            45_0000_0000,
            7.0,
            1.5,
            10.50,
            11.00,
            10.00,
            2.0,
            "消费",
            10.90,
            10.50,
            11.20,
            10.30,
            10.80,
            150_0000,
            5.0,
            100_0000,
            100_0000,
            3.5,
            0,
            0,
            0,
            0,
            18.0,
            1.8,
            6.0,
            4.0,
        ),
        # 000005: 688 开头，hard_gates 直接过滤
        (
            "688001",
            "科创板",
            2.0,
            60_0000_0000,
            30_0000_0000,
            3.5,
            1.5,
            10.40,
            10.10,
            10.00,
            2.0,
            "半导体",
            10.50,
            10.20,
            10.80,
            10.00,
            10.30,
            100_0000,
            4.0,
            100_0000,
            100_0000,
            1.5,
            0,
            0,
            0,
            0,
            30.0,
            3.0,
            15.0,
            12.0,
        ),
    ]

    for r in rows:
        conn.execute(
            """
            INSERT INTO stock_basic
                (stock_code, stock_name, trade_date, change_pct,
                 total_market_cap, circ_market_cap,
                 turnover_rate, volume_ratio,
                 ma5, ma10, ma20, ma5_angle,
                 industry, price, open, high, low, prev_close,
                 main_force_net, main_force_ratio,
                 avg_vol_5d, avg_vol_20d,
                 amplitude, super_large_net, large_net, medium_net, small_net,
                 pe_ttm, pb_ratio, revenue_growth, profit_growth)
            VALUES (?, ?, '2025-01-15',
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?)
        """,
            r,
        )

    for r in history_rows:
        conn.execute(
            """
            INSERT INTO stock_basic
                (stock_code, stock_name, trade_date, change_pct,
                 total_market_cap, circ_market_cap,
                 turnover_rate, volume_ratio,
                 ma5, ma10, ma20, ma5_angle,
                 industry, price, open, high, low, prev_close,
                 main_force_net, main_force_ratio,
                 avg_vol_5d, avg_vol_20d,
                 amplitude, super_large_net, large_net, medium_net, small_net,
                 pe_ttm, pb_ratio, revenue_growth, profit_growth)
            VALUES (?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?)
        """,
            r,
        )

    conn.commit()
    conn.close()
    return path


@pytest.fixture
def screener(db_path):
    return TrendScreener(db_path=db_path, min_score=50, top_n=10)


# =====================  Tests  =====================


class TestTrendScreener:
    def test_instantiation(self, db_path):
        s = TrendScreener(db_path=db_path)
        assert s.db_path == db_path
        assert s.min_score == 50
        assert s.top_n == 10

    def test_screen_returns_stock_scores(self, screener):
        results = screener.screen("2025-01-15")
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, StockScore) for r in results)

    def test_good_stocks_present(self, screener):
        """000001 和 000002 放量启动+主力介入，应被选入。"""
        results = screener.screen("2025-01-15")
        codes = {r.stock_code for r in results}
        assert "000001" in codes
        assert "000002" in codes

    def test_hard_gates_filter_st(self, screener):
        """ST 股被 hard_gates 过滤。"""
        results = screener.screen("2025-01-15")
        codes = {r.stock_code for r in results}
        assert "000004" not in codes

    def test_hard_gates_filter_688(self, screener):
        """688 开头的科创板被 hard_gates 过滤。"""
        results = screener.screen("2025-01-15")
        codes = {r.stock_code for r in results}
        assert "688001" not in codes

    def test_weak_stock_filtered(self, screener):
        """000003 只有量比因子不满足，应被过滤(因子数<2)。"""
        results = screener.screen("2025-01-15")
        codes = {r.stock_code for r in results}
        assert "000003" not in codes

    def test_score_range(self, screener):
        results = screener.screen("2025-01-15")
        for r in results:
            assert 0 <= r.score <= 100

    def test_result_order(self, screener):
        results = screener.screen("2025-01-15")
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_panic_market_returns_empty(self, screener):
        results = screener.screen("2025-01-15", market_state="恐慌")
        assert results == []

    def test_no_matching_date(self, screener):
        results = screener.screen("2099-12-31")
        assert results == []
