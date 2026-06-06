"""trading-system CLI 入口"""

import sys

# 全局：绕过 Shadowrocket/Surge/Clash 的 DNS 劫持
# 必须在所有网络请求之前安装
from system.utils.dns_bypass import install as _install_dns_bypass

_install_dns_bypass()

COMMANDS = [
    "review",
    "morning",
    "strategy",
    "monitor",
    "collect",
    "cleanup",
    "portfolio",
    "compare",
    "trade",
    "test",
    "track",
    "listen",
    "qmt-collect",
    "strategy-audit",
    "audit",
    "verify-predictions",
]


def cmd_review():
    """盘后全流程：采集 -> AI分析 -> 报告 -> Telegram推送，成功后自动跑策略管线"""
    import sys

    from analysis.review.service import ReviewService

    analyze_only = "--analyze-only" in sys.argv

    service = ReviewService()
    ok = service.generate_and_send(analyze_only=analyze_only)
    if ok:
        cmd_strategy()


def cmd_morning():
    """盘前简报：隔夜宏观 + 候选池确认 + 推送"""
    from analysis.morning import MorningBrief
    from system.utils.logger import get_task_logger, set_current_task
    from system.message import MessageSender

    set_current_task("morning")
    logger = get_task_logger("morning")

    telegram = None
    try:
        telegram = MessageSender()
    except Exception as e:
        logger.warning(f"Telegram 初始化失败（将只输出日志）: {e}")

    brief = MorningBrief(telegram_bot=telegram)
    brief.generate_and_send()


def cmd_monitor():
    """盘中盯盘 — 拉起 Watcher 进程（PID 文件防多实例）"""
    import atexit
    import os

    # stdout/stderr 重定向必须在 import Watcher 之前（logger 初始化时会捕获 sys.stdout）
    import sys as _sys
    from datetime import datetime as _dt

    from system.config.settings import PROJECT_ROOT

    _log_dir = (
        PROJECT_ROOT / "storage" / "logs" / _dt.now().strftime("%Y-%m-%d") / "tasks"
    )
    _log_dir.mkdir(parents=True, exist_ok=True)
    _monitor_fh = open(  # noqa: SIM115
        str(_log_dir / "monitor.log"), "a", encoding="utf-8", buffering=1
    )
    _sys.stdout = _monitor_fh
    _sys.stderr = _monitor_fh

    from data.live.quotes import QuoteClient
    from system.utils.logger import get_task_logger, set_current_task
    from system.message import MessageSender
    from trade.monitor.watcher import Watcher

    set_current_task("monitor")
    logger = get_task_logger("monitor")

    from contextlib import suppress

    pid_file = str(PROJECT_ROOT / "storage" / "watcher.pid")

    # 使用排他创建模式避免 TOCTOU 竞态
    try:
        fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        try:
            with open(pid_file) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            logger.error(f"盯盘已在运行 (PID {old_pid})，拒绝重复启动")
            print(f"盯盘已在运行 (PID {old_pid})，如确认已停请删除 {pid_file}")
            sys.exit(1)
        except (OSError, ValueError):
            os.remove(pid_file)
            fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            # 旧 PID 文件已清理，继续启动

    def _cleanup():
        with suppress(OSError):
            os.remove(pid_file)

    atexit.register(_cleanup)

    telegram = None
    try:
        telegram = MessageSender()
    except Exception as e:
        logger.warning(f"Telegram 初始化失败（将只输出日志）: {e}")

    qmt_quote = None
    try:
        qmt_quote = QuoteClient()
    except Exception as e:
        logger.warning(f"QMT 行情客户端初始化失败（无实时行情）: {e}")

    watcher = Watcher(
        telegram_bot=telegram,
        qmt_quote=qmt_quote,
    )
    try:
        watcher.run()
    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"盯盘异常退出: {e}")
        raise
    finally:
        _cleanup()


