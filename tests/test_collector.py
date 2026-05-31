# -*- coding: utf-8 -*-
"""采集器单元测试 — DataCollectorClient + QMTCollector"""

import json
import socket
import time
import pytest
from unittest.mock import patch, MagicMock, call


# ====================== DataCollectorClient ======================


class TestClientConnect:
    @pytest.fixture
    def client(self):
        from data.live.collector_client import DataCollectorClient
        return DataCollectorClient()

    def test_initial_state(self, client):
        assert client.connected is False
        assert client.host == "127.0.0.1"
        assert client.port == 15555

    @patch("data.live.collector_client.socket.socket")
    def test_connect_success(self, mock_sock_cls, client):
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock

        ok = client.connect()
        assert ok is True
        assert client.connected is True
        mock_sock.settimeout.assert_called_once_with(5.0)
        mock_sock.setblocking.assert_called_once_with(False)
        mock_sock.connect.assert_called_once_with(("127.0.0.1", 15555))

    @patch("data.live.collector_client.socket.socket")
    def test_connect_refused_sets_retry_throttle(self, mock_sock_cls, client):
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError
        mock_sock_cls.return_value = mock_sock

        ok = client.connect()
        assert ok is False
        assert client.connected is False
        assert client._next_retry > time.time()

    @patch("data.live.collector_client.socket.socket")
    def test_connect_retry_throttled(self, mock_sock_cls, client):
        """重试间隔内直接返回 False，不创建 socket"""
        client._next_retry = time.time() + 30
        ok = client.connect()
        assert ok is False
        mock_sock_cls.assert_not_called()

    @patch("data.live.collector_client.socket.socket")
    def test_connect_disconnects_existing_first(self, mock_sock_cls, client):
        """再次 connect 时先断开旧连接"""
        mock_old = MagicMock()
        client._sock = mock_old
        client.connected = True
        client._buf = b"garbage"

        mock_new = MagicMock()
        mock_sock_cls.return_value = mock_new

        client.connect()
        mock_old.close.assert_called_once()
        assert client._buf == b""


class TestClientDisconnect:
    def test_disconnect_no_socket(self):
        from data.live.collector_client import DataCollectorClient
        c = DataCollectorClient()
        c._sock = None
        c.disconnect()
        assert c.connected is False

    def test_disconnect_closes_and_clears(self):
        from data.live.collector_client import DataCollectorClient
        c = DataCollectorClient()
        mock_sock = MagicMock()
        c._sock = mock_sock
        c.connected = True
        c._buf = b"pending"
        c.disconnect()
        assert c.connected is False
        assert c._buf == b""
        mock_sock.close.assert_called_once()
        assert c._sock is None

    def test_disconnect_close_error_handled(self):
        from data.live.collector_client import DataCollectorClient
        c = DataCollectorClient()
        c._sock = MagicMock()
        c._sock.close.side_effect = OSError
        c.disconnect()
        assert c._sock is None


