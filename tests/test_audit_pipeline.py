"""审计子系统完整测试。

覆盖范围:
  1. BaseRuleAuditor / BaseAIAuditor — 基类接口
  2. StrategyRuleAuditor — 策略规则审计
  3. StrategyAIAuditor — 策略 AI 审计
  4. WatcherRuleAuditor — 盯盘规则审计
  5. WatcherAIAuditor — 盯盘 AI 审计
  6. AuditPipeline — 审计管线
  7. DecisionLogger — 决策日志
  8. ImprovementApplier + format_improvement_card — 改进建议应用
"""

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ================================================================
#  Fixtures
# ================================================================


@pytest.fixture
def audit_db_path():
    """临时 SQLite 数据库，含审计子系统所用全部表结构。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS strategy_funnel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            push_date TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            rank_position INTEGER,
            raw_snapshot TEXT NOT NULL DEFAULT '',
            factors_passed TEXT,
            factors_detail TEXT,
            scenarios TEXT,
            trend_mode TEXT,
            score REAL,
            open_price REAL,
            close_price REAL,
            day_change_pct REAL,
            bought INTEGER DEFAULT 0,
            buy_price REAL,
            day_pnl_pct REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS strategy_ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            push_date TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            rank_in_prompt INTEGER,
            verdict TEXT,
            confidence TEXT,
            what_i_see TEXT,
            what_concerns_me TEXT,
            decisive_factor TEXT,
            skip_reason TEXT,
            would_reconsider_if TEXT,
            buy_zone_min REAL,
            buy_zone_max REAL,
            stop_loss REAL,
            take_profit REAL,
            pricing_logic TEXT,
            signal_id INTEGER,
            day_change_pct REAL,
            day_pnl_pct REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS strategy_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_type TEXT NOT NULL,
            lesson_key TEXT NOT NULL,
            lesson_content TEXT NOT NULL,
            trigger_conditions TEXT,
            occurrence_count INTEGER DEFAULT 1,
            first_date TEXT NOT NULL,
            last_date TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lesson_type, lesson_key)
        );

        CREATE TABLE IF NOT EXISTS strategy_improvements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            push_date TEXT NOT NULL,
            improvement_type TEXT NOT NULL,
            target_module TEXT,
            target_param TEXT,
            suggested_change TEXT NOT NULL,
            code_diff TEXT,
            rationale TEXT NOT NULL,
            evidence_ids TEXT,
            status TEXT DEFAULT 'pending',
            applied_date TEXT,
            effectiveness_check TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS watcher_decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            ts TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            stock_code TEXT,
            decision_data TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            finding_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            stock_code TEXT,
            decision_log_ids TEXT,
            pattern_desc TEXT NOT NULL,
            evidence TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS watcher_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_type TEXT NOT NULL,
            lesson_key TEXT NOT NULL,
            lesson_content TEXT NOT NULL,
            trigger_conditions TEXT,
            occurrence_count INTEGER DEFAULT 1,
            first_date DATE NOT NULL,
            last_date DATE NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lesson_type, lesson_key)
        );

        CREATE TABLE IF NOT EXISTS watcher_improvements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            improvement_type TEXT NOT NULL,
            target_module TEXT NOT NULL,
            target_param TEXT,
            suggested_change TEXT NOT NULL,
            code_diff TEXT,
            rationale TEXT NOT NULL,
            evidence_ids TEXT,
            status TEXT DEFAULT 'pending',
            applied_date DATE,
            effectiveness_check TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS index_snapshots (
            trade_date TEXT NOT NULL,
            ts REAL NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            high REAL DEFAULT 0,
            low REAL DEFAULT 0,
            pre_close REAL DEFAULT 0,
            change_pct REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            index_code TEXT DEFAULT '000001',
            PRIMARY KEY (trade_date, ts)
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            trade_date TEXT NOT NULL,
            ts REAL NOT NULL,
            code TEXT NOT NULL,
            change_pct REAL DEFAULT 0,
            price REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            PRIMARY KEY (trade_date, ts, code)
        );

        CREATE TABLE IF NOT EXISTS stock_basic (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            price REAL, open REAL, high REAL, low REAL, prev_close REAL,
            change_pct REAL, total_market_cap REAL, circ_market_cap REAL,
            turnover_rate REAL, volume_ratio REAL, amplitude REAL, volume REAL,
            ma5 REAL, ma10 REAL, ma20 REAL, ma5_angle REAL,
            industry TEXT, concepts TEXT,
            main_force_net REAL, main_force_ratio REAL,
            super_large_net REAL, large_net REAL, medium_net REAL, small_net REAL,
            avg_vol_5d REAL, avg_vol_20d REAL,
            pe_ttm REAL, pb_ratio REAL, revenue_growth REAL, profit_growth REAL
        );

        CREATE TABLE IF NOT EXISTS trade_portfolio_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            account TEXT DEFAULT 'paper',
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            volume INTEGER,
            avg_cost REAL,
            current_price REAL,
            market_value REAL,
            pnl REAL,
            pnl_pct REAL,
            pre_close REAL DEFAULT 0,
            daily_pnl REAL DEFAULT 0,
            entry_date TEXT DEFAULT '',
            locked_volume INTEGER DEFAULT 0,
            stop_loss REAL,
            take_profit REAL,
            holding_days INTEGER DEFAULT 0,
            sector_code TEXT,
            created_at TEXT,
            UNIQUE(trade_date, account, stock_code)
        );

        CREATE TABLE IF NOT EXISTS trade_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            trade_date TEXT NOT NULL,
            order_time TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            order_type TEXT NOT NULL,
            order_price REAL,
            order_volume INTEGER,
            price_type TEXT DEFAULT 'limit',
            order_status TEXT DEFAULT 'pending',
            filled_volume INTEGER DEFAULT 0,
            filled_price REAL,
            filled_amount REAL,
            commission REAL,
            qmt_order_id TEXT,
            reject_reason TEXT,
            strategy_name TEXT,
            updated_at TEXT,
            account TEXT DEFAULT 'paper'
        );

        CREATE TABLE IF NOT EXISTS review_lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lesson_type TEXT NOT NULL,
            lesson_key TEXT NOT NULL,
            lesson_content TEXT NOT NULL,
            occurrence_count INTEGER DEFAULT 1,
            first_date TEXT NOT NULL,
            last_date TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lesson_type, lesson_key)
        );

        CREATE TABLE IF NOT EXISTS sector_snapshots (
            trade_date TEXT NOT NULL,
            ts TEXT NOT NULL,
            sector_name TEXT NOT NULL,
            avg_change REAL,
            PRIMARY KEY (trade_date, ts, sector_name)
        );
    """)
    conn.commit()
    conn.close()
    yield path
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def mock_ai_chat(monkeypatch):
    """Mock system.ai.ai.chat 返回可控 JSON。"""

    def _make_mock(response_text: str = ""):
        mock = MagicMock(return_value=response_text)
        monkeypatch.setattr("system.ai.ai.chat", mock)
        return mock

    return _make_mock