def cmd_collect():
    """数据采集 — 16个采集器，独立 try/except"""
    import sys
    from datetime import datetime

    from system.utils.logger import (
        get_task_logger,
        set_current_task,
    )

    set_current_task("collect")
    logger = get_task_logger("collect")
    trade_date = datetime.now().strftime("%Y-%m-%d")

    from system.config.trading_calendar import is_trading_day

    if not is_trading_day(trade_date):
        logger.info(f"{trade_date} 非交易日，跳过采集")
        return
    logger.info(f"开始数据采集 {trade_date}")

    # Module filter support
    module_filter = None
    if "--module" in sys.argv:
        idx = sys.argv.index("--module")
        if idx + 1 < len(sys.argv):
            module_filter = sys.argv[idx + 1]

    # All collectors: (name, module_path, class_name, category)
    collectors = [
        # Market (行情)
        (
            "stock_basic",
            "data.collectors.market.stock_basic_collector",
            "StockBasicCollector",
            "market",
        ),
        (
            "main_index",
            "data.collectors.market.main_index_collector",
            "MainIndexCollector",
            "market",
        ),
        (
            "industry_board",
            "data.collectors.market.industry_board_collector",
            "IndustryBoardCollector",
            "market",
        ),
        (
            "concept_board",
            "data.collectors.market.concept_board_collector",
            "ConceptBoardCollector",
            "market",
        ),
        (
            "sector_stocks",
            "data.collectors.market.sector_stocks_collector",
            "SectorStocksCollector",
            "market",
        ),
        (
            "suspend_resume",
            "data.collectors.market.suspend_resume_collector",
            "SuspendResumeCollector",
            "market",
        ),
        # News (盘中电报)
        (
            "telegraph",
            "data.collectors.events.telegraph_collector",
            "TelegraphCollector",
            "news",
        ),
        # Events (事件)
        (
            "cls_digest",
            "data.collectors.events.cls_digest_collector",
            "CLSDigestCollector",
            "events",
        ),
        ("lhb", "data.collectors.events.lhb_collector", "LHBCollector", "events"),
        (
            "limit_pool",
            "data.collectors.events.limit_pool_collector",
            "LimitPoolCollector",
            "events",
        ),
        (
            "strong_stock",
            "data.collectors.events.strong_stock_collector",
            "StrongStockCollector",
            "events",
        ),
        (
            "regulatory",
            "data.collectors.events.regulatory_letter_collector",
            "RegulatoryLetterCollector",
            "events",
        ),
        (
            "stock_monitor",
            "data.collectors.events.stock_monitor_collector",
            "StockMonitorCollector",
            "events",
        ),
        (
            "shareholder",
            "data.collectors.events.share_holder_change_collector",
            "ShareHolderChangeCollector",
            "events",
        ),
        (
            "notice",
            "data.collectors.events.notice_collector",
            "NoticeCollector",
            "events",
        ),
        # Macro (宏观)
        ("macro", "data.collectors.macro.macro_collector", "MacroCollector", "macro"),
    ]

    if module_filter:
        collectors = [c for c in collectors if c[3] == module_filter]
        logger.info(f"筛选模块: {module_filter} -> {len(collectors)} 个采集器")

    import importlib

    success = 0
    failed = 0
    for name, module_path, class_name, _category in collectors:
        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            instance = cls()
            instance.fetch_and_save()
            logger.info(f"  [OK] {name}")
            success += 1
        except Exception as e:
            logger.error(f"  [FAIL] {name}: {e}")
            failed += 1

    logger.info(f"采集完成: {success} 成功, {failed} 失败")


def cmd_cleanup():
    """周清理：清理 storage/ 下旧文件 + 清理数据库旧电报"""
    from ops.scripts.cleanup import run

    run()


def cmd_portfolio():
    """持仓查询"""
    from trade.paper.portfolio import Portfolio

    p = Portfolio()
    print(
        f"  现金: {p.cash:.2f}  总资产: {p.total_value:.2f}  持仓数: {len(p.positions)}"
    )


