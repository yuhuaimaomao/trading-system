"""QMT 行情接口封装"""

import logging

from system.qmt.client import QMTClient, strip_suffix

logger = logging.getLogger(__name__)

# 沪深后缀列表 — QMT /quotes 端点要求带后缀的完整代码
_SUFFIXES = [".SH", ".SZ", ".BJ"]


class QuoteClient:
    """实时行情客户端"""

    def __init__(self, client: QMTClient = None):
        self._client = client or QMTClient()

    def get_realtime(self, codes: list[str]) -> dict[str, dict]:
        """获取实时行情。自动处理代码后缀。

        QMT /quotes 要求 .SH/.SZ 后缀，但业务层用的代码不帶后缀。
        发送时自动加后缀，返回时 key 用原始代码（去后缀）。

        Returns:
            {code_without_suffix: {lastPrice, preClose, changePct, ...}}
        """
        if not codes:
            return {}

        # 构建扩展代码列表（每个原始代码尝试多个后缀）
        expanded: list[str] = []
        for code in codes:
            code_clean = strip_suffix(code)
            if any(code_clean.endswith(s) for s in _SUFFIXES):
                expanded.append(code_clean)
            else:
                for suffix in _SUFFIXES:
                    expanded.append(f"{code_clean}{suffix}")

        result = self._client.quotes(expanded)
        if not result.get("success", True):
            return {}
        data = result.get("data", result)

        # data 是 {full_code: item} 的 dict，key 转回无后缀的格式
        normalized: dict[str, dict] = {}
        if isinstance(data, dict):
            for full_code, item in data.items():
                short = strip_suffix(full_code)
                if short not in normalized or item.get("lastPrice"):
                    normalized[short] = item
        return normalized

    def get_price(self, code: str) -> float | None:
        result = self._client.quote(code)
        if not result.get("success", True):
            return None
        data = result.get("data", result)
        return data.get("last_price") or data.get("lastPrice") or data.get("price")

    def get_minute_kline(self, code: str, count: int = 240) -> list[dict]:
        return self._get_kline(code, "1m", count)

    def get_kline(self, code: str, period: str, count: int = 50) -> list[dict]:
        return self._get_kline(code, period, count)

    def _get_kline(self, code: str, period: str, count: int) -> list[dict]:
        code_clean = strip_suffix(code)
        for suffix in _SUFFIXES:
            full_code = f"{code_clean}{suffix}"
            result = self._client.history(full_code, period=period, count=count)
            if result.get("success", True):
                data = result.get("data", result)
                return data if isinstance(data, list) else []
        return []

    def get_history(
        self,
        code: str,
        period: str = "1d",
        start: str = None,
        end: str = None,
        count: int = None,
    ) -> list[dict]:
        result = self._client.history(
            code, period=period, start=start, end=end, count=count
        )
        if not result.get("success", True):
            return []
        return result.get("data", result) or []

    def get_all_quotes(self) -> dict[str, dict]:
        """获取全市场实时快照。用于涨跌家数、市场宽度、异动检测。"""
        result = self._client.all_quotes()
        if not result.get("success", True):
            return {}
        data = result.get("data", result)
        if not isinstance(data, dict):
            return {}
        normalized: dict[str, dict] = {}
        for full_code, item in data.items():
            short = strip_suffix(full_code)
            if short not in normalized or item.get("lastPrice"):
                normalized[short] = item
        return normalized

    def get_quote_detail(self, code: str) -> dict | None:
        """获取个股完整实时行情（含五档盘口）。"""
        code_clean = strip_suffix(code)
        result = self._client.quote(code_clean)
        if not result.get("success", True):
            return None
        return result.get("data", result)

    def get_instrument(self, code: str) -> dict | None:
        """获取合约基本信息（总股本、流通股本、涨跌停价等）。"""
        code_clean = strip_suffix(code)
        result = self._client.instrument(code_clean)
        if not result.get("success", True):
            return None
        return result.get("data", result)

    def get_ticks(self, code: str) -> list[dict]:
        """获取个股近期逐笔成交明细。"""
        code_clean = strip_suffix(code)
        result = self._client.tick(code_clean)
        if not result.get("success", True):
            return []
        data = result.get("data", result)
        return data if isinstance(data, list) else []
