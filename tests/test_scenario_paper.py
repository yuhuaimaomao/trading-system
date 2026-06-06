"""trade/scenario/ + trade/paper/ 模块综合测试。

涵盖 ScenarioEngine, definitions, templates, PaperAccount, executor, Portfolio。
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from trade.core.scan_state import MicroSignals
from trade.exec.paper.account import (
    COMMISSION_RATE,
    MIN_COMMISSION,
    STAMP_TAX_RATE,
    BuyResult,
    PaperAccount,
    SellResult,
)
from trade.exec.paper.executor import (
    calculate_buy_volume,
    execute_paper_buy,
    execute_paper_sell,
)
from trade.exec.paper.portfolio import Portfolio
from trade.scenario.scenario_defs import PROBABILITY_URGENCY, SCENARIO_SIGNALS
from trade.scenario.scenario_engine import ScenarioEngine
from trade.scenario.templates import (
    build_prompt,
    detect_scenario,
    get_template,
    list_scenarios,
)

# ═══════════════════════════════════════════════════════════════════
# ScenarioEngine 测试
# ═══════════════════════════════════════════════════════════════════


class TestScenarioEngine:
    """情景概率状态机 — 8 个情景初始化 / 信号更新 / 归一化 / 关卡 / 收敛"""

    SCENARIO_NAMES = frozenset(
        {
            "normal_stable",
            "developing_uptrend",
            "developing_downtrend",
            "accelerating_down",
            "accelerating_up",
            "potential_reversal_up",
            "potential_reversal_down",
            "dead_bounce",
        }
    )

    BULLISH_NAMES = {"developing_uptrend", "accelerating_up", "potential_reversal_up"}
    BEARISH_NAMES = {
        "developing_downtrend",
        "accelerating_down",
        "dead_bounce",
        "potential_reversal_down",
    }

    def test_all_8_scenarios_defined_with_unique_names(self):
        engine = ScenarioEngine()
        assert len(engine.probs) == 8
        assert set(engine.probs.keys()) == self.SCENARIO_NAMES

    def test_each_scenario_has_probability_urgency_signals(self):
        engine = ScenarioEngine()
        micro = MicroSignals(price_velocity=0.01, ema12_pos="on")
        for _ in range(3):
            outlook = engine.update(micro)

        # 每个情景有概率（大于0）
        for name in self.SCENARIO_NAMES:
            assert name in engine.probs
            assert engine.probs[name] > 0

        # 每个情景在 SCENARIO_SIGNALS 中有 config
        for name in self.SCENARIO_NAMES:
            cfg = SCENARIO_SIGNALS[name]
            assert "label" in cfg
            assert "direction" in cfg

        # outlook 包含 signals
        assert isinstance(outlook.primary.signals, list)

    def test_bullish_signals_shift_probability_up(self):
        engine = ScenarioEngine()
        initial = dict(engine.probs)

        micro = MicroSignals(
            price_velocity=0.05,
            price_accel=0.03,
            ema12_pos="above",
            breadth_trend="improving",
            higher_lows=True,
            higher_highs=True,
        )
        for _ in range(5):
            engine.update(micro)

        total_bullish = sum(engine.probs[n] for n in self.BULLISH_NAMES)
        initial_bullish = sum(initial[n] for n in self.BULLISH_NAMES)
        assert total_bullish > initial_bullish

    def test_bearish_signals_shift_probability_down(self):
        engine = ScenarioEngine()
        initial = dict(engine.probs)

        micro = MicroSignals(
            price_velocity=-0.05,
            price_accel=-0.03,
            ema12_pos="below",
            breadth_trend="deteriorating",
            lower_highs=True,
            vol_pulse="expanding",
            vol_price_confirm="yes",
        )
        for _ in range(5):
            engine.update(micro)

        total_bearish = sum(engine.probs[n] for n in self.BEARISH_NAMES)
        initial_bearish = sum(initial[n] for n in self.BEARISH_NAMES)
        assert total_bearish > initial_bearish, (
            f"bearish {total_bearish:.3f} should > initial {initial_bearish:.3f}"
        )

    def test_neutral_signals_minimal_shift(self):
        engine = ScenarioEngine()

        micro = MicroSignals(
            price_velocity=0.0,
            price_accel=0.0,
            ema12_pos="on",
            breadth_trend="stable",
            range_contracting=True,
            vol_pulse="normal",
            breadth_pct=0.5,
        )
        for _ in range(5):
            engine.update(micro)

        # normal_stable should still dominate
        assert engine.probs["normal_stable"] > 0.25
        # 最大情景概率不应超过 0.6（强一致信号才可能）
        assert engine.probs["normal_stable"] < 0.8

    def test_anti_collapse_extreme_signals(self):
        """极端多头信号 20 次后，主流情景概率较高，无情景概率 < 0（归一化后正值）。"""
        engine = ScenarioEngine()
        micro = MicroSignals(
            price_velocity=0.1,
            price_accel=0.05,
            ema12_pos="above",
            breadth_trend="improving",
            higher_lows=True,
            higher_highs=True,
        )
        for _ in range(20):
            engine.update(micro)

        # 所有概率为正
        for name, prob in engine.probs.items():
            assert prob > 0, f"{name} 概率 {prob:.6f} <= 0"
        # 归一化: sum ≈ 1.0
        assert abs(sum(engine.probs.values()) - 1.0) < 0.01

        # dead_bounce / bearish 情景被压到很低但不会消失
        dead = engine.probs.get("dead_bounce", 0)
        assert dead < 0.05  # 极端多头下死猫跳应接近消除

    def test_probabilities_sum_to_one_after_10_updates(self):
        engine = ScenarioEngine()
        for _ in range(10):
            micro = MicroSignals(price_velocity=0.01 * (-1) ** (_ % 2))
            engine.update(micro)
            assert abs(sum(engine.probs.values()) - 1.0) < 0.01

    def test_key_levels_extracted_from_data(self):
        engine = ScenarioEngine()
        # 用 bearish 信号 + 排除 normal_stable 确认来推动主情景变为 bearish
        micro = MicroSignals(
            price_velocity=-0.07,
            price_accel=-0.03,
            ema12_pos="below",
            breadth_trend="deteriorating",
            lower_highs=True,
            vol_pulse="expanding",
            vol_price_confirm="yes",
            breadth_pct=0.2,  # 不在 0.4-0.6，使 normal_stable 失去 "宽度均衡" 确认
        )
        for _ in range(10):
            engine.update(
                micro,
                key_support=[3350.0, 3300.0],
                key_resistance=[3420.0, 3450.0],
            )
        # 用迭代 10 次后的概率
        assert engine.probs["developing_downtrend"] > engine.probs["normal_stable"], (
            f"developing_downtrend={engine.probs['developing_downtrend']:.3f} "
            f"normal_stable={engine.probs['normal_stable']:.3f}"
        )
        outlook = engine.update(
            micro,
            key_support=[3350.0, 3300.0],
            key_resistance=[3420.0, 3450.0],
        )
        assert outlook.key_support == [3350.0, 3300.0]
        assert outlook.key_resistance == [3420.0, 3450.0]
        # bearish primary → confirm_at = support[0], invalidate_at = resistance[0]
        assert outlook.primary.direction == "bearish", (
            f"primary={outlook.primary.name} dir={outlook.primary.direction}"
        )
        assert outlook.primary.confirm_at == 3350.0
        assert outlook.primary.invalidate_at == 3420.0

    def test_convergence_speed_reasonable(self):
        """10 次更新内收敛，最后两次之间无剧烈振荡。"""
        engine = ScenarioEngine()
        micro = MicroSignals(
            price_velocity=0.04,
            price_accel=0.02,
            ema12_pos="above",
            breadth_trend="improving",
            higher_lows=True,
        )
        probs_over_time = []
        for _ in range(10):
            engine.update(micro)
            probs_over_time.append(dict(engine.probs))

        last = probs_over_time[-1]
        prev = probs_over_time[-2]
        for name in last:
            delta = abs(last[name] - prev[name])
            assert delta < 0.15, (
                f"{name} 在最后两次更新间变化 {delta:.3f} 过大（>0.15）"
            )

    def test_urgency_in_outlook(self):
        engine = ScenarioEngine()
        micro = MicroSignals(price_velocity=0.05, ema12_pos="above")
        outlook = engine.update(micro)
        assert outlook.urgency in ("none", "watch", "act", "critical")
        assert isinstance(outlook.summary, str) and len(outlook.summary) > 0


# ═══════════════════════════════════════════════════════════════════
# scenario / definitions 测试
# ═══════════════════════════════════════════════════════════════════


class TestScenarioDefinitions:
    """定义文件完整性 — 情景 / 信号 / 紧急程度配置"""

    SCENARIO_NAMES = frozenset(
        {
            "normal_stable",
            "developing_uptrend",
            "developing_downtrend",
            "accelerating_down",
            "accelerating_up",
            "potential_reversal_up",
            "potential_reversal_down",
            "dead_bounce",
        }
    )

    def test_SCENARIO_SIGNALS_has_all_scenarios(self):
        for name in self.SCENARIO_NAMES:
            assert name in SCENARIO_SIGNALS, f"{name} 在 SCENARIO_SIGNALS 中缺失"
        assert len(SCENARIO_SIGNALS) == len(self.SCENARIO_NAMES)

    def test_PROBABILITY_URGENCY_has_all_entries(self):
        assert len(PROBABILITY_URGENCY) >= 4

    def test_all_urgency_values_valid(self):
        for threshold, level, reason in PROBABILITY_URGENCY:
            assert 0 <= threshold <= 1.0
            assert level in ("critical", "act", "watch", "none"), f"未知级别 {level}"
            assert isinstance(reason, str) and len(reason) > 0
        thresholds = [t for t, _, _ in PROBABILITY_URGENCY]
        assert thresholds == sorted(thresholds, reverse=True)

    def test_all_signal_definitions_have_required_fields(self):
        for name, cfg in SCENARIO_SIGNALS.items():
            assert "label" in cfg, f"{name} 缺少 label"
            assert "direction" in cfg, f"{name} 缺少 direction"
            assert cfg["direction"] in ("bullish", "bearish", "neutral"), (
                f"{name} direction={cfg['direction']} 无效"
            )
            assert "confirm" in cfg, f"{name} 缺少 confirm"
            assert "reject" in cfg, f"{name} 缺少 reject"
            assert isinstance(cfg["confirm"], list), f"{name} confirm 不是 list"
            assert isinstance(cfg["reject"], list), f"{name} reject 不是 list"
            assert isinstance(cfg["threshold"], (int, float)), (
                f"{name} threshold 不是数字"
            )
            assert 0 < cfg["threshold"] <= 1.0, (
                f"{name} threshold={cfg['threshold']} 不在范围"
            )
            assert "pre_action" in cfg, f"{name} 缺少 pre_action"
            assert isinstance(cfg["pre_action"], str), f"{name} pre_action 不是 str"


# ═══════════════════════════════════════════════════════════════════
# scenario / templates 测试
# ═══════════════════════════════════════════════════════════════════


class TestScenarioTemplates:
    """AI 场景模板 — 注册 / 检测 / 构建 Prompt"""

    def test_get_template_breakout_returns_PromptTemplate(self):
        tmpl = get_template("breakout")
        from trade.scenario.templates.prompt_model import PromptTemplate

        assert isinstance(tmpl, PromptTemplate)
        assert tmpl.scenario == "breakout"
        assert tmpl.max_tokens == 80

    def test_get_template_trapped_exit_returns_PromptTemplate(self):
        tmpl = get_template("trapped_exit")
        from trade.scenario.templates.prompt_model import PromptTemplate

        assert isinstance(tmpl, PromptTemplate)
        assert tmpl.scenario == "trapped_exit"
        assert tmpl.max_tokens == 80

    def test_get_template_unknown_returns_None(self):
        assert get_template("unknown") is None
        assert get_template("") is None

    def test_list_scenarios_returns_registered(self):
        scenarios = list_scenarios()
        assert isinstance(scenarios, list)
        assert "breakout" in scenarios
        assert "trapped_exit" in scenarios
        assert len(scenarios) == 2

    def test_detect_scenario_is_trapped(self):
        assert detect_scenario({"is_trapped": True}) == "trapped_exit"
        assert detect_scenario({"is_trapped": True, "loss_pct": 0}) == "trapped_exit"

    def test_detect_scenario_loss_gt_5_percent(self):
        assert detect_scenario({"loss_pct": 6.5}) == "trapped_exit"
        assert detect_scenario({"loss_pct": 5.1}) == "trapped_exit"
        assert detect_scenario({"loss_pct": 5.0}) != "trapped_exit"  # 等于 5% 不算 > 5

    def test_detect_scenario_zone_type_breakout(self):
        assert detect_scenario({"zone_type": "breakout"}) == "breakout"

    def test_build_prompt_breakout_returns_valid(self):
        system, user, max_tokens = build_prompt(
            "breakout",
            code="000001",
            name="平安银行",
            price=15.5,
            change_pct=3.2,
            sector_name="银行",
            sector_pct=1.5,
            sector_rank=5,
            sector_total=50,
            amount_desc="放量",
            price_trend="震荡上行",
            market_env="震荡",
            risk_level="safe",
            index_high=3400,
            index_low=3350,
        )
        assert isinstance(system, str) and len(system) > 0
        assert isinstance(user, str) and len(user) > 0
        assert max_tokens == 80
        assert "000001" in user
        assert "平安银行" in user

    def test_build_prompt_trapped_exit_returns_valid(self):
        system, user, max_tokens = build_prompt(
            "trapped_exit",
            code="000001",
            name="平安银行",
            price=14.0,
            cost=15.5,
            loss_pct=9.68,
            sl=13.5,
            tp=16.0,
            lowest=13.8,
            rebound_high=14.5,
            rebound_pct=5.1,
            resistance_label="BBI",
            resistance_price=14.8,
            sector_trend="弱势",
            market_env="下跌",
            risk_level="risk",
        )
        assert isinstance(system, str) and len(system) > 0
        assert isinstance(user, str) and len(user) > 0
        assert "000001" in user

    def test_build_prompt_missing_fields_raises_ValueError(self):
        with pytest.raises(ValueError, match="缺少必填字段"):
            build_prompt("breakout", code="000001")  # name 等字段缺失

    def test_build_prompt_unknown_scenario_raises_KeyError(self):
        with pytest.raises(KeyError, match="未注册的场景"):
            build_prompt("unknown", code="000001")


# ═══════════════════════════════════════════════════════════════════
# PaperAccount 测试
# ═══════════════════════════════════════════════════════════════════


class TestPaperAccount:
    """模拟盘账户 — 买卖执行 / 状态恢复 / 盈亏 / 回撤 / 快照持久化"""

    @pytest.fixture
    def account(self, db_path):
        """创建 PaperAccount，固定交易日，mock QMT 相关调用。"""
        with (
            patch.object(PaperAccount, "_get_pre_close", return_value=0),
            patch.object(PaperAccount, "_get_day_high", return_value=0),
        ):
            acc = PaperAccount(
                db_path=db_path,
                telegram_bot=None,
                initial_capital=200000.0,
            )
            acc._trade_date = "2026-06-01"
            yield acc

    # ========== 买卖执行 ==========

    def test_buy_reduces_cash_and_adds_position(self, account):
        """买入成功 → 扣现金、建持仓。"""
        result = account.buy("000001", "平安银行", 15.0, 100, source="test")
        assert result.success
        assert "000001" in account.positions
        assert account.cash < 200000.0 - 15.0 * 100  # 现金减去了至少股款
        expected_cost = 15.0 * 100 + max(15.0 * 100 * COMMISSION_RATE, MIN_COMMISSION)
        assert account.cash == pytest.approx(200000.0 - expected_cost, abs=0.02)
        assert result.volume == 100
        assert result.cost == pytest.approx(expected_cost, abs=0.02)

    def test_sell_increases_cash_and_removes_position(self, account):
        """卖出成功 → 加现金、删持仓。"""
        account.buy("000001", "平安银行", 15.0, 100, source="test")
        # 解锁 T+1 以便卖出
        account.positions["000001"].locked_volume = 0
        cash_before = account.cash

        result = account.sell("000001", 16.0, reason="止盈")

        assert result.success
        assert "000001" not in account.positions
        assert account.cash > cash_before
        assert result.pnl > 0  # 卖出价 > 成本价

    def test_buy_insufficient_cash_rejected(self, account):
        """现金不足 → 返回拒绝。"""
        account._portfolio.cash = 100.0  # 只留 100 块
        result = account.buy("000001", "平安银行", 50.0, 100, source="test")
        assert not result.success
        assert "现金不足" in result.reason

    def test_buy_invalid_volume_rejected(self, account):
        """无效股数（非整百）→ 返回拒绝。"""
        result = account.buy("000001", "平安银行", 15.0, 50, source="test")
        assert not result.success
        assert "无效股数" in result.reason

    def test_sell_no_position_rejected(self, account):
        """卖出不存在的持仓 → 返回拒绝。"""
        result = account.sell("000001", 15.0, reason="test")
        assert not result.success
        assert "无持仓" in result.reason

    def test_sell_locked_t1_rejected(self, account):
        """T+1 锁定 → 返回拒绝。"""
        account.buy("000001", "平安银行", 15.0, 100, source="test")
        # locked_volume 默认为 volume，available_volume = 0
        result = account.sell("000001", 15.5, reason="卖")
        assert not result.success
        assert "T+1" in result.reason or "可用" in result.reason

    # ========== 属性 / 盈亏 / 回撤 ==========

    def test_total_value_equals_cash_plus_market_value(self, account):
        assert account.total_value == pytest.approx(account.cash, abs=0.01)
        account.buy("000001", "平安银行", 15.0, 100, source="test")
        total = account.cash + account.positions["000001"].market_value
        assert account.total_value == pytest.approx(total, abs=0.01)

    def test_daily_pnl_calculated_correctly(self, account):
        """daily_pnl = total_value - _prev_total（初始 = initial_cash）。"""
        account.buy("000001", "平安银行", 15.0, 100, source="test")
        # daily_pnl = total_value - 200000
        expected = account.total_value - 200000.0
        assert account.daily_pnl == pytest.approx(expected, abs=0.01)

    def test_drawdown_tracked_correctly(self, account):
        """买入后价格下跌 → drawdown > 0。"""
        account.buy("000001", "平安银行", 15.0, 100, source="test")
        # 设 day_high 为 16.0，当前价 15.0
        pos = account.positions["000001"]
        pos.day_high = 16.0
        pos.update_price(14.0)
        dd = (16.0 - 14.0) * 100
        assert account.drawdown == pytest.approx(dd, abs=0.01)

    def test_drawdown_zero_when_no_dd(self, account):
        account.buy("000001", "平安银行", 15.0, 100, source="test")
        account.positions["000001"].day_high = 15.0
        assert account.drawdown == 0.0

    # ========== 价格更新 ==========

    def test_update_prices_updates_all_positions(self, account):
        account.buy("000001", "平安银行", 15.0, 100, source="test")
        account.buy("000002", "万科A", 20.0, 200, source="test")
        account.positions["000001"].locked_volume = 0
        account.positions["000002"].locked_volume = 0

        account.update_prices({"000001": 16.0, "000002": 19.0})

        assert account.positions["000001"].current_price == 16.0
        assert account.positions["000002"].pnl_pct == pytest.approx(-0.05, abs=0.01)
        assert account.positions["000001"].market_value == 1600.0

    # ========== 恢复 ==========

    def test_restore_loads_previous_state(self, db_path):
        """从 DB 恢复快照和持仓。"""
        # 先创建账户，买股票，写入状态
        with (
            patch.object(PaperAccount, "_get_pre_close", return_value=0),
            patch.object(PaperAccount, "_get_day_high", return_value=0),
        ):
            acc1 = PaperAccount(
                db_path=db_path, telegram_bot=None, initial_capital=200000.0
            )
            acc1._trade_date = "2026-06-01"
            acc1.buy("000001", "平安银行", 15.0, 100, source="test")
            expected_cash = acc1.cash
            expected_mv = acc1.positions["000001"].market_value

        # 创建新账户并恢复
        with (
            patch.object(PaperAccount, "_get_pre_close", return_value=0),
            patch.object(PaperAccount, "_get_day_high", return_value=0),
        ):
            acc2 = PaperAccount(
                db_path=db_path, telegram_bot=None, initial_capital=200000.0
            )
            acc2.restore("2026-06-01")

        assert "000001" in acc2.positions
        assert acc2.cash == pytest.approx(expected_cash, abs=0.01)
        assert acc2.positions["000001"].stock_name in ("平安银行", "000001")
        assert acc2.total_value == pytest.approx(expected_cash + expected_mv, abs=0.01)

    # ========== 持久化 ==========

    def test_persist_state_saves_to_db(self, account, db_path):
        """buy 后 _persist_state 写入快照表 + 持仓表。"""
        account.buy("000001", "平安银行", 15.0, 100, source="test")

        conn = sqlite3.connect(db_path)
        try:
            snap = conn.execute(
                "SELECT total_value, cash, market_value, position_count "
                "FROM trade_portfolio_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert snap is not None
            assert snap[3] >= 1  # position_count
            assert snap[1] < 200000.0  # 现金减少
            assert snap[2] > 0  # 有市值

            pos_row = conn.execute(
                "SELECT stock_code, volume, avg_cost, current_price "
                "FROM trade_portfolio_positions WHERE stock_code='000001'"
            ).fetchone()
            assert pos_row is not None
            assert pos_row[0] == "000001"
            assert pos_row[1] == 100

            order_row = conn.execute(
                "SELECT order_type, stock_code, order_status "
                "FROM trade_orders WHERE stock_code='000001'"
            ).fetchone()
            assert order_row is not None
            assert order_row[0] == "buy"
            assert order_row[2] == "filled"
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
# executor 测试
# ═══════════════════════════════════════════════════════════════════


class TestExecutor:
    """模拟盘买卖执行 — executor 层逻辑."""

    # ========== 买入 ==========

    def test_execute_paper_buy_returns_dict_with_success(self):
        account = MagicMock(spec=PaperAccount)
        account.buy.return_value = BuyResult(
            success=True, volume=100, cost=1512.75, commission=12.75
        )
        result = execute_paper_buy(
            code="000001",
            name="平安银行",
            price=15.0,
            volume=100,
            sl=14.0,
            tp=17.0,
            signal_id=None,
            source="test",
            paper_account=account,
        )
        assert result["success"] is True
        assert result["cost"] == 1512.75
        assert result["commission"] == 12.75
        assert "reason" in result
        assert "pnl_meta" in result

    def test_execute_paper_buy_with_signal_updates_repo(self):
        account = MagicMock(spec=PaperAccount)
        account.buy.return_value = BuyResult(
            success=True, volume=100, cost=1512.75, commission=12.75
        )
        repo = MagicMock()
        result = execute_paper_buy(
            code="000001",
            name="平安银行",
            price=15.0,
            volume=100,
            sl=14.0,
            tp=17.0,
            signal_id=42,
            source="signal",
            paper_account=account,
            repo=repo,
        )
        assert result["success"]
        repo.update_signal_status.assert_called_once_with(42, "bought")
        account.buy.assert_called_once_with(
            "000001", "平安银行", 15.0, 100, signal_id=42, source="signal"
        )

    def test_execute_paper_buy_insufficient_cash(self):
        account = MagicMock(spec=PaperAccount)
        account.buy.return_value = BuyResult(
            success=False, reason="现金不足: 需50,000 仅20,000"
        )
        result = execute_paper_buy(
            code="000001",
            name="平安银行",
            price=100.0,
            volume=500,
            sl=90.0,
            tp=110.0,
            signal_id=None,
            source="test",
            paper_account=account,
        )
        assert result["success"] is False
        assert "现金不足" in result["reason"]

    def test_execute_paper_buy_volume_too_small(self):
        account = MagicMock(spec=PaperAccount)
        result = execute_paper_buy(
            code="000001",
            name="平安银行",
            price=15.0,
            volume=50,  # < 100
            sl=14.0,
            tp=17.0,
            signal_id=None,
            source="test",
            paper_account=account,
        )
        assert result["success"] is False
        assert "资金不足" in result["reason"]
        account.buy.assert_not_called()

    def test_buy_pnl_meta_on_success(self):
        account = MagicMock(spec=PaperAccount)
        account.buy.return_value = BuyResult(
            success=True, volume=100, cost=1512.75, commission=12.75
        )
        result = execute_paper_buy(
            code="000001",
            name="平安银行",
            price=15.0,
            volume=100,
            sl=14.0,
            tp=17.0,
            signal_id=42,
            source="signal",
            paper_account=account,
        )
        meta = result["pnl_meta"]
        assert meta["sl"] == 14.0
        assert meta["tp"] == 17.0
        assert meta["signal_id"] == 42

    # ========== 卖出 ==========

    def test_execute_paper_sell_returns_dict_with_success(self):
        account = MagicMock(spec=PaperAccount)
        account.sell.return_value = SellResult(
            success=True, pnl=500.0, pnl_pct=5.0, proceeds=15900.0, commission=100.0
        )
        pos_meta = {"000001": {}}
        bought_watch = {"000001": {}}

        result = execute_paper_sell(
            code="000001",
            name="平安银行",
            price=16.0,
            stype="stop_loss",
            paper_account=account,
            pos_meta=pos_meta,
            bought_watch=bought_watch,
        )
        assert result["success"] is True
        assert result["pnl"] == 500.0
        assert result["pnl_pct"] == 5.0
        # 清理
        assert "000001" not in pos_meta
        assert "000001" not in bought_watch

    def test_execute_paper_sell_no_position(self):
        account = MagicMock(spec=PaperAccount)
        account.sell.return_value = SellResult(success=False, reason="无持仓 000001")
        pos_meta = {}
        bought_watch = {}

        result = execute_paper_sell(
            code="000001",
            name="平安银行",
            price=16.0,
            stype="stop_loss",
            paper_account=account,
            pos_meta=pos_meta,
            bought_watch=bought_watch,
        )
        assert result["success"] is False

    # ========== 佣金 ==========

    def test_commission_calculated_correctly_in_buy(self, db_path):
        """验证 PaperAccount.buy 中的佣金计算。"""
        with (
            patch.object(PaperAccount, "_get_pre_close", return_value=0),
            patch.object(PaperAccount, "_get_day_high", return_value=0),
        ):
            acc = PaperAccount(
                db_path=db_path, telegram_bot=None, initial_capital=200000.0
            )
            acc._trade_date = "2026-06-01"

            # 小金额：佣金取 MIN_COMMISSION = 5.0
            result = acc.buy("000001", "平安银行", 5.0, 100, source="test")
            assert result.success
            expected_commission = max(5.0 * 100 * COMMISSION_RATE, MIN_COMMISSION)
            assert result.commission == pytest.approx(expected_commission, abs=0.01)

            # 大金额（1000 股 @ 100 元，够 20 万现金）：佣金按比例
            acc2 = PaperAccount(
                db_path=db_path, telegram_bot=None, initial_capital=200000.0
            )
            acc2._trade_date = "2026-06-01"
            result2 = acc2.buy("000001", "平安银行", 100.0, 1000, source="test")
            assert result2.success
            expected_commission2 = max(100.0 * 1000 * COMMISSION_RATE, MIN_COMMISSION)
            assert result2.commission == pytest.approx(expected_commission2, abs=0.01)

    def test_stamp_tax_on_sells_only(self, db_path):
        """验证卖出时计算印花税，买入时不计算。"""
        with (
            patch.object(PaperAccount, "_get_pre_close", return_value=0),
            patch.object(PaperAccount, "_get_day_high", return_value=0),
        ):
            acc = PaperAccount(
                db_path=db_path, telegram_bot=None, initial_capital=200000.0
            )
            acc._trade_date = "2026-06-01"

            # 买入 1000 股 @ 50 元
            buy_result = acc.buy("000001", "平安银行", 50.0, 1000, source="test")
            assert buy_result.success
            buy_commission = buy_result.commission

            # 解锁 T+1 并卖出
            acc.positions["000001"].locked_volume = 0
            sell_result = acc.sell("000001", 55.0, reason="止盈")
            assert sell_result.success

            # 买入佣金 = max(金额 * 万0.85, 5)
            expected_buy_comm = max(50.0 * 1000 * COMMISSION_RATE, MIN_COMMISSION)
            assert buy_commission == pytest.approx(expected_buy_comm, abs=0.01)

            # 卖出佣金 = max(金额 * 万0.85, 5) + 金额 * 万5
            sell_amount = 55.0 * 1000
            expected_sell_comm = (
                max(sell_amount * COMMISSION_RATE, MIN_COMMISSION)
                + sell_amount * STAMP_TAX_RATE
            )
            assert sell_result.commission == pytest.approx(expected_sell_comm, abs=0.01)

            # 卖出佣金 > 买入佣金（因为多了印花税）
            assert sell_result.commission > buy_commission

    # ========== 计算股数 ==========

    def test_calculate_buy_volume_normal(self):
        v = calculate_buy_volume(
            price=10.0,
            max_amount=50000,
            total_value=200000,
            cash=150000,
            max_position_pct=0.15,
        )
        assert v >= 0
        assert v % 100 == 0  # 整百股

    def test_calculate_buy_volume_cash_bound(self):
        """现金不足时应按现金约束计算。"""
        v = calculate_buy_volume(
            price=100.0,
            max_amount=500000,
            total_value=200000,
            cash=50000,
            max_position_pct=0.30,
        )
        # max_affordable = int(50000 * 0.9 / 100 / 100) * 100 = 400
        # capital = min(500000, 200000 * 0.30) = 60000
        # volume = min(int(60000 / 100 / 100) * 100, 400) = 400
        assert v == 400

    def test_calculate_buy_volume_no_cash(self):
        v = calculate_buy_volume(
            price=10.0,
            max_amount=50000,
            total_value=200000,
            cash=0,
            max_position_pct=0.15,
        )
        assert v == 0

    def test_calculate_buy_volume_position_pct_cap(self):
        """单只仓位上限应生效。"""
        v = calculate_buy_volume(
            price=10.0,
            max_amount=100000,
            total_value=100000,
            cash=100000,
            max_position_pct=0.10,  # max position = 10000 元
        )
        # capital = min(100000, 100000 * 0.10) = 10000
        # max_affordable = int(100000 * 0.9 / 10 / 100) * 100 = 9000
        # volume = min(int(10000 / 10 / 100) * 100, 9000) = 9000
        # Wait: int(10000 / 10 / 100) * 100 = int(10) * 100 = 1000
        # volume = min(1000, 9000) = 1000
        assert v == 1000, f"预期 1000 得到 {v}"


# ═══════════════════════════════════════════════════════════════════
# Portfolio 扩展测试 — 在原 test_portfolio.py 基本测试基础上新增
# ═══════════════════════════════════════════════════════════════════


class TestPortfolioExtended:
    """组合管理器 — 加仓均价 / 全仓卖出 / T+1 锁定"""

    def test_multiple_buys_same_stock_average_cost(self):
        """同一股票多次买入 → 合并均价正确。"""
        p = Portfolio(initial_cash=100000)
        # 第一次买入 100 股 @ 10.0
        p.open_position("000001", "票A", 100, 10.0, commission=5.0)
        pos = p.positions["000001"]
        # avg_cost = (10.0 * 100 + 5.0) / 100 = 10.05
        assert pos.avg_cost == pytest.approx(10.05)
        assert pos.volume == 100

        # 第二次买入 200 股 @ 12.0
        p.open_position("000001", "票A", 200, 12.0, commission=5.0)
        # old_total_cost = 10.05 * 100 = 1005
        # new_total_cost = 12.0 * 200 + 5.0 = 2405
        # avg_cost = (1005 + 2405) / 300 = 3410 / 300 = 11.3667
        assert pos.avg_cost == pytest.approx(11.3667, abs=0.01)
        assert pos.volume == 300

    def test_multiple_buys_locked_volume_accumulates(self):
        """同一股票多次买入 → 锁定股数累积。"""
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "票A", 100, 10.0)
        assert p.positions["000001"].locked_volume == 100

        p.open_position("000001", "票A", 200, 12.0)
        assert p.positions["000001"].locked_volume == 300  # 当日全部锁定

    def test_full_sell_position_removed(self):
        """卖出后持仓从字典中移除。"""
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "票A", 100, 10.0)
        assert "000001" in p.positions

        success = p.close_position("000001", 12.0, "止盈", commission=5.0)
        assert success
        assert "000001" not in p.positions
        assert len(p.positions) == 0

    def test_close_position_updates_cash(self):
        """卖出后现金增加（收到股款 - 佣金）。"""
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "票A", 100, 10.0, commission=5.0)
        cash_before = p.cash

        p.close_position("000001", 12.0, "止盈", commission=5.0)
        expected_proceeds = 12.0 * 100 - 5.0
        assert p.cash == pytest.approx(cash_before + expected_proceeds, abs=0.01)

    def test_close_position_updates_trade_log(self):
        """卖出后 trade_log 应有记录。"""
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "票A", 100, 10.0)
        p.close_position("000001", 12.0, "止盈", commission=5.0)

        sell_log = [t for t in p.trade_log if t["type"] == "sell"]
        assert len(sell_log) == 1
        assert sell_log[0]["stock_code"] == "000001"
        assert sell_log[0]["price"] == 12.0
        assert sell_log[0]["reason"] == "止盈"

    def test_locked_volume_T1_cannot_sell(self):
        """T+1 锁定 → available_volume = 0。"""
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "票A", 1000, 10.0)

        pos = p.positions["000001"]
        assert pos.locked_volume == 1000
        assert pos.available_volume == 0

    def test_locked_volume_partial_unlock(self):
        """部分解锁 → available_volume 反映可卖数量。"""
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "票A", 1000, 10.0)

        pos = p.positions["000001"]
        pos.locked_volume = 300  # 300 锁定，700 可用
        assert pos.locked_volume == 300
        assert pos.available_volume == 700

    def test_open_position_multiple_different_stocks(self):
        """多只不同股票买入 → 各自独立持仓。"""
        p = Portfolio(initial_cash=100000)
        p.open_position("000001", "票A", 100, 10.0)
        p.open_position("000002", "票B", 200, 20.0)
        p.open_position("000003", "票C", 300, 30.0)

        assert len(p.positions) == 3
        assert p.positions["000001"].volume == 100
        assert p.positions["000002"].volume == 200
        assert p.positions["000003"].volume == 300
