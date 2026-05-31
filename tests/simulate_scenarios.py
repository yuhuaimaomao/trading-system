# -*- coding: utf-8 -*-
"""全链路场景模拟 — 使用真实 Portfolio/RiskEngine + 全部 Mixin。

每个场景模拟完整交易日（9:25→15:00），记录每一步：
  - 大盘模式切换 → Regime 决策
  - 风控检查：can_open / adjust_stops / evaluate_existing / check_positions
  - 持仓巡检：止损/止盈/移动止盈/利润回撤止盈
  - 尾盘决策：持否/减仓/止损建议
  - 所有告警消息

用法: python3 tests/simulate_scenarios.py [--scenario name]
"""

import sys, os, json, sqlite3
from datetime import datetime, time as dt_time, timedelta
from unittest.mock import MagicMock, patch
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ═══════════════════════════════════════════════════════════════
# 时间控制
# ═══════════════════════════════════════════════════════════════

_current_time = datetime(2026, 5, 29, 9, 25, 0)

def _now():
    return _current_time

def set_time(t: datetime):
    global _current_time
    _current_time = t

def advance_time(minutes=5):
    global _current_time
    _current_time += timedelta(minutes=minutes)

# Patch datetime in all modules that use it
for mod_path in ["trade.monitor.closing", "trade.monitor.market_state",
                  "trade.monitor.position_risk"]:
    p = patch(f"{mod_path}.datetime")
    m = p.start()
    m.now.side_effect = _now


# ═══════════════════════════════════════════════════════════════
# 场景价格生成器
# ═══════════════════════════════════════════════════════════════

import random as _random

def _make_sequence(generator, steps=65):
    """65步 ≈ 9:25→14:50，每5分钟一步"""
    return list(generator(steps))

def gen_uptrend(base=3300, slope=0.5, noise=1.5, steps=65):
    rng = _random.Random(42)
    return [base + i * slope + rng.uniform(-noise, noise) for i in range(steps)]

def gen_decline(base=3350, slope=-0.8, noise=1.0, steps=65):
    rng = _random.Random(42)
    return [base + i * slope + rng.uniform(-noise, noise) for i in range(steps)]

def gen_panic(base=3350, mild=20, crash_len=20, crash_slope=-2.5, steps=65):
    rng = _random.Random(42)
    s = [base + i * -0.3 + rng.uniform(-0.5, 0.5) for i in range(mild)]
    s += [s[-1] + i * crash_slope + rng.uniform(-1, 1) for i in range(crash_len)]
    s += [s[-1] + rng.uniform(-3, 3) for _ in range(steps - mild - crash_len)]
    return s

def gen_v_reversal(base=3350, decline_slope=-1.4, recovery_slope=1.8, steps=65):
    rng = _random.Random(42)
    half = steps // 2
    d = [base + i * decline_slope + rng.uniform(-0.5, 0.5) for i in range(half)]
    return d + [d[-1] + i * recovery_slope + rng.uniform(-0.5, 0.5) for i in range(steps - half)]

def gen_inverted_v(base=3300, rally_slope=0.7, decline_slope=-1.2, steps=65):
    rng = _random.Random(42)
    half = steps // 2
    r = [base + i * rally_slope + rng.uniform(-0.3, 0.3) for i in range(half)]
    return r + [r[-1] + i * decline_slope + rng.uniform(-0.5, 0.5) for i in range(steps - half)]

def gen_dead_cat(base=3350, decline_slope=-1.5, steps=65):
    rng = _random.Random(42)
    d_len = 40
    d = [base + i * decline_slope + rng.uniform(-0.5, 0.5) for i in range(d_len)]
    bottom, top = d[-1], base
    target = bottom + (top - bottom) * 0.382
    b_len = steps - d_len
    return d + [bottom + (target - bottom) * (i/b_len) + rng.uniform(-0.5, 0.5) for i in range(b_len)]

def gen_late_dump(base=3300, dump_start=50, dump_slope=-2.5, steps=65):
    rng = _random.Random(42)
    s = [base + rng.uniform(-2, 3) for _ in range(dump_start)]
    return s + [s[-1] + i * dump_slope + rng.uniform(-1, 1) for i in range(steps - dump_start)]


# ═══════════════════════════════════════════════════════════════
# 全链路模拟器
# ═══════════════════════════════════════════════════════════════