class TestClientRecvAll:
    @pytest.fixture
    def client(self):
        from data.live.collector_client import DataCollectorClient
        c = DataCollectorClient()
        c._sock = MagicMock()
        c.connected = True
        return c

    def test_no_socket_returns_empty(self):
        from data.live.collector_client import DataCollectorClient
        c = DataCollectorClient()
        c._sock = None
        assert c.recv_all() == []

    def test_not_connected_returns_empty(self):
        from data.live.collector_client import DataCollectorClient
        c = DataCollectorClient()
        c._sock = MagicMock()
        c.connected = False
        assert c.recv_all() == []

    def test_blocking_io_error_returns_empty(self, client):
        client._sock.recv.side_effect = BlockingIOError
        assert client.recv_all() == []

    def test_single_json_message(self, client):
        msg = {"type": "index", "ts": 1234567890.0, "price": 3300.0}
        client._sock.recv.side_effect = [
            (json.dumps(msg) + "\n").encode(),
            BlockingIOError,
        ]
        result = client.recv_all()
        assert result == [msg]

    def test_multiple_messages_one_recv(self, client):
        msgs = [
            {"type": "index", "ts": 1.0, "price": 3300.0},
            {"type": "market", "ts": 1.5, "stocks": {"000001": {"price": 12.5}}},
        ]
        raw = "".join(json.dumps(m) + "\n" for m in msgs).encode()
        client._sock.recv.side_effect = [raw, BlockingIOError]
        result = client.recv_all()
        assert len(result) == 2
        assert result[0]["type"] == "index"
        assert result[1]["type"] == "market"

    def test_partial_line_buffered(self, client):
        """半行数据留在 buffer 中等下次 recv"""
        msg = {"type": "index", "ts": 1.0}
        raw = (json.dumps(msg) + "\n").encode()
        # 第一个 recv 只给半行
        half = len(raw) // 2
        client._sock.recv.side_effect = [raw[:half], raw[half:], BlockingIOError]
        result = client.recv_all()
        assert len(result) == 1
        assert result[0] == msg

    def test_invalid_json_skipped(self, client):
        raw = b"not json\n" + (json.dumps({"ok": True}) + "\n").encode()
        client._sock.recv.side_effect = [raw, BlockingIOError]
        result = client.recv_all()
        assert result == [{"ok": True}]

    def test_disconnect_detected_on_recv_empty(self, client):
        client._sock.recv.return_value = b""
        client._buf = b'{"partial":'
        result = client.recv_all()
        assert client.connected is False
        assert result == []

    def test_connection_reset_disconnects(self, client):
        client._sock.recv.side_effect = ConnectionResetError
        result = client.recv_all()
        assert client.connected is False
        assert result == []

    def test_buffer_accumulation_across_calls(self, client):
        """多次 recv_all 累积 buffer"""
        msg1 = json.dumps({"n": 1}) + "\n"
        msg2 = json.dumps({"n": 2}) + "\n"
        # 第一次只给一行
        client._sock.recv.side_effect = [msg1.encode() + b'{"n":', BlockingIOError]
        r1 = client.recv_all()
        assert len(r1) == 1
        assert r1[0] == {"n": 1}
        assert client._buf == b'{"n":'

        # 第二次补全
        client._sock.recv.side_effect = [b'2}\n' + msg2.encode(), BlockingIOError]
        r2 = client.recv_all()
        assert len(r2) == 2
        assert r2[0] == {"n": 2}
        assert r2[1] == {"n": 2}

    def test_empty_lines_skipped(self, client):
        raw = b'\n{"ok":1}\n\n'
        client._sock.recv.side_effect = [raw, BlockingIOError]
        result = client.recv_all()
        assert result == [{"ok": 1}]


# ====================== QMTCollector 市场数据归一化 ======================


