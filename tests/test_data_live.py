"""data/live/ 模块测试"""

from data.collect.live.cache import DailyFactorCache, InstrumentCache, IntradayCache
from data.collect.live.order_book import (
    get_big_order_direction,
    get_instrument_info,
    get_order_book_imbalance,
)


class TestIntradayCache:
    def test_new_round(self):
        cache = IntradayCache()
        cache.new_round(5)
        cache.set("000001", {"rsi6": 60})
        assert cache.get("000001") == {"rsi6": 60}

    def test_no_data(self):
        cache = IntradayCache()
        assert cache.get("nope") is None

    def test_scan_property(self):
        cache = IntradayCache()
        cache.new_round(42)
        assert cache.scan == 42


class TestDailyFactorCache:
    def test_set_get(self):
        cache = DailyFactorCache()
        cache.set("000001", {"ma5_angle": 2.5})
        assert cache.get("000001") == {"ma5_angle": 2.5}

    def test_miss(self):
        cache = DailyFactorCache()
        assert cache.get("nope") is None


class TestInstrumentCache:
    def test_set_get(self):
        cache = InstrumentCache()
        cache.set("000001", {"up_stop": 11.0})
        assert cache.get("000001") == {"up_stop": 11.0}


class TestOrderBook:
    def test_no_qmt(self):
        ratio, reason = get_order_book_imbalance("000001", 10.0, None)
        assert ratio == 0.5
        assert reason == ""

    def test_big_order_no_qmt(self):
        ratio, reason = get_big_order_direction("000001", None)
        assert ratio == 0.5

    def test_instrument_no_qmt(self):
        info = get_instrument_info("000001", None, {})
        assert info == {}
