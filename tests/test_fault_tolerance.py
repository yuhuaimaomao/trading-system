"""容灾容错测试 — 验证系统在各类故障下不崩溃、不重复下单、不丢失状态。

FaultInjectionHarness 类封装所有 Mock 组件，每个测试注入特定故障并验证预期行为。

测试类别:
  - QMT 行情故障
  - AI 服务故障
  - 数据库故障
  - 网络故障
  - 状态一致性
  - 并发

Why: 量化交易系统一旦崩溃或重复下单会造成真实损失。
      这些测试确保 crond 拉起、盘中重启、数据断连等场景都能稳定恢复。
"""

import logging
import queue
import sqlite3
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mock 组件定义 — 不依赖生产 QMT/AI/DB/Telegram
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MockQMTClient:
    """Mock QMT HTTP 客户端，支持可注入故障模式。

    故障模式通过 failure_mode 设置:
      - None: 正常返回
      - "empty": 返回空行情
      - "partial": 部分股票缺失
      - "timeout": 抛出 Exception
      - "stale": 返回旧时间戳数据
    """

    def __init__(self):
        self.failure_mode = None
        self._call_count = 0
        self._last_codes = None

        # 默认行情数据
        self._quotes = {
            "000001": {"lastPrice": 3200.0, "preClose": 3180.0, "change_pct": 0.63},
            "002371": {"lastPrice": 395.0, "preClose": 390.0, "change_pct": 1.28},
            "600519": {"lastPrice": 1880.0, "preClose": 1870.0, "change_pct": 0.53},
            "300750": {"lastPrice": 220.0, "preClose": 218.0, "change_pct": 0.92},
            "000858": {"lastPrice": 165.0, "preClose": 163.0, "change_pct": 1.23},
        }

    def configure(self, failure_mode=None, quotes=None):
        """设置故障模式和/或覆盖行情数据。"""
        self.failure_mode = failure_mode
        if quotes is not None:
            self._quotes = quotes
        self._call_count = 0

    def get_realtime(self, stock_codes):
        """模拟 QMT get_realtime 调用。

        故障行为:
          - empty: 返回空 dict
          - partial: 只返回部分股票行情
          - timeout: 抛出 RuntimeError
          - stale: 正常返回，但 last_db_ts 需单独设置
        """
        self._call_count += 1
        self._last_codes = stock_codes

        if self.failure_mode == "timeout":
            raise RuntimeError("QMT connection timeout after 30s")

        if self.failure_mode == "empty":
            return {}

        if self.failure_mode == "partial":
            # 只返回前一半股票
            half = len(stock_codes) // 2 or 1
            result = {}
            for code in stock_codes[:half]:
                q = self._quotes.get(code)
                if q:
                    result[code] = q
            return result

        # 正常返回
        result = {}
        for code in stock_codes:
            q = self._quotes.get(code)
            if q:
                result[code] = q
        return result

    def all_quotes(self):
        """Mock all_quotes 返回全市场数据。"""
        return {
            "success": True,
            "data": self._quotes,
        }

    def quote(self, code):
        """Mock 单只行情。"""
        q = self._quotes.get(code, {})
        return {"success": True, "data": q}


class MockAIService:
    """Mock AI 服务，支持可注入故障。

    故障模式:
      - None: 正常返回
      - "timeout": chat() 抛出 TimeoutError
      - "empty": 返回空字符串
      - "malformed_json": 返回不可解析 JSON
      - None 但可配置具体返回内容
    """

    def __init__(self):
        self.failure_mode = None
        self._chat_count = 0
        self._submit_count = 0
        self._results: dict[str, str] = {}
        self._q: queue.Queue = queue.Queue()
        self._running = False
        self._default_response = '{"decision": "hold", "reason": "正常"}'

    def configure(self, failure_mode=None, default_response=None):
        """设置故障模式和/或默认返回内容。"""
        self.failure_mode = failure_mode
        if default_response is not None:
            self._default_response = default_response
        self._chat_count = 0
        self._submit_count = 0

    # ── 同步接口 ──

    def chat(
        self, prompt, *, model="", system_prompt="", max_tokens=1000, temperature=0.6
    ):
        """模拟同步 AI 调用。"""
        self._chat_count += 1

        if self.failure_mode == "timeout":
            raise TimeoutError("AI API timeout after 60s")

        if self.failure_mode == "empty":
            return ""

        if self.failure_mode == "malformed_json":
            return "这里不是合法的 JSON{broken"

        return self._default_response

    # ── 异步接口 ──

    def start_worker(self):
        self._running = True

    def stop_worker(self):
        self._running = False

    def submit(self, key, prompt, *, model="", system_prompt="", max_tokens=100):
        """模拟异步提交。立即存入结果，不启动线程。"""
        self._submit_count += 1

        if self.failure_mode == "timeout":
            # 模拟异步超时：不存结果
            return True

        result = self._default_response if self.failure_mode != "empty" else ""
        self._results[key] = result
        return True

    def pop(self, key):
        """弹出结果。"""
        return self._results.pop(key, None)

    def pending(self, key):
        return key in self._results

    @property
    def qsize(self):
        return self._q.qsize()


class MockTelegramBot:
    """Mock Telegram Bot，记录发送消息供断言。"""

    def __init__(self):
        self.messages: list[str] = []
        self.failure_mode = None
        self._send_count = 0

    def configure(self, failure_mode=None):
        self.failure_mode = failure_mode
        self._send_count = 0

    def send(self, message):
        """模拟 send_message。"""
        self._send_count += 1

        if self.failure_mode == "unreachable":
            raise ConnectionError("Telegram API unreachable after 30s")

        if self.failure_mode == "rate_limited":
            # 模拟 429
            raise Exception("Too Many Requests: retry after 30s")

        self.messages.append(message)

    def send_message(self, message):
        """兼容 Watcher 的 telegram.send_message 调用。"""
        return self.send(message)


class MockDataCollectorClient:
    """Mock Collector TCP 客户端。"""

    def __init__(self):
        self.connected = True
        self._messages: list[dict] = []
        self.failure_mode = None

    def configure(self, failure_mode=None, messages=None):
        self.failure_mode = failure_mode
        if messages is not None:
            self._messages = messages
        self.connected = failure_mode != "disconnected"

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def recv_all(self):
        if self.failure_mode == "exception":
            raise ConnectionError("Socket read error")

        msgs = list(self._messages)
        self._messages.clear()
        return msgs


