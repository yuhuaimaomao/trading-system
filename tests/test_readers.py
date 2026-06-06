"""DB-based tests for data/readers/ (stock_reader, sector_reader, limit_pool_reader).

Uses temp SQLite DB with minimal test data. No mocking, no external deps.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from data.readers.limit_pool_reader import LimitPoolReader
from data.readers.sector_reader import SectorReader
from data.readers.stock_reader import StockReader

# ── helpers ──────────────────────────────────────────────────────────


_READER_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS stock_basic (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    price REAL, open REAL, high REAL, low REAL, prev_close REAL,
    change_pct REAL, total_market_cap REAL, circ_market_cap REAL,
    turnover_rate REAL, volume_ratio REAL, amplitude REAL, volume REAL,
    ma5 REAL, ma10 REAL, ma20 REAL, ma5_angle REAL,
    industry TEXT, concepts TEXT,
    main_force_net REAL, main_force_ratio REAL,
    super_large_net REAL, large_net REAL, medium_net REAL, small_net REAL,
    avg_vol_5d REAL, avg_vol_20d REAL,
    pe_ttm REAL, pb_ratio REAL, revenue_growth REAL, profit_growth REAL,
    pe_dynamic REAL
);

CREATE TABLE IF NOT EXISTS stock_indicators (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    ma60 REAL, ma120 REAL,
    bb_upper REAL, bb_mid REAL, bb_lower REAL, bb_pct_b REAL, bb_width REAL,
    macd_dif REAL, macd_dea REAL, macd_bar REAL,
    kdj_k REAL, kdj_d REAL, kdj_j REAL,
    rsi6 REAL, rsi12 REAL, rsi24 REAL,
    bbi_daily REAL, bbi_weekly REAL
);

CREATE TABLE IF NOT EXISTS sector_info (
    sector_code TEXT PRIMARY KEY,
    sector_name TEXT,
    need_collect INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sector_industry (
    trade_date TEXT NOT NULL,
    sector_code TEXT NOT NULL,
    sector_name TEXT,
    change_percent REAL,
    up_count INTEGER,
    main_force_net REAL,
    super_large_net REAL,
    top_stock TEXT,
    top_stock_change REAL,
    latest_price REAL
);

CREATE TABLE IF NOT EXISTS sector_concept (
    trade_date TEXT NOT NULL,
    sector_code TEXT NOT NULL,
    sector_name TEXT,
    change_percent REAL,
    up_count INTEGER,
    main_force_net REAL,
    super_large_net REAL
);

CREATE TABLE IF NOT EXISTS sector_stocks (
    sector_code TEXT NOT NULL,
    stock_code TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS limit_pool (
    trade_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    pool_type TEXT,
    consecutive_boards INTEGER,
    first_seal_time TEXT,
    seal_amount REAL,
    zt_stat TEXT,
    industry TEXT,
    open_count INTEGER
);
"""

