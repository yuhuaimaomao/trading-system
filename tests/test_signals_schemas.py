"""测试信号与状态数据模型 — dataclass 构造、默认值、序列化。"""

from stock.signals import (
    AccountSummary,
    HoldingInfo,
    HoldingReview,
    OrderSignal,
    ReviewContext,
    SignalSource,
    SignalType,
    StockProfile,
    StockScore,
    StrategyAiDecision,
    StrategyAiResult,
)
from stock.stock_schemas import (
    AnalysisResult,
    StockAnalysisReport,
    StockAnalysisRequest,
)
from trade.core.scan_state import (
    MarketOutlook,
    MarketRegime,
    MarketScenario,
    MicroSignals,
    ScanState,
)

# ═══════════════════════════════════════════════════════════════
# SignalType / SignalSource 枚举
# ═══════════════════════════════════════════════════════════════


class TestSignalTypeEnum:
    def test_values(self):
        assert SignalType.BUY.value == 1
        assert SignalType.SELL.value == 2
        assert SignalType.HOLD.value == 3

    def test_members(self):
        assert set(SignalType.__members__) == {"BUY", "SELL", "HOLD"}


class TestSignalSourceEnum:
    def test_values(self):
        assert SignalSource.RULE.value == 1
        assert SignalSource.AI_ENHANCED.value == 2
        assert SignalSource.RISK.value == 3
        assert SignalSource.REVIEW.value == 4

    def test_members(self):
        assert set(SignalSource.__members__) == {
            "RULE",
            "AI_ENHANCED",
            "RISK",
            "REVIEW",
        }


# ═══════════════════════════════════════════════════════════════
# OrderSignal
# ═══════════════════════════════════════════════════════════════


class TestOrderSignal:
    def test_construct_minimal(self):
        sig = OrderSignal("000001", "平安银行", SignalType.BUY, SignalSource.RULE)
        assert sig.stock_code == "000001"
        assert sig.stock_name == "平安银行"
        assert sig.signal_type == SignalType.BUY
        assert sig.source == SignalSource.RULE
        # 默认值
        assert sig.timestamp == ""
        assert sig.buy_zone_min is None
        assert sig.buy_zone_max is None
        assert sig.target_position is None
        assert sig.sell_reason == ""
        assert sig.stop_loss is None
        assert sig.take_profit is None
        assert sig.trailing_stop is None
        assert sig.strategy_name == ""
        assert sig.signal_score == 0.0
        assert sig.reason == ""
        assert sig.expected_trend == ""
        assert sig.trend_mode == ""
        assert sig.sector_name == ""

    def test_construct_full(self):
        sig = OrderSignal(
            stock_code="002371",
            stock_name="北方华创",
            signal_type=SignalType.BUY,
            source=SignalSource.AI_ENHANCED,
            timestamp="2026-06-06T09:30:00",
            buy_zone_min=380.0,
            buy_zone_max=400.0,
            target_position=0.3,
            stop_loss=370.0,
            take_profit=440.0,
            trailing_stop=0.05,
            strategy_name="trend_follow_v2",
            signal_score=82.5,
            reason="放量突破MA5且主力持续流入",
            expected_trend="短期震荡上行",
            trend_mode="strong",
            sector_name="半导体",
        )
        assert sig.stock_code == "002371"
        assert sig.signal_type == SignalType.BUY
        assert sig.source == SignalSource.AI_ENHANCED
        assert sig.timestamp == "2026-06-06T09:30:00"
        assert sig.buy_zone_min == 380.0
        assert sig.buy_zone_max == 400.0
        assert sig.target_position == 0.3
        assert sig.stop_loss == 370.0
        assert sig.take_profit == 440.0
        assert sig.trailing_stop == 0.05
        assert sig.strategy_name == "trend_follow_v2"
        assert sig.signal_score == 82.5
        assert sig.reason == "放量突破MA5且主力持续流入"
        assert sig.expected_trend == "短期震荡上行"
        assert sig.trend_mode == "strong"
        assert sig.sector_name == "半导体"

    def test_sell_signal_defaults(self):
        """SELL 信号的 buy 字段应保持默认 None。"""
        sig = OrderSignal("000001", "平安银行", SignalType.SELL, SignalSource.RISK)
        assert sig.signal_type == SignalType.SELL
        assert sig.source == SignalSource.RISK
        assert sig.buy_zone_min is None
        assert sig.buy_zone_max is None
        assert sig.target_position is None
        assert sig.sell_reason == ""

    def test_to_dict(self):
        sig = OrderSignal(
            stock_code="002371",
            stock_name="北方华创",
            signal_type=SignalType.BUY,
            source=SignalSource.AI_ENHANCED,
            buy_zone_min=380.0,
            buy_zone_max=400.0,
            target_position=0.3,
            stop_loss=370.0,
            take_profit=440.0,
            trailing_stop=0.05,
            signal_score=82.5,
            strategy_name="trend_follow_v2",
            reason="突破确认",
            expected_trend="上行",
            trend_mode="strong",
            sector_name="半导体",
        )
        d = sig.to_dict()
        assert d["stock_code"] == "002371"
        assert d["signal_type"] == "BUY"
        assert d["source"] == "AI_ENHANCED"
        assert d["buy_zone_min"] == 380.0
        assert d["trailing_stop"] == 0.05
        assert d["signal_score"] == 82.5
        assert d["trend_mode"] == "strong"
        assert d["sector_name"] == "半导体"

    def test_to_dict_roundtrip(self):
        """通过 to_dict() 序列化后可用 dict 重建 OrderSignal。"""
        sig = OrderSignal(
            stock_code="600519",
            stock_name="贵州茅台",
            signal_type=SignalType.BUY,
            source=SignalSource.REVIEW,
            buy_zone_min=1800.0,
            buy_zone_max=1850.0,
            target_position=0.2,
            stop_loss=1750.0,
            take_profit=2000.0,
        )
        d = sig.to_dict()
        restored = OrderSignal(
            stock_code=d["stock_code"],
            stock_name=d["stock_name"],
            signal_type=SignalType[d["signal_type"]],
            source=SignalSource[d["source"]],
            buy_zone_min=d["buy_zone_min"],
            buy_zone_max=d["buy_zone_max"],
            target_position=d["target_position"],
            stop_loss=d["stop_loss"],
            take_profit=d["take_profit"],
            trailing_stop=d["trailing_stop"],
            signal_score=d["signal_score"],
            strategy_name=d["strategy_name"],
            reason=d["reason"],
            expected_trend=d["expected_trend"],
            trend_mode=d["trend_mode"],
            sector_name=d["sector_name"],
        )
        assert restored.stock_code == sig.stock_code
        assert restored.signal_type == sig.signal_type
        assert restored.source == sig.source
        assert restored.buy_zone_min == sig.buy_zone_min
        assert restored.stop_loss == sig.stop_loss

    def test_repr_buy(self):
        sig = OrderSignal(
            "002371",
            "北方华创",
            SignalType.BUY,
            SignalSource.AI_ENHANCED,
            buy_zone_min=380.0,
            buy_zone_max=400.0,
            target_position=0.3,
            stop_loss=370.0,
            trend_mode="strong",
        )
        r = repr(sig)
        assert r.startswith("BUY")
        assert "002371" in r
        assert "北方华创" in r
        assert "380.00-400.00" in r
        assert "30%" in r or "0%" in r  # target_position=0.3 → 30%

    def test_repr_sell(self):
        sig = OrderSignal(
            "002371",
            "北方华创",
            SignalType.SELL,
            SignalSource.RISK,
            sell_reason="跌破止损位",
        )
        r = repr(sig)
        assert r.startswith("SELL")
        assert "跌破止损位" in r

    def test_repr_hold(self):
        sig = OrderSignal("002371", "北方华创", SignalType.HOLD, SignalSource.RULE)
        r = repr(sig)
        assert r.startswith("HOLD")
        assert "002371" in r

    def test_repr_with_sector(self):
        sig = OrderSignal(
            "002371",
            "北方华创",
            SignalType.BUY,
            SignalSource.AI_ENHANCED,
            buy_zone_min=380,
            buy_zone_max=400,
            target_position=0.3,
            stop_loss=370,
            sector_name="半导体",
        )
        r = repr(sig)
        assert "[半导体]" in r


