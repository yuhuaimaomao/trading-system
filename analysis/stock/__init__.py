"""个股分析引擎 — 独立于实时管线，共用 analysis/indicators + data/readers。

使用方式:
    from analysis.stock import StockAnalyzer

    analyzer = StockAnalyzer(["technical", "money_flow", "sector_attr"])
    report = analyzer.quick("600519")   # 快速模式（盘中用）
    report = analyzer.deep("600519")    # 深度模式（盘后用）
"""

from analysis.stock.registry import get, get_many, list_all, register
from analysis.stock.schemas import StockAnalysisReport


class StockAnalyzer:
    """个股分析统一入口。"""

    def __init__(self, dimensions: list[str] | None = None):
        self._dimensions = dimensions or list_all()

    def analyze(self, symbol: str, **params) -> StockAnalysisReport:
        """跑全部（或指定）维度，聚合结果。"""
        analyzers = get_many(self._dimensions)
        results = []
        for a in analyzers:
            try:
                results.append(a.analyze(symbol, **params))
            except Exception as e:
                from analysis.stock.schemas import AnalysisResult
                results.append(AnalysisResult(
                    dimension=a.name, ok=False, data={},
                    conclusions=[], risk_flags=[], error=str(e),
                ))
        return StockAnalysisReport(symbol=symbol, name="", results=results)

    def quick(self, symbol: str) -> StockAnalysisReport:
        """快速模式：技术面 + 资金面 + 板块归因，给 Watcher/盘前用。"""
        return self.analyze(symbol, mode="quick")

    def deep(self, symbol: str) -> StockAnalysisReport:
        """深度模式：全维度，给盘后研究/AI prompt 用。"""
        return self.analyze(symbol, mode="deep")
