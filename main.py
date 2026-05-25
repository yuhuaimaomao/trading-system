# -*- coding: utf-8 -*-
"""trading-system CLI 入口"""

import sys

COMMANDS = ["review", "morning", "monitor", "collect", "cleanup",
            "portfolio", "trade", "backtest", "test", "track"]


def cmd_review():
    """盘后全流程：采集 -> AI分析 -> 报告 -> Telegram推送"""
    import sys
    from analysis.review.service import ReviewService

    analyze_only = '--analyze-only' in sys.argv

    service = ReviewService()
    service.generate_and_send(analyze_only=analyze_only)

def cmd_morning():
    """盘前简报：隔夜宏观 + 候选池确认 + 推送"""
    from analysis.morning import MorningBrief
    from system.utils.telegram import MessageSender
    from system.utils.logger import set_current_task, get_task_logger

    set_current_task('morning')
    logger = get_task_logger('morning')

    telegram = None
    try:
        telegram = MessageSender()
    except Exception as e:
        logger.warning(f"Telegram 初始化失败（将只输出日志）: {e}")

    brief = MorningBrief(telegram_bot=telegram)
    brief.generate_and_send()

def cmd_monitor():
    """盘中盯盘 — 拉起 Watcher 进程"""
    from trade.monitor.watcher import Watcher
    from data.live.quotes import QuoteClient
    from system.utils.telegram import MessageSender
    from system.utils.logger import set_current_task, get_task_logger

    set_current_task('monitor')
    logger = get_task_logger('monitor')

    telegram = None
    try:
        telegram = MessageSender()
    except Exception as e:
        logger.warning(f"Telegram 初始化失败（将只输出日志）: {e}")

    qmt_quote = None
    try:
        qmt_quote = QuoteClient()
    except Exception as e:
        logger.warning(f"QMT 行情客户端初始化失败（将使用DB收盘价）: {e}")

    watcher = Watcher(
        telegram_bot=telegram,
        qmt_quote=qmt_quote,
        scan_interval=60,
    )
    try:
        watcher.run()
    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"盯盘异常退出: {e}")
        raise

def cmd_collect():
    """数据采集 — 16个采集器，独立 try/except"""
    import sys
    from datetime import datetime
    from system.utils.logger import set_current_task, get_task_logger, get_collector_logger

    set_current_task('collect')
    logger = get_task_logger('collect')
    trade_date = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"开始数据采集 {trade_date}")

    # Module filter support
    module_filter = None
    if '--module' in sys.argv:
        idx = sys.argv.index('--module')
        if idx + 1 < len(sys.argv):
            module_filter = sys.argv[idx + 1]

    # All collectors: (name, module_path, class_name, category)
    collectors = [
        # Market (行情)
        ("stock_basic", "data.collectors.market.stock_basic_collector", "StockBasicCollector", "market"),
        ("main_index", "data.collectors.market.main_index_collector", "MainIndexCollector", "market"),
        ("industry_board", "data.collectors.market.industry_board_collector", "IndustryBoardCollector", "market"),
        ("concept_board", "data.collectors.market.concept_board_collector", "ConceptBoardCollector", "market"),
        ("sector_stocks", "data.collectors.market.sector_stocks_collector", "SectorStocksCollector", "market"),
        ("suspend_resume", "data.collectors.market.suspend_resume_collector", "SuspendResumeCollector", "market"),
        # News (盘中电报)
        ("telegraph", "data.collectors.events.telegraph_collector", "TelegraphCollector", "news"),
        # Events (事件)
        ("cls_digest", "data.collectors.events.cls_digest_collector", "CLSDigestCollector", "events"),
        ("lhb", "data.collectors.events.lhb_collector", "LHBCollector", "events"),
        ("limit_pool", "data.collectors.events.limit_pool_collector", "LimitPoolCollector", "events"),
        ("strong_stock", "data.collectors.events.strong_stock_collector", "StrongStockCollector", "events"),
        ("regulatory", "data.collectors.events.regulatory_letter_collector", "RegulatoryLetterCollector", "events"),
        ("stock_monitor", "data.collectors.events.stock_monitor_collector", "StockMonitorCollector", "events"),
        ("shareholder", "data.collectors.events.share_holder_change_collector", "ShareHolderChangeCollector", "events"),
        ("notice", "data.collectors.events.notice_collector", "NoticeCollector", "events"),
        # Macro (宏观)
        ("macro", "data.collectors.macro.macro_collector", "MacroCollector", "macro"),
    ]

    if module_filter:
        collectors = [c for c in collectors if c[3] == module_filter]
        logger.info(f"筛选模块: {module_filter} -> {len(collectors)} 个采集器")

    import importlib
    success = 0
    failed = 0
    for name, module_path, class_name, category in collectors:
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
    from trade.portfolio.portfolio import Portfolio
    p = Portfolio()
    print(f"  现金: {p.cash:.2f}  总资产: {p.total_value:.2f}  持仓数: {len(p.positions)}")

def cmd_trade():
    print("[trade] 待实现 — 手动录入交易")

def cmd_backtest():
    """回测"""
    print("回测框架（骨架）")
    print("Usage: python main.py backtest --start 2025-01-01 --end 2025-12-31 --stocks 000001,000002")
    from analysis.backtest import BacktestEngine, DataLoader, calculate_metrics
    print("模块已就绪: BacktestEngine, DataLoader, calculate_metrics")

def cmd_track():
    """股票追踪：更新当日行情 + 次日表现 + 统计"""
    from datetime import datetime, timedelta
    from analysis.tracker import StockTracker
    from system.utils.logger import set_current_task, get_task_logger

    set_current_task('track')
    logger = get_task_logger('track')

    tracker = StockTracker()
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

    tracker.update_daily_data(today)
    tracker.update_next_day_data(yesterday, today)

    stats = tracker.get_statistics()
    logger.info(f"总推荐: {stats['total']} | 胜率: {stats['win_rate']:.1f}% | 平均收益: {stats['avg_return']:.2f}%")

def cmd_test():
    print("[test] 配置检查...")
    from system.config.settings import DATABASE_PATH, LOGS_DIR, DASHSCOPE_API_KEY
    print(f"  DB: {DATABASE_PATH}")
    print(f"  Logs: {LOGS_DIR}")
    print(f"  千问 API: {'已配置' if DASHSCOPE_API_KEY else '未配置'}")
    print("  OK")


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <command> [options]")
        print(f"Commands: {', '.join(COMMANDS)}")
        sys.exit(1)

    cmd = sys.argv[1]
    {
        "review": cmd_review, "morning": cmd_morning,
        "monitor": cmd_monitor, "collect": cmd_collect,
        "cleanup": cmd_cleanup, "portfolio": cmd_portfolio,
        "trade": cmd_trade, "backtest": cmd_backtest, "test": cmd_test, "track": cmd_track,
    }.get(cmd, lambda: print(f"Unknown: {cmd}"))()


if __name__ == "__main__":
    main()
