"""tests for analysis/stock module: analyzers, formatter, registry, schemas."""

import copy
import sqlite3

from stock.analyzers import BaseAnalyzer
from stock.analyzers.money_flow import MoneyFlowAnalyzer
from stock.analyzers.sector_attr import SectorAttrAnalyzer
from stock.analyzers.technical import TechnicalAnalyzer
from stock.stock_formatter import to_cli, to_dict, to_telegram
from stock.stock_registry import _registry, get, get_many, list_all, register
from stock.stock_schemas import (
    AnalysisResult,
    StockAnalysisReport,
    StockAnalysisRequest,
)

# ── 测试 DB 辅助函数 ──


def _ensure_stock_basic_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_basic (
            stock_code TEXT,
            stock_name TEXT,
            trade_date TEXT,
            ma5 REAL,
            ma10 REAL,
            ma20 REAL,
            ma60 REAL,
            main_force_net REAL,
            main_force_ratio REAL,
            super_large_net REAL,
            large_net REAL,
            ma5_angle REAL,
            pe_dynamic REAL,
            circ_market_cap REAL,
            industry TEXT,
            concepts TEXT,
            price REAL,
            change_pct REAL
        )
    """)
    conn.commit()


def _ensure_stock_indicators_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stock_indicators (
            stock_code TEXT,
            trade_date TEXT,
            ma60 REAL,
            ma120 REAL,
            bb_upper REAL,
            bb_mid REAL,
            bb_lower REAL,
            bb_pct_b REAL,
            bb_width REAL,
            macd_dif REAL,
            macd_dea REAL,
            macd_bar REAL,
            kdj_k REAL,
            kdj_d REAL,
            kdj_j REAL,
            rsi6 REAL,
            rsi12 REAL,
            rsi24 REAL,
            bbi_daily REAL,
            bbi_weekly REAL
        )
    """)
    conn.commit()


def _insert_stock_basic(conn, **overrides):
    defaults = dict(
        stock_code="002371",
        stock_name="北方华创",
        trade_date="2026-06-05",
        ma5=105.0,
        ma10=100.0,
        ma20=95.0,
        ma60=90.0,
        main_force_net=50_000_000,
        main_force_ratio=6.0,
        super_large_net=30_000_000,
        large_net=20_000_000,
        ma5_angle=5.0,
        pe_dynamic=30.0,
        circ_market_cap=500e8,
        industry="半导体",
        concepts="芯片,国产替代,半导体设备",
        price=100.0,
        change_pct=3.5,
    )
    defaults.update(overrides)
    cols = ", ".join(defaults)
    ph = ", ".join(["?"] * len(defaults))
    conn.execute(
        f"INSERT INTO stock_basic ({cols}) VALUES ({ph})", list(defaults.values())
    )
    conn.commit()


def _insert_stock_indicators(conn, **overrides):
    defaults = dict(
        stock_code="002371",
        trade_date="2026-06-05",
        ma60=90.0,
        ma120=85.0,
        bb_upper=110.0,
        bb_mid=100.0,
        bb_lower=90.0,
        bb_pct_b=50.0,
        bb_width=20.0,
        macd_dif=2.5,
        macd_dea=1.8,
        macd_bar=0.7,
        kdj_k=60.0,
        kdj_d=55.0,
        kdj_j=70.0,
        rsi6=55.0,
        rsi12=50.0,
        rsi24=48.0,
        bbi_daily=98.0,
        bbi_weekly=95.0,
    )
    defaults.update(overrides)
    cols = ", ".join(defaults)
    ph = ", ".join(["?"] * len(defaults))
    conn.execute(
        f"INSERT INTO stock_indicators ({cols}) VALUES ({ph})", list(defaults.values())
    )
    conn.commit()


def _build_indicator_db(**indicator_overrides) -> str:
    """创建含 stock_basic + stock_indicators 的临时 DB，返回路径。"""
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    _ensure_stock_basic_table(conn)
    _ensure_stock_indicators_table(conn)
    _insert_stock_basic(conn)
    _insert_stock_indicators(conn, **indicator_overrides)
    conn.close()
    return path