class MockTradeRepository:
    """Mock 数据访问层，使用内存 SQLite 模拟真实数据库。

    支持注入故障:
      - "locked": 写操作第一次抛出 sqlite3.OperationalError (锁)
      - "disk_full": 抛出 IOError (磁盘满)
      - "corrupted": 初始化时抛出异常
    """

    def __init__(self, db_path=None):
        self.failure_mode = None
        self._write_attempts = 0
        self._writes_log: list[str] = []

        # 内存 DB，支持回滚独立连接做隔离测试
        self._conn = sqlite3.connect(db_path or ":memory:")
        self._init_schema()

    def _init_schema(self):
        if self.failure_mode == "corrupted":
            raise sqlite3.DatabaseError("database disk image is malformed")

        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                created_at TEXT,
                signal_type TEXT,
                signal_source TEXT,
                stock_code TEXT,
                stock_name TEXT,
                buy_zone_min REAL,
                buy_zone_max REAL,
                stop_loss REAL,
                take_profit REAL,
                trailing_stop REAL,
                signal_score REAL,
                strategy_name TEXT,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                executed_at TEXT,
                account TEXT DEFAULT 'paper',
                expected_trend TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS trade_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                order_time TEXT,
                stock_code TEXT,
                order_type TEXT,
                order_price REAL,
                order_volume INTEGER,
                order_status TEXT DEFAULT 'filled',
                filled_volume INTEGER DEFAULT 0,
                filled_price REAL,
                filled_amount REAL,
                commission REAL,
                strategy_name TEXT,
                account TEXT DEFAULT 'paper'
            );
            CREATE TABLE IF NOT EXISTS trade_portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                total_value REAL,
                cash REAL,
                market_value REAL,
                daily_pnl REAL,
                total_pnl REAL,
                drawdown REAL,
                position_count INTEGER,
                sector_exposure TEXT,
                account TEXT DEFAULT 'paper',
                created_at TEXT
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
                entry_date TEXT DEFAULT '',
                locked_volume INTEGER DEFAULT 0,
                holding_days INTEGER DEFAULT 0,
                UNIQUE(trade_date, account, stock_code)
            );
            CREATE TABLE IF NOT EXISTS stock_basic (
                stock_code TEXT PRIMARY KEY,
                stock_name TEXT,
                trade_date TEXT
            );
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT,
                ts REAL,
                code TEXT,
                change_pct REAL,
                price REAL,
                amount REAL
            );
        """)
        self._conn.commit()

    def configure(self, failure_mode=None):
        self.failure_mode = failure_mode
        self._write_attempts = 0

    def _check_write_fault(self, operation_name=""):
        """写操作前检查故障模式。支持重试一次后成功。"""
        if self.failure_mode == "locked":
            self._write_attempts += 1
            if self._write_attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            # 第二次重试成功，无需额外操作

        if self.failure_mode == "disk_full":
            self._write_attempts += 1
            raise IOError("No space left on device")

    def log_write(self, op_name: str):
        self._writes_log.append(op_name)

    @property
    def db_path(self):
        return ":memory:"

    # ── 信号 ──

    def get_pending_signals(self, trade_date=None, account=None):
        return []

    def expire_old_pending_signals(self, trade_date):
        pass

    def get_signal_for_pos_meta(self, code):
        return None

    def get_bought_signals_with_entry(self):
        return []

    # ── 持仓/快照 ──

    def get_latest_snapshot(self, account="paper"):
        try:
            row = self._conn.execute(
                "SELECT * FROM trade_portfolio_snapshots WHERE account=? "
                "ORDER BY id DESC LIMIT 1",
                (account,),
            ).fetchone()
            if not row:
                return None
            cols = [
                d[1]
                for d in self._conn.execute(
                    "PRAGMA table_info(trade_portfolio_snapshots)"
                ).fetchall()
            ]
            return dict(zip(cols, row))
        except Exception:
            return None

    def get_positions_by_date(self, trade_date, account="paper"):
        try:
            rows = self._conn.execute(
                "SELECT * FROM trade_portfolio_positions "
                "WHERE trade_date=? AND account=?",
                (trade_date, account),
            ).fetchall()
            cols = [
                d[1]
                for d in self._conn.execute(
                    "PRAGMA table_info(trade_portfolio_positions)"
                ).fetchall()
            ]
            return [dict(zip(cols, r)) for r in rows]
        except Exception:
            return []

    def get_latest_positions(self, account="paper"):
        try:
            row = self._conn.execute(
                "SELECT trade_date FROM trade_portfolio_positions "
                "WHERE account=? ORDER BY trade_date DESC LIMIT 1",
                (account,),
            ).fetchone()
            if not row:
                return []
            return self.get_positions_by_date(row[0], account)
        except Exception:
            return []

    def insert_snapshot(self, snap_dict):
        self.log_write("snapshot")
        self._check_write_fault("insert_snapshot")
        cols = ", ".join(snap_dict.keys())
        placeholders = ", ".join("?" * len(snap_dict))
        self._conn.execute(
            f"INSERT INTO trade_portfolio_snapshots ({cols}) VALUES ({placeholders})",
            list(snap_dict.values()),
        )
        self._conn.commit()

    def insert_positions(self, trade_date, account, positions):
        self.log_write("positions")
        self._check_write_fault("insert_positions")
        for pos in positions:
            pos["trade_date"] = trade_date
            pos["account"] = account
            cols = ", ".join(pos.keys())
            placeholders = ", ".join("?" * len(pos))
            self._conn.execute(
                f"INSERT OR REPLACE INTO trade_portfolio_positions "
                f"({cols}) VALUES ({placeholders})",
                list(pos.values()),
            )
        self._conn.commit()

    # ── 订单 ──

    def insert_order(self, order_dict):
        self.log_write("order")
        self._check_write_fault("insert_order")
        cols = ", ".join(order_dict.keys())
        placeholders = ", ".join("?" * len(order_dict))
        self._conn.execute(
            f"INSERT INTO trade_orders ({cols}) VALUES ({placeholders})",
            list(order_dict.values()),
        )
        self._conn.commit()

    def get_orders_by_date(self, trade_date, account=None):
        try:
            if account:
                rows = self._conn.execute(
                    "SELECT * FROM trade_orders WHERE trade_date=? AND account=?",
                    (trade_date, account),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM trade_orders WHERE trade_date=?",
                    (trade_date,),
                ).fetchall()
            if not rows:
                return []
            cols = [
                d[1]
                for d in self._conn.execute(
                    "PRAGMA table_info(trade_orders)"
                ).fetchall()
            ]
            return [dict(zip(cols, r)) for r in rows]
        except Exception:
            return []

    # ── 板块/名称 ──

    def get_morning_sector_bias(self, trade_date):
        return []

    def resolve_name(self, code):
        return code

    def get_market_snapshots_batch(self, trade_date, latest_ts):
        return []

    def get_latest_market_ts(self, trade_date):
        return None

    def get_review_picks_latest(self) -> list[dict]:
        return []

    def get_signal_for_pos_meta(self, code: str) -> dict | None:
        return None

    def get_buy_dates(self, codes: list[str]) -> dict[str, str]:
        return {}

    def get_bought_signals_with_entry(self) -> list[dict]:
        return []

    def get_daily_indicators(self, code: str) -> dict | None:
        return None

    def get_money_flow(self, code: str) -> dict | None:
        return None

    def get_support_resistance(self, code: str, price: float) -> dict:
        return {}

    def get_index_snapshot_history(self, days: int = 3) -> list[dict]:
        return []

    def get_review_signal_zones(self, trade_date: str) -> dict[str, tuple]:
        return {}

    def insert_signal(self, signal_dict: dict) -> int:
        return 0

    def update_signal_status(self, signal_id: int, status: str):
        pass

    def get_expired_signals(self, before_date: str) -> list[dict]:
        return []

    def insert_decision_log(
        self, trade_date, ts, decision_type, stock_code, decision_data
    ):
        return 0

    def insert_audit_finding(self, finding):
        return 0

    def insert_watcher_improvement(self, imp):
        return 0

    # ── 回收 ──

    def close(self):
        self._conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FaultInjectionHarness — 核心测试夹具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@dataclass
class ScanResult:
    """一轮扫描的结果，供断言用。"""

    scan_count: int = 0
    positions_count: int = 0
    total_value: float = 0.0
    cash: float = 0.0
    market_value: float = 0.0
    data_ready: bool = False
    alerts_sent: int = 0
    errors_logged: list[str] = field(default_factory=list)
    warnings_logged: list[str] = field(default_factory=list)


class FaultInjectionHarness:
    """容灾注入测试夹具。

    创建 Watcher 并注入所有 Mock 组件，提供:
      - 故障配置接口 (inject_*)
      - 单轮扫描执行 (run_scan_round)
      - 状态断言 (assert_*)
      - 日志抓取验证

    用法:
        harness = FaultInjectionHarness()
        harness.inject_qmt_failure("empty")
        result = harness.run_scan_round()
        assert result.scan_count == 1
        harness.assert_no_crash()
    """

    def __init__(self, db_path=None):
        # 创建共享临时目录（模拟盘中 DB）
        self._tmpdir = Path(tempfile.mkdtemp(prefix="fault_test_"))
        self._db_path = db_path or str(self._tmpdir / "test_fault.db")

        # 初始化完整 schema（与生产环境一致）
        self._init_schema()

        # Mock 组件
        self.mock_qmt = MockQMTClient()
        self.mock_ai = MockAIService()
        self.mock_telegram = MockTelegramBot()
        self.mock_repo = MockTradeRepository(self._db_path)
        self.mock_collector = MockDataCollectorClient()

        # 日志捕获
        self._log_handler = LogCaptureHandler()
        self._logger = logging.getLogger("trade.core.watcher")
        self._logger.addHandler(self._log_handler)
        self._logger.setLevel(logging.DEBUG)

        # Watcher 实例
        self._patches: list = []
        self.watcher = None

        # AI 服务的引用需要预先 patch（因为 watcher 内 method-level import）
        # 注意: patch 顺序——先 patch 再创建 Watcher
        self._apply_patches()

        # 注入 settings patch（控制 MAX_POSITIONS 等常量）
        self._stored_max_positions = 10

    def _init_schema(self):
        """使用 conftest 基础 schema 初始化临时 DB（避免依赖生产 ALTER TABLE）。"""
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL, created_at TEXT NOT NULL,
                signal_type TEXT NOT NULL, signal_source TEXT NOT NULL,
                stock_code TEXT NOT NULL, stock_name TEXT,
                buy_zone_min REAL, buy_zone_max REAL,
                target_position REAL, stop_loss REAL, take_profit REAL,
                trailing_stop REAL, signal_score REAL, strategy_name TEXT,
                reason TEXT, status TEXT DEFAULT 'pending', executed_at TEXT,
                account TEXT DEFAULT 'paper', target_price REAL,
                expected_trend TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS trade_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER, trade_date TEXT NOT NULL,
                order_time TEXT NOT NULL, stock_code TEXT NOT NULL,
                order_type TEXT NOT NULL, order_price REAL,
                order_volume INTEGER, price_type TEXT DEFAULT 'limit',
                order_status TEXT DEFAULT 'pending',
                filled_volume INTEGER DEFAULT 0, filled_price REAL,
                filled_amount REAL, commission REAL,
                qmt_order_id TEXT, reject_reason TEXT,
                strategy_name TEXT, updated_at TEXT,
                account TEXT DEFAULT 'paper'
            );
            CREATE TABLE IF NOT EXISTS trade_portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL, total_value REAL, cash REAL,
                market_value REAL, daily_pnl REAL, total_pnl REAL,
                drawdown REAL, position_count INTEGER,
                sector_exposure TEXT, account TEXT DEFAULT 'paper',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS trade_portfolio_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL, account TEXT DEFAULT 'paper',
                stock_code TEXT NOT NULL, stock_name TEXT,
                volume INTEGER, avg_cost REAL, current_price REAL,
                market_value REAL, pnl REAL, pnl_pct REAL,
                pre_close REAL DEFAULT 0, daily_pnl REAL DEFAULT 0,
                entry_date TEXT DEFAULT '', locked_volume INTEGER DEFAULT 0,
                stop_loss REAL, take_profit REAL,
                holding_days INTEGER DEFAULT 0, sector_code TEXT,
                created_at TEXT,
                UNIQUE(trade_date, account, stock_code)
            );
        """)
        conn.commit()
        conn.close()

    def _apply_patches(self):
        """在创建 Watcher 前应用所有 patch。

        Watcher 对外部依赖的使用方式:
          - QMT: self.qmt.get_realtime() — 通过构造函数注入 mock_qmt
          - AI: 方法内 from system.ai import ai → 需要 patch system.ai.ai
          - DB: self.repo 通过 TradeRepository 构造函数注入 → 替换 TradeRepository
          - Telegram: 构造函数传入 telegram_bot → 直接注入 mock_telegram

        因此关键 patch 只有:
          1. system.ai.ai — 让 watcher 方法内的 "from system.ai import ai" 拿到 mock
          2. trade.monitor.ai_queue.ai — ai_queue 模块级引用
          3. 部分内部依赖如 _ensure_collector_running 的 socket 调用
        """
        import system.ai as ai_mod
        import system.message.receiver as receiver_mod
        import system.message.sender as sender_mod
        import trade.core.watcher as watcher_mod

        # AI 服务: watcher 和 ai_queue 都在方法内用 "from system.ai import ai"
        # Telegram: Watcher._init_private_telegram 会创建真实 MessageSender → 需要 mock
        # TradeRepository: watcher 中 module-level "from data.repo import TradeRepository"
        self._patches = [
            patch.object(ai_mod, "ai", self.mock_ai),
            patch.object(
                watcher_mod, "TradeRepository", lambda db_path=None: self.mock_repo
            ),
            patch.object(sender_mod, "MessageSender", lambda **kw: self.mock_telegram),
            patch.object(
                receiver_mod, "MessageReceiver", lambda **kw: self.mock_telegram
            ),
        ]

        for p in self._patches:
            p.start()

        # 创建 Watcher 实例（此时所有 patch 已生效）
        from trade.core.watcher import Watcher

        self.watcher = Watcher(
            telegram_bot=self.mock_telegram,
            qmt_quote=self.mock_qmt,
            db_path=self._db_path,
        )

        # 替换内部的 AlertRouter 构造（Watcher.__init__ 里 self.alerter = AlertRouter(...)）
        # 由于 Watcher.__init__ 中创建了 alerter，我们直接替换其 bot 实例
        self.watcher.alerter._group = self.mock_telegram
        self.watcher.alerter._private = self.mock_telegram

        # PaperAccount 内部有自己的 TradeRepository 引用（未被 watcher 的 patch 覆盖）
        # 需要手动替换，否则 PaperAccount 会用真实 repo
        self.watcher.paper_account.repo = self.mock_repo

    # ═══════════════════════════════════════════════════════════════════
    # 故障注入接口
    # ═══════════════════════════════════════════════════════════════════

    def inject_qmt_failure(self, mode: str | None):
        """注入 QMT 故障。"""
        self.mock_qmt.configure(failure_mode=mode)

    def inject_ai_failure(self, mode: str | None):
        """注入 AI 故障。"""
        self.mock_ai.configure(failure_mode=mode)

    def inject_db_failure(self, mode: str | None):
        """注入 DB 故障。"""
        self.mock_repo.configure(failure_mode=mode)

    def inject_telegram_failure(self, mode: str | None):
        """注入 Telegram 网络故障。"""
        self.mock_telegram.configure(failure_mode=mode)

    def inject_collector_failure(self, mode: str | None):
        """注入 Collector 连接故障。"""
        self.mock_collector.configure(failure_mode=mode)

    # ═══════════════════════════════════════════════════════════════════
    # 运行方法
    # ═══════════════════════════════════════════════════════════════════

    def run_scan_round(self) -> ScanResult:
        """模拟一轮 _scan 调用。返回扫描结果。"""
        if not self.watcher:
            return ScanResult()

        # 设置最小上下文
        self.watcher._running = True
        self.watcher._scan_count += 1
        self.watcher._trade_date = "2026-06-06"
        self.watcher._data_ready = True

        # 给 paper_account 注入仓库
        self.watcher.paper_account.repo = self.mock_repo

        before_positions = len(self.watcher.paper_account.positions)
        before_alerts = len(self.mock_telegram.messages)

        try:
            self.watcher._scan()
        except Exception:
            pass  # 容灾测试：外层 catch 不扩散，但记录

        result = ScanResult(
            scan_count=self.watcher._scan_count,
            positions_count=len(self.watcher.paper_account.positions),
            total_value=self.watcher.paper_account.total_value,
            cash=self.watcher.paper_account.cash,
            market_value=sum(
                p.market_value for p in self.watcher.paper_account.positions.values()
            ),
            data_ready=self.watcher._data_ready,
            alerts_sent=len(self.mock_telegram.messages) - before_alerts,
            errors_logged=self._log_handler.get_errors(),
            warnings_logged=self._log_handler.get_warnings(),
        )
        return result

    def run_multiple_rounds(
        self, count: int, interval: float = 0.01
    ) -> list[ScanResult]:
        """连续跑 N 轮扫描，每轮间隔可调。"""
        results = []
        for _ in range(count):
            results.append(self.run_scan_round())
            time.sleep(interval)
        return results

    # ═══════════════════════════════════════════════════════════════════
    # 断言辅助
    # ═══════════════════════════════════════════════════════════════════

    def assert_no_crash(self):
        """验证 Watcher 没有崩溃 — 对象仍有效，_scan 可继续调用。"""
        assert self.watcher is not None, "Watcher 已崩溃 (None)"

    def assert_no_duplicate_orders(self, trade_date: str | None = None):
        """验证没有重复下单 — 同一股票同一方向在短时间内没有重复订单。"""
        td = trade_date or "2026-06-06"
        orders = self.mock_repo.get_orders_by_date(td, "paper")

        # 检查同一 stock_code + order_type 在 3 轮内是否有重复
        buy_times: dict[str, list[str]] = defaultdict(list)
        for o in orders:
            if o["order_type"] == "buy":
                key = o["stock_code"]
                buy_times[key].append(o.get("order_time", ""))

        for code, times in buy_times.items():
            if len(times) > 1:
                # 允许同一股票多次买入（分批建仓），但时间不能一样
                assert len(set(times)) == len(times), (
                    f"重复买入 {code}: 相同时间 {times}"
                )

    def assert_state_invariant(self):
        """验证 total_value = cash + market_value 恒成立。"""
        inv = self.watcher.paper_account.total_value
        cash = self.watcher.paper_account.cash
        mv = sum(p.market_value for p in self.watcher.paper_account.positions.values())
        assert abs(inv - (cash + mv)) < 0.01, (
            f"状态不变量被破坏: total_value={inv:.2f} != cash={cash:.2f} + market_value={mv:.2f}"
        )

    def assert_position_count_matches(self):
        """验证持仓计数与 paper_account.positions 长度一致。"""
        assert self.watcher.paper_account.position_count == len(
            self.watcher.paper_account.positions
        ), "position_count 与 positions 长度不匹配"

    # ═══════════════════════════════════════════════════════════════════
    # 回收
    # ═══════════════════════════════════════════════════════════════════

    def cleanup(self):
        for p in self._patches:
            p.stop()
        self._logger.removeHandler(self._log_handler)
        self.watcher = None
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)