def cmd_strategy():
    """盘前管线：趋势筛选 → AI 分析 → 信号入库"""
    import re

    from analysis.strategy import StrategyPipeline
    from system.utils.logger import get_task_logger, set_current_task
    from system.message import MessageSender

    set_current_task("strategy")
    logger = get_task_logger("strategy")

    trade_date = (
        sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
    )
    if trade_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
        logger.error(f"日期格式无效: {trade_date}，需为 YYYY-MM-DD")
        sys.exit(1)

    telegram = None
    try:
        telegram = MessageSender()
    except Exception as e:
        logger.warning(f"Telegram 初始化失败（将只输出日志）: {e}")

    pipeline = StrategyPipeline(telegram_bot=telegram)
    pipeline.run(trade_date=trade_date)


def cmd_compare():
    """收盘后双线比对：实盘 vs 模拟盘成交"""
    from datetime import datetime

    from system.utils.logger import get_task_logger, set_current_task
    from system.message import MessageSender
    from trade.execution.comparator import OrderComparator

    set_current_task("compare")
    logger = get_task_logger("compare")

    telegram = None
    try:
        telegram = MessageSender()
    except Exception as e:
        logger.warning(f"Telegram 初始化失败（将只输出日志）: {e}")

    trade_date = datetime.now().strftime("%Y-%m-%d")
    comparator = OrderComparator(telegram_bot=telegram)
    report = comparator.compare(trade_date)
    output = comparator.format_report(report)
    logger.info(output)
    print(output)


def cmd_trade():
    """手动录入交易 — 解析用户回复并记录成交"""
    print("[trade] 用法: python main.py trade --text '模拟盘 000001 1000股 12.50'")
    import sys

    from trade.execution.manual import ManualExecutor

    if "--text" in sys.argv:
        idx = sys.argv.index("--text")
        if idx + 1 < len(sys.argv):
            text = sys.argv[idx + 1]
            result = ManualExecutor.parse_reply(text)
            print(f"解析结果: {result}")
    else:
        print("请用 --text 传入消息内容")


def cmd_track():
    """股票追踪：更新当日行情 + 次日表现 + 统计"""
    from datetime import datetime, timedelta

    from analysis.tracker import StockTracker
    from system.utils.logger import get_task_logger, set_current_task

    set_current_task("track")
    logger = get_task_logger("track")

    tracker = StockTracker()
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    tracker.update_daily_data(today)
    tracker.update_next_day_data(yesterday, today)

    stats = tracker.get_statistics()
    logger.info(
        f"总推荐: {stats['total']} | 胜率: {stats['win_rate']:.1f}% | 平均收益: {stats['avg_return']:.2f}%"
    )


def cmd_listen():
    """监听 Telegram 用户回复（前台阻塞运行）。
    由 cron 在 9:00 启动、18:00 停止，不需要手动管理生命周期。
    """
    import time

    from system.utils.logger import get_task_logger, set_current_task
    from system.message import MessageReceiver, MessageSender
    from trade.execution.manual import ManualExecutor

    set_current_task("listen")
    logger = get_task_logger("listen")

    telegram = None
    private_telegram = None
    try:
        telegram = MessageSender()
    except Exception as e:
        logger.warning(f"Telegram 发送初始化失败: {e}")
    try:
        from system.config.settings import TELEGRAM_PRIVATE_CHAT_ID

        if TELEGRAM_PRIVATE_CHAT_ID:
            private_telegram = MessageSender(chat_id=TELEGRAM_PRIVATE_CHAT_ID)
    except Exception:
        pass

    receiver = MessageReceiver()
    executor = ManualExecutor()

    logger.info("开始监听 Telegram 消息")

    try:
        while True:
            updates = receiver.fetch_updates(timeout=30)
            for msg in updates:
                text = msg.get("text", "")
                if not text:
                    continue
                logger.info(f"收到: {msg['user']}: {text}")
                result = executor.handle_user_reply(text)
                if result:
                    reply_text, account = result
                    logger.info(f"回复({account}): {reply_text}")
                    if account == "real" and private_telegram:
                        private_telegram.send(reply_text)
                    elif telegram:
                        telegram.send(reply_text)
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("监听被中断")


