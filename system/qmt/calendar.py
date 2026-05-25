# -*- coding: utf-8 -*-
"""交易日历"""

from datetime import date
from system.qmt.client import QMTClient


class TradingCalendar:
    _cache_date: str = ""
    _cache_dates: set[str] = set()

    def __init__(self, client: QMTClient = None):
        self._client = client or QMTClient()

    def is_trading_day(self, d: date = None) -> bool:
        if d is None:
            d = date.today()
        ds = d.isoformat()
        self._ensure_cache()
        return ds in self._cache_dates

    def _ensure_cache(self):
        today = date.today().isoformat()
        if self._cache_date == today:
            return
        result = self._client.calendar("sh")
        if result.get("success") is False:
            return
        data = result.get("data", result)
        if isinstance(data, list):
            self._cache_dates = set(data)
        elif isinstance(data, dict):
            self._cache_dates = set(data.get("trade_days", data.get("dates", [])))
        self._cache_date = today
