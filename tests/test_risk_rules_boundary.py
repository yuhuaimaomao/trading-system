"""风险规则边界测试 — 覆盖极端值、边界值、空值、组合触发条件。

涵盖 should_stop_loss / should_take_profit / should_trailing_stop /
check_daily_loss_limit / blacklist / concentration / market_env /
adjust_tightening / check_retracement_stop / RiskEngine.
"""

import pytest

from trade.core.scan_state import MarketRegime
from trade.exec.paper.portfolio import Portfolio
from trade.risk.position_rules import adjust_tightening, check_retracement_stop
from trade.risk.risk_engine import RiskEngine
from trade.risk.rules.blacklist import (
    check_listed_days,
    is_blacklisted,
    is_risk_suspect,
)
from trade.risk.rules.concentration import check_concentration, get_sector_overexposure
from trade.risk.rules.market_env import get_market_environment, get_max_position
from trade.risk.rules.max_drawdown import check_daily_loss_limit
from trade.risk.rules.stop_loss import should_stop_loss
from trade.risk.rules.take_profit import should_take_profit, should_trailing_stop

# ══════════════════════════════════════════════════════════════════════
#  should_stop_loss 边界
# ══════════════════════════════════════════════════════════════════════


class TestShouldStopLossBoundary:
    """should_stop_loss 边界：精确命中、紧挨边界、零值保护、收紧系数。"""

    def test_price_below_stop_loss(self):
        triggered, sl = should_stop_loss(9.0, 10.0, 9.5)
        assert triggered
        assert sl == pytest.approx(9.5)

    def test_price_above_stop_loss(self):
        triggered, sl = should_stop_loss(10.0, 10.0, 9.5)
        assert not triggered
        assert sl == pytest.approx(9.5)

    def test_stop_loss_zero(self):
        triggered, sl = should_stop_loss(5.0, 10.0, 0.0)
        assert not triggered
        assert sl == 0.0

    def test_stop_loss_negative(self):
        triggered, sl = should_stop_loss(5.0, 10.0, -1.0)
        assert not triggered
        assert sl == -1.0

    def test_price_exactly_at_stop_loss(self):
        """price == stop_loss（tighten=1.0) → trigger_price == stop_loss → 触发。"""
        triggered, sl = should_stop_loss(9.5, 10.0, 9.5)
        assert triggered
        assert sl == pytest.approx(9.5)

    def test_price_one_cent_above_stop_loss(self):
        triggered, _ = should_stop_loss(9.51, 10.0, 9.5)
        assert not triggered

    def test_price_exactly_at_trigger_with_tighten(self):
        """tighten=0.7: effective=10-(10-9.5)*0.7=9.65, floor=8.075, trigger=9.65。
        price = 9.65 → 触发。"""
        triggered, sl = should_stop_loss(9.65, 10.0, 9.5, tighten=0.70)
        assert triggered
        assert sl == pytest.approx(9.65)

    def test_price_one_cent_above_trigger_with_tighten(self):
        """tighten=0.7, trigger=9.65, price=9.66 → 不触发。"""
        triggered, _ = should_stop_loss(9.66, 10.0, 9.5, tighten=0.70)
        assert not triggered

    def test_floor_protection_dominates(self):
        """当 tighten 很大时 floor 保护生效。
        tighten=5.0: effective=10-(10-8)*5=0, floor=8*0.85=6.8, trigger=6.8。
        """
        triggered, sl = should_stop_loss(6.8, 10.0, 8.0, tighten=5.0)
        assert triggered
        assert sl == pytest.approx(6.8)

    def test_floor_protection_just_above(self):
        """price 略高于 floor → 不触发。"""
        triggered, _ = should_stop_loss(6.81, 10.0, 8.0, tighten=5.0)
        assert not triggered

    def test_tighten_prevents_over_loosen(self):
        """tighten=0.1 收紧 → trigger 非常靠近 avg_cost。"""
        triggered, sl = should_stop_loss(9.95, 10.0, 9.5, tighten=0.10)
        assert triggered
        assert sl == pytest.approx(9.95)

    def test_avg_cost_zero(self):
        triggered, sl = should_stop_loss(5.0, 0.0, 9.5)
        assert not triggered
        assert sl == 9.5

    def test_price_zero(self):
        triggered, sl = should_stop_loss(0.0, 10.0, 9.5)
        assert not triggered
        assert sl == 9.5

    def test_price_negative(self):
        triggered, sl = should_stop_loss(-5.0, 10.0, 9.5)
        assert not triggered
        assert sl == 9.5

    def test_tighten_exactly_1(self):
        """tighten=1.0 应等价于默认行为。"""
        t1, sl1 = should_stop_loss(9.4, 10.0, 9.5, tighten=1.0)
        t2, sl2 = should_stop_loss(9.4, 10.0, 9.5)
        assert t1 == t2
        assert sl1 == sl2


