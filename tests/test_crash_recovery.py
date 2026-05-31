# -*- coding: utf-8 -*-
"""盘中重启容灾全流程模拟测试。

模拟真实场景：
1. 交易进行中 Watcher 崩溃
2. DB 中有 collector 写入的 index_snapshots + market_snapshots
3. Watcher 重启: 先连 collector → 读 DB → ts 去重 → 后续增量

验证点:
- 先连 socket 再读 DB（防止读 DB 期间丢数据）
- O(1) ts 去重: DB 已有 ts 跳过，新 ts 应用
- index_prices/high/low/turnovers 从 DB 正确恢复
- 恢复后增量数据正常追加
- 边界条件: ts 精确等于/略小于/略大于 _last_db_ts

用法: python3 tests/test_crash_recovery.py
"""

import json
import logging
import os
import socket
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)


# ═══════════════════════════════════════════════
# Mock Collector TCP Server
# ═══════════════════════════════════════════════

class MockCollector:
    def __init__(self, port):
        self.port = port
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", port))
        self._server.listen(1)
        self._client = None
        self._running = True
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._client:
            try:
                self._client.close()
            except OSError:
                pass
        try:
            self._server.close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        self._server.settimeout(1.0)
        while self._running:
            try:
                sock, addr = self._server.accept()
                self._client = sock
                break
            except socket.timeout:
                continue
            except OSError:
                return

    def send(self, msg: dict):
        if not self._client:
            return False
        try:
            raw = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            self._client.sendall(raw)
            return True
        except OSError:
            return False


# ═══════════════════════════════════════════════
# 可重置的 Watcher
# ═══════════════════════════════════════════════