def cmd_qmt_collect():
    """QMT 实时数据采集进程 — 独立进程，TCP 推送至 Watcher + DB 容灾"""
    import logging
    import sys as _sys
    from datetime import datetime as _dt

    from data.live.qmt_collector import QMTCollector
    from system.config.settings import PROJECT_ROOT
    from system.utils.logger import get_task_logger, set_current_task

    # stdout/stderr → qmt_collect.log（含所有 logging.getLogger 的输出）
    _log_dir = (
        PROJECT_ROOT / "storage" / "logs" / _dt.now().strftime("%Y-%m-%d") / "tasks"
    )
    _log_dir.mkdir(parents=True, exist_ok=True)
    _collect_fh = open(  # noqa: SIM115
        str(_log_dir / "qmt_collect.log"), "a", encoding="utf-8", buffering=1
    )
    _sys.stdout = _collect_fh
    _sys.stderr = _collect_fh

    # 确保所有 logger 输出流到 stderr（被重定向到 qmt_collect.log）
    logging.basicConfig(
        level=logging.DEBUG,
        stream=_sys.stderr,
        force=True,
        format="%(asctime)s - %(levelname)s - [%(name)s] %(message)s",
    )

    set_current_task("qmt_collect")
    logger = get_task_logger("qmt_collect")

    collector = QMTCollector()
    try:
        collector.run_forever()
    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"QMT Collector 异常退出: {e}")
        raise


def cmd_strategy_audit():
    """选股审计：规则审计 + AI 审计 + 生成改进建议"""
    import sys
    from datetime import datetime, timedelta

    from analysis.audit.ai_auditor import AIAuditor
    from analysis.audit.improvement_applier import ImprovementApplier
    from analysis.audit.rule_auditor import RuleAuditor
    from system.utils.logger import get_task_logger, set_current_task
    from system.message import MessageSender

    set_current_task("strategy_audit")
    logger = get_task_logger("strategy_audit")

    push_date = None
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if args and not args[0].startswith("-"):
        push_date = args[0]
    if not push_date:
        push_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    if "--apply" in sys.argv:
        idx = sys.argv.index("--apply")
        if idx + 1 < len(sys.argv):
            imp_id = int(sys.argv[idx + 1])
            applier = ImprovementApplier()
            ok = applier.apply(imp_id)
            logger.info(f"改进 #{imp_id} 应用{'成功' if ok else '失败或需人工审核'}")
            print(f"改进 #{imp_id} 应用{'成功' if ok else '失败或需人工审核'}")
            return

    if "--list" in sys.argv:
        applier = ImprovementApplier()
        pending = applier.list_pending()
        if not pending:
            print("无待处理改进")
        else:
            for imp in pending:
                print(
                    f"  #{imp['id']} [{imp['improvement_type']}] "
                    f"{imp.get('target_module', '')} — {imp['suggested_change'][:80]}"
                )
        return

    logger.info(f"开始规则审计 push_date={push_date}")
    rule_auditor = RuleAuditor()
    rule_findings = rule_auditor.audit(push_date)
    logger.info(f"规则审计: {len(rule_findings)} 条发现")

    logger.info("开始 AI 审计")
    ai_auditor = AIAuditor()
    result = ai_auditor.audit(push_date, rule_findings)

    bias_count = len(result.get("bias_findings", []))
    omission_count = len(result.get("omission_findings", []))
    improvement_count = len(result.get("improvements", []))
    logger.info(
        f"AI 审计完成: {bias_count} 偏见, {omission_count} 遗漏, {improvement_count} 改进"
    )

    from system.config.settings import (
        TELEGRAM_REPORT_BOT_TOKEN,
        TELEGRAM_REPORT_CHAT_ID,
    )

    if TELEGRAM_REPORT_CHAT_ID and (rule_findings or result.get("improvements")):
        try:
            sender = MessageSender(
                chat_id=TELEGRAM_REPORT_CHAT_ID,
                bot_token=TELEGRAM_REPORT_BOT_TOKEN,
            )
            lines = [f"🔍 选股审计 {push_date}", ""]
            lines.append(f"📊 规则发现: {len(rule_findings)} 条")
            for f in rule_findings:
                sev = f.get("severity", "P2")
                if sev in ("P0", "P1"):
                    lines.append(
                        f"  [{sev}] {f.get('type', '')}: {f.get('evidence', '')[:80]}"
                    )

            if result.get("bias_findings"):
                lines.append("")
                lines.append("🧠 偏见发现:")
                for b in result["bias_findings"]:
                    sev = b.get("severity", "P2")
                    lines.append(
                        f"  [{sev}] {b.get('bias_type', '')}: {b.get('pattern', '')[:80]}"
                    )

            if result.get("omission_findings"):
                lines.append("")
                lines.append("👁️ 遗漏发现:")
                for o in result["omission_findings"]:
                    lines.append(
                        f"  {o.get('signal_type', '')}: {o.get('impact', '')[:80]}"
                    )

            if result.get("improvements"):
                lines.append("")
                lines.append("🔧 改进建议:")
                for i, imp in enumerate(result["improvements"], 1):
                    lines.append(
                        f"  #{i} [{imp.get('type', '')}] {imp.get('target', '')}"
                    )
                    lines.append(f"     {imp.get('suggested_change', '')[:100]}")
                lines.append("")
                lines.append("💡 回复「strategy-audit --apply N」应用改进")
            sender.send("\n".join(lines))
            logger.info("审计结果已推送 Telegram")
        except Exception as e:
            logger.warning(f"审计结果推送失败: {e}")

    print(f"\n📊 选股审计 {push_date}")
    print(f"  规则发现: {len(rule_findings)} 条")
    print(f"  偏见发现: {bias_count} 条")
    print(f"  遗漏发现: {omission_count} 条")
    print(f"  改进建议: {improvement_count} 条")


