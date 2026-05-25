# -*- coding: utf-8 -*-
"""QMT 行情接口封装"""

from system.qmt.client import QMTClient


class QuoteClient:
    """实时行情客户端"""

    def __init__(self, client: QMTClient = None):
        self._client = client or QMTClient()

    def get_realtime(self, codes: list[str]) -> dict:
        result = self._client.quotes(codes)
        if not result.get("success", True):
            return {}
        data = result.get("data", result)
        return {item.get("code", ""): item for item in data} if isinstance(data, list) else data

    def get_price(self, code: str) -> float | None:
        result = self._client.quote(code)
        if not result.get("success", True):
            return None
        data = result.get("data", result)
        return data.get("last_price") or data.get("lastPrice") or data.get("price")

    def get_minute_kline(self, code: str, count: int = 240) -> list[dict]:
        result = self._client.minute_kline(code, count=count)
        if not result.get("success", True):
            return []
        return result.get("data", result) or []

    def get_history(self, code: str, period: str = "1d",
                    start: str = None, end: str = None, count: int = None) -> list[dict]:
        result = self._client.history(code, period=period, start=start, end=end, count=count)
        if not result.get("success", True):
            return []
        return result.get("data", result) or []
