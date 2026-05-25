# -*- coding: utf-8 -*-
"""QMT 自动下单执行器 — 预留"""

from analysis.signals import OrderSignal


class QMTExecutor:
    def __init__(self):
        pass

    def execute(self, signal: OrderSignal, account: str = "live") -> bool:
        raise NotImplementedError("待 QMT 下单接口实测后实现")
