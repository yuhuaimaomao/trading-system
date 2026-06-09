"""
市场基础数据 — 跨域共享的行情查询。

stock_basic / sector / snapshot / 事件（涨跌停、龙虎榜）。
所有业务线均可使用。
"""

from data.market.events_data import LimitPoolReader
from data.market.sector_data import SectorReader
from data.market.stock_basic import StockReader

__all__ = ["StockReader", "SectorReader", "LimitPoolReader"]
