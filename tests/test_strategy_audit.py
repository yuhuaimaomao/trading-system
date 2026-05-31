# -*- coding: utf-8 -*-
"""选股自我进化系统集成测试"""

import json
from analysis.signals import StrategyAiDecision, StrategyAiResult
from analysis.audit.prompts import STRATEGY_AUDIT_PROMPT


class TestStrategyAiDecision:
    def test_buy_decision_has_required_fields(self):
        d = StrategyAiDecision(
            stock_code="000001",
            stock_name="测试",
            rank_in_prompt=1,
            verdict="buy",
            confidence="high",
            what_i_see="沿MA5上行",
            what_concerns_me="板块走弱",
            decisive_factor="主力连续3日净买",
        )
        assert d.verdict == "buy"
        assert d.decisive_factor
        assert not d.skip_reason

    def test_skip_decision_has_skip_fields(self):
        d = StrategyAiDecision(
            stock_code="000002",
            stock_name="测试2",
            rank_in_prompt=2,
            verdict="skip",
            what_i_see="弱势震荡",
            what_concerns_me="无板块支撑",
            decisive_factor="量能不足",
            skip_reason="成交量持续萎缩，无启动迹象",
            would_reconsider_if="放量突破MA20且板块转强",
        )
        assert d.verdict == "skip"
        assert d.skip_reason
        assert d.would_reconsider_if

    def test_buy_decision_has_pricing(self):
        d = StrategyAiDecision(
            stock_code="000001",
            stock_name="测试",
            rank_in_prompt=1,
            verdict="buy",
            confidence="medium",
            buy_zone_min=12.20,
            buy_zone_max=12.80,
            stop_loss=11.80,
            take_profit=14.50,
            pricing_logic="基于MA10支撑",
        )
        assert d.buy_zone_min == 12.20
        assert d.stop_loss == 11.80
        assert d.pricing_logic


class TestAiResult:
    def test_ai_result_defaults(self):
        r = StrategyAiResult(model_used="qwen")
        assert r.model_used == "qwen"
        assert r.decisions == []
        assert r.holdings_review == []
        assert r.self_assessment == ""

    def test_ai_result_with_decisions(self):
        d = StrategyAiDecision(
            stock_code="000001", stock_name="test", rank_in_prompt=1,
            verdict="buy", confidence="high",
        )
        r = StrategyAiResult(
            model_used="qwen",
            decisions=[d],
            self_assessment="对这只票有信心",
        )
        assert len(r.decisions) == 1
        assert r.self_assessment


class TestAuditPrompt:
    def test_prompt_has_required_sections(self):
        assert "审查对象" in STRATEGY_AUDIT_PROMPT
        assert "逐票审查" in STRATEGY_AUDIT_PROMPT
        assert "发现偏见" in STRATEGY_AUDIT_PROMPT
        assert "发现遗漏" in STRATEGY_AUDIT_PROMPT
        assert "建议改进" in STRATEGY_AUDIT_PROMPT

    def test_prompt_format_works(self):
        prompt = STRATEGY_AUDIT_PROMPT.format(
            push_date="2026-05-30",
            market_context="大盘普涨",
            holdings_context="模拟盘持仓2只",
            ai_decisions="测试决策",
            actual_outcomes="测试结果",
            rule_findings="[]",
            historical_lessons="无",
        )
        assert "2026-05-30" in prompt
        assert "大盘普涨" in prompt


class TestRuleAuditorEmpty:
    def test_audit_no_data_returns_empty(self):
        from analysis.audit.rule_auditor import RuleAuditor
        auditor = RuleAuditor()
        findings = auditor.audit("2099-01-01")
        assert findings == []