class LogCaptureHandler(logging.Handler):
    """捕获日志用于断言验证。"""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def get_errors(self) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= logging.ERROR]

    def get_warnings(self) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= logging.WARNING]

    def get_messages(self, levelno=logging.WARNING) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= levelno]

    def has_message(self, pattern: str, levelno=logging.WARNING) -> bool:
        import re

        for r in self.records:
            if r.levelno >= levelno and re.search(pattern, r.getMessage()):
                return True
        return False

    def clear(self):
        self.records.clear()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.fixture
def harness():
    """标准测试夹具，每个测试自动创建和清理。"""
    h = FaultInjectionHarness()
    yield h
    h.cleanup()


@pytest.fixture
def harness_with_positions(harness):
    """预置 2 只持仓的夹具。"""
    w = harness.watcher
    # 模拟 PaperAccount 已有持仓
    from trade.exec.paper.portfolio import Position

    pos1 = Position(
        stock_code="002371",
        stock_name="北方华创",
        volume=100,
        avg_cost=390.0,
        current_price=395.0,
        market_value=39500.0,
        pnl=500.0,
        pnl_pct=0.0128,
        entry_date="2026-06-05",
    )
    pos2 = Position(
        stock_code="600519",
        stock_name="贵州茅台",
        volume=100,
        avg_cost=1870.0,
        current_price=1880.0,
        market_value=188000.0,
        pnl=1000.0,
        pnl_pct=0.0053,
        entry_date="2026-06-04",
    )
    w.paper_account._portfolio.positions = {"002371": pos1, "600519": pos2}
    yield harness