# ═══════════════════════════════════════════════════════════════
# HoldingInfo
# ═══════════════════════════════════════════════════════════════


class TestHoldingInfo:
    def test_construct_required(self):
        h = HoldingInfo(
            stock_code="002371",
            stock_name="北方华创",
            account="paper",
            entry_date="2026-05-20",
            holding_days=17,
            avg_cost=390.0,
            volume=1000,
            current_price=410.0,
            pnl_pct=5.13,
            market_value=410000.0,
            stop_loss=370.0,
            take_profit=440.0,
        )
        assert h.stock_code == "002371"
        assert h.account == "paper"
        assert h.holding_days == 17
        assert h.avg_cost == 390.0
        assert h.volume == 1000
        assert h.current_price == 410.0
        assert h.pnl_pct == 5.13

    def test_defaults(self):
        h = HoldingInfo(
            stock_code="600519",
            stock_name="贵州茅台",
            account="real",
            entry_date="2026-06-01",
            holding_days=5,
            avg_cost=1850.0,
            volume=200,
            current_price=1870.0,
            pnl_pct=1.08,
            market_value=374000.0,
            stop_loss=1800.0,
            take_profit=2000.0,
        )
        assert h.industry == ""
        assert h.ma5 == 0
        assert h.ma10 == 0
        assert h.ma20 == 0
        assert h.highest_price == 0
        assert h.signal_score == 0
        assert h.is_today_buy is False
        assert h.profile is None

    def test_profile_optional(self):
        """profile 接受 StockProfile。"""
        profile = StockProfile(code="600519", name="贵州茅台", trade_date="2026-06-06")
        h = HoldingInfo(
            stock_code="600519",
            stock_name="贵州茅台",
            account="real",
            entry_date="2026-06-01",
            holding_days=5,
            avg_cost=1850.0,
            volume=200,
            current_price=1870.0,
            pnl_pct=1.08,
            market_value=374000.0,
            stop_loss=1800.0,
            take_profit=2000.0,
            profile=profile,
        )
        assert h.profile is not None
        assert h.profile.code == "600519"
        assert h.profile.trade_date == "2026-06-06"

    def test_is_today_buy(self):
        h = HoldingInfo(
            stock_code="601318",
            stock_name="中国平安",
            account="paper",
            entry_date="2026-06-06",
            holding_days=0,
            avg_cost=48.0,
            volume=5000,
            current_price=48.5,
            pnl_pct=1.04,
            market_value=242500.0,
            stop_loss=46.0,
            take_profit=52.0,
            is_today_buy=True,
        )
        assert h.is_today_buy is True


