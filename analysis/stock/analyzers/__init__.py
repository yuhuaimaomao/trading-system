"""个股分析器基类。"""

from analysis.stock.schemas import AnalysisResult


class BaseAnalyzer:
    """分析器基类 — 每个维度一个子类，实现 analyze(symbol, **params) -> AnalysisResult。"""

    name: str = ""  # 维度名，和 registry key 一致

    def analyze(self, symbol: str, **params) -> AnalysisResult:
        raise NotImplementedError