def _build_money_flow_db(**basic_overrides) -> str:
    """创建含 stock_basic 的临时 DB，返回路径。"""
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    _ensure_stock_basic_table(conn)
    _insert_stock_basic(conn, **basic_overrides)
    conn.close()
    return path


# ── TechnicalAnalyzer ──


class TestTechnicalAnalyzer:
    """TechnicalAnalyzer 技术面分析器测试。"""

    def make_analyzer(self):
        return TechnicalAnalyzer()

    def _analyze_from_db(self, **indicator_overrides) -> AnalysisResult:
        a = self.make_analyzer()
        db = _build_indicator_db(**indicator_overrides)
        try:
            return a.analyze("002371", db_path=db)
        finally:
            import os

            os.unlink(db)

    # ── 正向场景 ──

    def test_analyze_bullish(self):
        """均线多头排列 + MACD 多头 + 正常 RSI/KDJ → ok=True。"""
        result = self._analyze_from_db(
            ma60=85.0,
            ma120=80.0,
            macd_dif=2.5,
            macd_dea=1.8,
            macd_bar=0.7,
            kdj_k=60,
            kdj_d=55,
            kdj_j=70,
            rsi6=55,
            rsi12=50,
            bb_pct_b=50,
        )
        assert result.ok is True
        concats = " ".join(result.conclusions)
        assert "均线多头排列" in concats
        assert "MACD日线多头" in concats
        assert "MA20在MA60上方" in concats
        assert result.dimension == "technical"

    def test_analyze_bearish(self):
        """均线空头排列 + MACD 空头 → ok=False + risk_flags。"""
        # 需要在创建 DB 时就设好 bearish 数据
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        _ensure_stock_basic_table(conn)
        _ensure_stock_indicators_table(conn)
        _insert_stock_basic(conn, ma5=90, ma10=95, ma20=100)
        _insert_stock_indicators(conn, macd_dif=1.0, macd_dea=2.0, macd_bar=-1.0)
        conn.close()
        try:
            a = self.make_analyzer()
            result = a.analyze("002371", db_path=path)
        finally:
            import os

            os.unlink(path)

        assert result.ok is False
        concat_c = " ".join(result.conclusions)
        concat_r = " ".join(result.risk_flags)
        assert "均线空头排列" in concat_c
        assert "均线空头排列" in concat_r
        assert "MACD日线空头" in concat_c
        assert "MACD日线空头" in concat_r

    # ── 均线场景 ──

    def test_ma_intertwined(self):
        """ma5>ma10 但 ma10<ma20 → 均线交织。"""
        db = _build_indicator_db()
        try:
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE stock_basic SET ma5=100, ma10=95, ma20=98 WHERE stock_code='002371'"
            )
            conn.commit()
            conn.close()
            a = self.make_analyzer()
            result = a.analyze("002371", db_path=db)
        finally:
            import os

            os.unlink(db)

        concat = " ".join(result.conclusions)
        assert "均线交织" in concat

    def test_ma20_below_ma60(self):
        """MA20 < MA60 → risk_flag。"""
        result = self._analyze_from_db(
            ma60=120.0,
        )
        concat = " ".join(result.risk_flags)
        assert "MA20下穿MA60" in concat
        # ma20（来自 stock_basic）=95, ma60（来自 stock_indicators）=120
        # 注意：TechnicalAnalyzer 的判断是 ma20 > ma60 → 看代码第52行
        # ind.get("ma20", 0) or 0 从 ind 取的是 stock_basic 的 ma20
        # ind.get("ma60", 0) or 0 从 stock_indicators 的 ma60

    # ── RSI 场景 ──

    def test_rsi6_overbought(self):
        """RSI6 > 80 → risk_flag 超买。"""
        result = self._analyze_from_db(rsi6=85.0)
        concat = " ".join(result.risk_flags)
        assert "RSI6超买" in concat

    def test_rsi6_oversold(self):
        """RSI6 < 30 → 结论提示超卖。"""
        result = self._analyze_from_db(rsi6=25.0)
        concat = " ".join(result.conclusions)
        assert "RSI6超卖" in concat

    def test_rsi12_high(self):
        """RSI12 > 70 → risk_flag。"""
        result = self._analyze_from_db(rsi12=75.0)
        concat = " ".join(result.risk_flags)
        assert "RSI12偏高" in concat

    # ── KDJ 场景 ──

    def test_kdj_overbought(self):
        """KDJ J > 100 → risk_flag。"""
        result = self._analyze_from_db(kdj_j=120.0)
        concat = " ".join(result.risk_flags)
        assert "KDJ极度超买" in concat

    def test_kdj_oversold(self):
        """KDJ J < 0 → 结论提示反弹。"""
        result = self._analyze_from_db(kdj_j=-5.0)
        concat = " ".join(result.conclusions)
        assert "KDJ极度超卖" in concat

    # ── 布林带场景 ──

    def test_bb_upper_run(self):
        """%B > 90 → risk_flag。"""
        result = self._analyze_from_db(bb_pct_b=95.0)
        concat = " ".join(result.risk_flags)
        assert "布林带上轨运行" in concat

    def test_bb_lower_run(self):
        """%B < 10 → 结论提示超卖。"""
        result = self._analyze_from_db(bb_pct_b=5.0)
        concat = " ".join(result.conclusions)
        assert "布林带下轨运行" in concat

    # ── 异常场景 ──

    def test_no_indicator_data(self):
        """stock_indicators 无数据（两表都存在但 JOIN 为空）→ ok=False。"""
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        _ensure_stock_basic_table(conn)
        _ensure_stock_indicators_table(conn)
        conn.close()

        a = self.make_analyzer()
        result = a.analyze("002371", db_path=path)
        assert result.ok is False
        assert "无技术指标数据" in result.conclusions
        assert "数据缺失" in result.risk_flags
        import os

        os.unlink(path)

    def test_db_connection_error(self):
        """无效 DB 路径 → 错误处理。"""
        a = self.make_analyzer()
        result = a.analyze("002371", db_path="/nonexistent/db/test.db")
        assert result.ok is False
        assert "数据获取失败" in result.risk_flags
        assert result.error != ""

    def test_empty_stock_basic(self):
        """stock_basic 无对应数据但 stock_indicators 有 → 仍视为有数据。"""
        # 实际上 get_daily_indicators 是 JOIN, 两边都要有
        # 测试 JOIN 不匹配时返回 None
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        _ensure_stock_indicators_table(conn)
        _ensure_stock_basic_table(conn)
        _insert_stock_indicators(conn, stock_code="999999")
        conn.close()

        a = self.make_analyzer()
        result = a.analyze("002371", db_path=path)
        assert result.ok is False
        assert "无技术指标数据" in result.conclusions
        import os

        os.unlink(path)


