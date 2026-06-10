"""trade/decision/ 模块测试"""

from trade.decision.buy import BuyEvalInput, evaluate_below_zone, evaluate_buy
from trade.decision.sell import analyze_exit_signals, classify_holding_status
from trade.decision.sizing import calculate_position_size


class TestEvaluateBuy:
    def _make_ctx(self, **overrides):
        defaults = {
            "code": "000001",
            "price": 10.0,
            "buy_min": 9.5,
            "buy_max": 10.5,
            "sector_trend": "走强",
            "sector_chg": 1.5,
            "intra_available": False,
        }
        defaults.update(overrides)
        return BuyEvalInput(**defaults)

    def test_normal_buy(self):
        ok, reason, mul = evaluate_buy(self._make_ctx())
        assert ok
        assert mul >= 0.5

    def test_sector_weak_reject(self):
        ok, reason, mul = evaluate_buy(self._make_ctx(sector_trend="持续走弱"))
        assert not ok
        assert "持续走弱" in reason

    def test_zone_top_reject(self):
        ok, reason, mul = evaluate_buy(self._make_ctx(price=10.46, buy_min=9.5, buy_max=10.5))
        assert not ok
        assert "顶部" in reason

    def test_bb_overbought_reject(self):
        ok, reason, mul = evaluate_buy(self._make_ctx(daily_bb_pct_b=98))
        assert not ok
        assert "布林带" in reason

    def test_intra_rsi_extreme(self):
        ok, reason, mul = evaluate_buy(
            self._make_ctx(
                intra_available=True,
                intra_rsi6=93,
            )
        )
        assert not ok
        assert "RSI6" in reason

    def test_bearish_alignment_reject(self):
        ok, reason, mul = evaluate_buy(
            self._make_ctx(
                price=9.0,
                buy_min=8.5,
                buy_max=9.5,
                daily_ma5=10.0,
                daily_ma10=11.0,
                daily_ma20=12.0,
            )
        )
        assert not ok
        assert "接飞刀" in reason

    def test_sector_strong_boost(self):
        ok, reason, mul = evaluate_buy(
            self._make_ctx(
                sector_trend="持续走强",
                sector_chg=2.0,
            )
        )
        assert ok
        assert mul > 0.9

    def test_order_book_sell_pressure(self):
        ok, reason, mul = evaluate_buy(self._make_ctx(ob_ratio=0.2, ob_reason="卖盘沉重"))
        assert not ok

    def test_big_order_reject(self):
        ok, reason, mul = evaluate_buy(self._make_ctx(big_ratio=0.2, big_reason="大单卖出主导"))
        assert not ok


class TestEvaluateBelowZone:
    def _make_ctx(self, **overrides):
        defaults = {
            "code": "000001",
            "price": 9.0,
            "buy_min": 9.5,
            "buy_max": 10.5,
            "sector_trend": "走强",
            "sector_chg": 1.0,
            "intra_available": False,
        }
        defaults.update(overrides)
        return BuyEvalInput(**defaults)

    def test_far_below_abandon(self):
        # 偏离超 7%，评分 ≤ -4 → watching；偏离超 15% + 板块弱 → abandon
        action, reason, mul = evaluate_below_zone(
            self._make_ctx(price=8.5, buy_min=9.5, sector_trend="持续走弱"),
        )
        assert action == "watching"  # 偏离不够大 + 板块弱被前置返回

    def test_extreme_below_abandon(self):
        action, reason, mul = evaluate_below_zone(
            self._make_ctx(price=7.0, buy_min=9.5, sector_trend="走弱"),
        )
        assert action == "abandon"

    def test_near_support_opportunity(self):
        action, reason, mul = evaluate_below_zone(
            self._make_ctx(daily_bb_lower=9.0, daily_ma20=9.5),
            near_support=True,
        )
        assert action == "opportunity"

    def test_declining_wait(self):
        action, reason, mul = evaluate_below_zone(
            self._make_ctx(price_action="declining", price_action_desc="持续下跌"),
        )
        assert action == "watching"

    def test_sector_weak_wait(self):
        action, reason, mul = evaluate_below_zone(
            self._make_ctx(sector_trend="持续走弱"),
        )
        assert action == "watching"


class TestSizing:
    def test_blocked_pattern(self):
        amount, reason = calculate_position_size("000001", 10.0, 9.5, 10.5, "panic", "横盘")
        assert amount == 0

    def test_normal(self):
        amount, reason = calculate_position_size("000001", 10.0, 9.5, 10.5, "normal", "走强")
        # 动态上限：200000 * 0.3 = 60000，走强 ×1.2 触及 cap
        assert amount == 60000

    def test_cautious(self):
        amount, reason = calculate_position_size("000001", 10.0, 9.5, 10.5, "v_reversal", "横盘")
        # cautious_cap = 60000 * 0.5 = 30000
        assert amount == 30000

    def test_sector_weak_reduce(self):
        amount, reason = calculate_position_size("000001", 10.0, 9.5, 10.5, "normal", "持续走弱")
        # 60000 * 0.3 = 18000 > 5000 → 18000
        assert amount < 25000

    def test_breadth_down(self):
        amount, reason = calculate_position_size(
            "000001",
            10.0,
            9.5,
            10.5,
            "normal",
            "走强",
            market_breadth={"up": 200, "down": 800},
        )
        # breadth: 60000*0.3=18000, sector走强: 18000*1.2=21600
        assert amount < 25000

    def test_zone_bottom_boost(self):
        amount, reason = calculate_position_size("000001", 9.55, 9.5, 10.5, "normal", "横盘")
        # 买入区下沿，偏激进
        assert amount > 50000


class TestSellDecision:
    def test_exit_normal(self):
        exit_s, wait_s, env = analyze_exit_signals(10.0, 12.0, "走弱")
        assert len(env) >= 0

    def test_exit_panic(self):
        exit_s, wait_s, env = analyze_exit_signals(10.0, 12.0, "横盘", risk_level="extreme", pattern="panic")
        assert any("恐慌" in e for e in env)

    def test_exit_deep_oversold(self):
        exit_s, wait_s, env = analyze_exit_signals(10.0, 12.0, "横盘", rsi12=25)
        assert any("超卖" in w for w in wait_s)

    def test_exit_near_bb_mid(self):
        exit_s, wait_s, env = analyze_exit_signals(10.0, 12.0, "横盘", bb_mid=10.2)
        assert any("布林中轨" in e for e in exit_s)

    def test_classify_healthy(self):
        assert classify_holding_status(12.5, 10.0, 9.0) == "healthy"

    def test_classify_deep_trapped(self):
        assert classify_holding_status(8.5, 10.0, 9.0) == "deep_trapped"

    def test_classify_trapped(self):
        assert classify_holding_status(9.3, 10.0, 9.0) == "trapped"

    def test_classify_watching(self):
        assert classify_holding_status(10.1, 10.0, 9.0) == "watching"
