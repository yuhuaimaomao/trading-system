# -*- coding: utf-8 -*-
"""非阻塞 TCP 客户端，连接 QMT Collector 接收实时市场数据。"""

import json
import logging
import socket
import time

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 15555
RECONNECT_INTERVAL = 30  # 秒


class DataCollectorClient:
    """非阻塞 TCP 客户端，JSON lines 协议。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._buf = b""
        self.connected = False
        self._next_retry: float = 0

    def connect(self) -> bool:
        """连接 collector。重复调用会先断开再重连。"""
        now = time.time()
        if now < self._next_retry:
            return False

        self.disconnect()
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(5.0)
            self._sock.connect((self.host, self.port))
            self._sock.setblocking(False)
            self.connected = True
            self._buf = b""
            self._next_retry = 0
            logger.info(f"已连接 QMT Collector {self.host}:{self.port}")
            return True
        except (ConnectionRefusedError, OSError) as e:
            self._sock = None
            self.connected = False
            self._next_retry = now + RECONNECT_INTERVAL
            logger.debug(f"QMT Collector 连接失败 ({e})，{RECONNECT_INTERVAL}s 后重试")
            return False

    def disconnect(self):
        self.connected = False
        self._buf = b""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def recv_all(self) -> list[dict]:
        """非阻塞读取所有可用的完整 JSON 消息。"""
        if not self._sock or not self.connected:
            return []

        messages = []
        try:
            while True:
                try:
                    data = self._sock.recv(65536)
                except BlockingIOError:
                    break
                if not data:
                    logger.warning("QMT Collector 断开连接")
                    self.disconnect()
                    break
                self._buf += data
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.warning(f"QMT Collector 连接异常: {e}")
            self.disconnect()
            return messages

        # 解析完整行
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        return messages
