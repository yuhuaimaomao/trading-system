"""市场环境判定 — 多维度打分（均线+量能+宽度+波动+板块轮动）。"""


def get_market_environment(
    index_price: float,
    index_ma20: float,
    index_ma60: float = 0,
    volume_trend: float = 0,
    breadth_ratio: float = 0,
    daily_amplitude: float = 0,
    active_sectors: int = 0,
) -> str:
    """多维度判定市场环境：bull / swing / bear。

    维度：
    - 价格 vs MA20/MA60（中期/长期趋势）
    - 量能趋势（近5天成交额变化方向）
    - 市场宽度（涨跌家数比）
    - 日内振幅（波动率）
    - 活跃板块数（赚钱效应）
    """
    score = 0

    # 1. 价格 vs MA20（权重最高）
    if index_ma20 > 0:
        dev20 = (index_price - index_ma20) / index_ma20
        if dev20 > 0.05:
            score += 3
        elif dev20 > 0.02:
            score += 2
        elif dev20 > 0:
            score += 1
        elif dev20 < -0.05:
            score -= 3
        elif dev20 < -0.02:
            score -= 2
        else:
            score -= 1

    # 2. 价格 vs MA60（长期趋势确认）
    if index_ma60 > 0:
        dev60 = (index_price - index_ma60) / index_ma60
        if dev60 > 0.03:
            score += 2
        elif dev60 > 0:
            score += 1
        elif dev60 < -0.03:
            score -= 2
        else:
            score -= 1

    # 3. 量能趋势（正=放量，负=缩量）
    if volume_trend > 0.15:
        score += 2
    elif volume_trend > 0:
        score += 1
    elif volume_trend < -0.15:
        score -= 2
    elif volume_trend < 0:
        score -= 1

    # 4. 涨跌家数比（>1=涨多跌少）
    if breadth_ratio > 2.0:
        score += 2
    elif breadth_ratio > 1.2:
        score += 1
    elif breadth_ratio < 0.5:
        score -= 2
    elif breadth_ratio < 0.8:
        score -= 1

    # 5. 日内振幅（高波动+无方向=风险）
    if daily_amplitude > 0.03:
        score -= 1
    elif daily_amplitude < 0.008:
        score += 0

    # 6. 活跃板块数（赚钱效应）
    if active_sectors >= 5:
        score += 1
    elif active_sectors <= 1:
        score -= 1

    # 总分 → 环境判定
    if score >= 3:
        return "bull"
    elif score >= 0:
        return "swing"
    else:
        return "bear"


def get_max_position(env: str) -> float:
    """根据市场环境返回仓位上限"""
    limits = {"bull": 0.80, "swing": 0.50, "bear": 0.20}
    return limits.get(env, 0.50)