class TestCollectorNormalization:
    """测试 all_quotes 后归一化去后缀、字段别名逻辑。"""

    def _make_collector(self):
        """不启动 server socket 的 minimal collector。"""
        from data.live.qmt_collector import QMTCollector
        with patch.object(QMTCollector, '__init__', lambda self: None):
            c = QMTCollector.__new__(QMTCollector)
            c.qmt = MagicMock()
            c._watcher_sock = None
            c._trade_date = "2026-05-29"
            return c

    def test_suffix_stripped(self):
        """000001.SH → 000001"""
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        raw = {
            "000001.SH": {"lastPrice": 12.5, "changePct": 0.015, "amount": 50000000},
        }
        c.qmt.all_quotes.return_value = {"success": True, "data": raw}
        c.qmt.quote.return_value = {"success": False}

        c._fetch_and_push()
        msg = c._send_json.call_args_list[0][0][0]
        assert "000001" in msg["stocks"]
        assert "000001.SH" not in msg["stocks"]

    def test_field_alias_last_price(self):
        """last_price 作为 price 别名"""
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.return_value = {
            "success": True, "data": {"000001.SH": {"last_price": 12.5, "amount": 0}},
        }
        c.qmt.quote.return_value = {"success": False}

        c._fetch_and_push()
        msg = c._send_json.call_args_list[0][0][0]
        assert msg["stocks"]["000001"]["price"] == 12.5

    def test_skip_stocks_without_price(self):
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.return_value = {
            "success": True, "data": {
                "000001.SH": {"lastPrice": None, "amount": 0},
                "000002.SH": {"lastPrice": 25.0, "amount": 100000},
            },
        }
        c.qmt.quote.return_value = {"success": False}

        c._fetch_and_push()
        msg = c._send_json.call_args_list[0][0][0]
        assert "000001" not in msg["stocks"]
        assert "000002" in msg["stocks"]

    def test_all_quotes_failure_handled(self):
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.side_effect = Exception("QMT crash")
        c.qmt.quote.return_value = {"success": False}
        c._fetch_and_push()
        c._send_json.assert_not_called()

    def test_all_quotes_empty_dict(self):
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.return_value = {"success": True, "data": {}}
        c.qmt.quote.return_value = {"success": False}
        c._fetch_and_push()
        c._send_json.assert_not_called()

    def test_all_quotes_not_dict(self):
        """data 不是 dict 时不推送（可能是 list 或错误信息）"""
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.return_value = {"success": True, "data": []}
        c.qmt.quote.return_value = {"success": False}
        c._fetch_and_push()
        c._send_json.assert_not_called()

    # ---------- index quote ----------

    def test_index_quote_push(self):
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.return_value = {"success": True, "data": {}}
        c.qmt.quote.return_value = {
            "success": True,
            "data": {
                "lastPrice": 3350.5,
                "preClose": 3340.0,
                "changePct": 0.31,
                "amount": 120000000000,
            },
        }

        c._fetch_and_push()
        # _send_json 被调用了两次（market + index 各一次），但 market 没有数据
        index_msg = c._send_json.call_args_list[0][0][0]
        assert index_msg["type"] == "index"
        assert index_msg["price"] == 3350.5
        assert index_msg["pre_close"] == 3340.0
        assert index_msg["amount"] == 120000000000

    def test_index_quote_change_pct_auto_normalize(self):
        """changePct=0.31 (<1) 原样保留，changePct=31 (>1) 除以100"""
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.return_value = {"success": True, "data": {}}
        c.qmt.quote.return_value = {
            "success": True,
            "data": {"lastPrice": 3350.0, "preClose": 3300.0, "changePct": 31.0},
        }
        c._fetch_and_push()
        msg = c._send_json.call_args_list[0][0][0]
        # change_pct 现在自算: (3350-3300)/3300 ≈ 0.01515
        assert abs(msg["change_pct"] - 0.01515) < 0.001

    def test_index_quote_failure_handled(self):
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.return_value = {"success": True, "data": {}}
        c.qmt.quote.side_effect = Exception("timeout")
        c._fetch_and_push()
        c._send_json.assert_not_called()

    def test_index_quote_no_price_skipped(self):
        c = self._make_collector()
        c._write_market_snapshots = MagicMock()
        c._write_index_snapshot = MagicMock()
        c._send_json = MagicMock()

        c.qmt.all_quotes.return_value = {"success": True, "data": {}}
        c.qmt.quote.return_value = {
            "success": True,
            "data": {"lastPrice": None, "last_price": None},
        }
        c._fetch_and_push()
        c._send_json.assert_not_called()


# ====================== QMTCollector DB 写入 ======================


