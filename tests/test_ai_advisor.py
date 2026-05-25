# -*- coding: utf-8 -*-
"""AIAdvisor 单元测试"""

import json
from unittest.mock import MagicMock, patch

import pytest

from analysis.advisor import AIAdvisor
from analysis.signals import StockScore, OrderSignal, SignalType, SignalSource

# =====================  Fixtures  =====================


@pytest.fixture
def mock_stock_scores():
    """一组模拟的 StockScore 对象。"""
    return [
        StockScore(
            stock_code="000001",
            stock_name="平安银行",
            trend_mode="strong",
            score=75.0,
            price=12.50,
            change_pct=3.2,
            mcap=500.0,
            circ_mcap=300.0,
            turnover_rate=4.5,
            volume_ratio=1.3,
            ma5=12.10,
            ma10=11.60,
            ma20=11.00,
            ma5_angle=6.5,
            industry="银行",
            mf_wan=8000,
            mf_ratio=0.08,
            bias_ma5=3.31,
            bias_ma20=13.64,
        ),
        StockScore(
            stock_code="000002",
            stock_name="万科A",
            trend_mode="normal",
            score=68.0,
            price=15.80,
            change_pct=1.5,
            mcap=800.0,
            circ_mcap=600.0,
            turnover_rate=3.2,
            volume_ratio=0.9,
            ma5=15.50,
            ma10=15.70,
            ma20=15.20,
            ma5_angle=2.1,
            industry="地产",
            mf_wan=3000,
            mf_ratio=0.03,
            bias_ma5=0.00,
            bias_ma20=3.95,
        ),
    ]


@pytest.fixture
def mock_qwen_response():
    """模拟千问模型返回的 JSON。"""
    return """
```json
{
  "stocks": [
    {
      "stock_code": "000001",
      "stock_name": "平安银行",
      "action": "buy",
      "confidence": 80,
      "buy_zone_min": 12.30,
      "buy_zone_max": 12.60,
      "stop_loss": 11.80,
      "take_profit": 14.00,
      "reason": "MA多头排列，主力流入，趋势强势",
      "key_risk": "板块轮动风险"
    },
    {
      "stock_code": "000002",
      "stock_name": "万科A",
      "action": "skip",
      "confidence": 45,
      "reason": "趋势不清晰，量能不足",
      "key_risk": "无明显支撑"
    }
  ]
}
```
"""


@pytest.fixture
def mock_deepseek_response():
    """模拟 DeepSeek 模型返回的 JSON。"""
    return """
```json
{
  "stocks": [
    {
      "stock_code": "000001",
      "stock_name": "平安银行",
      "action": "buy",
      "confidence": 75,
      "buy_zone_min": 12.40,
      "buy_zone_max": 12.70,
      "stop_loss": 11.90,
      "take_profit": 13.50,
      "reason": "均线发散良好，主力净流入",
      "key_risk": "短期涨幅较大"
    },
    {
      "stock_code": "000002",
      "stock_name": "万科A",
      "action": "buy",
      "confidence": 55,
      "buy_zone_min": 15.60,
      "buy_zone_max": 15.90,
      "stop_loss": 15.00,
      "take_profit": 17.00,
      "reason": "稳健趋势，回调低吸机会",
      "key_risk": "地产政策风险"
    }
  ]
}
```
"""


@pytest.fixture
def mock_analyzer_qwen():
    """返回一个 mock AIAnalyzer（qwen 模式）。"""
    analyzer = MagicMock()
    analyzer.model = "qwen3.6-plus"
    return analyzer


@pytest.fixture
def mock_analyzer_deepseek():
    """返回一个 mock AIAnalyzer（deepseek 模式）。"""
    analyzer = MagicMock()
    analyzer.model = "deepseek-chat"
    return analyzer


# =====================  Tests: Instantiation  =====================


class TestAIAdvisorInstantiation:
    """测试 AIAdvisor 实例化。"""

    @patch("analysis.advisor.AIAdvisor._create_analyzer")
    def test_default_creates_both(self, mock_create):
        """默认模式应该创建千问和 DeepSeek 两个分析器。"""
        mock_create.return_value = MagicMock()
        advisor = AIAdvisor()
        assert advisor._analyzers is not None

    @patch("analysis.advisor.AIAdvisor._create_analyzer")
    def test_qwen_only(self, mock_create):
        """指定 qwen 时只创建千问分析器。"""
        mock_create.return_value = MagicMock()
        advisor = AIAdvisor(model="qwen")
        assert len(advisor._analyzers) == 1
        assert advisor._analyzers[0][0] == "qwen"

    @patch("analysis.advisor.AIAdvisor._create_analyzer")
    def test_deepseek_only(self, mock_create):
        """指定 deepseek 时只创建 DeepSeek 分析器。"""
        mock_create.return_value = MagicMock()
        advisor = AIAdvisor(model="deepseek")
        assert len(advisor._analyzers) == 1
        assert advisor._analyzers[0][0] == "deepseek"

    def test_no_analyzers_when_all_fail(self):
        """如果所有分析器创建失败，_analyzers 应为空列表。"""
        with patch.object(AIAdvisor, "_create_analyzer", return_value=None):
            advisor = AIAdvisor()
            assert advisor._analyzers == []


