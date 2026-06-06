"""PositionRiskMixin._check_positions 行为测试 — 锁住现有逻辑后重构"""

import pytest
from trade.monitor.position_risk import PositionRiskMixin
from trade.paper.portfolio import Portfolio


class _MockPA:
    """模拟 PaperAccount — 只有 _check_positions 需要的属性"""

    def __init__(self, portfolio):
        self._portfolio = portfolio
        self._sold = []

    @property
    def positions(self):
        return self._portfolio.positions

    @property
    def cash(self):
        return self._portfolio.cash

    @property
    def total_value(self):
        return self._portfolio.total_value

    @property
    def daily_pnl(self):
        return self._portfolio.daily_pnl

    def update_prices(self, prices):
        self._portfolio.update_prices(prices)

    def sell(self, code, price, reason="", signal_id=None):
        self._sold.append({"code": code, "price": price, "reason": reason})
        pos = self._portfolio.positions.get(code)
        if pos:
            pos.update_price(price)
            self._portfolio.cash += price * pos.volume
            pnl = (price - pos.avg_cost) * pos.volume
            del self._portfolio.positions[code]
            return type("SellResult", (), {"success": True, "pnl": pnl, "pnl_pct": pnl / (pos.avg_cost * pos.volume) if pos.avg_cost > 0 else 0, "proceeds": price * pos.volume, "commission": 5, "reason": reason})()
        return type("SellResult", (), {"success": False, "pnl": 0, "pnl_pct": 0, "proceeds": 0, "commission": 0, "reason": "not found"})()


def _make_pa(cash=200000, daily_loss=0):
    """daily_loss: 模拟当日已亏损金额（通过 _prev_total 控制）"""
    p = Portfolio(initial_cash=cash)
    p._peak_value = cash
    p._prev_total = cash + daily_loss  # 让 daily_pnl = total_value - _prev_total = -daily_loss
    return _MockPA(p)


def _add_position(pa, code, name, volume, avg_cost, entry_date="2026-05-30", locked=0):
    """直接写入 Portfolio，同时扣现金模拟真实买入。
    默认 locked=0（非当日买入，可卖出）。测试 T+1 时传 locked=volume。
    """
    from trade.paper.portfolio import Position
    cost = avg_cost * volume
    pa._portfolio.cash -= cost
    pa._portfolio.positions[code] = Position(
        stock_code=code, stock_name=name, volume=volume,
        avg_cost=avg_cost, current_price=avg_cost,
        market_value=cost, entry_date=entry_date,
        locked_volume=locked,
    )


class _TestWatcher(PositionRiskMixin):
    """最小 Watcher 替身"""

    def __init__(self, paper_account, trade_date="2026-06-01"):
        self.paper_account = paper_account
        self._trade_date = trade_date
        self._pos_meta = {}
        self._bought_watch = {}
        self._regime = None
        self._sl_reminders = {}
        self._alerted_sl_tp = set()
        self._alerts = []
        self._stop_signals = []
        self.qmt = None
        self._scan_count = 0
        self._recently_sold = {}
        self._industry_cache = {}

    def _minutes_since_open(self):
        return 999  # 跳过开盘缓冲期

    def _get_sector_trend(self, code):
        return "横盘"

    def _is_limit_down(self, code, price):
        return False

    def _invalidate_watch_codes_cache(self):
        pass

    def _deep_rebound_improving(self, code, deep_state):
        return False

    def _alert(self, msg):
        self._alerts.append(msg)

    def _handle_stop_signal(self, key, code, name, stype, price,
                            trigger, ref_price, trend, limit_down, extra=""):
        self._stop_signals.append({
            "key": key, "code": code, "stype": stype, "price": price,
            "trigger": trigger, "extra": extra,
        })
        self.paper_account.sell(code, price, stype)


# ═══════════════════════════════════════════════════════════════
# 日内熔断
# ═══════════════════════════════════════════════════════════════

class TestDailyLossCircuitBreaker:
    def test_triggers_when_loss_exceeds_3pct(self):
        pa = _make_pa(cash=200000, daily_loss=9000)  # 4.5%
        _add_position(pa, "000001", "票A", 1000, 10.0)
        _add_position(pa, "000002", "票B", 500, 20.0)
        pa.update_prices({"000001": 9.0, "000002": 18.0})

        w = _TestWatcher(pa)
        w._check_positions({"000001": 9.0, "000002": 18.0})

        assert len(w._alerts) == 1
        assert "日内熔断" in w._alerts[0]
        assert len(pa._sold) == 2  # 两个浮亏仓位

    def test_t1_protection_blocks_circuit_breaker_sell(self):
        pa = _make_pa(cash=200000, daily_loss=9000)
        # locked=volume → 当日买入，可用为 0
        _add_position(pa, "000001", "票A", 1000, 10.0, locked=1000)
        pa.update_prices({"000001": 9.0})

        w = _TestWatcher(pa)
        w._check_positions({"000001": 9.0})

        assert len(w._alerts) == 1
        assert "T+1" in w._alerts[0]
        assert len(pa._sold) == 0  # 没卖出

    def test_no_trigger_when_loss_below_3pct(self):
        pa = _make_pa(cash=200000, daily_loss=2000)  # 1%
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.9})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {"sl": 0, "tp": 0, "trailing_stop": 0, "highest_price": 10.0}
        w._check_positions({"000001": 9.9})

        assert not any("日内熔断" in a for a in w._alerts)


# ═══════════════════════════════════════════════════════════════
# 止损
# ═══════════════════════════════════════════════════════════════