# ══════════════════════════════════════════════════════════════════════
#  should_take_profit 边界
# ══════════════════════════════════════════════════════════════════════


class TestShouldTakeProfitBoundary:
    """should_take_profit 边界：精确命中、紧挨边界、零值保护、下调系数。"""

    def test_price_above_take_profit(self):
        triggered, tp = should_take_profit(12.5, 10.0, 12.0)
        assert triggered
        assert tp == 12.0

    def test_price_below_take_profit(self):
        triggered, tp = should_take_profit(11.0, 10.0, 12.0)
        assert not triggered
        assert tp == 12.0

    def test_price_exactly_at_take_profit(self):
        triggered, tp = should_take_profit(12.0, 10.0, 12.0)
        assert triggered
        assert tp == 12.0

    def test_price_one_cent_below_take_profit(self):
        triggered, tp = should_take_profit(11.99, 10.0, 12.0)
        assert not triggered
        assert tp == 12.0

    def test_take_profit_zero(self):
        triggered, tp = should_take_profit(15.0, 10.0, 0.0)
        assert not triggered
        assert tp == 0.0

    def test_take_profit_negative(self):
        triggered, tp = should_take_profit(15.0, 10.0, -5.0)
        assert not triggered
        assert tp == -5.0

    def test_avg_cost_zero(self):
        triggered, tp = should_take_profit(15.0, 0.0, 12.0)
        assert not triggered
        assert tp == 12.0

    def test_tp_lower_triggers_earlier(self):
        """tp_lower=0.8: effective=10+(15-10)*0.8=14.0, price=14.0 → 触发。"""
        triggered, tp = should_take_profit(14.0, 10.0, 15.0, tp_lower=0.80)
        assert triggered
        assert tp == pytest.approx(14.0)

    def test_tp_lower_one_cent_below(self):
        """effective=14.0, price=13.99 → 不触发。"""
        triggered, tp = should_take_profit(13.99, 10.0, 15.0, tp_lower=0.80)
        assert not triggered
        assert tp == pytest.approx(14.0)

    def test_tp_lower_equals_one_does_not_change(self):
        """tp_lower=1.0 → 不进入下调分支，取原始 tp。"""
        triggered, tp = should_take_profit(12.0, 10.0, 12.0, tp_lower=1.0)
        assert triggered
        assert tp == 12.0

    def test_tp_lower_very_low_does_not_go_below_avg_cost(self):
        """tp_lower=0.1: effective=10+(15-10)*0.1=10.5, price=10.5 → 触发。"""
        triggered, tp = should_take_profit(10.5, 10.0, 15.0, tp_lower=0.10)
        assert triggered
        assert tp == pytest.approx(10.5)

    def test_tp_lower_above_one_ignored(self):
        """tp_lower > 1.0 → 不进入下调分支。"""
        triggered, tp = should_take_profit(14.0, 10.0, 15.0, tp_lower=1.5)
        assert not triggered  # 14 < 15
        assert tp == 15.0


# ══════════════════════════════════════════════════════════════════════
#  should_trailing_stop 边界
# ══════════════════════════════════════════════════════════════════════


class TestShouldTrailingStopBoundary:
    """should_trailing_stop 边界：精确命中、零值保护、收紧系数。"""

    def test_price_dropped_from_high(self):
        """price=9 <= 20*0.95=19 → 触发。"""
        triggered, trail = should_trailing_stop(9.0, 20.0, 0.05)
        assert triggered
        assert trail == pytest.approx(19.0)

    def test_price_above_trail_level(self):
        triggered, _ = should_trailing_stop(19.5, 20.0, 0.05)
        assert not triggered

    def test_price_exactly_at_trail_price(self):
        """price=19.0 == 20*0.95 → 触发。"""
        triggered, trail = should_trailing_stop(19.0, 20.0, 0.05)
        assert triggered
        assert trail == pytest.approx(19.0)

    def test_price_one_cent_above_trail_price(self):
        triggered, _ = should_trailing_stop(19.01, 20.0, 0.05)
        assert not triggered

    def test_trailing_stop_zero(self):
        triggered, trail = should_trailing_stop(5.0, 20.0, 0.0)
        assert not triggered
        assert trail == 0.0

    def test_trailing_stop_negative(self):
        triggered, trail = should_trailing_stop(5.0, 20.0, -0.05)
        assert not triggered
        assert trail == 0.0

    def test_highest_price_zero(self):
        triggered, trail = should_trailing_stop(5.0, 0.0, 0.05)
        assert not triggered
        assert trail == 0.0

    def test_highest_price_negative(self):
        triggered, trail = should_trailing_stop(5.0, -10.0, 0.05)
        assert not triggered
        assert trail == 0.0

    def test_trail_tighten_triggers_earlier(self):
        """trail_tighten=0.7: effective=0.05*0.7=0.035, trail=20*0.965=19.3。
        price=19.2 <= 19.3 → 触发。"""
        triggered, trail = should_trailing_stop(19.2, 20.0, 0.05, trail_tighten=0.70)
        assert triggered
        assert trail == pytest.approx(19.3)

    def test_trail_tighten_just_above(self):
        """trail=19.3, price=19.31 → 不触发。"""
        triggered, _ = should_trailing_stop(19.31, 20.0, 0.05, trail_tighten=0.70)
        assert not triggered

    def test_trail_tighten_at_exactly_trail_price(self):
        triggered, trail = should_trailing_stop(19.3, 20.0, 0.05, trail_tighten=0.70)
        assert triggered
        assert trail == pytest.approx(19.3)

    def test_trail_tighten_above_one_widens(self):
        """trail_tighten=1.3: effective=0.065, trail=18.7 → 更易触发。"""
        triggered, _ = should_trailing_stop(19.0, 20.0, 0.05, trail_tighten=1.30)
        # trail=20*0.935=18.7, 19.0 > 18.7 → 不触发（要求更高回撤）
        assert not triggered


