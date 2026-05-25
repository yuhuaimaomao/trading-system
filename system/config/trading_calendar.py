# -*- coding: utf-8 -*-
"""
交易日历配置

优先读取 QMT 交易日历（qmt_calendar 表），
QMT 不可用时回退到硬编码节假日后排除周末。
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent.parent / "storage" / "stock_market.db"

# 硬编码节假日（QMT 不可用时的回退方案）
A_HOLIDAYS = [
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23", "2026-02-24",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
]

_qmt_cache = None  # 交易日集合的内存缓存


def _load_qmt_calendar():
    """从数据库加载 QMT 交易日历，失败返回 None"""
    global _qmt_cache
    if _qmt_cache is not None:
        return _qmt_cache
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute("SELECT DISTINCT trade_date FROM qmt_calendar WHERE market='sh'").fetchall()
        conn.close()
        if rows:
            _qmt_cache = {r[0] for r in rows}
            logger.info(f"QMT 交易日历加载: {len(_qmt_cache)} 个交易日")
            return _qmt_cache
    except Exception:
        pass
    return None


def is_trading_day(date_str: str = None):
    """判断是否为 A 股交易日，优先 QMT 日历，回退硬编码节假日"""
    if date_str is None:
        today = datetime.now()
    else:
        today = datetime.strptime(date_str, "%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    # 周末一定不是交易日
    if today.weekday() >= 5:
        return False

    qmt = _load_qmt_calendar()
    if qmt is not None:
        return today_str in qmt

    # 回退：硬编码节假日
    if today_str in A_HOLIDAYS:
        return False
    return True


def get_previous_trading_day(target_date: str = None, offset: int = 1):
    """
    获取上一个交易日
    
    Args:
        target_date: 目标日期，默认今天
        offset: 向前偏移的交易天数（默认1 = 上一个交易日，7 = 7个交易日前）
    
    Returns:
        上一个交易日字符串（YYYY-MM-DD）或 None（如果没找到）
    """
    if target_date is None:
        target_date = datetime.now()
    else:
        target_date = datetime.strptime(target_date, "%Y-%m-%d")
    
    check_date = target_date - timedelta(days=1)
    found = 0
    
    # 最多向前查 60 天（覆盖长假+周末）
    for _ in range(60):
        date_str = check_date.strftime("%Y-%m-%d")
        
        if is_trading_day(date_str):
            found += 1
            if found >= offset:
                return date_str
        
        check_date -= timedelta(days=1)
    
    return None


def get_recent_trading_days(target_date: str = None, count: int = 5) -> list:
    """
    获取最近 count 个交易日的日期列表（不包含 target_date 本身）

    Args:
        target_date: 目标日期，默认今天
        count: 需要获取的交易日数量

    Returns:
        交易日列表，按从远到近排序 [D-count, ..., D-1]
    """
    if target_date is None:
        target_date = datetime.now()
    elif isinstance(target_date, str):
        target_date = datetime.strptime(target_date, "%Y-%m-%d")

    days = []
    check_date = target_date - timedelta(days=1)
    while len(days) < count:
        date_str = check_date.strftime("%Y-%m-%d")
        if is_trading_day(date_str):
            days.append(date_str)
        check_date -= timedelta(days=1)
        if (target_date - check_date).days > 60:
            break  # 最多查60天
    return days


def get_next_trading_day(target_date: str = None):
    """
    获取下一个交易日
    
    Args:
        target_date: 目标日期，默认今天
    
    Returns:
        下一个交易日字符串（YYYY-MM-DD）或 None（如果没找到）
    """
    if target_date is None:
        target_date = datetime.now()
    else:
        target_date = datetime.strptime(target_date, "%Y-%m-%d")
    
    # 从目标日期的下一天开始向后查找
    check_date = target_date + timedelta(days=1)
    
    # 最多向后查 30 天
    for _ in range(30):
        date_str = check_date.strftime("%Y-%m-%d")
        
        if is_trading_day(date_str):
            return date_str
        
        check_date += timedelta(days=1)
    
    # 如果没找到（理论上不应该发生），返回 None
    return None