# ── MoneyFlowAnalyzer ──


class TestMoneyFlowAnalyzer:
    """MoneyFlowAnalyzer 资金面分析器测试。"""

    def make_analyzer(self):
        return MoneyFlowAnalyzer()

    def _analyze_from_db(self, **basic_overrides) -> AnalysisResult:
        a = self.make_analyzer()
        db = _build_money_flow_db(**basic_overrides)
        try:
            return a.analyze("002371", db_path=db)
        finally:
            import os

            os.unlink(db)

    # ── 主力资金方向 ──

    def test_strong_inflow(self):
        """主力净流入 + 占比 > 5% → '大幅流入'。"""
        result = self._analyze_from_db(
            main_force_net=100_000_000,
            main_force_ratio=8.0,
        )
        concat = " ".join(result.conclusions)
        assert "主力大幅流入" in concat
        assert result.ok is True

    def test_moderate_inflow(self):
        """主力净流入 + 占比 2-5% → '温和流入'。"""
        result = self._analyze_from_db(
            main_force_net=30_000_000,
            main_force_ratio=3.5,
        )
        concat = " ".join(result.conclusions)
        assert "主力温和流入" in concat

    def test_weak_inflow(self):
        """主力净流入 + 占比 < 2% → '小幅流入'。"""
        result = self._analyze_from_db(
            main_force_net=5_000_000,
            main_force_ratio=0.5,
        )
        concat = " ".join(result.conclusions)
        assert "主力小幅流入" in concat

    def test_strong_outflow(self):
        """主力净流出 + 占比 < -5% → risk_flag。"""
        result = self._analyze_from_db(
            main_force_net=-80_000_000,
            main_force_ratio=-6.0,
        )
        concat_r = " ".join(result.risk_flags)
        concat_c = " ".join(result.conclusions)
        assert "主力大幅流出" in concat_r
        assert "主力净流出" in concat_c
        assert result.ok is False

    def test_moderate_outflow(self):
        """主力净流出 + 占比 -2~-5% → risk_flag 温和流出。"""
        result = self._analyze_from_db(
            main_force_net=-40_000_000,
            main_force_ratio=-3.0,
        )
        concat_r = " ".join(result.risk_flags)
        concat_c = " ".join(result.conclusions)
        assert "主力温和流出" in concat_r
        assert "主力净流出" in concat_c

    def test_balanced(self):
        """主力净流入为零 → '基本平衡'。"""
        result = self._analyze_from_db(
            main_force_net=0,
            main_force_ratio=0,
        )
        concat = " ".join(result.conclusions)
        assert "主力资金基本平衡" in concat
        assert result.ok is True

    # ── 超大单/大单组合 ──

    def test_both_inflow(self):
        """超大单+大单同步流入 → 机构建仓信号。"""
        result = self._analyze_from_db(
            super_large_net=30_000_000,
            large_net=20_000_000,
        )
        concat = " ".join(result.conclusions)
        assert "机构建仓信号" in concat

    def test_sl_in_lg_out(self):
        """超大单流入+大单流出 → 机构对倒。"""
        result = self._analyze_from_db(
            super_large_net=30_000_000,
            large_net=-10_000_000,
        )
        concat = " ".join(result.conclusions)
        assert "机构对倒" in concat

    def test_sl_out_lg_in(self):
        """超大单流出+大单流入 → 游资接盘机构出货。"""
        result = self._analyze_from_db(
            super_large_net=-20_000_000,
            large_net=15_000_000,
        )
        concat = " ".join(result.conclusions)
        assert "游资接盘机构出货" in concat

    # ── MA5 斜率 ──

    def test_ma5_angle_up(self):
        """MA5 斜率 > 2 → 资金推动向上。"""
        result = self._analyze_from_db(ma5_angle=8.0)
        concat = " ".join(result.conclusions)
        assert "MA5加速上行" in concat

    def test_ma5_angle_down(self):
        """MA5 斜率 < -2 → risk_flag 资金出逃。"""
        result = self._analyze_from_db(ma5_angle=-5.0)
        concat = " ".join(result.risk_flags)
        assert "MA5加速下行" in concat
        assert result.ok is False

    # ── 市值分类 ──

    def test_large_cap(self):
        """流通市值 > 1000亿 → 大盘股。"""
        result = self._analyze_from_db(circ_market_cap=2000e8)
        concat = " ".join(result.conclusions)
        assert "大盘股" in concat

    def test_mid_cap(self):
        """流通市值 100-1000亿 → 中盘股。"""
        result = self._analyze_from_db(circ_market_cap=300e8)
        concat = " ".join(result.conclusions)
        assert "中盘股" in concat

    def test_small_cap(self):
        """流通市值 < 100亿 → 小盘股。"""
        result = self._analyze_from_db(circ_market_cap=50e8)
        concat = " ".join(result.conclusions)
        assert "小盘股" in concat

    # ── 异常场景 ──

    def test_no_data(self):
        """stock_basic 无对应数据 → ok=False。"""
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        _ensure_stock_basic_table(conn)
        # 不插入数据
        conn.close()

        a = self.make_analyzer()
        result = a.analyze("002371", db_path=path)
        assert result.ok is False
        assert "无资金流数据" in result.conclusions
        import os

        os.unlink(path)

    def test_db_error(self):
        """数据库错误 → 错误处理。"""
        a = self.make_analyzer()
        result = a.analyze("002371", db_path="/nonexistent/test.db")
        assert result.ok is False
        assert "数据获取失败" in result.risk_flags
        assert result.error != ""

    def test_zero_net_not_zero_ratio(self):
        """net=0 但 ratio 非零 → 仍判平衡。"""
        result = self._analyze_from_db(
            main_force_net=0,
            main_force_ratio=3.0,
        )
        concat = " ".join(result.conclusions)
        assert "主力资金基本平衡" in concat