def cmd_audit():
    """收盘后盯盘自审计：规则审计 + AI 审计 + 改进建议推送"""
    import sys
    from datetime import datetime

    if "--help" in sys.argv:
        print("用法: python main.py audit [选项]")
        print("  --rule-only   仅规则审计")
        print("  --ai-only     仅 AI 审计（需已有 audit_findings）")
        print("  --apply N     应用第 N 条改进建议")
        print("  --list        列出待处理的改进建议")
        return

    from data.repo import TradeRepository
    from system.config.settings import TELEGRAM_REPORT_CHAT_ID

    repo = TradeRepository()
    trade_date = datetime.now().strftime("%Y-%m-%d")

    if "--list" in sys.argv:
        imps = repo.get_pending_watcher_improvements()
        if imps:
            print(f"待处理改进建议 ({len(imps)} 条):")
            for imp in imps:
                print(
                    f"  #{imp['id']} [{imp['improvement_type']}] {imp['suggested_change'][:80]}"
                )
        else:
            print("无待处理改进建议")
        return

    apply_idx = None
    for i, arg in enumerate(sys.argv):
        if arg == "--apply" and i + 1 < len(sys.argv):
            apply_idx = int(sys.argv[i + 1])
            break

    if apply_idx:
        from trade.monitor.audit.watcher_improvement import ImprovementApplier

        applier = ImprovementApplier(repo)
        result = applier.apply(apply_idx)
        print(result)
        try:
            from system.message import MessageSender

            MessageSender(chat_id=TELEGRAM_REPORT_CHAT_ID).send(result)
        except Exception:
            pass
        return

    rule_only = "--rule-only" in sys.argv
    ai_only = "--ai-only" in sys.argv

    if not ai_only:
        from trade.monitor.audit.watcher_rule_auditor import RuleAuditor

        print(f"规则审计 {trade_date} ...")
        rule = RuleAuditor(repo=repo)
        n = len(rule.run_and_save(trade_date))
        print(f"  完成: {n} 条发现")

    if not rule_only:
        from trade.monitor.audit.watcher_ai_auditor import AIAuditor

        print(f"AI 审计 {trade_date} ...")
        ai = AIAuditor(repo=repo)
        result = ai.run_and_save(trade_date)
        if result:
            n_imps = len(result.get("improvements", []))
            print(f"  完成: {n_imps} 条改进建议")
            imps = repo.get_pending_watcher_improvements()
            for imp in imps[-3:]:
                from trade.monitor.audit.watcher_improvement import (
                    format_improvement_card,
                )

                card = format_improvement_card(imp)
                print(card)
                try:
                    from system.message import MessageSender

                    MessageSender(chat_id=TELEGRAM_REPORT_CHAT_ID).send(card)
                except Exception:
                    pass
        else:
            print("  AI 审计无输出")