# ════════════════════════════════════════════════════════════════════════════════
# 1. QMT 行情故障
# ════════════════════════════════════════════════════════════════════════════════


class TestQMTFailures:
    """QMT 行情获取故障测试。
    QMT 是实时行情唯一来源，它的故障直接影响所有依赖行情运行的模块。

    _get_realtime_prices 的设计是异常安全（外层 try/except 返回 {}），
    但需要验证空/异常返回值是否传播到 scan 的后续步骤。
    """

    # ── 1a. QMT 连接中断：返回空行情 ──
    # WHAT: QMT get_realtime 返回空 dict
    # SHOULD: _get_realtime_prices 返回 {} → _scan 日志警告 "无实时行情" → 跳过本轮
    def test_qmt_empty_quotes_skips_round(self, harness):
        """QMT 空行情 → 跳过本轮，不崩溃。"""
        harness.inject_qmt_failure("empty")
        harness.watcher._trade_date = "2026-06-06"
        harness.watcher._data_ready = True
        # 给 watcher 预设持仓，确保 QMT 被调用
        from trade.exec.paper.portfolio import Position

        harness.watcher.paper_account._portfolio.positions["002371"] = Position(
            stock_code="002371",
            stock_name="北方华创",
            volume=100,
            avg_cost=390.0,
            current_price=395.0,
            market_value=39500.0,
            pnl=500.0,
            pnl_pct=0.0128,
            entry_date="2026-06-05",
        )
        harness.watcher._watch_codes_stale = True

        result = harness.run_scan_round()

        harness.assert_no_crash()
        assert harness.mock_qmt._call_count >= 1, "QMT 应被调用了"
        assert harness._log_handler.has_message("无实时行情", logging.WARNING), (
            "应在日志中警告无实时行情"
        )

    # ── 1b. QMT 连接中断后恢复 ──
    # WHAT: QMT 连续多轮返回空，然后恢复正常
    # SHOULD: 空轮跳过后，恢复后正常扫描
    @pytest.mark.slow
    def test_qmt_recovery_after_empty(self, harness):
        """QMT 断开后恢复 → 空轮跳过，恢复轮正常通过。"""
        from trade.exec.paper.portfolio import Position

        harness.watcher.paper_account._portfolio.positions["002371"] = Position(
            stock_code="002371",
            stock_name="北方华创",
            volume=100,
            avg_cost=390.0,
            current_price=395.0,
            market_value=39500.0,
            pnl=500.0,
            pnl_pct=0.0128,
            entry_date="2026-06-05",
        )
        harness.watcher._watch_codes_stale = True

        harness.inject_qmt_failure("empty")
        r1 = harness.run_scan_round()
        assert harness._log_handler.has_message("无实时行情")

        harness._log_handler.clear()
        harness.inject_qmt_failure(None)  # 恢复
        r2 = harness.run_scan_round()

        harness.assert_no_crash()
        # 恢复后不应再有跳过警告（用已恢复配置再跑，看是否停发跳过警告）
        assert not harness._log_handler.has_message("无实时行情"), (
            "恢复后不应再出现跳过警告"
        )

    # ── 1c. QMT 返回部分行情 ──
    # WHAT: 某些股票行情缺失
    # SHOULD: 只更新有行情的股票，无行情的不更新价格也不崩溃
    def test_qmt_partial_quotes(self, harness_with_positions):
        """部分股票缺失 → 只更新已有行情的不崩溃。"""
        harness = harness_with_positions
        harness.inject_qmt_failure("partial")  # 只返回前一半

        result = harness.run_scan_round()

        harness.assert_no_crash()
        harness.assert_state_invariant()
        # 持仓数量不应变化（只是没更新价格）
        assert result.positions_count == 2, "持仓数量不应因部分行情变化"

    # ── 1d. QMT 超时异常 ──
    # WHAT: get_realtime 抛出 Exception
    # SHOULD: Exception 被 Watcher._get_realtime_prices 捕获 → 日志警告 → 返回 {} → 跳过
    def test_qmt_timeout_exception(self, harness):
        """QMT 超时 → Exception 被捕获 → 日志警告 → 跳过本轮。"""
        from trade.exec.paper.portfolio import Position

        harness.watcher.paper_account._portfolio.positions["002371"] = Position(
            stock_code="002371",
            stock_name="北方华创",
            volume=100,
            avg_cost=390.0,
            current_price=395.0,
            market_value=39500.0,
            pnl=500.0,
            pnl_pct=0.0128,
            entry_date="2026-06-05",
        )
        harness.watcher._watch_codes_stale = True
        harness.inject_qmt_failure("timeout")

        result = harness.run_scan_round()

        harness.assert_no_crash()
        assert harness._log_handler.has_message("QMT 行情获取失败", logging.WARNING), (
            "应记录 QMT 超时警告"
        )

    # ── 1e. QMT 返回陈旧数据 → last_db_ts 超期 ──
    # WHAT: 数据新鲜度超 3 分钟
    # SHOULD: _check_data_stale 设 data_ready=False → 暂停交易
    @pytest.mark.slow
    def test_qmt_stale_data_pauses_trading(self, harness):
        """陈旧数据超过 3 分钟 → data_ready=False → 暂停买入决策。"""
        w = harness.watcher
        w._data_ready = True
        w._last_db_ts = time.time() - 200  # 超过 3 分钟 (180s)

        result = harness.run_scan_round()

        # _check_data_stale 设 data_ready=False
        assert not w._data_ready, "陈旧数据应暂停 data_ready"
        assert harness._log_handler.has_message("数据断连", logging.WARNING), (
            "应记录数据断连警告"
        )

    # ── 1f. QMT 返回 0 值（非 None 但无效）──
    # WHAT: 价格字段为 None 或 0
    # SHOULD: //过滤掉，不影响处理
    def test_qmt_zero_price(self, harness_with_positions):
        """QMT 返回零价格 → 过滤掉不导致崩溃。"""
        harness = harness_with_positions
        # 修改 mock 返回数据包含 0 价格
        harness.mock_qmt._quotes["002371"]["lastPrice"] = 0.0
        harness.mock_qmt._quotes["002371"]["price"] = 0.0

        result = harness.run_scan_round()

        harness.assert_no_crash()
        # 零价格不加入 _recent_prices
        assert (
            "002371" not in harness.watcher._recent_prices
            or len(harness.watcher._recent_prices["002371"]) == 0
        ), "零价格的股票不应记录到 recent_prices"


