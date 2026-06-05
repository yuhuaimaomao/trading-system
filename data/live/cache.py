"""盘中缓存管理 — 日内指标缓存、日线因子缓存、合约信息缓存。

设计：
- IntradayCache: 同一扫描轮内复用（每次 _scan 刷新）
- DailyFactorCache: 全天不变（日线数据盘中不更新）
- InstrumentCache: 全天不变（合约信息盘中不更新）
"""


class IntradayCache:
    """日内指标缓存 — 同一扫描轮内复用。

    Watcher 每轮 _scan() 开始时调用 new_round() 刷新。
    """

    def __init__(self):
        self._data: dict[str, dict] = {}
        self._scan: int = -1

    def new_round(self, scan_count: int):
        """开始新一轮扫描，上一轮的缓存仍可读但写入会覆盖。"""
        self._scan = scan_count

    def get(self, code: str) -> dict | None:
        """获取缓存值，仅当前轮有效。"""
        return self._data.get(code)

    def set(self, code: str, data: dict):
        """写入缓存。"""
        self._data[code] = data

    @property
    def scan(self) -> int:
        return self._scan


class DailyFactorCache:
    """日线因子缓存 — 全天不变，首次查询后永久缓存。"""

    def __init__(self):
        self._data: dict[str, dict] = {}

    def get(self, code: str) -> dict | None:
        return self._data.get(code)

    def set(self, code: str, data: dict):
        self._data[code] = data


class InstrumentCache:
    """合约信息缓存 — 全天不变，首次查询后永久缓存。"""

    def __init__(self):
        self._data: dict[str, dict] = {}

    def get(self, code: str) -> dict | None:
        return self._data.get(code)

    def set(self, code: str, data: dict):
        self._data[code] = data
