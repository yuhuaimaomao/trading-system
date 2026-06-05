"""个股分析数据结构 — 独立于实时管线。"""

from dataclasses import dataclass, field


@dataclass
class StockAnalysisRequest:
    symbol: str                    # "600519"
    dimensions: list[str]          # ["technical", "money_flow"]
    params: dict = field(default_factory=dict)  # {"days": 60, "kline_type": "day"}


@dataclass
class AnalysisResult:
    dimension: str                 # "technical"
    ok: bool
    data: dict                     # 结构化数据，给管线消费
    conclusions: list[str]         # 结论短句，给输出消费
    risk_flags: list[str]          # 风险标签
    error: str = ""


@dataclass
class StockAnalysisReport:
    symbol: str
    name: str
    results: list[AnalysisResult]
    aggregated: dict = field(default_factory=dict)  # 综合评分 + 关键风险汇总