def cmd_verify_predictions():
    """收盘后核验复盘预测 vs 次日实际市场数据"""
    import sys
    from datetime import datetime

    from analysis.review.prediction_verifier import PredictionVerifier
    from system.config.trading_calendar import get_previous_trading_day
    from system.utils.logger import get_task_logger, set_current_task
    from system.message import MessageSender

    set_current_task("verify_predictions")
    logger = get_task_logger("verify_predictions")

    # 解析 --date 参数
    push_date = None
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    for i, a in enumerate(sys.argv):
        if a == "--date" and i + 1 < len(sys.argv):
            push_date = sys.argv[i + 1]
            break
    if not push_date and args:
        push_date = args[0]
    if not push_date:
        push_date = get_previous_trading_day(datetime.now().strftime("%Y-%m-%d"))

    logger.info(f"开始核验 {push_date} 的预测…")
    verifier = PredictionVerifier()
    report = verifier.verify(push_date)

    if report.get("error"):
        msg = f"❌ 预测核验失败: {report['error']}"
        logger.error(msg)
        print(msg)
        return

    # 汇总输出
    lines = [
        f"📊 预测核验报告 {report['push_date']}（对比 {report['checked_date']} 实际数据）",
        f"核验总数：{report['total']} 条",
        f"  指数：{report['index_count']} 条 | 板块：{report['sector_count']} 条 | 情景：{report['scenario_count']} 条",
        f"✅ 正确：{report['correct']} 条",
        f"❌ 错误：{report['incorrect'] - report['unmatched']} 条",
        f"⚠️ 未匹配：{report['unmatched']} 条",
        f"📈 准确率：{report['accuracy']:.1f}%",
    ]
    summary = "\n".join(lines)
    print(summary)
    logger.info(summary.replace("\n", " | "))

    # 逐条明细
    for detail in report.get("details", []):
        icon = "✅" if detail["is_correct"] else "❌"
        line = f"  {icon} [{detail['pred_type']}] {detail['target_name']}: {detail['actual_result']}"
        logger.info(line)

    if report.get("unmatched_sectors"):
        logger.warning(f"未匹配板块: {', '.join(report['unmatched_sectors'])}")

    # Telegram 推送
    try:
        telegram = MessageSender()
        telegram.send(summary)
        logger.info("✅ 核验报告已推送 Telegram")
    except Exception as e:
        logger.warning(f"Telegram 推送失败: {e}")


def cmd_test():
    print("[test] 配置检查...")
    from system.config.settings import AI_MODEL, DATABASE_PATH, LOGS_DIR

    print(f"  DB: {DATABASE_PATH}")
    print(f"  Logs: {LOGS_DIR}")
    print(f"  AI 模型: {AI_MODEL or '未配置'}")
    print("  OK")


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <command> [options]")
        print(f"Commands: {', '.join(COMMANDS)}")
        sys.exit(1)

    cmd = sys.argv[1]
    {
        "review": cmd_review,
        "morning": cmd_morning,
        "strategy": cmd_strategy,
        "monitor": cmd_monitor,
        "collect": cmd_collect,
        "cleanup": cmd_cleanup,
        "portfolio": cmd_portfolio,
        "compare": cmd_compare,
        "trade": cmd_trade,
        "test": cmd_test,
        "track": cmd_track,
        "listen": cmd_listen,
        "qmt-collect": cmd_qmt_collect,
        "strategy-audit": cmd_strategy_audit,
        "audit": cmd_audit,
        "verify-predictions": cmd_verify_predictions,
    }.get(cmd, lambda: print(f"Unknown: {cmd}"))()


if __name__ == "__main__":
    main()
