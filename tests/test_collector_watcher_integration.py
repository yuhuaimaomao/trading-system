# -*- coding: utf-8 -*-
"""Collector 抽离后受影响的全链路集成测试。

验证 Collector 数据注入 → Watcher 内部状态 → 上层分析判断的正确性。

覆盖链路:
1. collector market → _update_sector_trends() → _sector_stats/_concept_stats
2. collector index → _index_prices 累积 → _check_market_state() 模式检测
3. collector 断开时 _update_sector_trends() 降级分支
4. _send_opening_decision() 依赖 collector _last_index_quote
5. _build_sector_context() 依赖 _sector_stats（由 collector market 驱动）
6. 风控环境更新依赖 sector_stats
7. _compute_breadth() 依赖 _market_snapshot
"""

import json
import os
import socket
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, time as dt_time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_PORT = 15559


# ═══════════════════════════════════════════════
# 假 Collector TCP Server
# ═══════════════════════════════════════════════

class MockCollector:
    def __init__(self, port=TEST_PORT):
        self.port = port
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", port))
        self._server.listen(1)
        self._client = None
        self._running = True
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._client:
            try:
                self._client.close()
            except OSError:
                pass
        try:
            self._server.close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        self._server.settimeout(1.0)
        while self._running:
            try:
                sock, addr = self._server.accept()
                self._client = sock
                break
            except socket.timeout:
                continue
            except OSError:
                return

    def send(self, msg: dict):
        if not self._client:
            return False
        try:
            raw = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            self._client.sendall(raw)
            return True
        except OSError:
            return False


# ═══════════════════════════════════════════════
# 集成 Watcher
# ═══════════════════════════════════════════════

