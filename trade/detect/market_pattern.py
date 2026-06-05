"""大盘16种模式分类 — 从日内价格序列识别市场状态。

纯检测逻辑，不依赖 Watcher state。输入价格序列+辅助数据，输出模式名。
"""

from datetime import datetime
from datetime import time as dt_time

MORNING_START = dt_time(9, 30)
MORNING_END = dt_time(11, 30)
AFTERNOON_START = dt_time(13, 0)
LATE_SESSION = dt_time(14, 30)


# ━━━━━━━━ 工具函数 ━━━━━━━━


def _session_phase() -> str:
    """判断当前处于哪个交易时段。"""
    now = datetime.now().time()
    if now < MORNING_START:
        return "pre_open"
    if now < dt_time(10, 0):
        return "opening"
    if now < dt_time(11, 0):
        return "morning"
    if now < MORNING_END:
        return "late_morning"
    if now < AFTERNOON_START:
        return "lunch"
    if now < dt_time(14, 0):
        return "afternoon"
    if now < LATE_SESSION:
        return "late_afternoon"
    return "closing"


def _calc_intraday_ema(prices: list[float], period: int) -> float:
    """从价格序列计算日内EMA最新值。"""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for i in range(period, len(prices)):
        ema = prices[i] * k + ema * (1 - k)
    return ema


# ━━━━━━━━ 模式检测子函数 ━━━━━━━━


def _detect_higher_highs(px: list[float]) -> bool:
    """每 ~20 分钟窗口做一次比较，连续 3 个窗口创新高 → 强势单边上涨。"""
    if len(px) < 60:
        return False
    window = 20
    windows = [px[i:i + window] for i in range(0, len(px) - window + 1, window)]
    if len(windows) < 3:
        return False
    recent = windows[-3:]
    highs = [max(w) for w in recent]
    return highs[0] < highs[1] < highs[2]


