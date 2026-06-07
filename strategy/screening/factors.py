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
    _ma5 = row.get("ma5") or 0
    ma10 = row.get("ma10") or 0
    ma20 = row.get("ma20") or 0
    price = row.get("price") or 0

    if "ST" in name.upper():
        return False
    if code.startswith("688"):
        return False
    if change_pct <= -9.5:
        return False
    if change_pct >= 9.5:
        row["is_limit_up"] = True  # 涨停标记，不过滤
    if mcap < 50_0000_0000:  # 50 亿
        return False
    if vol_ratio <= 0.3:
        return False
    return price > ma20 and ma10 > ma20


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
    """5日均量 > 20日均量*1.2，且近5日累计涨幅 > 0。
    加价格条件是为了过滤「放量下跌」——量增价跌不在趋势筛选范围。
    """
    avg5 = row.get("avg_vol_5d") or 0
    avg20 = row.get("avg_vol_20d") or 0
    if not (avg5 > avg20 * 1.2 and avg20 > 0):
        return None
    # 近5日累计涨幅必须 > 0，排除放量下跌
    if len(history) >= 5:
        recent_returns = [(h.get("change_pct") or 0) for h in history[-5:]]
        if sum(recent_returns) <= 0:
            return None
    return "量能放大"


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
        std = var**0.5
        if std > 0:
            sharpe = (mean_ret / std) * (252**0.5)
            if sharpe > 1.0:
                return "趋势强劲"
    return None


# ============================================================
# 板块类 (4)
# ============================================================


def check_sector_hot(
    row: dict,
    history: list[dict],
    sector_hot: Optional[dict] = None,
    stock_sectors: Optional[dict] = None,
) -> Optional[str]:
    """任一所属概念板块近 5 日至少 1 次上榜 top5"""
    if not sector_hot or not stock_sectors:
        return None
    code = row.get("stock_code", "")
    sectors = stock_sectors.get(code, [])
    for s in sectors:
        if sector_hot.get(s, 0) >= 1:
            return "板块加持"
    return None


def check_leader_in_sector(
    row: dict,
    history: list[dict],
    sector_stocks_pct: Optional[dict] = None,
    stock_sectors: Optional[dict] = None,
) -> Optional[str]:
    """在所属概念板块个股中涨幅排前 3"""
    if not sector_stocks_pct or not stock_sectors:
        return None
    code = row.get("stock_code", "")
    sectors = stock_sectors.get(code, [])
    for s in sectors:
        stocks_in_sector = [c for c, scs in stock_sectors.items() if s in scs]
        ranks = sorted(
            [(c, sector_stocks_pct.get(c, -999)) for c in stocks_in_sector],
            key=lambda x: x[1],
            reverse=True,
        )
        top_codes = [r[0] for r in ranks[:3] if r[1] > -900]
        if code in top_codes:
            return "领涨龙头"
    return None


def check_stronger_than_sector(
    row: dict,
    history: list[dict],
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
    row: dict,
    history: list[dict],
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
    row: dict,
    history: list[dict],
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


# ============================================================
# 位置与量价配合类 (2)
# ============================================================


def check_price_position(row: dict, history: list[dict]) -> Optional[str]:
    """价格在近20日高低区间的相对位置。
    高位（>80%分位）：已涨很多，追高需谨慎，回踩信号要区分主力锁仓还是出货。
    低位（<30%分位）：刚启动或调整充分，放量信号更有参与价值。
    中间位置不返回标签，不干扰评分。
    """
    if len(history) < 20:
        return None
    price = row.get("price") or 0
    if price <= 0:
        return None
    highs = [h.get("high") or 0 for h in history[-20:]]
    lows = [h.get("low") or 0 for h in history[-20:]]
    hh, ll = max(highs), min(lows)
    if hh <= ll:
        return None
    # 当前价格在20日高低区间的百分位
    position_pct = (price - ll) / (hh - ll) * 100
    if position_pct > 80:
        return "高位运行"
    if position_pct < 30:
        return "低位启动"
    return None


def check_vol_price_rise(row: dict, history: list[dict]) -> Optional[str]:
    """量价齐升：当日上涨 + 量比 > 1.2。
    上涨日放量说明资金主动买入而非被动承接，是趋势延续的重要确认。
    与 check_volume_expand 互补：那个看5日/20日均量对比，这个看当日量价配合。
    """
    change_pct = row.get("change_pct") or 0
    vol_ratio = row.get("volume_ratio") or 0
    if change_pct > 0 and vol_ratio > 1.2:
        return "量价齐升"
    return None


def check_sector_vol_price(
    row: dict,
    history: list[dict],
    sector_changes: Optional[dict] = None,
    sector_funds: Optional[dict] = None,
    stock_sectors: Optional[dict] = None,
) -> Optional[str]:
    """板块量价齐升：个股所属板块涨 + 板块主力净流入 + 个股也涨。
    板块整体量价配合意味着板块资金在主动做多，不是个别股票的单打独斗。
    与 check_sector_fund_resonance 的区别：那个只看资金方向，这个加上了价格方向确认。
    """
    if not sector_changes or not sector_funds or not stock_sectors:
        return None
    code = row.get("stock_code", "")
    stock_pct = row.get("change_pct") or 0
    if stock_pct <= 0:
        return None  # 个股必须涨
    sectors = stock_sectors.get(code, [])
    for s in sectors:
        s_chg = sector_changes.get(s, -999)
        s_fund = sector_funds.get(s, 0)
        # 板块涨 + 板块资金进 → 板块量价齐升
        if s_chg > 0.5 and s_fund > 0:
            return "板块量价齐升"
    return None