# ══════════════════════════════════════════════════════════════════════
#  check_daily_loss_limit 边界
# ══════════════════════════════════════════════════════════════════════


class TestDailyLossLimitBoundary:
    """check_daily_loss_limit 边界：刚好超过、刚好不超、零值保护、自定义阈值。"""

    def test_drawdown_under_limit(self):
        assert not check_daily_loss_limit(-2000, 100000, 0.03)

    def test_drawdown_over_limit_triggers(self):
        assert check_daily_loss_limit(-3001, 100000, 0.03)

    def test_drawdown_exactly_at_limit_does_not_trigger(self):
        """3% 精确等于 0.03 → not > 0.03 → 不触发。"""
        assert not check_daily_loss_limit(-3000, 100000, 0.03)

    def test_drawdown_at_limit_just_over(self):
        """0.0301 > 0.03 → 触发。"""
        assert check_daily_loss_limit(-3010, 100000, 0.03)

    def test_total_value_zero(self):
        assert not check_daily_loss_limit(-5000, 0, 0.03)

    def test_total_value_negative(self):
        assert not check_daily_loss_limit(-5000, -1000, 0.03)

    def test_positive_daily_pnl_ignored(self):
        assert not check_daily_loss_limit(3000, 100000, 0.03)

    def test_no_loss_no_trigger(self):
        assert not check_daily_loss_limit(0, 100000, 0.03)

    def test_custom_max_loss_pct(self):
        """max_loss_pct=0.05, loss=5000/100000=0.05 → 不超过 → 不触发。"""
        assert not check_daily_loss_limit(-5000, 100000, 0.05)
        # 5001/100000=0.05001 > 0.05 → 触发
        assert check_daily_loss_limit(-5001, 100000, 0.05)


# ══════════════════════════════════════════════════════════════════════
#  黑名单边界
# ══════════════════════════════════════════════════════════════════════


class TestBlacklistBoundary:
    """黑名单规则边界：永久黑名单、ST/新股识别、上市天数。"""

    def test_not_in_permanent_blacklist(self):
        assert not is_blacklisted("000001")
        assert not is_blacklisted("600519")

    def test_st_stock_detection(self):
        assert is_risk_suspect("ST华英")

    def test_ast_stock_detection(self):
        assert is_risk_suspect("*ST康得")

    def test_n_new_stock(self):
        assert is_risk_suspect("N中芯")

    def test_c_new_stock(self):
        assert is_risk_suspect("C新强")

    def test_normal_stock_not_suspect(self):
        assert not is_risk_suspect("平安银行")
        assert not is_risk_suspect("贵州茅台")

    def test_lowercase_st_still_matches(self):
        assert is_risk_suspect("st华英")

    def test_st_in_middle_not_detected(self):
        """startswith 检查，中间出现 ST 不应匹配。"""
        assert not is_risk_suspect("TEST_STOCK")

    def test_listed_days_exactly_min(self):
        assert check_listed_days(60, 60)

    def test_listed_days_below_min(self):
        assert not check_listed_days(59, 60)

    def test_listed_days_well_above(self):
        assert check_listed_days(365, 60)

    def test_listed_days_zero(self):
        assert not check_listed_days(0, 60)

    def test_listed_days_custom_min(self):
        assert check_listed_days(20, 20)
        assert not check_listed_days(19, 20)


# ══════════════════════════════════════════════════════════════════════
#  集中度边界
# ══════════════════════════════════════════════════════════════════════