# =====================  Tests: Prompt Building  =====================


class TestPromptBuilding:
    """测试 Prompt 构建逻辑。"""

    def test_build_prompt_contains_stock_data(self, mock_stock_scores):
        """测试 prompt 中包含股票数据。"""
        prompt = AIAdvisor._build_prompt(mock_stock_scores, "2025-01-15")

        # 应包含头部信息
        assert "交易日期: 2025-01-15" in prompt

        # 应包含两只股票的代码
        assert "000001" in prompt
        assert "平安银行" in prompt
        assert "000002" in prompt
        assert "万科A" in prompt

        # 应包含关键数据点
        assert "MA5: 12.10" in prompt
        assert "MA10: 11.60" in prompt
        assert "MA20: 11.00" in prompt
        assert "主力净流入: 8000万" in prompt
        assert "强趋势" in prompt
        assert "稳健趋势" in prompt

    def test_build_prompt_without_trade_date(self, mock_stock_scores):
        """不传 trade_date 时不应抛出异常。"""
        prompt = AIAdvisor._build_prompt(mock_stock_scores)
        assert "000001" in prompt

    def test_build_prompt_empty_candidates(self):
        """空候选列表应返回基本 prompt。"""
        prompt = AIAdvisor._build_prompt([])
        assert "候选股票池" in prompt


# =====================  Tests: Response Parsing  =====================


class TestResponseParsing:
    """测试 JSON 响应解析。"""

    def test_parse_valid_json(self, mock_qwen_response):
        """解析有效的 JSON 响应应返回 OrderSignal 列表。"""
        signals = AIAdvisor._parse_json_response(mock_qwen_response, "qwen")
        assert signals is not None
        assert len(signals) == 1  # 只有 action=buy 的股票
        assert signals[0].stock_code == "000001"
        assert signals[0].stock_name == "平安银行"
        assert signals[0].signal_type == SignalType.BUY
        assert signals[0].source == SignalSource.AI_ENHANCED
        assert signals[0].signal_score == 80
        assert signals[0].buy_zone_min == 12.30
        assert signals[0].buy_zone_max == 12.60
        assert signals[0].stop_loss == 11.80
        assert signals[0].take_profit == 14.00
        assert signals[0].strategy_name == "ai_advisor_qwen"

    def test_parse_without_codeblock(self):
        """解析没有 ```json 包裹的 JSON。"""
        text = '{"stocks": [{"stock_code": "000001", "stock_name": "A", "action": "buy", "confidence": 70, "buy_zone_min": 10, "buy_zone_max": 11, "stop_loss": 9, "take_profit": 13, "reason": "test"}]}'
        signals = AIAdvisor._parse_json_response(text, "test")
        assert signals is not None
        assert len(signals) == 1

    def test_parse_skip_action_excluded(self):
        """action=skip 的股票应被排除。"""
        text = """```json
{
  "stocks": [
    {
      "stock_code": "000001",
      "stock_name": "A",
      "action": "skip",
      "confidence": 30
    },
    {
      "stock_code": "000002",
      "stock_name": "B",
      "action": "buy",
      "confidence": 80,
      "buy_zone_min": 10,
      "buy_zone_max": 11,
      "stop_loss": 9,
      "take_profit": 13,
      "reason": "good"
    }
  ]
}
```
"""
        signals = AIAdvisor._parse_json_response(text, "test")
        assert signals is not None
        assert len(signals) == 1
        assert signals[0].stock_code == "000002"

    def test_parse_invalid_json_returns_none(self):
        """无效 JSON 应返回 None。"""
        text = "这不是 JSON"
        signals = AIAdvisor._parse_json_response(text, "test")
        assert signals is None

    def test_parse_empty_stocks(self):
        """stocks 为空列表时返回 None。"""
        text = '{"stocks": []}'
        signals = AIAdvisor._parse_json_response(text, "test")
        assert signals is None

    def test_parse_low_confidence_skipped(self):
        """confidence <= 0 应被跳过。"""
        text = """```json
{
  "stocks": [
    {
      "stock_code": "000001",
      "stock_name": "A",
      "action": "buy",
      "confidence": 0,
      "buy_zone_min": 10,
      "buy_zone_max": 11,
      "stop_loss": 9,
      "take_profit": 13,
      "reason": "good"
    }
  ]
}
```
"""
        signals = AIAdvisor._parse_json_response(text, "test")
        assert signals is None or len(signals) == 0

    def test_parse_partial_failure_continues(self):
        """部分股票解析失败不影响其他股票。"""
        text = """```json
{
  "stocks": [
    {
      "stock_code": "",
      "stock_name": "",
      "action": "buy",
      "confidence": 80,
      "reason": "bad"
    },
    {
      "stock_code": "000001",
      "stock_name": "A",
      "action": "buy",
      "confidence": 70,
      "buy_zone_min": 10,
      "buy_zone_max": 11,
      "stop_loss": 9,
      "take_profit": 13,
      "reason": "good"
    }
  ]
}
```
"""
        signals = AIAdvisor._parse_json_response(text, "test")
        assert signals is not None
        assert len(signals) == 1
        assert signals[0].stock_code == "000001"

    def test_parse_stock_result_returns_order_signal(self):
        """_parse_stock_result 应返回正确 OrderSignal。"""
        item = {
            "stock_code": "000001",
            "stock_name": "平安银行",
            "action": "buy",
            "confidence": 85,
            "buy_zone_min": 12.30,
            "buy_zone_max": 12.60,
            "stop_loss": 11.80,
            "take_profit": 14.00,
            "reason": "测试分析",
            "key_risk": "测试风险",
        }
        signal = AIAdvisor._parse_stock_result(item, "qwen")
        assert signal is not None
        assert signal.signal_score == 85
        assert signal.target_position == 0.10

    def test_parse_stock_result_skip_returns_none(self):
        """action=skip 应返回 None。"""
        item = {"stock_code": "000001", "action": "skip"}
        signal = AIAdvisor._parse_stock_result(item, "test")
        assert signal is None