# ═══════════════════════════════════════════════════════════════
# AccountSummary
# ═══════════════════════════════════════════════════════════════


class TestAccountSummary:
    def test_construct(self):
        a = AccountSummary(
            account="paper",
            label="模拟盘",
            initial_capital=1000000.0,
            total_value=1050000.0,
            cash=300000.0,
            market_value=750000.0,
            position_ratio=0.714,
            daily_pnl=5000.0,
            position_count=3,
        )
        assert a.account == "paper"
        assert a.label == "模拟盘"
        assert a.initial_capital == 1000000.0
        assert a.total_value == 1050000.0
        assert a.cash == 300000.0
        assert a.market_value == 750000.0
        assert a.position_ratio == 0.714
        assert a.daily_pnl == 5000.0
        assert a.position_count == 3


# ═══════════════════════════════════════════════════════════════
# StockProfile
# ═══════════════════════════════════════════════════════════════


class TestStockProfile:
    def test_construct_minimal(self):
        p = StockProfile(code="600519", name="贵州茅台", trade_date="2026-06-06")
        assert p.code == "600519"
        assert p.name == "贵州茅台"
        assert p.trade_date == "2026-06-06"
        # 默认值
        assert p.score == 0.0
        assert p.trend_mode == ""
        assert p.scenarios == []
        assert p.tags == []
        assert p.snapshot == {}
        assert p.history == {}
        assert p.rps == {}
        assert p.sectors == []
        assert p.sector_resonance == {}
        assert p.valuation == {}
        assert p.market_state == ""
        assert p.telegraphs == []
        assert p.indicators == {}
        assert p.risks == []
        assert p.legacy_note == ""

    def test_list_defaults_are_independent(self):
        """每个实例的 list 字段应相互独立。"""
        p1 = StockProfile(code="000001", name="平安银行", trade_date="2026-06-06")
        p2 = StockProfile(code="002371", name="北方华创", trade_date="2026-06-06")
        p1.tags.append("放量")
        p1.scenarios.append("主升浪")
        assert len(p1.tags) == 1
        assert len(p2.tags) == 0
        assert len(p1.scenarios) == 1
        assert len(p2.scenarios) == 0

    def test_to_text_basic(self):
        p = StockProfile(code="600519", name="贵州茅台", trade_date="2026-06-06")
        text = p.to_text()
        assert "600519" in text
        assert "贵州茅台" in text
        assert "无" in text  # 没有场景时显示"无"

    def test_to_text_with_snapshot(self):
        p = StockProfile(
            code="002371",
            name="北方华创",
            trade_date="2026-06-06",
            snapshot={
                "price": 410.0,
                "change_pct": 2.35,
                "amplitude": 3.12,
                "volume_ratio": 1.5,
                "turnover_rate": 2.8,
                "main_force_net": 50000000.0,  # 5000万
                "main_force_ratio": 0.15,
                "open": 400.0,
                "high": 415.0,
                "low": 398.0,
                "industry": "半导体",
            },
            history={
                "ma5": 405.0,
                "ma10": 398.0,
                "ma20": 390.0,
            },
        )
        text = p.to_text()
        assert "410.00" in text
        assert "+2.35%" in text
        assert "MA5:" in text
        assert "半导体" in text

    def test_legacy_note(self):
        p = StockProfile(
            code="002371",
            name="北方华创",
            trade_date="2026-06-06",
            legacy_note="昨日突破箱体",
        )
        text = p.to_text()
        assert "昨日遗留推荐" in text
        assert "昨日突破箱体" in text


# ═══════════════════════════════════════════════════════════════
# StrategyAiDecision / StrategyAiResult
# ═══════════════════════════════════════════════════════════════