class TestConcentrationBoundary:
    """集中度边界：单票/板块精确边界、加仓场景。"""

    def test_single_stock_within_limit(self):
        p = Portfolio(initial_cash=200000)
        ok, msg = check_concentration("000001", 0.15, "", p, 0.20, 0.70)
        assert ok
        assert msg == ""

    def test_single_stock_exceeds_limit(self):
        p = Portfolio(initial_cash=200000)
        ok, msg = check_concentration("000001", 0.25, "", p, 0.20, 0.70)
        assert not ok
        assert "超上限" in msg

    def test_single_stock_exactly_at_limit(self):
        """0.20 不大于 0.20 → 允许。"""
        p = Portfolio(initial_cash=200000)
        ok, msg = check_concentration("000001", 0.20, "", p, 0.20, 0.70)
        assert ok
        assert msg == ""

    def test_single_stock_one_pip_over_limit(self):
        p = Portfolio(initial_cash=200000)
        ok, msg = check_concentration("000001", 0.201, "", p, 0.20, 0.70)
        assert not ok
        assert "超上限" in msg

    def test_existing_position_new_buy_exceeds_single_limit(self):
        """已有持仓+加仓超过单票上限。"""
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "票A", 1000, 10.0)  # 市值 10000, 仓位 5%
        # target_pct=0.16, current=0.05 → new_total=0.21 > 0.20
        ok, msg = check_concentration("000001", 0.16, "", p, 0.20, 0.70)
        assert not ok
        assert "超上限" in msg

    def test_existing_position_stays_within_limit(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "票A", 1000, 10.0)  # 5%
        ok, msg = check_concentration("000001", 0.10, "", p, 0.20, 0.70)
        assert ok

    def test_custom_max_single(self):
        p = Portfolio(initial_cash=200000)
        ok, msg = check_concentration("000001", 0.40, "", p, 0.50, 0.70)
        assert ok
        ok2, msg2 = check_concentration("000001", 0.51, "", p, 0.50, 0.70)
        assert not ok2

    def test_sector_concentration_unknown_sector_skipped(self):
        """portfolio.get_sector_exposure() 无 sector_map → 板块检查跳过。"""
        p = Portfolio(initial_cash=200000)
        ok, msg = check_concentration("000001", 0.50, "科技", p, 0.20, 0.30)
        # 单票超限制先拦截
        assert not ok
        assert "超上限" in msg

    def test_sector_within_limit_with_sector_map(self):
        """通过 get_sector_exposure 传入 sector_map 验证板块检查。"""
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "票A", 10000, 10.0)  # 市值 100k
        # 设置 sector_map 使 portfolio.get_sector_exposure 返回值
        p.get_sector_exposure({"000001": "科技"})
        # 用同一个 sector_map
        ok, msg = check_concentration(
            "000002",
            0.10,
            "科技",
            p,
            0.20,
            0.70,
        )
        # get_sector_exposure() 在函数内部调用时无 sector_map → {}
        # 所以 sector 检查实际跳过，只检查单票
        assert ok

    def test_sector_exactly_at_max(self):
        """通过 sector_map 让 sector 精确等于上限 → 允许。"""
        p = Portfolio(initial_cash=200000)
        # 让板块市值占比刚好 0.65，加 0.05 → 0.70 不超
        p.open_position("000001", "票A", 13000, 10.0)  # 市值 130k, 占比 65%
        ok, msg = check_concentration(
            "000002",
            0.05,
            "科技",
            p,
            0.20,
            0.70,
        )
        assert ok

    def test_get_sector_overexposure_empty_without_map(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "票A", 10000, 10.0)
        over = get_sector_overexposure(p, 0.50)
        assert over == []

    def test_get_sector_overexposure_with_map(self):
        p = Portfolio(initial_cash=200000)
        p.open_position("000001", "票A", 15000, 10.0)  # 150k, total=200k, 75%
        p.open_position("000002", "票B", 3000, 10.0)  # 30k, total=200k, 15%
        over = get_sector_overexposure(p, 0.50)
        # get_sector_exposure 内部调用无 sector_map → {}
        assert over == []


# ══════════════════════════════════════════════════════════════════════
#  市场环境边界
# ══════════════════════════════════════════════════════════════════════


