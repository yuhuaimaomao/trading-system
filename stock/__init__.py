"""个股分析引擎 — 独立于实时管线，共用 analysis/indicators + data/readers。

使用方式:
    from stock import StockAnalyzer

    analyzer = StockAnalyzer()
    report = analyzer.quick("600519")   # 技术+资金+板块
    report = analyzer.deep("600519")    # 全维度（未来扩展）
    print(analyzer.format_cli(report))
"""

import sqlite3

from stock.stock_formatter import to_cli, to_dict, to_telegram
from stock.stock_registry import _registry, get_many, list_all
from stock.stock_schemas import StockAnalysisReport
from system.config import settings


# ── 注册内置分析器 ──
def _register_builtins():
    if "technical" not in _registry:
        from stock.analyzers.money_flow import MoneyFlowAnalyzer
        from stock.analyzers.sector_attr import SectorAttrAnalyzer
        from stock.analyzers.technical import TechnicalAnalyzer

        _registry["technical"] = TechnicalAnalyzer()
        _registry["money_flow"] = MoneyFlowAnalyzer()
        _registry["sector_attr"] = SectorAttrAnalyzer()


class StockAnalyzer:
    """个股分析统一入口。"""

    QUICK_DIMS = ["technical", "money_flow", "sector_attr"]

    def __init__(self, dimensions: list[str] | None = None):
        _register_builtins()
        self._dimensions = dimensions or self.QUICK_DIMS

    def analyze(self, symbol: str, **params) -> StockAnalysisReport:
        """跑指定维度，聚合结果。"""
        analyzers = get_many(self._dimensions)
        # 名称解析
        name = self._resolve_name(symbol)

        results = []
        for a in analyzers:
            try:
                results.append(a.analyze(symbol, **params))
            except Exception as e:
                from stock.stock_schemas import AnalysisResult

                results.append(
                    AnalysisResult(
                        dimension=a.name,
                        ok=False,
                        data={},
                        conclusions=[],
                        risk_flags=[],
                        error=str(e),
                    )
                )

        # 聚合
        all_risks = [f for r in results for f in r.risk_flags]
        aggregated = {
            "ok": all(r.ok for r in results),
            "total_risks": len(all_risks),
            "risk_summary": all_risks[:5] if all_risks else [],
        }

        return StockAnalysisReport(
            symbol=symbol, name=name, results=results, aggregated=aggregated
        )

    def quick(self, symbol: str) -> StockAnalysisReport:
        """快速模式：技术+资金+板块，给 Watcher/盘前用。"""
        return self.analyze(symbol, mode="quick")

    def deep(self, symbol: str) -> StockAnalysisReport:
        """深度模式：全维度，给盘后研究/AI prompt 用。"""
        return self.analyze(symbol, mode="deep")

    def format_cli(self, report: StockAnalysisReport) -> str:
        return to_cli(report)

    def format_telegram(self, report: StockAnalysisReport) -> str:
        return to_telegram(report)

    @staticmethod
    def _resolve_name(code: str) -> str:
        try:
            conn = sqlite3.connect(settings.DATABASE_PATH)
            row = conn.execute(
                "SELECT stock_name FROM stock_basic WHERE stock_code=? "
                "ORDER BY trade_date DESC LIMIT 1",
                (code,),
            ).fetchone()
            conn.close()
            return row[0] if row else code
        except Exception:
            return code