# ================================================================
#  1. BaseRuleAuditor / BaseAIAuditor — 基类接口
# ================================================================


class TestBaseRuleAuditor:
    """验证 BaseRuleAuditor 自动收集 check_* 方法的约定。"""

    def test_base_audit_collects_check_methods(self):
        from audit.audit_base import BaseRuleAuditor

        class Concrete(BaseRuleAuditor):
            def check_foo(self, date):
                return [{"type": "foo"}]

            def check_bar(self, date):
                return [{"type": "bar"}]

        auditor = Concrete()
        findings = auditor.audit("2026-06-01")
        assert len(findings) == 2
        # dir() 结果按字母序排列，check_bar 先于 check_foo
        types = [f["type"] for f in findings]
        assert "foo" in types
        assert "bar" in types

    def test_base_audit_empty_when_no_check_methods(self):
        from audit.audit_base import BaseRuleAuditor

        auditor = BaseRuleAuditor()
        findings = auditor.audit("2026-06-01")
        assert findings == []

    def test_base_audit_skips_check_errors(self):
        from audit.audit_base import BaseRuleAuditor

        class WithError(BaseRuleAuditor):
            def check_good(self, date):
                return [{"type": "good"}]

            def check_bad(self, date):
                raise ValueError("boom")

        auditor = WithError()
        findings = auditor.audit("2026-06-01")
        assert len(findings) == 1
        assert findings[0]["type"] == "good"

    def test_base_audit_init_stores_repo_and_db_path(self):
        from audit.audit_base import BaseRuleAuditor

        auditor = BaseRuleAuditor(repo="fake_repo", db_path="/tmp/test.db")
        assert auditor.repo == "fake_repo"
        assert auditor.db_path == "/tmp/test.db"


class TestBaseAIAuditor:
    """验证 BaseAIAuditor 的 review / _parse 行为。"""

    def test_review_calls_ai_and_parses_response(self, mock_ai_chat):
        from audit.audit_base import BaseAIAuditor

        mock_ai_chat(
            '```json\n{"improvements": [{"type": "prompt_tune"}], "lessons": []}\n```'
        )

        class TestAuditor(BaseAIAuditor):
            def _build_prompt(self, findings, context):
                return "test prompt"

            def _system_prompt(self):
                return "test system"

        auditor = TestAuditor()
        result = auditor.review([{"type": "test"}], {"date": "2026-06-01"})

        assert result["improvements"] == [{"type": "prompt_tune"}]
        assert result["lessons"] == []

    def test_review_empty_prompt_returns_empty(self, mock_ai_chat):
        from audit.audit_base import BaseAIAuditor

        class EmptyPromptAuditor(BaseAIAuditor):
            def _build_prompt(self, findings, context):
                return ""

            def _system_prompt(self):
                return ""

        auditor = EmptyPromptAuditor()
        result = auditor.review([{"type": "test"}])
        assert result == {}

    def test_review_ai_exception_returns_empty(self, mock_ai_chat):
        from audit.audit_base import BaseAIAuditor

        def raise_error(*args, **kwargs):
            raise RuntimeError("API error")

        mock_ai_chat(raise_error)

        class TestAuditor(BaseAIAuditor):
            def _build_prompt(self, findings, context):
                return "test prompt"

            def _system_prompt(self):
                return "test system"

        auditor = TestAuditor()
        result = auditor.review([{"type": "test"}])
        assert result == {}

    def test_parse_code_block_json(self):
        from audit.audit_base import BaseAIAuditor

        raw = '```json\n{"improvements": [{"type": "test"}], "lessons": []}\n```'
        result = BaseAIAuditor._parse(None, raw)
        assert result["improvements"] == [{"type": "test"}]

    def test_parse_fallback_plain_braces(self):
        from audit.audit_base import BaseAIAuditor

        raw = 'some text {"improvements": [], "lessons": [{"type": "found"}]} trailing'
        result = BaseAIAuditor._parse(None, raw)
        assert result["lessons"] == [{"type": "found"}]

    def test_parse_no_json_returns_default(self):
        from audit.audit_base import BaseAIAuditor

        raw = "纯文本回复，没有 JSON"
        result = BaseAIAuditor._parse(None, raw)
        assert result == {"improvements": [], "lessons": []}

    def test_parse_invalid_json_returns_default(self):
        from audit.audit_base import BaseAIAuditor

        raw = '```json\n{"improvements": [broken]}\n```'
        result = BaseAIAuditor._parse(None, raw)
        assert result == {"improvements": [], "lessons": []}

    def test_parse_plain_code_block_no_json_lang(self):
        from audit.audit_base import BaseAIAuditor

        raw = '```\n{"improvements": [{"type": "x"}], "lessons": []}\n```'
        result = BaseAIAuditor._parse(None, raw)
        assert result["improvements"] == [{"type": "x"}]


# ================================================================
#  2. StrategyRuleAuditor — 策略规则审计
# ================================================================