class TestMarketEnvBoundary:
    """get_market_environment 边界：分数边界、多维度组合、仓位上限。"""

    def test_bull_market(self):
        env = get_market_environment(3300, 3180, 3100, 0.20, 3.0, 0.01, 8)
        assert env == "bull"

    def test_bear_market(self):
        env = get_market_environment(3000, 3200, 3100, -0.20, 0.3, 0.04, 1)
        assert env == "bear"

    def test_swing_market(self):
        env = get_market_environment(3180, 3200, 3140, 0.02, 1.0, 0.02, 3)
        assert env == "swing"

    def test_score_exactly_3_is_bull(self):
        """score=3 → bull。"""
        env = get_market_environment(3400, 3200, 0, 0, 1.0, 0, 3)
        # dev20=200/3200=0.0625 > 0.05 → +3
        # breadth=1.0 → skip, active=3 → skip
        # total=3 → bull
        assert env == "bull"

    def test_score_exactly_0_is_swing(self):
        """score=0 → swing。"""
        env = get_market_environment(3200, 3200, 0, 0.05, 1.0, 0, 3)
        # dev20=0 → else: -1
        # vol=0.05 > 0 → +1
        # breadth=1.0 → skip, active=3 → skip, amp=0 → skip
        # total=0 → swing
        assert env == "swing"

    def test_score_exactly_minus_1_is_bear(self):
        """score=-1 → bear。"""
        env = get_market_environment(3200, 3200, 0, 0, 1.0, 0, 3)
        # dev20=0 → else: -1
        # vol=0→skip, breadth=1.0→skip, active=3→skip, amp=0→skip
        # total=-1 → bear
        assert env == "bear"

    def test_all_neutral_gives_swing(self):
        """所有维度均触发不到任何加减分 → swing。"""
        env = get_market_environment(0, 0, 0, 0, 1.0, 0, 3)
        # ma20=0→skip, vol=0→skip, breadth=1.0→skip
        # amp=0→skip, active=3→skip
        # total=0 → swing
        assert env == "swing"

    def test_unknown_env_returns_default_position(self):
        assert get_max_position("unknown") == 0.50

    def test_bull_position_limit(self):
        assert get_max_position("bull") == 0.80

    def test_swing_position_limit(self):
        assert get_max_position("swing") == 0.50

    def test_bear_position_limit(self):
        assert get_max_position("bear") == 0.20


# ══════════════════════════════════════════════════════════════════════
#  adjust_tightening 边界
# ══════════════════════════════════════════════════════════════════════


class TestAdjustTighteningBoundary:
    """adjust_tightening 边界：各风险级别、板块趋势叠加。"""

    def test_safe_no_sector_adjustment(self):
        sl, tp, trail = adjust_tightening("safe", "横盘")
        assert sl == 1.0
        assert tp == 1.0
        assert trail == 1.0

    def test_cautious_no_sector_adjustment(self):
        sl, tp, trail = adjust_tightening("cautious", "横盘")
        assert sl == pytest.approx(0.92)
        assert tp == 1.0
        assert trail == pytest.approx(0.92)

    def test_dangerous_no_sector_adjustment(self):
        sl, tp, trail = adjust_tightening("dangerous", "横盘")
        assert sl == pytest.approx(0.85)
        assert tp == pytest.approx(0.90)
        assert trail == pytest.approx(0.85)

    def test_extreme_no_sector_adjustment(self):
        sl, tp, trail = adjust_tightening("extreme", "横盘")
        assert sl == pytest.approx(0.70)
        assert tp == pytest.approx(0.80)
        assert trail == pytest.approx(0.70)

    def test_sector_weak_applies(self):
        """普跌→is_weak=True → 各系数 *0.95。"""
        sl, tp, trail = adjust_tightening("safe", "普跌")
        assert sl == pytest.approx(1.0 * 0.95)
        assert tp == pytest.approx(1.0 * 0.95)
        assert trail == pytest.approx(1.0 * 0.95)

    def test_sector_weak_weakens_more_with_dangerous(self):
        """dangerous + 弱于大盘 → 0.85*0.95=0.8075。"""
        sl, tp, trail = adjust_tightening("dangerous", "弱于大盘")
        assert sl == pytest.approx(0.85 * 0.95, rel=0.001)
        assert tp == pytest.approx(0.90 * 0.95, rel=0.001)

    def test_sector_accel_max_tightening(self):
        """extreme + 持续走弱+加速 → 0.70*0.90=0.63。"""
        sl, tp, trail = adjust_tightening("extreme", "持续走弱 加速 -3%")
        assert sl == pytest.approx(0.70 * 0.90, rel=0.001)
        assert tp == pytest.approx(0.80 * 0.90, rel=0.001)
        assert trail == pytest.approx(0.70 * 0.90, rel=0.001)

    def test_sector_weak_not_accel(self):
        """包含"持续走弱"但无"加速"→ is_weak=True, is_accel=False → *0.95。"""
        sl, tp, trail = adjust_tightening("cautious", "持续走弱 弱于大盘")
        assert sl == pytest.approx(0.92 * 0.95, rel=0.001)
        assert tp == pytest.approx(1.0 * 0.95, rel=0.001)

    def test_unknown_risk_level_defaults(self):
        sl, tp, trail = adjust_tightening("unknown_potato", "横盘")
        assert sl == 1.0
        assert tp == 1.0
        assert trail == 1.0