class TestCollectorDBWrite:
    """测试 _write_index_snapshot / _write_market_snapshots DB 写入。"""

    @pytest.fixture
    def db_path(self, tmp_path):
        return str(tmp_path / "test.db")

    @pytest.fixture
    def c(self, db_path):
        import sqlite3
        # market_snapshots 需预先建表（_migrate_db 只 ALTER 不改 CREATE）
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS market_snapshots (
                trade_date TEXT NOT NULL,
                ts TEXT NOT NULL,
                code TEXT NOT NULL,
                change_pct REAL DEFAULT 0,
                price REAL DEFAULT 0,
                amount REAL DEFAULT 0
            )"""
        )
        conn.commit()
        conn.close()

        from data.live.qmt_collector import QMTCollector
        with patch.object(QMTCollector, '__init__', lambda self: None):
            obj = QMTCollector.__new__(QMTCollector)
            obj.db_path = db_path
            obj._trade_date = "2026-05-29"
            obj.qmt = MagicMock()
            obj._watcher_sock = None
            obj._server = MagicMock()
            obj._migrate_db()
            return obj

    def test_write_index_snapshot(self, c, db_path):
        c._write_index_snapshot(1767000000.0, 3350.5, 3360.0, 3340.0, 3340.0, 0.0031, 120000000000)
        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM index_snapshots").fetchone()
        conn.close()
        assert row[0] == "2026-05-29"
        assert row[1] == 1767000000.0
        assert row[2] == 3350.5  # price

    def test_write_market_snapshots(self, c, db_path):
        stocks = {
            "000001": {"price": 12.5, "changePct": 0.015, "amount": 50000000},
            "000002": {"price": 25.0, "changePct": -0.01, "amount": 80000000},
        }
        c._write_market_snapshots("2026-05-29T14:00:00", stocks)

        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM market_snapshots ORDER BY code").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][2] == "000001"
        assert rows[0][3] == pytest.approx(0.015)

    def test_write_market_snapshots_empty(self, c, db_path):
        c._write_market_snapshots("2026-05-29T14:00:00", {})
        import sqlite3
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
        conn.close()
        assert count == 0

    def test_market_snapshots_handles_invalid_change_pct(self, c, db_path):
        stocks = {"000001": {"price": 12.5, "changePct": "N/A", "amount": 0}}
        c._write_market_snapshots("2026-05-29T14:00:00", stocks)
        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT change_pct FROM market_snapshots").fetchone()
        conn.close()
        assert row[0] == 0.0  # 异常时默认 0


# ====================== QMTCollector 连接管理 ======================


class TestCollectorConnection:
    def _make_collector_with_server(self):
        """创建 minimal collector，mock server socket 和配置。"""
        from data.live.qmt_collector import QMTCollector
        with patch.object(QMTCollector, '__init__', lambda self: None):
            c = QMTCollector.__new__(QMTCollector)
            c._server = MagicMock()
            c._watcher_sock = None
            return c

    def test_accept_watcher_first(self):
        c = self._make_collector_with_server()
        mock_sock = MagicMock()
        c._server.accept.return_value = (mock_sock, ("127.0.0.1", 54321))

        c._accept_watcher()
        assert c._watcher_sock is mock_sock
        mock_sock.setblocking.assert_called_once_with(True)

    def test_reject_second_watcher(self):
        c = self._make_collector_with_server()
        c._watcher_sock = MagicMock()
        mock_new = MagicMock()
        c._server.accept.return_value = (mock_new, ("127.0.0.1", 54322))

        c._accept_watcher()
        mock_new.close.assert_called_once()
        assert c._watcher_sock is not mock_new  # 原连接不变

    def test_accept_blocking(self):
        c = self._make_collector_with_server()
        c._server.accept.side_effect = BlockingIOError
        c._accept_watcher()  # 不报错即可

    def test_close_watcher(self):
        c = self._make_collector_with_server()
        mock_sock = MagicMock()
        c._watcher_sock = mock_sock
        c._close_watcher()
        mock_sock.close.assert_called_once()
        assert c._watcher_sock is None

    def test_close_watcher_noop(self):
        c = self._make_collector_with_server()
        c._watcher_sock = None
        c._close_watcher()  # 不报错

    def test_send_json_no_watcher(self):
        c = self._make_collector_with_server()
        c._watcher_sock = None
        c._send_json({"type": "index"})  # 无 watcher，不发送

    def test_send_json_with_watcher(self):
        c = self._make_collector_with_server()
        c._watcher_sock = MagicMock()
        c._send_json({"type": "index", "price": 3300.0})
        c._watcher_sock.sendall.assert_called_once()
        sent = c._watcher_sock.sendall.call_args[0][0]
        assert b"3300.0" in sent
        assert sent.endswith(b"\n")

    def test_send_json_broken_pipe_closes_watcher(self):
        c = self._make_collector_with_server()
        c._watcher_sock = MagicMock()
        c._watcher_sock.sendall.side_effect = BrokenPipeError
        c._send_json({"type": "index"})
        assert c._watcher_sock is None

    def test_check_disconnect_detects_close(self):
        c = self._make_collector_with_server()
        mock_sock = MagicMock()
        mock_sock.recv.return_value = b""
        c._watcher_sock = mock_sock
        c._check_watcher_disconnect(mock_sock)
        assert c._watcher_sock is None

    def test_check_disconnect_reset(self):
        c = self._make_collector_with_server()
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = ConnectionResetError
        c._watcher_sock = mock_sock
        c._check_watcher_disconnect(mock_sock)
        assert c._watcher_sock is None


# ====================== QMTCollector 交易时间 ======================


class TestTradingHours:
    def test_morning_session(self):
        from data.live.qmt_collector import QMTCollector
        with patch("data.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = __import__("datetime").time(10, 0)
            mock_dt.time = __import__("datetime").time
            assert QMTCollector._in_trading_hours() is True

    def test_lunch_break(self):
        from data.live.qmt_collector import QMTCollector
        with patch("data.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = __import__("datetime").time(12, 0)
            mock_dt.time = __import__("datetime").time
            assert QMTCollector._in_trading_hours() is False

    def test_afternoon_session(self):
        from data.live.qmt_collector import QMTCollector
        with patch("data.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = __import__("datetime").time(14, 0)
            mock_dt.time = __import__("datetime").time
            assert QMTCollector._in_trading_hours() is True

    def test_after_market_close(self):
        from data.live.qmt_collector import QMTCollector
        with patch("data.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = __import__("datetime").time(16, 0)
            mock_dt.time = __import__("datetime").time
            assert QMTCollector._in_trading_hours() is False

    def test_before_market_opens(self):
        from data.live.qmt_collector import QMTCollector
        with patch("data.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = __import__("datetime").time(8, 0)
            mock_dt.time = __import__("datetime").time
            assert QMTCollector._in_trading_hours() is False

    def test_boundary_925(self):
        from data.live.qmt_collector import QMTCollector
        with patch("data.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = __import__("datetime").time(9, 25)
            mock_dt.time = __import__("datetime").time
            assert QMTCollector._in_trading_hours() is True

    def test_boundary_1500(self):
        from data.live.qmt_collector import QMTCollector
        with patch("data.live.qmt_collector.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = __import__("datetime").time(15, 0)
            mock_dt.time = __import__("datetime").time
            assert QMTCollector._in_trading_hours() is False  # 15:00 不含


# ====================== QMTCollector _init_klines ======================


class TestInitKlines:
    @pytest.fixture
    def db_path(self, tmp_path):
        return str(tmp_path / "test.db")

    def _make_collector(self, db_path):
        from data.live.qmt_collector import QMTCollector
        with patch.object(QMTCollector, '__init__', lambda self: None):
            c = QMTCollector.__new__(QMTCollector)
            c.db_path = db_path
            c._trade_date = "2026-05-29"
            c.qmt = MagicMock()
            return c

    def test_init_klines_with_data(self, db_path):
        c = self._make_collector(db_path)
        c._migrate_db()
        bars = [
            {"time": 1000 + i * 60, "close": 3300 + i * 2, "high": 3310 + i * 2,
             "low": 3290 + i * 2, "preClose": 3295 + i * 2, "changePct": 15 + i,
             "amount": 1e10 + i * 1e8}
            for i in range(6)
        ]
        c.qmt.history.return_value = {"success": True, "data": bars}

        c._init_klines()

        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM index_snapshots ORDER BY ts").fetchall()
        conn.close()
        assert len(rows) == 6
        assert rows[0][2] == 3300.0  # first price
        # change_pct 现在自算: (close-preClose)/preClose = (3300-3295)/3295 ≈ 0.001517
        assert abs(rows[0][6] - 0.001517) < 0.0001

    def test_init_klines_empty_data(self, db_path):
        c = self._make_collector(db_path)
        c._migrate_db()
        c.qmt.history.return_value = {"success": True, "data": []}
        c._init_klines()

        import sqlite3
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM index_snapshots").fetchone()[0]
        conn.close()
        assert count == 0

    def test_init_klines_failure_flag(self, db_path):
        c = self._make_collector(db_path)
        c._migrate_db()
        c.qmt.history.return_value = {"success": False, "data": []}
        c._init_klines()

        import sqlite3
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM index_snapshots").fetchone()[0]
        conn.close()
        assert count == 0

    def test_init_klines_exception_handled(self, db_path):
        c = self._make_collector(db_path)
        c._migrate_db()
        c.qmt.history.side_effect = Exception("QMT down")
        c._init_klines()  # 不抛异常

    def test_init_klines_skips_null_close(self, db_path):
        c = self._make_collector(db_path)
        c._migrate_db()
        bars = [
            {"time": 1000, "close": None, "amount": 0},
        ] + [
            {"time": 1060 + i * 60, "close": 3310 + i, "high": 3320 + i,
             "low": 3300 + i, "preClose": 0, "changePct": 0, "amount": 0}
            for i in range(5)
        ]
        c.qmt.history.return_value = {"success": True, "data": bars}
        c._init_klines()

        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT * FROM index_snapshots").fetchall()
        conn.close()
        assert len(rows) == 5  # null close 被跳过

    def test_init_klines_not_list(self, db_path):
        c = self._make_collector(db_path)
        c._migrate_db()
        c.qmt.history.return_value = {"success": True, "data": {"not": "list"}}
        c._init_klines()
        # 不报错


# ====================== DB Migration ======================


class TestDBMigration:
    def test_migrate_creates_index_snapshots(self, tmp_path):
        from data.live.qmt_collector import QMTCollector
        db_path = str(tmp_path / "test.db")
        with patch.object(QMTCollector, '__init__', lambda self: None):
            c = QMTCollector.__new__(QMTCollector)
            c.db_path = db_path
            c._migrate_db()

        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO index_snapshots VALUES ('2026-05-29', 1.0, 3300, 0, 0, 0, 0, 0)")
        conn.commit()
        conn.close()

    def test_migrate_adds_columns_idempotent(self, tmp_path):
        from data.live.qmt_collector import QMTCollector
        db_path = str(tmp_path / "test.db")
        with patch.object(QMTCollector, '__init__', lambda self: None):
            c = QMTCollector.__new__(QMTCollector)
            c.db_path = db_path
            c._migrate_db()
            c._migrate_db()  # 二次调用不报错

    def test_migrate_db_not_created_yet(self):
        """db_path 不存在时 migration 不报错（内存 DB invalid path 场景）"""
        from data.live.qmt_collector import QMTCollector
        with patch.object(QMTCollector, '__init__', lambda self: None):
            c = QMTCollector.__new__(QMTCollector)
            c.db_path = "/nonexistent/dir/db.sqlite"
            c._migrate_db()  # 不抛异常，因为 _migrate_db 内部 try/except


# ====================== 消息协议 ======================


class TestMessageProtocol:
    """验证 JSON lines 协议兼容性：客户端能正确解析服务端发的消息。"""

    def test_roundtrip_index_message(self):
        """Colletor → socket → Client 完整路径。"""
        from data.live.collector_client import DataCollectorClient
        msg = {"type": "index", "ts": 1767000000.123, "price": 3350.5,
               "pre_close": 3340.0, "change_pct": 0.0031, "amount": 120000000000}

        # 模拟客户端收到 raw bytes
        client = DataCollectorClient()
        client._sock = MagicMock()
        client.connected = True
        raw = (json.dumps(msg) + "\n").encode()
        client._sock.recv.side_effect = [raw, BlockingIOError]

        result = client.recv_all()
        assert result == [msg]

    def test_roundtrip_market_message(self):
        from data.live.collector_client import DataCollectorClient
        stocks = {
            "000001": {"price": 12.5, "changePct": 0.015, "amount": 50000000},
            "000002": {"price": 25.0, "changePct": -0.01, "amount": 80000000},
        }
        msg = {"type": "market", "ts": 1767000000.456, "stocks": stocks}

        client = DataCollectorClient()
        client._sock = MagicMock()
        client.connected = True
        raw = (json.dumps(msg) + "\n").encode()
        client._sock.recv.side_effect = [raw, BlockingIOError]

        result = client.recv_all()
        assert result == [msg]
        assert result[0]["stocks"]["000001"]["price"] == 12.5

    def test_ts_is_float(self):
        """ts 必须是 float 保证 O(1) 比较去重。"""
        from data.live.collector_client import DataCollectorClient
        client = DataCollectorClient()
        client._sock = MagicMock()
        client.connected = True
        raw = b'{"type":"index","ts":1767000000.123,"price":3350.0}\n'
        client._sock.recv.side_effect = [raw, BlockingIOError]
        result = client.recv_all()
        assert isinstance(result[0]["ts"], float)
