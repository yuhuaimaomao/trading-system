"""板块趋势检测 — 从 sector_stats 数据识别板块方向、强度、热度。

纯检测函数，不依赖 Watcher state。
"""


def get_concept_trend_score(
    code: str, concept_cache: dict[str, list[str]], concept_stats: dict[str, dict],
) -> tuple[int, str]:
    """返回股票所属概念板块的趋势评分。正=概念偏强，负=概念偏弱。"""
    concepts = concept_cache.get(code, [])
    if not concepts:
        return 0, ""

    score = 0
    weak_count = 0
    strong_count = 0
    for c in concepts[:5]:
        cs = concept_stats.get(c, {})
        if not cs:
            continue
        chg = cs.get("change_pct", 0)
        if chg < -1.0:
            weak_count += 1
            score -= 1
        elif chg > 1.0:
            strong_count += 1
            score += 1

    reason = ""
    if weak_count >= 3:
        reason = f" {weak_count}个概念板块偏弱"
    elif strong_count >= 3:
        reason = f" {strong_count}个概念板块偏强"

    return max(-3, min(3, score)), reason


def get_sector_trend(
    code: str,
    industry_cache: dict[str, str],
    sector_stats: dict[str, dict],
    concept_cache: dict[str, list[str]] | None = None,
    concept_stats: dict[str, dict] | None = None,
) -> str:
    """返回股票所在板块的日内趋势描述 — 含行业、概念、连续性、量能、相对强度。"""
    industry = industry_cache.get(code, "")
    if not industry:
        return ""

    stats = sector_stats.get(industry)
    if not stats:
        return "数据不足"

    history = stats.get("trend_history", [])
    if len(history) < 2:
        return "数据积累中"

    # 1. 趋势方向 + 强度
    first, last = history[0], history[-1]
    cumulative = last - first
    n = len(history)

    # 线性回归斜率
    x_mean = (n - 1) / 2
    y_mean = sum(history) / n
    num = sum((i - x_mean) * (history[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0

    if slope > 0.003 and n >= 5:
        direction = "持续走强" if cumulative > 0.3 else "走强"
    elif slope < -0.003 and n >= 5:
        direction = "持续走弱" if cumulative < -0.3 else "走弱"
    elif abs(cumulative) < 0.3:
        direction = "横盘"
    elif cumulative > 0:
        direction = "走强"
    else:
        direction = "走弱"

    # 2. 加速度
    is_weak = "走弱" in direction
    is_strong = "走强" in direction
    accel = ""
    if n >= 4:
        half = n // 2
        recent_half = history[-half:]
        r_mean = sum(recent_half) / half if half > 0 else 0
        recent_slope = sum(
            (i - (half - 1) / 2) * (recent_half[i] - r_mean)
            for i in range(half)
        ) / max(sum((i - (half - 1) / 2) ** 2 for i in range(half)), 0.01)
        if (is_strong and recent_slope > slope * 1.5) or (is_weak and recent_slope < slope * 1.5):
            accel = "加速"
        elif (is_strong and recent_slope < slope * 0.3) or (is_weak and recent_slope > slope * 0.3):
            accel = "趋缓"

    # 3. 相对强度
    rel = stats.get("relative", 0)
    rel_str = ""
    if rel > 0.5:
        rel_str = "强于大盘"
    elif rel < -0.5:
        rel_str = "弱于大盘"

    # 4. 板块广度
    breadth = stats.get("breadth", 0)
    breadth_str = ""
    if breadth > 0.4:
        breadth_str = "普涨"
    elif breadth < -0.4:
        breadth_str = "普跌"

    # 5. 量能
    vol_ratio = stats.get("vol_ratio", 1.0)
    vol_str = ""
    if vol_ratio > 1.5:
        vol_str = "放量"
    elif vol_ratio < 0.5:
        vol_str = "缩量"

    parts = [industry, direction]
    if accel:
        parts.append(accel)
    parts.append(f"{cumulative:+.1f}%")
    if vol_str:
        parts.append(vol_str)
    if rel_str:
        parts.append(rel_str)
    if breadth_str:
        parts.append(breadth_str)

    continuity = stats.get("continuity", 0)
    if continuity >= 3:
        parts.append(f"连续{continuity}轮")

    # 概念板块叠加
    if concept_cache and concept_stats:
        concepts = concept_cache.get(code, [])
        if concepts:
            concept_parts = []
            for c in concepts[:3]:
                cs = concept_stats.get(c, {})
                if cs:
                    cp = cs.get("change_pct", 0)
                    concept_parts.append(f"{c}{cp:+.1f}%")
            if concept_parts:
                parts.append("|".join(concept_parts[:2]))

    return " ".join(parts)


def get_sector_change(
    code: str, industry_cache: dict[str, str], sector_stats: dict[str, dict],
) -> float | None:
    """返回股票所属行业的平均涨跌幅。"""
    industry = industry_cache.get(code, "")
    if not industry:
        return None
    stats = sector_stats.get(industry)
    if not stats:
        return None
    return stats.get("change_pct")


def get_sector_decline(
    code: str, industry_cache: dict[str, str], sector_stats: dict[str, dict],
) -> float | None:
    """板块从近期高点回落的幅度（正数=回落多少）。"""
    industry = industry_cache.get(code, "")
    if not industry:
        return None
    stats = sector_stats.get(industry)
    if not stats:
        return None
    history = stats.get("trend_history", [])
    if len(history) < 3:
        return None
    recent = history[-5:]
    peak = max(recent)
    current = recent[-1]
    decline = peak - current
    return round(decline, 2) if decline > 0 else None


def get_sector_recovery_risk(
    code: str, industry_cache: dict[str, str], sector_stats: dict[str, dict],
) -> float | None:
    """板块从日内深跌中反弹的幅度（死猫跳风险）。"""
    industry = industry_cache.get(code, "")
    if not industry:
        return None
    stats = sector_stats.get(industry)
    if not stats:
        return None
    history = stats.get("trend_history", [])
    if len(history) < 6:
        return None
    intra_low = min(history)
    current = history[-1]
    recovery = current - intra_low
    if recovery > 2.0:
        return round(recovery, 2)
    return None