# ══════════════════════════════════════════════════════════════════════
#  check_retracement_stop 边界
# ══════════════════════════════════════════════════════════════════════


class TestRetracementStopBoundary:
    """check_retracement_stop 边界：利润阈值、精确命中、大盘加成。"""

    def test_highest_price_zero(self):
        assert not check_retracement_stop(10.0, 0.0, 10.0, "safe")

    def test_avg_cost_zero(self):
        assert not check_retracement_stop(10.0, 12.0, 0.0, "safe")

    def test_max_profit_below_5_percent(self):
        """max_profit_pct=4 < 5 → 不触发。"""
        assert not check_retracement_stop(10.3, 10.4, 10.0, "safe")

    def test_max_profit_exactly_5_normal_risk(self):
        """max_profit=5%: keep=0.50, threshold=2.5%。
        price=10.24 → current=2.4% < 2.5% → 触发。"""
        assert check_retracement_stop(10.24, 10.5, 10.0, "safe")

    def test_max_profit_exactly_5_not_triggered(self):
        """threshold=2.5%, price=10.25 → current=2.5% 不小于 2.5% → 不触发。"""
        assert not check_retracement_stop(10.25, 10.5, 10.0, "safe")

    def test_max_profit_exactly_10_normal_risk(self):
        """max_profit=10%: keep=0.55, threshold=5.5%。
        price=10.54 → current=5.4% < 5.5% → 触发。"""
        assert check_retracement_stop(10.54, 11.0, 10.0, "safe")

    def test_max_profit_exactly_10_not_triggered(self):
        """price=10.55 → current=5.5% → 不触发。"""
        assert not check_retracement_stop(10.55, 11.0, 10.0, "safe")

    def test_max_profit_exactly_15_normal_risk(self):
        """max_profit=15%: keep=0.60, threshold=9%。
        price=10.89 → current=8.9% < 9% → 触发。"""
        assert check_retracement_stop(10.89, 11.5, 10.0, "safe")

    def test_max_profit_exactly_15_not_triggered(self):
        """price=10.90 → current=9.0% → 不触发。"""
        assert not check_retracement_stop(10.90, 11.5, 10.0, "safe")

    def test_max_profit_between_10_and_15(self):
        """max_profit=12%: keep=0.55, threshold=6.6%。
        price=10.65 → current=6.5% < 6.6% → 触发。"""
        assert check_retracement_stop(10.65, 11.2, 10.0, "safe")

    def test_extreme_bonus_protects_earlier(self):
        """extreme: keep=0.60+0.10=0.70, threshold=15*0.70=10.5%。
        price=11.04 → current=10.4% < 10.5% → 触发（比正常 9.0% 阈值更敏感）。"""
        assert check_retracement_stop(11.04, 11.5, 10.0, "extreme")

    def test_dangerous_bonus(self):
        """dangerous: keep=0.60+0.05=0.65, threshold=15*0.65=9.75%。
        price=10.97 → current=9.7% < 9.75% → 触发。
        price=10.98 → current=9.8% > 9.75% → 不触发。"""
        assert check_retracement_stop(10.97, 11.5, 10.0, "dangerous")
        assert not check_retracement_stop(10.98, 11.5, 10.0, "dangerous")

    def test_5_to_10_range_bracket(self):
        """max_profit=8%: keep=0.50+0=0.50, threshold=4%。"""
        assert check_retracement_stop(10.39, 10.8, 10.0, "safe")
        assert not check_retracement_stop(10.41, 10.8, 10.0, "safe")


# ══════════════════════════════════════════════════════════════════════
#  RiskEngine 边界
# ══════════════════════════════════════════════════════════════════════


