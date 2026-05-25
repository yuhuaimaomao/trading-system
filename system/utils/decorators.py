# -*- coding: utf-8 -*-
"""
装饰器模块

提供常用装饰器：
- trading_day_only: 仅交易日执行
"""

import functools
import logging
from datetime import datetime
from system.config.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def trading_day_only(func):
    """
    交易日才执行装饰器
    
    用法:
        @trading_day_only
        def review():
            ...
    
    Returns:
        如果非交易日，返回 {'status': 'skipped', 'reason': 'non_trading_day'}
        如果交易日，执行原函数
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            if not is_trading_day():
                logger.info("⚠️ 今日非交易日，跳过")
                return {'status': 'skipped', 'reason': 'non_trading_day'}
        except Exception as e:
            logger.error(f"交易日检查失败：{e}")
            return {'status': 'error', 'reason': str(e)}
        
        return func(*args, **kwargs)
    return wrapper
