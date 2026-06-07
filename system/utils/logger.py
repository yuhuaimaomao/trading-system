"""
日志工具 v4.0

按业务线分目录，按功能组分文件：

  tasks/       — 任务入口 (INFO, 不冒泡)
  collect/     — 数据采集 (DEBUG, 冒泡到 task)
  strategy/    — 策略+选股 (DEBUG, 冒泡到 task)
  trade/       — 盯盘+交易 (DEBUG, 冒泡到 task)
  review/      — 复盘 (DEBUG, 冒泡到 task)
  audit/       — 审计 (DEBUG, 冒泡到 task)
  message/     — 消息通信 (DEBUG, 冒泡到 task)
  system/      — 基础设施 (DEBUG, 冒泡到 task)

用法：
  # 任务入口
  set_current_task('monitor')
  logger = get_task_logger('monitor')

  # 功能组（同一组共用，日志内[文件名:行号]区分来源）
  logger = get_trade_logger('decision')     # → trade/decision.log
  logger = get_collect_logger('market')     # → collect/market.log
  logger = get_strategy_logger('screening') # → strategy/screening.log
"""

import logging
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Optional

from system.config.settings import LOGS_DIR

_current_task: ContextVar[Optional[str]] = ContextVar("current_task", default=None)


def set_current_task(task_name: str):
    _current_task.set(task_name)


def get_current_task() -> Optional[str]:
    return _current_task.get()


def _build_name(name: str, category: str) -> str:
    task = _current_task.get()
    if task:
        return f"task.{task}.{category}.{name}"
    return name


def get_task_logger(task_name: str, trade_date: str = None) -> logging.Logger:
    """任务日志 → logs/{date}/tasks/{task_name}.log (INFO, 不冒泡)"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    name = f"task.{task_name}"
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger

    log_dir = Path(LOGS_DIR) / trade_date / "tasks"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / f"{task_name}.log")

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


def _get_module_logger(
    name: str,
    category: str,
    trade_date: str = None,
) -> logging.Logger:
    """功能组日志 → logs/{date}/{category}/{name}.log (DEBUG, INFO+ 冒泡到 task)"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    full_name = _build_name(name, category)
    logger = logging.getLogger(full_name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True

    if logger.handlers:
        return logger

    log_dir = Path(LOGS_DIR) / trade_date / category
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / f"{name}.log")

    file_fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(file_fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S"
        )
    )
    logger.addHandler(ch)

    return logger


# ── 业务线 logger ──────────────────────────────────────────


def get_collect_logger(name: str, trade_date: str = None) -> logging.Logger:
    """数据采集 → logs/{date}/collect/{name}.log"""
    return _get_module_logger(name, category="collect", trade_date=trade_date)


def get_strategy_logger(name: str, trade_date: str = None) -> logging.Logger:
    """策略+选股 → logs/{date}/strategy/{name}.log"""
    return _get_module_logger(name, category="strategy", trade_date=trade_date)


def get_trade_logger(name: str, trade_date: str = None) -> logging.Logger:
    """盯盘+交易 → logs/{date}/trade/{name}.log"""
    return _get_module_logger(name, category="trade", trade_date=trade_date)


def get_review_logger(name: str, trade_date: str = None) -> logging.Logger:
    """复盘 → logs/{date}/review/{name}.log"""
    return _get_module_logger(name, category="review", trade_date=trade_date)


def get_audit_logger(name: str, trade_date: str = None) -> logging.Logger:
    """审计 → logs/{date}/audit/{name}.log"""
    return _get_module_logger(name, category="audit", trade_date=trade_date)


def get_message_logger(name: str, trade_date: str = None) -> logging.Logger:
    """消息通信 → logs/{date}/message/{name}.log"""
    return _get_module_logger(name, category="message", trade_date=trade_date)


def get_system_logger(name: str, trade_date: str = None) -> logging.Logger:
    """基础设施 → logs/{date}/system/{name}.log"""
    return _get_module_logger(name, category="system", trade_date=trade_date)


# ── 向后兼容别名 ─────────────────────────────────────────────

get_collector_logger = get_collect_logger
get_core_logger = get_system_logger


def setup_root_logger(task_name: str, trade_date: str = None):
    """root logger → tasks/{task_name}.log（必须在 stdout 重定向前调用）"""
    if trade_date is None:
        trade_date = datetime.now().strftime("%Y-%m-%d")

    log_dir = Path(LOGS_DIR) / trade_date / "tasks"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / f"{task_name}.log")

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d - %(levelname)s - [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and h.baseFilename == log_file:
            return

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    for pkg in ("trade", "data", "system"):
        lg = logging.getLogger(pkg)
        lg.setLevel(logging.DEBUG)
        lg.propagate = True

    for h in root.handlers:
        if isinstance(h, logging.StreamHandler):
            h.setLevel(logging.WARNING)
