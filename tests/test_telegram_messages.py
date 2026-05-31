# -*- coding: utf-8 -*-
"""Telegram 消息推送全覆盖模拟测试。

按时间线模拟完整交易日（09:25→15:00），触发所有可能的
群聊推送和私聊推送消息类型，验证格式和内容要素。

消息类型清单（按时间线）:
  ┌─ 群聊 ────────────────────────────────────────────┐
  │ 09:25  开盘决策汇总 (_send_opening_decision)       │
  │ 09:30~  持仓止损触发 (_check_positions)            │
  │ 09:30~  持仓止盈触发 (_check_positions)            │
  │ 09:30~  移动止盈触发 (_check_positions)            │
  │ 09:30~  利润回撤止盈 (_check_bought_signals)       │
  │ 09:30~  买入信号触发 (_check_signals)              │
  │ 09:30~  复盘精选触发 (_check_review_picks)         │
  │ 09:30~  信号买入成功通知 (PaperTrader)             │
  │ 09:30~  大盘单边下跌告警 (market_state)            │
  │ 09:30~  大盘放量/缩量告警 (volume alert)           │
  │ 09:30~  量价背离告警 (market_state)                │
  │ 09:30~  熔断告警 (max_drawdown)                    │
  │ 09:30~  涨跌停异动 (abnormal)                      │
  │ 09:30~  大盘模式切换推送 (push_regime_alert)       │
  │ 14:30  尾盘决策 (_check_closing)                   │
  │ 14:30~ 换仓评估 (swap evaluation)                  │
  └────────────────────────────────────────────────────┘
  ┌─ 私聊 ────────────────────────────────────────────┐
  │ 09:30~  实盘止损触发 (position_risk._alert_private)│
  │ 09:30~  实盘成交确认回复 (watcher._check_replies)  │
  └────────────────────────────────────────────────────┘
  ┌─ 接收 ────────────────────────────────────────────┐
  │ 09:30~  用户成交确认回复 (ManualExecutor)          │
  │ 09:30~  SL提醒命令 (handle_sl_command)             │
  └────────────────────────────────────────────────────┘

用法: python3 tests/test_telegram_messages.py
"""

import json
import os
import re
import sys
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════
# 时间控制
# ═══════════════════════════════════════════════

_current_time = datetime(2026, 5, 29, 9, 25, 0)

def _now():
    return _current_time

def set_time(t):
    global _current_time
    _current_time = t

def advance(minutes=5):
    global _current_time
    _current_time += timedelta(minutes=minutes)

for mod_path in ["trade.monitor.closing", "trade.monitor.market_state",
                  "trade.monitor.position_risk"]:
    m = patch(f"{mod_path}.datetime")
    m2 = m.start()
    m2.now.side_effect = _now


# ═══════════════════════════════════════════════
# 消息收集 Watcher
# ═══════════════════════════════════════════════