# =====================  Tests: Merge Logic  =====================


class TestMergeLogic:
    """测试多模型合并逻辑。"""

    def make_signal(self, code: str, name: str, buy_min: float, buy_max: float,
                    sl: float, tp: float, score: float, model: str = "qwen"):
        return OrderSignal(
            stock_code=code,
            stock_name=name,
            signal_type=SignalType.BUY,
            source=SignalSource.AI_ENHANCED,
            buy_zone_min=buy_min,
            buy_zone_max=buy_max,
            stop_loss=sl,
            take_profit=tp,
            target_position=0.10,
            signal_score=score,
            strategy_name=f"ai_advisor_{model}",
            reason=f"{model} reason",
        )

    def test_single_model_returns_unchanged(self):
        """只有一个模型结果时直接返回。"""
        s = self.make_signal("000001", "A", 12.30, 12.60, 11.80, 14.00, 80)
        merged = AIAdvisor._merge_results([s])
        assert len(merged) == 1
        assert merged[0] is s

    def test_merge_averages_buy_zone(self):
        """买入区间取平均值。"""
        s1 = self.make_signal("000001", "A", 12.30, 12.60, 11.80, 14.00, 80)
        s2 = self.make_signal("000001", "A", 12.40, 12.70, 11.90, 13.50, 75)
        merged = AIAdvisor._merge_results([s1], [s2])
        assert len(merged) == 1
        assert merged[0].buy_zone_min == 12.35  # (12.30 + 12.40) / 2
        assert merged[0].buy_zone_max == 12.65  # (12.60 + 12.70) / 2

    def test_merge_takes_stricter_stop_loss(self):
        """止损取较高值（更严格）。"""
        s1 = self.make_signal("000001", "A", 12.30, 12.60, 11.80, 14.00, 80)
        s2 = self.make_signal("000001", "A", 12.40, 12.70, 11.90, 13.50, 75)
        merged = AIAdvisor._merge_results([s1], [s2])
        assert merged[0].stop_loss == 11.90  # max(11.80, 11.90)

    def test_merge_takes_conservative_take_profit(self):
        """止盈取较低值（更保守）。"""
        s1 = self.make_signal("000001", "A", 12.30, 12.60, 11.80, 14.00, 80)
        s2 = self.make_signal("000001", "A", 12.40, 12.70, 11.90, 13.50, 75)
        merged = AIAdvisor._merge_results([s1], [s2])
        assert merged[0].take_profit == 13.50  # min(14.00, 13.50)

    def test_merge_averages_scores(self):
        """信号分取平均值。"""
        s1 = self.make_signal("000001", "A", 12.30, 12.60, 11.80, 14.00, 80)
        s2 = self.make_signal("000001", "A", 12.40, 12.70, 11.90, 13.50, 75)
        merged = AIAdvisor._merge_results([s1], [s2])
        assert merged[0].signal_score == 77.5  # (80 + 75) / 2

    def test_merge_keeps_unique_stocks(self):
        """只出现在一个模型中的股票被保留。"""
        s1 = self.make_signal("000001", "A", 12.30, 12.60, 11.80, 14.00, 80)
        s2 = self.make_signal("000002", "B", 15.60, 15.90, 15.00, 17.00, 55)
        merged = AIAdvisor._merge_results([s1], [s2])
        assert len(merged) == 2
        codes = {s.stock_code for s in merged}
        assert codes == {"000001", "000002"}

    def test_merge_different_stocks_common(self):
        """一只股票两个模型都有，另一只只有一个模型有。"""
        s1a = self.make_signal("000001", "A", 12.30, 12.60, 11.80, 14.00, 80)
        s2a = self.make_signal("000001", "A", 12.40, 12.70, 11.90, 13.50, 75)
        s2b = self.make_signal("000002", "B", 15.60, 15.90, 15.00, 17.00, 55)
        merged = AIAdvisor._merge_results([s1a], [s2a, s2b])
        assert len(merged) == 2
        by_code = {s.stock_code: s for s in merged}
        assert "000001" in by_code
        assert "000002" in by_code
        # 000001 是合并后的
        assert by_code["000001"].signal_score == 77.5
        # 000002 是原始的
        assert by_code["000002"].signal_score == 55

    def test_empty_result_lists(self):
        """空列表返回空列表。"""
        assert AIAdvisor._merge_results() == []