class TestStrategyAiDecision:
    def test_construct_minimal(self):
        d = StrategyAiDecision(
            stock_code="002371",
            stock_name="北方华创",
            rank_in_prompt=1,
            verdict="buy",
        )
        assert d.stock_code == "002371"
        assert d.rank_in_prompt == 1
        assert d.verdict == "buy"
        # 默认值
        assert d.confidence == ""
        assert d.what_i_see == ""
        assert d.what_concerns_me == ""
        assert d.decisive_factor == ""
        assert d.skip_reason == ""
        assert d.would_reconsider_if == ""
        assert d.buy_zone_min is None
        assert d.buy_zone_max is None
        assert d.stop_loss is None
        assert d.take_profit is None
        assert d.pricing_logic == ""
        assert d.signal_id is None
        assert d.day_change_pct is None
        assert d.day_pnl_pct is None

    def test_construct_buy_full(self):
        d = StrategyAiDecision(
            stock_code="002371",
            stock_name="北方华创",
            rank_in_prompt=1,
            verdict="buy",
            confidence="high",
            what_i_see="放量突破MA5",
            what_concerns_me="RSI偏高",
            decisive_factor="主力持续流入",
            buy_zone_min=385.0,
            buy_zone_max=400.0,
            stop_loss=375.0,
            take_profit=440.0,
            pricing_logic="前高440为压力位",
            signal_id=42,
            day_change_pct=2.35,
            day_pnl_pct=5.13,
        )
        assert d.verdict == "buy"
        assert d.confidence == "high"
        assert d.buy_zone_min == 385.0
        assert d.signal_id == 42

    def test_construct_skip_full(self):
        d = StrategyAiDecision(
            stock_code="000001",
            stock_name="平安银行",
            rank_in_prompt=5,
            verdict="skip",
            skip_reason="量能不足",
            would_reconsider_if="放量突破MA10",
        )
        assert d.verdict == "skip"
        assert d.skip_reason == "量能不足"
        assert d.would_reconsider_if == "放量突破MA10"


class TestStrategyAiResult:
    def test_construct_minimal(self):
        r = StrategyAiResult(model_used="deepseek-v4-pro")
        assert r.model_used == "deepseek-v4-pro"
        assert r.decisions == []
        assert r.holdings_review == []
        assert r.self_assessment == ""
        assert r.raw_response == ""

    def test_with_decisions(self):
        decisions = [
            StrategyAiDecision("002371", "北方华创", 1, "buy", confidence="high"),
            StrategyAiDecision("000001", "平安银行", 2, "skip", skip_reason="量能不足"),
        ]
        r = StrategyAiResult(
            model_used="deepseek-v4-pro",
            decisions=decisions,
            self_assessment="今日选股偏谨慎",
            raw_response="...JSON...",
        )
        assert len(r.decisions) == 2
        assert r.decisions[0].stock_code == "002371"
        assert r.decisions[1].verdict == "skip"
        assert r.self_assessment == "今日选股偏谨慎"
        assert r.raw_response == "...JSON..."

    def test_holdings_review(self):
        reviews = [
            HoldingReview(stock_code="600519", action="hold", reason="趋势完好"),
            HoldingReview(stock_code="002371", action="close", reason="破位"),
        ]
        r = StrategyAiResult(
            model_used="deepseek-v4-pro",
            holdings_review=reviews,
        )
        assert len(r.holdings_review) == 2
        assert r.holdings_review[0].action == "hold"


# ═══════════════════════════════════════════════════════════════
# HoldingReview
# ═══════════════════════════════════════════════════════════════


class TestHoldingReview:
    def test_construct_minimal(self):
        hr = HoldingReview(stock_code="002371")
        assert hr.stock_code == "002371"
        assert hr.account == ""
        assert hr.action == ""
        assert hr.new_stop_loss is None
        assert hr.new_take_profit is None
        assert hr.expected_holding_days is None
        assert hr.tomorrow_outlook == ""
        assert hr.reason == ""

    def test_construct_full(self):
        hr = HoldingReview(
            stock_code="002371",
            account="paper",
            action="hold",
            new_stop_loss=370.0,
            new_take_profit=440.0,
            expected_holding_days=5,
            tomorrow_outlook="震荡上行",
            reason="趋势仍在",
        )
        assert hr.action == "hold"
        assert hr.new_stop_loss == 370.0
        assert hr.expected_holding_days == 5

    def test_to_summary(self):
        hr = HoldingReview(
            stock_code="002371",
            action="hold",
            new_stop_loss=370.0,
            reason="趋势完好",
        )
        s = hr.to_summary()
        assert "002371" in s
        assert "370.00" in s  # new_stop_loss formatted

    def test_to_summary_add(self):
        hr = HoldingReview(
            stock_code="002371",
            action="add",
            new_stop_loss=370.0,
            tomorrow_outlook="看涨",
            reason="放量突破",
        )
        s = hr.to_summary()
        assert "加仓" in s or "➕" in s
        assert "看涨" in s

    def test_to_summary_close(self):
        hr = HoldingReview(stock_code="600519", action="close", reason="跌破MA20")
        s = hr.to_summary()
        assert "清仓" in s or "🔴" in s
        assert "跌破MA20" in s


# ═══════════════════════════════════════════════════════════════
# ReviewContext
# ═══════════════════════════════════════════════════════════════


