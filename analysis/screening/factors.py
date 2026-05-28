"""趋势筛选因子 — 每个函数返回标签名或 None

签名统一: fn(row: dict, history: list[dict]) -> Optional[str]
板块类因子的额外参数通过 kwargs 传入。
"""

from typing import Optional


# ============================================================
# 硬关卡
# ============================================================

def check_hard_gates(row: dict) -> bool:
    """6 道硬关卡（板块黑名单由 TrendScreener 基于 sector_stocks 统一过滤）"""
    name = str(row.get("stock_name", ""))
    code = str(row.get("stock_code", ""))
    change_pct = row.get("change_pct") or 0
    mcap = row.get("total_market_cap") or 0
    vol_ratio = row.get("volume_ratio") or 0
    ma5 = row.get("ma5") or 0
    ma10 = row.get("ma10") or 0
    ma20 = row.get("ma20") or 0
    price = row.get("price") or 0

    if "ST" in name.upper():
        return False
    if code.startswith("688"):
        return False
    if abs(change_pct) >= 9.5:
        return False
    if mcap < 50_0000_0000:  # 50 亿
        return False
    if vol_ratio <= 0.5:
        return False
    if not (price > ma20 and ma10 > ma20):
        return False
    return True


# ============================================================
# 量价类 (3)
# ============================================================

def check_volume_breakout(row: dict, history: list[dict]) -> Optional[str]:
    if (row.get("volume_ratio") or 0) >= 1.5:
        return "放量启动"
    return None


def check_volume_pullback(row: dict, history: list[dict]) -> Optional[str]:
    vol_ratio = row.get("volume_ratio") or 0
    change_pct = row.get("change_pct") or 0
    if vol_ratio <= 0.7 and -2.0 <= change_pct <= 0:
        return "缩量回调"
    return None


def check_amplitude_contract(row: dict, history: list[dict]) -> Optional[str]:
    if (row.get("amplitude") or 99) < 3.0:
        return "蓄力中"
    return None


# ============================================================
# 资金类 (2)
# ============================================================

def check_main_force_buy(row: dict, history: list[dict]) -> Optional[str]:
    mf_net = row.get("main_force_net") or 0
    mf_ratio = row.get("main_force_ratio") or 0
    if mf_net > 0 and mf_ratio > 3.0:
        return "主力介入"
    return None


def check_chip_concentrate(row: dict, history: list[dict]) -> Optional[str]:
    mf_net = row.get("main_force_net") or 0
    small_net = row.get("small_net") or 0
    if mf_net > 0 and small_net < 0:
        return "筹码集中"
    return None


# ============================================================
# 多日类 (6)
# ============================================================

def check_consecutive_yang(row: dict, history: list[dict]) -> Optional[str]:
    if len(history) < 3:
        return None
    recent = history[-3:]
    yang = sum(1 for h in recent if (h.get("price") or 0) > (h.get("open") or 0))
    if yang >= 3:
        return "强势连阳"
    return None


def check_pullback_hold(row: dict, history: list[dict]) -> Optional[str]:
    if len(history) < 5:
        return None
    high5 = max((h.get("high") or 0 for h in history[-5:]), default=0)
    if high5 <= 0:
        return None
    price = row.get("price") or 0
    ma10 = row.get("ma10") or 999
    pullback = (high5 - price) / high5
    if pullback < 0.05 and price > ma10:
        return "回踩确认"
    return None


def check_trend_persist(row: dict, history: list[dict]) -> Optional[str]:
    if len(history) < 5:
        return None
    for h in history[-5:]:
        if not ((h.get("ma5") or 0) > (h.get("ma10") or 0) > (h.get("ma20") or 0)):
            return None
    return "趋势延续"


def check_low_volatility(row: dict, history: list[dict]) -> Optional[str]:
    """ATR14 / price < 3%"""
    if len(history) < 14:
        return None
    tr_sum = 0.0
    for h in history[-14:]:
        hi = h.get("high") or 0
        lo = h.get("low") or 0
        pc = h.get("prev_close") or 0
        tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
        tr_sum += tr
    atr14 = tr_sum / 14
    price = row.get("price") or 1
    if atr14 / price < 0.03:
        return "低波蓄力"
    return None


def check_volume_expand(row: dict, history: list[dict]) -> Optional[str]:
    avg5 = row.get("avg_vol_5d") or 0
    avg20 = row.get("avg_vol_20d") or 0
    if avg5 > avg20 * 1.2 and avg20 > 0:
        return "量能放大"
    return None


