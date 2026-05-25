# -*- coding: utf-8 -*-
"""回测框架 — 简单回测引擎骨架"""

from .data_loader import DataLoader
from .engine import BacktestEngine, BacktestConfig, OrderSignal, Trade
from .metrics import calculate_metrics

__all__ = [
    "DataLoader",
    "BacktestEngine",
    "BacktestConfig",
    "OrderSignal",
    "Trade",
    "calculate_metrics",
]