class TestReviewContext:
    def test_construct_defaults(self):
        rc = ReviewContext()
        assert rc.trade_date == ""
        assert rc.sentiment_cycle == ""
        assert rc.main_lines == ""
        assert rc.sub_lines == ""
        assert rc.retreating_sectors == ""
        assert rc.outlook == ""
        assert rc.review_picks == []
        assert rc.review_stocks_raw == []
        assert rc.monitor_conditions == ""
        assert rc.suggested_position == 0.0
        assert rc.position_cap == 0.0
        assert rc.main_attack == ""
        assert rc.avoid_direction == ""

    def test_construct_full(self):
        rc = ReviewContext(
            trade_date="2026-06-05",
            sentiment_cycle="退潮期",
            main_lines="人工智能",
            sub_lines="半导体",
            retreating_sectors="新能源",
            outlook="震荡偏弱",
            review_picks=["002371", "600519"],
            review_stocks_raw=[{"code": "002371", "name": "北方华创"}],
            monitor_conditions="观察量能",
            suggested_position=0.3,
            position_cap=0.6,
            main_attack="AI算力",
            avoid_direction="地产链",
        )
        assert rc.trade_date == "2026-06-05"
        assert rc.sentiment_cycle == "退潮期"
        assert len(rc.review_picks) == 2
        assert rc.suggested_position == 0.3
        assert rc.position_cap == 0.6

    def test_to_text_empty(self):
        rc = ReviewContext()
        assert rc.to_text() == ""

    def test_to_text_full(self):
        rc = ReviewContext(
            sentiment_cycle="震荡期",
            main_lines="AI算力",
            sub_lines="半导体",
            retreating_sectors="消费",
            outlook="偏多",
            review_picks=["002371"],
            monitor_conditions="观察量能是否放大",
            suggested_position=0.5,
            position_cap=0.7,
            main_attack="科技",
            avoid_direction="消费",
        )
        text = rc.to_text()
        assert "震荡期" in text
        assert "AI算力" in text
        assert "002371" in text
        assert "50%" in text or "0.5" in text or "0.50" in text
        assert "科技" in text
        assert "消费" in text


# ═══════════════════════════════════════════════════════════════
# StockScore
# ═══════════════════════════════════════════════════════════════


class TestStockScore:
    def test_construct_minimal(self):
        s = StockScore(
            stock_code="002371",
            stock_name="北方华创",
            trend_mode="strong",
            score=85.0,
            price=410.0,
            change_pct=2.35,
            mcap=2000.0,
            circ_mcap=1500.0,
            turnover_rate=3.2,
            volume_ratio=1.5,
            ma5=405.0,
            ma10=398.0,
            ma20=390.0,
            ma5_angle=25.0,
            industry="半导体",
            mf_wan=5000.0,
            mf_ratio=0.15,
        )
        assert s.stock_code == "002371"
        assert s.trend_mode == "strong"
        assert s.score == 85.0
        assert s.ma5 == 405.0
        assert s.ma5_angle == 25.0
        assert s.industry == "半导体"

    def test_defaults(self):
        s = StockScore(
            stock_code="002371",
            stock_name="北方华创",
            trend_mode="normal",
            score=70.0,
            price=400.0,
            change_pct=1.2,
            mcap=2000.0,
            circ_mcap=1500.0,
            turnover_rate=2.0,
            volume_ratio=1.0,
            ma5=395.0,
            ma10=392.0,
            ma20=388.0,
            ma5_angle=10.0,
            industry="半导体",
            mf_wan=3000.0,
            mf_ratio=0.1,
        )
        assert s.bias_ma5 == 0.0
        assert s.bias_ma20 == 0.0
        assert s.tags == []
        assert s.scenarios == []

    def test_construct_full(self):
        s = StockScore(
            stock_code="002371",
            stock_name="北方华创",
            trend_mode="strong",
            score=85.0,
            price=410.0,
            change_pct=2.35,
            mcap=2000.0,
            circ_mcap=1500.0,
            turnover_rate=3.2,
            volume_ratio=1.5,
            ma5=405.0,
            ma10=398.0,
            ma20=390.0,
            ma5_angle=25.0,
            industry="半导体",
            mf_wan=5000.0,
            mf_ratio=0.15,
            bias_ma5=1.23,
            bias_ma20=5.13,
            tags=["放量突破", "主力流入"],
            scenarios=["主升浪"],
        )
        assert s.bias_ma5 == 1.23
        assert s.bias_ma20 == 5.13
        assert s.tags == ["放量突破", "主力流入"]
        assert s.scenarios == ["主升浪"]

    def test_list_defaults_independent(self):
        s1 = StockScore(
            stock_code="000001",
            stock_name="平安银行",
            trend_mode="strong",
            score=80,
            price=10,
            change_pct=1,
            mcap=100,
            circ_mcap=80,
            turnover_rate=1,
            volume_ratio=1,
            ma5=9.8,
            ma10=9.7,
            ma20=9.5,
            ma5_angle=5,
            industry="银行",
            mf_wan=100,
            mf_ratio=0.05,
        )
        s2 = StockScore(
            stock_code="002371",
            stock_name="北方华创",
            trend_mode="normal",
            score=75,
            price=400,
            change_pct=2,
            mcap=2000,
            circ_mcap=1500,
            turnover_rate=3,
            volume_ratio=1.2,
            ma5=395,
            ma10=390,
            ma20=385,
            ma5_angle=15,
            industry="半导体",
            mf_wan=5000,
            mf_ratio=0.15,
        )
        s1.tags.append("only_for_s1")
        assert len(s1.tags) == 1
        assert len(s2.tags) == 0