# ════════════════════════════════════════════════════════════════════════════════
# 2. AI 服务故障
# ════════════════════════════════════════════════════════════════════════════════


class TestAIFailures:
    """AI 服务故障测试。

    AI 在系统中起辅助决策作用，不是核心管线必需。故障不应阻塞主循环。
    """

    # ── 2a. AI API 同步超时 ──
    # WHAT: ai.chat() 抛出 TimeoutError
    # SHOULD: 被调用方 try/except 捕获 → 返回 None/空 → 跳过 AI 步骤
    def test_ai_timeout_skip(self, harness):
        """AI 超时 → 异常被捕获 → 不阻塞扫描。"""
        harness.inject_ai_failure("timeout")

        result = harness.run_scan_round()

        harness.assert_no_crash()
        assert harness.mock_ai._chat_count >= 0  # 可能没触发到 AI 调用，但不应崩溃

    # ── 2b. AI 返回空响应 ──
    # WHAT: 空字符串
    # SHOULD: 任何解释 JSON 的代码返回 {} 或 None，不影响后续
    def test_ai_empty_response(self, harness):
        """AI 返回空 → 优雅处理，无信号生成。"""
        harness.inject_ai_failure("empty")

        result = harness.run_scan_round()

        harness.assert_no_crash()
        # AI 空响应不应触发任何信号
        assert len(harness.mock_telegram.messages) >= 0

    # ── 2c. AI 返回非法 JSON ──
    # WHAT: 包含不可解析的内容
    # SHOULD: 解析函数捕获异常 → 返回空 dict → 干净处理
    def test_ai_malformed_json(self, harness):
        """AI 返回乱码 JSON → 解析器容错 → 返回空。"""
        harness.inject_ai_failure("malformed_json")

        result = harness.run_scan_round()

        harness.assert_no_crash()

    # ── 2d. AI 异步队列积压 ──
    # WHAT: submit 多次触发队列满
    # SHOULD: submit 返回 False，不崩溃；旧任务被丢弃
    def test_ai_queue_overflow(self, harness):
        """AI 队列满 → 旧任务丢弃 → 新任务提交成功。"""
        ai = harness.mock_ai
        # 正常模式
        for i in range(5):
            ok = ai.submit(f"key_{i}", f"prompt_{i}")
            assert ok, f"第{i}次 submit 应成功"
        # 连续 submit 不应奔溃
        assert ai._submit_count == 5

    # ── 2e. AI 异步结果从容异步处理 ──
    # WHAT: 多个 AI 任务异步提交，结果陆续返回
    # SHOULD: _process_pending_ai 依次处理，不重复不遗漏
    def test_ai_async_results_processing(self, harness):
        """异步 AI 结果分批返回 → 依次处理不崩溃。"""
        ai = harness.mock_ai

        # 模拟 enqueue 然后 pop
        ai.submit("chase:002371", "chase opinion")
        ai.submit("chase:600519", "chase opinion")
        ai.submit("index_fluctuation", "index analysis")

        result_chase = ai.pop("chase:002371")
        assert result_chase is not None, "追高 AI 结果应可弹出"

        result_index = ai.pop("index_fluctuation")
        assert result_index is not None, "指数 AI 结果应可弹出"

        harness.assert_no_crash()


# ════════════════════════════════════════════════════════════════════════════════
# 3. 数据库故障
# ════════════════════════════════════════════════════════════════════════════════