_TEST_DATA_SQL = """
INSERT INTO stock_basic (trade_date, stock_code, stock_name, price, change_pct,
    total_market_cap, circ_market_cap, turnover_rate, volume_ratio, amplitude,
    ma5, ma10, ma20, ma5_angle, industry,
    main_force_net, main_force_ratio, super_large_net, large_net, medium_net, small_net,
    avg_vol_5d, avg_vol_20d, pe_ttm, pb_ratio, volume, pe_dynamic)
VALUES ('2026-06-05', '000001', '平安银行', 12.5, 2.5,
    150000000000, 120000000000, 3.5, 1.2, 4.5,
    12.0, 11.8, 11.5, 25.0, '银行',
    50000000, 0.35, 20000000, 15000000, 10000000, 5000000,
    100000000, 80000000, 8.5, 1.2, 5000000, 8.2);

INSERT INTO stock_basic (trade_date, stock_code, stock_name, price, change_pct,
    total_market_cap, circ_market_cap, turnover_rate, volume_ratio, amplitude,
    ma5, ma10, ma20, ma5_angle, industry,
    main_force_net, main_force_ratio, super_large_net, large_net, medium_net, small_net,
    avg_vol_5d, avg_vol_20d, pe_ttm, pb_ratio, volume, pe_dynamic)
VALUES ('2026-06-04', '000001', '平安银行', 12.2, 1.5,
    148000000000, 118000000000, 2.8, 1.0, 3.8,
    11.9, 11.7, 11.4, 22.0, '银行',
    40000000, 0.30, 18000000, 12000000, 8000000, 4000000,
    90000000, 75000000, 8.3, 1.1, 4000000, 8.0);

INSERT INTO stock_basic (trade_date, stock_code, stock_name, price, change_pct,
    total_market_cap, circ_market_cap, turnover_rate, volume_ratio, amplitude,
    ma5, ma10, ma20, ma5_angle, industry,
    main_force_net, main_force_ratio, super_large_net, large_net, medium_net, small_net,
    avg_vol_5d, avg_vol_20d, pe_ttm, pb_ratio, volume, pe_dynamic)
VALUES ('2026-06-05', '999999', '测试A', 10.0, 0.5,
    10000000000, 8000000000, 1.0, 0.5, 2.0,
    9.9, 9.8, 9.7, 5.0, '测试',
    100000, 0.01, 50000, 30000, 15000, 5000,
    5000000, 4000000, 15.0, 1.0, 500000, 14.5);

INSERT INTO stock_indicators (trade_date, stock_code, ma60, ma120,
    bb_upper, bb_mid, bb_lower, bb_pct_b, bb_width,
    macd_dif, macd_dea, macd_bar,
    kdj_k, kdj_d, kdj_j,
    rsi6, rsi12, rsi24,
    bbi_daily, bbi_weekly)
VALUES ('2026-06-05', '000001',
    11.0, 10.5,
    13.5, 12.0, 10.5, 0.6, 0.25,
    0.5, 0.3, 0.2,
    70.0, 65.0, 80.0,
    65.0, 60.0, 55.0,
    12.2, 12.0);

INSERT INTO stock_indicators (trade_date, stock_code, ma60, ma120,
    bb_upper, bb_mid, bb_lower, bb_pct_b, bb_width,
    macd_dif, macd_dea, macd_bar,
    kdj_k, kdj_d, kdj_j,
    rsi6, rsi12, rsi24,
    bbi_daily, bbi_weekly)
VALUES ('2026-06-04', '000001',
    10.8, 10.3,
    13.2, 11.8, 10.3, 0.55, 0.24,
    0.4, 0.25, 0.15,
    68.0, 63.0, 78.0,
    62.0, 58.0, 53.0,
    12.0, 11.8);

INSERT INTO sector_info (sector_code, sector_name, need_collect)
VALUES ('BK01', '银行', 1);
INSERT INTO sector_info (sector_code, sector_name, need_collect)
VALUES ('BK02', '钢铁', 1);
INSERT INTO sector_info (sector_code, sector_name, need_collect)
VALUES ('GN01', '国企改革', 1);
INSERT INTO sector_info (sector_code, sector_name, need_collect)
VALUES ('GN02', '新能源', 1);

INSERT INTO sector_industry (trade_date, sector_code, sector_name, change_percent,
    up_count, main_force_net, super_large_net, top_stock, top_stock_change, latest_price)
VALUES ('2026-06-05', 'BK01', '银行', 2.0, 15, 100000000, 50000000, '000001', 2.5, 1200.0);

INSERT INTO sector_industry (trade_date, sector_code, sector_name, change_percent,
    up_count, main_force_net, super_large_net, top_stock, top_stock_change, latest_price)
VALUES ('2026-06-05', 'BK02', '钢铁', 1.0, 5, 20000000, 10000000, '600001', 1.0, 800.0);

INSERT INTO sector_concept (trade_date, sector_code, sector_name, change_percent,
    up_count, main_force_net, super_large_net)
VALUES ('2026-06-05', 'GN01', '国企改革', 1.5, 8, 30000000, 15000000);

INSERT INTO sector_concept (trade_date, sector_code, sector_name, change_percent,
    up_count, main_force_net, super_large_net)
VALUES ('2026-06-05', 'GN02', '新能源', 0.8, 3, 10000000, 5000000);

INSERT INTO sector_stocks (sector_code, stock_code)
VALUES ('BK01', '000001');
INSERT INTO sector_stocks (sector_code, stock_code)
VALUES ('BK02', '999999');
INSERT INTO sector_stocks (sector_code, stock_code)
VALUES ('GN01', '000001');

INSERT INTO limit_pool (trade_date, stock_code, stock_name, pool_type,
    consecutive_boards, first_seal_time, seal_amount, zt_stat, industry, open_count)
VALUES ('2026-06-05', '000001', '平安银行', '涨停',
    2, '09:30', 50000000, '1/1', '银行', 0);
"""


def _create_reader_tables(conn):
    """Create all tables needed by the readers (idempotent)."""
    conn.executescript(_READER_TABLES_SQL)
    conn.commit()


def _populate_test_data(db_path):
    """Create tables + insert minimal test data into DB at db_path."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _create_reader_tables(conn)
    conn.executescript(_TEST_DATA_SQL)
    conn.commit()
    conn.close()


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def db_path():
    """Override conftest db_path: fresh temp file, no trade tables."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def conn(db_path):
    """Connection with populated test data for reader tests."""
    _populate_test_data(db_path)
    _conn = sqlite3.connect(db_path)
    _conn.row_factory = sqlite3.Row
    yield _conn
    _conn.close()


