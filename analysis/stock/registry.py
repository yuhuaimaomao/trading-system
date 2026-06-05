"""分析器注册表。"""

from analysis.stock.analyzers import BaseAnalyzer

_registry: dict[str, BaseAnalyzer] = {}


def register(analyzer: BaseAnalyzer):
    """注册一个分析器。"""
    _registry[analyzer.name] = analyzer


def get(name: str) -> BaseAnalyzer | None:
    """获取指定分析器。"""
    return _registry.get(name)


def list_all() -> list[str]:
    """列出所有已注册维度。"""
    return list(_registry.keys())


def get_many(names: list[str]) -> list[BaseAnalyzer]:
    """批量获取分析器。"""
    return [a for n in names if (a := _registry.get(n))]