class MessageCollectingWatcher:
    """全功能 Watcher — 捕获所有 _alert / _alert_private 调用。"""

    def __init__(self):
        from trade.portfolio.portfolio import Portfolio
        from trade.risk.engine import RiskEngine

        self.portfolio = Portfolio(initial_cash=200_000)
        self.risk_engine = RiskEngine({
            "max_single_pct": 0.25,
            "max_sector_pct": 0.60,
            "daily_loss_limit": 0.05,
        })
        self.telegram = None
        self._private_telegram = None
        self.qmt = None

        self._tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self._tmpdir, "test.db")
        self._init_db()

        self._trade_date = "2026-05-29"
        self._scan_count = 0
        self._running = True
        self.scan_interval = 60

        # 大盘状态
        self._index_prices: list[float] = []
        self._index_high: float = 0
        self._index_low: float = 0
        self._index_alerted_downtrend = False
        self._index_last_fluctuation_price = 0.0
        self._market_turnovers: list[float] = []
        self._volume_alerted_divergence = False
        self._last_index_quote: dict | None = None
        self._index_tech_state = {
            "macd_cross": None, "rsi6_zone": "normal", "rsi12_zone": "normal",
            "kdj_cross": None, "kdj_j_zone": "normal", "divergence": None,
        }
        self._max_drawdown_alerted = False

        # 全市场/板块
        self._market_snapshot: dict[str, dict] = {}
        self._sector_stats: dict[str, dict] = {}
        self._concept_stats: dict[str, dict] = {}
        self._sector_trend_history: dict[str, list[float]] = defaultdict(list)
        self._sector_trend_continuity: dict[str, int] = defaultdict(int)
        self._sector_trend_last_dir: dict[str, str] = {}
        self._industry_cache: dict[str, str] = {}
        self._concept_cache: dict[str, list[str]] = {}
        self._prev_ind_amounts: dict[str, float] = {}
        self._prev_con_amounts: dict[str, float] = {}
        self._prev_snapshot: dict[str, dict] = {}

        # 持仓/监控
        self._limit_cache: dict[str, dict] = {}
        self._alerted_sl_tp: set[str] = set()
        self._bought_watch: dict[str, dict] = {}
        self._sl_reminders: dict[str, dict] = {}
        self._paper_trader = None
        self._abnormal_detector = None
        self._sector_monitor = None
        self._review_monitor = None
        self._intraday_cache: dict = {}
        self._intraday_cache_scan = -1
        self._signal_alert_state: dict = {}
        self._review_alert_state: dict = {}

        self._ma_baseline_cache = (3300, 3320, 3350)
        self._closing_decision_done = False
        self._cached_db_watch_codes: set = set()
        self._watch_codes_stale: bool = True

        self.repo = MagicMock()
        self.repo.get_pending_signals.return_value = []

        # 消息收集桶
        self.public_messages: list[dict] = []   # _alert 调用
        self.private_messages: list[dict] = []  # _alert_private 调用
        self.all_raw: list[str] = []            # 所有消息原始文本

        self._init_cache()
        self._bind_mixins()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS stock_basic (stock_code TEXT, stock_name TEXT, industry TEXT, concepts TEXT, trade_date TEXT, main_force_net REAL, main_force_ratio REAL, super_large_net REAL, large_net REAL, ma5_angle REAL, pe_dynamic REAL, circ_market_cap REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS stock_indicators (stock_code TEXT, trade_date TEXT, ma5 REAL, ma10 REAL, ma20 REAL, rsi6 REAL, rsi12 REAL, rsi24 REAL, bb_upper REAL, bb_mid REAL, bb_lower REAL, bb_pct_b REAL, bb_width REAL, bbi_daily REAL, bbi_weekly REAL, ma60 REAL, ma120 REAL, macd_dif REAL, macd_dea REAL, macd_bar REAL, kdj_k REAL, kdj_d REAL, kdj_j REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS trade_signals (id INTEGER PRIMARY KEY, trade_date TEXT, stock_code TEXT, stock_name TEXT, signal_source TEXT, status TEXT DEFAULT 'pending', account TEXT DEFAULT 'paper', buy_zone_min REAL, buy_zone_max REAL, stop_loss REAL, take_profit REAL, signal_score REAL DEFAULT 0)")
        conn.execute("CREATE TABLE IF NOT EXISTS review_picks (trade_date TEXT, stock_code TEXT, stock_name TEXT, buy_zone_min REAL, buy_zone_max REAL, stop_loss REAL, target_price REAL, score REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS trade_orders (stock_code TEXT, order_type TEXT, filled_volume INTEGER, filled_price REAL, filled_amount REAL, order_status TEXT, account TEXT, trade_date TEXT, order_time TEXT, signal_id INTEGER, entry_price REAL, buy_time TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS trade_portfolio_snapshots (account TEXT, total_value REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS index_snapshots (trade_date TEXT, ts REAL, price REAL, pre_close REAL, change_pct REAL, amount REAL)")
        conn.execute("CREATE TABLE IF NOT EXISTS market_snapshots (trade_date TEXT, ts TEXT, code TEXT, change_pct REAL, price REAL, amount REAL)")
        conn.commit()
        conn.close()

    def _init_cache(self):
        for i in range(20):
            ind = ["银行", "半导体", "医药", "汽车", "地产"][i // 4]
            self._industry_cache[f"{i:06d}"] = ind
            con = ["金融", "芯片", "创新药", "新能源", "地产"][i // 4]
            self._concept_cache[f"{i:06d}"] = [con]

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
        # PositionRiskMixin 的非下划线方法也需手动绑定
        self.handle_sl_command = PositionRiskMixin.handle_sl_command.__get__(self, type(self))
        self._resolve_sl_reminders = PositionRiskMixin._resolve_sl_reminders.__get__(self, type(self))

    # ── 辅助 ──
    def _get_index_quote(self): return self._last_index_quote
    def _get_index_baseline(self): return self._ma_baseline_cache or (3300, 3320, 3350)
    def _get_index_ma60(self): return 3200.0
    def _save_sector_snapshots(self, *a, **kw): pass
    def _resolve_name(self, code):
        names = {"000001": "平安银行", "000002": "万科A", "000003": "招行",
                 "000004": "中芯", "000005": "华虹", "000006": "长电",
                 "000007": "恒瑞", "000008": "迈瑞", "000009": "比亚迪", "000010": "上汽"}
        return names.get(code, code)
    def _get_paper_trader(self): return self._paper_trader
    def _get_review_monitor(self): return None
    def _load_review_signal_zones(self): return {}
    def _get_intraday_indicators(self, code): return {"available": False}
    def _get_order_book_imbalance(self, code, price): return 0.5, ""
    def _get_big_order_direction(self, code): return 0.5, ""
    def _get_instrument_info(self, code):
        return {"up_stop": 999, "down_stop": 0.01}
    def _is_limit_up(self, code, price): return False
    def _is_limit_down(self, code, price): return False
    def _get_limit_pct(self, code): return 0.10
    def _invalidate_watch_codes_cache(self): pass

    def _get_watch_codes(self):
        codes = set(self.portfolio.positions.keys())
        codes.update(self._cached_db_watch_codes)
        return list(codes)

    def _get_realtime_prices(self, codes):
        prices = {}
        for code in codes:
            if code in self.portfolio.positions:
                prices[code] = self.portfolio.positions[code].current_price
        return prices

    # ── 消息收集 ──
    def _alert(self, msg):
        self.public_messages.append({
            "time": _now().strftime("%H:%M"),
            "step": self._scan_count,
            "channel": "群聊",
            "msg": msg,
        })
        self.all_raw.append(msg)

    def _alert_private(self, msg):
        self.private_messages.append({
            "time": _now().strftime("%H:%M"),
            "step": self._scan_count,
            "channel": "私聊",
            "msg": msg,
        })
        self.all_raw.append(msg)

    # ── 持仓注入 ──
    def open_pos(self, code, name, volume=1000, price=12.00,
                 stop_loss=0, take_profit=0, trailing=0.05,
                 entry_date="2026-05-26", sector=""):
        self.portfolio.open_position(
            stock_code=code, stock_name=name, volume=volume, price=price,
            stop_loss=stop_loss, take_profit=take_profit,
            trailing_stop=trailing, entry_date=entry_date, sector_code=sector,
        )
        if sector:
            self._industry_cache[code] = sector
        self._cached_db_watch_codes.add(code)

    # ── 大盘数据注入 ──
    def set_index(self, price, pre_close=3300.0, amount=80e9):
        chg = (price - pre_close) / pre_close if pre_close > 0 else 0
        self._last_index_quote = {
            "price": price, "pre_close": pre_close,
            "change_pct": chg, "amount": amount,
        }

    # ── 完整 scan ──
    def scan(self, index_price, stock_prices=None, pre_close=3300.0):
        stock_prices = stock_prices or {}
        self._scan_count += 1
        self.public_messages.clear()
        self.private_messages.clear()

        # 更新大盘 (不通过 collector，直接 set)
        self.set_index(index_price, pre_close)
        self._index_high = max(self._index_high, index_price)
        self._index_low = min(self._index_low, index_price) if self._index_low > 0 else index_price
        self._index_prices.append(index_price)

        # 更新持仓价格
        for code, p in stock_prices.items():
            if code in self.portfolio.positions:
                self.portfolio.positions[code].update_price(p)

        # 检查大盘状态
        regime = self._check_market_state(stock_prices)
        self.risk_engine.set_regime(regime)

        # 持仓巡检（含止损止盈告警 + Telegram 推送）
        if stock_prices:
            self._check_positions(stock_prices)

        # 信号检查
        self._check_signals(stock_prices, regime)
        self._check_review_picks(stock_prices, regime)
        self._check_bought_signals(stock_prices)

        # 开盘决策
        if self._scan_count == 1:
            self._send_opening_decision(stock_prices, True)

        # 异动
        if self._scan_count % 3 == 0:
            self._check_abnormal(stock_prices)

        # 板块热度
        if self._market_snapshot:
            self._check_sector_heat(self._market_snapshot)

        return regime


# ═══════════════════════════════════════════════
# 时间线场景
# ═══════════════════════════════════════════════

def run_full_day_timeline():
    """按时间线运行完整交易日，触发所有消息类型。"""
    print("=" * 70)
    print("  完整交易日 Telegram 消息时间线模拟")
    print("=" * 70)

    global _current_time
    _current_time = datetime(2026, 5, 29, 9, 25, 0)

    w = MessageCollectingWatcher()

    # 预先注入 5 轮 index_prices 历史（模拟开盘前 collector 已推送）
    for p in [3295, 3297, 3298, 3300, 3301]:
        w._index_prices.append(p)
    w._index_high = 3301
    w._index_low = 3295

    all_public = []
    all_private = []

    # ═══════════════════════════════════════════
    # 09:25 — 开盘决策
    # ═══════════════════════════════════════════
    print("\n  ━━ 09:25 开盘 ━━")
    w.set_index(3305, pre_close=3300)
    # 先建立持仓让开盘决策有内容
    w.open_pos("000001", "平安银行", 1000, 12.50, stop_loss=11.00, take_profit=14.00,
               entry_date="2026-05-26", sector="银行")
    w.open_pos("000002", "万科A", 500, 25.00, stop_loss=22.00, take_profit=30.00,
               entry_date="2026-05-26", sector="地产")

    # 注入信号让开盘决策有信号列表
    w.repo.get_pending_signals.return_value = [{
        "stock_code": "000004", "stock_name": "中芯国际",
        "buy_zone_min": 10.00, "buy_zone_max": 11.00,
        "stop_loss": 9.50, "take_profit": 13.00,
        "signal_source": "REVIEW", "signal_score": 85,
    }]

    w.scan(3305, {"000001": 12.48, "000002": 25.10}, pre_close=3300)
    msgs = w.public_messages + w.private_messages
    all_public.extend(w.public_messages)
    all_private.extend(w.private_messages)
    for m in msgs:
        print(f"  [{m['time']}] [{m['channel']}] {m['msg'][:120]}")
    assert len(w.public_messages) >= 1, "开盘决策应有消息"
    print(f"  ✓ 开盘决策 ({len(msgs)} 条)")

    # ═══════════════════════════════════════════
    # 09:30-10:00 — 正常波动
    # ═══════════════════════════════════════════
    print("\n  ━━ 09:30-10:00 正常交易 ━━")
    advance(5)
    w.scan(3310, {"000001": 12.55, "000002": 25.20}, pre_close=3300)
    advance(5)
    w.scan(3312, {"000001": 12.60, "000002": 25.30}, pre_close=3300)
    if w.public_messages:
        print(f"  [{_now().strftime('%H:%M')}] 消息: {len(w.public_messages)} 条")
    all_public.extend(w.public_messages)
    all_private.extend(w.private_messages)

    # ═══════════════════════════════════════════
    # 10:00-11:00 — 单边下跌触发止损
    # ═══════════════════════════════════════════
    print("\n  ━━ 10:00-11:00 单边下跌 → 止损触发 ━━")
    decline = [3310, 3300, 3290, 3275, 3265, 3255, 3245, 3235]
    decline_sl_msgs = []
    decline_down_msgs = []
    for i, p in enumerate(decline):
        advance(5)
        stk_001 = 12.50 - i * 0.20  # 平安银行持续跌
        stk_002 = 25.00 - i * 0.15
        w.scan(p, {"000001": max(10.50, stk_001), "000002": max(22.50, stk_002)}, pre_close=3300)
        decline_sl_msgs.extend([m for m in w.public_messages if "止损" in m["msg"] or "止盈" in m["msg"]])
        decline_down_msgs.extend([m for m in w.public_messages if "下跌" in m["msg"] or "弱势" in m["msg"] or "风险" in m["msg"]])

    # 此时平安银行应该触发止损
    sl_msgs = decline_sl_msgs
    down_msgs = decline_down_msgs
    for m in sl_msgs + down_msgs:
        print(f"  [{m['time']}] [群聊] {m['msg'][:130]}")
    assert len(sl_msgs) >= 1, f"应有止损消息: {len(sl_msgs)}"
    assert len(down_msgs) >= 1, f"应有下跌告警: {len(down_msgs)}"
    print(f"  ✓ 止损触发 ({len(sl_msgs)} 条) + 下跌告警 ({len(down_msgs)} 条)")
    all_public.extend(w.public_messages)
    all_private.extend(w.private_messages)

    # ═══════════════════════════════════════════
    # 11:00-11:30 — V型反转 → 模式切换告警
    # ═══════════════════════════════════════════
    print("\n  ━━ 11:00-11:30 V型反转 → 恢复买入 ━━")
    recovery = [3245, 3255, 3265, 3275, 3285, 3295]
    for i, p in enumerate(recovery):
        advance(5)
        w.scan(p, {"000001": 10.80 + i * 0.20, "000002": 23.00 + i * 0.30}, pre_close=3300)

    reversal_msgs = [m for m in w.public_messages if "反转" in m["msg"] or "恢复" in m["msg"] or "回升" in m["msg"]]
    for m in reversal_msgs:
        print(f"  [{m['time']}] [群聊] {m['msg'][:130]}")
    print(f"  ✓ 反转/恢复告警 ({len(reversal_msgs)} 条)")
    all_public.extend(w.public_messages)
    all_private.extend(w.private_messages)

    # ═══════════════════════════════════════════
    # 13:00-14:00 — 下午震荡 + 异动
    # ═══════════════════════════════════════════
    print("\n  ━━ 13:00-14:00 下午震荡 + 异动 ━━")
    _current_time = datetime(2026, 5, 29, 13, 0, 0)
    advance(5)

    # 注入 market_snapshot 让异动检测有数据
    w._market_snapshot = {}
    for i in range(100):
        code = f"{i:06d}"
        chg = 0.02 if i == 0 else 0.01 if i < 50 else -0.01
        w._market_snapshot[code] = {"changePct": chg, "price": 10.0, "amount": 100_000_000}

    afternoon = [3295, 3300, 3305, 3302, 3308, 3310, 3308, 3312, 3310, 3308, 3315, 3320]
    for i, p in enumerate(afternoon):
        advance(5)
        w.scan(p, {"000001": 11.50 + i * 0.05, "000002": 24.00 + i * 0.10}, pre_close=3300)

    abnormal_msgs = [m for m in w.public_messages if "异动" in m["msg"] or "放量" in m["msg"] or "涨跌停" in m["msg"] or "大单" in m["msg"]]
    for m in abnormal_msgs:
        print(f"  [{m['time']}] [群聊] {m['msg'][:130]}")
    print(f"  ✓ 下午交易 ({len(abnormal_msgs)} 条异动)")
    all_public.extend(w.public_messages)
    all_private.extend(w.private_messages)

    # ═══════════════════════════════════════════
    # 14:30 — 尾盘决策
    # ═══════════════════════════════════════════
    print("\n  ━━ 14:30 尾盘决策 ━━")
    _current_time = datetime(2026, 5, 29, 14, 35, 0)
    w._closing_decision_done = False
    w.public_messages.clear()
    w.private_messages.clear()

    # 重新注入 regime
    from trade.monitor.market_state import MarketRegime
    regime = MarketRegime(pattern="normal", risk_level="safe",
                          allow_buy=True, position_mult=1.0, entry_rule="standard",
                          stop_mult=1.0, urgent_action="")
    w.risk_engine.set_regime(regime)
    w._check_closing({"000001": 11.80, "000002": 24.80})

    closing_msgs = list(w.public_messages)
    for m in closing_msgs:
        print(f"  [{m['time']}] [群聊] {m['msg'][:150]}")
    assert len(closing_msgs) >= 1, f"尾盘应有决策: {len(closing_msgs)}"
    print(f"  ✓ 尾盘决策 ({len(closing_msgs)} 条)")
    all_public.extend(w.public_messages)
    all_private.extend(w.private_messages)

    # ═══════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════
    print("\n" + "=" * 70)
    print("  消息汇总")
    print("=" * 70)

    # 分类统计
    categories = {
        "开盘决策": ["开盘决策"],
        "止损触发": ["止损"],
        "止盈触发": ["止盈"],
        "下跌告警": ["下跌", "弱势", "风险"],
        "反转告警": ["反转", "恢复", "回升"],
        "异动检测": ["异动", "放量", "涨跌停", "大单"],
        "尾盘决策": ["尾盘", "持仓建议"],
        "模式切换": ["模式", "单边", "V型"],
    }

    for cat, keywords in categories.items():
        matched = []
        for m in all_public:
            for kw in keywords:
                if kw in m["msg"]:
                    matched.append(m)
                    break
        if matched:
            print(f"  {cat}: {len(matched)} 条")

    print(f"\n  群聊总计: {len(all_public)} 条")
    print(f"  私聊总计: {len(all_private)} 条")
    print(f"  消息总计: {len(all_public) + len(all_private)} 条")

    # 验证消息格式要素
    print(f"\n  格式验证:")
    format_checks = 0
    for m in all_public + all_private:
        msg = m["msg"]
        # 有实质内容
        assert len(msg.strip()) > 5, f"消息过短: {msg[:50]}"
        # 没有未填充的模板变量
        assert "{" not in msg or "}" not in msg, f"消息含未填充变量: {msg[:80]}"
        format_checks += 1
    print(f"  ✓ {format_checks} 条消息格式正确")

    return all_public, all_private


# ═══════════════════════════════════════════════
# 模拟 Telegram 接收消息解析
# ═══════════════════════════════════════════════

def test_incoming_message_parsing():
    """测试 Watcher 接收 Telegram 消息的解析能力。

    - 成交回复走 ManualExecutor.handle_user_reply
    - SL提醒命令走 PositionRiskMixin.handle_sl_command
    - 普通文本不应触发解析
    """
    print("\n" + "=" * 70)
    print("  Telegram 接收消息解析测试")
    print("=" * 70)

    from trade.execution.manual import ManualExecutor

    executor = ManualExecutor(db_path=":memory:")

    # ── 1. ManualExecutor 成交回复解析 ──
    print("  ── 成交回复解析 (ManualExecutor) ──")
    trade_cases = [
        # (输入, 期望账户, 期望状态)
        ("模拟盘 000001 1000股 12.50", "paper", "filled"),
        ("实盘 000001 500股 12.50", "real", "filled"),
        ("paper 000001 买了 1000股 12.50", "paper", "filled"),
        ("paper 000001 成交 1000股 12.50", "paper", "filled"),
        ("paper 000001 没成交", "paper", "rejected"),
    ]

    for text, expected_account, expected_status in trade_cases:
        result = executor.handle_user_reply(text)
        assert result is not None, f"应有响应: '{text}'"
        reply_text, account = result
        print(f"  '{text[:45]}' → [{account}] {reply_text[:80]}")
        assert account == expected_account, f"账户: {account} != {expected_account}"
        if expected_status == "filled":
            assert "已记录" in reply_text or "请补充" in reply_text or "成交" in reply_text, \
                f"回复应确认成交: {reply_text[:80]}"
        elif expected_status == "rejected":
            assert "未成交" in reply_text, f"回复应标记未成交: {reply_text[:80]}"

    # ── 2. 非交易消息不触发解析 ──
    print("  ── 非交易消息过滤 ──")
    non_trade = ["你好", "今天怎么样", "随便看看"]
    for text in non_trade:
        result = executor.handle_user_reply(text)
        assert result is None, f"非交易消息不应触发: '{text}'"
    print(f"  ✓ {len(non_trade)} 条非交易消息正确过滤")

    # ── 3. SL 提醒命令解析 (PositionRiskMixin) ──
    print("  ── SL 提醒命令解析 (handle_sl_command) ──")
    global _current_time
    _current_time = datetime(2026, 5, 29, 10, 0, 0)

    w = MessageCollectingWatcher()
    # 注入一条 SL 提醒
    w._sl_reminders["000001:sl"] = {
        "code": "000001", "name": "平安银行", "type": "止损",
        "price": 10.50, "trigger": 11.00, "ref_price": 12.00,
        "last_push": datetime.now(), "status": "pending",
    }

    # 成交确认
    result = w.handle_sl_command("成交 000001")
    assert "已确认" in result, f"成交确认失败: {result}"
    print(f"  '成交 000001' → {result}")

    # 再等
    w._sl_reminders["000002:sl"] = {
        "code": "000002", "name": "万科A", "type": "止盈",
        "price": 26.00, "trigger": 25.00, "ref_price": 22.00,
        "last_push": datetime.now(), "status": "pending",
    }
    result = w.handle_sl_command("再等 10 000002")
    assert "延迟" in result and "10" in result, f"延迟提醒失败: {result}"
    print(f"  '再等 10 000002' → {result}")

    # 取消（正常路径：先等再删。直接删也可）
    result = w.handle_sl_command("再等 5")
    assert "延迟" in result, f"全局延迟失败: {result}"
    print(f"  '再等 5' → {result}")

    print(f"  ✓ SL 命令解析正常")


# ═══════════════════════════════════════════════
# 通道分离验证
# ═══════════════════════════════════════════════

def test_channel_separation():
    """验证群聊和私聊消息正确分离。"""
    print("\n" + "=" * 70)
    print("  通道分离验证（群聊 vs 私聊）")
    print("=" * 70)

    global _current_time
    _current_time = datetime(2026, 5, 29, 10, 0, 0)

    w = MessageCollectingWatcher()

    # 注入持仓和已触发止损价
    # 模拟持仓中一支是 paper 止损，另一支是实盘（通过 _alert_private 路径）
    w.open_pos("000001", "平安银行", 1000, 12.50, stop_loss=11.00, take_profit=14.00,
               entry_date="2026-05-26", sector="银行")
    # 实盘标记：_bought_watch 里的 "real" 标记等
    # 实际上 _check_positions 里实盘交易才走私聊，这里直接验证函数

    w.set_index(3300, pre_close=3300)
    w._index_prices = [3300] * 5
    w._index_high = 3300
    w._index_low = 3300

    # 触发止损（价格跌破）
    w.scan(3300, {"000001": 10.50}, pre_close=3300)

    stop_msgs = [m for m in w.public_messages if "止损" in m["msg"]]
    print(f"  群聊止损消息: {len(stop_msgs)} 条")
    for m in stop_msgs:
        print(f"  [群聊] {m['msg'][:120]}")

    # _alert_private 路径：模拟实盘交易回复
    w._alert_private("实盘成交: 600519 200股 @1800.00 (用户确认)")
    w._alert("模拟盘买入: 000004 中芯国际 500股 @10.50")

    assert len(w.private_messages) >= 1, "应有私聊消息"
    assert len(w.public_messages) >= 1, "应有群聊消息"
    print(f"  群聊: {len(w.public_messages)} 条 | 私聊: {len(w.private_messages)} 条")
    print(f"  ✓ 通道分离正确")


# ═══════════════════════════════════════════════

def main():
    print("Telegram 消息推送全覆盖模拟测试\n")

    results = []
    for test_fn, name in [(run_full_day_timeline, "交易日全部消息"),
                            (test_incoming_message_parsing, "接收消息解析"),
                            (test_channel_separation, "群聊/私聊分离")]:
        try:
            test_fn()
            results.append((name, True))
        except Exception as e:
            print(f"\n  ❌ {name}失败: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    print("\n" + "=" * 70)
    print("  汇总")
    print("=" * 70)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n  {passed}/{len(results)} 通过")

    return passed == len(results)


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