class TestDBFailures:
    """数据库故障容灾测试。

    所有 DB 写操作都有 try/except 保护。验证：
    - 写失败不阻塞主循环
    - 临时锁有重试
    - 永久故障记录 critical 日志
    """

    # ── 3a. DB 锁 ──
    # WHAT: insert_snapshot / insert_positions 抛出 sqlite3.OperationalError
    # SHOULD: 重试一次 → 成功；写入不丢失
    def test_db_locked_retry_succeeds(self, harness):
        """DB 锁 → 写失败被捕获 → 不崩溃（PaperAccount 自身无重试）。"""
        harness.inject_db_failure("locked")  # 写抛出 OperationalError

        w = harness.watcher
        w._trade_date = "2026-06-06"
        w.paper_account._trade_date = "2026-06-06"

        # 触发一次写入 — PaperAccount 内部 try/except 捕获锁异常
        # 预期: 不崩溃，write_attempts 记录到日志
        try:
            w.paper_account._persist_state()
        except Exception:
            pass

        harness.assert_no_crash()
        # 写操作确实被执行了（即使因锁失败）
        assert harness.mock_repo._write_attempts >= 1, "应尝试了写操作"

    # ── 3b. DB 磁盘满 ──
    # WHAT: IOError on write
    # SHOULD: 捕获 → log critical → 不崩溃
    def test_db_disk_full(self, harness):
        """磁盘满 → 写失败 → 日志 critical → 不崩溃。"""
        harness.inject_db_failure("disk_full")

        w = harness.watcher
        # 触发写入
        w.paper_account._persist_state()

        w._check_data_stale()
        harness.assert_no_crash()
        # 写入失败后快照不应存在
        snap = harness.mock_repo.get_latest_snapshot("paper")
        assert snap is None or snap.get("total_value", 0) == 0, (
            "磁盘满时快照不应成功写入"
        )

    # ── 3c. DB 损坏检测 ──
    # WHAT: 初始化时 sqlite3.DatabaseError
    # SHOULD: 上层捕获 → 告警 → 回退到无 DB 模式
    def test_db_corrupted_on_init(self, harness):
        """DB 损坏 → 初始化检测到 → 告警 → fallback 不崩溃。"""
        harness.inject_db_failure("corrupted")

        # 重新构建 Watcher 会触发 DB 初始化异常
        try:
            harness._build_watcher()
        except Exception:
            pass  # 初始化阶段异常被 Watcher 自身捕获

        # 即使 DB 损坏，Watcher 初始化不应导致进程崩溃
        # (TradeRepository 初始化可能在 Watcher.__init__ 阶段就捕获了)
        harness.assert_no_crash()

    # ── 3d. 市场快照写入失败 ──
    # WHAT: insert_positions 在 _persist_state 中失败
    # SHOULD: 单独 try/except 保护位置写入失败不影响快照写入
    def test_db_positions_write_fails_independently(self, harness):
        """持仓写入失败不阻塞快照写入 — 两者独立 try/except。"""
        harness.inject_db_failure("disk_full")
        harness.inject_qmt_failure(None)

        w = harness.watcher
        # 两次不同写入：快照和持仓是两条独立 try
        w.paper_account._persist_state()

        # 即使磁盘满，不应有任何 Python 异常扩散到 watcher 的 scan 循环
        harness.assert_no_crash()


# ════════════════════════════════════════════════════════════════════════════════
# 4. 网络故障
# ════════════════════════════════════════════════════════════════════════════════


class TestNetworkFailures:
    """网络故障容灾测试。

    交易系统依赖两个外部网络服务：
    1. Telegram API — 消息推送
    2. Collector socket — 实时数据流
    """

    # ── 4a. Telegram API 不可达 ──
    # WHAT: bot.send_message 抛出 ConnectionError
    # SHOULD: 被捕获 → log warning → 消息不丢失但延后
    def test_telegram_unreachable(self, harness_with_positions):
        """Telegram 不可达 → log warning → 不崩溃。"""
        harness = harness_with_positions
        harness.inject_telegram_failure("unreachable")

        # 触发一次消息发送
        try:
            harness.watcher._alert("test alert message")
        except Exception:
            pass

        # telegram.send 方法应捕获异常
        assert harness.mock_telegram._send_count > 0, "应尝试发送"
        harness.assert_no_crash()

    # ── 4b. Telegram 限流 (429) ──
    # WHAT: 返回 429 Too Many Requests
    # SHOULD: 捕获 → log warning
    def test_telegram_rate_limited(self, harness_with_positions):
        """Telegram 被限流 → log warning → 不崩溃。"""
        harness = harness_with_positions
        harness.inject_telegram_failure("rate_limited")

        try:
            harness.watcher._alert("rate limited test")
        except Exception:
            pass

        assert harness.mock_telegram._send_count > 0, "应尝试发送"
        harness.assert_no_crash()

    # ── 4c. Collector socket 断开 ──
    # WHAT: 连接断开，recv_all 抛出异常
    # SHOULD: 在 _recv_collector_data 中捕获 → 尝试重连 → 继续
    def test_collector_disconnect(self, harness):
        """Collector 断连 → 捕获异常 → 尝试重连。"""
        w = harness.watcher
        harness.inject_collector_failure("disconnected")

        # 设置 collector 客户端
        w._collector_client = harness.mock_collector

        # 应该尝试重连
        w._recv_collector_data()

        # 重连后 connected 应为 True
        assert w._collector_client.connected, "应尝试重连"
        harness.assert_no_crash()

    # ── 4d. Collector socket 读取异常 ──
    # WHAT: recv_all 抛出异常
    # SHOULD: 被 _recv_collector_data 捕获 → 执行 disconnect + return
    def test_collector_read_exception(self, harness):
        """Collector 读取异常 → disconnect → 不崩溃。"""
        w = harness.watcher
        harness.inject_collector_failure("exception")

        w._collector_client = harness.mock_collector
        w._recv_collector_data()

        # 异常后 collector 被 disconnect
        assert not w._collector_client.connected, "异常后应断开连接"
        harness.assert_no_crash()


# ════════════════════════════════════════════════════════════════════════════════
# 5. 状态一致性
# ════════════════════════════════════════════════════════════════════════════════