def _detect_w_bottom(px: list[float], n: int, medium_n: int, lo: float,
                     hi: float, turnovers: list[float] | None = None) -> bool:
    """W型双底检测。"""
    if n < 60:
        return False

    ema12 = _calc_intraday_ema(px, 12)
    cur = px[-1]
    first_third_avg = sum(px[:n // 3]) / (n // 3) if n >= 3 else px[0]
    if first_third_avg < cur * 0.998 and cur > ema12:
        return False

    mid = n // 2
    first_half = px[:mid]
    second_half = px[mid:]

    def find_valleys(arr):
        valleys = []
        for i in range(1, len(arr) - 1):
            if arr[i] <= arr[i - 1] and arr[i] < arr[i + 1]:
                valleys.append((i, arr[i]))
        return valleys

    v1 = find_valleys(first_half)
    v2 = find_valleys(second_half)
    if not v1 or not v2:
        return False

    bottom1 = min(v1, key=lambda x: x[1])
    bottom2 = min(v2, key=lambda x: x[1])
    b1, b2 = bottom1[1], bottom2[1]
    if abs(b1 - b2) / b1 > 0.008:
        return False

    mid_section = px[bottom1[0]:mid + bottom2[0]]
    if not mid_section:
        return False
    peak = max(mid_section)
    if peak <= 0 or (peak - min(b1, b2)) / min(b1, b2) < 0.01:
        return False
    if cur <= peak:
        return False

    valley_pos = bottom2[0]
    surrounding = second_half[max(0, valley_pos - 3):min(len(second_half), valley_pos + 4)]
    if surrounding:
        valley_depth = (max(surrounding) - b2) / b2 if b2 > 0 else 0
        if valley_depth < 0.005:
            return False

    if turnovers and len(turnovers) >= n:
        vol1_idx = bottom1[0]
        vol2_idx = mid + bottom2[0]
        vol_data = turnovers[-n:] if len(turnovers) >= n else turnovers
        if vol1_idx < len(vol_data) and vol2_idx < len(vol_data):
            vol_around_b1 = vol_data[max(0, vol1_idx - 2):min(len(vol_data), vol1_idx + 3)]
            vol_around_b2 = vol_data[max(0, vol2_idx - 2):min(len(vol_data), vol2_idx + 3)]
            avg_vol1 = sum(vol_around_b1) / len(vol_around_b1) if vol_around_b1 else 0
            avg_vol2 = sum(vol_around_b2) / len(vol_around_b2) if vol_around_b2 else 0
            if avg_vol1 > 0 and avg_vol2 > avg_vol1 * 1.1:
                return False

    return True


def _detect_m_top(px: list[float], n: int, medium_n: int,
                  lo: float, hi: float) -> bool:
    """M型双顶检测。"""
    if n < 40:
        return False
    mid = n // 2
    first_half = px[:mid]
    second_half = px[mid:]

    def find_peaks(arr):
        peaks = []
        for i in range(1, len(arr) - 1):
            if arr[i] >= arr[i - 1] and arr[i] > arr[i + 1]:
                peaks.append((i, arr[i]))
        return peaks

    p1 = find_peaks(first_half)
    p2 = find_peaks(second_half)
    if not p1 or not p2:
        return False

    top1 = max(p1, key=lambda x: x[1])
    top2 = max(p2, key=lambda x: x[1])
    t1, t2 = top1[1], top2[1]
    if abs(t1 - t2) / t1 > 0.01:
        return False

    mid_section = px[top1[0]:mid + top2[0]]
    if not mid_section:
        return False
    valley = min(mid_section)
    if (max(t1, t2) - valley) / max(t1, t2) < 0.01:
        return False

    cur = px[-1]
    pos_in_range = (cur - lo) / (hi - lo)
    return cur < t2 * 0.997 and pos_in_range < 0.5


def _detect_gap_up_fade(px: list[float], n: int, short_chg: float,
                        pos_in_range: float, range_pct: float,
                        hi: float, lo: float, quote: dict | None) -> bool:
    """跳空高开后持续回落。"""
    if range_pct < 0.008:
        return False
    open_price = px[0]
    open_zone = (open_price - lo) / (hi - lo) if hi > lo else 0.5
    if open_zone < 0.6:
        return False
    if not quote:
        return False
    prev = quote.get("pre_close", 0)
    if prev <= 0 or (open_price - prev) / prev < 0.005:
        return False
    return pos_in_range < 0.3 and short_chg < -0.0015


def _detect_gap_down_recover(px: list[float], n: int, short_chg: float,
                             pos_in_range: float, range_pct: float,
                             hi: float, lo: float, quote: dict | None) -> bool:
    """跳空低开后持续回升。"""
    if range_pct < 0.008:
        return False
    open_price = px[0]
    open_zone = (open_price - lo) / (hi - lo) if hi > lo else 0.5
    if open_zone > 0.3:
        return False
    if not quote:
        return False
    prev = quote.get("pre_close", 0)
    if prev <= 0 or (prev - open_price) / prev < 0.005:
        return False
    return pos_in_range > 0.7 and short_chg > 0.0015


def _detect_late_dump(px: list[float], n: int, short_n: int,
                      short_chg: float, range_pct: float) -> bool:
    """尾盘时段快速下跌。"""
    if n < short_n * 2:
        return False
    recent = px[-short_n:]
    prev = px[-2 * short_n:-short_n]
    avg_recent = sum(recent) / len(recent)
    avg_prev = sum(prev) / len(prev)
    drop = (avg_recent - avg_prev) / avg_prev if avg_prev > 0 else 0
    return drop < -0.003


def _detect_late_rally(px: list[float], n: int, short_n: int,
                       short_chg: float, range_pct: float) -> bool:
    """尾盘时段快速拉升。"""
    if n < short_n * 2:
        return False
    early = px[:int(n * 0.8)]
    if len(early) >= 10:
        early_chg = (early[-1] - early[0]) / early[0] if early[0] > 0 else 0
        if early_chg > 0.005:
            return False
    recent = px[-short_n:]
    prev = px[-2 * short_n:-short_n]
    avg_recent = sum(recent) / len(recent)
    avg_prev = sum(prev) / len(prev)
    rise = (avg_recent - avg_prev) / avg_prev if avg_prev > 0 else 0
    return rise > 0.002


def _detect_fishing_line(px: list[float], n: int, medium_n: int,
                         short_n: int, hi: float, lo: float,
                         phase: str) -> bool:
    """全天缓慢推升→尾盘急剧下跌，典型出货信号。"""
    if n < 40 or phase not in ("late_afternoon", "closing"):
        return False
    first_80pct = px[:int(n * 0.8)]
    if len(first_80pct) < 15:
        return False
    first_chg = (first_80pct[-1] - first_80pct[0]) / first_80pct[0] if first_80pct[0] > 0 else 0
    if first_chg < 0.005:
        return False
    last_20pct = px[int(n * 0.8):]
    if len(last_20pct) < 5:
        return False
    last_chg = (last_20pct[-1] - last_20pct[0]) / last_20pct[0] if last_20pct[0] > 0 else 0
    return last_chg < -0.005


def _detect_wide_choppy(px: list[float], n: int, medium_n: int,
                        ema12: float, ema26: float, range_pct: float,
                        day_hi: float, day_lo: float) -> bool:
    """振幅>1%但无方向，价格多次穿越EMA12。"""
    if range_pct < 0.01 or n < 30:
        return False
    crosses = 0
    prev_above = px[0] > ema12 if ema12 > 0 else None
    for p in px[1:]:
        if ema12 <= 0:
            break
        cur_above = p > ema12
        if prev_above is not None and cur_above != prev_above:
            crosses += 1
        prev_above = cur_above
    pos_in_range = (px[-1] - day_lo) / (day_hi - day_lo) if day_hi > day_lo else 0.5
    return crosses >= 3 and 0.3 < pos_in_range < 0.7


# ━━━━━━━━ 主分类函数 ━━━━━━━━


def classify_market_pattern(
    index_prices: list[float],
    index_high: float,
    index_low: float,
    market_turnovers: list[float] | None = None,
    market_snapshot: dict | None = None,
    last_index_quote: dict | None = None,
) -> str:
    """识别市场模式：基于多时间窗口滚动对比 + 日内EMA + 分时结构。

    已支持 16 种模式：
    趋势类: normal, uptrend, one_sided
    反转类: v_reversal, inverted_v, w_bottom, m_top
    极端类: panic, melt_up
    陷阱类: dead_cat, fishing_line
    跳空类: gap_up_fade, gap_down_recover
    尾盘类: late_rally, late_dump
    震荡类: wide_choppy

    Args:
        index_prices: 日内上证价格序列
        index_high: 日内最高价
        index_low: 日内最低价
        market_turnovers: 全市场成交额序列（W底量能确认用，可选）
        market_snapshot: 全市场快照（计算涨跌比用，可选）
        last_index_quote: 最新指数行情（跳空确认用，需含 pre_close）
    """
    px = index_prices
    if len(px) < 20:
        return "normal"

    n = len(px)
    hi, lo = index_high, index_low
    if hi <= lo:
        return "normal"

    cur = px[-1]
    range_pct = (hi - lo) / lo
    pos_in_range = (cur - lo) / (hi - lo)

    ema12 = _calc_intraday_ema(px, 12)
    ema26 = _calc_intraday_ema(px, 26)

    short_n = min(15, max(5, n // 4))
    medium_n = min(60, max(20, n // 2))

    short_recent = px[-short_n:]
    short_prev = px[-2 * short_n:-short_n] if n >= 2 * short_n else px[:short_n]
    avg_short = sum(short_recent) / (len(short_recent) or 1)
    avg_short_prev = sum(short_prev) / (len(short_prev) or 1)
    short_chg = (avg_short - avg_short_prev) / avg_short_prev if avg_short_prev > 0 else 0

    medium_recent = px[-medium_n:]
    avg_medium = sum(medium_recent) / (len(medium_recent) or 1)

    phase = _session_phase()

    # ━━ 尾盘时段优先检测 ━━
    if phase in ("late_afternoon", "closing"):
        if _detect_fishing_line(px, n, medium_n, short_n, hi, lo, phase):
            return "fishing_line"
        if _detect_late_dump(px, n, short_n, short_chg, range_pct):
            return "late_dump"
        if _detect_late_rally(px, n, short_n, short_chg, range_pct):
            return "late_rally"

    # ━━ 跳空检测 ━━
    if _detect_gap_up_fade(px, n, short_chg, pos_in_range, range_pct, hi, lo, last_index_quote):
        return "gap_up_fade"
    if _detect_gap_down_recover(px, n, short_chg, pos_in_range, range_pct, hi, lo, last_index_quote):
        return "gap_down_recover"

    # ━━ 恐慌 ━━
    if range_pct > 0.01 and pos_in_range < 0.2:
        drop_short = abs(short_chg) if short_chg < -0.002 else 0
        if n >= 2 * medium_n:
            medium_prev_px = px[-2 * medium_n:-medium_n]
            avg_medium_prev = sum(medium_prev_px) / len(medium_prev_px) if medium_prev_px else avg_medium
            drop_medium = max(0, (avg_medium_prev - avg_medium) / avg_medium_prev) if avg_medium_prev > 0 else 0
            if drop_short > drop_medium * 0.8 and drop_short > 0.003:
                return "panic"
        elif drop_short > 0.004:
            return "panic"

    # ━━ 加速上涨(melt-up) / V型反转 / 死猫跳 ━━
    if range_pct > 0.01 and pos_in_range > 0.8:
        rise_short = short_chg if short_chg > 0.002 else 0
        if n >= 2 * medium_n:
            medium_prev_px = px[-2 * medium_n:-medium_n]
            avg_medium_prev = sum(medium_prev_px) / len(medium_prev_px) if medium_prev_px else avg_medium
            rise_medium = max(0, (avg_medium - avg_medium_prev) / avg_medium_prev) if avg_medium_prev > 0 else 0
            if rise_short > rise_medium * 0.8 and rise_short > 0.002:
                return "melt_up"
        elif rise_short > 0.003:
            return "melt_up"

    if short_chg > 0.002 and pos_in_range > 0.3:
        mid_low = min(px[-medium_n:])
        mid_start = px[-medium_n] if n >= medium_n else px[0]
        recovery = (cur - mid_low) / mid_low if mid_low > 0 else 0
        if recovery > 0.002 and mid_start > mid_low * 1.003:
            if pos_in_range > 0.5 and cur > ema12:
                return "v_reversal"
            drop_from_hi = (hi - mid_low) / hi if hi > 0 else 0
            if pos_in_range <= 0.5 and drop_from_hi > 0.005:
                return "dead_cat"

    # ━━ 单边下跌 ━━
    if ema12 > 0 and cur < ema12 and short_chg < 0:
        breadth_ok = True
        if market_snapshot:
            up = down = 0
            for item in market_snapshot.values():
                chg = item.get("changePct", 0)
                try:
                    chg = float(chg)
                except (ValueError, TypeError):
                    continue
                if chg > 0:
                    up += 1
                elif chg < 0:
                    down += 1
            total = up + down
            if total > 0 and down / total < 0.55:
                breadth_ok = False
        if breadth_ok:
            if n >= 2 * medium_n:
                medium_prev_px = px[-2 * medium_n:-medium_n]
                avg_medium_prev = sum(medium_prev_px) / len(medium_prev_px) if medium_prev_px else avg_medium
                if avg_medium < avg_medium_prev:
                    decline = (avg_medium_prev - avg_medium) / avg_medium_prev
                    if decline > 0.005:
                        return "one_sided"
            elif avg_medium < ema12 and short_chg < -0.003:
                return "one_sided"

    # ━━ 倒V/A型 ━━
    if range_pct > 0.01 and pos_in_range < 0.3 and short_chg < -0.002:
        open_zone = px[:min(short_n, len(px))]
        avg_open = sum(open_zone) / len(open_zone) if open_zone else lo
        if hi - avg_open > (hi - lo) * 0.35:
            return "inverted_v"

    # ━━ 单边上涨 ━━
    if _detect_higher_highs(px) and cur > ema12:
        return "uptrend"

    if ema12 > 0 and cur > ema12:
        if abs(short_chg) < 0.001 and n >= 2 * medium_n:
            medium_prev_px = px[-2 * medium_n:-medium_n]
            avg_medium_prev = sum(medium_prev_px) / len(medium_prev_px) if medium_prev_px else avg_medium
            if avg_medium > avg_medium_prev:
                rise = (avg_medium - avg_medium_prev) / avg_medium_prev
                if rise > 0.005:
                    return "uptrend"
        elif short_chg > 0:
            if n >= 2 * medium_n:
                medium_prev_px = px[-2 * medium_n:-medium_n]
                avg_medium_prev = sum(medium_prev_px) / len(medium_prev_px) if medium_prev_px else avg_medium
                if avg_medium > avg_medium_prev:
                    rise = (avg_medium - avg_medium_prev) / avg_medium_prev
                    if rise > 0.003:
                        return "uptrend"
            elif avg_medium > ema12 and short_chg > 0.002:
                return "uptrend"

    # ━━ 宽幅震荡 ━━
    if _detect_wide_choppy(px, n, medium_n, ema12, ema26, range_pct, hi, lo):
        return "wide_choppy"

    # ━━ W型双底 ━━
    if _detect_w_bottom(px, n, medium_n, lo, hi, market_turnovers):
        return "w_bottom"

    # ━━ M型双顶 ━━
    if _detect_m_top(px, n, medium_n, lo, hi):
        return "m_top"

    return "normal"
