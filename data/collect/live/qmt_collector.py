"""QMT 实时数据采集进程 — 独立进程，TCP 推送至 Watcher，同时写 DB 容灾。

启动后：
1. 取 240 条分钟 K 线回填 index_snapshots
2. 进入主循环：每 60s 取 all_quotes + index quote → push socket → write DB
"""

import json
import select
import socket
import sqlite3
import time
from datetime import datetime
from datetime import time as dt_time

from system.config import settings
from system.qmt.client import QMTClient
from system.utils.logger import get_collect_logger

logger = get_collect_logger("live")

PORT = 15555
# 采集循环不停轮询，select 超时 2s，无 watcher 连接时不停拉取
MORNING_START = dt_time(9, 30)
MORNING_END = dt_time(11, 30)
AFTERNOON_START = dt_time(13, 0)
MARKET_CLOSE = dt_time(15, 0)


class QMTCollector:
    """QMT 数据采集器 — TCP server + 定时 fetch + DB 容灾。"""

    def __init__(self):
        self.db_path = settings.DATABASE_PATH
        self._trade_date = datetime.now().strftime("%Y-%m-%d")
        self._running = True

        # DB migration
        self._migrate_db()

        # QMT client
        self.qmt = QMTClient()

        # TCP server
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", PORT))
        self._server.listen(1)
        self._server.setblocking(False)
        self._watcher_sock: socket.socket | None = None

        logger.info(f"QMT Collector 启动，监听 127.0.0.1:{PORT}")

    # ======================== DB ========================

    def _migrate_db(self):
        """建表 / 加列，幂等。"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS index_snapshots (
                    trade_date TEXT NOT NULL,
                    ts REAL NOT NULL,
                    price REAL NOT NULL DEFAULT 0,
                    high REAL DEFAULT 0,
                    low REAL DEFAULT 0,
                    pre_close REAL DEFAULT 0,
                    change_pct REAL DEFAULT 0,
                    amount REAL DEFAULT 0,
                    PRIMARY KEY (trade_date, ts)
                )"""
            )
            for col, typ in [
                ("price", "REAL DEFAULT 0"),
                ("amount", "REAL DEFAULT 0"),
                ("volume", "REAL DEFAULT 0"),
                ("high", "REAL DEFAULT 0"),
                ("low", "REAL DEFAULT 0"),
                ("open", "REAL DEFAULT 0"),
                ("pre_close", "REAL DEFAULT 0"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE market_snapshots ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            for col, typ in [("index_code", "TEXT DEFAULT '000001.SH'")]:
                try:
                    conn.execute(f"ALTER TABLE index_snapshots ADD COLUMN {col} {typ}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"DB migration 失败: {e}")

    def _write_index_snapshot(
        self,
        ts: float,
        code: str,
        price: float,
        high: float,
        low: float,
        pre_close: float,
        change_pct: float,
        amount: float,
    ):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """INSERT OR REPLACE INTO index_snapshots
                   (trade_date, ts, index_code, price, high, low, pre_close, change_pct, amount)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self._trade_date,
                    ts,
                    code,
                    price,
                    high,
                    low,
                    pre_close,
                    change_pct,
                    amount,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"index_snapshots 写入失败: {e}")

    def _write_market_snapshots(self, ts: float, stocks: dict[str, dict]):
        """批量写 market_snapshots。"""
        rows = []
        for code, item in stocks.items():
            chg = item.get("changePct", 0)
            try:
                chg = float(chg)
            except (ValueError, TypeError):
                chg = 0.0
            price = item.get("price", 0) or 0
            amount = item.get("amount", 0) or 0
            volume = item.get("volume", 0) or 0
            high = item.get("high", price) or price
            low = item.get("low", price) or price
            open_ = item.get("open", price) or price
            pre_close = item.get("preClose", 0) or 0
            rows.append(
                (
                    self._trade_date,
                    ts,
                    code,
                    round(chg, 4),
                    round(float(price), 4),
                    round(float(amount), 2),
                    round(float(volume), 0),
                    round(float(high), 4),
                    round(float(low), 4),
                    round(float(open_), 4),
                    round(float(pre_close), 4),
                )
            )
        if not rows:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            conn.executemany(
                """INSERT OR REPLACE INTO market_snapshots
                   (trade_date, ts, code, change_pct, price, amount,
                    volume, high, low, open, pre_close)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"market_snapshots 写入失败: {e}")

    # ======================== 初始 K 线回填 ========================

    def _init_klines(self):
        """启动时取 240 条分钟 K 线写入 index_snapshots。"""
        try:
            result = self.qmt.history("000001.SH", period="1m", count=240)
            if not result.get("success", True):
                logger.info("分钟K线获取失败，跳过回填")
                return
            data = result.get("data", result)
            if not isinstance(data, list) or len(data) < 5:
                logger.info("分钟K线数据不足，跳过回填")
                return

            bars = []
            today_start = time.mktime(datetime.now().replace(hour=9, minute=0, second=0, microsecond=0).timetuple())
            for bar in data:
                close_val = bar.get("close")
                if close_val is None:
                    continue
                bar_time = bar.get("time") or bar.get("timestamp")
                if bar_time:
                    ts = float(bar_time) if isinstance(bar_time, (int, float)) else time.time()
                else:
                    ts = time.time()
                # 只保留今日 9:00 之后的 K 线
                if ts < today_start:
                    continue

                pre_close = float(bar.get("preClose", 0) or 0)
                close_val = float(bar.get("close", 0))
                change_pct = (close_val - pre_close) / pre_close if pre_close else 0

                bars.append(
                    (
                        self._trade_date,
                        ts,
                        "000001.SH",
                        close_val,
                        float(bar.get("high", 0)),
                        float(bar.get("low", 0)),
                        pre_close,
                        change_pct,
                        float(bar.get("amount", 0) or 0),
                    )
                )

            if bars:
                conn = sqlite3.connect(self.db_path)
                conn.executemany(
                    """INSERT OR REPLACE INTO index_snapshots
                       (trade_date, ts, index_code, price, high, low, pre_close, change_pct, amount)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    bars,
                )
                conn.commit()
                conn.close()
                logger.info(f"初始K线回填: {len(bars)} 条")
        except Exception as e:
            logger.warning(f"初始K线回填失败: {e}")

    # ======================== 网络 ========================

    def _accept_watcher(self):
        try:
            sock, addr = self._server.accept()
            if self._watcher_sock:
                logger.info(f"拒绝新连接 {addr}（已有 Watcher 连接）")
                sock.close()
                return
            self._watcher_sock = sock
            self._watcher_sock.setblocking(True)
            logger.info(f"Watcher 已连接: {addr}")
        except BlockingIOError:
            pass

    def _check_watcher_disconnect(self, fd):
        """检测 Watcher 是否断开。"""
        try:
            data = fd.recv(1024)
            if not data:
                raise ConnectionResetError
        except (ConnectionResetError, BrokenPipeError, OSError):
            logger.info("Watcher 断开连接")
            self._close_watcher()

    def _close_watcher(self):
        if self._watcher_sock:
            try:
                self._watcher_sock.close()
            except OSError:
                pass
            self._watcher_sock = None

    def _send_json(self, msg: dict):
        """推送 JSON 消息给 Watcher。"""
        if not self._watcher_sock:
            return
        try:
            raw = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
            self._watcher_sock.sendall(raw)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning(f"推送失败: {e}")
            self._close_watcher()

    # ======================== 数据获取 ========================

    def _fetch_and_push(self):
        """取 QMT 数据 → push socket → write DB。"""
        t0 = time.time()
        pushed_market = False
        pushed_index = False

        # 1. 全市场快照
        try:
            result = self.qmt.all_quotes()
            if result.get("success", True):
                raw = result.get("data", result)
                if isinstance(raw, dict) and raw:
                    # 归一化：去后缀，标准化字段名
                    stocks = {}
                    for full_code, item in raw.items():
                        short = full_code.split(".")[0] if "." in full_code else full_code
                        price = item.get("lastPrice") or item.get("last_price") or item.get("price")
                        if price is None:
                            continue
                        price_float = float(price)
                        # QMT 返回的 changePct 已为百分比值（-4.5 = -4.5%），直接使用
                        raw_chg = item.get("changePct")
                        if raw_chg is None:
                            raw_chg = item.get("change_pct", 0)
                        amt = item.get("amount")
                        if amt is None:
                            amt = 0
                        # 成交量、最高/最低/开盘价 → 尾盘选股引擎用
                        vol = item.get("volume") or 0
                        high = item.get("high") or price_float
                        low = item.get("low") or price_float
                        open_ = item.get("open") or price_float
                        pre_close = item.get("preClose") or item.get("pre_close") or 0
                        stocks[short] = {
                            "price": price_float,
                            "changePct": float(raw_chg),
                            "amount": float(amt),
                            "volume": float(vol),
                            "high": float(high),
                            "low": float(low),
                            "open": float(open_),
                            "preClose": float(pre_close),
                        }
                    if stocks:
                        ts = time.time()
                        self._send_json({"type": "market", "ts": ts, "stocks": stocks})
                        self._write_market_snapshots(ts, stocks)
                        pushed_market = True
                        logger.debug(f"market push: {len(stocks)} 只")
        except Exception as e:
            logger.warning(f"all_quotes 获取失败: {e}")

        # 2. 多指数采集（批量一次拉取，不再逐个请求）
        INDEX_CODES = {
            "000001.SH": "上证指数",
            "399001.SZ": "深证成指",
            "399006.SZ": "创业板指",
            "399303.SZ": "国证2000",
            "000688.SH": "科创50",
        }
        try:
            result = self.qmt.quotes(list(INDEX_CODES.keys()))
            if result.get("success", True):
                data = result.get("data", result)
                if isinstance(data, dict):
                    for code, name in INDEX_CODES.items():
                        item = data.get(code)
                        if not isinstance(item, dict):
                            continue
                        price = item.get("lastPrice") or item.get("last_price")
                        if not price:
                            continue
                        ts = time.time()
                        pre_close = float(item.get("preClose") or 0)
                        change_pct = (float(price) - pre_close) / pre_close if pre_close else 0
                        amount = float(item.get("amount") or item.get("turnover") or 0)
                        idx_high = float(item.get("high") or price)
                        idx_low = float(item.get("low") or price)
                        msg = {
                            "type": "index",
                            "ts": ts,
                            "code": code,
                            "name": name,
                            "price": float(price),
                            "high": idx_high,
                            "low": idx_low,
                            "pre_close": pre_close,
                            "change_pct": change_pct,
                            "amount": amount,
                        }
                        self._send_json(msg)
                        self._write_index_snapshot(
                            ts,
                            code,
                            float(price),
                            idx_high,
                            idx_low,
                            pre_close,
                            change_pct,
                            amount,
                        )
                        pushed_index = True
                        if code == "000001.SH":
                            logger.debug(f"index push: {name} {float(price):.2f}")
        except Exception as e:
            logger.warning(f"index quote 获取失败: {e}")

        elapsed = time.time() - t0
        if pushed_market or pushed_index:
            logger.info(
                f"fetch+push 完成 ({elapsed:.1f}s)"
                f"{' market' if pushed_market else ''}"
                f"{' index' if pushed_index else ''}"
            )

    # ======================== 生命周期 ========================

    @staticmethod
    def _in_trading_hours() -> bool:
        now = datetime.now().time()
        return MORNING_START <= now < MORNING_END or AFTERNOON_START <= now < MARKET_CLOSE

    @staticmethod
    def _after_market() -> bool:
        return datetime.now().time() >= MARKET_CLOSE

    def run(self):
        """主循环 — 持续采集，拿到数据立刻推送。收盘退出。"""
        while self._running:
            # 午休暂停
            if not self._in_trading_hours():
                if self._after_market():
                    break  # 收盘，退出循环
                time.sleep(5)
                continue

            self._trade_date = datetime.now().strftime("%Y-%m-%d")

            # 拉取全市场行情 + 指数 → 写 DB（Watcher 连着时同步推送）
            self._fetch_and_push()

            # select 处理连接事件，超时设为 2s（快速轮询）
            reads = [self._server]
            if self._watcher_sock:
                reads.append(self._watcher_sock)

            try:
                ready, _, _ = select.select(reads, [], [], 2.0)
            except (ValueError, OSError):
                time.sleep(1)
                continue

            for fd in ready:
                if fd is self._server:
                    self._accept_watcher()
                elif fd is self._watcher_sock:
                    self._check_watcher_disconnect(fd)

        self._close_watcher()
        self._server.close()
        logger.info("QMT Collector 退出")

    def run_forever(self):
        """前台运行，盘后休眠。"""
        logger.info("QMT Collector 启动")
        self._init_klines()
        _was_trading = False

        while True:
            in_trading = self._in_trading_hours()
            if in_trading:
                self._trade_date = datetime.now().strftime("%Y-%m-%d")
                _was_trading = True
                try:
                    self.run()
                except Exception as e:
                    logger.error(f"主循环异常: {e}", exc_info=True)
                    time.sleep(5)
            elif _was_trading:
                # 盘中运行过 → 刚收盘 → 拉最后一次数据落盘，退出
                logger.info("已收盘，拉取最后一次数据...")
                self._trade_date = datetime.now().strftime("%Y-%m-%d")
                try:
                    self._fetch_and_push()
                except Exception as e:
                    logger.error(f"收盘数据拉取失败: {e}")
                logger.info("QMT Collector 退出")
                return
            else:
                # 盘前启动 → 等开盘
                time.sleep(10)