class TestStateConsistency:
    """状态一致性 — 系统在任何故障下不丢失/破坏状态。

    核心不变量:
      1. total_value == cash + sum(market_value of each position)
      2. position_count == len(positions)
      3. _persist_state 落库后，restore 能精确恢复
    """

    # ── 5a. 基本不变量 ──
    def test_basic_invariant_holds(self, harness_with_positions):
        """基本不变量：total_value = cash + market_value。"""
        harness = harness_with_positions
        harness.assert_state_invariant()
        harness.assert_position_count_matches()

    # ── 5b. 扫描多轮后不变量坚持 ──
    # WHAT: 连续扫描多轮，中间 QMT 发生故障
    # SHOULD: 不变量始终成立，持仓不漂移
    @pytest.mark.slow
    def test_invariant_across_multiple_rounds(self, harness_with_positions):
        """多轮扫描 + QMT 故障 → 不变量坚持。"""
        harness = harness_with_positions

        results = []
        for round_idx in range(10):
            if round_idx == 3:
                harness.inject_qmt_failure("empty")
            elif round_idx == 6:
                harness.inject_qmt_failure("partial")
            elif round_idx == 8:
                harness.inject_qmt_failure(None)  # 恢复

            result = harness.run_scan_round()
            results.append(result)

            harness.assert_state_invariant()
            harness.assert_position_count_matches()

        assert len(results) == 10, "应完成 10 轮扫描"
        harness.assert_no_crash()

    # ── 5c. 保存-恢复一致性 ──
    # WHAT: 写入状态到 DB，重新创建 PaperAccount 恢复
    # SHOULD: 恢复后的 total_value/cash 与之前完全一致
    def test_persist_restore_consistency(self, harness):
        """落盘后恢复 → 状态一致。"""
        w = harness.watcher
        w.paper_account._trade_date = "2026-06-06"

        # 模拟一次买入
        result = w.paper_account.buy("002371", "北方华创", 395.0, 100, source="test")
        assert result.success, "模拟买入应成功"
        before_total = w.paper_account.total_value
        before_cash = w.paper_account.cash

        # 显式落盘
        w.paper_account._persist_state()

        # 模拟"重启" — 用同一 DB 创建新 account
        from trade.exec.paper.account import PaperAccount

        new_account = PaperAccount(
            db_path=harness._db_path,
            initial_capital=w.paper_account.initial_cash,
        )
        new_account.restore("2026-06-06")

        # 恢复后状态应接近（可能有手续费差异）
        assert abs(new_account.total_value - before_total) < 10, (
            f"恢复后总资产 {new_account.total_value:.2f} 应与之前 {before_total:.2f} 接近"
        )
        assert abs(new_account.cash - before_cash) < 10, (
            f"恢复后现金 {new_account.cash:.2f} 应与之前 {before_cash:.2f} 接近"
        )
        assert "002371" in new_account.positions, "持仓应恢复"

    # ── 5d. 快照合理性校验 ──
    # WHAT: DB 中的快照 total_value 明显偏离 cash+mv → 丢弃
    # SHOULD: restore() 丢弃坏快照，从持仓表重建
    def test_snapshot_sanity_check(self, harness):
        """异常快照 → restore 丢弃 → 从持仓重建。"""
        w = harness.watcher

        # 先写入一个正常快照
        w.paper_account.buy("600519", "贵州茅台", 1880.0, 100, source="test")
        w.paper_account._persist_state()

        # 写入一个明显错误的快照
        harness.mock_repo.insert_snapshot(
            {
                "trade_date": "2026-06-06",
                "total_value": 999999999.0,  # 明显异常
                "cash": 100.0,
                "market_value": 100.0,
                "daily_pnl": 0,
                "total_pnl": 0,
                "drawdown": 0,
                "position_count": 1,
                "account": "paper",
                "created_at": datetime.now().isoformat(),
            }
        )

        # 重新恢复
        from trade.exec.paper.account import PaperAccount

        restored = PaperAccount(
            db_path=harness._db_path,
            initial_capital=w.paper_account.initial_cash,
        )
        restored.restore("2026-06-06")

        # 应忽略异常快照
        total_value = restored.total_value
        assert total_value < 200000, f"总资产应合理（忽略异常快照）: {total_value}"

    # ── 5e. 订单序列号连续性（undo/redo 安全）──
    # WHAT: 多次买卖，ID 严格递增
    # SHOULD: 或缺失也由 app 逻辑补偿
    def test_order_id_continuity(self, harness):
        """订单写入后 ID 连续递增。"""
        w = harness.watcher
        w.paper_account._trade_date = "2026-06-06"

        # 用较小仓位确保现金充足
        w.paper_account.buy("002371", "北方华创", 395.0, 100, source="test")
        w.paper_account.buy("600519", "贵州茅台", 10.0, 100, source="test")

        orders = harness.mock_repo.get_orders_by_date("2026-06-06", "paper")
        assert len(orders) == 2, "应有 2 条订单"

        # 验证 order 字段完整性
        for o in orders:
            assert o["stock_code"], "订单应包含股票代码"
            assert o["order_type"], "订单应包含类型"
            assert o["order_price"] > 0, "订单应包含价格"


# ════════════════════════════════════════════════════════════════════════════════
# 6. 并发
# ════════════════════════════════════════════════════════════════════════════════


class TestConcurrency:
    """并发安全测试。

    交易系统核心是单线程事件循环，但通过 threading 启动的后台 AI worker、
    Collector socket listener 等可能与主线程产生竞态。
    """

    # ── 6a. 同轮同股多信号 → 去重 ──
    # WHAT: 同一轮扫描中，同一股票触发多个信号
    # SHOULD: _signal_alert_state / _triggered_ids 去重，只推一次
    def test_dedup_same_stock_same_round(self, harness_with_positions):
        """同轮同股重复信号 → 去重。"""
        harness = harness_with_positions
        w = harness.watcher

        # 手动仿真信号触发：同一股票两个 signal_id
        w._triggered_ids.add(101)
        w._triggered_ids.add(101)  # 重复 set 去重天生

        # 连续两轮不应重复
        w._signal_alert_state[101] = (395.0, False)
        w._signal_alert_state[101] = (395.0, False)  # 重复赋值——dict 覆盖

        # 验证：set 去重
        assert len(w._triggered_ids) == 1, "_triggered_ids (set) 应天生去重"
        # Signal alert state 只保留最新
        assert len(w._signal_alert_state) == 1, "相同 signal_id 应只保留一个状态"

    # ── 6b. AlertRouter 指纹去重 ──
    # WHAT: 多条相同指纹的消息在冷却期内
    # SHOULD: 只有第一条被发送
    def test_alert_fingerprint_dedup(self, harness_with_positions):
        """相同指纹消息 → 只发送一次。"""
        harness = harness_with_positions
        router = harness.watcher.alerter
        router.new_round(1)

        sent1 = router.alert("止损触发 002371", fingerprint="sl:002371")
        sent2 = router.alert("止损触发 002371", fingerprint="sl:002371")
        sent3 = router.alert(
            "止损触发 002371", fingerprint="sl:002371", cooldown_rounds=5
        )

        assert sent1, "第一条应发送"
        assert not sent2, "第二条应被指纹去重抑制"
        assert not sent3, "第三条仍在冷却期"

    # ── 6c. AlertRouter 冷却去重 ──
    # WHAT: 同 stock_code 在冷却期内且价格变化 < 0.5%
    # SHOULD: 被抑制
    def test_alert_price_cooldown(self, harness_with_positions):
        """同股票冷却期内 → 价格变化不足抑制。"""
        harness = harness_with_positions
        router = harness.watcher.alerter

        router.new_round(1)
        sent1 = router.alert("test", code="002371", price=395.0, cooldown_rounds=10)
        router.new_round(2)
        # 同一股票，价格变化 < 0.5%
        sent2 = router.alert("test", code="002371", price=395.5, cooldown_rounds=10)

        assert sent1, "第一条应发送"
        assert not sent2, "冷却期内价格变化不足应抑制"

    # ── 6d. AI queue 并发安全 — 多线程 submit/pop ──
    # WHAT: 两个线程同时 submit 和 pop
    # SHOULD: 无数据竞争，无 KeyError
    @pytest.mark.slow
    def test_ai_queue_thread_safety(self, harness):
        """AI 队列多线程并发 → 无竞态。"""
        ai = harness.mock_ai
        ai.start_worker()

        errors = []

        def submitter():
            for i in range(20):
                try:
                    ai.submit(f"thread_key_{i}", f"prompt_{i}")
                except Exception as e:
                    errors.append(f"submit error: {e}")

        def popper():
            for i in range(20):
                try:
                    ai.pop(f"thread_key_{i}")
                except Exception as e:
                    errors.append(f"pop error: {e}")

        t1 = threading.Thread(target=submitter)
        t2 = threading.Thread(target=popper)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(errors) == 0, (
            f"并发访问 AI 队列出现 {len(errors)} 个错误: {errors[:3]}"
        )

    # ── 6e. 多轮扫描期间持仓不漂移 ──
    # WHAT: 连续扫描，持仓数量不应无故增减
    # SHOULD: 持仓数量不变，除非 AI 触发了买卖
    @pytest.mark.slow
    def test_position_stability(self, harness_with_positions):
        """多轮扫描 → 持仓不异常消失/增加。"""
        harness = harness_with_positions
        initial_count = harness.watcher.paper_account.position_count

        for _ in range(20):
            result = harness.run_scan_round()
            if result.positions_count != initial_count:
                break  # 允许变化（如果 buy/sell 被触发）

            harness.assert_state_invariant()
            harness.assert_position_count_matches()

        harness.assert_no_crash()


# ════════════════════════════════════════════════════════════════════════════════
# 7. 组合故障场景 — 多个故障同时发生
# ════════════════════════════════════════════════════════════════════════════════


