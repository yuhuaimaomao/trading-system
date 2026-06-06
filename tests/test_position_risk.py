"""PositionRiskMixin._check_positions 行为测试 — 锁住现有逻辑后重构"""

import pytest

from trade.exec.paper.portfolio import Portfolio
from trade.risk.position_risk import PositionRiskMixin


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
            return type(
                "SellResult",
                (),
                {
                    "success": True,
                    "pnl": pnl,
                    "pnl_pct": pnl / (pos.avg_cost * pos.volume)
                    if pos.avg_cost > 0
                    else 0,
                    "proceeds": price * pos.volume,
                    "commission": 5,
                    "reason": reason,
                },
            )()
        return type(
            "SellResult",
            (),
            {
                "success": False,
                "pnl": 0,
                "pnl_pct": 0,
                "proceeds": 0,
                "commission": 0,
                "reason": "not found",
            },
        )()


def _make_pa(cash=200000, daily_loss=0):
    """daily_loss: 模拟当日已亏损金额（通过 _prev_total 控制）"""
    p = Portfolio(initial_cash=cash)
    p._peak_value = cash
    p._prev_total = (
        cash + daily_loss
    )  # 让 daily_pnl = total_value - _prev_total = -daily_loss
    return _MockPA(p)


def _add_position(pa, code, name, volume, avg_cost, entry_date="2026-05-30", locked=0):
    """直接写入 Portfolio，同时扣现金模拟真实买入。
    默认 locked=0（非当日买入，可卖出）。测试 T+1 时传 locked=volume。
    """
    from trade.exec.paper.portfolio import Position

    cost = avg_cost * volume
    pa._portfolio.cash -= cost
    pa._portfolio.positions[code] = Position(
        stock_code=code,
        stock_name=name,
        volume=volume,
        avg_cost=avg_cost,
        current_price=avg_cost,
        market_value=cost,
        entry_date=entry_date,
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
        self._sector_stats = {}
        self._triggered_ids = set()
        self._market_snapshot = {}
        self._sector_trend_history = {}
        self._concept_trend_history = {}
        self._concept_stats = {}
        self._concept_cache = {}
        self._index_prices = []
        self._index_high = 0.0
        self._index_low = 0.0
        self._index_map = {}
        self._index_close_high = 0.0
        self._index_close_low = 0.0
        self._market_turnovers = []
        self._limit_cache = {}
        self._intraday_cache = {}
        self._daily_factor_cache = {}
        self._morning_sector_bias = {}
        self._snapshot_price_history = {}
        self._recent_prices = {}
        self._instrument_cache = {}
        self._push_cooldown = {}
        self._health_alert_seen = {}
        self._pullback_scan_count = 0
        self._pullback_alerted_today = set()
        self._pending_chase = {}
        self._pending_index_ai = {}
        self._cached_db_watch_codes = set()
        self._watch_codes_stale = True
        self._alert_fingerprints = {}
        self._signal_alert_state = {}
        self._review_alert_state = {}
        self._prev_snapshot = {}
        self._data_ready = True
        self._data_ready_at = 0
        self._data_missing_rounds = 0
        self._last_index_quote = {}
        self._last_db_ts = 0
        self._index_alerted_downtrend = False
        self._index_alerted_ma20 = 0
        self._index_last_fluctuation_price = 0.0
        self._volume_alerted_divergence = False
        self._closing_decision_done = False
        self._max_drawdown_alerted = False
        self._last_resonance_push_scan = -100
        self._last_resonance_index_dir = ""
        self._index_tech_state = {}
        self._ma_baseline_cache = None
        self._intraday_cache_scan = -1

    def build_state(self):
        from trade.core.scan_state import ScanState

        return ScanState(
            running=True,
            trade_date=self._trade_date,
            scan_count=self._scan_count,
            index_prices=self._index_prices,
            index_high=self._index_high,
            index_low=self._index_low,
            index_map=self._index_map,
            index_close_high=self._index_close_high,
            index_close_low=self._index_close_low,
            index_alerted_downtrend=self._index_alerted_downtrend,
            index_alerted_ma20=self._index_alerted_ma20,
            index_last_fluctuation_price=self._index_last_fluctuation_price,
            index_tech_state=self._index_tech_state,
            market_breadth=self._market_breadth
            if hasattr(self, "_market_breadth")
            else {},
            market_turnovers=self._market_turnovers,
            volume_alerted_divergence=self._volume_alerted_divergence,
            regime=self._regime,
            closing_decision_done=self._closing_decision_done,
            max_drawdown_alerted=self._max_drawdown_alerted,
            data_ready=self._data_ready,
            data_ready_at=self._data_ready_at,
            data_missing_rounds=self._data_missing_rounds,
            market_snapshot=self._market_snapshot,
            last_index_quote=self._last_index_quote,
            last_db_ts=self._last_db_ts,
            sector_stats=self._sector_stats,
            concept_stats=self._concept_stats,
            industry_cache=self._industry_cache,
            concept_cache=self._concept_cache,
            last_resonance_push_scan=self._last_resonance_push_scan,
            last_resonance_index_dir=self._last_resonance_index_dir,
            triggered_ids=self._triggered_ids,
            alerted_sl_tp=self._alerted_sl_tp,
            alert_fingerprints=self._alert_fingerprints,
            signal_alert_state=self._signal_alert_state,
            review_alert_state=self._review_alert_state,
            prev_snapshot=self._prev_snapshot,
            recently_sold=self._recently_sold,
            pos_meta=self._pos_meta,
            bought_watch=self._bought_watch,
            recent_prices=self._recent_prices,
            snapshot_price_history=self._snapshot_price_history,
            sl_reminders=self._sl_reminders,
            ma_baseline_cache=self._ma_baseline_cache,
            limit_cache=self._limit_cache,
            instrument_cache=self._instrument_cache,
            intraday_cache=self._intraday_cache,
            intraday_cache_scan=self._intraday_cache_scan,
            daily_factor_cache=self._daily_factor_cache,
            cached_db_watch_codes=self._cached_db_watch_codes,
            watch_codes_stale=self._watch_codes_stale,
            pullback_scan_count=self._pullback_scan_count,
            pullback_alerted_today=self._pullback_alerted_today,
            pending_chase=self._pending_chase,
            pending_index_ai=self._pending_index_ai,
            morning_sector_bias=self._morning_sector_bias,
            push_cooldown=self._push_cooldown,
            health_alert_seen=self._health_alert_seen,
        )

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

    def _handle_stop_signal(
        self,
        key,
        code,
        name,
        stype,
        price,
        trigger,
        ref_price,
        trend,
        limit_down,
        extra="",
    ):
        self._stop_signals.append(
            {
                "key": key,
                "code": code,
                "stype": stype,
                "price": price,
                "trigger": trigger,
                "extra": extra,
            }
        )
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
        w._check_positions(w.build_state(), {"000001": 9.0, "000002": 18.0})

        assert len(w._alerts) == 1
        assert "日内熔断" in w._alerts[0]
        assert len(pa._sold) == 2  # 两个浮亏仓位

    def test_t1_protection_blocks_circuit_breaker_sell(self):
        pa = _make_pa(cash=200000, daily_loss=9000)
        # locked=volume → 当日买入，可用为 0
        _add_position(pa, "000001", "票A", 1000, 10.0, locked=1000)
        pa.update_prices({"000001": 9.0})

        w = _TestWatcher(pa)
        w._check_positions(w.build_state(), {"000001": 9.0})

        assert len(w._alerts) == 1
        assert "T+1" in w._alerts[0]
        assert len(pa._sold) == 0  # 没卖出

    def test_no_trigger_when_loss_below_3pct(self):
        pa = _make_pa(cash=200000, daily_loss=2000)  # 1%
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.9})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {
            "sl": 0,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 10.0,
        }
        w._check_positions(w.build_state(), {"000001": 9.9})

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
        w._pos_meta["000001"] = {
            "sl": 9.8,
            "tp": 12.0,
            "trailing_stop": 0,
            "highest_price": 10.0,
        }
        w._check_positions(w.build_state(), {"000001": 9.5})

        assert any(s["stype"] == "止损" for s in w._stop_signals)

    def test_no_trigger_when_price_above_stop(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.9})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {
            "sl": 9.5,
            "tp": 12.0,
            "trailing_stop": 0,
            "highest_price": 10.0,
        }
        w._check_positions(w.build_state(), {"000001": 9.9})

        assert not any(s["stype"] == "止损" for s in w._stop_signals)

    def test_t1_protection_blocks_stop_loss(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0, entry_date="2026-06-01")
        pa.update_prices({"000001": 9.0})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {
            "sl": 9.8,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 10.0,
        }
        w._check_positions(w.build_state(), {"000001": 9.0})

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
        w._pos_meta["000001"] = {
            "sl": 9.5,
            "tp": 12.0,
            "trailing_stop": 0,
            "highest_price": 10.0,
        }
        w._check_positions(w.build_state(), {"000001": 12.5})

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
        w._pos_meta["000001"] = {
            "sl": 5.0,
            "tp": 25.0,
            "trailing_stop": 0.05,
            "highest_price": 20.0,
        }
        w._check_positions(w.build_state(), {"000001": 9.0})

        assert any(s["stype"] == "移动止盈" for s in w._stop_signals)

    def test_no_trigger_above_trail_price(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 19.5})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {
            "sl": 9.5,
            "tp": 25.0,
            "trailing_stop": 0.05,
            "highest_price": 20.0,
        }
        w._check_positions(w.build_state(), {"000001": 19.5})

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
        w._pos_meta["000001"] = {
            "sl": 9.8,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 10.0,
        }
        w._check_positions(w.build_state(), {"000001": 9.82})
        assert any(s["stype"] == "止损" for s in w._stop_signals)

    def test_sector_normal_no_adjustment(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        # price 9.81 > effective_sl 9.80 → 刚好不触发
        pa.update_prices({"000001": 9.81})

        w = _TestWatcher(pa)
        # 横盘 → sl_tighten=1.0, effective=10-(10-9.8)*1.0=9.8
        w._pos_meta["000001"] = {
            "sl": 9.8,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 10.0,
        }
        w._check_positions(w.build_state(), {"000001": 9.81})
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
        w._pos_meta["000001"] = {
            "sl": 9.0,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 12.0,
        }
        w._check_positions(w.build_state(), {"000001": 15.0})

        assert w._pos_meta["000001"]["highest_price"] == 15.0

    def test_tracks_max_profit_pct(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 14.0})

        w = _TestWatcher(pa)
        w._pos_meta["000001"] = {
            "sl": 9.0,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 12.0,
        }
        w._check_positions(w.build_state(), {"000001": 14.0})

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
        w._pos_meta["000001"] = {
            "sl": 9.0,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 11.8,
        }
        w._check_positions(w.build_state(), {"000001": 11.0})
        # 当前 10% < 18%*0.60=10.8% → 触发 T1

        assert any("利润回撤" in s["stype"] for s in w._stop_signals)

    def test_no_trigger_below_5pct_max_profit(self):
        pa = _make_pa()
        _add_position(pa, "000001", "票A", 1000, 10.0)
        pa.update_prices({"000001": 9.5})

        w = _TestWatcher(pa)
        w._bought_watch["000001"] = {"max_profit_pct": 0.04}  # < 5%
        w._pos_meta["000001"] = {
            "sl": 9.0,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 10.4,
        }
        w._check_positions(w.build_state(), {"000001": 9.5})

        assert not any("利润回撤" in s["stype"] for s in w._stop_signals)