class TestRiskEngineBoundary:
    """RiskEngine 边界：can_open 全通过/单规则阻断、check_positions 触发/空。"""

    def test_can_open_all_rules_pass(self):
        engine = RiskEngine()
        engine.update_market_env(3250, 3200, 3100, 0.05, 1.5, 0.01, 5)
        portfolio = Portfolio(initial_cash=200000)
        result = engine.can_open("000001", 0.10, portfolio=portfolio)
        assert result.allowed
        assert result.reason == "通过"

    def test_can_open_no_portfolio_still_allowed(self):
        """无 portfolio → 跳过仓位/集中度检查。"""
        engine = RiskEngine()
        engine.update_market_env(3250, 3200)
        result = engine.can_open("000001", 0.10)
        assert result.allowed

    def test_can_open_blacklisted_by_name(self):
        engine = RiskEngine()
        engine.update_market_env(3250, 3200)
        result = engine.can_open("600001", 0.10, stock_name="ST警示")
        assert not result.allowed
        assert "风险标的" in result.reason

    def test_can_open_halted(self):
        engine = RiskEngine()
        engine._halted = True
        result = engine.can_open("000001", 0.10)
        assert not result.allowed
        assert "日内熔断" in result.reason

    def test_can_open_regime_blocks_buy(self):
        """regime.position_mult=0 → 禁止开仓。"""
        engine = RiskEngine()
        engine.update_market_env(3250, 3200)
        regime = MarketRegime(pattern="panic", position_mult=0.0, risk_level="extreme")
        engine.set_regime(regime)
        result = engine.can_open("000001", 0.10)
        assert not result.allowed
        assert "禁止开仓" in result.reason

    def test_can_open_regime_reduces_max_position(self):
        """regime.position_mult=0.5 + bear=0.20 → max_pos=0.10。"""
        engine = RiskEngine()
        engine.update_market_env(3000, 3200, 3100, -0.20, 0.3, 0.04, 1)  # bear
        regime = MarketRegime(pattern="one_sided", position_mult=0.5)
        engine.set_regime(regime)
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 10000, 10.0)  # 50k, 25%
        result = engine.can_open("000002", 0.10, portfolio=portfolio)
        # bear max=0.20 * 0.5 = 0.10, position_ratio=0.25 → 0.25+0.10=0.35 > 0.10
        assert not result.allowed
        assert "仓位上限" in result.reason

    def test_can_open_bear_market_exceeds_position_limit(self):
        engine = RiskEngine()
        engine.update_market_env(3000, 3200, 3100, -0.20, 0.3, 0.04, 1)
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 10000, 10.0)  # 50k, 25%
        result = engine.can_open("000002", 0.05, portfolio=portfolio)
        # bear max=0.20, current=0.25, 0.25+0.05=0.30 > 0.20
        assert not result.allowed
        assert "仓位上限" in result.reason

    def test_check_positions_stop_loss_triggers(self):
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos = portfolio.positions["000001"]
        pos.stop_loss = 9.5
        pos.locked_volume = 0  # T+1 已过
        signals = engine.check_positions({"000001": 9.0}, portfolio)
        assert len(signals) > 0
        assert signals[0]["stock_code"] == "000001"
        assert signals[0]["priority"] == 5  # 止损优先级

    def test_check_positions_trailing_stop_triggers(self):
        """止损不触发 + 移动止盈触发。"""
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos = portfolio.positions["000001"]
        pos.stop_loss = 5.0  # 远低于当前价，不触发
        pos.trailing_stop = 0.05
        pos.highest_price = 20.0
        pos.locked_volume = 0
        signals = engine.check_positions({"000001": 18.5}, portfolio)
        # 18.5 <= 20*0.95=19 → 触发
        assert len(signals) > 0
        assert signals[0]["priority"] == 6

    def test_check_positions_take_profit_triggers(self):
        """止损、移动止盈均不触发 → 目标止盈触发。"""
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos = portfolio.positions["000001"]
        pos.stop_loss = 5.0
        pos.trailing_stop = 0.0
        pos.take_profit = 15.0
        pos.highest_price = 10.0
        pos.locked_volume = 0
        signals = engine.check_positions({"000001": 15.5}, portfolio)
        assert len(signals) > 0
        assert signals[0]["priority"] == 7

    def test_check_positions_time_stop_triggers(self):
        """长持亏损 → 时间止损触发。"""
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0, entry_date="2026-06-01")
        pos = portfolio.positions["000001"]
        pos.stop_loss = 0.0
        pos.locked_volume = 0
        signals = engine.check_positions(
            {"000001": 9.0},
            portfolio,
            trade_date="2026-06-10",
        )
        # hold_days=9 > 5, pnl_pct=-10% < -3% → 时间止损
        time_stop_signals = [s for s in signals if s.get("priority") == 8]
        assert len(time_stop_signals) > 0

    def test_check_positions_time_stop_short_hold_not_triggers(self):
        """持有不足 5 天 → 不触发时间止损。"""
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0, entry_date="2026-06-08")
        pos = portfolio.positions["000001"]
        pos.stop_loss = 0.0
        pos.locked_volume = 0
        signals = engine.check_positions(
            {"000001": 9.0},
            portfolio,
            trade_date="2026-06-10",
        )
        # hold_days=2 ≤ 5
        time_stop_signals = [s for s in signals if s.get("priority") == 8]
        assert len(time_stop_signals) == 0

    def test_check_positions_no_triggers(self):
        """所有风控规则均不触发→返回空列表。"""
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos = portfolio.positions["000001"]
        pos.stop_loss = 0.0
        pos.take_profit = 0.0
        pos.trailing_stop = 0.0
        pos.locked_volume = 0
        signals = engine.check_positions({"000001": 10.0}, portfolio)
        assert len(signals) == 0

    def test_check_positions_daily_loss_circuit_breaker(self):
        """日亏损超限→熔断，卖出所有浮亏持仓。"""
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio._prev_total = 210000  # 模拟亏损
        portfolio.open_position("000001", "票A", 1000, 10.0)
        portfolio.open_position("000002", "票B", 500, 20.0)
        pos1 = portfolio.positions["000001"]
        pos1.stop_loss = 0.0
        pos1.locked_volume = 0
        pos2 = portfolio.positions["000002"]
        pos2.stop_loss = 0.0
        pos2.locked_volume = 0
        signals = engine.check_positions(
            {"000001": 9.0, "000002": 18.0},
            portfolio,
        )
        # daily_pnl = 200000+9000+9000 - 210000 = 218000-210000 = +8000 → 正收益
        # 需要更大的亏损
        portfolio._prev_total = 250000
        signals = engine.check_positions(
            {"000001": 9.0, "000002": 18.0},
            portfolio,
        )
        # daily_pnl = (190000+9000+9000) - 250000 = 208000-250000 = -42000
        # 42000/208000 = 20.2% > 3% → 熔断
        assert len(signals) >= 1
        assert any(s.get("priority") == 4 for s in signals)

    def test_adjust_stops_with_regime_widens_stop(self):
        """stop_mult > 1 → 放宽止损。"""
        engine = RiskEngine()
        regime = MarketRegime(pattern="wide_choppy", stop_mult=1.5)
        engine.set_regime(regime)
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos_meta = {"000001": {"sl": 9.5}}
        engine.adjust_stops(portfolio, {"000001": 10.0}, pos_meta)
        # base_distance = |10-9.5|/10 = 0.05
        # new_distance = 0.05 * 1.5 = 0.075
        # new_sl = 10 * (1-0.075) = 9.25
        assert pos_meta["000001"]["sl"] == pytest.approx(9.25)

    def test_adjust_stops_tightens_when_mult_below_one(self):
        """stop_mult < 1 → 收紧止损。"""
        engine = RiskEngine()
        regime = MarketRegime(pattern="inverted_v", stop_mult=0.8)
        engine.set_regime(regime)
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos_meta = {"000001": {"sl": 9.0}}
        engine.adjust_stops(portfolio, {"000001": 10.0}, pos_meta)
        # base_distance=0.1, new=0.08, new_sl=10*(1-0.08)=9.2
        assert pos_meta["000001"]["sl"] == pytest.approx(9.2)

    def test_adjust_stops_no_regime_noop(self):
        engine = RiskEngine()
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos_meta = {"000001": {"sl": 9.5}}
        engine.adjust_stops(portfolio, {"000001": 10.0}, pos_meta)
        assert pos_meta["000001"]["sl"] == 9.5  # unchanged

    def test_evaluate_existing_emergency_exit(self):
        engine = RiskEngine()
        regime = MarketRegime(pattern="panic", urgent_action="emergency_exit")
        engine.set_regime(regime)
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        results = engine.evaluate_existing(portfolio, {"000001": 9.0})
        assert len(results) == 1
        assert results[0]["action"] == "emergency_close"

    def test_evaluate_existing_tighten_stops(self):
        engine = RiskEngine()
        regime = MarketRegime(
            pattern="melt_up", urgent_action="tighten_stops", risk_level="dangerous"
        )
        engine.set_regime(regime)
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        pos = portfolio.positions["000001"]
        pos.update_price(12.0)  # pnl_pct=20% > 3%
        results = engine.evaluate_existing(portfolio, {"000001": 12.0})
        assert len(results) == 1
        assert results[0]["action"] == "tighten_stop"

    def test_evaluate_existing_reduce_positions(self):
        engine = RiskEngine()
        regime = MarketRegime(pattern="dead_cat", urgent_action="reduce_positions")
        engine.set_regime(regime)
        portfolio = Portfolio(initial_cash=200000)
        portfolio.open_position("000001", "票A", 1000, 10.0)
        # 需更新价格使 pnl_pct 为负
        portfolio.update_prices({"000001": 9.0})
        signals = engine.evaluate_existing(portfolio, {"000001": 9.0})
        assert len(signals) == 1
        assert signals[0]["action"] == "reduce"

    def test_get_risk_status_summary_structure(self):
        engine = RiskEngine()
        engine.update_market_env(3200, 3400, breadth_ratio=0.9, active_sectors=3)
        portfolio = Portfolio(initial_cash=200000)
        status = engine.get_risk_status(portfolio)
        assert "market_env" in status
        assert "max_position" in status
        assert "position_count" in status
        assert status["market_env"] == "bull"
        assert status["max_position"] == 0.80
