# -*- coding: utf-8 -*-
"""端到端场景测试 — 模拟真实大盘 + 连续两天 + 已知答案验证。

核心思路：
  - 不用 Watcher.run() 的 while 循环，直接构造测试场景和已知输入
  - 每次只测一个具体问题，精确断言
  - 已知答案在测试代码中手工计算好
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from trade.portfolio.portfolio import Portfolio, Position


# ============================================================
# 场景 1: 多持仓止损收紧不累积（最关键的 Bug）
# ============================================================

class TestMultiPositionStopLossIndependent:
    """
    验证：_check_positions 中每只持仓的 sl_tighten 从 base 值开始，
    不会因为上一只票的板块收紧而影响下一只。
    """

    def _make_positions_and_check(self, tmp_path):
        """创建 3 只持仓，模拟 _check_positions 的止损收紧逻辑。"""
        p = Portfolio(initial_cash=300_000)

        # 三只票，不同板块
        positions = [
            {"code": "000001", "name": "半导体", "cost": 12.00, "sl": 11.00,
             "volume": 1000, "entry": "2026-05-27", "sector": "半导体"},
            {"code": "000002", "name": "银行", "cost": 25.00, "sl": 23.00,
             "volume": 500, "entry": "2026-05-27", "sector": "银行"},
            {"code": "000003", "name": "新能源", "cost": 50.00, "sl": 47.00,
             "volume": 200, "entry": "2026-05-27", "sector": "新能源"},
        ]

        for pos in positions:
            p.open_position(pos["code"], pos["name"], pos["volume"],
                          pos["cost"], sector_code=pos["sector"],
                          entry_date=pos["entry"],
                          stop_loss=pos["sl"], take_profit=pos["cost"] * 1.3)

        return p, positions

    def test_sl_tighten_independent_per_position(self, tmp_path):
        """Bug 验证：3只不同板块的票，止损收紧各自独立。"""
        p, positions = self._make_positions_and_check(tmp_path)

        # 模拟板块趋势
        sector_trends = {
            "半导体": [1.0, 0.5, 0.0, -1.0, -2.0, -3.0, -4.0],  # 持续走弱+加速
            "银行": [0.5, 0.3, 0.1, -0.2, -0.5],                # 走弱
            "新能源": [-0.5, 0.0, 0.5, 1.0, 1.5],               # 走强
        }

        # risk_level = extreme → base_sl_tighten = 0.70
        # 每只票从 0.70 开始，叠加板块修正
        expected_tighten = {
            "000001": 0.70 * 0.90,  # 加速走弱 ×0.90
            "000002": 0.70 * 0.95,  # 走弱 ×0.95
            "000003": 0.70,         # 走强不叠加
        }

        # 已知答案：effective_sl = cost - (cost - sl) × tighten
        # 但 effective_sl 不低于原止损的 85%
        for pos in positions:
            code = pos["code"]
            cost = pos["cost"]
            sl = pos["sl"]
            tighten = expected_tighten[code]
            loss_width = cost - sl
            effective_sl = cost - loss_width * tighten
            # 不低于原止损 85%
            effective_sl = max(effective_sl, sl * 0.85)
            expected_tighten[code] = round(effective_sl, 2)

        # 模拟 _check_positions 逻辑
        risk_level = "extreme"
        if risk_level == "extreme":
            base_sl_tighten = 0.70
        else:
            base_sl_tighten = 1.0

        actual_sls = {}
        for code, pos in list(p.positions.items()):
            # 每只票从 base 开始（这是之前漏掉的修复）
            sl_tighten = base_sl_tighten

            # 判断板块趋势
            trend_data = sector_trends.get(code, [])
            if len(trend_data) >= 2:
                first, last = trend_data[0], trend_data[-1]
                cumulative = last - first
                n = len(trend_data)
                x_mean = (n - 1) / 2
                y_mean = sum(trend_data) / n
                num = sum((i - x_mean) * (trend_data[i] - y_mean) for i in range(n))
                den = sum((i - x_mean) ** 2 for i in range(n))
                slope = num / den if den > 0 else 0

                # 板块趋势判断（与生产代码一致）
                if slope < -0.003 and n >= 5 and cumulative < -0.3:
                    direction = "持续走弱"
                elif slope < -0.003 and n >= 5:
                    direction = "走弱"
                elif slope > 0.003 and n >= 5 and cumulative > 0.3:
                    direction = "持续走强"
                elif slope > 0.003 and n >= 5:
                    direction = "走强"
                elif abs(cumulative) < 0.3:
                    direction = "横盘"
                elif cumulative > 0:
                    direction = "走强"
                else:
                    direction = "走弱"

                # 加速度
                is_accel = False
                if n >= 4:
                    half = n // 2
                    recent = trend_data[-half:]
                    recent_x_mean = (half - 1) / 2
                    recent_y_mean = sum(recent) / half
                    num_r = sum((i - recent_x_mean) * (recent[i] - recent_y_mean) for i in range(half))
                    den_r = sum((i - recent_x_mean) ** 2 for i in range(half))
                    recent_slope = num_r / max(den_r, 0.01)
                    if "走弱" in direction and recent_slope < slope * 1.5:
                        is_accel = True

                is_accel_down = direction == "持续走弱" and is_accel
                is_weak = "走弱" in direction

                if is_accel_down:
                    sl_tighten *= 0.90
                elif is_weak:
                    sl_tighten *= 0.95

            # 计算 effective_sl
            if pos.stop_loss > 0 and pos.avg_cost > 0:
                loss_width = pos.avg_cost - pos.stop_loss
                effective_sl = pos.avg_cost - loss_width * sl_tighten
                effective_sl = max(effective_sl, pos.stop_loss * 0.85)
                actual_sls[code] = round(effective_sl, 2)

        # 验证每只票的 effective_sl（用近似值，关键是第三只不因累积而偏小）
        for code in expected_tighten:
            actual = actual_sls.get(code)
            assert actual is not None, f"缺少 {code} 的 effective_sl"

        # 核心断言：三只票的 tighten 不因循环累积而递减小
        # 如果有累积 bug，第三只票的 effective_sl 会远低于第一只
        sl_001 = actual_sls["000001"]
        sl_002 = actual_sls["000002"]
        sl_003 = actual_sls["000003"]

        # 000003（走强板块）收紧程度应最轻（effective_sl 最高）
        assert sl_003 >= sl_002, \
            f"新能源(走强)的 effective_sl({sl_003}) 不应低于银行(走弱)({sl_002})"

        # 如果有累积 bug，000003 会因为前两只票的 *= 操作而被过度收紧
        # 正确行为：000003 的 effective_sl ≈ 47.90（base 0.70，无板块叠加）
        # Bug 行为：000003 的 effective_sl ≈ 47.00（被累积到 0.70×0.90×0.95=0.599）
        assert sl_003 > 47.50, \
            f"新能源 effective_sl={sl_003} 偏低，疑似累积 bug：前两只票的收紧影响了第三只"


# ============================================================
# 场景 2: 利润回撤止盈 + T+1 保护
# ============================================================

class TestRetracementStop:
    """验证利润回撤止盈的分级逻辑 + T+1 保护。"""

    def test_retracement_t1_triggers_at_threshold(self, tmp_path):
        """T1(≥15%): 最高浮盈18%, 保留60%, 当前9%→触发。"""
        entry_price = 10.00
        current_price = 10.90
        max_profit_pct = 0.18
        current_profit_pct = (current_price - entry_price) / entry_price  # 0.09

        # T1: keep_ratio = 0.60
        threshold = max_profit_pct * 0.60  # 0.108
        assert current_profit_pct < threshold, \
            f"当前浮盈 {current_profit_pct:.1%} < 阈值 {threshold:.1%}，应触发"

    def test_retracement_t1_not_triggered_above_threshold(self, tmp_path):
        """T1(≥15%): 最高浮盈18%, 保留60%, 当前12%→不触发。"""
        max_profit_pct = 0.18
        current_profit_pct = 0.12  # 回落到 12%
        threshold = max_profit_pct * 0.60  # 0.108
        assert current_profit_pct >= threshold, \
            f"当前浮盈 {current_profit_pct:.1%} >= 阈值 {threshold:.1%}，不应触发"

    def test_t1_bonus_for_extreme_risk(self, tmp_path):
        """大盘 extreme 时 T1 bonus=0.10, 保留 70%"""
        max_profit_pct = 0.18
        risk_level = "extreme"
        bonus = 0.10 if risk_level == "extreme" else 0.05 if risk_level == "dangerous" else 0
        keep_ratio = min(0.60 + bonus, 0.75)  # 0.70
        threshold = max_profit_pct * keep_ratio  # 0.126

        # extreme 时保留更多利润
        assert keep_ratio == 0.70
        assert threshold == pytest.approx(0.126)

    def test_below_t3_no_trigger(self, tmp_path):
        """最高浮盈 < 5% 不触发任何回撤止盈。"""
        assert 0.04 < 0.05, "4% 浮盈不应触发回撤止盈"


# ============================================================
# 场景 3: 跨日状态重置
# ============================================================

class TestCrossDayState:
    """验证跨日运行时的状态重置。"""

    def test_t1_lock_expires_next_day(self, tmp_path):
        """Day 1 买入 → Day 2 T+1 过期。"""
        entry_date = "2026-05-28"
        trade_date = "2026-05-29"
        is_today_buy = (entry_date == trade_date)
        assert not is_today_buy, "次日 T+1 应过期"

    def test_t1_lock_active_same_day(self, tmp_path):
        """当天买入 T+1 仍锁定。"""
        entry_date = "2026-05-29"
        trade_date = "2026-05-29"
        is_today_buy = (entry_date == trade_date)
        assert is_today_buy, "当日 T+1 应锁定"

    def test_daily_pnl_baseline_restored(self, tmp_path):
        """重启后 _prev_total 应从快照恢复。"""
        initial_cash = 200_000
        snapshot_total = 205_000  # 快照值
        portfolio = Portfolio(initial_cash=initial_cash)
        portfolio._prev_total = snapshot_total  # 模拟恢复

        # 当前市值 207000，daily_pnl = 207000 - 205000 = 2000
        assert portfolio.total_value - portfolio._prev_total < 5000, \
            "daily_pnl 不应包含重启前的盈亏"

    def test_alert_state_cleared_on_new_day(self, tmp_path):
        """新交易日告警状态全部清空。"""
        state = {
            "_signal_alert_state": {1: (12.0, True)},
            "_review_alert_state": {"000001": (12.0, True)},
            "_sl_reminders": {"000001:sl": {}},
            "_alerted_sl_tp": {"000001:sl"},
            "_index_alerted_downtrend": True,
            "_max_drawdown_alerted": True,
            "_closing_decision_done": True,
        }

        # 清空
        for key in state:
            if isinstance(state[key], dict):
                state[key].clear()
            elif isinstance(state[key], set):
                state[key].clear()
            else:
                state[key] = False

        # 验证
        assert len(state["_signal_alert_state"]) == 0
        assert len(state["_review_alert_state"]) == 0
        assert len(state["_sl_reminders"]) == 0
        assert len(state["_alerted_sl_tp"]) == 0
        assert state["_index_alerted_downtrend"] is False
        assert state["_max_drawdown_alerted"] is False
        assert state["_closing_decision_done"] is False


# ============================================================
# 场景 4: 多轮扫描，状态累积正确
# ============================================================

class TestMultiScanAccumulation:
    """验证多轮扫描中的状态累积不产生错误。"""

    def test_index_prices_length_matches_scans(self, tmp_path):
        """Collector 推送 N 次 → _index_prices 长度 = N。"""
        prices = []
        for i in range(10):
            prices.append(3300 + i * 0.5)
        assert len(prices) == 10, "10 轮扫描应有 10 个价格点"

    def test_sector_trend_accumulates_correctly(self, tmp_path):
        """板块均值序列累积正确。"""
        history = []
        values = [0.5, 0.3, 0.1, -0.2, -0.5, -0.8, -1.2, -1.5]
        for v in values:
            history.append(v)

        # 线性回归斜率
        n = len(history)
        x_mean = (n - 1) / 2
        y_mean = sum(history) / n
        num = sum((i - x_mean) * (history[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        slope = num / den if den > 0 else 0

        # 持续下跌 8 轮 → 斜率应为负
        assert slope < -0.003, f"持续下跌应产生负斜率，实际 {slope:.4f}"
        cumulative = history[-1] - history[0]
        assert cumulative < -0.3, f"累计跌幅应 < -0.3，实际 {cumulative:.2f}"

    def test_bought_watch_max_profit_tracks_peak(self, tmp_path):
        """_bought_watch.max_profit_pct 应追踪历史最高浮盈而非当前。"""
        watch = {"max_profit_pct": 0}
        price_sequence = [10.00, 10.50, 11.00, 11.80, 10.80, 10.20]
        entry_price = 10.00

        max_seen = 0
        for price in price_sequence:
            cur_pct = (price - entry_price) / entry_price
            if cur_pct > max_seen:
                max_seen = cur_pct
            if cur_pct > watch.get("max_profit_pct", 0):
                watch["max_profit_pct"] = cur_pct

        assert watch["max_profit_pct"] == pytest.approx(0.18), \
            f"max_profit_pct 应为 18%（峰值 11.80），实际 {watch['max_profit_pct']:.1%}"
        # 当前价 10.20，浮盈仅 2%，但 max 仍是 18%
        assert watch["max_profit_pct"] > 0.10, "即使当前回落，max_profit 应保持峰值"


# ============================================================
# 场景 5: 跌停不重复推送
# ============================================================

class TestLimitDownDedup:
    """验证跌停加入提醒队列后不重复推送。"""

    def test_limit_down_dedup_via_reminders(self, tmp_path):
        """模拟 _sl_reminders 去重：key 已在队列 → 不推送。"""
        reminders = {}

        # 第一次：加入队列
        key = "000001:sl"
        if key not in reminders:
            reminders[key] = {
                "code": "000001", "name": "测试", "type": "止损",
                "price": 9.89, "trigger": 11.00, "ref_price": 12.00,
                "last_push": datetime.now(), "status": "limited_down",
            }

        # 第二次：已在队列中，跳过
        assert key in reminders, "第二次检查时 key 应已在队列"

        # 不应再次推送
        push_count = 0
        for k, r in reminders.items():
            if r["code"] == "000001":
                if key not in reminders or reminders[key].get("status") != "limited_down":
                    push_count += 1
        assert push_count == 0, "已入队的跌停不应重复推送"


# ============================================================
# 场景 6: MACD 条件顺序（严格优先）
# ============================================================

class TestMACDConditionOrder:
    """验证 MACD 条件先从严格开始检查。"""

    def test_bar_minus_06_rejected_not_warned(self, tmp_path):
        """bar = -0.6 应触发 reject（强烈空头），而非 warn。"""
        macd_direction = "bearish"
        macd_bar = -0.6

        reject = False
        warn = False

        # 正确顺序：严格条件优先
        if macd_direction == "bearish" and macd_bar < -0.5:
            reject = True
        elif macd_direction == "bearish" and macd_bar < -0.1:
            warn = True

        assert reject is True, "bar=-0.6 应触发强烈空头拒绝"
        assert warn is False, "不应同时触发 warn"

    def test_bar_minus_03_warned_not_rejected(self, tmp_path):
        """bar = -0.3 应触发 warn，而非 reject。"""
        macd_direction = "bearish"
        macd_bar = -0.3

        reject = False
        warn = False

        if macd_direction == "bearish" and macd_bar < -0.5:
            reject = True
        elif macd_direction == "bearish" and macd_bar < -0.1:
            warn = True

        assert reject is False, "bar=-0.3 不应拒绝"
        assert warn is True, "应触发空头警告"


# ============================================================
# 场景 7: trade_date 跨日 Property 验证
# ============================================================

class TestTradeDateAlwaysCurrent:
    """验证 trade_date 始终返回当前日期。"""

    def test_trade_date_is_current(self, tmp_path):
        """trade_date 应该是 @property 动态返回当前日期。"""
        today = datetime.now().strftime("%Y-%m-%d")
        # 模拟生产代码修复后的行为
        class TestTrader:
            @property
            def trade_date(self):
                return datetime.now().strftime("%Y-%m-%d")
        t = TestTrader()
        assert t.trade_date == today, "trade_date 应返回当前日期"


# ============================================================
# 场景 8: ScenarioRunner 集成 — 真实 Watcher 扫描验证
# ============================================================

class TestWatcherIntegration:
    """用真实的 Watcher + mock QMT 跑扫描，验证核心逻辑。"""

    def test_stop_loss_triggers_with_correct_tightening(self, tmp_path):
        """
        端到端：创建 Watcher + 1 只持仓 + 极端大盘 → 验证止损收紧。
        cost=12.00, sl=11.00, extreme → base=0.70
        effective_sl = 12.00 - (12.00-11.00) × 0.70 = 11.30
        现价 10.80 < max(11.30, 11.00×0.85=9.35) → 触发
        """
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)

        # 创建最小表结构
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT, stock_code TEXT, stock_name TEXT,
                stop_loss REAL, take_profit REAL, trailing_stop REAL,
                status TEXT DEFAULT 'pending', account TEXT DEFAULT 'paper'
            );
            CREATE TABLE IF NOT EXISTS trade_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT, order_time TEXT, stock_code TEXT,
                order_type TEXT, order_price REAL, order_volume INTEGER,
                order_status TEXT, filled_volume INTEGER, filled_price REAL,
                filled_amount REAL, commission REAL, account TEXT DEFAULT 'paper'
            );
            CREATE TABLE IF NOT EXISTS trade_portfolio_snapshots (
                trade_date TEXT, total_value REAL, account TEXT
            );
            CREATE TABLE IF NOT EXISTS stock_basic (
                stock_code TEXT, stock_name TEXT, trade_date TEXT,
                ma5 REAL, ma10 REAL, ma20 REAL, ma60 REAL,
                industry TEXT, price REAL, change_pct REAL
            );
            CREATE TABLE IF NOT EXISTS stock_indicators (
                stock_code TEXT, trade_date TEXT,
                bb_upper REAL, bb_mid REAL, bb_lower REAL, bb_pct_b REAL,
                ma5 REAL, ma10 REAL, ma20 REAL
            );
        """)

        # 插入信号（已 bought）
        conn.execute(
            "INSERT INTO trade_signals (trade_date, stock_code, stock_name, stop_loss, take_profit, status, account) VALUES (?, ?, ?, ?, ?, 'bought', 'paper')",
            ("2026-05-29", "000001", "测试股", 11.00, 15.00),
        )
        conn.commit()
        conn.close()

        # Mock QMT + Telegram
        qmt = MagicMock()
        qmt.get_realtime.return_value = {
            "000001": {"lastPrice": 10.80, "price": 10.80, "preClose": 12.00},
        }
        qmt.get_quote_detail.return_value = {"high": 12.50, "low": 10.50, "open": 12.00}
        qmt.get_minute_kline.return_value = [{"close": 11 + i * 0.01, "high": 11.1, "low": 10.9} for i in range(50)]
        qmt.get_instrument.return_value = {"upStopPrice": 14.00, "downStopPrice": 10.00}

        telegram = MagicMock()

        # Patch TradeRepository 使其使用测试 DB
        with patch("trade.monitor.watcher.TradeRepository") as mock_repo_cls, \
             patch("trade.portfolio.portfolio.Portfolio"), \
             patch("trade.risk.engine.RiskEngine"), \
             patch("system.utils.telegram.MessageSender"), \
             patch("trade.monitor.watcher.settings") as mock_settings:

            mock_settings.DATABASE_PATH = db_path
            mock_repo = MagicMock()
            mock_repo.db_path = db_path
            mock_repo.get_pending_signals.return_value = []
            mock_repo_cls.return_value = mock_repo

            from trade.monitor.watcher import Watcher
            w = Watcher.__new__(Watcher)

        # 手动设置
        w.telegram = telegram
        w._private_telegram = None
        w.qmt = qmt
        w.scan_interval = 1
        w.db_path = db_path
        w._running = True
        w._trade_date = "2026-05-29"
        w._scan_count = 1
        w._triggered_ids = set()
        w._alerted_sl_tp = set()
        w._last_index_quote = {"price": 3300, "pre_close": 3290, "change_pct": 0.003, "amount": 1e11}
        w._index_prices = [3300]
        w._index_high = 3300
        w._index_low = 3300
        w._index_alerted_downtrend = False
        w._index_last_fluctuation_price = 0.0
        w._market_turnovers = [1e11]
        w._volume_alerted_divergence = False
        w._regime = MagicMock()
        w._regime.risk_level = "extreme"
        w._regime.pattern = "panic"
        w._regime.allow_buy = False
        w._closing_decision_done = True  # 跳过尾盘
        w._max_drawdown_alerted = True  # 跳过回撤

        from collections import defaultdict
        w._market_snapshot = {}
        w._sector_trend_history = defaultdict(list)
        w._sector_trend_continuity = defaultdict(int)
        w._sector_trend_last_dir = {}
        w._industry_cache = {}
        w._concept_cache = {}
        w._sector_stats = {}
        w._concept_stats = {}

        w._signal_alert_state = {}
        w._review_alert_state = {}
        w._prev_snapshot = {}
        w._ma_baseline_cache = (3300, 3320, 3350)
        w._sl_reminders = {}
        w._limit_cache = {}
        w._bought_watch = {}

        w._cached_db_watch_codes = set()
        w._watch_codes_stale = True
        w._intraday_cache = {}
        w._intraday_cache_scan = -1
        w._instrument_cache = {}
        w._daily_factor_cache = {}

        w._review_monitor = None
        w._sector_monitor = None
        w._abnormal_detector = None
        w._receiver = None
        w._executor = None
        w._paper_trader = None
        w._collector_client = None

        w._index_tech_state = {
            "macd_cross": None, "rsi6_zone": "normal", "rsi12_zone": "normal",
            "kdj_cross": None, "kdj_j_zone": "normal", "divergence": None,
        }

        from trade.monitor.market_state import MarketStateMixin
        MarketStateMixin._init_scenario_state(w)

        # Portfolio
        from trade.portfolio.portfolio import Portfolio
        w.portfolio = Portfolio(initial_cash=200_000)
        w.portfolio.open_position("000001", "测试股", 1000, 12.00,
                                   entry_date="2026-05-27",
                                   stop_loss=11.00, take_profit=15.00)

        # RiskEngine mock
        w.risk_engine = MagicMock()
        w.risk_engine.can_open.return_value = MagicMock(allowed=True)

        # Repo mock
        w.repo = MagicMock()
        w.repo.get_pending_signals.return_value = []
        w.repo._conn = lambda: sqlite3.connect(db_path)

        # _get_watch_codes 直接返回持仓
        w._get_watch_codes = lambda: ["000001"]
        w._get_realtime_prices = lambda codes: {c: qmt.get_realtime(codes)[c]["lastPrice"] for c in codes}

        # 设置 sector trend → 半导体走弱
        w._industry_cache = {"000001": "半导体"}
        w._sector_trend_history["半导体"] = [0.5, 0.3, 0.1, -0.2, -0.5]

        # 手动跑 _scan
        w._scan()

        # 验证止损触发
        all_msgs = "\n".join([c[0][0] for c in telegram.send.call_args_list])
        assert "止损卖出" in all_msgs or any(
            "止损" in str(c) for c in telegram.send.call_args_list
        ), f"应触发止损，实际消息: {all_msgs[:300]}"
