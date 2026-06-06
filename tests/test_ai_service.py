"""Mock-based tests for system/ai/ module (AIService, FunctionCallingEngine, prompt templates)."""

import os

# Set env before any system imports so that all module-level settings see the right values
os.environ["AI_MODEL"] = "test-model"
os.environ["DASHSCOPE_API_KEY"] = "test-dashscope-key"
os.environ["DEEPSEEK_API_KEY"] = "test-deepseek-key"

import importlib
import json
import queue
from unittest.mock import MagicMock, patch

import pytest

from system.ai.ai_service import _MODEL_ENV_MAP, AIService, _resolve_model
from system.ai.function_calling import FunctionCallingEngine
from system.ai.stock_tools import TOOLS_DEFINITION

# ═══════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════


@pytest.fixture(autouse=True)
def reset_env():
    """Ensure known env per test, then restore."""
    old_vals = {}
    for key in (
        "AI_MODEL",
        "AI_MODEL_REVIEW",
        "AI_MODEL_SCREENING",
        "AI_MODEL_MORNING",
        "AI_MODEL_WATCHER",
        "AI_MODEL_WATCHER_CHASE",
        "AI_MODEL_WATCHER_SWAP",
        "AI_MODEL_WATCHER_INDEX",
        "AI_MODEL_WATCHER_TRAPPED",
        "AI_MODEL_WATCHER_BREAKOUT",
        "AI_MODEL_AUDIT",
        "DASHSCOPE_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        old_vals[key] = os.environ.get(key)
    os.environ["AI_MODEL"] = "test-model"
    os.environ["DASHSCOPE_API_KEY"] = "test-dashscope-key"
    os.environ["DEEPSEEK_API_KEY"] = "test-deepseek-key"
    yield
    for key, val in old_vals.items():
        if val is not None:
            os.environ[key] = val
        else:
            os.environ.pop(key, None)


@pytest.fixture
def svc():
    """Clean AIService instance for each test."""
    return AIService()


# ═══════════════════════════════════════════════
# 1. AIService model resolution
# ═══════════════════════════════════════════════


@pytest.mark.parametrize(
    "business",
    [
        "review",
        "screening",
        "strategy",
        "morning",
        "watcher",
        "watcher_chase",
        "watcher_swap",
        "watcher_index",
        "watcher_trapped",
        "watcher_breakout",
        "audit",
    ],
)
def test_model_env_map_contains(business):
    assert business in _MODEL_ENV_MAP


def test_model_env_map_length():
    assert len(_MODEL_ENV_MAP) == 11


def test_resolve_model_known_business_returns_model():
    os.environ["AI_MODEL_REVIEW"] = "review-model"
    result = _resolve_model("review")
    assert result == "review-model"


def test_resolve_model_known_business_falls_back_to_global():
    os.environ.pop("AI_MODEL_REVIEW", None)
    result = _resolve_model("review")
    assert result == "test-model"


def test_resolve_model_unknown_name_returns_global():
    result = _resolve_model("nonexistent_business")
    assert result == "test-model"


def test_resolve_model_empty_string_returns_global():
    result = _resolve_model("")
    assert result == "test-model"


def test_resolve_model_actual_model_name_returns_global():
    """Passing an actual model name as business arg does not match any key, falls back."""
    result = _resolve_model("deepseek-v4-pro")
    assert result == "test-model"


def test_resolve_model_no_ai_model_env_falls_to_empty():
    os.environ.pop("AI_MODEL", None)
    result = _resolve_model("unknown")
    assert result == ""


@pytest.mark.parametrize(
    "business,env_key",
    [
        ("review", "AI_MODEL_REVIEW"),
        ("screening", "AI_MODEL_SCREENING"),
        ("watcher_chase", "AI_MODEL_WATCHER_CHASE"),
        ("audit", "AI_MODEL_AUDIT"),
        ("morning", "AI_MODEL_MORNING"),
    ],
)
def test_resolve_model_via_env_key(business, env_key):
    custom = "custom-business-model"
    old = os.environ.get(env_key)
    os.environ[env_key] = custom
    try:
        assert _resolve_model(business) == custom
    finally:
        if old is not None:
            os.environ[env_key] = old
        else:
            os.environ.pop(env_key, None)


# ═══════════════════════════════════════════════
# 2. AIService.chat
# ═══════════════════════════════════════════════


class TestChat:
    """Tests for AIService.chat() — mock _request to avoid real API calls."""

    def test_chat_returns_text(self, svc):
        mock_resp = {"choices": [{"message": {"content": "answer text"}}]}
        with patch.object(svc, "_request", return_value=mock_resp):
            result = svc.chat("hello")
        assert result == "answer text"

    def test_chat_returns_stripped_text(self, svc):
        mock_resp = {"choices": [{"message": {"content": "  spaced answer  "}}]}
        with patch.object(svc, "_request", return_value=mock_resp):
            result = svc.chat("hello")
        assert result == "spaced answer"

    def test_chat_empty_response_returns_empty_string(self, svc):
        mock_resp = {"choices": [{"message": {"content": ""}}]}
        with patch.object(svc, "_request", return_value=mock_resp):
            result = svc.chat("hello")
        assert result == ""

    def test_chat_missing_content_returns_empty_string(self, svc):
        mock_resp = {"choices": [{"message": {}}]}
        with patch.object(svc, "_request", return_value=mock_resp):
            result = svc.chat("hello")
        assert result == ""

    def test_chat_passes_system_prompt_to_build_payload(self, svc):
        with patch.object(svc, "_build_payload", return_value={}) as mock_build:
            with patch.object(
                svc,
                "_request",
                return_value={"choices": [{"message": {"content": "ok"}}]},
            ):
                svc.chat("hello", system_prompt="You are a trader")
        _, kwargs = mock_build.call_args
        assert kwargs.get("system_prompt") == "You are a trader"

    def test_chat_default_system_prompt_is_empty(self, svc):
        with patch.object(svc, "_build_payload", return_value={}) as mock_build:
            with patch.object(
                svc,
                "_request",
                return_value={"choices": [{"message": {"content": "ok"}}]},
            ):
                svc.chat("hello")
        _, kwargs = mock_build.call_args
        assert kwargs.get("system_prompt") == ""

    def test_chat_passes_max_tokens_to_build_payload(self, svc):
        with patch.object(svc, "_build_payload", return_value={}) as mock_build:
            with patch.object(
                svc,
                "_request",
                return_value={"choices": [{"message": {"content": "ok"}}]},
            ):
                svc.chat("hello", max_tokens=500)
        _, kwargs = mock_build.call_args
        assert kwargs.get("max_tokens") == 500

    def test_chat_default_max_tokens_is_1000(self, svc):
        with patch.object(svc, "_build_payload", return_value={}) as mock_build:
            with patch.object(
                svc,
                "_request",
                return_value={"choices": [{"message": {"content": "ok"}}]},
            ):
                svc.chat("hello")
        _, kwargs = mock_build.call_args
        assert kwargs.get("max_tokens") == 1000

    def test_chat_with_model_override_uses_resolved_model(self, svc):
        """model='review' -> _request receives resolved model as first positional arg."""
        os.environ["AI_MODEL_REVIEW"] = "review-model"
        with patch.object(
            svc, "_request", return_value={"choices": [{"message": {"content": "ok"}}]}
        ) as mock_req:
            svc.chat("hello", model="review")
        # _request(self, model_name, payload) — model_name is first positional arg
        args, _ = mock_req.call_args
        assert args[0] == "review-model"

    def test_chat_with_model_override_unknown_falls_back(self, svc):
        """model not in _MODEL_ENV_MAP -> falls to AI_MODEL env."""
        with patch.object(
            svc, "_request", return_value={"choices": [{"message": {"content": "ok"}}]}
        ) as mock_req:
            svc.chat("hello", model="custom_business")
        # Should still complete without error (falls back to AI_MODEL default)
        assert mock_req.called

    def test_chat_uses_default_temperature(self, svc):
        with patch.object(svc, "_build_payload", return_value={}) as mock_build:
            with patch.object(
                svc,
                "_request",
                return_value={"choices": [{"message": {"content": "ok"}}]},
            ):
                svc.chat("hello")
        _, kwargs = mock_build.call_args
        assert kwargs.get("temperature") == 0.6


# ═══════════════════════════════════════════════
# 3. AIService.chat_with_tools
# ═══════════════════════════════════════════════


class TestChatWithTools:
    """Tests for AIService.chat_with_tools() — mock _request + process_tool_calls."""

    def test_chat_with_tools_content_only_returns_text(self, svc):
        mock_resp = {"choices": [{"message": {"content": "direct answer"}}]}
        with patch.object(svc, "_request", return_value=mock_resp):
            result = svc.chat_with_tools(
                [{"role": "user", "content": "hello"}],
                max_rounds=4,
            )
        assert result == "direct answer"

    def test_chat_with_tools_tool_calls_processed(self, svc):
        """Mock two rounds: first returns tool_calls, second returns final answer."""
        with patch.object(
            svc._fc,
            "process_tool_calls",
            return_value=[
                {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
            ],
        ):
            mock_request = MagicMock(
                side_effect=[
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "checking...",
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "function": {
                                                "name": "get_market_cap",
                                                "arguments": '{"stock_code": "688702"}',
                                            },
                                        },
                                    ],
                                }
                            }
                        ]
                    },
                    {"choices": [{"message": {"content": "final result"}}]},
                ]
            )
            with patch.object(svc, "_request", mock_request):
                result = svc.chat_with_tools(
                    [{"role": "user", "content": "check 688702"}],
                    max_rounds=4,
                )
        assert result == "final result"

    def test_chat_with_tools_multi_turn_accumulates_messages(self, svc):
        """After round 1, user + assistant(with tool_calls) + tool msgs are accumulated."""
        with patch.object(
            svc._fc,
            "process_tool_calls",
            return_value=[
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"market_cap": 100}',
                },
            ],
        ):
            mock_request = MagicMock(
                side_effect=[
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": "checking...",
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "function": {
                                                "name": "get_market_cap",
                                                "arguments": '{"stock_code": "688702"}',
                                            },
                                        },
                                    ],
                                }
                            }
                        ]
                    },
                    {"choices": [{"message": {"content": "done"}}]},
                ]
            )
            with patch.object(svc, "_request", mock_request):
                svc.chat_with_tools(
                    [{"role": "user", "content": "check 688702"}],
                    max_rounds=4,
                )
        calls = mock_request.call_args_list
        assert len(calls) == 2
        # Round 1 payload: system + user
        msgs_1 = calls[0][0][1]["messages"]
        assert len(msgs_1) == 2
        assert msgs_1[0]["role"] == "system"
        assert msgs_1[1]["role"] == "user"
        # Round 2 payload: system + user + assistant + tool
        msgs_2 = calls[1][0][1]["messages"]
        assert len(msgs_2) == 4
        assert msgs_2[2]["role"] == "assistant"
        assert msgs_2[3]["role"] == "tool"

    def test_chat_with_tools_empty_first_content_with_tool_calls(self, svc):
        """Assistant returns None/empty content but has tool_calls — should not crash."""
        with patch.object(
            svc._fc,
            "process_tool_calls",
            return_value=[
                {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
            ],
        ):
            mock_request = MagicMock(
                side_effect=[
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call_1",
                                            "function": {
                                                "name": "get_market_cap",
                                                "arguments": '{"stock_code": "688702"}',
                                            },
                                        },
                                    ],
                                }
                            }
                        ]
                    },
                    {"choices": [{"message": {"content": "result after tool"}}]},
                ]
            )
            with patch.object(svc, "_request", mock_request):
                result = svc.chat_with_tools(
                    [{"role": "user", "content": "check"}],
                    max_rounds=4,
                )
        assert result == "result after tool"

    def test_chat_with_tools_max_rounds_exceeded(self, svc):
        """When max_rounds is exhausted and tool_calls still present, return last content."""
        with patch.object(
            svc._fc,
            "process_tool_calls",
            return_value=[
                {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
            ],
        ):
            mock_resp = {
                "choices": [
                    {
                        "message": {
                            "content": "partial answer",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "get_market_cap",
                                        "arguments": '{"stock_code": "688702"}',
                                    },
                                },
                            ],
                        }
                    }
                ]
            }
            with patch.object(svc, "_request", return_value=mock_resp):
                result = svc.chat_with_tools(
                    [{"role": "user", "content": "check"}],
                    max_rounds=1,
                )
        assert result == "partial answer"

    def test_chat_with_tools_raw_returns_dict(self, svc):
        """chat_with_tools_raw returns {content, tool_calls} dict."""
        mock_resp = {"choices": [{"message": {"content": "raw answer"}}]}
        with patch.object(svc, "_request", return_value=mock_resp):
            result = svc.chat_with_tools_raw(
                [{"role": "user", "content": "hello"}],
            )
        assert result == {"content": "raw answer", "tool_calls": []}

    def test_chat_with_tools_raw_with_tool_calls(self, svc):
        mock_resp = {
            "choices": [
                {
                    "message": {
                        "content": "need data",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "test_tool", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
        with patch.object(svc, "_request", return_value=mock_resp):
            result = svc.chat_with_tools_raw(
                [{"role": "user", "content": "check"}],
            )
        assert result["content"] == "need data"
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "c1"


# ═══════════════════════════════════════════════
# 4. AIService.submit (async)
# ═══════════════════════════════════════════════


class TestSubmit:
    """Tests for async submission via AIService.submit/pop/pending."""

    def test_submit_without_worker_returns_false(self, svc):
        assert svc.submit("k", "hello") is False

    def test_submit_with_worker_enqueues(self, svc):
        svc.start_worker()
        try:
            ok = svc.submit("k1", "hello", model="review")
            assert ok is True
            assert svc.qsize == 1
            task = svc._q.get_nowait()
            assert task[0] == "k1"
            assert task[1] == "hello"
            assert task[2] == "review"
        finally:
            svc.stop_worker()

    def test_submit_default_model_is_empty(self, svc):
        svc.start_worker()
        try:
            svc.submit("k2", "world")
            task = svc._q.get_nowait()
            assert task[2] == ""  # default model is empty string
        finally:
            svc.stop_worker()

    def test_pop_returns_none_for_missing_key(self, svc):
        assert svc.pop("nonexistent") is None

    def test_pop_returns_value_and_removes_key(self, svc):
        with svc._lock:
            svc._results["k3"] = "result_text"
        val = svc.pop("k3")
        assert val == "result_text"
        assert svc.pop("k3") is None

    def test_pending_true_when_in_queue(self, svc):
        svc.start_worker()
        try:
            svc.submit("pk", "hello")
            assert svc.pending("pk") is True
        finally:
            svc.stop_worker()

    def test_pending_false_when_in_results(self, svc):
        with svc._lock:
            svc._results["done"] = "ok"
        assert svc.pending("done") is False

    def test_pending_false_for_nonexistent_key(self, svc):
        assert svc.pending("nope") is False

    def test_submit_full_queue_drains_one(self, svc):
        """When queue is full, submit drains one task then inserts the new one."""
        svc._q = queue.Queue(maxsize=1)
        svc.start_worker()
        try:
            svc.submit("first", "hello")
            ok = svc.submit("second", "world")
            assert ok is True
        finally:
            svc.stop_worker()

    def test_qsize_zero_initially(self, svc):
        assert svc.qsize == 0

    def test_worker_processes_task_and_stores_result(self, svc):
        """Integration check: worker calls chat() and stores result."""
        svc.start_worker()
        try:
            mock_resp = {"choices": [{"message": {"content": "worker result"}}]}
            with patch.object(svc, "_request", return_value=mock_resp):
                svc.submit("wk", "test prompt")
                import time

                for _ in range(20):  # wait up to 2s
                    res = svc.pop("wk")
                    if res is not None:
                        break
                    time.sleep(0.1)
                assert res == "worker result"
        finally:
            svc.stop_worker()


# ═══════════════════════════════════════════════
# 5. FunctionCallingEngine
# ═══════════════════════════════════════════════


class TestFunctionCallingEngine:
    """Tests for FunctionCallingEngine dispatch and TOOLS_DEFINITION integrity."""

    def test_tools_definition_count(self):
        assert len(TOOLS_DEFINITION) == 19

    def test_each_tool_has_type_function(self):
        for td in TOOLS_DEFINITION:
            assert td["type"] == "function"

    def test_each_tool_has_required_fields(self):
        for td in TOOLS_DEFINITION:
            func = td["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            params = func["parameters"]
            assert params["type"] == "object"
            assert "properties" in params

    def test_tool_names_are_unique(self):
        names = [td["function"]["name"] for td in TOOLS_DEFINITION]
        assert len(names) == len(set(names))

    def test_process_tool_calls_valid_call_returns_tool_message(self):
        fc = FunctionCallingEngine(db_path=":memory:")
        expected = {"market_cap": 100.5, "name": "TestCorp"}
        with patch.object(fc, "execute_tool", return_value=expected):
            tool_calls = [
                {
                    "id": "call_abc",
                    "function": {
                        "name": "get_market_cap",
                        "arguments": '{"stock_code": "688702"}',
                    },
                }
            ]
            result = fc.process_tool_calls(tool_calls)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "call_abc"
        content = json.loads(result[0]["content"])
        assert content["market_cap"] == 100.5

    def test_process_tool_calls_unknown_function_returns_error(self):
        fc = FunctionCallingEngine(db_path=":memory:")
        tool_calls = [
            {
                "id": "call_xyz",
                "function": {"name": "nonexistent_tool", "arguments": "{}"},
            }
        ]
        result = fc.process_tool_calls(tool_calls)
        assert len(result) == 1
        content = json.loads(result[0]["content"])
        assert "error" in content
        assert "未找到" in content["error"]

    def test_process_tool_calls_malformed_args_does_not_crash(self):
        fc = FunctionCallingEngine(db_path=":memory:")
        with patch.object(fc, "execute_tool", return_value={"result": "ok"}):
            tool_calls = [
                {
                    "id": "call_bad",
                    "function": {
                        "name": "get_market_cap",
                        "arguments": "not valid json{{{",
                    },
                }
            ]
            result = fc.process_tool_calls(tool_calls)
        assert len(result) == 1
        assert result[0]["role"] == "tool"

    def test_process_tool_calls_object_style_attributes(self):
        """Handle object-style tool_calls with attribute access instead of dict."""
        fc = FunctionCallingEngine(db_path=":memory:")
        with patch.object(fc, "execute_tool", return_value={"result": "ok"}):

            class MockFunction:
                name = "get_market_cap"
                arguments = '{"stock_code": "688702"}'

            class MockToolCall:
                id = "call_obj_1"
                function = MockFunction()

            result = fc.process_tool_calls([MockToolCall()])
        assert len(result) == 1
        assert result[0]["tool_call_id"] == "call_obj_1"

    def test_process_tool_calls_multiple_calls(self):
        fc = FunctionCallingEngine(db_path=":memory:")
        with patch.object(fc, "execute_tool", return_value={"result": "ok"}):
            tool_calls = [
                {"id": "c1", "function": {"name": "get_market_cap", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "get_stock_info", "arguments": "{}"}},
            ]
            result = fc.process_tool_calls(tool_calls)
        assert len(result) == 2

    def test_execute_tool_unknown_returns_error_dict(self):
        fc = FunctionCallingEngine(db_path=":memory:")
        result = fc.execute_tool("nonexistent_tool", {})
        assert "error" in result
        assert "未找到" in result["error"]

    def test_get_tools_definition_returns_tools_list(self):
        fc = FunctionCallingEngine(db_path=":memory:")
        result = fc.get_tools_definition()
        assert result is TOOLS_DEFINITION
        assert len(result) == 19


# ═══════════════════════════════════════════════
# 6. Prompt templates
# ═══════════════════════════════════════════════


class TestPromptTemplates:
    """Verify all 7 prompt files are importable and contain expected constants."""

    # Each entry: (module_name, expected_constant_names, expected_keyword)
    PROMPT_MODULES = [
        ("system.ai.prompts.review", ["REVIEW_REPORT_PROMPT"], "复盘"),
        ("system.ai.prompts.morning", ["MORNING_BRIEF_PROMPT"], "校准"),
        ("system.ai.prompts.strategy", ["AI_ADVISOR_PROMPT"], "选股"),
        ("system.ai.prompts.audit", ["AUDIT_SYSTEM", "STRATEGY_AUDIT_PROMPT"], "审计"),
        (
            "system.ai.prompts.watcher",
            [
                "CHASE_OPINION_SYSTEM",
                "CHASE_OPINION_TEMPLATE",
                "SWAP_EVAL_SYSTEM",
                "SWAP_EVAL_TEMPLATE",
                "INDEX_FLUCTUATION_SYSTEM",
                "INDEX_FLUCTUATION_TEMPLATE",
                "BREAKOUT_TEMPLATE",
                "TRAPPED_EXIT_TEMPLATE",
            ],
            "追高",
        ),
        (
            "system.ai.prompts.watcher_audit",
            [
                "WATCHER_AUDIT_SYSTEM",
                "WATCHER_AUDIT_USER",
            ],
            "审计",
        ),
        (
            "system.ai.prompts.telegraph",
            [
                "TELEGRAPH_STRUCTURE_PROMPT",
                "TELEGRAPH_AI_SYSTEM",
                "TELEGRAPH_FC_TOOLS",
            ],
            "电报",
        ),
    ]

    @pytest.mark.parametrize("module_name,constants,_", PROMPT_MODULES)
    def test_module_importable(self, module_name, constants, _):
        mod = importlib.import_module(module_name)
        assert mod is not None

    @pytest.mark.parametrize("module_name,constants,_", PROMPT_MODULES)
    def test_has_expected_constants(self, module_name, constants, _):
        mod = importlib.import_module(module_name)
        for name in constants:
            assert hasattr(mod, name), f"{module_name} missing {name}"

    @pytest.mark.parametrize("module_name,_,expected_keyword", PROMPT_MODULES)
    def test_prompt_contains_keyword(self, module_name, _, expected_keyword):
        """At least one string constant in the module contains the expected keyword."""
        mod = importlib.import_module(module_name)
        found = False
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            val = getattr(mod, attr_name)
            if isinstance(val, str) and expected_keyword in val:
                found = True
                break
        assert found, f"No constant in {module_name} contains '{expected_keyword}'"

    def test_telegraph_fc_tools_count(self):
        from system.ai.prompts.telegraph import TELEGRAPH_FC_TOOLS

        assert len(TELEGRAPH_FC_TOOLS) == 2  # search_stock + search_sector

    def test_watcher_prompt_template_instances(self):
        from system.ai.prompts.watcher import BREAKOUT_TEMPLATE, TRAPPED_EXIT_TEMPLATE

        assert BREAKOUT_TEMPLATE.scenario == "breakout"
        assert TRAPPED_EXIT_TEMPLATE.scenario == "trapped_exit"
        assert BREAKOUT_TEMPLATE.max_tokens == 80
        assert TRAPPED_EXIT_TEMPLATE.max_tokens == 80
        assert (
            "动量交易" in BREAKOUT_TEMPLATE.system_prompt
            or "突破" in BREAKOUT_TEMPLATE.system_prompt
        )
        assert (
            "被套" in TRAPPED_EXIT_TEMPLATE.system_prompt
            or "exit" in TRAPPED_EXIT_TEMPLATE.scenario
        )
