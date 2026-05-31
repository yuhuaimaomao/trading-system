# -*- coding: utf-8 -*-
"""模拟 QMT 行情源 — 按预设价格序列返回行情数据。

支持多种价格轨迹，所有数据可预先计算和验证。
"""

import math
from dataclasses import dataclass, field


# ── 轨迹生成器 ──

def _price(base: float, scan: int, scan_min: int, scan_max: int,
           start_val: float, end_val: float) -> float:
    """在 [scan_min, scan_max] 区间内线性插值。"""
    if scan <= scan_min:
        return base * (1 + start_val)
    if scan >= scan_max:
        return base * (1 + end_val)
    frac = (scan - scan_min) / (scan_max - scan_min)
    val = start_val + (end_val - start_val) * frac
    return round(base * (1 + val), 2)


def _sin(base: float, scan: int, scan_min: int, scan_max: int,
         amplitude: float, cycles: float = 2) -> float:
    """正弦波震荡。"""
    if scan < scan_min:
        return base
    frac = (scan - scan_min) / max(scan_max - scan_min, 1)
    return round(base * (1 + amplitude * math.sin(frac * cycles * 2 * math.pi)), 2)


# ── 轨迹定义 ──

@dataclass
class StockTrajectory:
    """单只股票的价格轨迹。"""
    code: str
    base_price: float          # 昨收价
    sector: str = ""
    concept: str = ""
    limit_pct: float = 0.10    # 涨跌停幅度

    # 价格序列: [price_at_scan_0, price_at_scan_1, ...]
    # 在 sim_qmt.define_stock 时生成
    prices: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    opens: list[float] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)

    # 五档盘口
    bid_vols: list[int] = field(default_factory=lambda: [10000, 8000, 6000, 4000, 2000])
    ask_vols: list[int] = field(default_factory=lambda: [10000, 8000, 6000, 4000, 2000])

    def price_at(self, scan: int) -> float:
        if not self.prices:
            return self.base_price
        idx = min(scan, len(self.prices) - 1)
        return self.prices[idx]

    def high_at(self, scan: int) -> float:
        if not self.highs:
            return self.price_at(scan) * 1.01
        return self.highs[min(scan, len(self.highs) - 1)]

    def low_at(self, scan: int) -> float:
        if not self.lows:
            return self.price_at(scan) * 0.99
        return self.lows[min(scan, len(self.lows) - 1)]

    def open_at(self, scan: int) -> float:
        if not self.opens:
            return self.price_at(scan)
        return self.opens[min(scan, len(self.opens) - 1)]

    def generate_flat(self, num_scans: int, noise: float = 0.002):
        """横盘轨迹。"""
        import random
        rng = random.Random(42 + int(self.code))
        for _ in range(num_scans):
            p = self.base_price * (1 + rng.uniform(-noise, noise))
            self.prices.append(round(p, 2))
            self.highs.append(round(p * 1.005, 2))
            self.lows.append(round(p * 0.995, 2))
            self.opens.append(round(p, 2))
            self.amounts.append(rng.uniform(5e7, 2e8))

    def generate_linear(self, num_scans: int, segments: list[dict]):
        """分段线性轨迹。

        segments: [{"start_scan": 0, "end_scan": 50, "from_pct": 0.0, "to_pct": 0.02}, ...]
        """
        for scan in range(num_scans):
            pct = 0.0
            for seg in segments:
                if seg["start_scan"] <= scan <= seg["end_scan"]:
                    pct = _price(1.0, scan, seg["start_scan"], seg["end_scan"],
                                 seg["from_pct"], seg["to_pct"]) - 1.0
                    break
                elif scan > segments[-1]["end_scan"]:
                    pct = segments[-1]["to_pct"]
                    break
            p = self.base_price * (1 + pct)
            self.prices.append(round(p, 2))
            self.highs.append(round(p * 1.01, 2))
            self.lows.append(round(p * 0.99, 2))
            self.opens.append(round(p, 2))
            self.amounts.append(1e8)

    def generate_flat_before_fall(self, num_scans: int, fall_start: int,
                                   fall_pct: float = -0.05):
        """前段横盘，fall_start 起线性下跌。"""
        for scan in range(num_scans):
            if scan < fall_start:
                pct = 0.0
            else:
                frac = (scan - fall_start) / max(num_scans - fall_start, 1)
                pct = fall_pct * frac
            p = self.base_price * (1 + pct)
            self.prices.append(round(p, 2))
            self.highs.append(round(p * 1.01, 2))
            self.lows.append(round(p * 0.99, 2))
            self.opens.append(round(p, 2))
            self.amounts.append(1e8)

    def generate_v_shape(self, num_scans: int, bottom_scan: int,
                          fall_pct: float = -0.03, rise_pct: float = 0.02):
        """V 型轨迹：跌到底部，然后回升。"""
        for scan in range(num_scans):
            if scan <= bottom_scan:
                frac = scan / max(bottom_scan, 1)
                pct = fall_pct * frac
            else:
                frac = (scan - bottom_scan) / max(num_scans - bottom_scan, 1)
                pct = fall_pct + (rise_pct - fall_pct) * frac
            p = self.base_price * (1 + pct)
            self.prices.append(round(p, 2))
            self.highs.append(round(p * 1.01, 2))
            self.lows.append(round(p * 0.99, 2))
            self.opens.append(round(p, 2))
            self.amounts.append(1e8)