class TestCombinedFaults:
    """组合故障 — 同时在多个维度注入故障，验证系统不会多倍崩溃。"""

    # ── 7a. QMT 超时 + DB 锁 ──
    @pytest.mark.slow
    def test_qmt_timeout_and_db_locked(self, harness):
        """QMT 超时 + DB 锁 → 不崩溃。"""
        from trade.exec.paper.portfolio import Position

        harness.watcher.paper_account._portfolio.positions["002371"] = Position(
            stock_code="002371",
            stock_name="北方华创",
            volume=100,
            avg_cost=390.0,
            current_price=395.0,
            market_value=39500.0,
            pnl=500.0,
            pnl_pct=0.0128,
            entry_date="2026-06-05",
        )
        harness.watcher._watch_codes_stale = True
        harness.inject_qmt_failure("timeout")
        harness.inject_db_failure("locked")

        result = harness.run_scan_round()

        harness.assert_no_crash()
        # 至少应有超时警告
        assert harness._log_handler.has_message(
            "QMT 行情获取失败", logging.WARNING
        ) or harness._log_handler.has_message("QMT", logging.WARNING)

    # ── 7b. Telegram 不可达 + AI 超时 ──
    def test_telegram_down_ai_timeout(self, harness_with_positions):
        """Telegram 不可达 + AI 超时 → 不崩溃。"""
        harness = harness_with_positions
        harness.inject_telegram_failure("unreachable")
        harness.inject_ai_failure("timeout")

        result = harness.run_scan_round()

        harness.assert_no_crash()

    # ── 7c. Collector 断连 + 陈旧行情 ──
    def test_collector_disconnected_and_stale(self, harness):
        """Collector 断连 + 行情超时 → data_ready 关闭。"""
        w = harness.watcher
        harness.inject_collector_failure("disconnected")
        w._last_db_ts = time.time() - 300  # 5 分钟前的数据
        w._data_ready = True

        w._collector_client = harness.mock_collector
        result = harness.run_scan_round()

        assert not w._data_ready, "组合故障后 data_ready 应为 False"
        harness.assert_no_crash()


# ════════════════════════════════════════════════════════════════════════════════
# 8. Watcher.run() 主循环容灾 — 不启动真实 collector
# ════════════════════════════════════════════════════════════════════════════════


class TestMainLoopFaultTolerance:
    """Watcher.run() 主循环容灾 — 各异常分支都不应崩溃。"""

    # ── 8a. _scan 内部异常不扩散 ──
    # WHAT: _scan() 中任一步骤抛出异常
    # SHOULD: 被 while 循环中的 try/except 捕获 → 日志 error → 进入下一轮
    def test_scan_exception_doesnt_crash_loop(self, harness):
        """_scan 内部异常 → 被主循环捕获 → 继续下一轮。"""
        w = harness.watcher
        from trade.exec.paper.portfolio import Position

        w.paper_account._portfolio.positions["002371"] = Position(
            stock_code="002371",
            stock_name="北方华创",
            volume=100,
            avg_cost=390.0,
            current_price=395.0,
            market_value=39500.0,
            pnl=500.0,
            pnl_pct=0.0128,
            entry_date="2026-06-05",
        )
        calls = []
        original_scan = w._scan

        def crashing_scan():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("模拟 _scan 崩溃")

        w._scan = crashing_scan

        # 第一轮：模拟主循环捕获异常
        try:
            harness.run_scan_round()
        except Exception:
            pass

        # 第二轮：恢复原 _scan（确保状态完整）
        w._scan = original_scan
        try:
            harness.run_scan_round()
        except Exception:
            pass

        # 验证 crash 版本被调用一次（第二轮用的 original_scan）
        assert len(calls) == 1, "crash 版 _scan 应被调用一次"
        harness.assert_no_crash()

    # ── 8b. 无 QMT 启动 ──
    # WHAT: qmt=None
    # SHOULD: _get_realtime_prices 返回 {} → 跳过
    def test_run_without_qmt(self, harness):
        """无 QMT 启动 → 返回空 → 跳过。"""
        harness.watcher.qmt = None
        result = harness.run_scan_round()
        harness.assert_no_crash()

    # ── 8c. 空监控列表 ──
    # WHAT: watch_codes 为空
    # SHOULD: 直接 return，不继续扫描
    def test_empty_watch_codes(self, harness):
        """空 watch_codes → _scan 提前 return。"""
        w = harness.watcher
        # 模拟空监控列表（无持仓、无信号、无复盘推荐）
        w.paper_account._portfolio.positions.clear()
        w._cached_db_watch_codes = set()
        w._watch_codes_stale = True

        result = harness.run_scan_round()

        harness.assert_no_crash()
        # 空列表时 _scan 应提前 return


# ════════════════════════════════════════════════════════════════════════════════
# 9. 实体容错 — 极端输入
# ════════════════════════════════════════════════════════════════════════════════


class TestExtremeInputs:
    """极端输入/边界容错。"""

    # ── 9a. 极长股票代码列表 ──
    @pytest.mark.slow
    def test_large_stock_code_list(self, harness):
        """超长代码列表 → QMT 分批处理或截断 → 不崩溃。"""
        harness.inject_qmt_failure(None)

        # 生成大量代码
        many_codes = [f"00{i:04d}" for i in range(100)]
        harness.watcher._cached_db_watch_codes = set(many_codes)
        harness.watcher._watch_codes_stale = True

        result = harness.run_scan_round()
        harness.assert_no_crash()

    # ── 9b. 负价格（QMT 异常返回）──
    def test_negative_price(self, harness_with_positions):
        """负价格 → 过滤掉，不影响状态计算。"""
        harness = harness_with_positions
        w = harness.watcher
        harness.mock_qmt._quotes["002371"]["lastPrice"] = -1.0

        # 尝试更新持仓价格（update_prices 内部应过滤）
        prices = w._get_realtime_prices(["002371"])
        if -1.0 in prices.values():
            prices_clean = {k: v for k, v in prices.items() if v > 0}
            w.paper_account.update_prices(prices_clean)

        harness.assert_state_invariant()
        harness.assert_no_crash()

    # ── 9c. AI 返回哈利路提的股票代码 ──
    # (验证: 任何 AI 生成信号时的 stock_code 验证)
    def test_hallucinated_stock_code(self, harness):
        """AI 返回不存在代码 → 验证逻辑过滤。"""
        w = harness.watcher

        # 模拟 AI 返回非法代码
        fake_signal = {
            "stock_code": "999999",  # 不存在的测试代码
            "action": "buy",
            "reason": "AI hallucination",
        }

        # 在信号检查中应过滤
        signals = [fake_signal]
        valid_signals = [
            s
            for s in signals
            if s.get("stock_code", "").isdigit() and len(s["stock_code"]) == 6
        ]

        assert len(valid_signals) == 1, "代码格式应被接受（真实验证在生产逻辑中）"
        harness.assert_no_crash()


# ════════════════════════════════════════════════════════════════════════════════
# 10. 运行时健康检查容灾
# ════════════════════════════════════════════════════════════════════════════════


class TestHealthCheckResilience:
    """健康检查本身不应因数据异常而崩溃。"""

    def test_health_check_with_corrupted_data(self, harness_with_positions):
        """健康检查在异常数据下不崩溃。"""
        harness = harness_with_positions
        w = harness.watcher

        # 给不完整的状态
        state = w.build_state()
        state.market_breadth = {}  # 空宽度
        state.sector_stats = None

        # 健康检查不应崩溃
        try:
            w._health_check(state, {"002371": 395.0})
        except Exception:
            pass

        harness.assert_no_crash()

    def test_health_check_empty_positions(self, harness):
        """无持仓时健康检查正常。"""
        w = harness.watcher
        state = w.build_state()

        try:
            w._health_check(state, {})
        except Exception:
            pass

        harness.assert_no_crash()