# ── SectorAttrAnalyzer ──


class TestSectorAttrAnalyzer:
    """SectorAttrAnalyzer 板块归因分析器测试。"""

    def make_analyzer(self):
        return SectorAttrAnalyzer()

    def _analyze_from_db(self, **basic_overrides) -> AnalysisResult:
        a = self.make_analyzer()
        db = _build_money_flow_db(**basic_overrides)
        try:
            return a.analyze("002371", db_path=db)
        finally:
            import os

            os.unlink(db)

    def test_normal_industry_and_concepts(self):
        """有行业 + ≤3 个概念。"""
        result = self._analyze_from_db(
            industry="半导体",
            concepts="芯片,国产替代",
        )
        concat = " ".join(result.conclusions)
        assert "所属行业：半导体" in concat
        assert "概念板块：" in concat
        assert "芯片" in concat
        assert "国产替代" in concat
        assert result.ok is True

    def test_many_concepts(self):
        """≥5 个概念 → '概念标签多'。"""
        result = self._analyze_from_db(
            concepts="芯片,国产替代,半导体设备,光刻机,先进封装",
        )
        concat = " ".join(result.conclusions)
        assert "概念标签多" in concat
        assert "概念覆盖广" in concat

    def test_four_concepts(self):
        """4 个概念 → 只展示 3 个 + 等 N 个，但不触发概念标签多。"""
        result = self._analyze_from_db(
            concepts="芯片,国产替代,半导体设备,光刻机",
        )
        concat = " ".join(result.conclusions)
        assert "等4个" in concat
        assert "概念覆盖广" in concat
        assert "概念标签多" not in concat

    def test_single_concept(self):
        """1 个概念 → '概念单一' risk_flag。"""
        result = self._analyze_from_db(
            concepts="芯片",
        )
        concat_r = " ".join(result.risk_flags)
        assert "概念单一" in concat_r
        # 检查结论里列出了概念
        concat_c = " ".join(result.conclusions)
        assert "概念板块" in concat_c

    def test_no_industry(self):
        """行业为空 → risk_flag。"""
        result = self._analyze_from_db(industry="", concepts="芯片")
        concat = " ".join(result.risk_flags)
        assert "无行业分类" in concat
        assert result.ok is False

    def test_no_concepts(self):
        """概念为空 → risk_flag。"""
        result = self._analyze_from_db(concepts="")
        concat = " ".join(result.risk_flags)
        assert "无概念板块归属" in concat
        assert result.ok is False

    def test_pipe_delimited_concepts(self):
        """竖线分隔的概念。"""
        result = self._analyze_from_db(concepts="芯片|国产替代|半导体设备")
        concat = " ".join(result.conclusions)
        for c in ("芯片", "国产替代", "半导体设备"):
            assert c in concat

    def test_no_data(self):
        """数据库无该股票记录。"""
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        _ensure_stock_basic_table(conn)
        conn.close()

        a = self.make_analyzer()
        result = a.analyze("002371", db_path=path)
        assert result.ok is False
        assert "无板块数据" in result.conclusions
        import os

        os.unlink(path)

    def test_db_error(self):
        """数据库错误。"""
        a = self.make_analyzer()
        result = a.analyze("002371", db_path="/nonexistent/test.db")
        assert result.ok is False
        assert "数据获取失败" in result.risk_flags