# ═══════════════════════════════════════════════════════════════
# MarketRegime
# ═══════════════════════════════════════════════════════════════


class TestMarketRegime:
    def test_defaults(self):
        r = MarketRegime()
        assert r.pattern == "normal"
        assert r.risk_level == "safe"
        assert r.risk_bias == "neutral"
        assert r.confidence == "medium"
        assert r.opportunity == "trend_follow"
        assert r.allow_buy is True
        assert r.position_mult == 1.0
        assert r.entry_rule == "standard"
        assert r.stop_mult == 1.0
        assert r.urgent_action == ""
        assert r.alert_level == "info"
        assert r.alert_msg == ""
        assert r.session_phase == "morning"
        assert r.gap_direction == ""
        assert r.breadth_healthy is True
        assert r.ma20_above is True
        assert r.multi_day_downtrend is False

    def test_dangerous_pattern(self):
        r = MarketRegime(
            pattern="sharp_decline",
            risk_level="danger",
            risk_bias="bearish",
            confidence="high",
            opportunity="defense",
            allow_buy=False,
            position_mult=0.0,
            entry_rule="none",
            stop_mult=0.8,
        )
        assert r.pattern == "sharp_decline"
        assert r.risk_level == "danger"
        assert r.allow_buy is False
        assert r.position_mult == 0.0
        assert r.entry_rule == "none"

    def test_pattern_valid_values(self):
        """pattern 字段应接受各种字符串，dataclass 无校验。"""
        for pat in ("normal", "volatile", "crash", "bull_run", "sharp_decline"):
            r = MarketRegime(pattern=pat)
            assert r.pattern == pat

    def test_confidence_values(self):
        for c in ("low", "medium", "high"):
            r = MarketRegime(confidence=c)
            assert r.confidence == c

    def test_allow_buy_values(self):
        r1 = MarketRegime(allow_buy=True)
        r2 = MarketRegime(allow_buy=False)
        assert r1.allow_buy is True
        assert r2.allow_buy is False


# ═══════════════════════════════════════════════════════════════
# MicroSignals
# ═══════════════════════════════════════════════════════════════


class TestMicroSignals:
    def test_defaults(self):
        m = MicroSignals()
        assert m.price_velocity == 0.0
        assert m.price_accel == 0.0
        assert m.ema12_pos == "on"
        assert m.ema12_just_crossed == ""
        assert m.vol_pulse == "normal"
        assert m.vol_price_confirm == "yes"
        assert m.breadth_pct == 0.5
        assert m.breadth_trend == "stable"
        assert m.higher_highs is False
        assert m.bounce_from_low == 0.0
        assert m.bounce_quality == ""
        assert m.lower_highs is False
        assert m.higher_lows is False
        assert m.rsi_signal == ""
        assert m.testing_support is False
        assert m.testing_resistance is False
        assert m.range_expanding is False
        assert m.range_contracting is False

    def test_construct_partial(self):
        m = MicroSignals(
            price_velocity=0.5,
            breadth_pct=0.6,
            higher_highs=True,
            rsi_signal="overbought",
        )
        assert m.price_velocity == 0.5
        assert m.breadth_pct == 0.6
        assert m.higher_highs is True
        assert m.rsi_signal == "overbought"
        # 其余保留默认
        assert m.price_accel == 0.0
        assert m.ema12_pos == "on"


# ═══════════════════════════════════════════════════════════════
# MarketScenario
# ═══════════════════════════════════════════════════════════════


class TestMarketScenario:
    def test_construct_minimal(self):
        s = MarketScenario(name="bull_breakout", label="多头突破")
        assert s.name == "bull_breakout"
        assert s.label == "多头突破"
        # 默认值
        assert s.probability == 0.0
        assert s.confidence == "low"
        assert s.direction == "neutral"
        assert s.confirm_at is None
        assert s.invalidate_at is None
        assert s.signals == []
        assert s.pre_action == ""

    def test_post_init_signals_default(self):
        """post_init 应确保 signals 为 list。"""
        s = MarketScenario(name="test", label="测试", signals=None)
        assert s.signals == []

    def test_construct_full(self):
        s = MarketScenario(
            name="bull_breakout",
            label="多头突破",
            probability=0.7,
            confidence="high",
            direction="up",
            confirm_at=4200.0,
            invalidate_at=4050.0,
            signals=[{"type": "volume_spike"}],
            pre_action="alert",
        )
        assert s.probability == 0.7
        assert s.confidence == "high"
        assert s.direction == "up"
        assert s.confirm_at == 4200.0
        assert s.invalidate_at == 4050.0
        assert len(s.signals) == 1
        assert s.pre_action == "alert"

    def test_union_float_none(self):
        """confirm_at / invalidate_at 可为 float 或 None。"""
        s = MarketScenario(name="test", label="测试")
        assert s.confirm_at is None
        assert s.invalidate_at is None


# ═══════════════════════════════════════════════════════════════
# MarketOutlook
# ═══════════════════════════════════════════════════════════════