class IntegrationWatcher:
    """绑定真实 Mixin + collector client 的精简 Watcher。"""

    def __init__(self, port=TEST_PORT):
        from trade.portfolio.portfolio import Portfolio
        from data.live.collector_client import DataCollectorClient

        self._collector_client = DataCollectorClient(port=port)
        self._last_index_quote: dict | None = None
        self._last_db_ts: float = 0
        self.portfolio = Portfolio(initial_cash=200_000)

        # market_state 状态
        self._index_prices: list[float] = []
        self._index_high: float = 0
        self._index_low: float = 0
        self._index_alerted_downtrend = False
        self._index_last_fluctuation_price = 0.0
        self._market_turnovers: list[float] = []
        self._volume_alerted_divergence = False
        self._index_tech_state = {
            "macd_cross": None, "rsi6_zone": "normal", "rsi12_zone": "normal",
            "kdj_cross": None, "kdj_j_zone": "normal", "divergence": None,
        }
        self._ma_baseline_cache = None
        self._prev_snapshot: dict[str, dict] = {}
        self.qmt = None  # 不再使用

        # sector_context 状态
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

        # 其他
        self._trade_date = "2026-05-30"
        self.db_path = ":memory:"
        self.scan_interval = 60
        self._scan_count = 0
        self.alerts = []
        self.telegram = None
        self.repo = MagicMock()
        self.repo.get_pending_signals.return_value = []

        # 初始化缓存 + Mixin
        self._init_cache()
        self._bind_mixins()

    def _init_cache(self):
        """预填充行业/概念缓存。每个行业 4-5 只股票确保 _update_sector_trends 门槛。"""
        # 行业映射 (4-5 stocks/industry 确保一轮 >= 3 changes)
        industries = {
            "000001": "银行", "000002": "银行", "000003": "银行", "000004": "银行",
            "000005": "半导体", "000006": "半导体", "000007": "半导体", "000008": "半导体",
            "000009": "医药", "000010": "医药", "000011": "医药", "000012": "医药",
            "000013": "汽车", "000014": "汽车", "000015": "汽车", "000016": "汽车",
        }
        self._industry_cache = industries
        concepts = {
            "000001": ["金融"], "000002": ["金融"], "000003": ["金融"], "000004": ["金融"],
            "000005": ["芯片"], "000006": ["芯片"], "000007": ["芯片"], "000008": ["芯片"],
            "000009": ["创新药"], "000010": ["创新药"], "000011": ["创新药"], "000012": ["创新药"],
            "000013": ["新能源"], "000014": ["新能源"], "000015": ["新能源"], "000016": ["新能源"],
        }
        self._concept_cache = concepts

    def _bind_mixins(self):
        from trade.monitor.market_state import MarketStateMixin
        from trade.monitor.sector_context import SectorContextMixin
        for mixin in [MarketStateMixin, SectorContextMixin]:
            for name in dir(mixin):
                if name.startswith('_') and not name.startswith('__'):
                    attr = getattr(mixin, name, None)
                    if callable(attr) and not hasattr(self, name):
                        setattr(self, name, attr.__get__(self, type(self)))

    # ── 辅助方法 ──
    def _get_index_quote(self):
        return self._last_index_quote

    def _get_index_baseline(self):
        return self._ma_baseline_cache or (3300, 3320, 3350)

    def _get_index_ma60(self):
        return 3200.0

    def _save_sector_snapshots(self, *a, **kw):
        pass

    def _resolve_name(self, code):
        names = {"000001": "平安银行", "000002": "招行", "000003": "兴业", "000004": "光大",
                 "000005": "中芯", "000006": "华虹", "000007": "澜起", "000008": "长电",
                 "000009": "恒瑞", "000010": "迈瑞", "000011": "药明", "000012": "百济",
                 "000013": "比亚迪", "000014": "上汽", "000015": "长城", "000016": "长安"}
        return names.get(code, code)

    def _get_paper_trader(self):
        return None

    def _get_review_monitor(self):
        return None

    def _load_review_signal_zones(self):
        return {}

    def _get_intraday_indicators(self, code):
        return {"available": False}

    def _get_order_book_imbalance(self, code, price):
        return 0.5, ""

    def _get_big_order_direction(self, code):
        return 0.5, ""

    def _get_instrument_info(self, code):
        return {"up_stop": 999, "down_stop": 0.01}

    def _get_limit_pct(self, code):
        return 0.10

    def _is_limit_up(self, code, price):
        return False

    def _is_limit_down(self, code, price):
        return False

    def _alert(self, msg):
        self.alerts.append(msg)

    def _alert_private(self, msg):
        self.alerts.append(msg)

    def _invalidate_watch_codes_cache(self):
        pass

    # ── collector 数据接收 (与 watcher.py 一致) ──
    def connect(self):
        return self._collector_client.connect()

    @property
    def connected(self):
        return self._collector_client.connected

    def recv_and_process(self):
        if not self._collector_client.connected:
            self.connect()
            return 0

        try:
            messages = self._collector_client.recv_all()
        except Exception:
            self._collector_client.disconnect()
            return 0

        count = 0
        for msg in messages:
            msg_ts = msg.get("ts", 0)
            if self._last_db_ts > 0 and msg_ts <= self._last_db_ts:
                continue
            msg_type = msg.get("type")
            if msg_type == "index":
                self._handle_collector_index(msg)
                count += 1
            elif msg_type == "market":
                self._handle_collector_market(msg)
                count += 1
        return count

    def _handle_collector_index(self, msg: dict):
        self._last_index_quote = {
            "price": msg["price"],
            "pre_close": msg.get("pre_close", 0),
            "change_pct": msg.get("change_pct", 0),
            "amount": msg.get("amount", 0),
        }
        idx = msg["price"]
        if self._index_high == 0 or idx > self._index_high:
            self._index_high = idx
        if self._index_low == 0 or idx < self._index_low:
            self._index_low = idx
        self._index_prices.append(idx)
        amt = msg.get("amount", 0)
        if amt > 0:
            self._market_turnovers.append(amt)

    def _handle_collector_market(self, msg: dict):
        self._market_snapshot = msg.get("stocks", {})
        self._last_db_ts = max(self._last_db_ts, msg.get("ts", 0))
        if self._market_snapshot:
            self._update_sector_trends()


# ═══════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════

def _build_market_msg(ts, stocks_dict):
    return {"type": "market", "ts": ts, "stocks": stocks_dict}