def _seed_strategy_data(conn, push_date: str):
    """向 strategy_funnel + strategy_ai_decisions 插入样本数据。"""
    conn.execute(
        "INSERT INTO strategy_funnel (push_date, trade_date, stock_code, stock_name, "
        "factors_passed, factors_detail, scenarios, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            push_date,
            push_date,
            "000001",
            "平安银行",
            '["momentum", "volume"]',
            '{"momentum": {"passed": true, "margin_pct": 3.0}}',
            '["上涨放量"]',
            75,
        ),
    )
    conn.execute(
        "INSERT INTO strategy_funnel (push_date, trade_date, stock_code, stock_name, "
        "factors_passed, factors_detail, scenarios, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            push_date,
            push_date,
            "000002",
            "万科A",
            '["momentum"]',
            '{"momentum": {"passed": true, "margin_pct": 1.5}}',
            '["震荡"]',
            60,
        ),
    )
    conn.execute(
        "INSERT INTO strategy_funnel (push_date, trade_date, stock_code, stock_name, "
        "factors_passed, factors_detail, scenarios, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            push_date,
            push_date,
            "000003",
            "某股票A",
            '["momentum", "volume"]',
            '{"volume": {"passed": true, "margin_pct": 2.0}}',
            '["上涨放量"]',
            65,
        ),
    )
    conn.execute(
        "INSERT INTO strategy_funnel (push_date, trade_date, stock_code, stock_name, "
        "factors_passed, factors_detail, scenarios, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            push_date,
            push_date,
            "000004",
            "某股票B",
            '["volume"]',
            '{"volume": {"passed": true, "margin_pct": 4.0}}',
            '["震荡"]',
            55,
        ),
    )
    conn.execute(
        "INSERT INTO strategy_funnel (push_date, trade_date, stock_code, stock_name, "
        "factors_passed, factors_detail, scenarios, score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            push_date,
            push_date,
            "000005",
            "某股票C",
            '["momentum"]',
            '{"momentum": {"passed": true, "margin_pct": 1.0}}',
            '["上涨放量"]',
            70,
        ),
    )

    # strategy_ai_decisions
    conn.execute(
        "INSERT INTO strategy_ai_decisions (push_date, trade_date, stock_code, stock_name, "
        "verdict, confidence, day_change_pct, skip_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (push_date, push_date, "000001", "平安银行", "buy", "high", -1.5, None),
    )
    conn.execute(
        "INSERT INTO strategy_ai_decisions (push_date, trade_date, stock_code, stock_name, "
        "verdict, confidence, day_change_pct, skip_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (push_date, push_date, "000002", "万科A", "buy", "medium", -2.0, None),
    )
    conn.execute(
        "INSERT INTO strategy_ai_decisions (push_date, trade_date, stock_code, stock_name, "
        "verdict, confidence, day_change_pct, skip_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (push_date, push_date, "000003", "某股票A", "skip", "low", 5.0, "量能不足"),
    )
    conn.execute(
        "INSERT INTO strategy_ai_decisions (push_date, trade_date, stock_code, stock_name, "
        "verdict, confidence, day_change_pct, skip_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (push_date, push_date, "000004", "某股票B", "skip", "low", -4.0, "趋势不明"),
    )
    conn.execute(
        "INSERT INTO strategy_ai_decisions (push_date, trade_date, stock_code, stock_name, "
        "verdict, confidence, day_change_pct, skip_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (push_date, push_date, "000005", "某股票C", "buy", "high", 3.5, None),
    )

    conn.commit()


class TestStrategyRuleAuditor:
    """策略规则审计 — audit / 空数据 / 必要字段。"""

    def test_audit_with_valid_date_returns_findings(self, audit_db_path):
        from audit.strategy_rule_auditor import RuleAuditor as StrategyRuleAuditor

        conn = sqlite3.connect(audit_db_path)
        _seed_strategy_data(conn, "2026-06-01")
        conn.close()

        auditor = StrategyRuleAuditor(db_path=audit_db_path)
        findings = auditor.audit("2026-06-01")

        assert isinstance(findings, list)
        assert len(findings) > 0, "预期至少有一条审计发现"

    def test_audit_with_no_data_returns_empty(self, audit_db_path):
        from audit.strategy_rule_auditor import RuleAuditor as StrategyRuleAuditor

        auditor = StrategyRuleAuditor(db_path=audit_db_path)
        findings = auditor.audit("2026-06-01")
        assert findings == []

    def test_findings_have_required_fields(self, audit_db_path):
        from audit.strategy_rule_auditor import RuleAuditor as StrategyRuleAuditor

        conn = sqlite3.connect(audit_db_path)
        _seed_strategy_data(conn, "2026-06-01")
        conn.close()

        auditor = StrategyRuleAuditor(db_path=audit_db_path)
        findings = auditor.audit("2026-06-01")

        assert len(findings) > 0
        for f in findings:
            assert "type" in f, f"finding 缺少 type: {f}"
            assert "severity" in f, f"finding 缺少 severity: {f}"

    def test_skip_counterfactual_produces_findings(self, audit_db_path):
        """skip 票如果涨幅 >3% 应产生 skip_missed_gain 发现。"""
        from audit.strategy_rule_auditor import RuleAuditor as StrategyRuleAuditor

        conn = sqlite3.connect(audit_db_path)
        conn.execute(
            "INSERT INTO strategy_ai_decisions (push_date, trade_date, stock_code, "
            "stock_name, verdict, confidence, day_change_pct, skip_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-06-02",
                "2026-06-02",
                "600001",
                "大涨票",
                "skip",
                "high",
                8.5,
                "无信号",
            ),
        )
        conn.commit()
        conn.close()

        auditor = StrategyRuleAuditor(db_path=audit_db_path)
        findings = auditor.audit("2026-06-02")
        skipped = [f for f in findings if f["type"] == "skip_missed_gain"]
        assert len(skipped) >= 1
        assert skipped[0]["missed_return"] == 8.5


# ================================================================
#  3. StrategyAIAuditor — 策略 AI 审计
# ================================================================


