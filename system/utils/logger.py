# -*- coding: utf-8 -*-
"""
日志工具 v3.1

Logger 层级 + 传播机制：
  task.review                            → tasks/review.log (INFO)
    ├── task.review.collector.xxx        → collectors/xxx.log (DEBUG, 冒泡)
    ├── task.review.core.analyzer        → core/analyzer.log (DEBUG, 冒泡)
    └── task.review.core.telegram        → core/telegram_bot.log (DEBUG, 冒泡)

用法：
  Service:
    set_current_task('review')
    logger = get_task_logger('review')

  采集器 __init__:
    self.logger = get_collector_logger('xxx')
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from system.config.settings import LOGS_DIR

_current_task: Optional[str] = None


def set_current_task(task_name: str):
    """设置当前任务上下文，子模块 logger 自动获得 task.{task}.collector/core.{name} 层级名"""
    global _current_task
    _current_task = task_name


def get_current_task() -> Optional[str]:
    return _current_task


def _build_name(name: str, category: str) -> str:
    """有任务上下文时返回 task.{task}.{category}.{name}，否则返回原名"""
    if _current_task:
        return f"task.{_current_task}.{category}.{name}"
    return name


def get_task_logger(task_name: str, trade_date: str = None) -> logging.Logger:
    """
    任务日志 → logs/{date}/tasks/{task_name}.log
    INFO 级别写入文件，冒泡关闭（任务 logger 是层级终点）
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    name = f"task.{task_name}"
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)  # logger 自身设 DEBUG，由 handler 控制级别
    logger.propagate = False  # 任务 logger 不往上冒泡

    if logger.handlers:
        return logger

    log_dir = Path(LOGS_DIR) / trade_date / "tasks"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / f"{task_name}.log")

    detailed_fmt = logging.Formatter(
        '%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh.setFormatter(detailed_fmt)
    logger.addHandler(fh)

    if os.isatty(1):  # 终端才输出，避免管道 tee 双写
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(detailed_fmt)
        logger.addHandler(ch)

    return logger


def _get_module_logger(
    name: str,
    category: str,
    trade_date: str = None,
) -> logging.Logger:
    """
    子模块日志（采集器/core）
    DEBUG 级别写入模块文件，INFO+ 冒泡到父 task logger
    """
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    full_name = _build_name(name, category)
    logger = logging.getLogger(full_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True  # 冒泡到父 task logger

    if logger.handlers:
        return logger

    # 文件路径：用最后的短名
    short_name = name.split(".")[-1]
    log_dir = Path(LOGS_DIR) / trade_date / category
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / f"{short_name}.log")

    detailed_fmt = logging.Formatter(
        '%(asctime)s.%(msecs)03d - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(detailed_fmt)
    logger.addHandler(fh)

    # 终端只打 WARNING+，避免子模块噪音淹没终端
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(ch)

    return logger


def get_collector_logger(collector_name: str, trade_date: str = None) -> logging.Logger:
    """
    采集器日志 → logs/{date}/collectors/{name}.log (DEBUG)
    有任务上下文时 INFO+ 自动冒泡到 task log
    """
    return _get_module_logger(collector_name, category="collectors", trade_date=trade_date)


def get_core_logger(name: str, trade_date: str = None) -> logging.Logger:
    """
    核心模块日志 → logs/{date}/core/{name}.log (DEBUG)
    有任务上下文时 INFO+ 自动冒泡到 task log
    """
    return _get_module_logger(name, category="core", trade_date=trade_date)


def get_system_logger(name: str) -> logging.Logger:
    """基础设施日志（保持兼容）"""
    return get_core_logger(name)
