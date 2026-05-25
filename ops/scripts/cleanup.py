# -*- coding: utf-8 -*-
"""周清理脚本 — 清理 storage/ 下旧文件 + 清理数据库旧电报

独立运行:
  PYTHONPATH=. python scripts/cleanup.py
"""

import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from system.config.settings import DATABASE_PATH, STORAGE_PATH
from system.utils.logger import set_current_task, get_task_logger


def run():
    set_current_task('cleanup')
    logger = get_task_logger('cleanup')

    # 尝试使用交易日历，失败则回退到简单 timedelta（约 10 日历天 ≈ 7 交易日）
    try:
        from system.config.trading_calendar import get_previous_trading_day as _gptd
        cutoff_str = _gptd(offset=7)
        cutoff = datetime.strptime(cutoff_str, "%Y-%m-%d") if cutoff_str else datetime.now() - timedelta(days=10)
    except Exception:
        cutoff = datetime.now() - timedelta(days=10)

    storage_path = Path(STORAGE_PATH)
    logger.info(f"📅 清理截止日期：{cutoff.strftime('%Y-%m-%d')}（7个交易日前）")

    # 1. 删除日志日期目录
    logs_dir = storage_path / "logs"
    if logs_dir.exists():
        deleted = 0
        for entry in logs_dir.iterdir():
            if not entry.is_dir():
                continue
            match = re.match(r"^(\d{4}-\d{2}-\d{2})$", entry.name)
            if not match:
                continue
            dir_date = datetime.strptime(match.group(1), "%Y-%m-%d")
            if dir_date < cutoff:
                shutil.rmtree(entry)
                deleted += 1
        if deleted:
            logger.info(f"  🗑️  日志：删除 {deleted} 个日期目录")

    # 2. 删除缓存 JSON 文件（按文件名中的日期）
    cache_dir = storage_path / "cache"
    if cache_dir.exists():
        deleted = 0
        for f in cache_dir.iterdir():
            if not f.is_file() or f.suffix != ".json":
                continue
            match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            if not match:
                continue
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d")
            if file_date < cutoff:
                f.unlink()
                deleted += 1
        if deleted:
            logger.info(f"  🗑️  缓存：删除 {deleted} 个 JSON 文件")

    # 3. 删除 PDF 文件（按文件名中的日期 YYYYMMDD）
    pdf_dir = storage_path / "pdf"
    if pdf_dir.exists():
        deleted = 0
        for f in pdf_dir.iterdir():
            if not f.is_file() or f.suffix != ".pdf":
                continue
            match = re.match(r"^(\d{8})_", f.name)
            if not match:
                continue
            file_date = datetime.strptime(match.group(1), "%Y%m%d")
            if file_date < cutoff:
                f.unlink()
                deleted += 1
        if deleted:
            logger.info(f"  🗑️  PDF：删除 {deleted} 个文件")

    # 4. 删除报告 TXT 文件（按文件名中的日期）
    reports_dir = storage_path / "reports"
    if reports_dir.exists():
        deleted = 0
        for f in reports_dir.iterdir():
            if not f.is_file() or f.suffix != ".txt":
                continue
            match = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
            if not match:
                continue
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d")
            if file_date < cutoff:
                f.unlink()
                deleted += 1
        if deleted:
            logger.info(f"  🗑️  报告：删除 {deleted} 个文件")

    # 5. 清理 cls_telegraph 30 天前的数据
    db_cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    db_path = Path(DATABASE_PATH)
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("""
                SELECT COUNT(*), COUNT(DISTINCT trade_date)
                FROM cls_telegraph
                WHERE trade_date < ?
            """, (db_cutoff,))
            row = cursor.fetchone()
            del_count, del_dates = row[0], row[1]
            if del_count > 0:
                conn.execute("DELETE FROM cls_telegraph WHERE trade_date < ?", (db_cutoff,))
                conn.commit()
                logger.info(f"  🗑️  电报：删除 {del_count} 条（{del_dates}天）")
            else:
                logger.info(f"  📰 电报：无需清理（最新 {db_cutoff}）")
        finally:
            conn.close()
    else:
        logger.info(f"  ⚠️  数据库不存在：{db_path}")

    # 6. 统计剩余电报分布
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            cursor = conn.execute("""
                SELECT trade_date, COUNT(*) as cnt
                FROM cls_telegraph
                GROUP BY trade_date
                ORDER BY trade_date DESC
                LIMIT 10
            """)
            rows = cursor.fetchall()
            total = conn.execute("SELECT COUNT(*) FROM cls_telegraph").fetchone()[0]
            logger.info(f"📊 电报数据分布（共 {total} 条，最近 10 天）：")
            for date, cnt in rows:
                bar = "█" * (cnt // 10)
                logger.info(f"  {date}  {cnt:>4}条  {bar}")
        finally:
            conn.close()

    logger.info("✅ 清理完成")


if __name__ == "__main__":
    run()
