# -*- coding: utf-8 -*-
"""Collector ↔ Watcher IPC 端到端测试 — 使用真实 TCP socket。

用假 TCP server 模拟 QMT Collector，测试 DataCollectorClient 接收
和 Watcher 数据处理全链路（不含 QMT）。

用法: python3 tests/test_collector_ipc.py
"""

import json
import logging
import socket
import threading
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)  # 静默日志

TEST_PORT = 15556  # 避免和真实 collector 15555 冲突


# ═══════════════════════════════════════════════
# 假 Collector TCP Server
# ═══════════════════════════════════════════════

class MockCollector:
    """假 QMT Collector — 在独立线程里监听、接受连接、发送 JSON lines。"""

    def __init__(self, port=TEST_PORT):
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
                break  # 接受一个连接后退出 accept 循环
            except socket.timeout:
                continue
            except OSError:
                return

    def send(self, msg: dict):
        """发送一条 JSON 消息。"""
        if not self._client:
            return False
        try:
            raw = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            self._client.sendall(raw)
            return True
        except OSError:
            return False


# ═══════════════════════════════════════════════
# 迷你 Watcher 状态接收器
# ═══════════════════════════════════════════════

class MiniWatcherState:
    """精简版 Watcher 状态 — 只测试 collector 数据处理链路。"""

    def __init__(self):
        from data.live.collector_client import DataCollectorClient
        self._collector_client = DataCollectorClient(port=TEST_PORT)
        self._last_index_quote: dict | None = None
        self._market_snapshot: dict[str, dict] = {}
        self._index_prices: list[float] = []
        self._index_high: float = 0
        self._index_low: float = 0
        self._market_turnovers: list[float] = []
        self._last_db_ts: float = 0
        self._sector_calls = 0  # 记录 _update_sector_trends 被调用的次数

    def connect(self):
        return self._collector_client.connect()

    @property
    def connected(self):
        return self._collector_client.connected

    def recv_and_process(self):
        """模拟 _recv_collector_data() → _handle_collector_index/market。"""
        if not self._collector_client.connected:
            self.connect()
            return 0

        try:
            messages = self._collector_client.recv_all()
        except Exception:
            self._collector_client.disconnect()
            return 0

        count = 0
        for msg in messages:
            msg_ts = msg.get("ts", 0)
            if self._last_db_ts > 0 and msg_ts <= self._last_db_ts:
                continue

            msg_type = msg.get("type")
            if msg_type == "index":
                self._handle_index(msg)
                count += 1
            elif msg_type == "market":
                self._handle_market(msg)
                count += 1

        return count

    def _handle_index(self, msg: dict):
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

    def _handle_market(self, msg: dict):
        self._market_snapshot = msg.get("stocks", {})
        self._last_db_ts = max(self._last_db_ts, msg.get("ts", 0))


# ═══════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════

def test_full_ipc_flow():
    """完整 IPC 流程：连接 → 多轮推送 → 接收 → 状态验证。"""
    print("\n" + "=" * 70)
    print("  测试 1: Collector ↔ Watcher 完整 IPC 流程")
    print("=" * 70)

    mock = MockCollector()
    mock.start()
    time.sleep(0.3)  # 等 server 就绪

    watcher = MiniWatcherState()

    # Step 1: 连接
    ok = watcher.connect()
    print(f"\n  [1] 连接 Collector: {'✓' if ok else '✗'}")
    assert ok, "连接失败"
    assert watcher.connected

    # 等待 mock collector 接受连接
    time.sleep(0.3)

    # Step 2: 推送 3 轮数据（模拟 3 次 60s fetch）
    print(f"\n  [2] 推送 3 轮数据...")

    # 轮次 1
    mock.send({
        "type": "index", "ts": time.time(),
        "price": 3350.0, "pre_close": 3340.0,
        "change_pct": 0.003, "amount": 80_000_000_000,
    })
    mock.send({
        "type": "market", "ts": time.time(),
        "stocks": {
            "000001": {"price": 12.50, "changePct": 0.015, "amount": 500_000_000},
            "000002": {"price": 25.00, "changePct": -0.010, "amount": 300_000_000},
            "000003": {"price": 8.00, "changePct": 0.020, "amount": 150_000_000},
        },
    })

    time.sleep(0.2)

    # 轮次 2
    mock.send({
        "type": "index", "ts": time.time(),
        "price": 3355.0, "pre_close": 3340.0,
        "change_pct": 0.0045, "amount": 90_000_000_000,
    })

    time.sleep(0.2)

    # 轮次 3
    mock.send({
        "type": "index", "ts": time.time(),
        "price": 3348.0, "pre_close": 3340.0,
        "change_pct": 0.0024, "amount": 75_000_000_000,
    })
    mock.send({
        "type": "market", "ts": time.time(),
        "stocks": {
            "000001": {"price": 12.70, "changePct": 0.031, "amount": 800_000_000},
            "000002": {"price": 24.50, "changePct": -0.030, "amount": 500_000_000},
        },
    })

    time.sleep(0.2)

    # Step 3: Watcher 接收
    count = watcher.recv_and_process()
    print(f"  [3] 接收消息: {count} 条 (期望 5 条)")

    # Step 4: 验证状态
    print(f"\n  [4] 验证状态:")
    q = watcher._last_index_quote
    print(f"      指数行情: price={q['price']:.1f} pre_close={q['pre_close']:.1f} "
          f"change_pct={q['change_pct']:.4f}")

    assert q["price"] == 3348.0, f"price: {q['price']}"
    assert q["pre_close"] == 3340.0
    assert len(watcher._index_prices) == 3, f"index_prices: {len(watcher._index_prices)}"
    assert watcher._index_high == 3355.0, f"high: {watcher._index_high}"
    assert watcher._index_low == 3348.0, f"low: {watcher._index_low}"
    assert len(watcher._market_turnovers) == 3
    assert len(watcher._market_snapshot) == 2, f"market_snapshot: {len(watcher._market_snapshot)}"

    # 最新 market 数据是最新一条推送的
    assert watcher._market_snapshot["000001"]["price"] == 12.70
    assert watcher._market_snapshot["000002"]["changePct"] == -0.030

    print(f"      index_prices:  {[f'{p:.1f}' for p in watcher._index_prices]}")
    print(f"      high: {watcher._index_high:.1f}  low: {watcher._index_low:.1f}")
    print(f"      turnovers:     {len(watcher._market_turnovers)} 条")
    print(f"      market snaps:  {len(watcher._market_snapshot)} 只")
    print(f"\n  ✅ 全部通过")

    mock.stop()
    return True