class TestStopLoss:
    def test_triggers_when_price_below_stop(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.5})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {"sl": 9.8, "tp": 12.0, "trailing_stop": 0, "highest_price": 10.0}
        w._check_positions({"000001": 9.5})

        assert any(s["stype"] == "止损" for s in w._stop_signals)

    def test_no_trigger_when_price_above_stop(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.9})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {"sl": 9.5, "tp": 12.0, "trailing_stop": 0, "highest_price": 10.0}
        w._check_positions({"000001": 9.9})

        assert not any(s["stype"] == "止损" for s in w._stop_signals)

    def test_t1_protection_blocks_stop_loss(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0, entry_date="2026-06-01")
        pa.update_prices({"000001": 9.0})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {"sl": 9.8, "tp": 0, "trailing_stop": 0, "highest_price": 10.0}
        w._check_positions({"000001": 9.0})

        assert not any(s["stype"] == "止损" for s in w._stop_signals)


# ═══════════════════════════════════════════════════════════════
# 止盈
# ═══════════════════════════════════════════════════════════════

class TestTakeProfit:
    def test_triggers_standard_take_profit(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 12.5})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {"sl": 9.5, "tp": 12.0, "trailing_stop": 0, "highest_price": 10.0}
        w._check_positions({"000001": 12.5})

        assert any(s["stype"] == "止盈" for s in w._stop_signals)


# ═══════════════════════════════════════════════════════════════
# 移动止盈
# ═══════════════════════════════════════════════════════════════

class TestTrailingStop:
    def test_triggers_when_price_drops_from_high(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.0})

        w = _TestWatcher(pa)
        # sl 设很低避免止损先触发（止损优先级高于移动止盈）
        w._pos_meta["000001"] = {"sl": 5.0, "tp": 25.0, "trailing_stop": 0.05, "highest_price": 20.0}
        w._check_positions({"000001": 9.0})

        assert any(s["stype"] == "移动止盈" for s in w._stop_signals)

    def test_no_trigger_above_trail_price(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 19.5})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {"sl": 9.5, "tp": 25.0, "trailing_stop": 0.05, "highest_price": 20.0}
        w._check_positions({"000001": 19.5})

        assert not any(s["stype"] == "移动止盈" for s in w._stop_signals)


# ═══════════════════════════════════════════════════════════════
# 板块感知调整
# ═══════════════════════════════════════════════════════════════

class TestSectorAwareAdjustment:
    def test_sector_accel_weak_tightens_more(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.82})

        w = _TestWatcher(pa)
        w._get_sector_trend = lambda code: "持续走弱 加速 -3% 弱于大盘 普跌"
        # sl=9.8, safe→1.0, accel_down→*0.90, effective=10-(10-9.8)*0.9=9.82
        # 9.82 <= max(9.82, 9.8*0.85=8.33) → 触发
        w._pos_meta["000001"] = {"sl": 9.8, "tp": 0, "trailing_stop": 0, "highest_price": 10.0}
        w._check_positions({"000001": 9.82})
        assert any(s["stype"] == "止损" for s in w._stop_signals)

    def test_sector_normal_no_adjustment(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        # price 9.81 > effective_sl 9.80 → 刚好不触发
        pa.update_prices({"000001": 9.81})

        w = _TestWatcher(pa)
        # 横盘 → sl_tighten=1.0, effective=10-(10-9.8)*1.0=9.8
        w._pos_meta["000001"] = {"sl": 9.8, "tp": 0, "trailing_stop": 0, "highest_price": 10.0}
        w._check_positions({"000001": 9.81})
        assert not any(s["stype"] == "止损" for s in w._stop_signals)


# ═══════════════════════════════════════════════════════════════
# 最高价 / 最高浮盈追踪
# ═══════════════════════════════════════════════════════════════

class TestPriceTracking:
    def test_updates_highest_price(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 15.0})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {"sl": 9.0, "tp": 0, "trailing_stop": 0, "highest_price": 12.0}
        w._check_positions({"000001": 15.0})

        assert w._pos_meta["000001"]["highest_price"] == 15.0

    def test_tracks_max_profit_pct(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 14.0})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {"sl": 9.0, "tp": 0, "trailing_stop": 0, "highest_price": 12.0}
        w._check_positions({"000001": 14.0})

        assert w._bought_watch["000001"]["max_profit_pct"] == pytest.approx(0.4)


# ═══════════════════════════════════════════════════════════════
# 利润回撤止盈
# ═══════════════════════════════════════════════════════════════

class TestRetracementStop:
    def test_triggers_t1_when_max_profit_15pct_drops_below_threshold(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 11.0})

        w = _TestWatcher(pa)
        w._bought_watch["000001"] = {"max_profit_pct": 0.18}  # 最高浮盈 18%
        w._pos_meta["000001"] = {"sl": 9.0, "tp": 0, "trailing_stop": 0, "highest_price": 11.8}
        w._check_positions({"000001": 11.0})
        # 当前 10% < 18%*0.60=10.8% → 触发 T1

        assert any("利润回撤" in s["stype"] for s in w._stop_signals)

    def test_no_trigger_below_5pct_max_profit(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.5})

        w = _TestWatcher(pa)
        w._bought_watch["000001"] = {"max_profit_pct": 0.04}  # < 5%
        w._pos_meta["000001"] = {"sl": 9.0, "tp": 0, "trailing_stop": 0, "highest_price": 10.4}
        w._check_positions({"000001": 9.5})

        assert not any("利润回撤" in s["stype"] for s in w._stop_signals)
