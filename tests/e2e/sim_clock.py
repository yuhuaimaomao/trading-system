# -*- coding: utf-8 -*-
"""可控时钟 — 用于加速 E2E 测试。

不全局 patch datetime（Python 3.14+ 不可变），
而是通过 monkey-patch Watcher 的时间方法和 time.sleep 实现加速。
内部方法中的 datetime.now() 日志调用不影响业务逻辑正确性。
"""

from datetime import datetime, date, time as dt_time, timedelta


class SimClock:
    """可控时钟。"""

    def __init__(self, start: datetime):
        self._dt = start

    def now(self) -> datetime:
        return self._dt

    def time(self) -> dt_time:
        return self._dt.time()

    def date(self) -> date:
        return self._dt.date()

    def advance(self, minutes: int = 1):
        self._dt += timedelta(minutes=minutes)

    def set(self, dt: datetime):
        self._dt = dt

    def strftime(self, fmt: str = "%Y-%m-%d") -> str:
        return self._dt.strftime(fmt)

    def __repr__(self) -> str:
        return f"SimClock({self._dt.strftime('%Y-%m-%d %H:%M:%S')})"


def install_clock(watcher, clock: SimClock):
    """注入 SimClock 到 Watcher，替换所有时间相关方法和 time.sleep。

    同时 monkey-patch _session_phase（market_state.py），
    因为它直接调用 datetime.now() 且无法通过 Watcher 实例覆盖。
    """
    import time as _time

    # 1. time.sleep → no-op
    _time.sleep = lambda seconds=0: None
    watcher.scan_interval = 0.001

    # 2. Watcher 时间判断方法 → 基于 clock
    from trade.monitor.watcher import MORNING_START, MORNING_END, AFTERNOON_START, MARKET_CLOSE

    def _in_trading_hours():
        t = clock.time()
        return (MORNING_START <= t < MORNING_END or
                AFTERNOON_START <= t < MARKET_CLOSE)

    def _in_lunch_break():
        return MORNING_END <= clock.time() < AFTERNOON_START

    def _before_market():
        return clock.time() < MORNING_START

    def _after_market():
        return clock.time() >= MARKET_CLOSE

    def _lunch_break():
        pass

    watcher._in_trading_hours = staticmethod(_in_trading_hours)
    watcher._in_lunch_break = staticmethod(_in_lunch_break)
    watcher._before_market = staticmethod(_before_market)
    watcher._after_market = staticmethod(_after_market)
    watcher._lunch_break = staticmethod(_lunch_break)

    # 3. Patch _session_phase (market_state.py 直接调用 datetime.now())
    import trade.monitor.market_state as _ms
    _original_session_phase = _ms._session_phase

    def _patched_session_phase() -> str:
        t = clock.time()
        dt_time = type(t)
        if t < dt_time(9, 30):
            return "pre_open"
        if t < dt_time(10, 0):
            return "opening"
        if t < dt_time(11, 0):
            return "morning"
        if t < dt_time(11, 30):
            return "late_morning"
        if t < dt_time(13, 0):
            return "lunch"
        if t < dt_time(14, 0):
            return "afternoon"
        if t < dt_time(14, 30):
            return "late_afternoon"
        return "closing"

    _ms._session_phase = _patched_session_phase
    watcher._original_session_phase = _original_session_phase

    # 4. 保存 clock 引用
    watcher._sim_clock = clock
    watcher._trade_date = clock.strftime("%Y-%m-%d")
