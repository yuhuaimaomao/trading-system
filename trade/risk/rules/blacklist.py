"""黑名单规则"""

# 永久黑名单：退市/严重违规
PERMANENT_BLACKLIST = set()

# 临时黑名单：当前 ST/*ST
_ST_PREFIXES = ("ST", "*ST", "N", "C")


def is_blacklisted(stock_code: str) -> bool:
    if stock_code in PERMANENT_BLACKLIST:
        return True
    return False


def is_st_or_suspect(stock_name: str) -> bool:
    """通过名称判断是否 ST 或风险标的"""
    upper = stock_name.upper()
    return any(upper.startswith(p) for p in _ST_PREFIXES)


def check_listed_days(listed_days: int, min_days: int = 60) -> bool:
    """上市不足 N 天不可交易"""
    return listed_days >= min_days