class TestMarketOutlook:
    def test_construct_minimal(self):
        primary = MarketScenario(name="base", label="基线")
        o = MarketOutlook(
            primary=primary,
            alternatives=[],
            key_support=[],
            key_resistance=[],
        )
        assert o.primary.name == "base"
        assert o.alternatives == []
        assert o.key_support == []
        assert o.key_resistance == []
        assert o.bias == "neutral"
        assert o.urgency == "none"
        assert o.summary == ""
        assert o.last_alert_scan == 0

    def test_construct_full(self):
        primary = MarketScenario(name="bull", label="看涨", probability=0.6)
        alt = MarketScenario(name="bear", label="看跌", probability=0.3)
        o = MarketOutlook(
            primary=primary,
            alternatives=[alt],
            key_support=[4000.0, 3950.0],
            key_resistance=[4200.0, 4300.0],
            bias="bullish",
            urgency="watch",
            summary="市场偏强",
            last_alert_scan=42,
        )
        assert len(o.alternatives) == 1
        assert o.alternatives[0].name == "bear"
        assert o.key_support == [4000.0, 3950.0]
        assert o.key_resistance == [4200.0, 4300.0]
        assert o.bias == "bullish"
        assert o.urgency == "watch"
        assert o.summary == "市场偏强"
        assert o.last_alert_scan == 42


# ═══════════════════════════════════════════════════════════════
# ScanState
# ═══════════════════════════════════════════════════════════════


class TestScanState:
    """ScanState 字段极多 — 验证关键组默认值即可。"""

    def test_defaults_core(self):
        s = ScanState()
        assert s.running is False
        assert s.trade_date == ""
        assert s.scan_count == 0
        assert s.review_monitor is None
        assert s.sector_monitor is None
        assert s.receiver is None
        assert s.executor is None

    def test_defaults_index(self):
        s = ScanState()
        assert s.index_prices == []
        assert s.index_high == 0.0
        assert s.index_low == 0.0
        assert s.index_map == {}

    def test_defaults_market(self):
        s = ScanState()
        assert s.market_breadth == {"up": 0, "down": 0, "flat": 0, "total": 0}
        assert s.market_turnovers == []
        assert s.volume_alerted_divergence is False
        assert s.regime is None
        assert s.closing_decision_done is False

    def test_defaults_data(self):
        s = ScanState()
        assert s.data_ready is False
        assert s.data_ready_at == 0.0
        assert s.market_snapshot == {}
        assert s.last_index_quote is None

    def test_defaults_triggered_ids(self):
        s = ScanState()
        assert s.triggered_ids == set()
        assert s.alerted_sl_tp == set()
        assert s.alert_fingerprints == {}

    def test_defaults_trend_tracking(self):
        s = ScanState()
        # sector 和 concept 的 history 是 defaultdict(list)
        assert len(s.sector_trend_history) == 0
        assert len(s.concept_trend_history) == 0
        assert len(s.sector_trend_continuity) == 0
        assert len(s.sector_trend_last_dir) == 0
        assert len(s.sector_stats) == 0

    def test_defaults_pullback(self):
        s = ScanState()
        assert s.pullback_scan_count == 0
        assert s.pullback_alerted_today == set()

    def test_defaults_ai_async(self):
        s = ScanState()
        assert s.pending_chase == {}
        assert s.pending_index_ai == {}
        assert s.morning_sector_bias == {}

    def test_defaults_scenario(self):
        s = ScanState()
        assert s.scenario_probs == {}
        assert s.scenario_scan_count == 0
        assert s.scenario_prev_velocity == 0.0
        assert s.scenario_recent_lows == []
        assert s.scenario_recent_highs == []
        assert s.scenario_prev_outlook is None

    def test_defaults_cache(self):
        s = ScanState()
        assert s.ma_baseline_cache is None
        assert s.limit_cache == {}
        assert s.instrument_cache == {}

    def test_defaults_pos_tracking(self):
        s = ScanState()
        assert s.pos_meta == {}
        assert s.bought_watch == {}
        assert s.recent_prices == {}
        assert s.sl_reminders == {}
        assert s.recently_sold == {}

    def test_defaults_watch_codes(self):
        s = ScanState()
        assert s.cached_db_watch_codes == set()
        assert s.watch_codes_stale is True

    def test_defaults_regime_tracking(self):
        s = ScanState()
        assert s.regime is None
        assert s.regime_pending_pattern == ""
        assert s.regime_confirm_count == 0
        assert s.regime_switch_times == []

    def test_construct_with_values(self):
        s = ScanState(
            running=True,
            trade_date="2026-06-06",
            scan_count=10,
            regime=MarketRegime(pattern="normal"),
            data_ready=True,
        )
        assert s.running is True
        assert s.trade_date == "2026-06-06"
        assert s.scan_count == 10
        assert s.regime is not None
        assert s.regime.pattern == "normal"
        assert s.data_ready is True

    def test_index_tech_state_default(self):
        s = ScanState()
        expected = {
            "macd_cross": None,
            "rsi6_zone": "normal",
            "rsi12_zone": "normal",
            "kdj_cross": None,
            "kdj_j_zone": "normal",
            "divergence": None,
        }
        assert s.index_tech_state == expected


