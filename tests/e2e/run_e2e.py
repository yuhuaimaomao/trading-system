# -*- coding: utf-8 -*-
"""E2E 全流程测试主入口。

用法:
    python tests/e2e/run_e2e.py          # 运行全部场景
    python tests/e2e/run_e2e.py --day 1  # 只跑 Day1
    python tests/e2e/run_e2e.py --record # 记录模式（生成 golden snapshots）
"""

import sys
import json
import traceback
from pathlib import Path
from datetime import datetime as RealDateTime
from unittest.mock import MagicMock, patch

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.e2e.sim_clock import SimClock, install_clock
from tests.e2e.sim_qmt import SimQMT
from tests.e2e.sim_telegram import SimTelegram
from tests.e2e.tracer import snapshot as take_snapshot, save_snapshot
from tests.e2e.assertions import compare_snapshots, AssertionReport
from tests.e2e.db_setup import setup_test_db, record_initial_state, record_test_changes


def build_watcher(db_path: str, qmt: SimQMT, telegram: SimTelegram, clock: SimClock):
    """构造 Watcher 实例，注入模拟依赖。"""
    import sqlite3

    # 阻止真实 DB / Telegram / QMT 连接
    with patch("trade.monitor.watcher.TradeRepository"), \
         patch("trade.portfolio.portfolio.Portfolio"), \
         patch("trade.risk.engine.RiskEngine"), \
         patch("system.utils.telegram.MessageSender"):
        from trade.monitor.watcher import Watcher
        w = Watcher.__new__(Watcher)

    # 基础设置
    w.telegram = telegram
    w._private_telegram = None
    w.qmt = qmt
    w.scan_interval = 60
    w.db_path = db_path
    w._running = True
    w._trade_date = clock.strftime("%Y-%m-%d")
    w._scan_count = 0
    w._triggered_ids = set()
    w._alerted_sl_tp = set()
    w._last_index_quote = None

    # 大盘
    w._index_prices = []
    w._index_high = 0.0
    w._index_low = 0.0
    w._index_alerted_downtrend = False
    w._index_last_fluctuation_price = 0.0
    w._market_turnovers = []
    w._volume_alerted_divergence = False
    w._regime = None
    w._closing_decision_done = False
    w._max_drawdown_alerted = False

    # 板块/缓存
    from collections import defaultdict
    w._market_snapshot = {}
    w._sector_trend_history = defaultdict(list)
    w._sector_trend_continuity = defaultdict(int)
    w._sector_trend_last_dir = {}
    w._industry_cache = {}
    w._concept_cache = {}
    w._sector_stats = {}
    w._concept_stats = {}

    # 提醒/去重
    w._signal_alert_state = {}
    w._review_alert_state = {}
    w._prev_snapshot = {}
    w._ma_baseline_cache = None
    w._sl_reminders = {}
    w._limit_cache = {}
    w._bought_watch = {}

    # 懒加载
    w._review_monitor = None
    w._sector_monitor = None
    w._abnormal_detector = None
    w._receiver = None
    w._executor = None
    w._paper_trader = None
    w._collector_client = None

    # 缓存
    w._cached_db_watch_codes = set()
    w._watch_codes_stale = True
    w._intraday_cache = {}
    w._intraday_cache_scan = -1
    w._instrument_cache = {}
    w._daily_factor_cache = {}

    # 指数技术
    w._index_tech_state = {
        "macd_cross": None, "rsi6_zone": "normal", "rsi12_zone": "normal",
        "kdj_cross": None, "kdj_j_zone": "normal", "divergence": None,
    }

    # 情景引擎
    from trade.monitor.market_state import MarketStateMixin
    MarketStateMixin._init_scenario_state(w)

    # Portfolio
    from trade.portfolio.portfolio import Portfolio
    w.portfolio = Portfolio(initial_cash=200_000)

    # Repo（注入测试 DB 路径，不会触碰生产库）
    from data.repo import TradeRepository
    w.repo = TradeRepository(db_path=db_path)

    # RiskEngine mock
    w.risk_engine = MagicMock()
    w.risk_engine.can_open.return_value = MagicMock(allowed=True)
    w.risk_engine.update_market_env = MagicMock()

    return w