# ── 指数轨迹 ──

@dataclass
class IndexTrajectory:
    """上证指数价格轨迹 + 市场宽度。"""
    prices: list[float] = field(default_factory=list)
    change_pcts: list[float] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)
    up_counts: list[int] = field(default_factory=list)
    down_counts: list[int] = field(default_factory=list)

    def generate_from_prices(self, prices: list[float], base: float = 3300):
        self.prices = prices
        self.change_pcts = [(p - base) / base for p in prices]
        default_amount = 1e11
        for i in range(len(prices)):
            self.amounts.append(default_amount + i * 1e9)
            # 根据涨跌幅调整涨跌比
            chg = self.change_pcts[i]
            if chg > 0.005:
                up, down = 3000, 1500
            elif chg < -0.01:
                up, down = 500, 4000
            elif chg < -0.005:
                up, down = 800, 3500
            else:
                up, down = 2000, 2500
            self.up_counts.append(up)
            self.down_counts.append(down)

    def price_at(self, scan: int) -> float:
        if not self.prices:
            return 3300
        return self.prices[min(scan, len(self.prices) - 1)]

    def change_at(self, scan: int) -> float:
        if not self.change_pcts:
            return 0
        return self.change_pcts[min(scan, len(self.change_pcts) - 1)]

    def amount_at(self, scan: int) -> float:
        if not self.amounts:
            return 1e11
        return self.amounts[min(scan, len(self.amounts) - 1)]


# ── QMT 模拟器 ──