# =====================  Tests: analyze() with mocked AI  =====================


class TestAnalyzeWithMocks:
    """测试 analyze() 整体流程（AI 被 mock）。"""

    @patch("analysis.advisor.AIAdvisor._create_analyzer")
    def test_analyze_empty_candidates(self, mock_create):
        """空候选返回空列表。"""
        mock_create.return_value = MagicMock()
        advisor = AIAdvisor()
        signals = advisor.analyze([])
        assert signals == []

    @patch("analysis.advisor.AIAdvisor._call_and_parse")
    def test_analyze_fallback_to_single_model(
        self, mock_call, mock_stock_scores, mock_qwen_response
    ):
        """一个模型失败时，回退到另一个模型的结果。"""
        # 手动构建一个 analyzer 列表，模拟只有一个可用
        mock_ana = MagicMock()
        mock_ana.model = "qwen3.6-plus"
        advisor_inst = AIAdvisor.__new__(AIAdvisor)
        advisor_inst._analyzers = [("qwen", mock_ana)]
        advisor_inst.logger = MagicMock()

        # mock _call_and_parse
        mock_call.return_value = [
            OrderSignal(
                stock_code="000001",
                stock_name="平安银行",
                signal_type=SignalType.BUY,
                source=SignalSource.AI_ENHANCED,
                buy_zone_min=12.30,
                buy_zone_max=12.60,
                stop_loss=11.80,
                take_profit=14.00,
                target_position=0.10,
                signal_score=80,
                strategy_name="ai_advisor_qwen",
                reason="MA多头排列",
            )
        ]

        signals = advisor_inst.analyze(mock_stock_scores, "2025-01-15")
        assert len(signals) == 1
        assert signals[0].stock_code == "000001"

    @patch("analysis.advisor.AIAdvisor._create_analyzer")
    def test_analyze_both_fail_returns_empty(self, mock_create, mock_stock_scores):
        """两个模型都失败时返回空列表。"""
        mock_create.return_value = MagicMock()
        advisor = AIAdvisor()
        advisor._analyzers = []  # 模拟无分析器
        signals = advisor.analyze(mock_stock_scores)
        assert signals == []


# =====================  Tests: _safe_float  =====================


class TestSafeFloat:
    """测试 _safe_float 辅助方法。"""

    def test_valid_float(self):
        assert AIAdvisor._safe_float({"a": 12.5}, "a") == 12.5

    def test_none_value(self):
        assert AIAdvisor._safe_float({"a": None}, "a") is None

    def test_string_number(self):
        assert AIAdvisor._safe_float({"a": "12.5"}, "a") == 12.5

    def test_missing_key(self):
        assert AIAdvisor._safe_float({}, "a") is None

    def test_invalid_string(self):
        assert AIAdvisor._safe_float({"a": "abc"}, "a") is None