def run_day(db_path: str, qmt: SimQMT, telegram: SimTelegram,
            clock: SimClock, num_scans: int, day_label: str,
            record_dir: Path = None) -> list[dict]:
    """运行一天完整的盯盘扫描。

    Returns: 每轮扫描的快照列表。
    """
    snapshots = []

    # 启动 Watcher
    w = build_watcher(db_path, qmt, telegram, clock)
    clock.set(clock.now().replace(hour=9, minute=24, second=0))

    # 安装时钟
    install_clock(w, clock)

    # 恢复持仓（快照清理和板块历史在测试 DB 上太慢，跳过）
    w._trade_date = clock.strftime("%Y-%m-%d")
    w._restore_positions()

    # 重置回撤基准
    w.portfolio._peak_value = w.portfolio.total_value
    w._max_drawdown_alerted = False

    # 跳过 _cleanup_old_snapshots() — 测试 DB 无需清理
    # 跳过 _load_sector_history() — 从零开始积累板块数据

    # 新交易日重置
    w._signal_alert_state.clear()
    w._review_alert_state.clear()
    w._sl_reminders.clear()
    w._alerted_sl_tp.clear()
    w._index_alerted_downtrend = False
    w._max_drawdown_alerted = False
    w._closing_decision_done = False

    # 注入行业缓存（从 DB 读）
    _load_industry_cache(w, db_path)

    print(f"\n{'='*60}")
    print(f"  {day_label} — {num_scans} 轮扫描")
    print(f"{'='*60}")

    for scan in range(num_scans):
        # 前进 1 分钟（加速模式）
        if scan == 0:
            clock.set(clock.now().replace(hour=9, minute=25, second=0))
        else:
            clock.advance(1)
            # 跳过午休
            t = clock.time()
            if t.hour == 11 and t.minute == 31:
                clock.set(clock.now().replace(hour=13, minute=0, second=0))

        qmt.scan = scan
        w._scan_count = scan + 1

        # 注入 collector 数据（模拟 QMT Collector 推送）
        w._last_index_quote = qmt.get_index_quote(scan)
        idx_price = w._last_index_quote["price"]
        w._index_prices.append(idx_price)
        if w._index_high == 0 or idx_price > w._index_high:
            w._index_high = idx_price
        if w._index_low == 0 or idx_price < w._index_low:
            w._index_low = idx_price
        w._market_turnovers.append(w._last_index_quote["amount"])

        # 每 3 轮注入全市场快照（模拟 collector 推送 market 消息）
        if scan % 3 == 0:
            w._market_snapshot = qmt.get_all_quotes_snapshot(scan)
            w._update_sector_trends()

        # 执行扫描
        try:
            w._scan()
        except Exception as e:
            print(f"  [Scan {scan}] 异常: {e}")
            traceback.print_exc()

        # 记录快照
        snap = take_snapshot(w, scan, clock.strftime("%H:%M"))
        snapshots.append(snap)

        # 日志（立即刷新）
        if scan == 0 or scan % 10 == 0 or scan == num_scans - 1:
            regime_str = snap["market_state"]["regime_pattern"] or "?"
            msgs = len(telegram.messages)
            pos = snap.get("portfolio", {}).get("position_count", 0)
            idx = snap["market_state"]["index_price"]
            line = (f"  [{clock.strftime('%H:%M')} Scan#{scan:03d}] "
                    f"上证{idx:.0f} 模式:{regime_str} 持仓:{pos} 消息:{msgs}")
            print(line, flush=True)

    # 收盘
    w._finalize_close()
    print(f"\n  {day_label} 收盘完成。消息总数: {len(telegram.messages)}")

    # 保存记录（如果指定）
    if record_dir:
        record_dir.mkdir(parents=True, exist_ok=True)
        for snap in snapshots:
            fname = f"day1_scan_{snap['scan']:03d}.json"
            save_snapshot(snap, record_dir / fname)

    return snapshots


def _load_industry_cache(w, db_path: str):
    """从 DB 加载行业映射。"""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """SELECT stock_code, industry FROM stock_basic
               WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
        ).fetchall()
        conn.close()
        w._industry_cache = {r[0]: (r[1] or "") for r in rows}
        print(f"  行业缓存: {len(w._industry_cache)} 只")
    except Exception as e:
        print(f"  行业缓存加载失败: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="E2E 全流程测试")
    parser.add_argument("--day", type=int, choices=[1, 2], default=0,
                        help="只跑指定天 (0=全部)")
    parser.add_argument("--record", action="store_true",
                        help="记录模式：保存 golden snapshots")
    args = parser.parse_args()

    # ── 准备数据库 ──
    db_path = setup_test_db()
    initial_state = record_initial_state(db_path)

    # ── 创建模拟组件 ──
    clock = SimClock(RealDateTime(2026, 5, 29, 9, 24, 0))
    qmt = SimQMT()
    telegram = SimTelegram()

    # ── 加载场景 ──
    from tests.e2e.scenarios.day1 import build_day1_scenario
    build_day1_scenario(qmt, db_path)

    # ── 运行 ──
    record_dir = Path(__file__).parent / "expected" if args.record else None
    all_snapshots = []

    if args.day in (0, 1):
        snaps = run_day(str(db_path), qmt, telegram, clock,
                        240, "Day1", record_dir)
        all_snapshots.extend(snaps)
        print(f"\nDay1 快照数: {len(snaps)}")

    if args.day in (0, 2):
        # 保存 Day1 收盘状态用于 Day2
        day1_msgs = list(telegram.messages)
        day1_private = list(telegram.private_messages)
        telegram.reset()

        # Day2 新时钟
        clock2 = SimClock(RealDateTime(2026, 5, 30, 9, 24, 0))
        qmt2 = SimQMT()
        from tests.e2e.scenarios.day2 import build_day2_scenario
        build_day2_scenario(qmt2, db_path)

        snaps = run_day(str(db_path), qmt2, telegram, clock2,
                        240, "Day2", record_dir)
        all_snapshots.extend(snaps)
        print(f"\nDay2 快照数: {len(snaps)}")

    # ── 输出变化 ──
    changes = record_test_changes(db_path, initial_state)
    print(f"\n{'='*60}")
    print("  测试产生的数据变更:")
    for table, info in changes.items():
        print(f"    {table}: +{info['new_rows']} rows (ID {info['new_ids']})")
    if not changes:
        print("    无数据变更（仅内存操作）")

    print(f"\n总快照: {len(all_snapshots)}")
    print(f"总消息: {len(telegram.messages)} (群聊) + {len(telegram.private_messages)} (私聊)")
    print("完成。")


if __name__ == "__main__":
    main()