class TestStrategyAIAuditor:
    """策略 AI 审计 — AI 调用 / 解析 / 错误处理。"""

    def test_audit_no_decisions_returns_empty(self, audit_db_path):
        from audit.strategy_ai_auditor import AIAuditor as StrategyAIAuditor

        auditor = StrategyAIAuditor(db_path=audit_db_path)
        result = auditor.audit("2026-06-01", [])
        assert result == {}

    def test_audit_calls_ai_and_parses_response(self, audit_db_path, mock_ai_chat):
        from audit.strategy_ai_auditor import AIAuditor as StrategyAIAuditor

        conn = sqlite3.connect(audit_db_path)
        _seed_strategy_data(conn, "2026-06-01")
        conn.close()

        ai_response = json.dumps(
            {
                "case_reviews": [
                    {"code": "000001", "verdict_match": True, "analysis": "OK"}
                ],
                "bias_findings": [],
                "omission_findings": [],
                "lessons": [
                    {
                        "type": "ai_reasoning",
                        "key": "test",
                        "content": "教训内容",
                        "trigger_conditions": {},
                    }
                ],
                "improvements": [
                    {
                        "type": "prompt_tune",
                        "target": "测试模块",
                        "suggested_change": "修改 prompt",
                        "rationale": "理由",
                    }
                ],
                "self_review": {
                    "did_strategy_ai_self_assessment_match": "",
                    "meta_pattern": "",
                },
            }
        )
        mock = mock_ai_chat(f"```json\n{ai_response}\n```")

        auditor = StrategyAIAuditor(db_path=audit_db_path)
        result = auditor.audit(
            "2026-06-01", [{"type": "factor_misleading", "severity": "P1"}]
        )

        assert result is not None
        assert len(result.get("improvements", [])) >= 1
        assert result["improvements"][0]["type"] == "prompt_tune"
        assert len(result.get("lessons", [])) >= 1
        mock.assert_called_once()

    def test_audit_invalid_json_handled_gracefully(self, audit_db_path, mock_ai_chat):
        from audit.strategy_ai_auditor import AIAuditor as StrategyAIAuditor

        conn = sqlite3.connect(audit_db_path)
        _seed_strategy_data(conn, "2026-06-01")
        conn.close()

        mock_ai_chat("```json\n{invalid json here}\n```")

        auditor = StrategyAIAuditor(db_path=audit_db_path)
        result = auditor.audit(
            "2026-06-01", [{"type": "factor_misleading", "severity": "P1"}]
        )

        assert result == {}

    def test_audit_ai_returns_empty_string(self, audit_db_path, mock_ai_chat):
        from audit.strategy_ai_auditor import AIAuditor as StrategyAIAuditor

        conn = sqlite3.connect(audit_db_path)
        _seed_strategy_data(conn, "2026-06-01")
        conn.close()

        mock_ai_chat("")

        auditor = StrategyAIAuditor(db_path=audit_db_path)
        result = auditor.audit(
            "2026-06-01", [{"type": "factor_misleading", "severity": "P1"}]
        )
        assert result == {}

    def test_audit_saves_results_to_db(self, audit_db_path, mock_ai_chat):
        from audit.strategy_ai_auditor import AIAuditor as StrategyAIAuditor
        from data.repo import TradeRepository

        conn = sqlite3.connect(audit_db_path)
        _seed_strategy_data(conn, "2026-06-01")
        conn.close()

        ai_response = json.dumps(
            {
                "case_reviews": [],
                "bias_findings": [],
                "omission_findings": [],
                "lessons": [
                    {
                        "type": "ai_reasoning",
                        "key": "saved_lesson",
                        "content": "已保存",
                        "trigger_conditions": {"factor": "volume"},
                    }
                ],
                "improvements": [
                    {
                        "type": "prompt_tune",
                        "target": "module1",
                        "suggested_change": "change X",
                        "rationale": "reason",
                    }
                ],
                "self_review": {
                    "did_strategy_ai_self_assessment_match": "",
                    "meta_pattern": "",
                },
            }
        )
        mock_ai_chat(f"```json\n{ai_response}\n```")

        auditor = StrategyAIAuditor(db_path=audit_db_path)
        auditor.audit("2026-06-01", [{"type": "factor_misleading", "severity": "P1"}])

        repo = TradeRepository(db_path=audit_db_path)
        lessons = repo.get_active_lessons()
        assert any(ls["lesson_key"] == "saved_lesson" for ls in lessons)

        improvements = repo.get_pending_improvements()
        assert any("change X" in imp["suggested_change"] for imp in improvements)


# ================================================================
#  4. WatcherRuleAuditor — 盯盘规则审计
# ================================================================


def _seed_watcher_data(conn, trade_date: str):
    """向 watcher_decision_log 插入样本决策日志。"""
    now_ts = datetime.now().timestamp()
    ts_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    # regime_change 决策
    conn.execute(
        "INSERT INTO watcher_decision_log (trade_date, ts, decision_type, stock_code, decision_data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            trade_date,
            ts_str,
            "regime_change",
            None,
            json.dumps({"pattern": "normal", "confidence": "high"}, ensure_ascii=False),
        ),
    )

    # buy_trigger
    conn.execute(
        "INSERT INTO watcher_decision_log (trade_date, ts, decision_type, stock_code, decision_data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            trade_date,
            ts_str,
            "buy_trigger",
            "600001",
            json.dumps(
                {
                    "price": 10.0,
                    "position_size": 1000,
                    "entry_rule": "trend_follow",
                    "sector_trend": "up",
                    "market_regime": "normal",
                },
                ensure_ascii=False,
            ),
        ),
    )

    # buy_filter
    conn.execute(
        "INSERT INTO watcher_decision_log (trade_date, ts, decision_type, stock_code, decision_data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            trade_date,
            ts_str,
            "buy_filter",
            "600002",
            json.dumps(
                {
                    "price": 20.0,
                    "reason_filtered": "量能不足",
                    "entry_rule": "breakout",
                },
                ensure_ascii=False,
            ),
        ),
    )

    # buy_trigger (第二只入仓位分析用)
    conn.execute(
        "INSERT INTO watcher_decision_log (trade_date, ts, decision_type, stock_code, decision_data) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            trade_date,
            ts_str,
            "buy_trigger",
            "600003",
            json.dumps(
                {
                    "price": 15.0,
                    "position_size": 2000,
                    "entry_rule": "dip_buy",
                    "sector_trend": "flat",
                    "market_regime": "normal",
                },
                ensure_ascii=False,
            ),
        ),
    )

    # 注入 index_snapshots 使 regime 审计不跳过
    conn.execute(
        "INSERT INTO index_snapshots (trade_date, ts, price) VALUES (?, ?, ?)",
        (trade_date, now_ts - 10, 3100.0),
    )
    conn.execute(
        "INSERT INTO index_snapshots (trade_date, ts, price) VALUES (?, ?, ?)",
        (trade_date, now_ts - 5, 3105.0),
    )
    conn.execute(
        "INSERT INTO index_snapshots (trade_date, ts, price) VALUES (?, ?, ?)",
        (trade_date, now_ts, 3102.0),
    )
    conn.execute(
        "INSERT INTO index_snapshots (trade_date, ts, price) VALUES (?, ?, ?)",
        (trade_date, now_ts + 5, 3103.0),
    )
    conn.execute(
        "INSERT INTO index_snapshots (trade_date, ts, price) VALUES (?, ?, ?)",
        (trade_date, now_ts + 10, 3101.0),
    )
    conn.execute(
        "INSERT INTO index_snapshots (trade_date, ts, price) VALUES (?, ?, ?)",
        (trade_date, now_ts + 30, 3100.0),
    )
    conn.execute(
        "INSERT INTO index_snapshots (trade_date, ts, price) VALUES (?, ?, ?)",
        (trade_date, now_ts + 60, 3098.0),
    )

    # 注入 market_snapshots 供 _get_close fallback
    conn.execute(
        "INSERT INTO market_snapshots (trade_date, ts, code, price) VALUES (?, ?, ?, ?)",
        (trade_date, now_ts, "600001", 10.5),
    )
    conn.execute(
        "INSERT INTO market_snapshots (trade_date, ts, code, price) VALUES (?, ?, ?, ?)",
        (trade_date, now_ts, "600002", 21.0),
    )
    conn.execute(
        "INSERT INTO market_snapshots (trade_date, ts, code, price) VALUES (?, ?, ?, ?)",
        (trade_date, now_ts, "600003", 14.0),
    )

    # 注入 stock_basic 供 _get_close
    conn.execute(
        "INSERT INTO stock_basic (trade_date, stock_code, stock_name, price) "
        "VALUES (?, ?, ?, ?)",
        (trade_date, "600001", "测试股票1", 10.5),
    )
    conn.execute(
        "INSERT INTO stock_basic (trade_date, stock_code, stock_name, price) "
        "VALUES (?, ?, ?, ?)",
        (trade_date, "600002", "测试股票2", 21.0),
    )
    conn.execute(
        "INSERT INTO stock_basic (trade_date, stock_code, stock_name, price) "
        "VALUES (?, ?, ?, ?)",
        (trade_date, "600003", "测试股票3", 14.0),
    )

    conn.commit()