@pytest.fixture
def empty_db(db_path):
    """Connection with reader tables created but no data inserted."""
    _conn = sqlite3.connect(db_path)
    _conn.row_factory = sqlite3.Row
    _create_reader_tables(_conn)
    yield _conn
    _conn.close()


# ── StockReader tests ────────────────────────────────────────────────


class TestStockReader:
    """StockReader: get_daily_indicators, get_money_flow, get_stock_basic,
    get_recent_prices, get_stock_name.
    """

    # ── get_daily_indicators ──

    def test_get_daily_indicators_success(self, conn):
        result = StockReader.get_daily_indicators(conn, "000001")
        assert result is not None
        assert isinstance(result, dict)

        expected_keys = {
            "ma5",
            "ma10",
            "ma20",
            "ma60",
            "ma120",
            "bb_upper",
            "bb_mid",
            "bb_lower",
            "bb_pct_b",
            "bb_width",
            "macd_dif",
            "macd_dea",
            "macd_bar",
            "kdj_k",
            "kdj_d",
            "kdj_j",
            "rsi6",
            "rsi12",
            "rsi24",
            "bbi_daily",
            "bbi_weekly",
        }
        assert set(result.keys()) == expected_keys

        assert result["ma5"] == 12.0
        assert result["ma60"] == 11.0
        assert result["bb_upper"] == 13.5
        assert result["bb_pct_b"] == 0.6
        assert result["macd_dif"] == 0.5
        assert result["kdj_k"] == 70.0
        assert result["rsi6"] == 65.0
        assert result["bbi_daily"] == 12.2

    def test_get_daily_indicators_unknown_code(self, conn):
        result = StockReader.get_daily_indicators(conn, "UNKNOWN")
        assert result is None

    # ── get_money_flow ──

    def test_get_money_flow_success(self, conn):
        result = StockReader.get_money_flow(conn, "000001")
        assert result is not None
        assert isinstance(result, dict)

        expected_keys = {
            "main_force_net",
            "main_force_ratio",
            "super_large_net",
            "large_net",
            "ma5_angle",
            "pe_dynamic",
            "circ_market_cap",
        }
        assert set(result.keys()) == expected_keys

        assert result["main_force_net"] == 50000000
        assert result["main_force_ratio"] == 0.35
        assert result["super_large_net"] == 20000000
        assert result["large_net"] == 15000000
        assert result["ma5_angle"] == 25.0
        assert result["pe_dynamic"] == 8.2
        assert result["circ_market_cap"] == 120000000000

    def test_get_money_flow_no_data(self, empty_db):
        result = StockReader.get_money_flow(empty_db, "NO_DATA")
        assert result is None

    # ── get_stock_basic ──

    def test_get_stock_basic_success(self, conn):
        result = StockReader.get_stock_basic(conn, "000001")
        assert result is not None
        assert isinstance(result, dict)
        assert result["stock_code"] == "000001"
        assert result["stock_name"] == "平安银行"
        assert result["price"] == 12.5
        assert result["industry"] == "银行"
        assert result["pe_ttm"] == 8.5

    def test_get_stock_basic_unknown_code(self, empty_db):
        result = StockReader.get_stock_basic(empty_db, "NO_DATA")
        assert result is None

    # ── get_recent_prices ──

    def test_get_recent_prices_success(self, conn):
        result = StockReader.get_recent_prices(conn, "000001")
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0] == 12.5  # most recent first
        assert result[1] == 12.2

    def test_get_recent_prices_limit(self, conn):
        result = StockReader.get_recent_prices(conn, "000001", limit=1)
        assert len(result) == 1
        assert result[0] == 12.5

    def test_get_recent_prices_no_data(self, empty_db):
        result = StockReader.get_recent_prices(empty_db, "NO_DATA")
        assert result == []

    # ── get_stock_name ──

    def test_get_stock_name_success(self, conn):
        result = StockReader.get_stock_name(conn, "000001")
        assert result == "平安银行"

    def test_get_stock_name_unknown(self, empty_db):
        result = StockReader.get_stock_name(empty_db, "NO_DATA")
        assert result is None


# ── SectorReader tests ───────────────────────────────────────────────