def check_trend_strength(row: dict, history: list[dict]) -> Optional[str]:
    """近 10 日涨幅 > 5% AND sharpe_20 > 1.0"""
    if len(history) < 10:
        return None
    changes = [h.get("change_pct") or 0 for h in history[-10:]]
    cum_return = 1.0
    for c in changes:
        cum_return *= 1 + c / 100
    total_return = (cum_return - 1) * 100
    if total_return <= 5.0:
        return None

    if len(history) >= 20:
        ch20 = [h.get("change_pct") or 0 for h in history[-20:]]
        mean_ret = sum(ch20) / len(ch20)
        if mean_ret == 0:
            return None
        var = sum((c - mean_ret) ** 2 for c in ch20) / len(ch20)
        std = var ** 0.5
        if std > 0:
            sharpe = (mean_ret / std) * (252 ** 0.5)
            if sharpe > 1.0:
                return "趋势强劲"
    return None


# ============================================================
# RPS 相对强弱 (4)
# ============================================================

def check_rps_20_strong(row: dict, history: list[dict]) -> Optional[str]:
    if (row.get("rps_20") or 0) >= 0.80:
        return "RPS20强"
    return None


def check_rps_60_strong(row: dict, history: list[dict]) -> Optional[str]:
    if (row.get("rps_60") or 0) >= 0.80:
        return "RPS60强"
    return None


def check_rps_120_strong(row: dict, history: list[dict]) -> Optional[str]:
    if (row.get("rps_120") or 0) >= 0.80:
        return "RPS120强"
    return None


def check_rps_resonance(row: dict, history: list[dict]) -> Optional[str]:
    rps20 = row.get("rps_20") or 0
    rps60 = row.get("rps_60") or 0
    if rps20 >= 0.70 and rps60 >= 0.70:
        return "RPS多周期共振"
    return None


# ============================================================
# 板块类 (4)
# ============================================================

def check_sector_hot(
    row: dict, history: list[dict],
    sector_hot: Optional[dict] = None,
    stock_sectors: Optional[dict] = None,
) -> Optional[str]:
    """任一所属概念板块近 3 日至少 1 次上榜 top5"""
    if not sector_hot or not stock_sectors:
        return None
    code = row.get("stock_code", "")
    sectors = stock_sectors.get(code, [])
    for s in sectors:
        if sector_hot.get(s, 0) >= 1:
            return "板块加持"
    return None


def check_leader_in_sector(
    row: dict, history: list[dict],
    sector_stocks_pct: Optional[dict] = None,
    stock_sectors: Optional[dict] = None,
) -> Optional[str]:
    """在所属概念板块个股中涨幅排前 3"""
    if not sector_stocks_pct or not stock_sectors:
        return None
    code = row.get("stock_code", "")
    sectors = stock_sectors.get(code, [])
    for s in sectors:
        stocks_in_sector = [
            c for c, scs in stock_sectors.items() if s in scs
        ]
        ranks = sorted(
            [(c, sector_stocks_pct.get(c, -999)) for c in stocks_in_sector],
            key=lambda x: x[1], reverse=True,
        )
        top_codes = [r[0] for r in ranks[:3] if r[1] > -900]
        if code in top_codes:
            return "领涨龙头"
    return None


def check_stronger_than_sector(
    row: dict, history: list[dict],
    sector_changes: Optional[dict] = None,
    stock_sectors: Optional[dict] = None,
) -> Optional[str]:
    """个股涨幅 > 所属板块涨幅 + 0.5pp"""
    if not sector_changes or not stock_sectors:
        return None
    code = row.get("stock_code", "")
    pct = row.get("change_pct") or 0
    sectors = stock_sectors.get(code, [])
    for s in sectors:
        s_chg = sector_changes.get(s, -999)
        if pct > s_chg + 0.5:
            return "强于板块"
    return None


def check_sector_fund_resonance(
    row: dict, history: list[dict],
    sector_funds: Optional[dict] = None,
    stock_sectors: Optional[dict] = None,
) -> Optional[str]:
    """板块主力净买 > 0 AND 个股主力净买 > 0"""
    if not sector_funds or not stock_sectors:
        return None
    code = row.get("stock_code", "")
    mf_net = row.get("main_force_net") or 0
    if mf_net <= 0:
        return None
    sectors = stock_sectors.get(code, [])
    for s in sectors:
        if sector_funds.get(s, 0) > 0:
            return "资金共振"
    return None


def check_weekly_bbi(
    row: dict, history: list[dict],
    weekly_bbi_map: Optional[dict] = None,
) -> Optional[str]:
    """价格在周线 BBI 上方 → 中期趋势多头确认"""
    if not weekly_bbi_map:
        return None
    code = row.get("stock_code", "")
    bbi = weekly_bbi_map.get(code)
    if bbi is None:
        return None
    price = row.get("price") or 0
    if price > bbi:
        return "周线多头"
    return None
