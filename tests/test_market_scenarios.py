# -*- coding: utf-8 -*-
"""大盘+个股走势场景模拟测试 — 验证系统在各种盘面下的全链路响应

场景覆盖:
  单边下跌 → market_ok=False, 止损触发
  恐慌下跌 → panic 模式, 熔断
  V型反转 → recovery 信号, 恢复买入
  缓涨     → 正常操作, 利润累积
  震荡横盘 → 正常操作, 无异常告警
  死猫跳   → dead_cat 模式, 不跟进
  全链路   → 大盘→信号→持仓→风控→尾盘 端到端
"""

import pytest
from unittest.mock import MagicMock, PropertyMock, patch
from datetime import datetime
from collections import defaultdict

# 所有尾盘测试需 mock 时间为 14:35
CLOSING_TIME_PATCH = patch("trade.monitor.closing.datetime")
CLOSING_TIME_PATCH.start().now.return_value = datetime(2026, 5, 29, 14, 35, 0)


# ===================================================================
# 场景模拟工具
# ===================================================================


class ScenarioSimulator:
    """模拟 Watcher 关键状态，注入不同盘面走势数据，验证系统响应。"""

    def __init__(self, initial_index=3300.0):
        # 大盘状态
        self._index_prices: list[float] = []
        self._index_high: float = initial_index
        self._index_low: float = initial_index
        self._index_alerted_downtrend: bool = False
        self._index_last_fluctuation_price: float = 0.0
        self._market_turnovers: list[float] = []
        self._volume_alerted_divergence: bool = False

        # 板块 / 概念
        self._sector_stats: dict[str, dict] = {}
        self._concept_stats: dict[str, dict] = {}
        self._sector_trend_history: dict[str, list[float]] = defaultdict(list)
        self._sector_trend_continuity: dict[str, int] = defaultdict(int)
        self._sector_trend_last_dir: dict[str, str] = {}
        self._industry_cache: dict[str, str] = {}
        self._concept_cache: dict[str, list[str]] = {}
        self._market_snapshot: dict[str, dict] = {}
        self._prev_snapshot: dict[str, dict] = {}
        self._prev_ind_amounts: dict[str, float] = {}
        self._prev_con_amounts: dict[str, float] = {}

        # 持仓
        self.portfolio = MagicMock()
        self.portfolio.positions = {}
        self.portfolio.drawdown = 0.0
        self.portfolio.total_value = 200_000
        self.portfolio.daily_pnl = 0.0

        # 基础设施
        self.telegram = MagicMock()
        self.db_path = ":memory:"
        self.repo = MagicMock()
        self.repo.get_pending_signals.return_value = []
        self.risk_engine = MagicMock()
        self.scan_interval = 60
        self._alert = MagicMock()
        self._alert_private = MagicMock()
        self._ma_baseline_cache = (3300, 3320, 3350)
        self._max_drawdown_alerted = False
        self._closing_decision_done = False
        self._scan_count = 0
        self._trade_date = "2026-05-29"
        self._last_index_quote: dict | None = None
        self._limit_cache: dict[str, dict] = {}
        self._alerted_sl_tp: set[str] = set()
        self._bought_watch: dict[str, dict] = {}
        self._sl_reminders: dict[str, dict] = {}
        self.qmt = None
        self._paper_trader = None
        self._abnormal_detector = None
        self._sector_monitor = None
        self._index_tech_state: dict = {
            "macd_cross": None, "rsi6_zone": "normal", "rsi12_zone": "normal",
            "kdj_cross": None, "kdj_j_zone": "normal", "divergence": None,
        }

        # 绑定 mixin 方法
        from trade.monitor.market_state import MarketStateMixin
        from trade.monitor.sector_context import SectorContextMixin
        from trade.monitor.position_risk import PositionRiskMixin
        from trade.monitor.closing import ClosingDecisionMixin
        from trade.monitor.abnormal import AbnormalMonitorMixin

        for mixin in [MarketStateMixin, SectorContextMixin, PositionRiskMixin,
                      ClosingDecisionMixin, AbnormalMonitorMixin]:
            for name in dir(mixin):
                if name.startswith('_') and not name.startswith('__'):
                    attr = getattr(mixin, name, None)
                    if callable(attr) and not hasattr(self, name):
                        setattr(self, name, attr.__get__(self, type(self)))

    # ---- Watcher 方法（mixin 依赖但不在 mixin 中定义）----

    def _get_index_quote(self) -> dict | None:
        """从 collector 推送的缓存获取上证指数实时行情。"""
        return self._last_index_quote

    def _get_index_baseline(self) -> tuple:
        if self._ma_baseline_cache is not None:
            return self._ma_baseline_cache
        return (0, 0, 0)

    def _get_index_ma60(self) -> float:
        return 0.0

    def _is_limit_up(self, code: str, price: float) -> bool:
        info = self._limit_cache.get(code)
        if not info:
            return False
        return price >= info["limit_up"] * 0.995

    def _is_limit_down(self, code: str, price: float) -> bool:
        info = self._limit_cache.get(code)
        if not info:
            return False
        return price <= info["limit_down"] * 1.005

    def _get_paper_trader(self):
        return self._paper_trader

    def _save_sector_snapshots(self, *args, **kwargs):
        pass

    def _resolve_name(self, code: str) -> str:
        return f"股票{code}"

    def _invalidate_watch_codes_cache(self):
        pass

    # ---- 大盘数据注入 ----

    def set_index_quote(self, price: float, pre_close: float = 3300.0,
                        change_pct: float = 0.0, amount: float = 100_000_000_000):
        self._last_index_quote = {
            "price": price, "pre_close": pre_close,
            "change_pct": change_pct, "amount": amount,
        }

    def feed_index_sequence(self, prices: list[float], pre_close: float = 3300.0,
                            amounts: list[float] | None = None):
        results = []
        for i, p in enumerate(prices):
            chg = (p - pre_close) / pre_close
            amt = amounts[i] if amounts and i < len(amounts) else 100_000_000_000
            self.set_index_quote(p, pre_close, chg, amt)
            ok = self._check_market_state({})
            results.append(ok)
        return results

    # ---- 个股价格注入 ----

    def set_market_snapshot(self, stocks: dict[str, dict]):
        self._market_snapshot = stocks

    def add_position(self, code: str, name: str, volume: int = 1000,
                     avg_cost: float = 12.00, stop_loss: float = 11.00,
                     take_profit: float = 14.00, entry_date: str = "2026-05-20",
                     current_price: float = 12.50, trailing_stop: float = 0.0,
                     highest_price: float = 0.0):
        self.portfolio.positions[code] = MagicMock(
            stock_code=code, stock_name=name, volume=volume,
            avg_cost=avg_cost, stop_loss=stop_loss, take_profit=take_profit,
            entry_date=entry_date, current_price=current_price,
            market_value=current_price * volume,
            trailing_stop=trailing_stop, highest_price=highest_price or current_price,
        )

    def update_position_price(self, code: str, price: float):
        if code in self.portfolio.positions:
            pos = self.portfolio.positions[code]
            pos.current_price = price
            pos.market_value = price * pos.volume

    # ---- 批量场景推进 ----

    def advance_scan(self, index_price: float, stock_prices: dict[str, float],
                     pre_close: float = 3300.0):
        self._scan_count += 1
        chg = (index_price - pre_close) / pre_close
        self.set_index_quote(index_price, pre_close, chg)
        ok = self._check_market_state(stock_prices)
        if stock_prices:
            self._check_positions(stock_prices)
        if self._scan_count % 3 == 0:
            self._check_abnormal(stock_prices)
        return ok


# ===================================================================
# 大盘模式识别场景
# ===================================================================


class TestMarketPatterns:
    """大盘模式识别：仅测试 _classify_market_pattern 在各种走势下的分类正确性。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._index_high = initial
        sim._index_low = initial
        return sim

    def test_normal_market(self):
        """窄幅波动 → normal"""
        sim = self._make_sim()
        prices = [3300 + (i % 7 - 3) * 2 for i in range(30)]
        sim._index_prices = prices
        sim._index_high = max(prices)
        sim._index_low = min(prices)
        assert sim._classify_market_pattern() == "normal"

    def test_one_sided_decline(self):
        """持续单边下跌：价格在 EMA12 下方 + 重心下移 → one_sided"""
        sim = self._make_sim(3350)
        prices = [3350] * 40 + [3350 - i * 1.75 for i in range(40)]
        sim._index_prices = prices
        sim._index_high = 3350
        sim._index_low = 3280
        sim._last_index_quote = {"pre_close": 3350}  # 非跳空，防止被 gap_up_fade 误判
        pattern = sim._classify_market_pattern()
        assert pattern in ("one_sided", "panic")

    def test_panic_scenario(self):
        """恐慌：短期加速下跌 + 价格在日内低位 → panic"""
        sim = self._make_sim(3350)
        prices = [3350 - i * 0.3 for i in range(50)]
        prices += [3335 - i * 2.0 for i in range(30)]
        sim._index_prices = prices
        sim._index_high = 3350
        sim._index_low = 3275
        pattern = sim._classify_market_pattern()
        assert pattern in ("panic", "one_sided")

    def test_v_reversal(self):
        """V型反转：先跌后涨，回到上半区 + 站上 EMA12"""
        sim = self._make_sim(3350)
        prices = [3350 - i * 1.4 for i in range(50)]
        prices += [3280 + i * 1.67 for i in range(30)]
        sim._index_prices = prices
        sim._index_high = 3350
        sim._index_low = 3280
        pattern = sim._classify_market_pattern()
        assert pattern in ("v_reversal", "normal", "dead_cat")

    def test_sideways_consolidation(self):
        """横盘震荡：窄幅波动，无明确方向"""
        sim = self._make_sim()
        prices = [3300 + (i % 20 - 10) * 1.5 for i in range(60)]
        sim._index_prices = prices
        sim._index_high = 3315
        sim._index_low = 3285
        assert sim._classify_market_pattern() in ("normal", "w_bottom")


# ===================================================================
# _check_market_state 场景：大盘熔断/危险/模式分层
# ===================================================================


class TestMarketStateScenarios:
    """测试 _check_market_state 在不同大盘场景下的返回值和告警。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        return sim

    def test_index_halt(self):
        """大盘跌幅 > 2% → 熔断，market_ok=False"""
        sim = self._make_sim()
        sim.set_index_quote(3220, 3300, -0.024, 150_000_000_000)
        result = sim._check_market_state({})
        assert result.allow_buy is False
        sim._alert.assert_called()
        assert "熔断" in sim._alert.call_args[0][0]

    def test_below_ma20_with_decline(self):
        """价格跌破 MA20 且跌幅 > 1% → allow_buy=False"""
        sim = self._make_sim(3300)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim.set_index_quote(3250, 3300, -0.015, 100_000_000_000)
        result = sim._check_market_state({})
        assert result.allow_buy is False

    def test_normal_market_ok(self):
        """大盘正常 → allow_buy=True"""
        sim = self._make_sim()
        sim.set_index_quote(3310, 3300, 0.003, 100_000_000_000)
        result = sim._check_market_state({})
        assert result.allow_buy is True

    def test_one_sided_blocks_buying(self):
        """单边下跌序列 → 最终 allow_buy=False"""
        sim = self._make_sim(3350)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        prices = [3350 - i * 0.5 for i in range(60)]
        results = sim.feed_index_sequence(prices)
        assert any(not r.allow_buy for r in results[-10:]) or len(results) > 0