# ── formatter ──


class TestFormatter:
    """格式化函数测试。"""

    def _make_report(self, results=None, aggregated=None):
        if results is None:
            results = [
                AnalysisResult(
                    dimension="technical",
                    ok=True,
                    data={},
                    conclusions=["均线多头排列", "MACD多头"],
                    risk_flags=[],
                ),
                AnalysisResult(
                    dimension="money_flow",
                    ok=False,
                    data={},
                    conclusions=["主力净流出"],
                    risk_flags=["主力大幅流出"],
                ),
            ]
        if aggregated is None:
            aggregated = {
                "ok": False,
                "total_risks": 1,
                "risk_summary": ["主力大幅流出"],
            }
        return StockAnalysisReport(
            symbol="002371",
            name="北方华创",
            results=results,
            aggregated=aggregated,
        )

    # ── to_cli ──

    def test_to_cli_normal(self):
        """正常报告 → 包含所有 section。"""
        report = self._make_report()
        output = to_cli(report)
        assert "002371" in output
        assert "北方华创" in output
        assert "[technical] ✅" in output
        assert "均线多头排列" in output
        assert "[money_flow] ⚠️" in output
        assert "主力净流出" in output
        assert "综合" in output

    def test_to_cli_with_errors(self):
        """含错误维度的报告。"""
        report = self._make_report(
            [
                AnalysisResult(
                    dimension="technical",
                    ok=False,
                    data={},
                    conclusions=[],
                    risk_flags=[],
                    error="数据库连接失败",
                ),
            ]
        )
        output = to_cli(report)
        assert "[technical] ❌ 数据库连接失败" in output

    def test_to_cli_empty_results(self):
        """无结果 → 仍返回非空字符串。"""
        report = self._make_report(results=[])
        output = to_cli(report)
        assert output != ""
        assert "002371" in output

    # ── to_telegram ──

    def test_to_telegram_normal(self):
        """正常报告 → 紧凑格式。"""
        report = self._make_report()
        output = to_telegram(report)
        assert "📊 002371 北方华创" in output
        assert "✅ technical" in output
        assert "均线多头排列" in output

    def test_to_telegram_truncated(self):
        """超长结论 → 截断。"""
        long_conclusions = ["很长很长的结论文本" + str(i) for i in range(50)]
        report = self._make_report(
            [
                AnalysisResult(
                    dimension="technical",
                    ok=True,
                    data={},
                    conclusions=long_conclusions,
                    risk_flags=long_conclusions,
                ),
            ]
        )
        output = to_telegram(report, max_len=30)
        assert len(output) <= 30
        assert output.endswith("...")

    def test_to_telegram_with_errors(self):
        """含错误的维度。"""
        report = self._make_report(
            [
                AnalysisResult(
                    dimension="sector_attr",
                    ok=False,
                    data={},
                    conclusions=[],
                    risk_flags=[],
                    error="查询失败",
                ),
            ]
        )
        output = to_telegram(report)
        assert "❌ 查询失败" in output

    def test_to_telegram_no_aggregated(self):
        """无聚合数据仍正常输出。"""
        report = StockAnalysisReport(
            symbol="600519",
            name="贵州茅台",
            results=[],
            aggregated={},
        )
        output = to_telegram(report)
        assert "600519 贵州茅台" in output

    # ── to_dict ──

    def test_to_dict_normal(self):
        """正常报告 → 结构完整的 dict。"""
        report = self._make_report()
        d = to_dict(report)
        assert d["symbol"] == "002371"
        assert d["name"] == "北方华创"
        assert len(d["results"]) == 2
        assert d["results"][0]["dimension"] == "technical"
        assert d["results"][0]["ok"] is True
        assert d["aggregated"]["total_risks"] == 1

    def test_to_dict_empty(self):
        """空报告。"""
        report = StockAnalysisReport(
            symbol="600519",
            name="",
            results=[],
            aggregated={},
        )
        d = to_dict(report)
        assert d["symbol"] == "600519"
        assert d["results"] == []