class TestWatcherRuleAuditor:
    """盯盘规则审计 — 逐决策回溯验证。"""

    def test_audit_with_valid_date_returns_findings(self, audit_db_path):
        from audit.watcher_rule_auditor import RuleAuditor as WatcherRuleAuditor

        conn = sqlite3.connect(audit_db_path)
        _seed_watcher_data(conn, "2026-06-01")
        conn.close()

        auditor = WatcherRuleAuditor(db_path=audit_db_path)
        findings = auditor.audit("2026-06-01")
        assert isinstance(findings, list)
        assert len(findings) >= 1

    def test_audit_with_no_data_returns_empty(self, audit_db_path):
        from audit.watcher_rule_auditor import RuleAuditor as WatcherRuleAuditor

        auditor = WatcherRuleAuditor(db_path=audit_db_path)
        findings = auditor.audit("2026-06-01")
        assert findings == []

    def test_findings_have_required_fields(self, audit_db_path):
        from audit.watcher_rule_auditor import RuleAuditor as WatcherRuleAuditor

        conn = sqlite3.connect(audit_db_path)
        _seed_watcher_data(conn, "2026-06-01")
        conn.close()

        auditor = WatcherRuleAuditor(db_path=audit_db_path)
        findings = auditor.audit("2026-06-01")

        for f in findings:
            assert "finding_type" in f, f"缺少 finding_type: {f}"
            assert "severity" in f, f"缺少 severity: {f}"
            assert "pattern_desc" in f, f"缺少 pattern_desc: {f}"
            assert "evidence" in f, f"缺少 evidence: {f}"

    def test_buy_signals_detect_bad_trades(self, audit_db_path):
        """buy_trigger 当日亏损 >3% 应产生 buy_bad 发现。"""
        from audit.watcher_rule_auditor import RuleAuditor as WatcherRuleAuditor

        conn = sqlite3.connect(audit_db_path)
        ts_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "INSERT INTO watcher_decision_log (trade_date, ts, decision_type, stock_code, decision_data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "2026-06-03",
                ts_str,
                "buy_trigger",
                "600001",
                json.dumps(
                    {"price": 100.0, "position_size": 500, "entry_rule": "test"}
                ),
            ),
        )
        conn.execute(
            "INSERT INTO stock_basic (trade_date, stock_code, stock_name, price) "
            "VALUES (?, ?, ?, ?)",
            ("2026-06-03", "600001", "亏损票", 92.0),
        )
        conn.commit()
        conn.close()

        auditor = WatcherRuleAuditor(db_path=audit_db_path)
        findings = auditor.audit("2026-06-03")
        bads = [f for f in findings if f.get("finding_type") == "buy_bad"]
        assert len(bads) >= 1, f"未生成 buy_bad 发现: {findings}"


# ================================================================
#  5. WatcherAIAuditor — 盯盘 AI 审计
# ================================================================


class TestWatcherAIAuditor:
    """盯盘 AI 审计 — 时序因果 + 模式提炼 + 改进建议。"""

    def test_audit_no_logs_returns_none(self, audit_db_path):
        from audit.watcher_ai_auditor import AIAuditor as WatcherAIAuditor
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)
        auditor = WatcherAIAuditor(repo=repo)
        result = auditor.audit("2026-06-01")
        assert result is None

    def test_audit_calls_ai_and_parses_response(self, audit_db_path, mock_ai_chat):
        from audit.watcher_ai_auditor import AIAuditor as WatcherAIAuditor
        from data.repo import TradeRepository

        conn = sqlite3.connect(audit_db_path)
        _seed_watcher_data(conn, "2026-06-01")
        conn.close()

        ai_response = json.dumps(
            {
                "causal_chains": [
                    {
                        "pattern": "止损过早",
                        "events": ["stop_trigger"],
                        "root_cause": "开盘恐慌",
                        "impact": "卖飞",
                    }
                ],
                "new_patterns": [
                    {"description": "开盘止损易卖飞", "frequency": 2, "conditions": {}}
                ],
                "improvements": [
                    {
                        "type": "param_tune",
                        "target_module": "position_risk",
                        "target_method": "check_stop_loss",
                        "suggested_change": "开盘5分钟不触发止损",
                        "code_diff": "",
                        "rationale": "减少误止损",
                        "auto_applicable": True,
                    }
                ],
                "lessons": [
                    {
                        "type": "stop_timing",
                        "key": "open_panic_stop",
                        "content": "开盘5分钟止损多为恐慌",
                        "trigger_conditions": {"minute": 5},
                    }
                ],
            }
        )
        mock = mock_ai_chat(f"```json\n{ai_response}\n```")

        repo = TradeRepository(db_path=audit_db_path)
        auditor = WatcherAIAuditor(repo=repo)
        result = auditor.audit("2026-06-01")

        assert result is not None
        assert len(result.get("causal_chains", [])) >= 1
        assert len(result.get("improvements", [])) >= 1
        assert result["improvements"][0]["type"] == "param_tune"
        mock.assert_called_once()

    def test_audit_invalid_json_returns_none(self, audit_db_path, mock_ai_chat):
        from audit.watcher_ai_auditor import AIAuditor as WatcherAIAuditor
        from data.repo import TradeRepository

        conn = sqlite3.connect(audit_db_path)
        _seed_watcher_data(conn, "2026-06-01")
        conn.close()

        mock_ai_chat("```json\n{not valid}\n```")

        repo = TradeRepository(db_path=audit_db_path)
        auditor = WatcherAIAuditor(repo=repo)
        result = auditor.audit("2026-06-01")
        assert result is None

    def test_run_and_save_persists_improvements(self, audit_db_path, mock_ai_chat):
        from audit.watcher_ai_auditor import AIAuditor as WatcherAIAuditor
        from data.repo import TradeRepository

        conn = sqlite3.connect(audit_db_path)
        _seed_watcher_data(conn, "2026-06-01")
        conn.close()

        ai_response = json.dumps(
            {
                "causal_chains": [],
                "new_patterns": [],
                "improvements": [
                    {
                        "type": "rule_add",
                        "target_module": "buy_decision",
                        "target_method": "check_volume",
                        "suggested_change": "加量能过滤",
                        "code_diff": "",
                        "rationale": "减少假突破",
                    }
                ],
                "lessons": [
                    {
                        "type": "signal_filter",
                        "key": "volume_filter",
                        "content": "量能不足易假突破",
                        "trigger_conditions": {"volume_ratio": 0.8},
                    }
                ],
            }
        )
        mock_ai_chat(f"```json\n{ai_response}\n```")

        repo = TradeRepository(db_path=audit_db_path)
        auditor = WatcherAIAuditor(repo=repo)
        result = auditor.run_and_save("2026-06-01")

        assert result is not None

        pending = repo.get_pending_watcher_improvements()
        assert len(pending) >= 1
        assert any("加量能过滤" in imp["suggested_change"] for imp in pending)

    def test_run_and_save_no_result_returns_none(self, audit_db_path):
        from audit.watcher_ai_auditor import AIAuditor as WatcherAIAuditor
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)
        auditor = WatcherAIAuditor(repo=repo)
        result = auditor.run_and_save("2026-06-01")
        assert result is None


