"""黑名单规则"""

# 永久黑名单：退市/严重违规
PERMANENT_BLACKLIST = set()

# 风险前缀：ST/*ST 退市风险 + N/C 新股（未经过充分市场检验）
_RISK_PREFIXES = ("ST", "*ST", "N", "C")


def is_blacklisted(stock_code: str) -> bool:
    if stock_code in PERMANENT_BLACKLIST:
        return True
    return False


def is_risk_suspect(stock_name: str) -> bool:
    """通过名称判断是否风险标的（ST/新股等）"""
    upper = stock_name.upper()
    return any(upper.startswith(p) for p in _RISK_PREFIXES)


def check_listed_days(listed_days: int, min_days: int = 60) -> bool:
    """上市不足 N 天不可交易"""
    return listed_days >= min_days
