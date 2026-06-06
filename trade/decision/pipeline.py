"""买入候选处理管线 — 逐候选的入场策略判断 + 买入区位置评估。

纯规则函数，由 buy_decision._check_buy_candidates 编排循环调用。
"""


def resolve_entry(entry_rule: str, zone_pos: float, price: float,
                  buy_min: float, buy_max: float) -> str | None:
    """根据 entry_rule 和买入区位置判断是否允许入场。

    返回 None = 允许，否则为跳过原因。
    """
    if entry_rule == "none":
        return "entry_rule=none，禁止买入"

    if entry_rule == "next_day":
        return "尾盘模式，次日再看"

    if entry_rule == "standard" and zone_pos > 0.7:
        return f"zone_pos={zone_pos:.0%}偏高，等回调"

    if entry_rule == "pullback":
        if zone_pos > 0.6:
            return f"zone_pos={zone_pos:.0%}等回调入场"
        if zone_pos < 0.2:
            return None  # 下沿，可入场

    if entry_rule == "confirm":
        if zone_pos > 0.4:
            return f"zone_pos={zone_pos:.0%}等回调买入"
        if zone_pos < 0.15:
            return None

    if entry_rule == "range_boundary":
        if zone_pos > 0.25:
            return f"zone_pos={zone_pos:.0%}等区间下沿再入场"
        if zone_pos < 0.1:
            return None

    # standard / pullback 且 zone_pos 不太高 → 允许
    if zone_pos <= 0.7:
        return None
    return f"zone_pos={zone_pos:.0%}偏高"


def check_above_zone(price: float, buy_max: float,
                     sector_trend: str) -> tuple[bool, bool]:
    """价格高于买入区时的判断。

    Returns: (is_chase_worthy, is_approach_worthy)
    - is_chase_worthy: 板块强，值得追高提醒
    - is_approach_worthy: 距买入区 < 3%，值得预告
    """
    above_pct = (price - buy_max) / buy_max * 100

    # 距买入区 > 3% → 太远，不关注
    if above_pct > 3:
        return False, False

    # 板块走强才值得追
    is_sector_strong = "持续走强" in sector_trend or ("走强" in sector_trend and "弱" not in sector_trend)
    is_sector_weak = any(w in sector_trend for w in ("持续走弱", "弱于大盘", "普跌", "横盘"))

    chase_worthy = is_sector_strong and not is_sector_weak
    approach_worthy = above_pct <= 3.0

    return chase_worthy, approach_worthy


def classify_zone_position(price: float, buy_min: float, buy_max: float) -> dict:
    """判断价格在买入区的位置。

    Returns: {in_zone, below_zone, above_zone, zone_pos, above_pct, below_pct}
    """
    zone_range = buy_max - buy_min if buy_max > buy_min else 1

    return {
        "in_zone": buy_min <= price <= buy_max,
        "below_zone": price < buy_min,
        "above_zone": price > buy_max,
        "zone_pos": (price - buy_min) / zone_range if zone_range > 0 else 0.5,
        "above_pct": (price - buy_max) / buy_max * 100 if buy_max > 0 else 0,
        "below_pct": (buy_min - price) / buy_min * 100 if buy_min > 0 else 0,
    }
