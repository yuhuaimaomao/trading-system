"""trade/decision/ 模块边界测试 — 覆盖极端值、空值、边界值、组合场景。"""

import pytest

from trade.core.scan_state import MarketOutlook, MarketRegime, MarketScenario
from trade.decision.buy import BuyEvalInput, evaluate_below_zone, evaluate_buy
from trade.decision.regime import (
    _upgrade_risk,
    assess_regime,
)
from trade.decision.sell import analyze_exit_signals, classify_holding_status
from trade.decision.sizing import (
    calc_unified_stop_loss,
    calc_unified_take_profit,
    calculate_position_size,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   evaluate_buy 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEvaluateBuyBoundary:
    """evaluate_buy 边界条件：价格位置、板块、技术指标、空值、负值。"""

    @staticmethod
    def make(**kw):
        defaults = {
            "code": "000001",
            "price": 10.0,
            "buy_min": 9.5,
            "buy_max": 10.5,
            "sector_trend": "走强",
            "sector_chg": 1.5,
            "intra_available": False,
        }
        defaults.update(kw)
        return BuyEvalInput(**defaults)

    # ── 价格位置 ──

    def test_price_inside_zone_normal(self):
        """价格在买入区中部，正常通过。"""
        ok, reason, mul = evaluate_buy(self.make(price=10.0))
        assert ok
        assert mul >= 0.9

    def test_price_at_zone_top_rejects(self):
        """zone_pos >= 0.95 → 拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(price=10.46, buy_min=9.5, buy_max=10.5))
        assert not ok
        assert "顶部" in reason
        assert mul == 0.0

    def test_price_zone_upper_warn(self):
        """0.65 <= zone_pos < 0.85 → 警告，仓位减至 0.7x。"""
        # zone_pos = (10.16 - 9.5) / 1.0 = 0.66
        ok, reason, mul = evaluate_buy(self.make(price=10.16, buy_min=9.5, buy_max=10.5))
        assert ok
        assert "偏上" in reason
        assert mul == pytest.approx(0.7, rel=0.01)

    def test_price_zone_bottom_boost(self):
        """zone_pos <= 0.33 + 技术支持 → 加分。"""
        # price=9.55 → zone_pos = 0.05
        # 布林下轨接近价格 → near_support → 1.2x
        ok, reason, mul = evaluate_buy(
            self.make(
                price=9.55,
                buy_min=9.5,
                buy_max=10.5,
                daily_bb_lower=9.50,
            )
        )
        assert ok
        # base=1.0 → near_support boost 1.2x
        assert mul == pytest.approx(1.0, rel=0.01)  # min(1.0, 1.2) = 1.0

    def test_price_exactly_at_zone_threshold_95pct(self):
        """zone_pos >= 0.95 触发拒绝。"""
        # zone_pos = (10.46 - 9.5) / 1.0 = 0.96
        ok, reason, mul = evaluate_buy(self.make(price=10.46, buy_min=9.5, buy_max=10.5))
        assert not ok
        assert "顶部" in reason

    def test_price_exactly_at_zone_threshold_65pct(self):
        """zone_pos 精确等于 0.65 触发警告。"""
        # zone_pos = (10.15 - 9.5) / 1.0 = 0.65
        ok, reason, mul = evaluate_buy(self.make(price=10.15, buy_min=9.5, buy_max=10.5))
        assert ok
        assert "偏上" in reason
        assert mul == pytest.approx(0.7, rel=0.01)

    # ── 板块趋势 ──

    def test_sector_trend_weak_warn(self):
        """ "走弱" in trend (非持续走弱) → 警告，仓位 0.5x。"""
        ok, reason, mul = evaluate_buy(self.make(sector_trend="板块走弱", sector_chg=-0.3))
        assert ok
        assert "板块偏弱" in reason
        # "走弱" in trend→0.5x, sector_chg<0 跳过（避免双重扣分）
        assert mul == pytest.approx(0.5, rel=0.01)

    def test_sector_trend_continuous_weak_reject(self):
        """ "持续走弱" → 拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(sector_trend="持续走弱", sector_chg=-0.5))
        assert not ok
        assert "持续走弱" in reason

    def test_sector_trend_strong_boost(self):
        """ "持续走强" → size_mul *= 1.2。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_trend="持续走强",
                sector_chg=2.0,
                price_action="reversing",
            )
        )
        assert ok
        # base=1.0 * 1.2(持续走强) * 1.15(reversing) = 1.38 → capped at 1.0
        assert mul == pytest.approx(1.0, rel=0.01)

    def test_sector_trend_unknown_phrase_no_match(self):
        """趋势字符串不匹配任何关键字 → 无板块调整。"""
        ok, reason, mul = evaluate_buy(self.make(sector_trend="震荡偏多", sector_chg=0.5))
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)

    def test_sector_chg_negative_one_reject(self):
        """sector_chg = -2.0 精确等于新阈值 → 拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(sector_chg=-2.0))
        assert not ok
        assert "跌幅" in reason

    def test_sector_chg_negative_just_above_threshold(self):
        """sector_chg = -0.99 不触发拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(sector_chg=-0.99))
        assert ok

    def test_sector_decline_over_1_5_reject(self):
        """sector_decline >= 1.5 → 拒绝。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_trend="走强",
                sector_chg=1.0,
                sector_decline=2.0,
            )
        )
        assert not ok
        assert "冲高回落" in reason

    def test_sector_recovery_risk_reject(self):
        """recovery_risk 非None → 拒绝。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_trend="走强",
                sector_chg=1.0,
                sector_recovery_risk=3.0,
            )
        )
        assert not ok
        assert "死猫跳" in reason

    def test_sector_strong_affects_intra_rsi_threshold(self):
        """sector_strong=True 时日内RSI超买阈值更宽松。"""
        # intra_rsi6=80, sector_strong → warn at 75→85, no reject at 80
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_trend="走强",
                sector_chg=1.5,
                sector_strong=True,
                intra_available=True,
                intra_rsi6=80,
            )
        )
        assert ok

    def test_sector_very_strong_affects_bb_threshold(self):
        """sector_very_strong=True → 布林带拒绝阈值提至 95。"""
        # daily_bb_pct_b=92, 正常拒绝(90)，但 sector_very_strong 提至 95
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_very_strong=True,
                daily_bb_pct_b=92,
            )
        )
        assert ok
        # 但 92 >= 85(warn) → 警告，0.8x
        assert "偏上" in reason
        assert mul == pytest.approx(0.8, rel=0.01)

    # ── RSI ──

    def test_daily_rsi_over_85_reject(self):
        """daily_rsi6 >= 85 → 拒绝（新阈值）。"""
        ok, reason, mul = evaluate_buy(self.make(daily_rsi6=86))
        assert not ok
        assert "RSI6" in reason and "超买" in reason

    def test_daily_rsi_over_70_under_80_warn(self):
        """70 <= daily_rsi6 < 80 → 警告，0.7x。"""
        ok, reason, mul = evaluate_buy(self.make(daily_rsi6=75))
        assert ok
        assert "偏高" in reason
        assert mul == pytest.approx(0.7, rel=0.01)

    def test_daily_rsi_under_30_no_impact(self):
        """daily_rsi6 < 30 → evaluate_buy 无显式逻辑，不应产生异常。"""
        ok, reason, mul = evaluate_buy(self.make(daily_rsi6=25))
        assert ok

    def test_daily_rsi_none_passes(self):
        """daily_rsi6 = None → 跳过 RSI 检查。"""
        ok, reason, mul = evaluate_buy(self.make(daily_rsi6=None))
        assert ok

    # ── KDJ ──

    def test_daily_kdj_j_over_110_reject(self):
        """daily_kdj_j > 110 → 拒绝（新阈值）。"""
        ok, reason, mul = evaluate_buy(self.make(daily_kdj_j=120))
        assert not ok
        assert "KDJ" in reason

    def test_daily_kdj_j_over_85_warn(self):
        """85 < daily_kdj_j <= 100 → 警告，0.6x。"""
        ok, reason, mul = evaluate_buy(self.make(daily_kdj_j=90))
        assert ok
        assert mul == pytest.approx(0.6, rel=0.01)

    # ── 布林带 ──

    def test_bb_pct_b_96_reject(self):
        """%B >= 95 → 拒绝（新阈值，非强势板块）。"""
        ok, reason, mul = evaluate_buy(self.make(daily_bb_pct_b=96))
        assert not ok

    def test_bb_pct_b_85_warn(self):
        """75 <= %B < 90 → 警告，0.8x。"""
        ok, reason, mul = evaluate_buy(self.make(daily_bb_pct_b=85))
        assert ok
        assert "偏上" in reason
        assert mul == pytest.approx(0.8, rel=0.01)

    def test_bb_pct_b_0_no_problem(self):
        """%B = 0 (价格在下轨) → 不触发任何布林警告。"""
        ok, reason, mul = evaluate_buy(self.make(daily_bb_pct_b=0))
        assert ok

    def test_bb_pct_b_none_skipped(self):
        """daily_bb_pct_b = None → 跳过布林检查。"""
        ok, reason, mul = evaluate_buy(self.make(daily_bb_pct_b=None))
        assert ok

    def test_near_bb_lower_boost(self):
        """price 接近 daily_bb_lower → near_support → 1.2x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                price=10.0,
                buy_min=9.5,
                buy_max=10.5,
                daily_bb_lower=9.75,
                daily_ma20=11.0,
            )
        )
        # abs(10-9.75)/9.75 = 0.0256 < 0.03 → near_support
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)  # min(1.0, 1.2)=1.0

    # ── MACD 死叉 ──

    def test_macd_death_cross_warn(self):
        """daily_macd_dif < dea 且 bar < -0.3 → 警告。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                daily_macd_dif=-0.5,
                daily_macd_dea=0.2,
                daily_macd_bar=-0.5,
            )
        )
        assert ok
        assert "MACD空头" in reason

    def test_macd_bullish_boost(self):
        """daily_macd_dif > dea 且 bar > 0.2 → 1.05x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                daily_macd_dif=0.5,
                daily_macd_dea=0.2,
                daily_macd_bar=0.3,
            )
        )
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)  # min(1.0, 1.05) = 1.0

    # ── 均线空头排列 ──

    def test_bearish_alignment_reject(self):
        """ma5 < ma10 < ma20 且 price < all → 拒绝。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                price=9.0,
                buy_min=8.5,
                buy_max=9.5,
                daily_ma5=9.5,
                daily_ma10=10.0,
                daily_ma20=10.5,
            )
        )
        assert not ok
        assert "接飞刀" in reason

    def test_below_all_ma_but_not_bearish_alignment(self):
        """价格低于所有均线但非空头排列 → 警告，0.7x。"""
        # ma5=10.5 > ma10=10.0 > ma20=9.5 (不是空头排列)
        ok, reason, mul = evaluate_buy(
            self.make(
                price=9.0,
                buy_min=8.5,
                buy_max=9.5,
                daily_ma5=10.5,
                daily_ma10=10.0,
                daily_ma20=9.5,
            )
        )
        assert ok
        assert "低于所有均线" in reason
        assert mul == pytest.approx(0.7, rel=0.01)

    def test_ma_all_zero_skipped(self):
        """ma5=ma10=ma20=0 → 跳过均线检查。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                price=9.0,
                buy_min=8.5,
                buy_max=9.5,
                daily_ma5=0,
                daily_ma10=0,
                daily_ma20=0,
            )
        )
        assert ok

    def test_bearish_alignment_but_price_above_recover(self):
        """空头排列但价格在最上方 → 不应触发接飞刀。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                price=10.8,
                buy_min=8.0,
                buy_max=12.0,
                daily_ma5=10.0,
                daily_ma10=10.5,
                daily_ma20=10.7,
            )
        )
        # zone_pos = (10.8-8)/4 = 0.7, not at top
        # below_all: 10.8 < 10.0 is False → no bearish alignment
        assert ok

    # ── 日内指标 ──

    def test_intra_rsi_extreme_reject(self):
        """intra_rsi6 >= 92 → 拒绝（新阈值）。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_rsi6=93,
            )
        )
        assert not ok
        assert "RSI6" in reason

    def test_intra_rsi_75_warn(self):
        """75 <= intra_rsi6 < 85 → 警告，0.7x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_rsi6=78,
            )
        )
        assert ok
        assert "RSI6" in reason
        assert mul == pytest.approx(0.7, rel=0.01)

    def test_intra_rsi_oversold_boost(self):
        """intra_rsi6 <= 20 → 1.1x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_rsi6=15,
            )
        )
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)  # min(1.0, 1.1) = 1.0

    def test_intra_macd_bearish_strong_reject(self):
        """MACD 强烈空头 bar < -0.8 → 拒绝（新阈值）。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_macd_direction="bearish",
                intra_macd_bar=-0.9,
            )
        )
        assert not ok
        assert "MACD" in reason

    def test_intra_macd_bearish_warn(self):
        """MACD 空头但未到拒绝阈值 → 警告。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_macd_direction="bearish",
                intra_macd_bar=-0.2,
            )
        )
        assert ok
        assert "MACD空头" in reason
        assert mul == pytest.approx(0.8, rel=0.01)

    def test_intra_macd_bullish_boost(self):
        """MACD 多头且 bar > 0.2 → 1.1x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_macd_direction="bullish",
                intra_macd_bar=0.3,
            )
        )
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)

    def test_intra_kdj_j_over_110_reject(self):
        """日内KDJ J > 110 → 拒绝（新阈值）。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_kdj_j=120,
            )
        )
        assert not ok
        assert "KDJ" in reason

    def test_intra_kdj_j_under_0_boost(self):
        """日内KDJ J < 0 → 1.1x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_kdj_j=-5,
            )
        )
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)

    def test_intra_kdj_death_cross_warn(self):
        """日内KDJ K<D 且 J<50 → 警告，0.85x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_kdj_k=30,
                intra_kdj_d=40,
                intra_kdj_j=35,
            )
        )
        assert ok
        assert "死叉" in reason
        assert mul == pytest.approx(0.85, rel=0.01)

    def test_price_vs_ma5_far_below_reject(self):
        """price_vs_ma5 < -3 → 拒绝。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                intra_available=True,
                intra_price_vs_ma5=-4.0,
            )
        )
        assert not ok
        assert "接飞刀" in reason

    # ── 盘口 ──

    def test_ob_ratio_low_reject(self):
        """ob_ratio <= 0.3 且有 reason → 拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(ob_ratio=0.2, ob_reason="卖盘沉重"))
        assert not ok
        assert "卖盘" in reason

    def test_ob_ratio_mid_warn(self):
        """0.3 < ob_ratio <= 0.42 → 警告，0.85x。"""
        ok, reason, mul = evaluate_buy(self.make(ob_ratio=0.35, ob_reason="卖盘偏大"))
        assert ok
        assert "卖压" in reason
        assert mul == pytest.approx(0.85, rel=0.01)

    def test_ob_ratio_high_boost(self):
        """ob_ratio >= 0.7 → 1.1x。"""
        ok, reason, mul = evaluate_buy(self.make(ob_ratio=0.75))
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)

    def test_ob_ratio_low_no_reason_skipped(self):
        """ob_ratio <= 0.3 但 ob_reason 为空 → 跳过。"""
        ok, reason, mul = evaluate_buy(self.make(ob_ratio=0.2, ob_reason=""))
        assert ok

    # ── 大单 ──

    def test_big_ratio_low_reject(self):
        """big_ratio <= 0.35 → 拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(big_ratio=0.2, big_reason="大单卖出主导"))
        assert not ok

    def test_big_ratio_mid_warn(self):
        """0.35 < big_ratio <= 0.45 → 警告，0.8x。"""
        ok, reason, mul = evaluate_buy(self.make(big_ratio=0.40, big_reason="大单偏卖出"))
        assert ok
        assert mul == pytest.approx(0.8, rel=0.01)

    def test_big_ratio_high_boost(self):
        """big_ratio >= 0.65 → 1.1x。"""
        ok, reason, mul = evaluate_buy(self.make(big_ratio=0.70, big_reason="大单买入主导"))
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)

    def test_big_ratio_low_no_reason_skipped(self):
        """big_ratio <= 0.35 但 big_reason 为空 → 跳过。"""
        ok, reason, mul = evaluate_buy(self.make(big_ratio=0.2, big_reason=""))
        assert ok

    # ── 涨跌停 ──

    def test_near_up_stop_reject(self):
        """距涨停 < 1% → 拒绝（新阈值）。"""
        ok, reason, mul = evaluate_buy(self.make(up_stop=10.08, price=10.0))
        # room = (10.08-10.0)/10.0*100 = 0.8% < 1%
        assert not ok
        assert "涨停" in reason

    def test_near_up_stop_warn(self):
        """2% <= 距涨停 < 4% → 警告，0.8x。"""
        ok, reason, mul = evaluate_buy(self.make(up_stop=10.30, price=10.0))
        # room = (10.30-10.0)/10.0*100 = 3.0%
        assert ok
        assert "涨停" in reason
        assert mul == pytest.approx(0.8, rel=0.01)

    def test_down_stop_risk_over_15_reject(self):
        """距跌停 > 15% → 拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(down_stop=8.0, price=10.0))
        # risk = (10-8)/10*100 = 20% > 15%
        assert not ok
        assert "跌停" in reason

    # ── 昨日资金流向 ──

    def test_yesterday_mf_large_outflow_reject(self):
        """昨日主力大幅流出 < -8 → 拒绝（新阈值）。"""
        ok, reason, mul = evaluate_buy(self.make(yesterday_mf_ratio=-9.0))
        assert not ok
        assert "流出" in reason

    def test_yesterday_mf_moderate_outflow_warn(self):
        """-8 <= 流出 < -3 → 警告，0.85x（新阈值）。"""
        ok, reason, mul = evaluate_buy(self.make(yesterday_mf_ratio=-5.0))
        assert ok
        assert mul == pytest.approx(0.85, rel=0.01)

    def test_yesterday_mf_large_inflow_boost(self):
        """流入 > 5 → 1.1x。"""
        ok, reason, mul = evaluate_buy(self.make(yesterday_mf_ratio=6.0))
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)

    # ── MA5 角度 ──

    def test_ma5_angle_fast_down_reject(self):
        """ma5_angle < -2 → 拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(ma5_angle=-3.0))
        assert not ok
        assert "加速下行" in reason

    def test_ma5_angle_negative_warn(self):
        """-2 <= ma5_angle < 0 → 警告，0.85x。"""
        ok, reason, mul = evaluate_buy(self.make(ma5_angle=-1.0))
        assert ok
        assert mul == pytest.approx(0.85, rel=0.01)

    # ── 日内位置 ──

    def test_day_position_low_with_rising_ma5_boost(self):
        """day_position < 0.15 且 ma5_angle > 1 → 1.1x。"""
        ok, reason, mul = evaluate_buy(self.make(day_position=0.1, ma5_angle=2.0))
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)

    def test_day_position_near_high_warn(self):
        """day_position > 0.9 → 警告，0.85x。"""
        ok, reason, mul = evaluate_buy(self.make(day_position=0.95))
        assert ok
        assert "高点" in reason
        assert mul == pytest.approx(0.85, rel=0.01)

    # ── BBI ──

    def test_price_below_bbi_warn(self):
        """price < bbi * 0.95 → 警告，0.85x。"""
        ok, reason, mul = evaluate_buy(self.make(bbi_daily=11.0, price=10.0))
        # 10 < 11*0.95 = 10.45 → True
        assert ok
        assert "BBI" in reason
        assert mul == pytest.approx(0.85, rel=0.01)

    # ── 价格走势 ──

    def test_price_action_declining_reject(self):
        """price_action=declining → 拒绝。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                price_action="declining",
                price_action_desc="持续回落",
            )
        )
        assert not ok
        assert "等待止跌" in reason

    def test_price_action_reversing_boost(self):
        """price_action=reversing → 1.15x。"""
        ok, reason, mul = evaluate_buy(self.make(price_action="reversing"))
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)  # min(1.0, 1.15) = 1.0

    # ── 概念板块 ──

    def test_concept_score_low_reject(self):
        """concept_score <= -2 → 拒绝。"""
        ok, reason, mul = evaluate_buy(self.make(concept_score=-3, concept_reason="多数板块弱"))
        assert not ok
        assert "概念板块" in reason

    def test_concept_score_negative_warn(self):
        """-2 < concept_score < 0 → 警告，0.6x。"""
        ok, reason, mul = evaluate_buy(self.make(concept_score=-1, concept_reason="略弱"))
        assert ok
        assert "概念板块" in reason
        assert mul == pytest.approx(0.6, rel=0.01)

    # ── AI 倾向 ──

    def test_ai_focus_boost(self):
        """ai_bias='focus' → sector_very_strong 提升 + size_mult 应用。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                ai_bias="focus",
                ai_size_mult=1.2,
            )
        )
        assert ok
        # base=1.0 * 1.2(ai_size_mult) → min(1.0, 1.2) = 1.0
        assert mul == pytest.approx(1.0, rel=0.01)

    def test_ai_avoid_reduces(self):
        """ai_bias='avoid' → size_mult 应用 + warn。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                ai_bias="avoid",
                ai_size_mult=0.7,
            )
        )
        assert ok
        assert "回避" in reason
        assert mul == pytest.approx(0.7, rel=0.01)

    def test_ai_avoid_with_continuous_weak_reject(self):
        """ai_bias='avoid' + 持续走弱 → 拒绝。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                ai_bias="avoid",
                sector_trend="持续走弱",
                sector_chg=-0.5,
                ai_size_mult=0.5,
            )
        )
        assert not ok
        assert "回避" in reason

    # ── 布林带宽 ──

    def test_bb_width_high_warn(self):
        """bb_width > 40 → 警告，0.8x。"""
        ok, reason, mul = evaluate_buy(self.make(bb_width=50))
        assert ok
        assert "波动" in reason
        assert mul == pytest.approx(0.8, rel=0.01)

    # ── 5min MACD ──

    def test_m5_macd_bearish_warn(self):
        """5min MACD 空头 → 警告，0.85x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                m5_macd_dif=-0.3,
                m5_macd_dea=0.1,
                m5_macd_bar=-0.3,
            )
        )
        assert ok
        assert "5min" in reason
        assert mul == pytest.approx(0.85, rel=0.01)

    def test_m5_macd_bullish_boost(self):
        """5min MACD 多头且 bar > 0.1 → 1.05x。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                m5_macd_dif=0.3,
                m5_macd_dea=0.1,
                m5_macd_bar=0.15,
            )
        )
        assert ok
        assert mul == pytest.approx(1.0, rel=0.01)

    # ── 极端值 / 空值 / 边界 ──

    def test_all_fields_missing_still_evaluates(self):
        """所有可选字段均为默认值或None → 不抛异常，sector_trend 空仅警告。"""
        ctx = BuyEvalInput(
            code="000001",
            price=10.0,
            buy_min=9.5,
            buy_max=10.5,
        )
        ok, reason, mul = evaluate_buy(ctx)
        # sector_trend="" → "数据不足" 现在只是 warn，不再 reject
        assert ok
        assert "数据不足" in reason

    def test_empty_sector_trend(self):
        """空字符串 sector_trend → '板块数据不足' 警告，降低仓位。"""
        ok, reason, mul = evaluate_buy(self.make(sector_trend="", sector_chg=None))
        assert ok
        assert "数据不足" in reason

    def test_negative_price(self):
        """负价格 → 不应崩溃。"""
        ok, reason, mul = evaluate_buy(self.make(price=-5.0))
        # 会触发 up_stop 计算但 price>0 条件保护
        # zone_pos = (-5 - 9.5)/1.0 = -14.5, 无 zone 触发
        # sector_chg = 1.5 不触发
        # 走强 → 无调整
        # 价格走势未设置 → 无调整
        assert ok  # 不拒绝，但 result 可能无意义

    def test_price_zero(self):
        """price=0 → 不应崩溃。"""
        ok, reason, mul = evaluate_buy(self.make(price=0.0))
        # zone_pos = (0-9.5)/1.0 = -9.5, no zone hit
        # 走强, 常规
        assert ok

    def test_price_huge(self):
        """超大价格 → 不应崩溃。"""
        ok, reason, mul = evaluate_buy(self.make(price=99999.0, buy_min=9.5, buy_max=10.5))
        # zone_pos = (99999-9.5)/1.0 = huge → >= 0.85 → 拒绝
        assert not ok
        assert "顶部" in reason

    def test_negative_sector_chg_just_below_threshold(self):
        """sector_chg 略低于 -2.0 触发拒绝（新阈值）。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_trend="走强",
                sector_chg=-2.01,
            )
        )
        assert not ok

    def test_mixed_reject_and_warn_no_conflict(self):
        """拒绝原因优先于警告，返回 reject 且 mul=0。"""
        # 同时触发布林带极度超买 reject (新阈值96) 和 sector weak warn
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_trend="板块走弱",
                sector_chg=-0.5,
                daily_bb_pct_b=96,
            )
        )
        assert not ok
        assert "布林带" in reason
        assert mul == 0.0

    def test_size_mul_capped_at_1_0_with_all_boosts(self):
        """所有正向调整叠加后不超 1.0。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_trend="持续走强",
                sector_chg=3.0,
                intra_available=True,
                intra_rsi6=15,
                price_action="reversing",
                big_ratio=0.70,
                big_reason="大单买入",
                ob_ratio=0.75,
                yesterday_mf_ratio=6.0,
            )
        )
        assert ok
        # 所有 boost 被 min(1.0, ...) 限制
        assert mul <= 1.0

    def test_size_mul_floor_with_warns(self):
        """警告场景下 size_mul 最低 0.5。"""
        ok, reason, mul = evaluate_buy(
            self.make(
                sector_trend="板块走弱",
                sector_chg=-0.5,
                intra_available=True,
                intra_rsi6=78,
                intra_macd_direction="bearish",
                intra_macd_bar=-0.2,
                ob_ratio=0.35,
                ob_reason="卖压",
                big_ratio=0.40,
                big_reason="大单偏卖",
            )
        )
        assert ok
        # 走弱 0.5, 日内RSI 0.7, MACD 0.8, ob 0.85, big 0.8
        # 0.5*0.7*0.8*0.85*0.8 = 0.1904
        # max(0.5, mul) → 0.5
        assert mul == pytest.approx(0.5, rel=0.01)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   evaluate_below_zone 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEvaluateBelowZoneBoundary:
    """evaluate_below_zone 边界条件：偏离幅度、支撑、板块、量能。"""

    @staticmethod
    def make(**kw):
        defaults = {
            "code": "000001",
            "price": 9.3,
            "buy_min": 9.5,
            "buy_max": 10.5,
            "sector_trend": "走强",
            "sector_chg": 1.0,
            "intra_available": False,
        }
        defaults.update(kw)
        return BuyEvalInput(**defaults)

    # ── 偏离幅度 ──

    def test_slightly_below_opportunity(self):
        """below_pct <= 2% → score+2，预期机会。"""
        # below_pct = (9.5-9.4)/9.5*100 = 1.05% → +2
        action, reason, mul = evaluate_below_zone(self.make(price=9.4))
        assert action == "opportunity"
        # base score 2, sector 走强 +1, 总分 3
        assert mul is not None

    def test_moderately_below_opportunity_with_extra(self):
        """2% < below_pct <= 4% → score 不变, 但支持因素多 → 仍可能机会。"""
        # below_pct = (9.5-9.25)/9.5*100 = 2.63% → score+0
        # 需要其他因素 push 到 >= 3
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.25,
                intra_available=True,
                intra_rsi6=25,
                intra_kdj_j=-5,
                # RSI25→+3, KDJ<0→+2 → total >= 5
            )
        )
        assert action == "opportunity"

    def test_far_below_abandon(self):
        """below_pct > 7% + 无正面因素 → abandon。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=8.5,
            )
        )
        # below_pct = (9.5-8.5)/9.5*100=10.5% → -5
        # sector 走强 → +1
        # total = -4 → watching(-4 to 0), not abandon
        assert action in ("watching",)
        assert "偏弱" in reason or "未企稳" in reason

    def test_extreme_below_abandon(self):
        """below_pct > 7% + 板块走弱 → score < -4 → abandon。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=8.0,
                sector_trend="板块走弱",
                sector_chg=-0.5,
            )
        )
        # below_pct = (9.5-8)/9.5*100 = 15.8% → -5
        # 走弱 → -3
        # total = -8 → abandon
        assert action == "abandon"

    # ── near_support ──

    def test_near_support_even_far_below_opportunity(self):
        """距支撑近且其他指标配合 → opportunity。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=8.8,
                intra_available=True,
                intra_rsi6=25,
                intra_kdj_j=-5,
                intra_macd_direction="bullish",
            ),
            near_support=True,
        )
        # below_pct=7.4% → -5
        # near_support → +4 → -1
        # RSI<=25 → +3 → 2
        # KDJ<0 → +2 → 4
        # MACD bullish → +1 → 5
        # sector 走强+1? No, "走强" trigger? Let me check...
        # evaluate_below_zone has: elif "走强" in trend: score += 1
        # So "走强" → +1
        # total = -5+4+3+2+1+1 = 6 → opportunity
        assert action == "opportunity"

    def test_near_support_without_other_factors_watching(self):
        """有支撑但其他因素不足 → watching。"""
        action, reason, mul = evaluate_below_zone(
            self.make(price=8.5),
            near_support=True,
        )
        # below_pct=10.5% → -5
        # near_support → +4 → -1
        # 走强 → +1 → 0
        # 0 → watching
        assert action == "watching"

    # ── 板块趋势 ──

    def test_sector_weak_below_zone_wait(self):
        """板块持续走弱 + price below zone → watching（前置返回）。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.0,
                sector_trend="持续走弱",
                sector_chg=-0.5,
            )
        )
        assert action == "watching"
        assert "持续走弱" in reason

    def test_sector_moderate_weak_score_reduction(self):
        """ "走弱" in trend → score -= 3。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.2,
                sector_trend="板块走弱",
                sector_chg=-0.5,
            )
        )
        # below_pct=3.16% → score+0
        # 走弱 → -3 → -3
        # 走强? No, "板块走弱" contains "走弱" but not "走强"
        # Wait: the code checks elif "走弱" in trend (line 434)
        # "板块走弱" → "走弱" is in trend → score -= 3
        # Then there's no else for "走强" since the chain ends
        # total = -3 → watching
        assert action == "watching"

    def test_sector_strong_boost(self):
        """ "持续走强" → score += 3。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.3,
                sector_trend="持续走强",
                sector_chg=2.0,
            )
        )
        # below_pct=2.1% → score+0 (2.1 > 2, ≤ 4)
        # wait: below_pct = (9.5-9.3)/9.5*100 = 2.1% → >2, ≤4 → score+0
        # 持续走强 → +3
        # total=3 → opportunity
        assert action == "opportunity"

    # ── 开盘板块数据不足 ──

    def test_sector_data_insufficient_wait(self):
        """板块数据不足 → watching 并返回 None mul。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                sector_trend="",
                sector_chg=None,
            )
        )
        assert action == "watching"
        assert "数据不足" in reason
        assert mul is None

    # ── 概念板块 ──

    def test_concept_score_low_reject(self):
        """concept_score <= -2 → watching。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                concept_score=-3,
                concept_reason="多数概念板块弱",
            )
        )
        assert action == "watching"

    # ── 量能 ──

    def test_vol_shrinking_boost(self):
        """vol_shrinking=True → score+2。"""
        action, reason, mul = evaluate_below_zone(
            self.make(price=9.4, intra_available=True, intra_rsi6=25),
            vol_shrinking=True,
        )
        # below_pct=1.05% → +2
        # intra_rsi6<=25 → +3 → 5
        # vol_shrinking → +2 → 7
        # sector 走强 → +1 → 8
        assert action == "opportunity"

    def test_vol_surging_penalty(self):
        """vol_surging=True → score-2。"""
        # 必须有足够其他因素补偿
        action, reason, mul = evaluate_below_zone(
            self.make(price=9.4),
            vol_surging=True,
        )
        # below_pct=1.05% → +2
        # vol_surging → -2 → 0
        # sector 走强+1 → 1
        # 1 < 3 → watching
        assert action == "watching"

    # ── 价格走势 ──

    def test_declining_below_zone_wait(self):
        """价格持续下跌 → watching 提前返回。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.0,
                price_action="declining",
                price_action_desc="持续下跌",
            )
        )
        assert action == "watching"
        assert "止跌" in reason

    def test_reversing_big_boost(self):
        """price_action=reversing → score+5。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.4,
                price_action="reversing",
            )
        )
        # below_pct=1.05% → +2
        # reversing → +5 → 7
        # sector 走强 → +1 → 8
        assert action == "opportunity"

    def test_stabilizing_boost(self):
        """price_action=stabilizing → score+3。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.25,
                price_action="stabilizing",
            )
        )
        # below_pct=2.63% → +0
        # stabilizing → +3 → 3
        # sector 走强 → +1 → 4
        assert action == "opportunity"

    # ── 边界 ──

    def test_below_pct_exactly_2(self):
        """below_pct 精确等于 2% → +2。"""
        # below_pct = (9.5-9.31)/9.5*100 = 2.0% (approximately)
        action, reason, mul = evaluate_below_zone(self.make(price=9.31))
        # 2.0% <= 2 → +2
        # sector 走强+1 → 3
        assert action == "opportunity"
        assert mul is not None

    def test_below_pct_exactly_4(self):
        """below_pct 精确等于 4% → 0 (≤4 branch)。"""
        # price such that (9.5-p)/9.5*100 = 4 → p = 9.12
        action, reason, mul = evaluate_below_zone(self.make(price=9.12))
        assert action in ("opportunity", "watching")

    def test_below_pct_exactly_7(self):
        """below_pct 精确等于 7% → -2。"""
        # price such that (9.5-p)/9.5*100 = 7 → p = 8.835
        action, reason, mul = evaluate_below_zone(self.make(price=8.835))
        # -2 + sector(走强+1) = -1 → watching
        assert action == "watching"

    def test_score_thresholds_boundary(self):
        """score 精确等于 6 / 3 / 0 / -4 的边界行为。"""
        # 构造 score=5 → opportunity, mul capped at 0.7
        ctx = self.make(
            price=9.3,  # below_pct=2.1% → +0
            intra_available=True,
            intra_rsi6=25,  # +3
            intra_kdj_j=-5,  # +2
        )
        # +0 +3 +2 +1(走强) = 6 → opportunity
        action, reason, mul = evaluate_below_zone(ctx)
        assert action == "opportunity"
        assert mul is not None
        # score>=6: mul = min(1.0, 0.5+6*0.05)=min(1.0, 0.8)=0.8

    def test_big_ratio_high_boost(self):
        """big_ratio >= 0.6 → score+3。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.4,  # below_pct=1.05% → +2
                big_ratio=0.7,
                big_reason="大单买入主导",
            )
        )
        # +2 +3 +1(走强) = 6 → opportunity
        assert action == "opportunity"

    def test_big_ratio_low_penalty(self):
        """big_ratio <= 0.4 → score-3。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.4,
                big_ratio=0.3,
                big_reason="大单卖出主导",
            )
        )
        # +2 -3 +1(走强) = 0 → watching
        assert action == "watching"

    def test_ob_ratio_high_boost(self):
        """ob_ratio >= 0.65 → score+2。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.4,
                ob_ratio=0.7,
            )
        )
        # +2 +2 +1 = 5 → opportunity, mul=0.5+5*0.05=0.75, min(0.7,0.75)=0.7
        assert action == "opportunity"

    def test_ob_ratio_low_penalty(self):
        """ob_ratio <= 0.35 → score-2。"""
        action, reason, mul = evaluate_below_zone(
            self.make(
                price=9.4,
                ob_ratio=0.3,
            )
        )
        # +2 -2 +1 = 1 → watching
        assert action == "watching"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   analyze_exit_signals 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAnalyzeExitSignalsBoundary:
    """analyze_exit_signals 边界：技术阻力、超卖、大盘环境组合。"""

    # ── 无信号 ──

    def test_no_signals_healthy(self):
        """价格远离所有阻力，无异常→所有列表为空。"""
        exit_s, wait_s, env = analyze_exit_signals(
            price=10.0,
            entry_price=8.0,
            trend="走强",
        )
        assert len(exit_s) == 0
        assert len(wait_s) == 0
        assert len(env) == 0

    # ── 布林中轨阻力 ──

    def test_exit_near_bb_mid(self):
        """price >= bb_mid*0.97 → exit signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            8.0,
            "横盘",
            bb_mid=10.2,
        )
        # 10.0 >= 10.2*0.97 = 9.894 → True
        assert any("布林中轨" in s for s in exit_s)

    def test_no_bb_mid_signal_below_threshold(self):
        """price < bb_mid*0.97 → 无信号。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            8.0,
            "横盘",
            bb_mid=10.5,
        )
        # 10.0 < 10.5*0.97 = 10.185 → False
        assert not any("布林中轨" in s for s in exit_s)

    # ── MA60 阻力 ──

    def test_exit_near_ma60(self):
        """price >= ma60*0.97 → exit signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            8.0,
            "横盘",
            ma60=10.2,
        )
        assert any("MA60" in s for s in exit_s)

    # ── MACD 空头 ──

    def test_exit_macd_bearish(self):
        """macd_bar<0 且 macd_dif<0 → exit signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            8.0,
            "横盘",
            macd_bar=-0.2,
            macd_dif=-0.1,
        )
        assert any("MACD" in s for s in exit_s)

    def test_exit_macd_bullish_no_signal(self):
        """macd_bar>0 → 无 MACD exit 信号。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            8.0,
            "横盘",
            macd_bar=0.2,
            macd_dif=0.1,
        )
        assert not any("MACD" in s for s in exit_s)

    # ── BBI ──

    def test_exit_far_below_bbi(self):
        """price < bbi 且 deviation > 5% → exit signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            8.0,
            "横盘",
            bbi_daily=11.0,
        )
        # (11-10)/10*100 = 10% > 5%
        assert any("BBI" in s for s in exit_s)

    def test_no_bbi_signal_small_deviation(self):
        """price < bbi 但 deviation <= 5% → 无信号。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            8.0,
            "横盘",
            bbi_daily=10.3,
        )
        # (10.3-10)/10*100 = 3% < 5%
        assert not any("BBI" in s for s in exit_s)

    # ── RSI 超卖 ──

    def test_wait_rsi_oversold_safe_market(self):
        """安全市场下 RSI12<30 → wait signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            risk_level="safe",
            rsi12=25,
        )
        assert any("超卖" in w for w in wait_s)
        assert not any("超卖" in s for s in exit_s)

    def test_exit_rsi_oversold_panic_market(self):
        """恐慌市场下 RSI12<30 → exit signal（反弹不可靠）。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            risk_level="extreme",
            pattern="panic",
            rsi12=25,
        )
        assert any("超卖" in s for s in exit_s)
        assert not any("超卖" in w for w in wait_s)

    def test_wait_rsi6_oversold(self):
        """安全市场下 RSI6<25 但 RSI12>=30 → wait by RSI6。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            rsi6=20,
            rsi12=35,
        )
        assert any("RSI(6)" in w for w in wait_s)

    def test_exit_rsi_oversold_onesided(self):
        """one_sided 模式 + RSI12<30 → exit（不信任反弹）。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            risk_level="dangerous",
            pattern="one_sided",
            rsi12=25,
        )
        assert any("超卖" in s for s in exit_s)

    # ── 布林下轨 ──

    def test_wait_near_bb_lower_safe(self):
        """安全市场触及布林下轨 → wait signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            bb_lower=10.0,
        )
        # price=10 <= 10*1.03=10.3 → True
        assert any("布林下轨" in w for w in wait_s)
        assert not any("布林下轨" in s for s in exit_s)

    def test_exit_near_bb_lower_panic(self):
        """恐慌市场触及布林下轨 → exit signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            risk_level="extreme",
            pattern="panic",
            bb_lower=10.0,
        )
        assert any("布林下轨" in s for s in exit_s)

    # ── KDJ ──

    def test_wait_kdj_oversold_safe(self):
        """安全市场 KDJ J<0 → wait signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            kdj_j=-5,
        )
        assert any("KDJ" in w for w in wait_s)

    def test_exit_kdj_oversold_panic(self):
        """恐慌市场 KDJ J<0 → exit signal。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            risk_level="extreme",
            pattern="panic",
            kdj_j=-5,
        )
        assert any("KDJ" in s for s in exit_s)

    # ── 大盘环境 ──

    def test_env_panic(self):
        """panic 模式 → env part 包含恐慌描述。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            risk_level="extreme",
            pattern="panic",
        )
        assert any("恐慌" in e for e in env)

    def test_env_dangerous(self):
        """risk_level=dangerous → env part。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            risk_level="dangerous",
        )
        assert any("危险" in e for e in env)

    def test_env_cautious(self):
        """risk_level=cautious → env part。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "横盘",
            risk_level="cautious",
        )
        assert any("谨慎" in e for e in env)

    # ── 板块走势 ──

    def test_sector_weak_accelerating_env(self):
        """板块加速走弱 → env part。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "持续走弱加速下跌",
            risk_level="safe",
        )
        assert any("加速" in e for e in env)

    def test_sector_weak_env(self):
        """板块走弱（弱于大盘）→ env part。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "弱于大盘",
            risk_level="safe",
        )
        assert any("走弱" in e for e in env)

    def test_sector_weak_pub_die_env(self):
        """板块普跌 → env part。"""
        exit_s, wait_s, env = analyze_exit_signals(
            10.0,
            12.0,
            "普跌",
            risk_level="safe",
        )
        assert any("走弱" in e or "普跌" in e for e in env)

    # ── 全部信号同时触发 ──

    def test_all_signals_simultaneous(self):
        """所有信号同时触发 → exit_signals 包含所有相关条目，wait_signals 为空。"""
        exit_s, wait_s, env = analyze_exit_signals(
            price=9.5,
            entry_price=12.0,
            trend="持续走弱加速",
            risk_level="extreme",
            pattern="panic",
            bb_mid=9.6,
            ma60=9.6,
            macd_bar=-0.5,
            macd_dif=-0.3,
            bbi_daily=11.0,
            rsi12=25,
            rsi6=20,
            bb_lower=9.8,
            kdj_j=-10,
        )
        # panic market → 所有超卖信号转入 exit_signal
        # price=9.5, bb_mid*0.97=9.312, 9.5>=9.312 → exit
        # ma60*0.97=9.312 → exit
        # macd_bar<0 and macd_dif<0 → exit
        # bbi: (11-9.5)/9.5*100=15.8% >5% → exit
        # bb_lower: 9.5 <= 9.8*1.03=10.094 → exit (panic)
        # kdj_j<0 → exit (panic)
        # rsi12<30 → exit (panic)
        assert len(exit_s) >= 6
        # wait_signals should be empty in panic
        assert len(wait_s) == 0
        assert len(env) >= 1

    def test_wait_signals_in_safe_market(self):
        """安全市场下多个超卖信号共存。"""
        exit_s, wait_s, env = analyze_exit_signals(
            price=9.5,
            entry_price=12.0,
            trend="横盘",
            risk_level="safe",
            rsi12=25,
            rsi6=20,
            bb_lower=9.6,
            kdj_j=-10,
        )
        # rsi12<30 → wait
        # rsi6<25 → wait (rsi12 >= 30 时才 check? No, else-if chain)
        # Actually: if rsi12 < 30 → wait; elif rsi6 < 25 → wait
        # Since rsi12=25 < 30, rsi6 branch is skipped
        assert any("RSI" in w for w in wait_s)
        # bb_lower check: price 9.5 <= 9.6*1.03=9.888 → wait
        assert any("布林下轨" in w for w in wait_s)
        # kdj_j<0 → wait
        assert any("KDJ" in w for w in wait_s)
        assert len(exit_s) == 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   classify_holding_status 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestClassifyHoldingStatusBoundary:
    """classify_holding_status 边界：各持仓状态切换点。"""

    def test_entry_price_zero(self):
        """entry_price <= 0 → watching。"""
        assert classify_holding_status(10.0, 0, 9.0) == "watching"

    def test_healthy_profit(self):
        """pnl_pct > 2% → healthy。"""
        assert classify_holding_status(10.3, 10.0, 9.0) == "healthy"
        # exactly 2% is NOT > 2%, so it's watching
        assert classify_holding_status(10.2, 10.0, 9.0) == "watching"

    def test_deep_trapped(self):
        """loss >= 10% → deep_trapped。"""
        assert classify_holding_status(8.9, 10.0, 9.0) == "deep_trapped"

    def test_trapped(self):
        """5% <= loss < 10% → trapped。"""
        assert classify_holding_status(9.3, 10.0, 9.0) == "trapped"
        assert classify_holding_status(9.5, 10.0, 9.0) == "trapped"

    def test_trapped_exactly_5_percent(self):
        """loss 恰好 5% → trapped（<= -5）。"""
        assert classify_holding_status(9.5, 10.0, 9.0) == "trapped"

    def test_at_risk_near_stop(self):
        """loss 2-5% 且 loss_used >= 0.85 → at_risk（需止损接近入场价）。"""
        # entry=10, sl=9.5 → loss budget = 0.5
        # loss_used = (10-price)/0.5 >= 0.85 → price <= 10-0.425 = 9.575
        # pnl at 9.57 = -4.3% → within (-5%, -2%], not trapped
        status = classify_holding_status(9.57, 10.0, 9.5)
        assert status == "at_risk"

    def test_watching_loss_small(self):
        """loss < 2% → watching。"""
        assert classify_holding_status(9.9, 10.0, 9.0) == "watching"
        assert classify_holding_status(9.81, 10.0, 9.0) == "watching"

    def test_add_opportunity(self):
        """loss 2-5%, loss_used<0.5, pct_b 5-30, rsi12<40 → add_opportunity。"""
        # entry=10, sl=9, loss_budget=1
        # price=9.7, pnl=-3%, loss_used=0.3 < 0.5
        status = classify_holding_status(
            9.7,
            10.0,
            9.0,
            pct_b=20,
            rsi12=35,
        )
        assert status == "add_opportunity"

    def test_add_opportunity_pct_b_outside(self):
        """pct_b 不在 5-30 范围 → 不触发 add_opportunity。"""
        status = classify_holding_status(
            9.8,
            10.0,
            9.0,
            pct_b=3,
            rsi12=35,
        )
        assert status != "add_opportunity"

    def test_watching_small_loss_no_stop(self):
        """loss < 2%, no stop_loss defined → watching。"""
        assert classify_holding_status(9.9, 10.0, 0) == "watching"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   calculate_position_size 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCalculatePositionSizeBoundary:
    """calculate_position_size 边界：模式、板块、宽度、买入区位置。"""

    def test_normal_pattern_full_size(self):
        """normal 模式 → base=60000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
        )
        assert amount == 60000

    def test_uptrend_pattern_full_size(self):
        """uptrend 模式 → base=60000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "uptrend",
            "横盘",
        )
        assert amount == 60000

    def test_unknown_pattern_default_16000(self):
        """未识别模式 → 默认 60000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "unknown_pattern_xyz",
            "横盘",
        )
        assert amount == 60000

    # ── 禁止模式 ──

    @pytest.mark.parametrize(
        "blocked_pattern",
        [
            "panic",
            "one_sided",
            "dead_cat",
            "inverted_v",
            "m_top",
            "gap_up_fade",
            "late_dump",
            "fishing_line",
        ],
    )
    def test_all_blocked_patterns(self, blocked_pattern):
        """BLOCKED 列表中的每个模式返回 0。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            blocked_pattern,
            "横盘",
        )
        assert amount == 0
        assert "暂停买入" in reason or "模式" in reason

    # ── 谨慎模式 ──

    @pytest.mark.parametrize(
        "cautious_pattern",
        [
            "v_reversal",
            "w_bottom",
            "melt_up",
            "late_rally",
            "wide_choppy",
            "gap_down_recover",
        ],
    )
    def test_all_cautious_patterns(self, cautious_pattern):
        """CAUTIOUS 列表中的每个模式 base=8000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            cautious_pattern,
            "横盘",
        )
        assert amount == 30000

    # ── 板块趋势 ──

    def test_sector_continuous_weak_reduce(self):
        """ "持续走弱" → base * 0.3，最低 5000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "持续走弱",
        )
        # 16000 * 0.3 = 4800 → max(4800, 5000) = 5000
        assert amount == 18000
        assert "持续走弱" in reason

    def test_sector_weak_reduce(self):
        """ "走弱" (非持续) → base * 0.6，最低 5000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "板块走弱",
        )
        # 16000 * 0.6 = 9600 > 5000 → 9600
        assert amount == 36000

    def test_sector_continuous_strong_boost(self):
        """ "持续走强" → base * 1.3，最高 16000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "持续走强",
        )
        assert amount == 60000  # 60000*1.3=20800 → capped at 16000

    def test_sector_strong_boost(self):
        """ "走强" (非持续) → base * 1.2，最高 16000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "板块走强",
        )
        # 16000 * 1.2 = 19200 → capped 16000
        assert amount == 60000

    # ── 市场宽度 ──

    def test_breadth_down_over_70_percent(self):
        """down_ratio > 0.7 → base * 0.3，最低 5000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            market_breadth={"up": 200, "down": 800},
        )
        # 800/(200+800) = 0.8 > 0.7 → 16000*0.3=4800 → max(4800,5000)=5000
        assert amount == 18000

    def test_breadth_down_over_60_percent(self):
        """0.6 < down_ratio <= 0.7 → base * 0.5，最低 5000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            market_breadth={"up": 350, "down": 650},
        )
        # 650/(350+650) = 0.65 > 0.6 → 16000*0.5=8000 > 5000
        assert amount == 30000

    def test_breadth_empty_skipped(self):
        """market_breadth 为空或 None → 跳过宽度调整。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            market_breadth={},
        )
        assert amount == 60000

    def test_breadth_total_zero_skipped(self):
        """up+down=0 → 跳过宽度调整。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            market_breadth={"up": 0, "down": 0},
        )
        assert amount == 60000

    # ── 买入区位置 ──

    def test_zone_lower_boost(self):
        """position_in_zone <= 0.33 → base * 1.1，最高 16000。"""
        amount, reason = calculate_position_size(
            "000001",
            9.55,
            9.5,
            10.5,
            "normal",
            "横盘",
        )
        # (9.55-9.5)/1.0 = 0.05 ≤ 0.33 → 16000*1.1=17600 → capped 16000
        assert amount == 60000
        assert "下沿" in reason

    def test_zone_upper_reduce(self):
        """position_in_zone >= 0.67 → base * 0.7，最低 5000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.2,
            9.5,
            10.5,
            "normal",
            "横盘",
        )
        # (10.2-9.5)/1.0 = 0.7 ≥ 0.67 → 16000*0.7=11200
        assert amount == 42000
        assert "上沿" in reason

    def test_zone_middle_no_adjustment(self):
        """0.33 < position_in_zone < 0.67 → 无调整。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
        )
        # (10.0-9.5)/1.0 = 0.5 → 无调整
        assert amount == 60000
        assert "下沿" not in reason
        assert "上沿" not in reason

    # ── 最小值下限 ──

    def test_minimum_floor_5000(self):
        """宽度+板块双重衰减后仍 >= 5000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "持续走弱",
            market_breadth={"up": 200, "down": 800},
        )
        # base=16000 → breadth *0.3 → 4800 → max(5000) → 5000
        # sector *0.3 → 1500 → max(5000) → 5000
        # Actually: breadth adjusts first, then sector
        # After breadth: max(16000*0.3, 5000) = 5000
        # Then sector: max(5000*0.3, 5000) = 5000
        assert amount == 5400

    # ── AI 板块倾向 ──

    def test_ai_focus_increase(self):
        """AI focus + size_mult=1.5 → 16000*1.5=24000→capped 16000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            industry_cache={"000001": "半导体"},
            morning_sector_bias={"半导体": {"bias": "focus", "size_mult": 1.5}},
        )
        assert amount == 60000

    def test_ai_focus_reduce(self):
        """AI focus + size_mult=0.6 → 16000*0.6=9600。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            industry_cache={"000001": "半导体"},
            morning_sector_bias={"半导体": {"bias": "focus", "size_mult": 0.6}},
        )
        assert amount == 36000

    def test_ai_avoid_reduce(self):
        """AI avoid → size_mult 应用，最低 3000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            industry_cache={"000001": "半导体"},
            morning_sector_bias={"半导体": {"bias": "avoid", "size_mult": 0.5}},
        )
        # 16000*0.5=8000 > 3000
        assert amount == 30000

    def test_ai_avoid_low_floor_3000(self):
        """AI avoid + size_mult 极低 → floor 3000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            industry_cache={"000001": "半导体"},
            morning_sector_bias={"半导体": {"bias": "avoid", "size_mult": 0.1}},
        )
        # 16000*0.1=1600 → max(1600, 3000) = 3000
        assert amount == 6000

    def test_ai_bias_empty_no_effect(self):
        """industry_cache 无匹配 → 跳过 AI 调整。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "横盘",
            industry_cache={"000002": "半导体"},
            morning_sector_bias={"半导体": {"bias": "focus", "size_mult": 1.5}},
        )
        assert amount == 60000

    # ── 返回值格式 ──

    def test_amount_rounded_to_100(self):
        """金额向下取整到 100。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "v_reversal",
            "横盘",
        )
        # base=8000, 8000//100*100 = 8000
        assert amount % 100 == 0

        # sector weak: 9600 // 100 * 100 = 9600
        amount2, _ = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "板块走弱",
        )
        assert amount2 % 100 == 0

    def test_cautious_with_sector_weak_floor(self):
        """谨慎模式 + 板块持续走弱 → 双重衰减后 floor 5000。"""
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "v_reversal",
            "持续走弱",
        )
        # base=8000, sector: max(8000*0.3, 5000)=5000
        assert amount == 9000

    def test_cautious_with_zone_upper(self):
        """谨慎模式 + 买入区上沿 → 8000*0.7=5600。"""
        amount, reason = calculate_position_size(
            "000001",
            10.2,
            9.5,
            10.5,
            "v_reversal",
            "横盘",
        )
        # base=8000, zone_upper: 8000*0.7=5600, floor 5000
        assert amount == 21000


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   calc_unified_stop_loss 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCalcUnifiedStopLossBoundary:
    """calc_unified_stop_loss 边界：ATR、策略、板块、支撑、地板。"""

    def test_default_stop_no_indicators(self):
        """无 indicators → 默认 ATR=3%。"""
        sl = calc_unified_stop_loss("000001", 10.0)
        # atr_pct=0.03, strategy_mult=1.0, sector_mult=1.0
        # raw = 10*(1-0.03*2*1*1)=10*0.94=9.4
        # floor = max(9.4, 9.3)=9.4
        assert sl == pytest.approx(9.40, rel=0.01)

    def test_trend_strategy_wider_stop(self):
        """trend 策略 → 1.2x ATR。"""
        sl = calc_unified_stop_loss("000001", 10.0, strategy_type="trend")
        # atr_pct=0.03, strategy_mult=1.2
        # raw = 10*(1-0.03*2*1.2) = 10*0.928 = 9.28
        # floor = max(9.28, 9.3) = 9.30
        assert sl == pytest.approx(9.30, rel=0.01)

    def test_chase_strategy_tighter_stop(self):
        """chase 策略 → 0.8x ATR。"""
        sl = calc_unified_stop_loss("000001", 10.0, strategy_type="chase")
        # raw = 10*(1-0.03*2*0.8) = 10*0.952 = 9.52
        # floor = max(9.52, 9.3) = 9.52
        assert sl == pytest.approx(9.52, rel=0.01)

    def test_sector_continuous_weak_tighter(self):
        """板块持续走弱 → sector_mult=0.85。"""
        sl = calc_unified_stop_loss("000001", 10.0, trend="持续走弱")
        # raw = 10*(1-0.03*2*1*0.85) = 10*0.949 = 9.49
        assert sl == pytest.approx(9.49, rel=0.01)

    def test_sector_weak_slight_tighten(self):
        """板块走弱 → sector_mult=0.92。"""
        sl = calc_unified_stop_loss("000001", 10.0, trend="板块走弱")
        # raw = 10*(1-0.03*2*1*0.92) = 10*0.9448 = 9.448
        assert sl == pytest.approx(9.45, rel=0.01)

    def test_sector_strong_wider_stop(self):
        """板块持续走强 → sector_mult=1.1。"""
        sl = calc_unified_stop_loss("000001", 10.0, trend="持续走强")
        # raw = 10*(1-0.03*2*1*1.1) = 10*0.934 = 9.34
        # floor = max(9.34, 9.3) = 9.34
        assert sl == pytest.approx(9.34, rel=0.01)

    def test_atr_from_indicator(self):
        """从 indicators 取真实 ATR。"""
        sl = calc_unified_stop_loss("000001", 10.0, daily_indicators={"atr14": 0.5})
        # atr_pct=0.5/10=0.05
        # raw = 10*(1-0.05*2*1*1) = 9.0
        # floor = max(9.0, 9.3) = 9.30
        assert sl == pytest.approx(9.30, rel=0.01)

    def test_support_constraint(self):
        """支撑位约束使止损不低于支撑*0.99。"""
        sl = calc_unified_stop_loss(
            "000001",
            10.0,
            daily_indicators={"_supports": [(9.5,)]},
        )
        # raw = 9.4, nearest_support=9.5, raw_sl=max(9.4, 9.5*0.99=9.405)=9.405
        # floor = max(9.405, 9.3) = 9.41 (rounded)
        assert sl >= 9.40

    def test_hard_floor_93_percent(self):
        """硬地板不低于 93%。"""
        # price=10, with very large ATR
        sl = calc_unified_stop_loss("000001", 10.0, daily_indicators={"atr14": 5.0})
        # atr_pct=0.5, raw = 10*(1-0.5*2) = 0
        # floor = max(0, 9.3) = 9.30
        assert sl == pytest.approx(9.30, rel=0.01)

    def test_price_zero_no_crash(self):
        """price=0 → 不崩溃。"""
        sl = calc_unified_stop_loss("000001", 0.0)
        assert sl >= 0

    def test_negative_price_no_crash(self):
        """price<0 → 不崩溃。"""
        sl = calc_unified_stop_loss("000001", -10.0)
        assert sl is not None

    def test_round_to_2_decimals(self):
        """结果保留 2 位小数。"""
        sl = calc_unified_stop_loss("000001", 10.0)
        assert sl == round(sl, 2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   calc_unified_take_profit 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCalcUnifiedTakeProfitBoundary:
    """calc_unified_take_profit 边界：ATR、策略、板块、阻力、天花板。"""

    def test_default_tp_no_indicators(self):
        """无 indicators → 默认 ATR=3%，3x ATR。"""
        tp = calc_unified_take_profit("000001", 10.0)
        # atr_pct=0.03, strategy_mult=1.0, sector_mult=1.0
        # raw = 10*(1+0.03*3*1*1) = 10.9
        # ceiling = min(10.9, 11.2) = 10.90
        assert tp == pytest.approx(10.90, rel=0.01)

    def test_trend_strategy_higher_tp(self):
        """trend → 1.3x ATR。"""
        tp = calc_unified_take_profit("000001", 10.0, strategy_type="trend")
        # raw = 10*(1+0.03*3*1.3) = 10*1.117 = 11.17
        # ceiling = min(11.17, 11.20) = 11.17
        assert tp == pytest.approx(11.17, rel=0.01)

    def test_chase_strategy_lower_tp(self):
        """chase → 0.7x ATR。"""
        tp = calc_unified_take_profit("000001", 10.0, strategy_type="chase")
        # raw = 10*(1+0.03*3*0.7) = 10*1.063 = 10.63
        # ceiling = min(10.63, 11.20) = 10.63
        assert tp == pytest.approx(10.63, rel=0.01)

    def test_sector_strong_higher_tp(self):
        """持续走强 → sector_mult=1.15。"""
        tp = calc_unified_take_profit("000001", 10.0, trend="持续走强")
        # raw = 10*(1+0.03*3*1*1.15) = 10*1.1035 = 11.035
        # ceiling = min(11.035, 11.20) = 11.04 (rounded)
        assert tp == pytest.approx(11.04, rel=0.01)

    def test_sector_weak_lower_tp(self):
        """持续走弱 → sector_mult=0.85。"""
        tp = calc_unified_take_profit("000001", 10.0, trend="持续走弱")
        # raw = 10*(1+0.03*3*0.85) = 10*1.0765 = 10.765
        assert tp == pytest.approx(10.77, rel=0.01)

    def test_resistance_constraint(self):
        """阻力位约束使止盈不高于最近阻力。"""
        tp = calc_unified_take_profit(
            "000001",
            10.0,
            daily_indicators={"_resistances": [(10.5,)]},
        )
        # raw=10.9, nearest_resistance=10.5, raw_tp=min(10.9,10.5)=10.5
        # ceiling = min(10.5, 11.20) = 10.50
        assert tp == pytest.approx(10.50, rel=0.01)

    def test_hard_ceiling_112_percent(self):
        """硬天花板不高于 112%。"""
        tp = calc_unified_take_profit(
            "000001",
            10.0,
            daily_indicators={"atr14": 5.0},
        )
        # atr_pct=0.5, raw=10*(1+0.5*3)=25, ceiling=min(25,11.20)=11.20
        assert tp == pytest.approx(11.20, rel=0.01)

    def test_price_zero_no_crash(self):
        """price=0 → 不崩溃。"""
        tp = calc_unified_take_profit("000001", 0.0)
        assert tp >= 0

    def test_negative_price_no_crash(self):
        """price<0 → 不崩溃。"""
        tp = calc_unified_take_profit("000001", -10.0)
        assert tp is not None

    def test_round_to_2_decimals(self):
        """结果保留 2 位小数。"""
        tp = calc_unified_take_profit("000001", 10.0)
        assert tp == round(tp, 2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#   assess_regime 边界
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestAssessRegimeBoundary:
    """assess_regime 边界：全部 16 模式、position_mult、allow_buy、时段、跳空。"""

    # ── 全部 16 模式基础映射 ──

    @pytest.mark.parametrize(
        "pattern,expected",
        [
            ("normal", (True, 1.0)),
            ("uptrend", (True, 1.0)),
            ("v_reversal", (True, 0.5)),
            ("w_bottom", (True, 0.7)),
            ("melt_up", (True, 0.3)),
            ("gap_down_recover", (True, 0.5)),
            ("late_rally", (True, 0.3)),
            ("wide_choppy", (True, 0.3)),
            ("one_sided", (False, 0.0)),
            ("inverted_v", (False, 0.0)),
            ("panic", (False, 0.0)),
            ("dead_cat", (False, 0.0)),
            ("m_top", (False, 0.0)),
            ("gap_up_fade", (False, 0.0)),
            ("late_dump", (False, 0.0)),
            ("fishing_line", (False, 0.0)),
        ],
    )
    def test_all_patterns_base_mapping(self, pattern, expected):
        """全部 16 种模式的 allow_buy 和 position_mult 基础值正确。"""
        allow_buy_expected, mult_expected = expected
        regime = assess_regime(pattern, 3400, 3390, 0.003)
        assert regime.pattern == pattern
        assert regime.allow_buy == allow_buy_expected
        assert regime.position_mult == pytest.approx(mult_expected, rel=0.01)
        assert isinstance(regime, MarketRegime)

    # ── 技术上下文：低于 MA20 ──

    def test_below_ma20_risk_upgrade(self):
        """价格低于 MA20 且偏离 > 1% → 风险升级。"""
        regime = assess_regime("normal", 3300, 3400, -0.03, ma20=3400)
        # deviation = (3400-3300)/3400 = 2.94% > 1%
        # safe → cautious
        assert regime.risk_level == "cautious"
        assert regime.confidence == "low"
        # allow_buy still True, position_mult *= 0.6 → 0.6
        assert regime.position_mult == pytest.approx(0.6, rel=0.01)

    def test_below_ma20_slight_deviation(self):
        """价格低于 MA20 且 0.5% < 偏离 <= 1% → 部分降仓。"""
        regime = assess_regime("normal", 3380, 3400, -0.01, ma20=3400)
        # deviation = (3400-3380)/3400 = 0.59%
        # 0.005 < 0.0059 <= 0.01 → position_mult *= 0.8 → 0.8
        assert regime.position_mult == pytest.approx(0.8, rel=0.01)
        # risk_level 不变（不升级）
        assert regime.risk_level == "safe"

    def test_below_ma20_minimal_deviation(self):
        """偏离 <= 0.5% → 无调整。"""
        regime = assess_regime("normal", 3395, 3400, -0.002, ma20=3400)
        # deviation = (3400-3395)/3400 = 0.15% <= 0.5%
        assert regime.position_mult == pytest.approx(1.0, rel=0.01)

    def test_above_ma20_no_adjustment(self):
        """价格在 MA20 之上 → 无调整。"""
        regime = assess_regime("normal", 3450, 3400, 0.015, ma20=3400)
        assert regime.risk_level == "safe"
        assert regime.position_mult == pytest.approx(1.0, rel=0.01)

    # ── 技术上下文：低于 MA60 ──

    def test_below_ma60_risk_upgrade(self):
        """价格低于 MA60 → 风险升级一次。"""
        regime = assess_regime("normal", 3300, 3400, -0.03, ma60=3450)
        # safe → cautious
        assert regime.risk_level == "cautious"
        assert regime.confidence == "low"

    def test_below_both_ma20_and_ma60_double_upgrade(self):
        """同时低于 MA20 和 MA60 → 风险升级两次。"""
        regime = assess_regime("normal", 3200, 3400, -0.06, ma20=3350, ma60=3400)
        # ma20: safe → cautious
        # ma60: cautious → dangerous
        assert regime.risk_level == "dangerous"
        assert regime.confidence == "low"

    # ── 市场宽度 ──

    def test_breadth_down_heavy_reduce(self):
        """down_ratio > 0.7 → 风险升级 + position_mult * 0.5。"""
        regime = assess_regime(
            "normal",
            3400,
            3390,
            0.001,
            market_breadth={"up": 150, "down": 850},
        )
        # 850/(150+850)=0.85 > 0.7
        # breadth: safe → cautious, position_mult = max(0.2, 1.0*0.5)=0.5
        assert "cautious" in regime.risk_level
        assert regime.position_mult == pytest.approx(0.5, rel=0.01)
        assert not regime.breadth_healthy

    def test_breadth_down_moderate(self):
        """0.6 < down_ratio <= 0.7 → position_mult * 0.7。"""
        regime = assess_regime(
            "normal",
            3400,
            3390,
            0.001,
            market_breadth={"up": 350, "down": 650},
        )
        # 650/(350+650)=0.65 > 0.6
        # 不升级 risk_level，position_mult = max(0.4, 1.0*0.7)=0.7
        assert regime.position_mult == pytest.approx(0.7, rel=0.01)

    def test_breadth_up_heavy_no_change(self):
        """up/down > 0.6 且 change 极小 → breadth_healthy=True。"""
        regime = assess_regime(
            "normal",
            3400,
            3390,
            0.001,
            market_breadth={"up": 700, "down": 300},
        )
        # up/total=0.7 > 0.6, abs(0.001)<0.005 → breadth_healthy=True
        assert regime.breadth_healthy

    # ── 时段 ──

    def test_opening_phase_reduces(self):
        """开盘阶段 → position_mult * 0.6 + entry_rule=confirm。"""
        regime = assess_regime("normal", 3400, 3390, 0.003, session_phase="opening")
        assert regime.position_mult == pytest.approx(0.6, rel=0.01)
        assert regime.entry_rule == "confirm"
        assert regime.confidence == "low"

    def test_pre_open_phase_reduces(self):
        """盘前阶段 → 同 opening。"""
        regime = assess_regime("normal", 3400, 3390, 0.003, session_phase="pre_open")
        assert regime.position_mult == pytest.approx(0.6, rel=0.01)
        assert regime.confidence == "low"

    def test_closing_phase_entry_rule_next_day(self):
        """尾盘阶段 + standard entry → entry_rule=next_day。"""
        regime = assess_regime("normal", 3400, 3390, 0.003, session_phase="closing")
        assert regime.entry_rule == "next_day"
        assert regime.session_phase == "closing"

    def test_closing_non_standard_entry_unchanged(self):
        """尾盘但 entry_rule 非 standard → 不修改。"""
        regime = assess_regime("uptrend", 3400, 3390, 0.003, session_phase="closing")
        # uptrend entry_rule=pullback, 不是 standard
        assert regime.entry_rule == "pullback"

    # ── 跳空方向 ──

    def test_gap_up_detected(self):
        """gap >= 1% → gap_up。"""
        regime = assess_regime("normal", 3430, 3380, 0.015)
        # (3430-3380)/3380 = 1.48% >= 1%
        assert regime.gap_direction == "gap_up"

    def test_gap_down_detected(self):
        """gap <= -1% → gap_down。"""
        regime = assess_regime("normal", 3340, 3400, -0.018)
        # (3340-3400)/3400 = -1.76% <= -1%
        assert regime.gap_direction == "gap_down"

    def test_no_gap_small_change(self):
        """|gap| < 1% → 无方向。"""
        regime = assess_regime("normal", 3400, 3390, 0.003)
        assert regime.gap_direction == ""

    def test_prev_close_zero_no_gap(self):
        """prev_close=0 → 跳过跳空计算。"""
        regime = assess_regime("normal", 3400, 0, 0.003)
        assert regime.gap_direction == ""

    # ── 情景预测 Outlook ──

    def test_outlook_bearish_critical(self):
        """outlook bearish + critical → 风险升级 + allow_buy=False (prob>0.55)。"""
        scenario = MarketScenario(
            name="test_bear",
            label="测试看空",
            probability=0.6,
            direction="bearish",
        )
        outlook = MarketOutlook(
            primary=scenario,
            alternatives=[],
            key_support=[],
            key_resistance=[],
            bias="bearish",
            urgency="critical",
        )
        regime = assess_regime("normal", 3400, 3390, 0.003, outlook=outlook)
        assert not regime.allow_buy
        assert regime.position_mult == 0.0
        assert regime.entry_rule == "none"
        assert regime.urgent_action == "tighten_stops"
        # risk upgraded: safe → cautious
        assert regime.risk_level == "cautious"

    def test_outlook_bearish_moderate_prob(self):
        """outlook bearish + critical + 0.35 < prob <= 0.55 → 降仓不禁止。"""
        scenario = MarketScenario(
            name="test_bear",
            label="测试看空",
            probability=0.4,
            direction="bearish",
        )
        outlook = MarketOutlook(
            primary=scenario,
            alternatives=[],
            key_support=[],
            key_resistance=[],
            bias="bearish",
            urgency="critical",
        )
        regime = assess_regime("normal", 3400, 3390, 0.003, outlook=outlook)
        assert regime.allow_buy  # prob <= 0.55, not disallowed
        # position_mult *= 0.5 → 0.5
        assert regime.position_mult == pytest.approx(0.5, rel=0.01)
        assert regime.entry_rule == "confirm"

    def test_outlook_bearish_low_prob(self):
        """outlook bearish + 0.35 < prob，urgency=watch → stop_mult * 1.1。"""
        scenario = MarketScenario(
            name="test_bear",
            label="测试看空",
            probability=0.4,
            direction="bearish",
        )
        outlook = MarketOutlook(
            primary=scenario,
            alternatives=[],
            key_support=[],
            key_resistance=[],
            bias="bearish",
            urgency="watch",
        )
        regime = assess_regime("normal", 3400, 3390, 0.003, outlook=outlook)
        assert regime.allow_buy
        assert regime.stop_mult == pytest.approx(1.1, rel=0.01)
        # standard entry → confirm
        assert regime.entry_rule == "confirm"

    def test_outlook_bullish_accelerating_up(self):
        """outlook bullish accelerating_up + critical → 风险升级 + 降仓。"""
        scenario = MarketScenario(
            name="accelerating_up",
            label="加速上涨",
            probability=0.6,
            direction="bullish",
        )
        outlook = MarketOutlook(
            primary=scenario,
            alternatives=[],
            key_support=[],
            key_resistance=[],
            bias="bullish",
            urgency="critical",
        )
        regime = assess_regime("normal", 3400, 3390, 0.003, outlook=outlook)
        # risk_level: safe → cautious
        assert regime.risk_level == "cautious"
        # position_mult = max(0.3, 1.0*0.5) = 0.5
        assert regime.position_mult == pytest.approx(0.5, rel=0.01)
        # stop_mult = 1.0*0.7 = 0.7
        assert regime.stop_mult == pytest.approx(0.7, rel=0.01)
        assert regime.urgent_action == "tighten_stops"

    # ── multi_day_downtrend ──

    def test_multi_day_downtrend_flag(self):
        """multi_day_downtrend=True → 标记正确传递。"""
        regime = assess_regime("normal", 3400, 3390, 0.003, multi_day_downtrend=True)
        assert regime.multi_day_downtrend

    # ── alert_msg ──

    def test_alert_msg_for_panic(self):
        """panic 模式 → alert_msg 非空。"""
        regime = assess_regime("panic", 3300, 3400, -0.03)
        assert len(regime.alert_msg) > 0
        assert "恐慌" in regime.alert_msg

    def test_no_alert_for_normal(self):
        """normal 模式 → alert_msg 为空。"""
        regime = assess_regime("normal", 3400, 3390, 0.003)
        assert regime.alert_msg == ""

    # ── 模式未知时回退 ──

    def test_unknown_pattern_fallback(self):
        """未知 pattern → 回退到 normal。"""
        regime = assess_regime("non_existent_pattern", 3400, 3390, 0.003)
        assert regime.pattern == "non_existent_pattern"
        # 风险等字段从 normal 模板
        assert regime.risk_level == "safe"
        assert regime.allow_buy
        # pattern 字段回传输入值 (注意这里保持输入)
        # PATTERN_REGIME.get(...) returns normal, then pattern override happens
        # Actually looking at the code: base = PATTERN_REGIME.get(pattern, PATTERN_REGIME["normal"]).copy()
        # Then MarketRegime(pattern=pattern, ...)
        # So pattern is "non_existent_pattern" but base from normal
        assert regime.allow_buy is True

    # ── _upgrade_risk 完整覆盖 ──

    def test_upgrade_risk_all_transitions(self):
        """_upgrade_risk 所有有效转换。"""
        assert _upgrade_risk("safe") == "cautious"
        assert _upgrade_risk("cautious") == "dangerous"
        assert _upgrade_risk("dangerous") == "extreme"
        assert _upgrade_risk("extreme") == "extreme"

    def test_upgrade_risk_unknown_treated_as_safe(self):
        """_upgrade_risk 未知级别从 0 升一级 → cautious。"""
        assert _upgrade_risk("unknown") == "cautious"