class FullPipelineSimulator:
    """使用真实 Portfolio + RiskEngine + 全部 Mixin 的完整模拟器。"""

    def __init__(self, name="", initial_cash=200_000, initial_index=3300.0):
        self.name = name
        self.initial_cash = initial_cash
        self.initial_index = initial_index

        # ── 真实组件 ──
        from trade.portfolio.portfolio import Portfolio
        from trade.risk.engine import RiskEngine

        self.portfolio = Portfolio(initial_cash=initial_cash)
        self.risk_engine = RiskEngine({
            "max_single_pct": 0.20,
            "max_sector_pct": 0.50,
            "daily_loss_limit": 0.03,
        })

        # ── DB ──
        self.db_path = ":memory:"
        self._init_db()

        # ── 基础设施 mock ──
        self.telegram = MagicMock()
        self.repo = MagicMock()
        self.repo.get_pending_signals.return_value = []

        # ── 大盘状态 ──
        self._index_prices: list[float] = []
        self._index_high = initial_index
        self._index_low = initial_index
        self._index_alerted_downtrend = False
        self._index_last_fluctuation_price = 0.0
        self._market_turnovers: list[float] = []
        self._volume_alerted_divergence = False
        self._last_index_quote: dict | None = None
        self._index_tech_state = {
            "macd_cross": None, "rsi6_zone": "normal", "rsi12_zone": "normal",
            "kdj_cross": None, "kdj_j_zone": "normal", "divergence": None,
        }

        # ── 板块 ──
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

        # ── 持仓/交易 ──
        self._limit_cache: dict[str, dict] = {}
        self._alerted_sl_tp: set[str] = set()
        self._bought_watch: dict[str, dict] = {}
        self._sl_reminders: dict[str, dict] = {}
        self._paper_trader = None  # 模拟盘暂不用
        self._abnormal_detector = None
        self._sector_monitor = None
        self._review_monitor = None
        self._intraday_cache: dict = {}
        self._intraday_cache_scan = -1

        # ── 状态标记 ──
        self._ma_baseline_cache = (initial_index, initial_index + 20, initial_index + 50)
        self._max_drawdown_alerted = False
        self._closing_decision_done = False
        self._scan_count = 0
        self._trade_date = "2026-05-29"
        self.scan_interval = 60
        self.qmt = None

        # ── 日志 ──
        self.alerts: list[dict] = []        # 所有告警
        self.decisions: list[dict] = []     # 所有决策（买入/卖出/风控）
        self.regime_log: list[dict] = []    # regime 变化
        self.trade_log: list[dict] = []     # 实际成交

        # ── 绑定 mixin ──
        self._bind_mixins()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_basic (
                stock_code TEXT, stock_name TEXT, industry TEXT,
                concept_list TEXT, is_st INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stock_indicators (
                stock_code TEXT, trade_date TEXT,
                bb_upper REAL, bb_mid REAL, bb_lower REAL, bb_pct_b REAL,
                ma5 REAL, ma10 REAL, ma20 REAL, rsi6 REAL, rsi12 REAL, rsi24 REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trade_signals (
                id INTEGER PRIMARY KEY, trade_date TEXT, stock_code TEXT,
                stock_name TEXT, signal_source TEXT, status TEXT DEFAULT 'pending',
                buy_zone_min REAL, buy_zone_max REAL, stop_loss REAL, take_profit REAL,
                signal_score REAL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS review_picks (
                trade_date TEXT, stock_code TEXT, stock_name TEXT,
                buy_zone_min REAL, buy_zone_max REAL, stop_loss REAL,
                target_price REAL, score REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS index_snapshots (
                trade_date TEXT, ts REAL, price REAL, pre_close REAL,
                change_pct REAL, amount REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                trade_date TEXT, ts REAL, snapshot_json TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _bind_mixins(self):
        from trade.monitor.market_state import MarketStateMixin
        from trade.monitor.sector_context import SectorContextMixin
        from trade.monitor.position_risk import PositionRiskMixin
        from trade.monitor.closing import ClosingDecisionMixin
        from trade.monitor.abnormal import AbnormalMonitorMixin
        from trade.monitor.buy_decision import BuyDecisionMixin

        for mixin in [MarketStateMixin, SectorContextMixin, PositionRiskMixin,
                      ClosingDecisionMixin, AbnormalMonitorMixin, BuyDecisionMixin]:
            for name in dir(mixin):
                if name.startswith('_') and not name.startswith('__'):
                    attr = getattr(mixin, name, None)
                    if callable(attr) and not hasattr(self, name):
                        setattr(self, name, attr.__get__(self, type(self)))

    # ── Watcher 依赖方法 ──

    def _get_index_quote(self):
        return self._last_index_quote

    def _get_index_baseline(self):
        return self._ma_baseline_cache or (0, 0, 0)

    def _get_index_ma60(self):
        return self.initial_index - 50  # MA60 在现价下方

    def _is_limit_up(self, code, price):
        info = self._limit_cache.get(code)
        return info and price >= info.get("limit_up", 9999) * 0.995

    def _is_limit_down(self, code, price):
        info = self._limit_cache.get(code)
        return info and price <= info.get("limit_down", 0) * 1.005

    def _get_paper_trader(self):
        return self._paper_trader

    def _save_sector_snapshots(self, *a, **kw):
        pass

    def _resolve_name(self, code):
        for c, pos in self.portfolio.positions.items():
            if c == code:
                return pos.stock_name
        return f"股票{code}"

    def _invalidate_watch_codes_cache(self):
        pass

    def _get_sector_trend(self, code):
        ind = self._industry_cache.get(code, "")
        if ind and ind in self._sector_trend_last_dir:
            d = self._sector_trend_last_dir[ind]
            cont = self._sector_trend_continuity.get(ind, 0)
            prefix = "持续" if cont >= 3 else ""
            return f"{prefix}{d}"
        return ""

    def _get_intraday_indicators(self, code):
        return {"available": False}

    def _get_order_book_imbalance(self, code, price):
        return 0.5, ""

    def _get_big_order_direction(self, code):
        return 0.5, ""

    def _get_instrument_info(self, code):
        info = self._limit_cache.get(code, {})
        return {"up_stop": info.get("limit_up", 0), "down_stop": info.get("limit_down", 0)}

    def _get_review_monitor(self):
        return None

    def _load_review_signal_zones(self):
        return {}

    # ── 告警捕获 ──

    def _alert(self, msg, is_private=False):
        self.alerts.append({
            "time": _now().strftime("%H:%M"),
            "step": self._scan_count,
            "private": is_private,
            "msg": msg[:200],
        })

    def _alert_private(self, msg):
        self._alert(msg, is_private=True)

    # ── 数据注入 ──

    def set_index(self, price, pre_close=3300.0, amount=100_000_000_000):
        chg = (price - pre_close) / pre_close if pre_close > 0 else 0
        self._last_index_quote = {
            "price": price, "pre_close": pre_close,
            "change_pct": chg, "amount": amount,
        }

    def open_position(self, code, name, volume=1000, price=12.00,
                      stop_loss=0.0, take_profit=0.0, trailing_stop=0.0,
                      entry_date="2026-05-26", sector=""):
        self.portfolio.open_position(
            stock_code=code, stock_name=name, volume=volume, price=price,
            stop_loss=stop_loss, take_profit=take_profit,
            trailing_stop=trailing_stop,
            entry_date=entry_date, sector_code=sector,
        )
        if sector:
            self._industry_cache[code] = sector

    # ── 核心：一步推进 ──

    def step(self, index_price, stock_prices, pre_close=3300.0):
        """执行一次完整 scan。"""
        self._scan_count += 1
        self.alerts.clear()

        # 更新大盘 (注意: _check_market_state 内部会 append _index_prices)
        self.set_index(index_price, pre_close)
        self._index_high = max(self._index_high, index_price)
        self._index_low = min(self._index_low, index_price)

        # 更新个股 price
        for code, p in stock_prices.items():
            if code in self.portfolio.positions:
                self.portfolio.positions[code].update_price(p)

        # 1. 大盘检测 → MarketRegime
        regime = self._check_market_state(stock_prices)

        # 2. 注入 regime 到风控
        self.risk_engine.set_regime(regime)

        # 3. 动态调整止损
        self.risk_engine.adjust_stops(self.portfolio, stock_prices)

        # 4. 紧急处置
        evals = self.risk_engine.evaluate_existing(self.portfolio, stock_prices)
        for ev in evals:
            self.decisions.append({
                "time": _now().strftime("%H:%M"),
                "step": self._scan_count,
                "type": ev["action"],
                "code": ev["stock_code"],
                "reason": ev["reason"],
                "price": ev.get("price", 0),
            })

        # 5. 持仓巡检（止损/止盈/移动止盈/回撤止盈）
        if stock_prices:
            self._check_positions(stock_prices)

        # 6. 风控全量持仓检查
        close_signals = self.risk_engine.check_positions(
            stock_prices, self.portfolio, self._trade_date,
        )
        for cs in close_signals:
            self.decisions.append({
                "time": _now().strftime("%H:%M"),
                "step": self._scan_count,
                "type": "risk_close",
                "code": cs["stock_code"],
                "reason": cs["reason"],
                "priority": cs["priority"],
            })
            # 实际平仓
            if cs["stock_code"] in self.portfolio.positions:
                pos = self.portfolio.positions[cs["stock_code"]]
                self.portfolio.close_position(cs["stock_code"],
                    stock_prices.get(cs["stock_code"], pos.current_price),
                    reason=cs["reason"])
                self.trade_log.append({
                    "time": _now().strftime("%H:%M"),
                    "step": self._scan_count,
                    "action": "close",
                    "code": cs["stock_code"],
                    "reason": cs["reason"],
                    "pnl_pct": pos.pnl_pct,
                })

        # 7. 异常检测
        if self._scan_count % 3 == 0 and stock_prices:
            self._check_abnormal(stock_prices)

        # 记录 regime
        self.regime_log.append({
            "step": self._scan_count,
            "time": _now().strftime("%H:%M"),
            "pattern": regime.pattern,
            "risk_level": regime.risk_level,
            "allow_buy": regime.allow_buy,
            "position_mult": regime.position_mult,
            "stop_mult": regime.stop_mult,
            "urgent_action": regime.urgent_action,
            "entry_rule": regime.entry_rule,
            "alert_msg": regime.alert_msg,
            "session_phase": regime.session_phase,
            "index": index_price,
        })

        return regime

    def do_closing(self, stock_prices):
        """执行尾盘决策。"""
        set_time(datetime(2026, 5, 29, 14, 35, 0))
        self._closing_decision_done = False
        self.alerts.clear()
        self.risk_engine.set_regime(
            self.regime_log[-1] if self.regime_log else None
        )
        # 直接把 regime_log 最后一条转成 MarketRegime
        from trade.monitor.market_state import MarketRegime
        last = self.regime_log[-1] if self.regime_log else {}
        regime = MarketRegime(
            pattern=last.get("pattern", "normal"),
            risk_level=last.get("risk_level", "safe"),
            allow_buy=last.get("allow_buy", True),
            position_mult=last.get("position_mult", 1.0),
            entry_rule=last.get("entry_rule", "standard"),
            stop_mult=last.get("stop_mult", 1.0),
            urgent_action=last.get("urgent_action", ""),
        )
        self.risk_engine.set_regime(regime)
        self._check_closing(stock_prices)
        return list(self.alerts)


# ═══════════════════════════════════════════════════════════════
# 场景定义与运行
# ═══════════════════════════════════════════════════════════════

def run_scenario(name, description, index_seq, stock_seqs, pre_close,
                 init_positions, expected_behaviors=None):
    """运行一个完整场景并打印详细报告。

    stock_seqs: {code: [p1, p2, ...]}  每个股票的价格序列
    init_positions: [{code, name, volume, price, stop_loss, take_profit, ...}]
    expected_behaviors: 预期行为列表，用于验证
    """
    global _current_time
    _current_time = datetime(2026, 5, 29, 9, 25, 0)

    sim = FullPipelineSimulator(name=name, initial_index=pre_close)
    sim._ma_baseline_cache = (pre_close, pre_close + 20, pre_close + 50)

    # 初始化持仓
    for cfg in init_positions:
        sim.open_position(**cfg)

    steps = len(index_seq)

    print(f"\n{'='*90}")
    print(f"  {name}")
    print(f"  {description}")
    print(f"{'='*90}")

    # 初始状态
    print(f"\n  ┌─ 初始状态 ──────────────────────────────────────────────")
    print(f"  │ 资金: {sim.initial_cash:,.0f}  前收: {pre_close:.1f}  "
          f"MA20={sim._ma_baseline_cache[1]:.1f}  MA60={sim._get_index_ma60():.1f}")
    for code, pos in sim.portfolio.positions.items():
        print(f"  │ 持仓 {code} {pos.stock_name}: "
              f"成本{pos.avg_cost:.2f} 现价{pos.current_price:.2f} "
              f"量{pos.volume} 止损{pos.stop_loss:.2f} 止盈{pos.take_profit:.2f} "
              f"入场{pos.entry_date}")
    print(f"  │ 仓位: {sim.portfolio.position_ratio:.1%}  现金: {sim.portfolio.cash:,.0f}")
    print(f"  └───────────────────────────────────────────────────────")

    # 逐步推进
    pattern_changes = []
    risk_decisions = []
    position_actions = []

    for i in range(steps):
        advance_time(5)
        stk = {}
        for code in stock_seqs:
            if i < len(stock_seqs[code]):
                stk[code] = max(0.01, stock_seqs[code][i])

        regime = sim.step(index_seq[i], stk, pre_close)

        # 记录告警
        for a in sim.alerts:
            if "止损" in a["msg"] or "止盈" in a["msg"] or "熔断" in a["msg"]:
                position_actions.append(a)

        # 记录模式切换
        if len(sim.regime_log) >= 2:
            prev = sim.regime_log[-2]
            curr = sim.regime_log[-1]
            if prev["pattern"] != curr["pattern"]:
                pattern_changes.append(curr)

        # 记录风控决策
        for d in sim.decisions:
            if d["step"] == sim._scan_count:
                risk_decisions.append(d)

    # 尾盘
    closing_stk = {}
    for code in stock_seqs:
        closing_stk[code] = max(0.01, stock_seqs[code][-1])
    closing_alerts = sim.do_closing(closing_stk)

    # ── 打印报告 ──

    # 走势概要
    print(f"\n  ┌─ 走势概要 ──────────────────────────────────────────────")
    print(f"  │ 步数: {steps}  开盘: {index_seq[0]:.1f}  "
          f"收盘: {index_seq[-1]:.1f}  最高: {max(index_seq):.1f}  最低: {min(index_seq):.1f}")
    chg = (index_seq[-1] - pre_close) / pre_close * 100
    print(f"  │ 涨跌: {chg:+.2f}%  振幅: {(max(index_seq)-min(index_seq))/pre_close*100:.2f}%")
    print(f"  └───────────────────────────────────────────────────────")

    # 模式变化
    print(f"\n  ┌─ 模式切换 ──────────────────────────────────────────────")
    if pattern_changes:
        for pc in pattern_changes:
            print(f"  │ {pc['time']} (步{pc['step']:2d}) → {pc['pattern']:<16s}  "
                  f"风险:{pc['risk_level']:<10s} 买入:{'✓' if pc['allow_buy'] else '✗'}  "
                  f"仓位×{pc['position_mult']}  止损×{pc['stop_mult']}  "
                  f"entry={pc['entry_rule']}")
            if pc['alert_msg']:
                print(f"  │   └ {pc['alert_msg'][:100]}")
    else:
        print(f"  │ (无模式切换)")
    print(f"  └───────────────────────────────────────────────────────")

    # 最终 regime
    final_r = sim.regime_log[-1]
    print(f"\n  ┌─ 最终 Regime ───────────────────────────────────────────")
    for k, v in final_r.items():
        print(f"  │ {k:20s}: {v}")
    print(f"  └───────────────────────────────────────────────────────")

    # 持仓动作
    print(f"\n  ┌─ 持仓动作 ({len(position_actions)} 条) ──────────────────────────")
    for a in position_actions:
        print(f"  │ [{a['time']}] {a['msg'][:130]}")
    if not position_actions:
        print(f"  │ (无)")
    print(f"  └───────────────────────────────────────────────────────")

    # 风控决策
    risk_from_engine = [d for d in risk_decisions if d["type"] in
                        ("emergency_close", "reduce", "tighten_stops")]
    print(f"\n  ┌─ 风控决策 ({len(risk_from_engine)} 条) ──────────────────────────")
    for d in risk_from_engine:
        print(f"  │ [{d['time']}] {d['type']}: {d['code']} — {d['reason'][:100]}")
    if not risk_from_engine:
        print(f"  │ (无)")
    print(f"  └───────────────────────────────────────────────────────")

    # 实际成交
    print(f"\n  ┌─ 实际成交 ({len(sim.trade_log)} 笔) ────────────────────────────")
    for t in sim.trade_log:
        print(f"  │ [{t['time']}] {t['action']} {t['code']}  "
              f"原因: {t['reason'][:80]}")
        if 'pnl_pct' in t:
            print(f"  │   └ 盈亏: {t['pnl_pct']:+.2%}")
    if not sim.trade_log:
        print(f"  │ (无)")
    print(f"  └───────────────────────────────────────────────────────")

    # 尾盘
    print(f"\n  ┌─ 尾盘决策 ─────────────────────────────────────────────")
    if closing_alerts:
        for a in closing_alerts:
            print(f"  │ {a['msg'][:150]}")
    else:
        print(f"  │ (无操作建议)")
    print(f"  └───────────────────────────────────────────────────────")

    # 终态
    print(f"\n  ┌─ 终态 ──────────────────────────────────────────────────")
    print(f"  │ 总资产: {sim.portfolio.total_value:,.0f}  "
          f"现金: {sim.portfolio.cash:,.0f}  "
          f"市值: {sim.portfolio.total_value - sim.portfolio.cash:,.0f}")
    print(f"  │ 总盈亏: {sim.portfolio.total_pnl:+,.0f}  "
          f"({sim.portfolio.total_pnl/sim.initial_cash:+.2%})")
    print(f"  │ 回撤: {sim.portfolio.drawdown:.2%}  "
          f"持仓数: {len(sim.portfolio.positions)}")
    for code, pos in sim.portfolio.positions.items():
        print(f"  │ {code} {pos.stock_name}: 现{pos.current_price:.2f} "
              f"盈亏{pos.pnl_pct:+.2%} 止损{pos.stop_loss:.2f}")
    print(f"  └───────────────────────────────────────────────────────")

    # 预期验证
    if expected_behaviors:
        print(f"\n  ┌─ 预期验证 ──────────────────────────────────────────────")
        all_ok = True
        for eb in expected_behaviors:
            check_type = eb.get("type")
            ok = False
            detail = ""
            if check_type == "pattern_changed_to":
                patterns = [r["pattern"] for r in sim.regime_log]
                ok = eb["value"] in patterns
                detail = f"模式中出现'{eb['value']}': {ok}"
            elif check_type == "no_buy":
                # 检查没有买入成交
                buys = [t for t in sim.trade_log if t["action"] == "buy"]
                ok = len(buys) == 0
                detail = f"无买入: {ok} (共{len(buys)}笔)"
            elif check_type == "stop_loss_hit":
                closes = [t for t in sim.trade_log
                          if t["action"] == "close" and t["code"] == eb.get("code")]
                ok = len(closes) > 0
                detail = f"{eb.get('code','')} 触发平仓: {ok}"
            elif check_type == "allow_buy_false":
                final_allow = sim.regime_log[-1]["allow_buy"]
                ok = not final_allow
                detail = f"allow_buy=False: {ok} (实际={final_allow})"
            elif check_type == "circuit_breaker":
                cb_hits = [d for d in risk_decisions
                          if "熔断" in d.get("reason", "")]
                ok = len(cb_hits) > 0
                detail = f"触发熔断: {ok}"
            elif check_type == "closing_alert":
                ok = len(closing_alerts) > 0
                detail = f"尾盘有建议: {ok}"
            if ok:
                print(f"  │ ✅ {eb['desc']}: {detail}")
            else:
                print(f"  │ ❌ {eb['desc']}: {detail}")
                all_ok = False
        if all_ok:
            print(f"  │ 🎯 全部通过")
        print(f"  └───────────────────────────────────────────────────────")

    # 返回结构化结果
    return {
        "name": name,
        "index_seq": index_seq,
        "stock_seqs": stock_seqs,
        "pattern_changes": pattern_changes,
        "position_actions": position_actions,
        "risk_decisions": risk_decisions,
        "trade_log": sim.trade_log,
        "closing_alerts": closing_alerts,
        "regime_log": sim.regime_log,
        "final_regime": final_r,
        "final_positions": [
            {"code": c, "name": p.stock_name, "price": p.current_price,
             "pnl_pct": p.pnl_pct, "stop_loss": p.stop_loss}
            for c, p in sim.portfolio.positions.items()
        ],
        "total_pnl": sim.portfolio.total_pnl,
        "total_pnl_pct": sim.portfolio.total_pnl / sim.initial_cash,
        "drawdown": sim.portfolio.drawdown,
    }


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def main():
    results = []
    STEPS = 65

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 1: 单边下跌 + 止损触发
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    idx = gen_decline(3350, steps=STEPS)
    stk = {"000001": [12.50 + i * -0.04 for i in range(STEPS)]}
    results.append(run_scenario(
        "场景1: 单边下跌 → 止损触发 → 禁止买入",
        "指数从 3350 单边跌至 ~3300，个股跟跌触发止损，系统应暂停买入、收紧止损",
        idx, stk, pre_close=3350,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.50,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "one_sided",
             "desc": "检测到单边下跌"},
            {"type": "allow_buy_false", "desc": "禁止新开仓"},
            {"type": "stop_loss_hit", "code": "000001", "desc": "止损触发平仓"},
            {"type": "no_buy", "desc": "下跌段无意外买入"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 2: 恐慌暴跌 + 熔断
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    idx = gen_panic(3350, steps=STEPS)
    stk = {"000001": [12.50 + i * -0.06 for i in range(STEPS)],
           "000002": [25.00 + i * -0.12 for i in range(STEPS)]}
    results.append(run_scenario(
        "场景2: 恐慌暴跌 → 多持仓止损 → 熔断",
        "前半段缓跌，中段加速暴跌，两只持仓同时触发止损，日亏损超3%触发熔断",
        idx, stk, pre_close=3350,
        init_positions=[
            {"code": "000001", "name": "平安银行", "volume": 1000, "price": 12.50,
             "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26"},
            {"code": "000002", "name": "万科A", "volume": 500, "price": 25.00,
             "stop_loss": 22.00, "take_profit": 30.00, "entry_date": "2026-05-26"},
        ],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "one_sided",
             "desc": "检测到单边/恐慌"},
            {"type": "stop_loss_hit", "code": "000001", "desc": "平安银行止损"},
            {"type": "allow_buy_false", "desc": "禁止买入"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 3: V型反转
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    idx = gen_v_reversal(3350, steps=STEPS)
    half = STEPS // 2
    stk_v = {"000001": [12.50 + i * -0.05 for i in range(half)] +
                       [12.50 + half * -0.05 + i * 0.07 for i in range(STEPS - half)]}
    results.append(run_scenario(
        "场景3: V型反转 → 先跌后涨 → 恢复买入",
        "上午跌至 -2%，下午强势反弹回到前收上方，系统应识别反转、恢复允许买入",
        idx, stk_v, pre_close=3350,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.50,
            "stop_loss": 10.50, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "dead_cat",
             "desc": "先检测到弱势反弹(下跌段)"},
            {"type": "no_buy", "desc": "下跌段无买入"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 4: 冲高回落 (A型) + 利润回撤止盈
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    idx = gen_inverted_v(3300, steps=STEPS)
    half = STEPS // 2
    stk_iv = {"000001": [12.00 + i * 0.04 for i in range(half)] +
                         [12.00 + half * 0.04 + i * -0.06 for i in range(STEPS - half)]}
    # 先让持仓有浮盈记录
    results.append(run_scenario(
        "场景4: 冲高回落(A型) → 利润回撤止盈触发",
        "上午拉升+2.5%，下午持续回落至水下。持仓先大赚后回吐，触发利润回撤止盈",
        idx, stk_iv, pre_close=3300,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.00,
            "stop_loss": 10.80, "take_profit": 13.50, "entry_date": "2026-05-26",
            "trailing_stop": 0.05,
        }],
        expected_behaviors=[
            {"type": "stop_loss_hit", "code": "000001",
             "desc": "利润回撤止盈触发"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 5: 死猫跳 — 不跟进
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    idx = gen_dead_cat(3350, steps=STEPS)
    stk_dc = {"000001": [12.50 + i * -0.04 for i in range(40)] +
                         [12.50 + 40 * -0.04 + i * 0.015 for i in range(25)]}
    results.append(run_scenario(
        "场景5: 死猫跳 → 弱反弹不跟进",
        "大跌-1.7%后弱反弹到38.2%分位（不到50%），系统应识别弱势反弹、禁止买入",
        idx, stk_dc, pre_close=3350,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.50,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "one_sided",
             "desc": "先检测到单边下跌"},
            {"type": "no_buy", "desc": "反弹段不触发买入"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 6: 尾盘跳水 + 持仓紧急评估
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    idx = gen_late_dump(3300, steps=STEPS)
    stk_ld = {"000001": [12.00 + i * 0.01 for i in range(50)] +
                         [12.00 + 50 * 0.01 + i * -0.06 for i in range(15)]}
    results.append(run_scenario(
        "场景6: 尾盘跳水 → 紧急评估持仓",
        "全天横盘微涨，14:00后急跌-1.5%，系统应在尾盘建议止损或减仓",
        idx, stk_ld, pre_close=3300,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.00,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "late_dump",
             "desc": "检测到尾盘跳水"},
            {"type": "allow_buy_false", "desc": "禁止新开仓"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 7: 稳步上涨 + 止盈触发
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    idx = gen_uptrend(3300, slope=0.5, steps=STEPS)
    stk_up = {"000001": [12.50 + i * 0.04 for i in range(STEPS)]}
    results.append(run_scenario(
        "场景7: 稳步上涨 → 止盈触发 → 利润锁定",
        "指数稳步上移+1.5%，个股跟涨触发止盈价，系统应自动止盈并通知",
        idx, stk_up, pre_close=3300,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.50,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "stop_loss_hit", "code": "000001", "desc": "止盈触发"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 8: T+1 锁定 + 正常波动
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    idx = gen_uptrend(3300, slope=0.2, noise=2.0, steps=STEPS)
    stk_t1 = {
        "000001": [12.00 + i * 0.01 for i in range(STEPS)],  # 老持仓
        "000002": [25.00 + i * 0.02 for i in range(STEPS)],  # 今天买的 (T+1)
    }
    results.append(run_scenario(
        "场景8: T+1 锁仓 — 今日买入不触发止损",
        "两只持仓：一只老持仓(正常止损止盈)，一只今日新买(T+1锁仓不卖)。"
        "价格跌到止损价时，老仓触发止损，T+1仓位只记录不卖出",
        idx, stk_t1, pre_close=3300,
        init_positions=[
            {"code": "000001", "name": "平安银行", "volume": 1000, "price": 12.00,
             "stop_loss": 11.50, "take_profit": 14.00, "entry_date": "2026-05-26"},
            {"code": "000002", "name": "万科A", "volume": 500, "price": 25.00,
             "stop_loss": 24.00, "take_profit": 28.00, "entry_date": "2026-05-29",
             "trailing_stop": 0.05},
        ],
        expected_behaviors=[
            {"type": "no_buy", "desc": "无意外买入"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 9: 加速上涨 (melt-up) — 开低后加速拉升到极端高位
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    rng = _random.Random(99)
    idx_mu = [3295 + rng.uniform(-0.3, 0.3) for _ in range(8)]  # 开盘小幅走低
    idx_mu += [idx_mu[-1] + i * 0.2 + rng.uniform(-0.2, 0.2) for i in range(20)]  # 缓慢回升
    # 后半段加速拉升，从~3299拉到~3360
    base_mu = idx_mu[-1]
    idx_mu += [base_mu + i * 1.8 + rng.uniform(-0.3, 0.3) for i in range(37)]  # 加速拉升
    stk_mu = {"000001": [12.00 + i * 0.06 for i in range(65)]}
    results.append(run_scenario(
        "场景9: 加速上涨(melt-up) → 追高风险极大",
        "开盘后缓涨，后半段加速拉升，价格推到日内极端高位，系统应预警追高风险",
        idx_mu, stk_mu, pre_close=3300,
        init_positions=[{
            "code": "000001", "name": "宁德时代", "volume": 500, "price": 12.00,
            "stop_loss": 11.00, "take_profit": 15.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "melt_up",
             "desc": "检测到加速上涨"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 10: W型双底 — 振幅>2%的两次探底
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    rng = _random.Random(77)
    base_w = 3350
    idx_w = [base_w + rng.uniform(-1, 1) for _ in range(5)]  # 开盘 ~3350
    idx_w += [base_w + i * -3.0 + rng.uniform(-0.5, 0.5) for i in range(14)]  # 第一波急跌 ~3308
    v1 = idx_w[-1]
    idx_w += [v1 + i * 2.5 + rng.uniform(-0.5, 0.5) for i in range(14)]  # 反弹 ~3343
    idx_w += [idx_w[-1] + i * -2.8 + rng.uniform(-0.4, 0.4) for i in range(14)]  # 第二波跌回接近v1 ~3306
    idx_w += [idx_w[-1] + i * 2.5 + rng.uniform(-0.5, 0.5) for i in range(18)]  # 再反弹 ~3350
    stk_w = {"000001": [12.00 + i * 0.005 for i in range(65)]}
    results.append(run_scenario(
        "场景10: W型双底 → 二次探底确认反转",
        "跌→涨→再跌(接近前低)→再涨。双底确认，反转可靠性高于单V",
        idx_w, stk_w, pre_close=3350,
        init_positions=[{
            "code": "000001", "name": "招商银行", "volume": 1000, "price": 12.00,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "w_bottom",
             "desc": "检测到W型双底"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 11: M型双顶 — 振幅>1.5%的两次冲高
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    rng = _random.Random(66)
    base_m = 3300
    idx_m = [base_m + rng.uniform(-1, 1) for _ in range(5)]
    idx_m += [base_m + i * 1.8 + rng.uniform(-0.5, 0.5) for i in range(14)]  # 第一波急涨 → ~3325
    p1 = idx_m[-1]
    idx_m += [p1 + i * -1.5 + rng.uniform(-0.4, 0.4) for i in range(14)]  # 回落 → ~3305
    idx_m += [idx_m[-1] + i * 1.6 + rng.uniform(-0.4, 0.4) for i in range(14)]  # 第二波涨近p1 → ~3324
    idx_m += [idx_m[-1] + i * -1.5 + rng.uniform(-0.4, 0.4) for i in range(18)]  # 再回落
    stk_m = {"000001": [12.00 + i * 0.01 for i in range(65)]}
    results.append(run_scenario(
        "场景11: M型双顶 → 两次冲高失败风险大",
        "涨→跌→再涨(接近前高)→再跌。双顶确认，风险大于普通倒V",
        idx_m, stk_m, pre_close=3300,
        init_positions=[{
            "code": "000001", "name": "贵州茅台", "volume": 100, "price": 12.00,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "m_top",
             "desc": "检测到M型双顶"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 12: 真正的跳空高开低走 — gap > 1.5%, 持续快速回落
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    rng = _random.Random(55)
    prev_c = 3300
    gap_open = prev_c * 1.02  # 高开 2%
    idx_gap = [gap_open + rng.uniform(-0.3, 0.3) for _ in range(5)]  # 开盘附近震荡
    idx_gap += [gap_open + i * -1.2 + rng.uniform(-0.4, 0.4) for i in range(30)]  # 前半段快速回落
    idx_gap += [idx_gap[-1] + i * -0.6 + rng.uniform(-0.3, 0.3) for i in range(30)]  # 后半段继续阴跌
    stk_gap = {"000001": [12.50 + i * -0.04 for i in range(65)]}
    results.append(run_scenario(
        "场景12: 跳空高开低走 → 追高盘全线套牢",
        "高开1.2%后持续回落一整天，尾盘接近日内低点，追高资金全部被套",
        idx_gap, stk_gap, pre_close=prev_c,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.50,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "gap_up_fade",
             "desc": "检测到高开低走"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 13: 低开高走 — gap > 1.5%, 持续快速回升
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    rng = _random.Random(44)
    prev_c2 = 3350
    gap_down_open = prev_c2 * 0.982  # 低开 1.8%
    idx_gdr = [gap_down_open + rng.uniform(-0.3, 0.3) for _ in range(5)]  # 开盘附近
    idx_gdr += [gap_down_open + i * 1.0 + rng.uniform(-0.3, 0.3) for i in range(30)]  # 前半段快速回升
    idx_gdr += [idx_gdr[-1] + i * 0.6 + rng.uniform(-0.3, 0.3) for i in range(30)]  # 后半段继续走高
    stk_gdr = {"000001": [11.50 + i * 0.04 for i in range(65)]}
    results.append(run_scenario(
        "场景13: 低开高走 → 恐慌情绪修复中",
        "低开1.2%后持续回升，收在日内高位。恐慌被逐步消化",
        idx_gdr, stk_gdr, pre_close=prev_c2,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 11.50,
            "stop_loss": 10.50, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "gap_down_recover",
             "desc": "检测到低开高走"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 14: 尾盘拉升 — 前80%横盘，最后20%急速拉升
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    rng = _random.Random(33)
    idx_lr = [3300 + rng.uniform(-1.5, 2) for _ in range(52)]  # 前80%全天窄幅横盘
    idx_lr += [idx_lr[-1] + i * 2.0 + rng.uniform(-0.3, 0.3) for i in range(13)]  # 最后20%急速拉升
    stk_lr = {"000001": [12.00 + i * 0.01 for i in range(50)] +
                         [12.50 + i * 0.04 for i in range(15)]}
    results.append(run_scenario(
        "场景14: 尾盘拉升 → 警惕次日低开",
        "全天横盘，尾盘快速拉升+1.5%，不宜追高，警惕次日低开风险",
        idx_lr, stk_lr, pre_close=3300,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.00,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "late_rally",
             "desc": "检测到尾盘拉升"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 15: 钓鱼线
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    rng = _random.Random(22)
    idx_fl = [3300 + i * 0.35 + rng.uniform(-0.5, 0.5) for i in range(50)]  # 全天推升
    top = idx_fl[-1]
    idx_fl += [top + i * -1.8 + rng.uniform(-0.5, 0.5) for i in range(15)]  # 尾盘急跌
    stk_fl = {"000001": [12.00 + i * 0.03 for i in range(50)] +
                         [13.50 + i * -0.08 for i in range(15)]}
    results.append(run_scenario(
        "场景15: 钓鱼线 → 全天推升尾盘急跌(出货信号)",
        "全天缓慢推升诱多，尾盘急剧下跌，典型主力出货。紧急退出",
        idx_fl, stk_fl, pre_close=3300,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.00,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "fishing_line",
             "desc": "检测到钓鱼线"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 场景 16: 宽幅震荡
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    rng = _random.Random(11)
    idx_wc = [3300]
    for i in range(64):
        direction = 1 if (i // 8) % 2 == 0 else -1
        idx_wc.append(idx_wc[-1] + direction * rng.uniform(2, 6))
    stk_wc = {"000001": [12.00 + (i % 12 - 6) * 0.3 for i in range(65)]}
    results.append(run_scenario(
        "场景16: 宽幅震荡 → 多空分歧大方向不明",
        "振幅>1.5%但无明确方向，价格多次穿越均线。建议减仓观望",
        idx_wc, stk_wc, pre_close=3300,
        init_positions=[{
            "code": "000001", "name": "平安银行", "volume": 1000, "price": 12.00,
            "stop_loss": 11.00, "take_profit": 14.00, "entry_date": "2026-05-26",
        }],
        expected_behaviors=[
            {"type": "pattern_changed_to", "value": "wide_choppy",
             "desc": "检测到宽幅震荡"},
        ],
    ))

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 汇总
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    print(f"\n{'='*90}")
    print(f"  汇总 ({len(results)} 场景)")
    print(f"{'='*90}")
    header = f"  {'场景':<36s} {'涨跌':>7s} {'模式':<14s} {'风险':<10s} {'买入':>4s} {'持仓动作':<10s} {'成交':>4s}"
    print(header)
    print(f"  {'─'*90}")
    for r in results:
        f = r["final_regime"]
        has_action = "✓" if r["position_actions"] else "—"
        has_trade = "✓" if r["trade_log"] else "—"
        print(f"  {r['name']:<36s} "
              f"{(r['index_seq'][-1]-r['index_seq'][0])/r['index_seq'][0]*100:>+6.2f}% "
              f"{f['pattern']:<14s} {f['risk_level']:<10s} "
              f"{'✓' if f['allow_buy'] else '✗':>4s} "
              f"{has_action:<10s} {has_trade:>4s}")

    # 保存完整日志
    log_path = os.path.join(os.path.dirname(__file__), "scenario_log.json")
    serializable = []
    for r in results:
        sr = dict(r)
        sr.pop("index_seq", None)
        sr.pop("stock_seqs", None)
        serializable.append(sr)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  完整日志: {log_path}")
    print()


if __name__ == "__main__":
    main()