class RecoverableWatcher:
    def __init__(self, db_path, port):
        self.db_path = db_path
        self._db_conn = sqlite3.connect(db_path)
        self.port = port

        from data.live.collector_client import DataCollectorClient
        self._collector_client = DataCollectorClient(port=port)
        self._last_index_quote: dict | None = None
        self._last_db_ts: float = 0

        self._index_prices: list[float] = []
        self._index_high: float = 0
        self._index_low: float = 0
        self._market_turnovers: list[float] = []
        self._market_snapshot: dict[str, dict] = {}
        self._trade_date = datetime.now().strftime("%Y-%m-%d")
        self.scan_interval = 60
        self._scan_count = 0

        self._sector_stats: dict[str, dict] = {}
        self._concept_stats: dict[str, dict] = {}
        self._sector_trend_history: dict[str, list[float]] = defaultdict(list)
        self._sector_trend_continuity: dict[str, int] = defaultdict(int)
        self._sector_trend_last_dir: dict[str, str] = {}
        self._industry_cache: dict[str, str] = {}
        self._concept_cache: dict[str, list[str]] = {}
        self._prev_ind_amounts: dict[str, float] = {}
        self._prev_con_amounts: dict[str, float] = {}

        self._init_db()
        self._init_cache()

    def _init_db(self):
        self._db_conn.execute("""
            CREATE TABLE IF NOT EXISTS index_snapshots (
                trade_date TEXT NOT NULL, ts REAL NOT NULL,
                price REAL NOT NULL DEFAULT 0, high REAL DEFAULT 0,
                low REAL DEFAULT 0, pre_close REAL DEFAULT 0,
                change_pct REAL DEFAULT 0, amount REAL DEFAULT 0,
                PRIMARY KEY (trade_date, ts)
            )
        """)
        self._db_conn.execute("""
            CREATE TABLE IF NOT EXISTS market_snapshots (
                trade_date TEXT NOT NULL, ts TEXT NOT NULL,
                code TEXT NOT NULL, change_pct REAL DEFAULT 0,
                price REAL DEFAULT 0, amount REAL DEFAULT 0,
                PRIMARY KEY (trade_date, ts, code)
            )
        """)
        self._db_conn.commit()

    def _init_cache(self):
        for i in range(16):
            ind = ["银行", "半导体", "医药", "汽车"][i // 4]
            self._industry_cache[f"{i:06d}"] = ind
            con = ["金融", "芯片", "创新药", "新能源"][i // 4]
            self._concept_cache[f"{i:06d}"] = [con]

    # ── 辅助方法 ──
    def _get_index_quote(self):
        return self._last_index_quote

    def _get_index_baseline(self):
        return (3300, 3320, 3350)

    def _get_index_ma60(self):
        return 3200.0

    def _save_sector_snapshots(self, *a, **kw):
        pass

    def _resolve_name(self, code):
        return f"股票{code}"

    def _get_paper_trader(self): return None
    def _get_review_monitor(self): return None
    def _load_review_signal_zones(self): return {}
    def _get_intraday_indicators(self, code): return {"available": False}
    def _get_order_book_imbalance(self, code, price): return 0.5, ""
    def _get_big_order_direction(self, code): return 0.5, ""
    def _get_instrument_info(self, code): return {"up_stop": 999, "down_stop": 0.01}
    def _alert(self, msg): pass
    def _alert_private(self, msg): pass
    def _invalidate_watch_codes_cache(self): pass

    # ── collector ──
    def connect(self):
        return self._collector_client.connect()

    @property
    def connected(self):
        return self._collector_client.connected

    def recv_and_process(self):
        if not self._collector_client.connected:
            self.connect()
            return 0, 0

        try:
            messages = self._collector_client.recv_all()
        except Exception:
            self._collector_client.disconnect()
            return 0, 0

        applied = 0
        skipped = 0
        for msg in messages:
            msg_ts = msg.get("ts", 0)
            if self._last_db_ts > 0 and msg_ts <= self._last_db_ts:
                skipped += 1
                continue

            msg_type = msg.get("type")
            if msg_type == "index":
                self._handle_collector_index(msg)
                applied += 1
            elif msg_type == "market":
                self._handle_collector_market(msg)
                applied += 1

        return applied, skipped

    def _handle_collector_index(self, msg):
        self._last_index_quote = {
            "price": msg["price"],
            "pre_close": msg.get("pre_close", 0),
            "change_pct": msg.get("change_pct", 0),
            "amount": msg.get("amount", 0),
        }
        idx = msg["price"]
        if self._index_high == 0 or idx > self._index_high:
            self._index_high = idx
        if self._index_low == 0 or idx < self._index_low:
            self._index_low = idx
        self._index_prices.append(idx)
        amt = msg.get("amount", 0)
        if amt > 0:
            self._market_turnovers.append(amt)

    def _handle_collector_market(self, msg):
        self._market_snapshot = msg.get("stocks", {})

    # ── DB 写入 ──
    def _write_index_to_db(self, ts: float, price: float, pre_close: float,
                           change_pct: float, amount: float):
        self._db_conn.execute(
            """INSERT OR REPLACE INTO index_snapshots
               (trade_date, ts, price, high, low, pre_close, change_pct, amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (self._trade_date, ts, price, price, price, pre_close, change_pct, amount),
        )
        self._db_conn.commit()

    def _write_market_to_db(self, ts_str: str, stocks: dict):
        rows = [(self._trade_date, ts_str, code,
                 round(float(item.get("changePct", 0)), 4),
                 round(float(item.get("price", 0)), 4),
                 round(float(item.get("amount", 0)), 2))
                for code, item in stocks.items()]
        self._db_conn.executemany(
            """INSERT OR REPLACE INTO market_snapshots
               (trade_date, ts, code, change_pct, price, amount)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
        self._db_conn.commit()

    # ── DB 恢复（与 watcher.py 一致）──
    def _restore_from_db(self):
        restored_index = 0
        restored_market = 0

        try:
            rows = self._db_conn.execute(
                """SELECT ts, price, high, low, amount FROM index_snapshots
                   WHERE trade_date=? ORDER BY ts ASC""",
                (self._trade_date,),
            ).fetchall()

            if rows and len(rows) >= 5:
                for ts_val, price, high, low, amount in rows:
                    self._index_prices.append(price)
                    if high and high > self._index_high:
                        self._index_high = high
                    if low and (self._index_low == 0 or low < self._index_low):
                        self._index_low = low
                    if amount and amount > 0:
                        self._market_turnovers.append(amount)
                    if ts_val > self._last_db_ts:
                        self._last_db_ts = ts_val
                restored_index = len(rows)
                last = rows[-1]
                self._last_index_quote = {
                    "price": last[1], "pre_close": 3300.0,
                    "change_pct": (last[1] - 3300) / 3300, "amount": last[4],
                }
        except Exception as e:
            print(f"  [restore] 指数恢复异常: {e}")

        try:
            rows = self._db_conn.execute(
                """SELECT ts, code, change_pct, price, amount FROM market_snapshots
                   WHERE trade_date=? ORDER BY ts DESC LIMIT 8000""",
                (self._trade_date,),
            ).fetchall()

            if rows:
                latest_ts = rows[0][0]
                batch = [r for r in rows if r[0] == latest_ts]
                self._market_snapshot = {}
                for ts_val, code, chg, price, amount in batch:
                    self._market_snapshot[code] = {
                        "changePct": chg, "price": price or 0, "amount": amount or 0,
                    }
                restored_market = len(batch)
                db_ts = float(latest_ts) if latest_ts else 0
                self._last_db_ts = max(self._last_db_ts, db_ts)
        except Exception as e:
            print(f"  [restore] 市场快照恢复异常: {e}")

        return restored_index, restored_market

    def close(self):
        try:
            self._collector_client.disconnect()
        except Exception:
            pass
        try:
            self._db_conn.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════

def _mk_index(ts, price, pre_close=3290.0, amount=80e9):
    return {"type": "index", "ts": ts, "price": price,
            "pre_close": pre_close, "change_pct": (price - pre_close) / pre_close,
            "amount": amount}

def _mk_market(ts, stocks):
    return {"type": "market", "ts": ts, "stocks": stocks}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 1: 完整盘中重启流程
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.skip(reason="独立运行: python tests/test_crash_recovery.py")
def test_full_crash_recovery_flow(port):
    print("=" * 70)
    print("  测试 1: 完整盘中重启容灾流程")
    print("=" * 70)

    db_path = ":memory:"
    mock = MockCollector(port)
    mock2 = None

    try:
        mock.start()
        time.sleep(0.3)

        w = RecoverableWatcher(db_path, port)
        w.connect()
        time.sleep(0.3)

        # ━━ Phase 1: 正常运行 5 轮 ━━
        print("\n  ━━ Phase 1: 正常运行 (5 轮) ━━")
        ts_seq = []
        for r in range(5):
            idx_ts = 1000.0 + r * 60
            ts_seq.append(idx_ts)
            price = 3300.0 + r * 1.5
            mock.send(_mk_index(idx_ts, price))

            stocks = {}
            for i in range(16):
                stocks[f"{i:06d}"] = {"price": 10.0 + r * 0.1,
                                       "changePct": 0.005 + r * 0.001,
                                       "amount": 100_000_000 + r * 1_000_000}
            mock.send(_mk_market(idx_ts + 0.1, stocks))

        time.sleep(0.3)
        applied, skipped = w.recv_and_process()
        print(f"  Phase1: applied={applied} skipped={skipped}")

        # Phase1 写 DB
        for r in range(5):
            idx_ts = 1000.0 + r * 60
            price = 3300.0 + r * 1.5
            w._write_index_to_db(idx_ts, price, 3290.0, (price - 3290) / 3290, 80e9 + r * 1e9)

        stocks_db = {}
        for i in range(16):
            stocks_db[f"{i:06d}"] = {"price": 10.4, "changePct": 0.009, "amount": 104_000_000}
        w._write_market_to_db(str(ts_seq[-1] + 0.1), stocks_db)

        print(f"  DB: 5 index + 16 market")
        print(f"  State: index_prices={len(w._index_prices)} high={w._index_high:.1f}")

        # ━━ Phase 2: 崩溃，collector 继续推送 3 轮到 DB ━━
        print("\n  ━━ Phase 2: Watcher 崩溃，Collector 继续推 3 轮 ━━")
        w._collector_client.disconnect()
        mock.stop()
        mock = None

        mock2 = MockCollector(port)
        mock2.start()
        time.sleep(0.3)

        new_base = ts_seq[-1] + 120  # 崩溃过了 2min
        for r in range(3):
            idx_ts = new_base + r * 60
            price = 3307.5 + r * 2.0
            mock2.send(_mk_index(idx_ts, price))
            stocks = {}
            for i in range(16):
                stocks[f"{i:06d}"] = {"price": 10.5 + r * 0.1,
                                       "changePct": 0.01 + r * 0.002,
                                       "amount": 105_000_000}
            mock2.send(_mk_market(idx_ts + 0.1, stocks))
            w._write_index_to_db(idx_ts, price, 3290.0, (price - 3290) / 3290, 85e9)

        last_mkt = {}
        for i in range(16):
            last_mkt[f"{i:06d}"] = {"price": 10.7, "changePct": 0.014, "amount": 107_000_000}
        w._write_market_to_db(str(new_base + 2 * 60 + 0.1), last_mkt)

        print(f"  DB 累积: 8 index + 16 market")

        # ━━ Phase 3: 盘中重启 ━━
        print("\n  ━━ Phase 3: 盘中重启 — 先连 socket → 读 DB → 去重 ━━")

        # Step 1: 先连 socket
        ok = w.connect()
        print(f"  [1] 重连: {'✓' if ok else '✗'}")
        assert ok
        time.sleep(0.3)

        # collector 推送 2 轮（堆积在 socket buffer）
        restart_base = new_base + 3 * 60
        mock2.send(_mk_index(restart_base, 3313.5))
        mock2.send(_mk_index(restart_base + 60, 3315.3))
        time.sleep(0.3)

        # Step 2: 读 DB 恢复
        n_idx, n_mkt = w._restore_from_db()
        print(f"  [2] DB 恢复: index={n_idx} market={n_mkt}  _last_db_ts={w._last_db_ts:.1f}")
        assert n_idx >= 8 and n_mkt >= 16

        # Step 3: 处理 socket buffer → 去重
        applied, skipped = w.recv_and_process()
        print(f"  [3] Buffer 处理: applied={applied} skipped={skipped}")
        assert applied == 2, f"重连后 2 条应应用: {applied}"
        assert w._index_prices[-1] == 3315.3

        print(f"  最新价: {w._index_prices[-1]:.1f}  "
              f"high={w._index_high:.1f} low={w._index_low:.1f}")

        # ━━ Phase 4: 后续增量 ━━
        print("\n  ━━ Phase 4: 后续增量 ━━")
        mock2.send(_mk_index(restart_base + 120, 3320.0))
        time.sleep(0.2)
        applied, skipped = w.recv_and_process()
        assert applied == 1 and skipped == 0
        assert w._index_prices[-1] == 3320.0
        print(f"  增量: applied={applied} skipped={skipped}  price=3320.0")

        print(f"\n  ✅ 完整盘中重启容灾流程通过")

    finally:
        if mock:
            mock.stop()
        if mock2:
            mock2.stop()
        try:
            w.close()
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 2: 去重边界条件
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.skip(reason="独立运行: python tests/test_crash_recovery.py")
def test_dedup_boundary_conditions(port):
    print("\n" + "=" * 70)
    print("  测试 2: 去重边界条件")
    print("=" * 70)

    mock = MockCollector(port)
    w = None
    try:
        mock.start()
        time.sleep(0.3)

        w = RecoverableWatcher(":memory:", port)
        w.connect()
        time.sleep(0.3)

        w._last_db_ts = 5000.0

        mock.send(_mk_index(4999.9, 100.0))
        mock.send(_mk_index(5000.0, 200.0))
        mock.send(_mk_index(5000.1, 300.0))
        time.sleep(0.3)

        applied, skipped = w.recv_and_process()
        print(f"  applied={applied} skipped={skipped}")
        assert applied == 1, f"只有 ts=5000.1 应被应用: {applied}"
        assert skipped == 2, f"2 条应被跳过: {skipped}"
        assert w._index_prices == [300.0]
        print(f"  ✅ ts=4999.9 跳过, ts=5000.0 跳过, ts=5000.1 应用")
    finally:
        mock.stop()
        if w:
            w.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 3: 零遗漏验证
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.skip(reason="独立运行: python tests/test_crash_recovery.py")
def test_zero_miss_during_db_read(port):
    print("\n" + "=" * 70)
    print("  测试 3: 零遗漏（先连 socket → 消息缓冲 → 读 DB → 处理 buffer）")
    print("=" * 70)

    mock = MockCollector(port)
    w = None
    try:
        mock.start()
        time.sleep(0.3)

        w = RecoverableWatcher(":memory:", port)

        # 预写 5 轮到 DB
        for r in range(5):
            idx_ts = 1000.0 + r * 60
            w._write_index_to_db(idx_ts, 3300.0 + r * 1.5, 3290.0, (3300 + r * 1.5 - 3290) / 3290, 80e9)
        print(f"  [准备] DB 中 5 轮 index_snapshots")

        # Step 1: 先连 socket
        ok = w.connect()
        time.sleep(0.3)
        assert ok
        print(f"  [1] 先连 collector socket")

        # collector 推送 3 条（堆积在 buffer）
        batch_ts = [1000.0 + 5 * 60 + 10 + i * 60 for i in range(3)]
        for i, ts in enumerate(batch_ts):
            mock.send(_mk_index(ts, 3310.0 + i * 2.0))
        time.sleep(0.5)
        print(f"  [2] 3 条消息在 socket buffer 中")

        # Step 2: 读 DB
        n_idx, n_mkt = w._restore_from_db()
        assert n_idx == 5
        print(f"  [3] DB 恢复: {n_idx} 条  _last_db_ts={w._last_db_ts:.1f}")
        assert abs(w._last_db_ts - 1240.0) < 1.0

        # Step 3: 处理 buffer — 3 条全应用，零丢失
        applied, skipped = w.recv_and_process()
        assert applied == 3 and skipped == 0, f"applied={applied} skipped={skipped}"
        assert len(w._index_prices) == 8  # 5 DB + 3 buffer
        assert w._index_prices[-1] == 3314.0
        print(f"  [4] Buffer 全部应用: {applied} 条  总计 index_prices={len(w._index_prices)}")
        print(f"  ✅ 零遗漏")

    finally:
        mock.stop()
        if w:
            w.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 测试 4: 盘前正常启动
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.mark.skip(reason="独立运行: python tests/test_crash_recovery.py")
def test_normal_start_no_db_read(port):
    print("\n" + "=" * 70)
    print("  测试 4: 盘前正常启动 — 不读 DB")
    print("=" * 70)

    mock = MockCollector(port)
    w = None
    try:
        mock.start()
        time.sleep(0.3)

        w = RecoverableWatcher(":memory:", port)
        w.connect()
        time.sleep(0.3)

        mock.send(_mk_index(100.0, 3310.0))
        time.sleep(0.2)
        applied, skipped = w.recv_and_process()
        assert applied == 1 and skipped == 0
        assert w._index_prices == [3310.0]
        print(f"  applied={applied}  price={w._last_index_quote['price']:.1f}")
        print(f"  ✅ 不读 DB，直接收 collector")
    finally:
        mock.stop()
        if w:
            w.close()


# ═══════════════════════════════════════════════

def main():
    print("盘中重启容灾全流程模拟测试\n")

    results = []
    # 每个测试用独立端口避免冲突
    tests = [
        (test_full_crash_recovery_flow, 15561),
        (test_dedup_boundary_conditions, 15562),
        (test_zero_miss_during_db_read, 15563),
        (test_normal_start_no_db_read, 15564),
    ]
    for test_fn, port in tests:
        try:
            test_fn(port)
            results.append((test_fn.__name__, True))
        except Exception as e:
            print(f"\n  ❌ 失败: {e}")
            import traceback
            traceback.print_exc()
            results.append((test_fn.__name__, False))

    print("\n" + "=" * 70)
    print("  汇总")
    print("=" * 70)
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n  {passed}/{len(results)} 通过")

    return passed == len(results)


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