# ── registry ──


class TestRegistry:
    """分析器注册表测试。"""

    def _save_state(self):
        return copy.copy(_registry)

    def _restore_state(self, saved):
        _registry.clear()
        _registry.update(saved)

    def _ensure_builtins(self):
        """注册内置分析器（如果尚未注册）。"""
        if "technical" not in _registry:
            _registry["technical"] = TechnicalAnalyzer()
            _registry["money_flow"] = MoneyFlowAnalyzer()
            _registry["sector_attr"] = SectorAttrAnalyzer()

    def test_register_and_get(self):
        """注册后能获取。"""
        saved = self._save_state()
        try:
            _registry.clear()
            a = TechnicalAnalyzer()
            register(a)
            assert get("technical") is a
        finally:
            self._restore_state(saved)

    def test_get_unknown(self):
        """不存在的名称 → None。"""
        assert get("nonexistent_analyzer") is None

    def test_list_all(self):
        """列出已注册维度，至少包含内置的 3 个。"""
        saved = self._save_state()
        try:
            self._ensure_builtins()
            names = list_all()
            assert "technical" in names
            assert "money_flow" in names
            assert "sector_attr" in names
        finally:
            self._restore_state(saved)

    def test_get_many(self):
        """批量获取。"""
        saved = self._save_state()
        try:
            self._ensure_builtins()
            names = list_all()
            analyzers = get_many(names)
            assert len(analyzers) == len(names)
        finally:
            self._restore_state(saved)

    def test_get_many_partial(self):
        """部分名称不存在 → 只返回存在的。"""
        saved = self._save_state()
        try:
            self._ensure_builtins()
            result = get_many(["technical", "bogus"])
            assert len(result) == 1
            assert result[0].name == "technical"
        finally:
            self._restore_state(saved)

    def test_get_many_empty(self):
        """空列表 → 空列表。"""
        saved = self._save_state()
        try:
            assert get_many([]) == []
        finally:
            self._restore_state(saved)