# ================================================================
#  6. AuditPipeline — 审计管线
# ================================================================


class TestAuditPipeline:
    """审计管线：规则审计 → AI 审计 → 入库 → 推送。"""

    def test_run_strategy_calls_rule_then_ai(self):
        from audit.audit_pipeline import AuditPipeline

        rule = MagicMock()
        rule.audit.return_value = [{"type": "factor_misleading", "severity": "P1"}]

        ai = MagicMock()
        ai.review.return_value = {
            "improvements": [
                {"improvement_type": "prompt_tune", "suggested_change": "加量能提醒"}
            ],
            "lessons": [{"type": "factor_tuning", "content": "动量因子需调整"}],
        }

        pipeline = AuditPipeline("strategy", rule, ai)
        result = pipeline.run("2026-06-01", push=False)

        assert len(result["findings"]) == 1
        assert result["findings"][0]["type"] == "factor_misleading"
        assert len(result["improvements"]) == 1
        assert len(result["lessons"]) == 1
        rule.audit.assert_called_once_with("2026-06-01")
        ai.review.assert_called_once_with(
            [{"type": "factor_misleading", "severity": "P1"}],
            {"date": "2026-06-01", "domain": "strategy"},
        )

    def test_run_watcher_same_flow(self):
        from audit.audit_pipeline import AuditPipeline

        rule = MagicMock()
        rule.audit.return_value = [{"finding_type": "buy_bad", "severity": "P1"}]

        ai = MagicMock()
        ai.review.return_value = {
            "improvements": [{"improvement_type": "param_tune"}],
            "lessons": [],
        }

        pipeline = AuditPipeline("watcher", rule, ai)
        result = pipeline.run("2026-06-01", push=False)

        assert len(result["findings"]) == 1
        assert len(result["improvements"]) == 1
        ai.review.assert_called_once()

    def test_run_no_findings_still_calls_ai_with_empty(self):
        """管线仍调用 AI，AI 拿到空发现后返回空结果。"""
        from audit.audit_pipeline import AuditPipeline

        rule = MagicMock()
        rule.audit.return_value = []

        ai = MagicMock()
        ai.review.return_value = {"improvements": [], "lessons": []}

        pipeline = AuditPipeline("strategy", rule, ai)
        result = pipeline.run("2026-06-01", push=False)

        assert result["findings"] == []
        assert result["improvements"] == []
        assert result["lessons"] == []
        # 代码中 AI 步骤始终执行（即使无发现），AI 收到空 list
        ai.review.assert_called_once_with(
            [], {"date": "2026-06-01", "domain": "strategy"}
        )

    def test_run_ai_only_skips_rule(self):
        from audit.audit_pipeline import AuditPipeline

        rule = MagicMock()
        ai = MagicMock()
        ai.review.return_value = {
            "improvements": [{"type": "prompt_tune"}],
            "lessons": [],
        }

        pipeline = AuditPipeline("watcher", rule, ai)
        result = pipeline.run("2026-06-01", push=False, ai_only=True)

        assert result["findings"] == []
        rule.audit.assert_not_called()

    def test_run_rule_only_skips_ai(self):
        from audit.audit_pipeline import AuditPipeline

        rule = MagicMock()
        rule.audit.return_value = [{"type": "test"}]

        pipeline = AuditPipeline("strategy", rule, None)
        result = pipeline.run("2026-06-01", push=False, rule_only=True)

        assert result["improvements"] == []
        assert result["lessons"] == []

    def test_run_saves_findings_to_db(self, audit_db_path):
        from audit.audit_pipeline import AuditPipeline
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)
        rule = MagicMock()
        # finding 的 key 需与 audit_findings 表列名一致
        rule.audit.return_value = [
            {
                "finding_type": "factor_misleading",
                "severity": "P1",
                "pattern_desc": "动量因子反向",
                "evidence": '{"factor": "momentum"}',
            }
        ]
        ai = MagicMock()
        ai.review.return_value = {"improvements": [], "lessons": []}

        pipeline = AuditPipeline("strategy", rule, ai, repo=repo)
        pipeline.run("2026-06-01", push=False)

        saved = repo.get_audit_findings("2026-06-01")
        assert len(saved) >= 1

    def test_run_saves_improvements_to_db(self, audit_db_path):
        from audit.audit_pipeline import AuditPipeline
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)
        rule = MagicMock()
        rule.audit.return_value = [
            {
                "type": "factor_misleading",
                "severity": "P1",
                "pattern_desc": "test",
                "evidence": "{}",
            }
        ]
        ai = MagicMock()
        ai.review.return_value = {
            "improvements": [
                {
                    "improvement_type": "param_tune",
                    "target_module": "factors",
                    "suggested_change": "调阈值",
                    "rationale": "提高胜率",
                },
                {
                    "improvement_type": "rule_add",
                    "target_module": "filters",
                    "suggested_change": "加过滤",
                    "rationale": "减少噪音",
                },
            ],
            "lessons": [],
        }

        pipeline = AuditPipeline("strategy", rule, ai, repo=repo)
        pipeline.run("2026-06-01", push=False)

        pending = repo.get_pending_watcher_improvements()
        assert len(pending) == 2

    def test_push_to_telegram_integration(self, monkeypatch):
        """当 TELEGRAM_REPORT_CHAT_ID 有值时验证 MessageSender 被调用。"""
        from audit.audit_pipeline import TELEGRAM_REPORT_CHAT_ID, AuditPipeline

        if not TELEGRAM_REPORT_CHAT_ID:
            pytest.skip("环境未配置 TELEGRAM_REPORT_CHAT_ID，跳过推送集成测试")
        mock_sender = MagicMock()
        monkeypatch.setattr(
            "audit.audit_pipeline.MessageSender", lambda **kw: mock_sender
        )

        rule = MagicMock()
        rule.audit.return_value = [{"type": "factor_misleading", "severity": "P1"}]
        ai = MagicMock()
        ai.review.return_value = {
            "improvements": [
                {"improvement_type": "prompt_tune", "suggested_change": "test"}
            ],
            "lessons": [],
        }

        pipeline = AuditPipeline("strategy", rule, ai)
        pipeline.run("2026-06-01", push=True)

        mock_sender.send.assert_called_once()
        sent = mock_sender.send.call_args[0][0]
        assert "strategy审计" in sent
        assert "2026-06-01" in sent
        assert "1 条" in sent

    def test_push_skipped_when_no_chat_id(self, monkeypatch):
        """TELEGRAM_REPORT_CHAT_ID 为空时不推送。"""
        import audit.audit_pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "TELEGRAM_REPORT_CHAT_ID", "")
        monkeypatch.setattr(pipeline_mod, "TELEGRAM_REPORT_BOT_TOKEN", "")
        mock_sender = MagicMock()
        monkeypatch.setattr(
            "audit.audit_pipeline.MessageSender", lambda **kw: mock_sender
        )

        rule = MagicMock()
        rule.audit.return_value = [{"type": "test"}]
        ai = MagicMock()
        ai.review.return_value = {"improvements": [], "lessons": []}

        pipeline = pipeline_mod.AuditPipeline("strategy", rule, ai)
        pipeline.run("2026-06-01", push=True)

        mock_sender.send.assert_not_called()


