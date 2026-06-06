"""CLI 入口集成测试 — 参数解析、PID 文件加锁、命令分发。

测试策略：
- 简单参数解析（帮助、命令列表、日期验证）→ 子进程执行
- 命令分发逻辑（PID 文件、审计路由、采集过滤等）→ 直接导入命令行函数 + mock
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ============================================================
# 测试辅助
# ============================================================

MAIN_PY = str(Path(__file__).parent.parent / "main.py")


def _base_env():
    """最小测试环境变量，避免网络 / API 调用。"""
    return {
        "AI_MODEL": "test-model",
        "TELEGRAM_REPORT_BOT_TOKEN": "none",
        "TELEGRAM_CHAT_ID": "",
        "TELEGRAM_REPORT_CHAT_ID": "",
        "DASHSCOPE_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "PROXY_ENABLED": "false",
        "TRADING_DB_PATH": "/tmp/test_cli_nonexistent.db",
    }


def run_cli(*args, env=None, expected_code=None):
    """在子进程中运行 CLI 命令。"""
    full_env = {**os.environ, **_base_env()}
    if env:
        full_env.update(env)
    result = subprocess.run(
        [sys.executable, MAIN_PY, *args],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
    )
    if expected_code is not None:
        assert result.returncode == expected_code, (
            f"Exit code: expected {expected_code}, got {result.returncode}\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )
    return result


# ============================================================
# 1. 帮助输出
# ============================================================


class TestHelpOutput:
    """--help 或无参数时的帮助信息输出"""

    def test_no_args_shows_usage(self):
        """无参数 → exit(1) + 用法信息"""
        r = run_cli(expected_code=1)
        assert "Usage: python main.py <command> [options]" in r.stdout
        assert "Commands:" in r.stdout

    def test_help_flag_handled_as_unknown(self):
        """--help 不是注册命令 → Unknown, exit(0)"""
        r = run_cli("--help")
        assert "Unknown: --help" in r.stdout


# ============================================================
# 2. 命令列表
# ============================================================


class TestCommandList:
    """无参数 / 未知命令显示 COMMANDS"""

    def test_no_args_shows_commands(self):
        """无参数 → COMMANDS 列表"""
        r = run_cli(expected_code=1)
        assert "Commands:" in r.stdout
        for cmd in ("review", "morning", "strategy", "monitor", "collect", "audit"):
            assert cmd in r.stdout

    def test_unknown_command(self):
        """未知命令 → Unknown"""
        r = run_cli("bogus-xyz-999")
        assert "Unknown: bogus-xyz-999" in r.stdout


# ============================================================
# 3. 日期验证
# ============================================================


class TestDateValidation:
    """strategy 命令的日期参数验证"""

    def test_invalid_month_semantic_fails(self):
        """月份 13 通过格式检查，但后续管线会因 DB 失败 → exit(1)"""
        r = run_cli("strategy", "2026-13-01")
        # 格式 ^\d{4}-\d{2}-\d{2}$ 通过，管线后续失败
        assert r.returncode != 0

    def test_abc_rejected(self):
        """非日期字符串 → exit(1) + 日期格式无效"""
        r = run_cli("strategy", "abc", expected_code=1)
        assert "日期格式无效" in (r.stdout + r.stderr)

    def test_valid_date_accepted(self):
        """有效日期 2026-06-06 → 不报日期格式错误"""
        r = run_cli("strategy", "2026-06-06")
        assert "日期格式无效" not in (r.stdout + r.stderr)


# ============================================================
# 4-5. PID 文件
# ============================================================


class TestPidFile:
    """PID 文件防多实例机制（直接导入 + mock 避免真实设备依赖）"""

    def test_old_dead_pid_cleaned_and_removed_on_exit(self, tmp_path):
        """已死进程的 PID 文件 → 清理旧 PID → 退出时删除"""
        storage = tmp_path / "storage"
        storage.mkdir()
        pid_file = storage / "watcher.pid"
        pid_file.write_text("999999")

        with patch("system.config.settings.PROJECT_ROOT", tmp_path):
            with patch("system.message.MessageSender"):
                with patch("data.collect.live.quotes.QuoteClient"):
                    with patch("trade.core.watcher.Watcher") as mw:
                        mw.return_value.run.side_effect = KeyboardInterrupt

                        import main

                        main.sys.argv = ["main.py", "monitor"]
                        main.cmd_monitor()

        assert not pid_file.exists(), "退出后 PID 文件应被删除"

    def test_valid_pid_rejects_second_start(self, tmp_path):
        """当前进程 PID 已存在 → exit(1) + 拒绝启动"""
        storage = tmp_path / "storage"
        storage.mkdir()
        pid_file = storage / "watcher.pid"
        pid_file.write_text(str(os.getpid()))

        with patch("system.config.settings.PROJECT_ROOT", tmp_path):
            with patch("system.message.MessageSender"):
                with patch("data.collect.live.quotes.QuoteClient"):
                    with patch("trade.core.watcher.Watcher"):
                        import main

                        main.sys.argv = ["main.py", "monitor"]
                        with pytest.raises(SystemExit) as exc:
                            main.cmd_monitor()
                        assert exc.value.code == 1

        assert pid_file.read_text().strip() == str(os.getpid()), "原 PID 文件应保留"


# ============================================================
# 6. 审计域路由
# ============================================================


class TestAuditDomainParsing:
    """--domain 参数路由到正确的审计器"""

    def test_domain_strategy(self):
        """--domain strategy → AuditPipeline(domain='strategy', ...)"""
        with patch("audit.audit_pipeline.MessageSender"):
            with patch("system.ai.ai"):
                with patch("sys.argv", ["main.py", "audit", "--domain", "strategy"]):
                    with patch("audit.AuditPipeline") as mock_pipeline:
                        with patch("data.repo.TradeRepository"):
                            import main

                            main.cmd_audit()

        mock_pipeline.assert_called_once()
        assert mock_pipeline.call_args[0][0] == "strategy"

    def test_domain_watcher_default(self):
        """默认（无 --domain）→ AuditPipeline(domain='watcher', ...)"""
        with patch("audit.audit_pipeline.MessageSender"):
            with patch("system.ai.ai"):
                with patch("sys.argv", ["main.py", "audit"]):
                    with patch("audit.AuditPipeline") as mock_pipeline:
                        with patch("data.repo.TradeRepository"):
                            import main

                            main.cmd_audit()

        mock_pipeline.assert_called_once()
        assert mock_pipeline.call_args[0][0] == "watcher"

    def test_domain_invalid(self):
        """--domain invalid → 走 else 分支（默认 watcher 审计器）"""
        with patch("audit.audit_pipeline.MessageSender"):
            with patch("system.ai.ai"):
                with patch("sys.argv", ["main.py", "audit", "--domain", "invalid"]):
                    with patch("audit.AuditPipeline") as mock_pipeline:
                        with patch("data.repo.TradeRepository"):
                            import main

                            main.cmd_audit()

        mock_pipeline.assert_called_once()
        assert mock_pipeline.call_args[0][0] == "watcher"


# ============================================================
# 7. 审计 --list / --apply
# ============================================================


class TestAuditApplyAndList:
    """审计命令的 --list 和 --apply 参数"""

    def test_list_shows_pending_improvements(self):
        """--list → 调用 list_pending"""
        fake_improvements = [
            {
                "id": 1,
                "improvement_type": "rule",
                "suggested_change": "test change",
            }
        ]
        with patch("audit.audit_pipeline.MessageSender"):
            with patch("system.ai.ai"):
                with patch("sys.argv", ["main.py", "audit", "--list"]):
                    with patch(
                        "audit.list_pending", return_value=fake_improvements
                    ) as mock_list:
                        with patch("audit.apply_improvement"):
                            with patch("audit.AuditPipeline"):
                                with patch("data.repo.TradeRepository"):
                                    import main

                                    main.cmd_audit()

        assert mock_list.call_count == 2  # 循环 + not 检查各一次

    def test_list_empty_shows_message(self):
        """--list 无待处理 → 显示无待处理消息"""
        with patch("audit.audit_pipeline.MessageSender"):
            with patch("system.ai.ai"):
                with patch("sys.argv", ["main.py", "audit", "--list"]):
                    with patch("audit.list_pending", return_value=[]) as mock_list:
                        with patch("audit.apply_improvement"):
                            with patch("audit.AuditPipeline"):
                                with patch("data.repo.TradeRepository"):
                                    import main

                                    main.cmd_audit()

        # list_pending 被调用两次：一次 for 循环，一次判断是否空
        assert mock_list.call_count == 2

    def test_apply_calls_apply_improvement(self):
        """--apply N → 调用 apply_improvement(repo, N)"""
        with patch("audit.audit_pipeline.MessageSender"):
            with patch("system.ai.ai"):
                with patch("sys.argv", ["main.py", "audit", "--apply", "3"]):
                    with patch(
                        "audit.apply_improvement", return_value="applied #3"
                    ) as mock_apply:
                        with patch("audit.list_pending"):
                            with patch("audit.AuditPipeline"):
                                with patch("data.repo.TradeRepository"):
                                    import main

                                    main.cmd_audit()

        mock_apply.assert_called_once()
        # apply_improvement(repo, 3) → 第二个参数为 3
        assert mock_apply.call_args[0][1] == 3


# ============================================================
# 8. 采集模块过滤
# ============================================================


class TestCollectSpecificModules:
    """collect 命令的 --module 过滤"""

    @patch("system.config.trading_calendar.is_trading_day", return_value=True)
    def test_collect_without_filter(self, mock_trading):
        """无 --module → import_module 被调用 16 次"""
        with patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock()

            import main

            main.sys.argv = ["main.py", "collect"]
            main.cmd_collect()

        assert mock_import.call_count == 16

    @patch("system.config.trading_calendar.is_trading_day", return_value=True)
    def test_collect_market_filter(self, mock_trading):
        """--module market → 只 import 6 个行情采集器"""
        with patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock()

            import main

            main.sys.argv = ["main.py", "collect", "--module", "market"]
            main.cmd_collect()

        assert mock_import.call_count == 6
        for call_args in mock_import.call_args_list:
            module_path = call_args[0][0]
            assert module_path.startswith("data.collect.market."), (
                f"非 market 采集器被调用: {module_path}"
            )

    @patch("system.config.trading_calendar.is_trading_day", return_value=True)
    def test_collect_events_filter(self, mock_trading):
        """--module events → 只 import 8 个事件采集器"""
        with patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock()

            import main

            main.sys.argv = ["main.py", "collect", "--module", "events"]
            main.cmd_collect()

        assert mock_import.call_count == 8
        for call_args in mock_import.call_args_list:
            module_path = call_args[0][0]
            assert module_path.startswith("data.collect.events."), (
                f"非 events 采集器被调用: {module_path}"
            )

    @patch("system.config.trading_calendar.is_trading_day", return_value=True)
    def test_collect_macro_filter(self, mock_trading):
        """--module macro → 只 import 1 个宏观采集器"""
        with patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock()

            import main

            main.sys.argv = ["main.py", "collect", "--module", "macro"]
            main.cmd_collect()

        assert mock_import.call_count == 1

    @patch("system.config.trading_calendar.is_trading_day", return_value=False)
    def test_collect_non_trading_day(self, mock_trading):
        """非交易日 → 跳过采集，不 import 任何模块"""
        with patch("importlib.import_module") as mock_import:
            import main

            main.sys.argv = ["main.py", "collect"]
            main.cmd_collect()

        mock_import.assert_not_called()


# ============================================================
# 9. review --analyze-only
# ============================================================


class TestReviewSubcommands:
    """review 命令的 --analyze-only 标志"""

    def test_analyze_only_flag(self):
        """--analyze-only → generate_and_send(analyze_only=True)"""
        with patch("sys.argv", ["main.py", "review", "--analyze-only"]):
            with patch("review.review_service.ReviewService") as mock_rs:
                mock_rs.return_value.generate_and_send.return_value = False

                import main

                main.cmd_review()

        mock_rs.return_value.generate_and_send.assert_called_once_with(
            analyze_only=True
        )

    def test_review_no_flag(self):
        """无 --analyze-only → generate_and_send(analyze_only=False)"""
        with patch("sys.argv", ["main.py", "review"]):
            with patch("review.review_service.ReviewService") as mock_rs:
                mock_rs.return_value.generate_and_send.return_value = False

                import main

                main.cmd_review()

        mock_rs.return_value.generate_and_send.assert_called_once_with(
            analyze_only=False
        )

    def test_review_calls_strategy_on_success(self):
        """generate_and_send 返回 True → 接着调用 cmd_strategy"""
        with patch("sys.argv", ["main.py", "review"]):
            with patch("review.review_service.ReviewService") as mock_rs:
                mock_rs.return_value.generate_and_send.return_value = True

                import main

                with patch.object(main, "cmd_strategy") as mock_strategy:
                    main.cmd_review()

        mock_strategy.assert_called_once()

    def test_review_no_strategy_on_failure(self):
        """generate_and_send 返回 False → 不调用 cmd_strategy"""
        with patch("sys.argv", ["main.py", "review"]):
            with patch("review.review_service.ReviewService") as mock_rs:
                mock_rs.return_value.generate_and_send.return_value = False

                import main

                with patch.object(main, "cmd_strategy") as mock_strategy:
                    main.cmd_review()

        mock_strategy.assert_not_called()


# ============================================================
# 10. morning 命令
# ============================================================


class TestMorningDate:
    """morning 命令正常初始化"""

    def test_morning_runs_without_date(self):
        """morning 命令不带参数时正常执行"""
        with patch("system.message.MessageSender"):
            with patch("strategy.morning.MorningBrief") as mock_mb:
                mock_mb.return_value.generate_and_send.return_value = None

                import main

                main.sys.argv = ["main.py", "morning"]
                main.cmd_morning()

        mock_mb.assert_called_once()
        mock_mb.return_value.generate_and_send.assert_called_once()

    def test_morning_with_date_arg(self):
        """morning 命令带日期参数时正常执行（当前代码不解析日期参数）"""
        with patch("system.message.MessageSender"):
            with patch("strategy.morning.MorningBrief") as mock_mb:
                mock_mb.return_value.generate_and_send.return_value = None

                import main

                main.sys.argv = ["main.py", "morning", "2026-06-06"]
                main.cmd_morning()

        mock_mb.assert_called_once()
        mock_mb.return_value.generate_and_send.assert_called_once()


# ============================================================
# 11. compare 命令
# ============================================================


class TestCompareCommand:
    """compare 命令初始化 OrderComparator"""

    def test_compare_initializes_comparator(self):
        """compare → 创建 OrderComparator 实例"""
        with patch("system.message.MessageSender"):
            with patch("trade.exec.real.comparator.OrderComparator") as mock_oc:
                mock_oc.return_value.compare.return_value = {}
                mock_oc.return_value.format_report.return_value = "test report"

                import main

                main.sys.argv = ["main.py", "compare"]
                main.cmd_compare()

        mock_oc.assert_called_once()
        mock_oc.return_value.compare.assert_called_once()
        mock_oc.return_value.format_report.assert_called_once()


# ============================================================
# 12. portfolio 命令
# ============================================================


class TestPortfolioCommand:
    """portfolio 命令初始化 Portfolio"""

    def test_portfolio_initializes_portfolio(self):
        """portfolio → 创建 Portfolio 实例"""
        with patch("trade.exec.paper.portfolio.Portfolio") as mock_pf:
            mock_pf.return_value.cash = 0.0
            mock_pf.return_value.total_value = 0.0
            mock_pf.return_value.positions = []

            import main

            main.sys.argv = ["main.py", "portfolio"]
            main.cmd_portfolio()

        mock_pf.assert_called_once()


# ============================================================
# 13. track 命令
# ============================================================


class TestTrackCommand:
    """track 命令初始化 StockTracker"""

    def test_track_initializes_tracker(self):
        """track → 创建 StockTracker 实例"""
        with patch("review.tracker.StockTracker") as mock_tracker:
            mock_tracker.return_value.update_daily_data.return_value = None
            mock_tracker.return_value.update_next_day_data.return_value = None
            mock_tracker.return_value.get_statistics.return_value = {
                "total": 0,
                "win_rate": 0.0,
                "avg_return": 0.0,
            }

            import main

            main.sys.argv = ["main.py", "track"]
            main.cmd_track()

        mock_tracker.assert_called_once()
        mock_tracker.return_value.update_daily_data.assert_called_once()
        mock_tracker.return_value.update_next_day_data.assert_called_once()
        mock_tracker.return_value.get_statistics.assert_called_once()
