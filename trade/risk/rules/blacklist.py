"""黑名单规则"""

# 永久黑名单：退市/严重违规（运行时从 DB 加载 ST 列表填充）
PERMANENT_BLACKLIST: set[str] = set()

# 风险前缀：ST/*ST 退市风险 + N/C 新股（未经过充分市场检验）
_RISK_PREFIXES = ("ST", "*ST", "N", "C")

_LOADED = False


def load_blacklist(db_path: str = None) -> int:
    """从 stock_basic 加载 ST/*ST 股票代码到黑名单。返回加载数量。"""
    global PERMANENT_BLACKLIST, _LOADED

    try:
        from data._base import get_db_conn

        with get_db_conn(db_path) as conn:
            rows = conn.execute(
                """SELECT DISTINCT stock_code FROM stock_basic
                   WHERE (stock_name LIKE 'ST%' OR stock_name LIKE '*ST%')
                   AND trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
        codes = {r[0] for r in rows}
        PERMANENT_BLACKLIST.update(codes)
        _LOADED = True
        return len(codes)
    except Exception:
        return 0


def refresh_blacklist(db_path: str = None) -> int:
    """清空并重新加载黑名单（用于每日数据更新后）。"""
    global PERMANENT_BLACKLIST, _LOADED
    PERMANENT_BLACKLIST.clear()
    _LOADED = False
    return load_blacklist(db_path)


def is_blacklisted(stock_code: str) -> bool:
    if not _LOADED:
        load_blacklist()
    return stock_code in PERMANENT_BLACKLIST


def is_risk_suspect(stock_name: str) -> bool:
    """通过名称判断是否风险标的（ST/新股等）"""
    upper = stock_name.upper()
    return any(upper.startswith(p) for p in _RISK_PREFIXES)


def check_listed_days(listed_days: int, min_days: int = 60) -> bool:
    """上市不足 N 天不可交易"""
    return listed_days >= min_days