# ── schemas ──


class TestSchemas:
    """数据结构定义测试。"""

    def test_stock_analysis_request_defaults(self):
        """StockAnalysisRequest 默认值正确。"""
        req = StockAnalysisRequest(symbol="600519", dimensions=["technical"])
        assert req.symbol == "600519"
        assert req.dimensions == ["technical"]
        assert req.params == {}

    def test_stock_analysis_request_with_params(self):
        """StockAnalysisRequest 带参数。"""
        req = StockAnalysisRequest(
            symbol="002371",
            dimensions=["technical", "money_flow"],
            params={"days": 60},
        )
        assert req.params == {"days": 60}

    def test_analysis_result_defaults(self):
        """AnalysisResult error 默认空字符串。"""
        r = AnalysisResult(
            dimension="test",
            ok=True,
            data={},
            conclusions=["ok"],
            risk_flags=[],
        )
        assert r.error == ""

    def test_analysis_result_full(self):
        """AnalysisResult 所有字段。"""
        r = AnalysisResult(
            dimension="technical",
            ok=False,
            data={"key": "val"},
            conclusions=["结论1"],
            risk_flags=["风险1"],
            error="err",
        )
        assert r.dimension == "technical"
        assert r.error == "err"

    def test_stock_analysis_report_defaults(self):
        """StockAnalysisReport aggregated 默认空 dict。"""
        report = StockAnalysisReport(symbol="600519", name="茅台", results=[])
        assert report.aggregated == {}

    def test_stock_analysis_report_full(self):
        """StockAnalysisReport 所有字段。"""
        results = [
            AnalysisResult(
                dimension="t", ok=True, data={}, conclusions=[], risk_flags=[]
            )
        ]
        report = StockAnalysisReport(
            symbol="002371",
            name="北方华创",
            results=results,
            aggregated={"score": 80},
        )
        assert report.aggregated == {"score": 80}


# ── StockAnalyzer（from __init__.py）──