class SimQMT:
    """模拟 QMT 行情客户端，按 scan 编号返回预设数据。"""

    def __init__(self):
        self._stocks: dict[str, StockTrajectory] = {}
        self._index: IndexTrajectory = IndexTrajectory()
        self._scan: int = 0
        self._minute_klines: dict[str, list[dict]] = {}
        self._ticks: dict[str, list[dict]] = {}

    @property
    def scan(self) -> int:
        return self._scan

    @scan.setter
    def scan(self, value: int):
        self._scan = value

    # ── 定义数据 ──

    def add_stock(self, traj: StockTrajectory):
        self._stocks[traj.code] = traj

    def set_index(self, traj: IndexTrajectory):
        self._index = traj

    def set_minute_kline(self, code: str, prices: list[float]):
        """根据价格序列生成分钟 K 线。"""
        klines = []
        for i, p in enumerate(prices):
            klines.append({
                "close": p,
                "high": round(p * 1.005, 2),
                "low": round(p * 0.995, 2),
                "open": prices[i - 1] if i > 0 else p,
                "volume": 100000,
            })
        self._minute_klines[code] = klines

    def set_ticks(self, code: str, buy_ratio: float = 0.5):
        """生成 tick 数据。buy_ratio > 0.5 表示买盘偏强。"""
        ticks = []
        amt = 0
        for i in range(100):
            amt += 50000 + (10000 if i % 3 == 0 else 0)
            direction = "buy" if (i % 100) / 100 < buy_ratio else "sell"
            ticks.append({"amount": amt, "direction": direction})
        self._ticks[code] = ticks

    # ── QMT 接口 ──

    def get_realtime(self, codes: list[str]) -> dict:
        result = {}
        s = self._scan
        for c in codes:
            if c == "000001" and c in self._stocks:
                # 上证指数特殊处理
                t = self._stocks[c]
                result[c] = {
                    "lastPrice": t.price_at(s),
                    "price": t.price_at(s),
                    "preClose": t.base_price,
                    "high": t.high_at(s),
                    "low": t.low_at(s),
                    "open": t.open_at(s),
                    "askVol": t.ask_vols,
                    "bidVol": t.bid_vols,
                }
            elif c in self._stocks:
                t = self._stocks[c]
                result[c] = {
                    "lastPrice": t.price_at(s),
                    "price": t.price_at(s),
                    "preClose": t.base_price,
                    "high": t.high_at(s),
                    "low": t.low_at(s),
                    "open": t.open_at(s),
                    "askVol": t.ask_vols,
                    "bidVol": t.bid_vols,
                }
        return result

    def get_quote_detail(self, code: str) -> dict | None:
        s = self._scan
        if code in self._stocks:
            t = self._stocks[code]
            return {
                "lastPrice": t.price_at(s),
                "price": t.price_at(s),
                "preClose": t.base_price,
                "high": t.high_at(s),
                "low": t.low_at(s),
                "open": t.open_at(s),
                "askVol": t.ask_vols,
                "bidVol": t.bid_vols,
            }
        return None

    def get_minute_kline(self, code: str, count: int = 120) -> list[dict] | None:
        klines = self._minute_klines.get(code)
        if klines:
            return klines[-count:]
        # fallback: 从价格生成简单 K 线
        if code in self._stocks:
            t = self._stocks[code]
            s = self._scan
            result = []
            start = max(0, s - count)
            for i in range(start, s + 1):
                p = t.price_at(i)
                result.append({
                    "close": p,
                    "high": round(p * 1.005, 2),
                    "low": round(p * 0.995, 2),
                    "open": t.price_at(max(0, i - 1)),
                })
            return result
        return None

    def get_kline(self, code: str, period: str = "1m", count: int = 120) -> list[dict] | None:
        return self.get_minute_kline(code, count)

    def get_ticks(self, code: str) -> list[dict] | None:
        return self._ticks.get(code)

    def get_instrument(self, code: str) -> dict | None:
        if code in self._stocks:
            t = self._stocks[code]
            limit_pct = t.limit_pct
            return {
                "upStopPrice": round(t.base_price * (1 + limit_pct), 2),
                "downStopPrice": round(t.base_price * (1 - limit_pct), 2),
                "preClose": t.base_price,
                "floatShare": 1e9,
                "totalShare": 2e9,
            }
        return None

    # ── Collector 模拟 ──

    def get_index_quote(self, scan: int = None) -> dict:
        s = scan if scan is not None else self._scan
        return {
            "price": self._index.price_at(s),
            "pre_close": 3300.0,
            "change_pct": self._index.change_at(s),
            "amount": self._index.amount_at(s),
        }

    def get_all_quotes_snapshot(self, scan: int = None) -> dict[str, dict]:
        """模拟 /all_quotes 全市场快照，涨跌比随指数变化。"""
        s = scan if scan is not None else self._scan
        result = {}
        for code, t in self._stocks.items():
            result[code] = {
                "price": t.price_at(s),
                "changePct": (t.price_at(s) - t.base_price) / t.base_price * 100,
                "amount": t.amounts[min(s, len(t.amounts) - 1)] if t.amounts else 1e8,
            }
        # 涨跌家数：根据当前指数涨跌调整
        idx_chg = self._index.change_at(s)
        if idx_chg > 0.005:      up_n, dn_n = 3500, 1500
        elif idx_chg > 0.002:    up_n, dn_n = 2800, 2000
        elif idx_chg < -0.01:    up_n, dn_n = 500, 4000
        elif idx_chg < -0.005:   up_n, dn_n = 800, 3500
        elif idx_chg < -0.002:   up_n, dn_n = 1500, 3000
        else:                     up_n, dn_n = 2200, 2400

        fake_total = 100
        up_ratio = up_n / (up_n + dn_n) if (up_n + dn_n) > 0 else 0.5
        for i in range(fake_total):
            is_up = i < int(fake_total * up_ratio)
            chg = (i % 10 + 1) * 0.1 * (1 if is_up else -1)
            result[f"600{i:03d}"] = {
                "price": 10.0 * (1 + chg / 100),
                "changePct": chg,
                "amount": 5e7,
            }
        return result