class TestSectorReader:
    """SectorReader: get_sector_stats, get_sector_stocks, get_sector_change,
    get_concept_stats, get_industry_sectors / get_concept_sectors.
    """

    # ── get_sector_stats ──

    def test_get_sector_stats_valid_date(self, conn):
        result = SectorReader.get_sector_stats(conn, "2026-06-05")
        assert result is not None
        assert isinstance(result, dict)
        assert "银行" in result
        assert "钢铁" in result
        assert result["银行"]["change"] == 2.0
        assert result["银行"]["up_count"] == 15
        assert result["银行"]["main_force_net"] == 100000000

    def test_get_sector_stats_empty(self, empty_db):
        result = SectorReader.get_sector_stats(empty_db, "2026-06-05")
        assert result is None

    # ── get_sector_stocks ──

    def test_get_sector_stocks_valid(self, conn):
        result = SectorReader.get_sector_stocks(
            conn,
            trade_date="2026-06-05",
            day_before="2026-06-03",
            yesterday="2026-06-04",
            sector_codes=["BK01"],
            sector_table="sector_industry",
            label="行业",
        )
        assert isinstance(result, list)
        assert len(result) >= 1
        assert result[0]["sector"] == "银行"
        assert result[0]["label"] == "行业"
        assert len(result[0]["stocks"]) > 0
        assert result[0]["stocks"][0]["code"] == "000001"

    def test_get_sector_stocks_multiple_sectors(self, conn):
        result = SectorReader.get_sector_stocks(
            conn,
            trade_date="2026-06-05",
            day_before="2026-06-03",
            yesterday="2026-06-04",
            sector_codes=["BK01", "BK02"],
            sector_table="sector_industry",
            label="行业",
        )
        assert isinstance(result, list)
        sector_names = {r["sector"] for r in result}
        assert "银行" in sector_names
        assert "钢铁" in sector_names

    def test_get_sector_stocks_empty(self, empty_db):
        result = SectorReader.get_sector_stocks(
            empty_db,
            trade_date="2026-06-05",
            day_before="2026-06-03",
            yesterday="2026-06-04",
            sector_codes=["BK01"],
            sector_table="sector_industry",
            label="行业",
        )
        assert result == []

    # ── get_sector_change ──

    def test_get_sector_change_valid(self, conn):
        result = SectorReader.get_sector_change(conn, "BK01", "2026-06-05")
        assert result == 2.0

    def test_get_sector_change_no_data(self, empty_db):
        result = SectorReader.get_sector_change(empty_db, "BK01", "2026-06-05")
        assert result is None

    # ── get_concept_stats ──

    def test_get_concept_stats_valid_date(self, conn):
        result = SectorReader.get_concept_stats(conn, "2026-06-05")
        assert result is not None
        assert isinstance(result, dict)
        assert "国企改革" in result
        assert "新能源" in result
        assert result["国企改革"]["change"] == 1.5
        assert result["国企改革"]["up_count"] == 8

    def test_get_concept_stats_empty(self, empty_db):
        result = SectorReader.get_concept_stats(empty_db, "2026-06-05")
        assert result is None

    # ── get_industry_sectors (core existing method) ──

    def test_get_industry_sectors_valid(self, conn):
        sectors, fund_map = SectorReader.get_industry_sectors(conn, "2026-06-05")
        assert isinstance(sectors, list)
        assert len(sectors) >= 2
        sector_names = {s["name"] for s in sectors}
        assert "银行" in sector_names
        assert "钢铁" in sector_names
        for s in sectors:
            assert "change" in s
            assert "main_force_net" in s
            assert "super_large_net" in s
        assert isinstance(fund_map, dict)
        assert "银行" in fund_map

    def test_get_industry_sectors_empty(self, empty_db):
        sectors, fund_map = SectorReader.get_industry_sectors(empty_db, "2026-06-05")
        assert sectors == []
        assert fund_map == {}

    # ── get_concept_sectors (core existing method) ──

    def test_get_concept_sectors_valid(self, conn):
        sectors, fund_map = SectorReader.get_concept_sectors(conn, "2026-06-05")
        assert isinstance(sectors, list)
        assert len(sectors) >= 1
        concept_names = {s["name"] for s in sectors}
        assert "国企改革" in concept_names
        assert isinstance(fund_map, dict)
        assert "国企改革" in fund_map

    def test_get_concept_sectors_empty(self, empty_db):
        sectors, fund_map = SectorReader.get_concept_sectors(empty_db, "2026-06-05")
        assert sectors == []
        assert fund_map == {}


# ── LimitPoolReader tests ────────────────────────────────────────────


class TestLimitPoolReader:
    """LimitPoolReader: get_limit_pool."""

    def test_get_limit_pool_valid_date(self, conn):
        result = LimitPoolReader.get_limit_pool(conn, "2026-06-05")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["stock_code"] == "000001"
        assert result[0]["stock_name"] == "平安银行"
        assert result[0]["pool_type"] == "涨停"
        assert result[0]["consecutive_boards"] == 2

    def test_get_limit_pool_no_data(self, empty_db):
        result = LimitPoolReader.get_limit_pool(empty_db, "2026-06-05")
        assert result == []