# ═══════════════════════════════════════════════════════════════
# Analysis schemas (from stock/schemas.py — light coverage)
# ═══════════════════════════════════════════════════════════════


class TestAnalysisSchemas:
    def test_stock_analysis_request(self):
        req = StockAnalysisRequest(
            symbol="600519", dimensions=["technical", "money_flow"]
        )
        assert req.symbol == "600519"
        assert req.dimensions == ["technical", "money_flow"]
        assert req.params == {}

    def test_analysis_result(self):
        r = AnalysisResult(
            dimension="technical",
            ok=True,
            data={"ma5": 405.0},
            conclusions=["均线多头排列"],
            risk_flags=["RSI超买"],
        )
        assert r.ok is True
        assert r.data["ma5"] == 405.0
        assert r.error == ""

    def test_analysis_result_with_error(self):
        r = AnalysisResult(
            dimension="technical",
            ok=False,
            data={},
            conclusions=[],
            risk_flags=[],
            error="数据获取失败",
        )
        assert r.ok is False
        assert r.error == "数据获取失败"

    def test_stock_analysis_report(self):
        results = [
            AnalysisResult("technical", True, {"ma5": 405}, ["多头"], []),
            AnalysisResult("money_flow", True, {"net": 5000}, ["流入"], []),
        ]
        report = StockAnalysisReport(
            symbol="600519",
            name="贵州茅台",
            results=results,
            aggregated={"ok": True, "total_risks": 0},
        )
        assert report.symbol == "600519"
        assert len(report.results) == 2
        assert report.aggregated["ok"] is True


# ═══════════════════════════════════════════════════════════════
# Edge cases
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_ordersignal_empty_strings(self):
        """空字符串字段应正确存储。"""
        sig = OrderSignal("", "", SignalType.HOLD, SignalSource.RULE)
        assert sig.stock_code == ""
        assert sig.stock_name == ""

    def test_stockprofile_no_snapshot_to_text(self):
        """空 profile 调用 to_text 不崩溃。"""
        p = StockProfile(code="", name="", trade_date="")
        # 不应引发异常
        text = p.to_text()
        assert isinstance(text, str)

    def test_reviewcontext_no_sections(self):
        """无任何节时 to_text 返回空字符串。"""
        rc = ReviewContext()
        assert rc.to_text() == ""

    def test_holdings_review_none_optionals(self):
        """可选数值字段显式设置为 None。"""
        hr = HoldingReview(
            stock_code="002371",
            action="hold",
            new_stop_loss=None,
            new_take_profit=None,
            expected_holding_days=None,
        )
        assert hr.new_stop_loss is None
        assert hr.new_take_profit is None

    def test_strategy_ai_decision_none_floats(self):
        """buy_zone_min/stop_loss/take_profit 可设为 None。"""
        d = StrategyAiDecision("000001", "平安银行", 1, "buy")
        assert d.buy_zone_min is None
        assert d.stop_loss is None

    def test_marketregime_all_fields_set(self):
        """所有字段都设置非默认值。"""
        r = MarketRegime(
            pattern="crash",
            risk_level="extreme",
            risk_bias="bearish",
            confidence="high",
            opportunity="defense",
            allow_buy=False,
            position_mult=0.0,
            entry_rule="none",
            stop_mult=0.5,
            urgent_action="reduce_position",
            alert_level="critical",
            alert_msg="日内暴跌",
            session_phase="afternoon",
            gap_direction="down",
            breadth_healthy=False,
            ma20_above=False,
            multi_day_downtrend=True,
        )
        assert r.pattern == "crash"
        assert r.alert_msg == "日内暴跌"
        assert r.multi_day_downtrend is True

    def test_microsignals_all_stress_values(self):
        m = MicroSignals(
            price_velocity=-2.5,
            price_accel=-0.8,
            ema12_pos="off",
            ema12_just_crossed="down",
            vol_pulse="surge",
            vol_price_confirm="no",
            breadth_pct=0.2,
            breadth_trend="falling",
            higher_highs=True,
            bounce_from_low=1.5,
            bounce_quality="strong",
            lower_highs=True,
            higher_lows=True,
            rsi_signal="oversold",
            testing_support=True,
            testing_resistance=False,
            range_expanding=True,
            range_contracting=False,
        )
        assert m.price_velocity == -2.5
        assert m.ema12_pos == "off"
        assert m.vol_pulse == "surge"
        assert m.rsi_signal == "oversold"
        assert m.testing_support is True

    def test_scanstate_triggered_ids_collection(self):
        s = ScanState()
        s.triggered_ids.add(1)
        s.triggered_ids.add(2)
        s.triggered_ids.add(1)  # 去重
        assert s.triggered_ids == {1, 2}

    def test_scanstate_market_breadth_struct(self):
        s = ScanState()
        s.market_breadth["up"] = 1500
        s.market_breadth["down"] = 500
        assert s.market_breadth["up"] == 1500
        assert s.market_breadth["total"] == 0  # 未被修改

    def test_scenario_probs_mutation(self):
        s = ScanState()
        s.scenario_probs["bull"] = 0.6
        s.scenario_probs["bear"] = 0.3
        assert s.scenario_probs["bull"] == 0.6
