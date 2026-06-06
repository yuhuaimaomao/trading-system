"""买入候选处理管线 — 逐候选的入场策略判断 + 买入区位置评估 + 决策路由。

纯规则函数，由 buy_decision._check_buy_candidates 编排循环调用。
"""

from dataclasses import dataclass


@dataclass
class CandidateResult:
    """单个候选的处理结果。"""
    action: str = "skip"       # skip / alert_reject / alert_buy / alert_chase / alert_approach / execute
    message: str = ""
    size_mul: float = 1.0
    should_skip: bool = True   # True = 本轮不继续处理此候选


def resolve_entry(entry_rule: str, zone_pos: float) -> str | None:
    """根据 entry_rule 和买入区位置判断是否允许入场。返回 None = 允许。"""
    if entry_rule == "none":
        return "entry_rule=none，禁止买入"
    if entry_rule == "next_day":
        return "尾盘模式，次日再看"
    if entry_rule == "standard" and zone_pos > 0.7:
        return f"zone_pos={zone_pos:.0%}偏高，等回调"
    if entry_rule == "pullback" and zone_pos > 0.6:
        return f"zone_pos={zone_pos:.0%}等回调入场"
    if entry_rule == "confirm" and zone_pos > 0.4:
        return f"zone_pos={zone_pos:.0%}等回调买入"
    if entry_rule == "range_boundary" and zone_pos > 0.25:
        return f"zone_pos={zone_pos:.0%}等区间下沿再入场"
    if zone_pos <= 0.7:
        return None
    return f"zone_pos={zone_pos:.0%}偏高"


def check_above_zone(price: float, buy_max: float,
                     sector_trend: str) -> tuple[bool, bool, float]:
    """价格高于买入区时的判断。
    Returns: (chase_worthy, approach_worthy, above_pct)
    """
    above_pct = (price - buy_max) / buy_max * 100
    if above_pct > 3:
        return False, False, above_pct

    is_sector_strong = "持续走强" in sector_trend or ("走强" in sector_trend and "弱" not in sector_trend)
    is_sector_weak = any(w in sector_trend for w in ("持续走弱", "弱于大盘", "普跌", "横盘"))
    return is_sector_strong and not is_sector_weak, above_pct <= 3.0, above_pct


def classify_zone_position(price: float, buy_min: float, buy_max: float) -> dict:
    """判断价格在买入区的位置。"""
    zone_range = buy_max - buy_min if buy_max > buy_min else 1
    return {
        "in_zone": buy_min <= price <= buy_max,
        "below_zone": price < buy_min,
        "above_zone": price > buy_max,
        "zone_pos": (price - buy_min) / zone_range if zone_range > 0 else 0.5,
        "above_pct": (price - buy_max) / buy_max * 100 if buy_max > 0 else 0,
        "below_pct": (buy_min - price) / buy_min * 100 if buy_min > 0 else 0,
    }