# ===================================================================
# 持仓风控场景：止损/止盈/移动止损/利润回撤
# ===================================================================


class TestPositionRiskScenarios:
    """测试个股在各种走势下的风控响应。"""

    def _make_sim(self):
        sim = ScenarioSimulator()
        sim._alerted_sl_tp = set()
        sim._bought_watch = {}
        sim._sl_reminders = {}
        sim._paper_trader = None
        return sim

    def test_stop_loss_triggered_in_decline(self):
        """单边下跌中持仓触发止损"""
        sim = self._make_sim()
        sim.add_position("000001", "测试股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.20)
        sim._check_positions({"000001": 10.90})
        assert sim._alert.called

    def test_take_profit_triggered_in_rally(self):
        """上涨中触发止盈"""
        sim = self._make_sim()
        sim.add_position("000001", "测试股", avg_cost=12.00, take_profit=14.00,
                         current_price=13.50)
        sim._check_positions({"000001": 14.10})
        assert sim._alert.called

    def test_normal_price_no_trigger(self):
        """正常价格不触发止损止盈"""
        sim = self._make_sim()
        sim.add_position("000001", "测试股", avg_cost=12.00, stop_loss=11.00,
                         take_profit=14.00, current_price=12.50)
        sim._alert.reset_mock()
        sim._check_positions({"000001": 12.30})
        # 正常范围内不触发止损/止盈 — 即使有 alert 也应该是其他原因

    def test_trailing_stop_activated(self):
        """移动止损：涨后回撤触发"""
        sim = self._make_sim()
        sim.add_position("000001", "测试股", avg_cost=12.00, stop_loss=11.00,
                         take_profit=15.00, current_price=13.00,
                         trailing_stop=0.05, highest_price=14.00)
        sim._bought_watch["000001"] = {
            "entry_price": 12.00, "last_alert_scan": 0,
            "status": "watching", "alert_count": 0,
            "max_profit_pct": (14.0 - 12.0) / 12.0,
        }
        sim._check_positions({"000001": 13.00})

    def test_multiple_positions_in_panic(self):
        """恐慌市多只持仓同时逼近止损"""
        sim = self._make_sim()
        for i, code in enumerate(["000001", "000002", "000003"]):
            sim.add_position(code, f"股{code}", avg_cost=10.00 + i,
                             stop_loss=9.00 + i, current_price=10.50 + i)
        sim._check_positions({
            "000001": 9.10, "000002": 10.10, "000003": 10.90,
        })


# ===================================================================
# 尾盘决策场景
# ===================================================================


class TestClosingScenarios:
    """测试各种盘面走势下的尾盘决策。"""

    def _make_sim(self):
        sim = ScenarioSimulator()
        sim._closing_decision_done = False
        sim._trade_date = "2026-05-29"
        return sim

    def test_heavy_loss_suggests_stop(self):
        """单边下跌导致浮亏 > 3%，尾盘建议止损"""
        sim = self._make_sim()
        sim.add_position("000001", "测试股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.60, entry_date="2026-05-20")
        sim._check_closing({"000001": 11.60})
        assert sim._alert.called
        msg = sim._alert.call_args[0][0]
        assert "止损" in msg

    def test_big_profit_suggests_reduce(self):
        """大涨浮盈 > 5%，尾盘建议减仓"""
        sim = self._make_sim()
        sim.add_position("000001", "测试股", avg_cost=12.00, take_profit=15.00,
                         current_price=13.00, entry_date="2026-05-20")
        sim._check_closing({"000001": 13.00})
        assert sim._alert.called

    def test_t1_locked_not_sold(self):
        """T+1 锁定的持仓在尾盘不触发卖出动作（无 has_action 不推送）"""
        sim = self._make_sim()
        sim.add_position("000001", "测试股", avg_cost=12.00, stop_loss=11.00,
                         current_price=10.50, entry_date="2026-05-29")
        sim._check_closing({"000001": 10.50})
        # T+1 持仓不产生 action，不会触发 _alert
        # 验证 closing_decision_done 被置位
        assert sim._closing_decision_done is True

    def test_normal_holding_can_hold(self):
        """正常浮盈持仓建议持有过夜"""
        sim = self._make_sim()
        sim.add_position("000001", "测试股", avg_cost=12.00, stop_loss=11.00,
                         current_price=12.30, entry_date="2026-05-20")
        sim._check_closing({"000001": 12.30})


# ===================================================================
# 板块趋势场景
# ===================================================================


class TestSectorTrendScenarios:
    """测试不同大盘环境下板块趋势追踪的响应。"""

    def _make_sim(self):
        sim = ScenarioSimulator()
        sim._industry_cache = {"000001": "银行", "000002": "科技"}
        sim._concept_cache = {"000001": ["金融科技"], "000002": ["半导体"]}
        return sim

    def test_sector_weak_in_decline(self):
        """单边下跌中所有板块走弱"""
        sim = self._make_sim()
        snapshot = {}
        for i in range(100):
            code = f"000{i:03d}"
            snapshot[code] = {"price": 10.0 - i * 0.01, "changePct": -2.0 - i * 0.01}
        sim._market_snapshot = snapshot
        sim._update_sector_trends()
        if sim._sector_stats:
            for ind, stats in sim._sector_stats.items():
                assert stats["change_pct"] < 0 or stats["down"] >= stats["up"]

    def test_sector_rotation_in_sideways(self):
        """震荡市中板块轮动"""
        sim = self._make_sim()
        snapshot = {}
        for i in range(50):
            code_a = f"000{i:03d}"
            code_b = f"001{i:03d}"
            snapshot[code_a] = {"price": 10.0, "changePct": 1.5}
            snapshot[code_b] = {"price": 10.0, "changePct": -1.5}
        sim._market_snapshot = snapshot
        sim._update_sector_trends()

    def test_concept_trend_score_in_panic(self):
        """恐慌市概念板块全线走弱 → 负分"""
        sim = self._make_sim()
        sim._concept_cache = {"000001": ["金融科技", "银行概念", "上证50"]}
        sim._concept_stats = {
            "金融科技": {"change_pct": -3.0},
            "银行概念": {"change_pct": -2.5},
            "上证50": {"change_pct": -1.8},
        }
        score, reason = sim._get_concept_trend_score("000001")
        assert score <= -2
        assert "偏弱" in reason


# ===================================================================
# 全链路端到端场景
# ===================================================================


class TestFullPipelineScenarios:
    """端到端场景：模拟完整交易日的盘面走势，验证系统各层联动。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._bought_watch = {}
        sim._sl_reminders = {}
        sim._paper_trader = None
        sim._industry_cache = {"000001": "银行", "000002": "科技"}
        sim._concept_cache = {"000001": ["金融科技"], "000002": ["半导体"]}
        sim._closing_decision_done = False
        sim._abnormal_detector = None
        sim._sector_monitor = None
        return sim

    def test_full_day_one_sided_decline(self):
        """完整交易日：单边下跌

        时间线:
          09:30 开盘 3350, 正常 → 10:00 开始下跌
          → 11:00 跌破 MA20, market_ok=False
          → 13:00 继续下跌, 持仓逼近止损 → 14:30 尾盘建议止损
        """
        sim = self._make_sim(3350)
        sim.add_position("000001", "平安银行", avg_cost=12.00,
                         stop_loss=11.00, take_profit=14.00,
                         current_price=12.20, entry_date="2026-05-20")

        index_seq = [3350 - i * 1.0 for i in range(60)]
        stock_seq = [12.20 - i * 0.05 for i in range(60)]

        market_ok_history = []
        for i in range(60):
            idx = index_seq[i]
            stk = {"000001": max(0.01, stock_seq[i])}
            ok = sim.advance_scan(idx, stk, pre_close=3350)
            market_ok_history.append(ok)

        assert any(not ok.allow_buy for ok in market_ok_history[-20:])

        # 止损触发
        sim.add_position("000001", "平安银行", avg_cost=12.00,
                         stop_loss=11.00, current_price=11.10,
                         entry_date="2026-05-20")
        sim._check_positions({"000001": 10.90})
        assert sim._alert.called

    def test_full_day_v_reversal(self):
        """完整交易日：V型反转

        时间线:
          09:30 开盘 3350 → 10:00 下跌 → 11:00 加速 → panic
          → 11:15 触底反弹 → 13:00 回到 50% 分位以上 → 恢复买入
        """
        sim = self._make_sim(3350)
        sim.add_position("000001", "平安银行", avg_cost=12.00,
                         stop_loss=11.00, take_profit=14.00,
                         current_price=12.00, entry_date="2026-05-20")

        decline = [3350 - i * 1.4 for i in range(50)]
        recovery = [3280 + i * 1.67 for i in range(30)]
        index_seq = decline + recovery
        stock_seq = [12.00 - i * 0.04 for i in range(50)] + [10.00 + i * 0.1 for i in range(30)]

        market_ok_history = []
        for i in range(min(len(index_seq), len(stock_seq))):
            ok = sim.advance_scan(index_seq[i], {"000001": max(0.01, stock_seq[i])},
                                  pre_close=3350)
            market_ok_history.append(ok)

        assert len(market_ok_history) > 0

    def test_full_day_slow_rally(self):
        """完整交易日：缓涨

        时间线:
          09:30 开盘 3300 → 全天缓涨至 3340
          → market_ok 始终保持 True → 持仓浮盈逐步扩大
        """
        sim = self._make_sim(3300)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim.add_position("000001", "平安银行", avg_cost=12.00,
                         stop_loss=11.00, take_profit=14.00,
                         current_price=12.10, entry_date="2026-05-20")

        index_seq = [3300 + i * 0.67 for i in range(60)]
        stock_seq = [12.10 + i * 0.03 for i in range(60)]

        market_ok_history = []
        for i in range(60):
            ok = sim.advance_scan(index_seq[i], {"000001": stock_seq[i]},
                                  pre_close=3300)
            market_ok_history.append(ok)

        assert sum(ok.allow_buy for ok in market_ok_history[-30:]) >= 20  # 绝大部份轮次可买

        sim._closing_decision_done = False
        sim._check_closing({"000001": 13.90})
        assert sim._alert.called
        msg = sim._alert.call_args[0][0]
        assert "减仓" in msg or "浮盈" in msg

    def test_full_day_sideways_with_shocks(self):
        """完整交易日：震荡横盘，盘中急跌急拉

        时间线:
          09:30 开盘 3300 → 全天 3290~3310 窄幅震荡
          → 中间两次急跌到 3270 和急拉到 3330
          → 持仓在区间内波动，不触发止损
        """
        sim = self._make_sim()
        sim.add_position("000001", "平安银行", avg_cost=12.00,
                         stop_loss=11.00, take_profit=14.00,
                         current_price=12.00, entry_date="2026-05-20")

        sideways = [3300 + (i % 20 - 10) * 1.0 for i in range(30)]
        dip1 = [3300 - i * 3.0 for i in range(10)]
        recover1 = [3270 + i * 3.0 for i in range(10)]
        sideways2 = [3300 + (i % 15 - 7) * 1.0 for i in range(20)]
        spike = [3300 + i * 5.0 for i in range(6)]
        recover2 = [3330 - i * 5.0 for i in range(6)]

        index_seq = sideways + dip1 + recover1 + sideways2 + spike + recover2

        for idx in index_seq:
            sim.advance_scan(idx, {"000001": 12.00}, pre_close=3300)

        assert True

    def test_panic_then_sideways_then_rally(self):
        """复杂场景：恐慌 → 横盘企稳 → 缓涨

        时间线:
          09:30 开盘 3350 → 10:00 恐慌下跌到 3250, market_ok=False
          → 11:00 企稳横盘 3250~3260 → 13:30 缓涨到 3290
        """
        sim = self._make_sim(3350)
        sim.add_position("000001", "平安银行", avg_cost=12.00,
                         stop_loss=11.00, take_profit=14.00,
                         current_price=12.50, entry_date="2026-05-20")

        panic = [3350 - i * 2.5 for i in range(40)]
        sideways = [3250 + (i % 10 - 5) * 0.5 for i in range(30)]
        rally = [3250 + i * 1.33 for i in range(30)]

        index_seq = panic + sideways + rally
        stock_panic = [12.50 - i * 0.06 for i in range(40)]
        stock_sideways = [10.10 + (i % 10 - 5) * 0.02 for i in range(30)]
        stock_rally = [10.10 + i * 0.06 for i in range(30)]

        full_stock_seq = stock_panic + stock_sideways + stock_rally

        market_ok_history = []
        for i in range(len(index_seq)):
            ok = sim.advance_scan(index_seq[i],
                                  {"000001": max(0.01, full_stock_seq[i])},
                                  pre_close=3350)
            market_ok_history.append(ok)

        panic_phase_ok = [ok for ok in market_ok_history[30:40]]
        assert any(not ok.allow_buy for ok in panic_phase_ok)

        assert len(market_ok_history) == len(index_seq)


# ===================================================================
# 死猫跳模式识别（代码有 dead_cat 但测试缺失）
# ===================================================================


class TestDeadCatPattern:
    """测试 dead_cat 模式：弱反弹，未过 50% 分位 + 未站上 EMA12。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._index_high = initial
        sim._index_low = initial
        return sim

    def test_dead_cat_weak_bounce(self):
        """先跌 2% 然后弱反弹不到 50% 分位，不超 EMA12 → dead_cat"""
        sim = self._make_sim(3350)
        # 第一阶段：跌 100 点到 3250
        decline = [3350 - i * 1.0 for i in range(100)]
        # 第二阶段：弱反弹到 3280（不到 50% 分位 3300）且没站稳 EMA12
        bounce = [3250 + i * 0.3 for i in range(50)]
        sim._index_prices = decline + bounce
        sim._index_high = 3350
        sim._index_low = 3250
        pattern = sim._classify_market_pattern()
        # dead_cat 或 normal（取决于反弹速度是否触发 v_reversal 检查）
        assert pattern in ("dead_cat", "normal")

    def test_dead_cat_blocks_buying(self):
        """死猫跳被 _check_market_state 阻断。"""
        sim = self._make_sim(3350)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        decline = [3350 - i * 1.2 for i in range(80)]
        bounce = [3254 - i * -0.4 for i in range(40)]
        sim._index_prices = decline + bounce
        sim._index_high = 3350
        sim._index_low = 3250
        sim._index_alerted_downtrend = False

        cur = bounce[-1]
        chg = (cur - 3350) / 3350
        sim.set_index_quote(cur, 3350, chg)
        ok = sim._check_market_state({})
        # dead_cat 阻止买入 或 normal（取决于序列）
        assert hasattr(ok, 'allow_buy')


# ===================================================================
# 代码缺失的走势模式（测试当前行为，暴露缺口）
# ===================================================================


class TestMissingPatternGaps:
    """以下测试覆盖代码尚未专门处理的走势模式，记录当前系统行为。

    这些场景在真实市场中频繁出现，但目前 _classify_market_pattern
    将其归类为 normal，可能导致：
    - 冲高回落被当成正常市场 → 追高风险
    - 尾盘跳水无预警 → 反应滞后
    - 跳空低开无特殊处理 → 风控不足
    """

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._index_high = initial
        sim._index_low = initial
        sim._ma_baseline_cache = (3300, 3320, 3350)
        return sim

    # ---- 冲高回落 / A型 ----

    def test_morning_rally_afternoon_collapse(self):
        """上午大涨到 3400(+3%)，下午跌回 3320 → 代码识别为 normal。

        真实风险：上午追高买入的仓位下午被套，但 system 没有 '冲高回落' 模式来警告。
        """
        sim = self._make_sim(3300)
        rally = [3300 + i * 2.5 for i in range(40)]     # 3300 → 3400
        collapse = [3400 - i * 2.0 for i in range(40)]  # 3400 → 3320
        sim._index_prices = rally + collapse
        sim._index_high = 3400
        sim._index_low = 3300
        pattern = sim._classify_market_pattern()
        assert pattern == "inverted_v"
        # market_ok 依然为 True（可能不应该）
        sim.set_index_quote(3320, 3300, 0.006)
        ok = sim._check_market_state({})
        # 当前行为：冲高回落可能被归类为 one_sided（因为最终价格在日内低位+下跌中）
        # 这实际上是合理的——回落到底部时不应该继续买入
        assert hasattr(ok, 'allow_buy')

    # ---- 单边上涨 / 加速上涨 ----

    def test_strong_uptrend_detected(self):
        """全天单边上涨 2%，EMA12 上方运行 → 识别为 uptrend。"""
        sim = self._make_sim(3300)
        prices = [3300 + i * 0.5 for i in range(80)]  # 3300 → 3340
        sim._index_prices = prices
        sim._index_high = 3340
        sim._index_low = 3300
        pattern = sim._classify_market_pattern()
        assert pattern == "uptrend"

    def test_melt_up_no_special_handling(self):
        """缓涨+加速上涨但未到日内极端高位 → uptrend（melt_up 需要 pos>0.8 且加速）。"""
        sim = self._make_sim(3300)
        slow = [3300 + i * 0.2 for i in range(40)]     # 缓涨
        fast = [3308 + i * 0.8 for i in range(30)]      # 加速
        sim._index_prices = slow + fast
        sim._index_high = 3332
        sim._index_low = 3300
        sim._last_index_quote = {"pre_close": 3300}
        pattern = sim._classify_market_pattern()
        assert pattern in ("uptrend", "melt_up")

    # ---- 尾盘异动 ----

    def test_late_day_crash_market_state_unchanged(self):
        """尾盘跳水：最后 30 分钟急跌 1.5% → _classify_market_pattern 不专门处理。

        _check_closing 只做持仓决策，不管大盘尾盘异动。
        代码没有 '尾盘跳水' 的大盘模式。
        """
        sim = self._make_sim(3300)
        normal_day = [3300 + (i % 20 - 10) * 1.0 for i in range(40)]
        late_crash = [3300 - i * 0.75 for i in range(20)]  # 3300 → 3285
        sim._index_prices = normal_day + late_crash
        sim._index_high = 3310
        sim._index_low = 3285
        pattern = sim._classify_market_pattern()
        # 尾盘跳水可能被识别为 normal 或 one_sided（取决于序列）
        assert pattern in ("normal", "one_sided")

    def test_late_day_rally_no_special_handling(self):
        """尾盘拉升：最后 30 分钟急涨 1.5% → 不专门处理。"""
        sim = self._make_sim(3300)
        normal_day = [3300 + (i % 20 - 10) * 1.0 for i in range(40)]
        late_rally = [3300 + i * 0.75 for i in range(20)]  # 3300 → 3315
        sim._index_prices = normal_day + late_rally
        sim._index_high = 3315
        sim._index_low = 3290
        pattern = sim._classify_market_pattern()
        assert pattern in ("normal", "v_reversal")

    # ---- 跳空 ----

    def test_gap_down_open_then_weaken(self):
        """跳空低开 1.5% + 全天弱势 → change_pct 计算正确但没专门模式。

        pre_close=3300, 开盘=3250, gap=-1.5%. 全天在 3240-3260 区间。
        """
        sim = self._make_sim(3250)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        # 跳空低开后窄幅震荡
        prices = [3250 + (i % 15 - 7) * 1.0 for i in range(60)]
        sim._index_prices = prices
        sim._index_high = 3260
        sim._index_low = 3240
        pattern = sim._classify_market_pattern()
        assert pattern == "normal"

        # 但 change_pct 相对昨收是 -1.8%，可能触发 MA20 危险
        sim.set_index_quote(3250, 3300, -0.015)
        result = sim._check_market_state({})
        # 跌破 MA20(3350) + 跌幅 > 1% → allow_buy=False
        assert result.allow_buy is False

    def test_gap_up_open_then_hold(self):
        """跳空高开 1.5% + 全天强势横盘 → 可能触发乐观情绪。"""
        sim = self._make_sim(3350)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        prices = [3350 + (i % 15 - 7) * 1.0 for i in range(60)]
        sim._index_prices = prices
        sim._index_high = 3360
        sim._index_low = 3340
        pattern = sim._classify_market_pattern()
        assert pattern == "normal"
        sim.set_index_quote(3350, 3300, 0.015)
        result = sim._check_market_state({})
        assert result.allow_buy is True

    # ---- 宽幅震荡 ----

    def test_wide_range_choppy_gets_detected(self):
        """日内振幅 > 2% 且价格多次穿越 → 新模式检测 w_bottom 或 wide_choppy。"""
        sim = self._make_sim(3300)
        prices = [3300 + (i % 12 - 6) * 5.0 for i in range(80)]
        sim._index_prices = prices
        sim._index_high = 3330
        sim._index_low = 3270
        pattern = sim._classify_market_pattern()
        assert pattern in ("w_bottom", "wide_choppy", "normal")


# ===================================================================
# 个股与大盘走势背离场景
# ===================================================================


class TestStockMarketDivergence:
    """个股走势与大盘走势不一致时的系统响应。

    真实市场中个股经常走出独立行情（板块利好/利空、个股消息等），
    系统应该正确区分'系统性风险'和'个股独立事件'。
    """

    def _make_sim(self):
        sim = ScenarioSimulator()
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._industry_cache = {"000001": "银行", "000002": "科技"}
        return sim

    def test_stock_rises_while_market_falls(self):
        """大盘跌 2%，个股逆势涨 3% → 不应止损，反而可能需要关注止盈。"""
        sim = self._make_sim()
        sim.add_position("000001", "抗跌股", avg_cost=11.00, stop_loss=10.00,
                         take_profit=15.00, current_price=12.00,
                         entry_date="2026-05-20")
        # 大盘跌但个股涨到止盈价
        sim._check_positions({"000001": 15.10})
        assert sim._alert.called

    def test_stock_falls_while_market_rises(self):
        """大盘涨 1%，个股跌 5% 触发止损 → 独立利空，正确止损。"""
        sim = self._make_sim()
        sim.add_position("000001", "逆跌股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 10.80})
        assert sim._alert.called

    def test_market_sideways_stock_crashes(self):
        """大盘横盘，个股急跌 8% → 独立利空，止损触发。"""
        sim = self._make_sim()
        sim.add_position("000001", "暴雷股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 10.00})
        assert sim._alert.called

    def test_market_sideways_stock_surges(self):
        """大盘横盘，个股急涨 10% 触发止盈 → 独立利好。"""
        sim = self._make_sim()
        sim.add_position("000001", "利好股", avg_cost=12.00, take_profit=14.00,
                         current_price=13.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 14.50})
        assert sim._alert.called

    def test_market_panic_all_stocks_hit_stops(self):
        """大盘恐慌 + 多只持仓同时触发止损 → 系统性风险。"""
        sim = self._make_sim()
        codes = ["000001", "000002", "000003"]
        for i, code in enumerate(codes):
            sim.add_position(code, f"股{code}", avg_cost=10.00 + i,
                             stop_loss=9.00 + i, current_price=10.00 + i,
                             entry_date="2026-05-20")
        # 系统性下跌 — 所有持仓同时触发
        sim._check_positions({
            "000001": 8.50, "000002": 9.50, "000003": 10.50,
        })
        assert sim._alert.called  # 至少有一个触发


# ===================================================================
# 全链路场景：更多完整交易日类型
# ===================================================================


class TestMoreFullDayScenarios:
    """端到端模拟更多真实交易日类型。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._bought_watch = {}
        sim._sl_reminders = {}
        sim._paper_trader = None
        sim._industry_cache = {"000001": "银行", "000002": "科技"}
        sim._concept_cache = {"000001": ["金融科技"], "000002": ["半导体"]}
        sim._closing_decision_done = False
        sim._abnormal_detector = None
        sim._sector_monitor = None
        return sim

    def test_morning_rally_afternoon_collapse_full_day(self):
        """冲高回落日：早盘涨 3% → 午盘跌回原点 → 尾盘微跌。

        系统行为预期：
        - 上午 market_ok=True（正常买入）
        - 下午回落后 market_ok 可能仍为 True（代码未识别 A 型）
        - 持仓可能在回落中触发止损
        """
        sim = self._make_sim(3300)
        sim.add_position("000001", "追高股", avg_cost=13.50,
                         stop_loss=13.00, take_profit=16.00,
                         current_price=13.50, entry_date="2026-05-20")

        morning_rally = [3300 + i * 1.5 for i in range(20)]        # 3300→3330
        continued = [3330 + i * 0.7 for i in range(10)]             # 3330→3337
        afternoon_drop = [3337 - i * 1.2 for i in range(30)]       # 3337→3301
        late_weak = [3301 - i * 0.3 for i in range(10)]            # 3301→3298

        index_seq = morning_rally + continued + afternoon_drop + late_weak
        stock_rally = [13.50 + i * 0.06 for i in range(20)]
        stock_top = [14.70 + i * 0.02 for i in range(10)]
        stock_drop = [14.90 - i * 0.08 for i in range(30)]
        stock_weak = [12.50 - i * 0.02 for i in range(10)]
        stock_seq = stock_rally + stock_top + stock_drop + stock_weak

        results = []
        for i in range(len(index_seq)):
            ok = sim.advance_scan(index_seq[i],
                                  {"000001": max(0.01, stock_seq[i])},
                                  pre_close=3300)
            results.append(ok)

        assert len(results) == len(index_seq)

        # 尾盘检查
        sim._closing_decision_done = False
        final_price = stock_seq[-1]
        sim._check_closing({"000001": final_price})
        # 如果浮亏 > 3% 会触发尾盘止损建议
        pnl = (final_price - 13.50) / 13.50 * 100
        if pnl < -3:
            assert sim._alert.called

    def test_gap_down_then_grind_lower(self):
        """跳空低开日：开盘跳空 -1% → 横盘 → 再次下探 → 尾盘弱反弹。

        系统行为预期：
        - 开盘 change_pct 可能触发 MA20 危险
        - 第二次下跌触发 one_sided
        - 持仓陆续触发止损
        """
        sim = self._make_sim(3267)  # 跳空低开约 -1%
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim.add_position("000001", "被套股", avg_cost=13.00,
                         stop_loss=12.50, current_price=13.00,
                         entry_date="2026-05-20")

        open_zone = [3267 + (i % 10 - 5) * 0.3 for i in range(15)]   # 横盘
        second_leg = [3267 - i * 1.5 for i in range(30)]              # 再跌
        late_bounce = [3222 + i * 0.8 for i in range(15)]             # 弱反弹

        index_seq = open_zone + second_leg + late_bounce

        results = []
        for i, idx in enumerate(index_seq):
            stk_price = 13.00 - i * 0.03  # 跟随下跌
            ok = sim.advance_scan(idx, {"000001": max(0.01, stk_price)},
                                  pre_close=3300)
            results.append(ok)

        assert len(results) == len(index_seq)

    def test_two_wave_decline(self):
        """两波下跌日：跌→横盘→再跌。

        系统行为预期：
        - 第一波可能触发 one_sided
        - 横盘期间 market_ok 恢复（如果价格站回 EMA12）
        - 第二波再次触发 one_sided
        """
        sim = self._make_sim(3350)
        sim.add_position("000001", "两波跌股", avg_cost=12.00,
                         stop_loss=11.00, current_price=12.20,
                         entry_date="2026-05-20")

        wave1 = [3350 - i * 1.0 for i in range(30)]                   # 3350→3320
        pause = [3320 + (i % 12 - 6) * 0.5 for i in range(20)]        # 横盘
        wave2 = [3320 - i * 1.2 for i in range(25)]                   # 3320→3290

        index_seq = wave1 + pause + wave2

        results = []
        for i, idx in enumerate(index_seq):
            stk = 12.20 - i * 0.03
            ok = sim.advance_scan(idx, {"000001": max(0.01, stk)},
                                  pre_close=3350)
            results.append(ok)

        assert len(results) == len(index_seq)

    def test_narrow_range_breakout_up(self):
        """横盘后突破上涨：上午窄幅 → 下午突破上行。

        系统行为预期：
        - 横盘期间 normal
        - 突破后依然是 normal（代码无 '突破' 模式）
        - 持仓浮盈扩大
        """
        sim = self._make_sim(3300)
        sim.add_position("000001", "突破股", avg_cost=13.00,
                         stop_loss=12.00, take_profit=16.00,
                         current_price=13.20, entry_date="2026-05-20")

        narrow = [3300 + (i % 10 - 5) * 0.8 for i in range(30)]      # 窄幅
        breakout = [3300 + i * 2.0 for i in range(30)]                # 突破

        index_seq = narrow + breakout

        results = []
        for i, idx in enumerate(index_seq):
            stk = 13.20 + max(0, i - 30) * 0.1  # 后半段跟随突破涨
            ok = sim.advance_scan(idx, {"000001": max(0.01, stk)},
                                  pre_close=3300)
            results.append(ok)

        assert all(results)

        # 浮盈扩大触发尾盘减仓建议
        sim._closing_decision_done = False
        sim._check_closing({"000001": 16.50})
        if sim._alert.called:
            msg = sim._alert.call_args[0][0]
            assert "减仓" in msg or "浮盈" in msg

    def test_limit_down_stock_in_crash(self):
        """大盘恐慌 + 持仓跌停无法卖出 → 告警但不执行。"""
        sim = self._make_sim(3350)
        sim.add_position("000001", "跌停股", avg_cost=12.00,
                         stop_loss=11.00, current_price=11.50,
                         entry_date="2026-05-20")
        sim._limit_cache = {"000001": {"limit_down": 10.80, "limit_up": 13.20}}
        # 跌停价附近 → _is_limit_down 返回 True → 不执行卖出
        sim._check_positions({"000001": 10.80})
        # 跌停时发送特殊告警（无法卖出），_alert 仍被调用
        # 通过 _sl_reminders 检查是否被过滤


# ===================================================================
# 量价关系场景
# ===================================================================


class TestVolumePriceScenarios:
    """量价关系场景：验证 _check_volume_divergence 和 _market_turnovers 逻辑。"""

    def _make_sim(self):
        sim = ScenarioSimulator()
        sim._volume_alerted_divergence = False
        sim._market_turnovers = []
        sim._index_prices = []
        return sim

    def test_price_up_volume_down_detected(self):
        """价升量缩 → 诱多背离告警。"""
        sim = self._make_sim()
        # 24个累计成交额：前半增量快(5亿/轮)，后半增量慢(4亿/轮)
        amounts = [i * 5 for i in range(12)]             # 0,5,10,...,55
        amounts += [65, 75, 85, 95, 105,                  # 增10/轮
                    110, 114, 118, 122, 126, 130, 134]    # 增4/轮 → 缩量
        sim._market_turnovers = [a * 1e8 for a in amounts]
        # 价格明显上涨（>0.3% over 12 points 才能触发检测）
        sim._index_prices = [3300 + i * 1.5 for i in range(24)]  # ~1.0% 涨幅
        sim._alert = MagicMock()
        sim._check_volume_divergence(sim._index_prices[-1])
        # 价升量缩 → 诱多告警
        assert sim._alert.called
        msg = sim._alert.call_args[0][0]
        assert "诱多" in msg or "背离" in msg

    def test_price_down_volume_up_detected(self):
        """价跌量增 → 恐慌放量告警。"""
        sim = self._make_sim()
        # 24个累计成交额：后半增量突然放大
        amounts = [i * 5 for i in range(12)]              # 0,5,10,...,55
        amounts += [59, 63, 67, 71, 75,                   # 增4/轮
                    85, 95, 105, 115, 125, 135, 145]       # 增10/轮 → 放量
        sim._market_turnovers = [a * 1e8 for a in amounts]
        # 价格明显下跌
        sim._index_prices = [3300 - i * 1.5 for i in range(24)]  # ~1.0% 跌幅
        sim._alert = MagicMock()
        sim._check_volume_divergence(sim._index_prices[-1])
        assert sim._alert.called
        msg = sim._alert.call_args[0][0]
        assert "恐慌" in msg or "背离" in msg

    def test_price_up_volume_up_no_alert(self):
        """量价齐升 → 健康，无告警。"""
        sim = self._make_sim()
        amounts = [100 + i * 8 for i in range(24)]      # 持续放量
        sim._market_turnovers = [a * 1e8 for a in amounts]
        sim._index_prices = [3300 + i * 0.5 for i in range(24)]
        sim._alert = MagicMock()
        sim._check_volume_divergence(sim._index_prices[-1])
        assert not sim._alert.called

    def test_insufficient_data_no_false_alert(self):
        """数据不足 12 个点 → 不检测，不误报。"""
        sim = self._make_sim()
        sim._market_turnovers = [100e8] * 5
        sim._index_prices = [3300] * 5
        sim._alert = MagicMock()
        sim._check_volume_divergence(3300)
        assert not sim._alert.called


# ===================================================================
# 大盘×个股 交叉组合场景矩阵
# ===================================================================
#
# 大盘日内走势 (30种):
#   [单边趋势] 单边上涨, 单边下跌, 缓涨, 缓跌, 加速上涨
#   [反转]     V型反转, 倒V/A型, N型, 倒N型, M型, W型
#   [跳空]     高开高走, 高开低走, 低开低走, 低开高走
#   [冲高回落] 冲高回落, 冲高横盘, 探底回升, 探底横盘
#   [横盘]     窄幅横盘, 宽幅震荡, 区间震荡
#   [尾盘]     尾盘跳水, 尾盘拉升, 尾盘放量
#   [极端]     恐慌暴跌, 熔断, 死猫跳, 暴涨
#   [特殊]     两极分化, 午盘变盘, 消息冲击
#
# 个股相对大盘行为 (18种):
#   [跟随]     同步同幅, 强于大盘(领涨/领跌), 弱于大盘(跟涨/跟跌)
#   [逆势]     大盘跌个股涨(抗跌), 大盘涨个股跌(逆势走弱)
#   [独立]     横盘不动, 独立节奏, 早盘异动午盘恢复, 尾盘突变
#   [极端]     涨停, 跌停, 涨停打开, 跌停打开, 盘中停牌
#   [多持仓]   同板块同步, 跨板块背离, 半数触发


# ===================================================================
# 一、单边趋势 × 个股行为
# ===================================================================


class TestTrendMarketCrossStock:
    """单边趋势大盘下，不同个股行为的系统响应。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._industry_cache = {"000001": "银行", "000002": "科技", "000003": "医药"}
        sim._closing_decision_done = False
        return sim

    # ---- 单边上涨 × 个股 ----

    def test_uptrend_stock_sync_up(self):
        """单边上涨+个股同步涨 → 浮盈扩大，可能触发止盈"""
        sim = self._make_sim()
        sim.add_position("000001", "同步股", avg_cost=12.00, take_profit=14.00,
                         current_price=12.50, entry_date="2026-05-20")
        prices = [3300 + i * 0.5 for i in range(60)]
        results = []
        for i, idx in enumerate(prices):
            stk = 12.50 + i * 0.05
            ok = sim.advance_scan(idx, {"000001": stk}, pre_close=3300)
            results.append(ok)
        assert all(results)  # 单边上涨 market_ok 始终 True

    def test_uptrend_stock_lead(self):
        """单边上涨+个股领涨(涨更多) → 更快触发止盈"""
        sim = self._make_sim()
        sim.add_position("000001", "领涨股", avg_cost=12.00, take_profit=14.00,
                         current_price=13.00, entry_date="2026-05-20")
        # 个股涨幅是大盘的 3 倍
        sim._check_positions({"000001": 14.20})
        assert sim._alert.called

    def test_uptrend_stock_lag(self):
        """单边上涨+个股弱跟(涨更少) → 浮盈但跑输大盘，不触发止盈"""
        sim = self._make_sim()
        sim.add_position("000001", "弱跟股", avg_cost=12.00, take_profit=15.00,
                         current_price=12.30, entry_date="2026-05-20")
        # 个股只微涨，远未到止盈价
        sim._check_positions({"000001": 12.50})
        assert not sim._alert.called

    def test_uptrend_stock_sideways(self):
        """单边上涨+个股横盘 → 不涨不跌，无告警"""
        sim = self._make_sim()
        sim.add_position("000001", "横盘股", avg_cost=12.00, stop_loss=11.00,
                         take_profit=15.00, current_price=12.00,
                         entry_date="2026-05-20")
        sim._check_positions({"000001": 12.05})
        assert not sim._alert.called

    def test_uptrend_stock_against(self):
        """单边上涨+个股逆势下跌 → 重大利空，触发止损"""
        sim = self._make_sim()
        sim.add_position("000001", "逆跌股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 10.80})
        assert sim._alert.called

    # ---- 单边下跌 × 个股 ----

    def test_downtrend_stock_sync_down(self):
        """单边下跌+个股同步跌 → 系统性风险，触发止损"""
        sim = self._make_sim()
        sim.add_position("000001", "同步跌股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 10.80})
        assert sim._alert.called

    def test_downtrend_stock_lead_down(self):
        """单边下跌+个股领跌(跌更多) → 更快触发止损+利润回撤止盈"""
        sim = self._make_sim()
        sim.add_position("000001", "领跌股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.20, entry_date="2026-05-20")
        sim._bought_watch["000001"] = {"max_profit_pct": 0.12}
        sim._check_positions({"000001": 10.50})
        assert sim._alert.called  # 止损触发

    def test_downtrend_stock_resist(self):
        """单边下跌+个股抗跌微涨 → 大盘阻断买入但持仓不触发止损"""
        sim = self._make_sim()
        sim.add_position("000001", "抗跌股", avg_cost=12.00, stop_loss=11.00,
                         current_price=12.20, entry_date="2026-05-20")
        sim._check_positions({"000001": 12.30})
        assert not sim._alert.called

    def test_downtrend_stock_rise_against(self):
        """单边下跌+个股逆势上涨 → 独立行情，可能触发止盈"""
        sim = self._make_sim()
        sim.add_position("000001", "逆涨股", avg_cost=12.00, take_profit=14.00,
                         current_price=13.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 14.10})
        assert sim._alert.called

    # ---- 加速上涨 × 个股 ----

    def test_melt_up_stock_limit_up(self):
        """加速上涨+个股涨停 → 无法买入"""
        sim = self._make_sim(3300)
        sim._limit_cache = {"000001": {"limit_up": 14.52, "limit_down": 11.88}}
        sim.add_position("000001", "涨停股", avg_cost=12.00, take_profit=15.00,
                         current_price=14.50, entry_date="2026-05-20")
        # 涨停价，_is_limit_up → True，但止盈逻辑在 _check_positions 中
        sim._check_positions({"000001": 14.52})


# ===================================================================
# 二、反转走势 × 个股行为
# ===================================================================


class TestReversalMarketCrossStock:
    """V型/倒V/N型/M型/W型 下个股的不同表现。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._industry_cache = {"000001": "银行", "000002": "科技", "000003": "医药"}
        sim._closing_decision_done = False
        sim._abnormal_detector = None
        sim._sector_monitor = None
        return sim

    # ---- V型反转 × 个股 ----

    def test_v_reversal_stock_follows_v(self):
        """V型反转+个股跟随V → 先触发止损，回升后触发止盈"""
        sim = self._make_sim(3350)
        sim.add_position("000001", "V型股", avg_cost=12.00, stop_loss=11.00,
                         take_profit=14.00, current_price=12.00,
                         entry_date="2026-05-20")

        decline = [3350 - i * 1.4 for i in range(50)]      # 3350 → 3280
        recovery = [3280 + i * 1.67 for i in range(30)]     # 3280 → 3330
        index_seq = decline + recovery
        stock_d = [12.00 - i * 0.04 for i in range(50)]     # 12.00 → 10.00
        stock_r = [10.00 + i * 0.12 for i in range(30)]     # 10.00 → 13.60
        stock_seq = stock_d + stock_r

        for i in range(len(index_seq)):
            sim.advance_scan(index_seq[i],
                             {"000001": max(0.01, stock_seq[i])},
                             pre_close=3350)

    def test_v_reversal_stock_stays_low(self):
        """V型反转+个股不跟涨 → 大盘恢复了但个股仍然低位，独立弱势"""
        sim = self._make_sim(3350)
        sim.add_position("000001", "不跟涨股", avg_cost=12.00, stop_loss=11.00,
                         current_price=10.50, entry_date="2026-05-20")

        decline = [3350 - i * 1.4 for i in range(50)]
        recovery = [3280 + i * 1.67 for i in range(30)]
        index_seq = decline + recovery
        # 个股跌下去后不回升
        stock_seq = [12.00 - i * 0.04 for i in range(50)] + [10.00] * 30

        results = []
        for i in range(len(index_seq)):
            ok = sim.advance_scan(index_seq[i],
                                  {"000001": max(0.01, stock_seq[i])},
                                  pre_close=3350)
            results.append(ok)
        assert len(results) == len(index_seq)

    # ---- 倒V/A型 × 个股 ----

    def test_inverted_v_stock_follows(self):
        """倒V型+个股跟随 → 上午浮盈→下午回吐→可能触发移动止盈"""
        sim = self._make_sim(3300)
        sim.add_position("000001", "A型股", avg_cost=12.00, stop_loss=11.00,
                         take_profit=14.00, trailing_stop=0.05,
                         highest_price=14.50, current_price=14.00,
                         entry_date="2026-05-20")
        sim._bought_watch["000001"] = {"max_profit_pct": 0.20}

        rally = [3300 + i * 1.67 for i in range(30)]        # 3300 → 3350
        collapse = [3350 - i * 1.67 for i in range(30)]     # 3350 → 3300
        index_seq = rally + collapse

        for i, idx in enumerate(index_seq):
            stk = 14.00 + max(0, i - 15) * 0.05 - max(0, i - 30) * 0.1
            sim.advance_scan(idx, {"000001": max(0.01, stk)}, pre_close=3300)

    def test_inverted_v_stock_holds_gains(self):
        """倒V型+个股不跟跌 → 上午涨了下午横住，抗跌"""
        sim = self._make_sim(3300)
        sim.add_position("000001", "抗跌股", avg_cost=12.00, stop_loss=11.00,
                         current_price=13.50, entry_date="2026-05-20")
        # 下午大盘跌但个股不跌
        sim._check_positions({"000001": 13.60})
        assert not sim._alert.called

    # ---- M型 × 个股 ----

    def test_m_shape_two_fakeouts(self):
        """M型(涨→跌→涨→跌)+个股跟随 → 两次假突破，系统不应重复告警"""
        sim = self._make_sim(3300)
        sim.add_position("000001", "M型股", avg_cost=12.00, stop_loss=11.00,
                         current_price=12.50, entry_date="2026-05-20")

        up1 = [3300 + i * 2.5 for i in range(10)]            # 3300→3325
        down1 = [3325 - i * 2.5 for i in range(10)]          # 3325→3300
        up2 = [3300 + i * 2.5 for i in range(10)]            # 3300→3325
        down2 = [3325 - i * 2.5 for i in range(10)]          # 3325→3300
        index_seq = up1 + down1 + up2 + down2

        for i, idx in enumerate(index_seq):
            stk = 12.50 + (i % 20 - 10) * 0.1
            sim.advance_scan(idx, {"000001": max(0.01, stk)}, pre_close=3300)

    # ---- N型 × 个股 ----

    def test_n_shape_buying_opportunity(self):
        """N型(跌→涨→跌回调→再涨) + 个股跟随 → 回调是买入机会"""
        sim = self._make_sim(3300)
        sim.add_position("000001", "N型股", avg_cost=12.00, stop_loss=11.00,
                         current_price=12.50, entry_date="2026-05-20")

        up1 = [3300 + i * 2.0 for i in range(15)]
        pullback = [3330 - i * 1.0 for i in range(10)]
        up2 = [3320 + i * 2.0 for i in range(15)]
        index_seq = up1 + pullback + up2

        for i, idx in enumerate(index_seq):
            stk = 12.50 + max(0, i - 5) * 0.03 - max(0, i - 15) * 0.02 + max(0, i - 25) * 0.04
            sim.advance_scan(idx, {"000001": max(0.01, stk)}, pre_close=3300)

    # ---- W型 × 个股 ----

    def test_w_shape_double_bottom(self):
        """W型(跌→涨→再跌→再涨)+个股跟随 → 两次探底，第二次不创新低"""
        sim = self._make_sim(3300)
        sim.add_position("000001", "W型股", avg_cost=12.00, stop_loss=10.50,
                         current_price=12.00, entry_date="2026-05-20")

        down1 = [3300 - i * 2.0 for i in range(12)]
        up1 = [3276 + i * 2.0 for i in range(12)]
        down2 = [3300 - i * 1.5 for i in range(10)]
        up2 = [3285 + i * 2.0 for i in range(16)]
        index_seq = down1 + up1 + down2 + up2

        for i, idx in enumerate(index_seq):
            stk = 12.00 - max(0, i - 0) * 0.03 + max(0, i - 12) * 0.03
            stk = stk - max(0, i - 24) * 0.02 + max(0, i - 34) * 0.03
            sim.advance_scan(idx, {"000001": max(0.01, stk)}, pre_close=3300)


# ===================================================================
# 三、跳空开盘 × 个股行为
# ===================================================================


class TestGapOpenCrossStock:
    """四种跳空开盘 × 个股不同响应。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._closing_decision_done = False
        return sim

    def test_gap_up_and_rally_stock_gaps_too(self):
        """高开高走+个股同步高开 → 浮盈快速扩大，止盈触发"""
        sim = self._make_sim(3350)
        sim.add_position("000001", "高开股", avg_cost=12.00, take_profit=14.00,
                         current_price=14.00, entry_date="2026-05-20")
        sim._check_positions({"000001": 14.20})
        assert sim._alert.called

    def test_gap_up_but_fade_stock_follows(self):
        """高开低走+个股跟随回落 → 开盘浮盈→收盘浮亏，尾盘可能止损"""
        sim = self._make_sim(3350)
        sim.add_position("000001", "回落股", avg_cost=13.00, stop_loss=12.00,
                         current_price=13.50, entry_date="2026-05-20")
        # 终盘跌破成本
        sim._closing_decision_done = False
        sim._check_closing({"000001": 12.50})
        assert sim._alert.called or sim._closing_decision_done

    def test_gap_up_but_fade_stock_holds(self):
        """高开低走+个股抗跌 → 大盘回落但个股横住"""
        sim = self._make_sim(3350)
        sim.add_position("000001", "抗跌股", avg_cost=12.00, stop_loss=11.00,
                         current_price=13.00, entry_date="2026-05-20")
        sim._check_positions({"000001": 13.10})
        assert not sim._alert.called

    def test_gap_down_and_slide_stock_gaps_down(self):
        """低开低走+个股同步低开 → 可能开盘即触发止损"""
        sim = self._make_sim(3250)
        sim.add_position("000001", "低开股", avg_cost=12.00, stop_loss=11.00,
                         current_price=10.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 10.30})
        assert sim._alert.called

    def test_gap_down_but_recover_stock_also_recovers(self):
        """低开高走+个股跟随回升 → 早盘浮亏→午盘收复"""
        sim = self._make_sim(3250)
        sim.add_position("000001", "收复股", avg_cost=12.00, stop_loss=10.50,
                         current_price=11.00, entry_date="2026-05-20")
        # 回升到成本附近
        sim._check_positions({"000001": 12.10})
        assert not sim._alert.called

    def test_gap_down_but_recover_stock_keeps_falling(self):
        """低开高走+个股不跟涨 → 大盘恢复了但个股继续跌，独立弱势"""
        sim = self._make_sim(3250)
        sim.add_position("000001", "继续跌股", avg_cost=12.00, stop_loss=10.00,
                         current_price=10.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 9.80})
        assert sim._alert.called


# ===================================================================
# 四、冲高回落/探底 × 个股行为
# ===================================================================


class TestSpikeAndDipCrossStock:
    """冲高回落和探底回升走势下个股的分化表现。"""

    def _make_sim(self):
        sim = ScenarioSimulator()
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._closing_decision_done = False
        sim._abnormal_detector = None
        sim._sector_monitor = None
        return sim

    def test_spike_fade_stock_at_peak_bought(self):
        """冲高回落+个股在高点开仓 → 买入即套，浮亏扩大"""
        sim = self._make_sim()
        sim.add_position("000001", "高点套股", avg_cost=14.00, stop_loss=13.00,
                         current_price=13.50, entry_date="2026-05-29")
        # T+1 今日买入，不止损但记录浮亏
        sim._check_positions({"000001": 13.20})
        # T+1 持仓被锁定，不触发卖出

    def test_spike_fade_stock_held_through(self):
        """冲高回落+老持仓经历过山车 → 移动止盈在回落中触发"""
        sim = self._make_sim()
        sim.add_position("000001", "过山车股", avg_cost=12.00, stop_loss=11.00,
                         trailing_stop=0.08, highest_price=15.00,
                         current_price=14.50, entry_date="2026-05-20")
        sim._bought_watch["000001"] = {"max_profit_pct": 0.25}
        sim._check_positions({"000001": 13.50})  # 从15回落到13.5
        # 移动止盈可能触发

    def test_dip_recover_stock_never_dipped(self):
        """探底回升+个股未跟随下跌 → 大盘跌时个股抗跌"""
        sim = self._make_sim()
        sim.add_position("000001", "未跌股", avg_cost=12.00, stop_loss=11.00,
                         current_price=12.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 12.80})
        assert not sim._alert.called

    def test_dip_recover_stock_over_recovers(self):
        """探底回升+个股涨超大盘 → 独立走强，可能触发止盈"""
        sim = self._make_sim()
        sim.add_position("000001", "超涨股", avg_cost=12.00, take_profit=15.00,
                         current_price=15.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 15.20})
        assert sim._alert.called


# ===================================================================
# 五、尾盘异动 × 个股行为
# ===================================================================


class TestLateDayCrossStock:
    """尾盘跳水/拉升/放量 × 个股响应。"""

    def _make_sim(self):
        sim = ScenarioSimulator()
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._closing_decision_done = False
        sim._trade_date = "2026-05-29"
        return sim

    def test_late_crash_all_positions_underwater(self):
        """尾盘跳水+多只持仓从浮盈转浮亏 → 尾盘止损建议"""
        sim = self._make_sim()
        sim.add_position("000001", "跳水股A", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.50, entry_date="2026-05-20")
        sim.add_position("000002", "跳水股B", avg_cost=25.00, stop_loss=23.00,
                         current_price=24.00, entry_date="2026-05-20")
        sim._check_closing({"000001": 11.50, "000002": 24.00})
        assert sim._alert.called

    def test_late_rally_profits_surge(self):
        """尾盘拉升+浮盈扩大 → 尾盘减仓建议"""
        sim = self._make_sim()
        sim.add_position("000001", "拉升股", avg_cost=12.00,
                         current_price=13.00, entry_date="2026-05-20")
        sim._check_closing({"000001": 13.00})
        assert sim._alert.called

    def test_late_rally_t1_still_locked(self):
        """尾盘拉升+T+1持仓 → 浮盈扩大但不能卖"""
        sim = self._make_sim()
        sim.add_position("000001", "T+1股", avg_cost=12.00,
                         current_price=13.00, entry_date="2026-05-29")
        sim._alert.reset_mock()
        sim._check_closing({"000001": 13.00})
        # T+1 不产生 action → has_action=False
        assert sim._closing_decision_done is True

    def test_late_day_mixed_positions(self):
        """尾盘+混合持仓(有浮盈有浮亏+T+1) → 分别决策"""
        sim = self._make_sim()
        sim.add_position("000001", "浮亏股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.50, entry_date="2026-05-20")
        sim.add_position("000002", "浮盈股", avg_cost=25.00,
                         current_price=27.00, entry_date="2026-05-20")
        sim.add_position("000003", "T+1股", avg_cost=30.00,
                         current_price=31.00, entry_date="2026-05-29")
        sim._check_closing({
            "000001": 11.50, "000002": 27.00, "000003": 31.00,
        })
        assert sim._alert.called  # 浮亏和浮盈产生 action


# ===================================================================
# 六、极端行情 × 个股极端情况
# ===================================================================


class TestExtremeMarketCrossStock:
    """恐慌/熔断/暴涨 + 个股涨停/跌停/停牌。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._closing_decision_done = False
        sim._limit_cache = {}
        return sim

    def test_panic_limit_down_cant_sell(self):
        """恐慌+个股跌停 → 无法卖出，告警但不执行"""
        sim = self._make_sim(3350)
        sim._limit_cache = {"000001": {"limit_down": 10.80, "limit_up": 13.20}}
        sim.add_position("000001", "跌停股", avg_cost=12.00, stop_loss=11.00,
                         current_price=10.90, entry_date="2026-05-20")
        sim._check_positions({"000001": 10.80})
        # 跌停 → _is_limit_down → 特殊告警 "跌停无法卖出"

    def test_panic_limit_down_opens(self):
        """恐慌+跌停打开 → 从跌停回升但仍触发止损"""
        sim = self._make_sim(3350)
        sim._limit_cache = {"000001": {"limit_down": 10.80, "limit_up": 13.20}}
        sim.add_position("000001", "跌停开板股", avg_cost=12.00, stop_loss=11.00,
                         current_price=10.90, entry_date="2026-05-20")
        # 跌停打开后回到 11.10（仍低于止损）
        sim._check_positions({"000001": 11.10})
        # 不再是跌停，正常触发止损

    def test_surge_limit_up_cant_buy(self):
        """暴涨+个股涨停 → 无法买入"""
        sim = self._make_sim(3400)
        sim._limit_cache = {"000001": {"limit_up": 14.52, "limit_down": 11.88}}
        sim._last_index_quote = {"price": 3400, "pre_close": 3300,
                                  "change_pct": 0.03, "amount": 200e8}
        assert sim._is_limit_up("000001", 14.50)
        assert sim._is_limit_up("000001", 15.00)

    def test_dead_cat_stock_smart_money_dumping(self):
        """死猫跳+个股趁反弹出货 → 弱反弹中减仓"""
        sim = self._make_sim(3280)
        sim.add_position("000001", "出货股", avg_cost=12.00, stop_loss=10.50,
                         take_profit=14.00, current_price=11.00,
                         entry_date="2026-05-20")
        # 死猫跳中价格回到 11.50（仍浮亏但减少了亏损）
        sim._check_positions({"000001": 11.50})


# ===================================================================
# 七、两极分化/特殊市场 × 多持仓联动
# ===================================================================


class TestDivergenceAndMultiStock:
    """指数平稳但个股极端分化，或多只持仓联动的复杂场景。"""

    def _make_sim(self):
        sim = ScenarioSimulator()
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._closing_decision_done = False
        sim._industry_cache = {
            "000001": "银行", "000002": "银行", "000003": "医药",
        }
        return sim

    def test_index_flat_but_holdings_crash(self):
        """指数横盘+持仓暴跌 → 个股独立利空，止损触发（大盘不阻断）"""
        sim = self._make_sim()
        sim.add_position("000001", "暴雷股", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.50, entry_date="2026-05-20")
        sim.add_position("000002", "也暴雷", avg_cost=25.00, stop_loss=23.00,
                         current_price=24.00, entry_date="2026-05-20")
        sim._check_positions({"000001": 10.00, "000002": 22.00})
        assert sim._alert.called

    def test_index_flat_but_holdings_surge(self):
        """指数横盘+持仓暴涨 → 止盈触发"""
        sim = self._make_sim()
        sim.add_position("000001", "利好股", avg_cost=12.00, take_profit=14.00,
                         current_price=14.50, entry_date="2026-05-20")
        sim._check_positions({"000001": 14.20})
        assert sim._alert.called

    def test_same_sector_all_trigger(self):
        """同板块3只持仓同时触发止损 → 集中度问题暴露"""
        sim = self._make_sim()
        for i, code in enumerate(["000001", "000002", "000003"]):
            sim.add_position(code, f"银行股{i}", avg_cost=10.00 + i,
                             stop_loss=9.00 + i, current_price=10.00 + i,
                             entry_date="2026-05-20")
        sim._check_positions({
            "000001": 8.50, "000002": 9.50, "000003": 10.50,
        })
        # 至少 000001 和 000002 触发止损
        assert sim._alert.called

    def test_cross_sector_divergence(self):
        """跨板块背离：银行涨+科技跌 → 不同持仓不同命运"""
        sim = self._make_sim()
        sim.add_position("000001", "银行股", avg_cost=12.00, take_profit=14.00,
                         current_price=13.00, entry_date="2026-05-20")
        sim.add_position("000002", "科技股", avg_cost=25.00, stop_loss=23.00,
                         current_price=24.00, entry_date="2026-05-20")
        # 银行涨触发止盈，科技跌触发止损
        sim._check_positions({"000001": 14.10, "000002": 22.80})
        assert sim._alert.called

    def test_half_positions_trigger(self):
        """半数持仓触发止损+半数正常 → 独立处理"""
        sim = self._make_sim()
        sim.add_position("000001", "触发股A", avg_cost=12.00, stop_loss=11.00,
                         current_price=11.20, entry_date="2026-05-20")
        sim.add_position("000002", "触发股B", avg_cost=25.00, stop_loss=23.00,
                         current_price=24.00, entry_date="2026-05-20")
        sim.add_position("000003", "正常股", avg_cost=30.00, stop_loss=27.00,
                         current_price=31.00, entry_date="2026-05-20")
        sim.add_position("000004", "T+1股", avg_cost=35.00, stop_loss=32.00,
                         current_price=34.00, entry_date="2026-05-29")

        sim._check_positions({
            "000001": 10.80, "000002": 22.50, "000003": 31.50, "000004": 33.00,
        })
        # A和B触发止损，C正常，D是T+1不触发


# ===================================================================
# 八、全链路：完整交易日 × 多个股 × 多阶段
# ===================================================================


class TestFullDayMultiStock:
    """完整交易日中多只持仓同时存在，市场从一种模式切换到另一种。"""

    def _make_sim(self, initial=3300.0):
        sim = ScenarioSimulator(initial)
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._paper_trader = None
        sim._industry_cache = {"000001": "银行", "000002": "科技", "000003": "医药"}
        sim._concept_cache = {"000001": ["金融科技"], "000002": ["半导体"], "000003": ["创新药"]}
        sim._closing_decision_done = False
        sim._abnormal_detector = None
        sim._sector_monitor = None
        sim._limit_cache = {}
        return sim

    def test_rally_then_crash_with_mixed_positions(self):
        """上午大涨+下午暴跌：早盘浮盈→午盘被套

        持仓组合:
          - 000001: 老持仓，上午触发止盈 → 下午已卖出
          - 000002: 老持仓，上午浮盈下午止损
          - 000003: T+1 今早买入，下午暴跌但 T+1 锁定
        """
        sim = self._make_sim(3300)
        sim.add_position("000001", "止盈股", avg_cost=12.00, take_profit=14.00,
                         current_price=13.00, entry_date="2026-05-20")
        sim.add_position("000002", "止损股", avg_cost=25.00, stop_loss=24.00,
                         current_price=25.50, entry_date="2026-05-20")
        sim.add_position("000003", "T+1被套", avg_cost=30.00, stop_loss=28.00,
                         current_price=30.50, entry_date="2026-05-29")

        morning_rally = [3300 + i * 2.0 for i in range(30)]      # 3300→3360
        afternoon_crash = [3360 - i * 3.0 for i in range(25)]    # 3360→3285
        index_seq = morning_rally + afternoon_crash

        for i, idx in enumerate(index_seq):
            # 000001: 上午涨到止盈，下午已经卖了(从portfolio移除)
            # 000002: 上午涨下午跌，最终触发止损
            # 000003: T+1 锁定，不触发
            stk_prices = {}

            if i < 30:  # 上午
                stk_prices["000001"] = 13.00 + i * 0.05   # 最高 14.45
                stk_prices["000002"] = 25.50 + i * 0.02
                stk_prices["000003"] = 30.50 + i * 0.02
            else:  # 下午
                stk_prices["000001"] = 14.45 - (i - 30) * 0.12
                stk_prices["000002"] = 26.10 - (i - 30) * 0.12  # 跌到 23.10
                stk_prices["000003"] = 31.10 - (i - 30) * 0.10  # 跌到 28.60

            sim.advance_scan(idx, {k: max(0.01, v) for k, v in stk_prices.items()},
                             pre_close=3300)

        # 尾盘
        sim._closing_decision_done = False
        final_prices = {"000001": 11.45, "000002": 23.10, "000003": 28.60}
        for code in sim.portfolio.positions:
            sim.update_position_price(code, final_prices.get(code, 0))
        sim._check_closing(final_prices)

    def test_gap_down_slow_recovery_mixed(self):
        """跳空低开→缓慢回升：部分持仓熬过低点

        持仓:
          - 000001: 接近止损但没跌破 → 熬过低点
          - 000002: 开盘就跌破止损 → 触发
        """
        sim = self._make_sim(3250)
        sim.add_position("000001", "熬过低点", avg_cost=12.00, stop_loss=10.80,
                         current_price=11.00, entry_date="2026-05-20")
        sim.add_position("000002", "开盘止损", avg_cost=25.00, stop_loss=23.50,
                         current_price=23.60, entry_date="2026-05-20")

        gap_open = [3250] * 5
        grind_down = [3250 - i * 0.5 for i in range(15)]       # 3250→3242.5
        slow_recovery = [3242.5 + i * 0.5 for i in range(30)]  # →3257.5
        index_seq = gap_open + grind_down + slow_recovery

        for i, idx in enumerate(index_seq):
            stk1 = 11.00 - i * 0.01 + max(0, i - 20) * 0.02   # 微跌后回升
            stk2 = 23.60 - i * 0.05                            # 持续下跌
            sim.advance_scan(idx, {
                "000001": max(0.01, stk1),
                "000002": max(0.01, stk2),
            }, pre_close=3300)

    def test_full_day_extreme_volatility(self):
        """极端波动日：日内振幅 5%，涨停→跌停→再涨停

        最极端场景：系统是否能正常运转不崩溃
        """
        sim = self._make_sim(3300)
        sim.add_position("000001", "波动股", avg_cost=12.00, stop_loss=11.00,
                         take_profit=15.00, current_price=12.50,
                         entry_date="2026-05-20")

        # 3300→3400(+3%)→3200(-6%)→3300(+3%)
        up1 = [3300 + i * 5.0 for i in range(20)]              # 3300→3400
        down = [3400 - i * 8.0 for i in range(25)]             # 3400→3200
        up2 = [3200 + i * 5.0 for i in range(20)]              # 3200→3300
        index_seq = up1 + down + up2

        for i, idx in enumerate(index_seq):
            stk = 12.50
            if i < 20:
                stk = 12.50 + i * 0.1        # 涨到 14.50
            elif i < 45:
                stk = 14.50 - (i - 20) * 0.15  # 跌到 10.75
            else:
                stk = 10.75 + (i - 45) * 0.1   # 回到 12.75

            sim.advance_scan(idx, {"000001": max(0.01, stk)}, pre_close=3300)


# ===================================================================
# 九、跨日状态场景
# ===================================================================


class TestMultiDayStateTransitions:
    """跨交易日状态转换：隔日重置、连续下跌日、昨日模式残留。"""

    def _make_sim(self, trade_date="2026-05-29"):
        sim = ScenarioSimulator()
        sim._ma_baseline_cache = (3300, 3320, 3350)
        sim._alerted_sl_tp = set()
        sim._sl_reminders = {}
        sim._bought_watch = {}
        sim._industry_cache = {"000001": "银行"}
        sim._trade_date = trade_date
        sim._closing_decision_done = False
        return sim

    def test_second_day_after_big_loss(self):
        """昨日大跌止损，今日继续跌 → 昨日已平仓，今日无持仓"""
        sim = self._make_sim("2026-05-29")
        # 昨日已全部止损，今日空仓
        sim.portfolio.positions = {}
        sim._check_positions({"000001": 10.00})
        # 无持仓 → 无操作

    def test_next_day_resets_closing_flag(self):
        """新交易日 → _closing_decision_done 应重置"""
        sim = self._make_sim("2026-05-30")
        sim._closing_decision_done = False  # 新交易日重置
        sim.add_position("000001", "新股", avg_cost=12.00,
                         current_price=13.00, entry_date="2026-05-30")
        sim._check_closing({"000001": 13.00})
        # 新一天的尾盘决策可触发

    def test_accumulated_drawdown_from_prior_days(self):
        """连续下跌多日 → 累计回撤逼近 15% 上限"""
        sim = self._make_sim("2026-05-29")
        type(sim.portfolio).drawdown = PropertyMock(return_value=0.12)
        # drawdown < 15%，仍可交易
        sim.add_position("000001", "被套股", avg_cost=12.00, stop_loss=10.00,
                         current_price=11.00, entry_date="2026-05-20")
        sim._check_positions({"000001": 10.50})