# ================================================================
#  7. DecisionLogger — 决策日志
# ================================================================


class TestDecisionLogger:
    """决策日志记录 — 写入与查询。"""

    def test_log_decision_creates_record(self, audit_db_path):
        from audit.watcher_decision_logger import DecisionLoggerMixin
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)

        # 用独立类混合 DecisionLoggerMixin
        class TestWatcher(DecisionLoggerMixin):
            def __init__(self, repo, trade_date):
                self.repo = repo
                self._trade_date = trade_date

        watcher = TestWatcher(repo, "2026-06-01")
        watcher._log_decision("buy_trigger", "600001", price=10.5, position_size=1000)

        logs = repo.get_decision_logs("2026-06-01")
        assert len(logs) == 1
        assert logs[0]["decision_type"] == "buy_trigger"
        assert logs[0]["stock_code"] == "600001"

    def test_log_multiple_decisions(self, audit_db_path):
        from audit.watcher_decision_logger import DecisionLoggerMixin
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)

        class TestWatcher(DecisionLoggerMixin):
            def __init__(self, repo, trade_date):
                self.repo = repo
                self._trade_date = trade_date

        watcher = TestWatcher(repo, "2026-06-02")
        watcher._log_decision(
            "regime_change", None, pattern="normal", confidence="high"
        )
        watcher._log_decision("buy_trigger", "600001", price=10.0)
        watcher._log_decision("stop_trigger", "600001", trigger_price=9.5)

        logs = repo.get_decision_logs("2026-06-02")
        assert len(logs) == 3

    def test_query_by_trade_date_and_decision_type(self, audit_db_path):
        from audit.watcher_decision_logger import DecisionLoggerMixin
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)

        class TestWatcher(DecisionLoggerMixin):
            def __init__(self, repo, trade_date):
                self.repo = repo
                self._trade_date = trade_date

        watcher = TestWatcher(repo, "2026-06-03")
        watcher._log_decision("regime_change", None, pattern="v_reversal")
        watcher._log_decision("buy_trigger", "600001", price=10.0)
        watcher._log_decision("stop_trigger", "600001", trigger_price=9.5)

        # 按日期 + 类型查询
        buy_logs = repo.get_decision_logs("2026-06-03", decision_type="buy_trigger")
        assert len(buy_logs) == 1
        assert buy_logs[0]["decision_type"] == "buy_trigger"

        regime_logs = repo.get_decision_logs(
            "2026-06-03", decision_type="regime_change"
        )
        assert len(regime_logs) == 1

        stop_logs = repo.get_decision_logs("2026-06-03", decision_type="stop_trigger")
        assert len(stop_logs) == 1

    def test_convenience_methods(self, audit_db_path):
        """便捷方法（_log_regime_change, _log_buy_trigger 等）正确写入。"""
        from audit.watcher_decision_logger import DecisionLoggerMixin
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)

        class TestWatcher(DecisionLoggerMixin):
            def __init__(self, repo, trade_date):
                self.repo = repo
                self._trade_date = trade_date

        watcher = TestWatcher(repo, "2026-06-04")

        watcher._log_regime_change(
            pattern="v_reversal",
            confidence="high",
            prev_pattern="normal",
            index_price=3100.0,
            index_change=-1.5,
            up_count=300,
            down_count=1800,
            top_sectors=["银行"],
            worst_sectors=["地产"],
        )
        watcher._log_buy_trigger(
            signal_id=1,
            stock_code="600001",
            price=10.0,
            buy_min=9.8,
            buy_max=10.2,
            position_size=1000,
            entry_rule="trend_follow",
            sector_trend="up",
            market_regime="normal",
        )
        watcher._log_buy_filter(
            signal_id=2,
            stock_code="600002",
            entry_rule="breakout",
            reason_filtered="量能不足",
            price=20.0,
            buy_min=19.5,
            buy_max=20.5,
        )

        logs = repo.get_decision_logs("2026-06-04")
        types = [log["decision_type"] for log in logs]
        assert "regime_change" in types
        assert "buy_trigger" in types
        assert "buy_filter" in types

    def test_logger_does_not_crash_on_error(self, audit_db_path):
        """日志写入异常不阻断主流程。"""
        from audit.watcher_decision_logger import DecisionLoggerMixin

        class BadRepo:
            def insert_decision_log(self, **kwargs):
                raise RuntimeError("DB error")

        class TestWatcher(DecisionLoggerMixin):
            def __init__(self, repo, trade_date):
                self.repo = repo
                self._trade_date = trade_date

        watcher = TestWatcher(BadRepo(), "2026-06-01")
        # 不应抛异常
        watcher._log_decision("buy_trigger", "600001", price=10.0)