def test_crash_recovery_dedup():
    """盘中重启去重：DB 已有 ts=1000 的数据，socket 消息 ts<=1000 应跳过。"""
    print("\n" + "=" * 70)
    print("  测试 2: 盘中重启去重 (ts 过滤)")
    print("=" * 70)

    mock = MockCollector()
    mock.start()
    time.sleep(0.3)

    watcher = MiniWatcherState()
    watcher.connect()
    time.sleep(0.3)

    # 模拟 DB 已恢复到 ts=1000.0
    watcher._last_db_ts = 1000.0

    # 推送 5 条：3 条旧数据 (ts <= 1000)，2 条新数据 (ts > 1000)
    old_ts = 1000.0
    new_ts = time.time()  # 远大于 1000

    mock.send({"type": "index", "ts": 999.0, "price": 3300.0,
               "pre_close": 3290.0, "change_pct": 0.003, "amount": 50_000_000_000})
    mock.send({"type": "index", "ts": 1000.0, "price": 3310.0,
               "pre_close": 3290.0, "change_pct": 0.006, "amount": 55_000_000_000})
    mock.send({"type": "market", "ts": 998.0, "stocks": {"000001": {"price": 10.0, "changePct": 0.01, "amount": 100_000}}})
    mock.send({"type": "index", "ts": new_ts, "price": 3320.0,
               "pre_close": 3290.0, "change_pct": 0.009, "amount": 60_000_000_000})
    mock.send({"type": "market", "ts": new_ts, "stocks": {"000002": {"price": 20.0, "changePct": 0.02, "amount": 200_000}}})

    time.sleep(0.3)

    count = watcher.recv_and_process()
    print(f"\n  接收消息: {count} 条 (期望 2 条新数据)")
    assert count == 2, f"去重失败，应接收 2 条新数据，实际 {count} 条"

    # 确认收到的是新数据
    assert watcher._last_index_quote["price"] == 3320.0
    assert len(watcher._index_prices) == 1  # 只有新数据进入
    assert watcher._market_snapshot["000002"]["price"] == 20.0

    # 确认旧数据没有进入
    assert "000001" not in watcher._market_snapshot

    print(f"      index_prices:  {[f'{p:.1f}' for p in watcher._index_prices]}")
    print(f"      market: {watcher._market_snapshot}")
    print(f"\n  ✅ 去重正确")

    mock.stop()
    return True


def test_disconnect_reconnect():
    """Watcher 在 Collector 断开后应检测到并自动重连。"""
    print("\n" + "=" * 70)
    print("  测试 3: 断线检测 + 自动重连")
    print("=" * 70)

    # 先启动一个 collector，连接，再关闭
    mock = MockCollector()
    mock.start()
    time.sleep(0.3)

    watcher = MiniWatcherState()
    ok = watcher.connect()
    print(f"\n  [1] 首次连接: {'✓' if ok else '✗'}")
    assert ok

    time.sleep(0.3)

    # 推送一条数据证明连接正常
    mock.send({"type": "index", "ts": time.time(), "price": 3300.0,
               "pre_close": 3290.0, "change_pct": 0.003, "amount": 50_000_000_000})
    time.sleep(0.2)
    count = watcher.recv_and_process()
    assert count == 1
    print(f"  [2] 接收正常: {count} 条")

    # 关闭 collector（模拟崩溃）
    mock.stop()
    time.sleep(0.2)

    # Watcher 尝试 recv，应检测到断开
    count = watcher.recv_and_process()
    assert not watcher.connected
    print(f"  [3] Collector 断开后: connected={watcher.connected}, recv={count}")

    # 立即重连——因为有时间节流，应该失败
    ok = watcher.connect()
    assert not ok  # 重试节流未到
    print(f"  [4] 立即重连（应被节流拒绝）: {'✗' if not ok else '⚠️'}")

    # 启动新的 collector
    mock2 = MockCollector()
    mock2.start()
    time.sleep(0.3)

    # 清除重试节流后重连
    watcher._collector_client._next_retry = 0
    ok = watcher.connect()
    print(f"  [5] 节流清除后重连: {'✓' if ok else '✗'}")
    assert ok
    assert watcher.connected

    time.sleep(0.3)

    # 新 collector 发数据，应正常接收
    mock2.send({"type": "index", "ts": time.time(), "price": 3350.0,
                "pre_close": 3340.0, "change_pct": 0.003, "amount": 60_000_000_000})
    time.sleep(0.2)
    count = watcher.recv_and_process()
    assert count == 1
    print(f"  [6] 重连后接收: {count} 条 ✓")

    mock2.stop()
    print(f"\n  ✅ 断开重连流程正确")