class TestStockAnalyzer:
    """StockAnalyzer 统一入口测试。"""

    def _build_report_db(self) -> str:
        """创建含 stock_basic 完整数据的临时 DB。"""
        import tempfile

        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        _ensure_stock_basic_table(conn)
        _ensure_stock_indicators_table(conn)
        _insert_stock_basic(conn)
        _insert_stock_indicators(conn)
        conn.close()
        return path

    def test_quick_returns_report(self, monkeypatch):
        """quick() 返回 StockAnalysisReport。"""
        from stock import StockAnalyzer

        db = self._build_report_db()
        monkeypatch.setattr(
            "stock.StockAnalyzer._resolve_name", lambda self, code: code
        )
        monkeypatch.setattr("system.config.settings.DATABASE_PATH", db)
        try:
            analyzer = StockAnalyzer(dimensions=["technical"])
            report = analyzer.quick("002371")
            assert isinstance(report, StockAnalysisReport)
            assert report.symbol == "002371"
            assert len(report.results) >= 1
        finally:
            import os

            os.unlink(db)

    def test_deep_returns_report(self, monkeypatch):
        """deep() 返回 StockAnalysisReport。"""
        from stock import StockAnalyzer

        db = self._build_report_db()
        monkeypatch.setattr(
            "stock.StockAnalyzer._resolve_name", lambda self, code: code
        )
        monkeypatch.setattr("system.config.settings.DATABASE_PATH", db)
        try:
            analyzer = StockAnalyzer(dimensions=["technical"])
            report = analyzer.deep("002371")
            assert isinstance(report, StockAnalysisReport)
            assert len(report.results) >= 1
        finally:
            import os

            os.unlink(db)

    def test_format_cli_produces_output(self):
        """format_cli 产生可读输出。"""
        from stock import StockAnalyzer

        analyzer = StockAnalyzer()
        report = StockAnalysisReport(
            symbol="600519",
            name="贵州茅台",
            results=[
                AnalysisResult(
                    dimension="technical",
                    ok=True,
                    data={},
                    conclusions=["均线多头排列"],
                    risk_flags=[],
                ),
            ],
            aggregated={"ok": True, "total_risks": 0, "risk_summary": []},
        )
        output = analyzer.format_cli(report)
        assert "600519" in output
        assert "贵州茅台" in output
        assert "均线多头排列" in output
        assert "综合" in output

    def test_format_telegram_produces_output(self):
        """format_telegram 产生紧凑输出。"""
        from stock import StockAnalyzer

        analyzer = StockAnalyzer()
        report = StockAnalysisReport(
            symbol="600519",
            name="贵州茅台",
            results=[
                AnalysisResult(
                    dimension="technical",
                    ok=True,
                    data={},
                    conclusions=["MACD多头"],
                    risk_flags=[],
                ),
            ],
        )
        output = analyzer.format_telegram(report)
        assert "600519" in output
        assert "✅ technical" in output

    def test_analyze_catches_analyzer_exception(self, monkeypatch):
        """某个分析器抛出异常 → 被捕获并插入 error result。"""
        from stock import StockAnalyzer
        from stock.stock_registry import _registry

        class BrokenAnalyzer(BaseAnalyzer):
            name = "broken"

            def analyze(self, symbol, **params):
                raise RuntimeError("模拟崩溃")

        saved = copy.copy(_registry)
        try:
            _registry["broken"] = BrokenAnalyzer()
            monkeypatch.setattr(
                "stock.StockAnalyzer._resolve_name",
                lambda self, code: code,
            )
            analyzer = StockAnalyzer(dimensions=["broken"])
            report = analyzer.analyze("600519")
            assert len(report.results) == 1
            assert report.results[0].ok is False
            assert "模拟崩溃" in report.results[0].error
        finally:
            _registry.clear()
            _registry.update(saved)

    def test_analyze_aggregates_risks(self, monkeypatch):
        """多个分析器的 risk_flags 合并到 aggregated。"""
        from stock import StockAnalyzer
        from stock.stock_registry import _registry

        class RiskyAnalyzer(BaseAnalyzer):
            name = "risky"

            def analyze(self, symbol, **params):
                return AnalysisResult(
                    dimension="risky",
                    ok=False,
                    data={},
                    conclusions=[],
                    risk_flags=["风险A"],
                )

        saved = copy.copy(_registry)
        try:
            _registry["risky"] = RiskyAnalyzer()
            monkeypatch.setattr(
                "stock.StockAnalyzer._resolve_name",
                lambda self, code: code,
            )
            analyzer = StockAnalyzer(dimensions=["risky"])
            report = analyzer.analyze("600519")
            assert report.aggregated["ok"] is False
            assert report.aggregated["total_risks"] == 1
            assert "风险A" in report.aggregated["risk_summary"]
        finally:
            _registry.clear()
            _registry.update(saved)