# ================================================================
#  8. ImprovementApplier — 改进建议应用
# ================================================================


class TestStrategyImprovementApplier:
    """策略改进建议应用器 — apply / list_pending。"""

    def test_apply_updates_status(self, audit_db_path):
        from audit.strategy_improvement import ImprovementApplier
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)
        # 先插入一条 pending 改进
        imp_id = repo.insert_improvement(
            {
                "push_date": "2026-06-01",
                "improvement_type": "prompt_tune",
                "target_module": "prompts",
                "suggested_change": "加量能提醒",
                "rationale": "减少假突破",
                "evidence_ids": "[]",
            }
        )

        applier = ImprovementApplier(db_path=audit_db_path)
        success = applier.apply(imp_id)

        assert success is True
        pending = repo.get_pending_improvements()
        assert not any(i["id"] == imp_id for i in pending)

    def test_list_pending_returns_only_pending(self, audit_db_path):
        from audit.strategy_improvement import ImprovementApplier
        from data.repo import TradeRepository

        repo = TradeRepository(db_path=audit_db_path)
        repo.insert_improvement(
            {
                "push_date": "2026-06-01",
                "improvement_type": "prompt_tune",
                "target_module": "prompts",
                "suggested_change": "改 prompt",
                "rationale": "提高准确率",
                "evidence_ids": "[]",
            }
        )
        repo.insert_improvement(
            {
                "push_date": "2026-06-02",
                "improvement_type": "data_add",
                "target_module": "data",
                "suggested_change": "加数据源",
                "rationale": "更多信息",
                "evidence_ids": "[]",
            }
        )
        # 应用其中一个
        pending_all = repo.get_pending_improvements()
        repo.apply_improvement(pending_all[0]["id"])

        applier = ImprovementApplier(db_path=audit_db_path)
        pending = applier.list_pending()

        assert len(pending) == 1
        assert pending[0]["improvement_type"] == "data_add"

    def test_apply_nonexistent_returns_false(self, audit_db_path):
        from audit.strategy_improvement import ImprovementApplier

        applier = ImprovementApplier(db_path=audit_db_path)
        success = applier.apply(999)
        assert success is False


class TestWatcherImprovementApplier:
    """盯盘改进建议应用器 — apply / _get_improvement / format_improvement_card。"""

    def _seed_pending(self, db_path: str) -> int:
        """插入一条 pending 的 watcher 改进，返回 id。"""
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "INSERT INTO watcher_improvements "
            "(trade_date, improvement_type, target_module, target_param, "
            "suggested_change, code_diff, rationale, evidence_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "2026-06-01",
                "rule_add",
                "buy_decision",
                "check_volume",
                "加量能过滤规则",
                "diff --git a/...",
                "减少假突破买入",
                "[]",
            ),
        )
        conn.commit()
        imp_id = cursor.lastrowid
        conn.close()
        return imp_id

    def test_apply_updates_status(self, audit_db_path):
        from audit.watcher_improvement import ImprovementApplier
        from data.repo.audit_repo import AuditRepo

        imp_id = self._seed_pending(audit_db_path)
        repo = AuditRepo(db_path=audit_db_path)
        applier = ImprovementApplier(repo=repo)

        result = applier.apply(imp_id)

        assert "已标记为 applied" in result
        pending = repo.get_pending_watcher_improvements()
        assert not any(i["id"] == imp_id for i in pending)

    def test_apply_nonexistent_returns_not_found(self, audit_db_path):
        from audit.watcher_improvement import ImprovementApplier
        from data.repo.audit_repo import AuditRepo

        repo = AuditRepo(db_path=audit_db_path)
        applier = ImprovementApplier(repo=repo)

        result = applier.apply(999)
        assert "未找到" in result

    def test_apply_with_code_diff_returns_diff_text(self, audit_db_path):
        from audit.watcher_improvement import ImprovementApplier
        from data.repo.audit_repo import AuditRepo

        imp_id = self._seed_pending(audit_db_path)
        repo = AuditRepo(db_path=audit_db_path)
        applier = ImprovementApplier(repo=repo)

        result = applier.apply(imp_id)
        assert "手动执行" in result
        assert "```diff" in result

    def test_get_improvement_returns_pending_only(self, audit_db_path):
        from audit.watcher_improvement import ImprovementApplier
        from data.repo.audit_repo import AuditRepo

        imp_id = self._seed_pending(audit_db_path)
        repo = AuditRepo(db_path=audit_db_path)
        applier = ImprovementApplier(repo=repo)

        # 应用前可查询
        imp = applier._get_improvement(imp_id)
        assert imp is not None
        assert imp["improvement_type"] == "rule_add"

        # 应用后再查询应返回 None
        applier.apply(imp_id)
        imp_after = applier._get_improvement(imp_id)
        assert imp_after is None

    def test_format_improvement_card(self, audit_db_path):
        from audit.watcher_improvement import (
            ImprovementApplier,
            format_improvement_card,
        )
        from data.repo.audit_repo import AuditRepo

        imp_id = self._seed_pending(audit_db_path)
        repo = AuditRepo(db_path=audit_db_path)
        applier = ImprovementApplier(repo=repo)

        imp = applier._get_improvement(imp_id)
        assert imp is not None

        card = format_improvement_card(imp)
        assert isinstance(card, str)
        assert "改进" in card
        assert str(imp_id) in card
        assert "加量能过滤规则" in card
        assert "减少假突破买入" in card
        assert "```diff" in card

    def test_format_imp_card_without_code_diff(self, audit_db_path):
        from audit.watcher_improvement import format_improvement_card

        imp = {
            "id": 1,
            "improvement_type": "param_tune",
            "target_module": "position_risk",
            "target_param": "stop_loss_pct",
            "suggested_change": "止损阈值从5%改为3%",
            "rationale": "减少假止损",
        }
        card = format_improvement_card(imp)
        assert isinstance(card, str)
        assert "改进" in card
