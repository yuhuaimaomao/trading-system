"""市场环境判定"""


def get_market_environment(index_price: float, index_ma20: float) -> str:
    """
    判定市场环境：牛市 / 震荡 / 熊市
    规则：沪深 300 处于 MA20 上方且 MA20 上翘 → 牛市
         处于 MA20 上下 3% → 震荡
         其余 → 熊市
    """
    if index_ma20 <= 0:
        return "swing"
    deviation = (index_price - index_ma20) / index_ma20
    if deviation > 0.03:
        return "bull"
    elif deviation < -0.03:
        return "bear"
    return "swing"


def get_max_position(env: str) -> float:
    """根据市场环境返回仓位上限"""
    limits = {"bull": 0.80, "swing": 0.50, "bear": 0.20}
    return limits.get(env, 0.50)