def test_multiple_rounds():
    """多轮推送压力测试：50 轮，验证无丢包无乱序。"""
    print("\n" + "=" * 70)
    print("  测试 4: 多轮推送 (50 轮)")
    print("=" * 70)

    mock = MockCollector()
    mock.start()
    time.sleep(0.3)

    watcher = MiniWatcherState()
    watcher.connect()
    time.sleep(0.3)

    sent_index = 0
    sent_market = 0
    base_price = 3300.0

    for i in range(50):
        base_price += 0.5
        mock.send({
            "type": "index", "ts": time.time() + i * 0.001,
            "price": base_price, "pre_close": 3300.0,
            "change_pct": (base_price - 3300) / 3300, "amount": 80_000_000_000 + i * 1_000_000,
        })
        sent_index += 1

        if i % 3 == 0:
            stocks = {}
            for j in range(100):
                code = f"{j:06d}"
                stocks[code] = {
                    "price": 10.0 + j * 0.1 + i * 0.05,
                    "changePct": (i - 25) * 0.02,
                    "amount": 1_000_000 * (j + 1),
                }
            mock.send({"type": "market", "ts": time.time() + i * 0.001, "stocks": stocks})
            sent_market += 1

    time.sleep(0.5)

    count = watcher.recv_and_process()
    expected = sent_index + sent_market
    print(f"\n  发送: index×{sent_index} + market×{sent_market} = {expected}")
    print(f"  接收: {count} 条")
    assert count == expected, f"丢包: 期望 {expected} 实际 {count}"

    assert len(watcher._index_prices) == sent_index
    assert watcher._index_prices[-1] == base_price
    assert watcher._index_high == base_price
    assert watcher._index_low == 3300.5

    latest_snapshot = watcher._market_snapshot
    assert len(latest_snapshot) == 100  # 最新一轮 100 只
    # 验证最后一轮数据正确
    assert latest_snapshot["000000"]["price"] == 10.0 + 48 * 0.05  # i=48 (最后一次 i%3==0)
    assert abs(latest_snapshot["000099"]["price"] - (10.0 + 99 * 0.1 + 48 * 0.05)) < 0.01

    print(f"      index_prices: {len(watcher._index_prices)} 条")
    print(f"      最后指数: {watcher._index_prices[-1]:.1f}")
    print(f"      最后 snapshot: {len(latest_snapshot)} 只")
    print(f"\n  ✅ 无丢包")

    mock.stop()


def test_empty_messages():
    """空消息和格式错误应被正确忽略。"""
    print("\n" + "=" * 70)
    print("  测试 5: 异常数据容错")
    print("=" * 70)

    mock = MockCollector()
    mock.start()
    time.sleep(0.3)

    watcher = MiniWatcherState()
    watcher.connect()
    time.sleep(0.3)

    # 推送正常数据
    mock.send({"type": "index", "ts": time.time(), "price": 3300.0,
               "pre_close": 3290.0, "change_pct": 0.003, "amount": 50_000_000_000})

    # 通过 raw socket 发送非法 JSON
    time.sleep(0.2)
    if mock._client:
        mock._client.sendall(b"not json\n")
        mock._client.sendall(b"\n")  # 空行
        mock._client.sendall(b'{"type":"index","ts":123,"price":3350}\n')  # 正常

    time.sleep(0.3)

    count = watcher.recv_and_process()
    print(f"\n  接收: {count} 条 (期望 2 条正常)")
    assert count == 2, f"容错失败: {count}"
    assert watcher._index_prices[-1] == 3350.0
    print(f"  ✅ 非法 JSON 和空行被忽略")


# ═══════════════════════════════════════════════

def main():
    print("Collector ↔ Watcher IPC 端到端测试")
    print("(使用真实 TCP socket，无需 QMT)")

    results = []
    for test in [test_full_ipc_flow, test_crash_recovery_dedup,
                 test_disconnect_reconnect, test_multiple_rounds,
                 test_empty_messages]:
        try:
            test()
            results.append((test.__name__, True))
        except Exception as e:
            print(f"\n  ❌ 失败: {e}")
            import traceback
            traceback.print_exc()
            results.append((test.__name__, False))

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