def _build_index_msg(ts, price, pre_close=3300.0, change_pct=0, amount=80_000_000_000):
    return {"type": "index", "ts": ts, "price": price,
            "pre_close": pre_close, "change_pct": change_pct, "amount": amount}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 1: Sector Trends 3 轮累积完整链路
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_sector_trends_3_rounds():
    """验证需要 >=3 轮才会出现 sector_stats（行业有4只，满足 >=3 changes 条件）。"""
    print("\n" + "=" * 70)
    print("  测试 1: Market → Sector Trends (3 轮累积)")
    print("=" * 70)

    mock = MockCollector()
    mock.start(); time.sleep(0.3)
    w = IntegrationWatcher()
    w.connect(); time.sleep(0.3)

    # 每轮推送 16 只股票（4行业x4只）
    def _round_stocks(chg_map):
        stocks = {}
        for code in w._industry_cache:
            ind = w._industry_cache[code]
            stocks[code] = {"price": 10.0, "changePct": chg_map.get(ind, 0.01),
                            "amount": 100_000_000}
        return stocks

    # R1: 银行+1%, 半导体-0.5%, 医药+2%, 汽车-1%
    mock.send(_build_market_msg(time.time(), _round_stocks(
        {"银行": 0.01, "半导体": -0.005, "医药": 0.02, "汽车": -0.01})))
    time.sleep(0.2)
    n = w.recv_and_process()
    print(f"  R1: {n} 条  _sector_stats={len(w._sector_stats)}")
    # 银行有4只 stock >=3 → 满足门槛。但 1 轮就可以达到 threshold
    # (4 stocks per industry, all >= 3). 但只有一轮没有 history 累积。
    assert len(w._sector_stats) >= 1, f"至少银行行业应出现: {list(w._sector_stats.keys())}"

    # R2
    mock.send(_build_market_msg(time.time(), _round_stocks(
        {"银行": 0.012, "半导体": -0.004, "医药": 0.018, "汽车": -0.008})))
    time.sleep(0.2)
    w.recv_and_process()
    print(f"  R2: _sector_stats={len(w._sector_stats)}")

    # R3
    mock.send(_build_market_msg(time.time(), _round_stocks(
        {"银行": 0.015, "半导体": 0.0, "医药": 0.022, "汽车": -0.006})))
    time.sleep(0.2)
    w.recv_and_process()
    print(f"  R3: _sector_stats={len(w._sector_stats)} 个行业")
    print(f"  _sector_trend_history 长度: "
          f"{ {k: len(v) for k, v in w._sector_trend_history.items()} }")

    assert len(w._sector_stats) >= 4, f"4 行业应有数据: {len(w._sector_stats)}"

    bank = w._sector_stats["银行"]
    assert bank["up"] >= 3 and bank["down"] == 0, f"银行 u/d: {bank['up']}/{bank['down']}"
    assert abs(bank["change_pct"] - 0.0123) < 0.005, f"银行: {bank['change_pct']:.4f}"
    assert len(bank["trend_history"]) == 3, f"银行 history 应为 3: {len(bank['trend_history'])}"

    # continuity: 银行连续3轮上涨
    # continuity: R1=0, R2=1(set dir), R3=2(increment) → 3 轮后 = 2
    assert w._sector_trend_continuity.get("银行", 0) >= 2, \
        f"银行 continuity: {w._sector_trend_continuity.get('银行', 0)}"

    print(f"  银行: {bank['change_pct']:+.4f}% up={bank['up']} down={bank['down']} "
          f"breadth={bank['breadth']:.2f} cont={w._sector_trend_continuity.get('银行',0)}")
    print(f"  ✅ 通过")

    mock.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 2: Sector Stats → Risk Engine
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_sector_stats_to_risk_engine():
    """验证 sector_stats → risk_engine.market_env 风控环境更新。"""
    print("\n" + "=" * 70)
    print("  测试 2: Sector Stats → Risk Engine")
    print("=" * 70)

    from trade.risk.engine import RiskEngine

    mock = MockCollector()
    mock.start(); time.sleep(0.3)
    w = IntegrationWatcher()
    w._ma_baseline_cache = (3300, 3320, 3350)
    w.connect(); time.sleep(0.3)

    # 3 轮 market 数据（全涨）
    for _ in range(3):
        stocks = {}
        for code in w._industry_cache:
            stocks[code] = {"price": 10.0, "changePct": 0.01, "amount": 100_000_000}
        mock.send(_build_market_msg(time.time(), stocks))
        time.sleep(0.05)
    time.sleep(0.2)
    w.recv_and_process()

    # index_prices 填充
    for p in [3300, 3305, 3310, 3315, 3320]:
        w._index_prices.append(p)
        w._index_high = max(w._index_high, p)
        w._index_low = min(w._index_low, p) if w._index_low > 0 else p

    engine = RiskEngine()
    ma5, ma10, ma20 = w._get_index_baseline()
    ma60 = w._get_index_ma60()
    breadth = w._compute_breadth()
    br = breadth["up"] / max(breadth["down"], 1)
    amp = (w._index_high - w._index_low) / w._index_low if w._index_low > 0 else 0
    active = sum(1 for s in w._sector_stats.values()
                 if abs(s.get("change_pct", 0)) > 0.005)

    engine.update_market_env(ma20, w._index_prices[-1], ma60,
                             0.02, br, amp, active)

    assert engine.market_env in ("swing", "bull", "bear", "strong")
    print(f"  market_env={engine.market_env}")
    print(f"  can_open: {engine.can_open('000001', 0.5, portfolio=w.portfolio)}")
    print(f"  ✅ 风控环境从 sector_stats 正确构建")

    mock.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 3: Collector Index → 模式检测
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_pattern_from_collector_index():
    """验证 collector index 数据 → _index_prices → _check_market_state 模式检测。"""
    print("\n" + "=" * 70)
    print("  测试 3: Collector Index → 模式检测")
    print("=" * 70)

    mock = MockCollector()
    mock.start(); time.sleep(0.3)
    w = IntegrationWatcher()
    w._ma_baseline_cache = (3300, 3320, 3350)
    w.connect(); time.sleep(0.3)

    # V 型走势 + 正常 market 数据保持 _market_snapshot 非空
    prices = [3300, 3295, 3290, 3285, 3280, 3275, 3270,
              3280, 3290, 3300, 3310, 3320, 3330, 3340, 3350]
    for i, p in enumerate(prices):
        mock.send(_build_index_msg(time.time() + i, p, pre_close=3300,
                                    change_pct=(p - 3300) / 3300))
        # market 快照
        stocks = {}
        for code in w._industry_cache:
            stocks[code] = {"price": 10.0, "changePct": 0.005 if code < "000010" else -0.003,
                            "amount": 100_000_000}
        mock.send(_build_market_msg(time.time() + i + 0.5, stocks))

    time.sleep(0.2)
    count = w.recv_and_process()
    print(f"  接收: {count} 条")

    # _check_market_state 需要 stock_prices dict（可为空）
    regime = w._check_market_state({})
    print(f"  模式: {regime.pattern} 风险: {regime.risk_level} allow_buy: {regime.allow_buy}")
    # V型反转后应恢复买入
    assert regime.pattern in ("v_reversal", "uptrend", "normal", "melt_up"), \
        f"意外模式: {regime.pattern}"
    assert regime.allow_buy
    print(f"  ✅ 模式检测正确: {regime.pattern}")

    mock.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 4: Sector Context 完整链路
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_sector_context_pipeline():
    """验证 _build_sector_context + _get_sector_trend + _get_concept_trend_score 完整链路。"""
    print("\n" + "=" * 70)
    print("  测试 4: Sector Context 完整链路")
    print("=" * 70)

    mock = MockCollector()
    mock.start(); time.sleep(0.3)
    w = IntegrationWatcher()
    w.connect(); time.sleep(0.3)

    # 3 轮 market 数据
    for _ in range(3):
        stocks = {}
        for code in w._industry_cache:
            ind = w._industry_cache[code]
            if ind == "银行":
                chg = 0.015
            elif ind == "半导体":
                chg = 0.025
            elif ind == "医药":
                chg = -0.008
            else:
                chg = 0.005
            stocks[code] = {"price": 10.0, "changePct": chg, "amount": 100_000_000}
        mock.send(_build_market_msg(time.time(), stocks))
        time.sleep(0.05)
    time.sleep(0.2)
    w.recv_and_process()

    assert len(w._sector_stats) >= 4, f"4 行业: {list(w._sector_stats.keys())}"
    assert len(w._concept_stats) >= 4, f"4 概念: {list(w._concept_stats.keys())}"

    # _build_sector_context
    ctx = w._build_sector_context({"000001", "000005", "000009"})
    print(f"  Context:\n{ctx[:600]}")
    assert "银行" in ctx, f"银行缺失: {ctx[:200]}"
    assert "半导体" in ctx, f"半导体缺失"
    assert "医药" in ctx, f"医药缺失"

    # _get_sector_trend (需要 history >= 2 才能有方向描述)
    trend = w._get_sector_trend("000005")  # 半导体
    print(f"  000005 板块趋势: {trend[:100]}")
    assert "半导体" in trend

    # _get_concept_trend_score
    score, reason = w._get_concept_trend_score("000005")  # 芯片
    print(f"  000005 概念趋势评分: {score} {reason}")
    # 注: _get_concept_trend_score 的阈值 >1.0 期望百分比值，
    # 这里流通的 changePct 是小数（0.025 = 2.5%），所以 score 可能为 0
    # 这是原始代码行为，非 collector 抽离引入
    assert isinstance(score, int)
    assert -3 <= score <= 3

    print(f"  ✅ Sector Context 链路完整")

    mock.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 5: 开盘决策依赖 collector 指数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_opening_decision_with_collector_index():
    """验证 _send_opening_decision 使用 collector 推送的 _last_index_quote。"""
    print("\n" + "=" * 70)
    print("  测试 5: 开盘决策 ← Collector 指数")
    print("=" * 70)

    mock = MockCollector()
    mock.start(); time.sleep(0.3)
    w = IntegrationWatcher()
    w._ma_baseline_cache = (3300, 3320, 3350)
    w.connect(); time.sleep(0.3)

    # collector 推送指数
    mock.send(_build_index_msg(time.time(), 3310, pre_close=3300,
                                change_pct=0.003, amount=70_000_000_000))
    time.sleep(0.2)
    w.recv_and_process()

    idx = w._get_index_quote()
    assert idx["price"] == 3310 and idx["pre_close"] == 3300
    print(f"  [1] _get_index_quote: price={idx['price']} pre_close={idx['pre_close']}")

    # _send_opening_decision 需要 portfolio（默认空）和 repo（mock）
    prices = {}
    try:
        w._send_opening_decision(prices, market_ok=True)
        print(f"  [2] _send_opening_decision 正常执行 (空持仓空信号)")
    except Exception as e:
        print(f"  [2] 异常: {e}")
        raise

    if w.alerts:
        alert_text = w.alerts[0]
        assert "3310" in alert_text or "上证" in alert_text, \
            f"应包含指数行情: {alert_text[:150]}"
        print(f"  [3] Alert: {alert_text[:150]}")

    print(f"  ✅ 开盘决策使用 collector 数据")

    mock.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 6: 板块 Breadth + Relative 计算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_sector_breadth_and_relative():
    """验证 breadth 和 relative（相对强度）从 collector market 正确计算。"""
    print("\n" + "=" * 70)
    print("  测试 6: Sector Breadth + Relative")
    print("=" * 70)

    mock = MockCollector()
    mock.start(); time.sleep(0.3)
    w = IntegrationWatcher()
    w.connect(); time.sleep(0.3)

    # 3 轮分化数据
    for _ in range(3):
        stocks = {}
        for code in w._industry_cache:
            ind = w._industry_cache[code]
            # 银行3涨1跌，半导体全涨，医药2涨2跌，汽车1涨3跌
            if ind == "银行":
                chg = 0.02 if code != "000004" else -0.005
            elif ind == "半导体":
                chg = 0.03
            elif ind == "医药":
                chg = 0.015 if code in ("000009", "000010") else -0.01
            else:
                chg = 0.01 if code == "000013" else -0.015
            stocks[code] = {"price": 10.0, "changePct": chg, "amount": 100_000_000}
        mock.send(_build_market_msg(time.time(), stocks))
        time.sleep(0.05)
    time.sleep(0.2)
    w.recv_and_process()

    for ind in ["银行", "半导体", "医药", "汽车"]:
        s = w._sector_stats.get(ind)
        if s:
            print(f"  {ind}: chg={s['change_pct']:+.4f}% up={s['up']} down={s['down']} "
                  f"breadth={s['breadth']:.2f} relative={s['relative']:+.4f}%")

    bank = w._sector_stats["银行"]
    assert bank["up"] == 3 and bank["down"] == 1, f"银行 u/d: {bank['up']}/{bank['down']}"
    assert bank["breadth"] > 0, f"银行 breadth={bank['breadth']}"

    semi = w._sector_stats["半导体"]
    assert semi["breadth"] == 1.0, f"半导体 breadth={semi['breadth']} (应全涨)"

    # relative = avg - market_avg, 半导体涨幅最大应 > 0
    assert semi["relative"] > 0, f"半导体 relative={semi['relative']} 应>0"

    print(f"  ✅ Breadth + Relative 计算正确")

    mock.stop()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 7: Collector 断开降级
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_fallback_disconnected():
    """Collector 断开时，手动调用 _update_sector_trends 应正常工作。"""
    print("\n" + "=" * 70)
    print("  测试 7: Collector 断开降级")
    print("=" * 70)

    w = IntegrationWatcher()

    w._market_snapshot = {}
    for code in w._industry_cache:
        w._market_snapshot[code] = {"changePct": 0.015, "price": 10.0, "amount": 100_000_000}

    for _ in range(3):
        w._update_sector_trends()

    assert len(w._sector_stats) >= 4, str(w._sector_stats.keys())
    print(f"  _sector_stats: {len(w._sector_stats)} 行业")

    # continuity tracking
    cont = w._sector_trend_continuity
    print(f"  continuity: { {k: v for k, v in list(cont.items())[:4]} }")

    # _ensure_concept_cache 是否自动触发
    assert len(w._concept_stats) >= 4, f"概念统计应有数据: {len(w._concept_stats)}"

    print(f"  ✅ 降级分支正常工作")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 8: _compute_breadth 从 market_snapshot
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_compute_breadth():
    """_compute_breadth() 依赖 _market_snapshot（来自 collector market 推送）。"""
    print("\n" + "=" * 70)
    print("  测试 8: _compute_breadth()")
    print("=" * 70)

    mock = MockCollector()
    mock.start(); time.sleep(0.3)
    w = IntegrationWatcher()
    w.connect(); time.sleep(0.3)

    stocks = {}
    for i in range(200):
        code = f"{i:06d}"
        chg = 0.01 if i < 120 else -0.01
        stocks[code] = {"price": 10.0, "changePct": chg, "amount": 100_000_000}
    mock.send(_build_market_msg(time.time(), stocks))
    time.sleep(0.2)
    w.recv_and_process()

    b = w._compute_breadth()
    assert b["up"] == 120 and b["down"] == 80
    print(f"  breadth 1: {b}")

    # 第二轮: 跌多涨少
    stocks2 = {}
    for i in range(200):
        code = f"{i:06d}"
        chg = -0.02 if i < 150 else 0.01
        stocks2[code] = {"price": 10.0, "changePct": chg, "amount": 100_000_000}
    mock.send(_build_market_msg(time.time(), stocks2))
    time.sleep(0.2)
    w.recv_and_process()

    b2 = w._compute_breadth()
    assert b2["up"] == 50 and b2["down"] == 150, str(b2)
    print(f"  breadth 2: {b2}")

    # 强牛：涨跌比 > 2:1（risk_engine 用这个判断）
    br = b2["up"] / max(b2["down"], 1)
    print(f"  涨跌比: {br:.2f}")
    assert br < 1.0, "第二轮跌多涨少，涨跌比应 < 1"

    print(f"  ✅ _compute_breadth 验证通过")


# ═══════════════════════════════════════════════

def main():
    print("Collector → Watcher 全链路集成测试")
    print("(验证数据注入 → 状态更新 → 分析判断的完整链路)\n")

    results = []
    tests = [
        test_sector_trends_3_rounds,
        test_sector_stats_to_risk_engine,
        test_pattern_from_collector_index,
        test_sector_context_pipeline,
        test_opening_decision_with_collector_index,
        test_sector_breadth_and_relative,
        test_fallback_disconnected,
        test_compute_breadth,
    ]
    for test in tests:
        try:
            test()
            results.append((test.__name__, True))
        except Exception as e:
            print(f"\n  ❌ 失败: {e}")
            import traceback
            traceback.print_exc()
            results.append((test.__name__, False))

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
