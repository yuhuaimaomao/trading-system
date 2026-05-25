#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
板块信息更新脚本

功能：
1. 扫描当日新增板块，自动标记 need_collect（基于事件关键词）
2. 保护已标记 need_collect=0 的板块
3. 处理板块消失的宽限期逻辑

执行时机：每日复盘数据采集完成后
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Set

from system.config.settings import DATABASE_PATH
from system.utils.logger import get_system_logger

logger = get_system_logger('update_sector_info')

# 事件关键词库（need_collect=0 的板块类型）
EVENT_KEYWORDS = [
    # 财报业绩类
    '年报', '季报', '中报', '三季报', '业绩', '财报', '预告', '预增', '预减', '预盈', '扭亏', '修正',
    # 交易特征类
    'ST', '微盘', '小盘', '中盘', '大盘', '超小盘', '超大盘', '低价', '高价', '破净', '破发', '超跌', '微利',
    # 时间特征类
    '昨日', '历史', '新高', '新低', '近期', '长期', '短期', '中期', '前期', '后期',
    # 指数类
    'HS300', '上证', '深证', '中证', '创业板', '科创', 'MSCI', '富时', '央视', '红利',
    # 资金类
    '基金', '社保', 'QFII', '证金', '汇金', '机构', '主力', '外资', '北向',
    # 地域类
    '上海', '深圳', '海南', '雄安', '粤港澳', '长江', '西部', '东北', '京津冀', '成渝', '滨海', '自贸区',
    # 特殊机制类
    'AB股', 'AH股', 'B股', 'GDR', 'CDR', '转债', '可转债', '融资融券', '股转', '摘帽', '退市',
    # 持股类
    '重仓', '持股', '持仓', '增持', '减持', '回购', '分红', '派息',
    # 其他
    '参股', '控股', '股权', '激励', '龙头', '权重', '龙头股', '白马', '蓝筹', '价值', '成长', '题材',
]


def is_event_sector(sector_name: str) -> bool:
    """
    判断是否为事件类板块（需要设置 need_collect=0）

    Args:
        sector_name: 板块名称

    Returns:
        是否为事件类板块
    """
    sector_name_lower = sector_name.lower()

    for keyword in EVENT_KEYWORDS:
        if keyword.lower() in sector_name_lower:
            return True

    return False


def update_sector_info_daily(trade_date: str = None):
    """
    更新板块信息表（每日执行）

    Args:
        trade_date: 交易日期，默认今天
    """
    if trade_date is None:
        trade_date = datetime.now().strftime('%Y-%m-%d')

    logger.info("=" * 60)
    logger.info(f"🔄 开始更新板块信息：{trade_date}")
    logger.info("=" * 60)

    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # 1. 获取当日板块数据
        logger.info("🔍 查询当日板块数据...")

        cursor.execute("""
            SELECT DISTINCT sector_code, sector_name, 'industry' as sector_type
            FROM sector_industry
            WHERE trade_date = ?
            UNION ALL
            SELECT DISTINCT sector_code, sector_name, 'concept' as sector_type
            FROM sector_concept
            WHERE trade_date = ?
        """, (trade_date, trade_date))

        current_sectors = cursor.fetchall()
        logger.info(f"📊 当日板块总数：{len(current_sectors)} 个")

        if len(current_sectors) == 0:
            logger.warning("⚠️ 当日无板块数据，跳过更新")
            return

        # 2. 获取现有 sector_info 数据
        logger.info("🔍 查询现有板块信息...")

        cursor.execute("""
            SELECT sector_code, need_collect
            FROM sector_info
        """)
        existing_sectors = {row['sector_code']: row['need_collect'] for row in cursor.fetchall()}

        logger.info(f"📋 现有板块总数：{len(existing_sectors)} 个")

        # 3. 处理新增板块
        logger.info("🆕 处理新增板块...")

        new_sectors_added = 0
        for sector in current_sectors:
            code = sector['sector_code']

            if code not in existing_sectors:
                # 新增板块
                name = sector['sector_name']
                sector_type = sector['sector_type']

                # 判断是否为事件类板块
                if is_event_sector(name):
                    need_collect = 0
                    reason = "事件类"
                else:
                    need_collect = 1
                    reason = "正常类"

                cursor.execute("""
                    INSERT INTO sector_info (
                        sector_code, sector_name, sector_type, claimed_count,
                        actual_count, count_match, created_at, need_collect
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    code, name, sector_type, 0, 0, 1,
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'), need_collect
                ))

                logger.info(f"  ✅ 新增：{code} {name} ({reason}) → need_collect={need_collect}")
                new_sectors_added += 1

        # 4. 统计更新结果
        logger.info(f"📈 新增板块：{new_sectors_added} 个")

        # 查询更新后的统计
        cursor.execute("SELECT COUNT(*) as count FROM sector_info WHERE need_collect = 0")
        event_sectors = cursor.fetchone()['count']

        cursor.execute("SELECT COUNT(*) as count FROM sector_info WHERE need_collect = 1")
        normal_sectors = cursor.fetchone()['count']

        logger.info(f"📊 更新后统计：")
        logger.info(f"  事件类板块：{event_sectors} 个 (need_collect=0)")
        logger.info(f"  正常类板块：{normal_sectors} 个 (need_collect=1)")

        conn.commit()
        logger.info("✅ 板块信息更新完成")

    except Exception as e:
        logger.error(f"❌ 板块信息更新失败：{e}", exc_info=True)
        conn.rollback()
        raise

    finally:
        conn.close()


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description='板块信息更新脚本')
    parser.add_argument('--date', type=str, help='交易日期 (YYYY-MM-DD)')

    args = parser.parse_args()

    trade_date = args.date or datetime.now().strftime('%Y-%m-%d')

    logger.info(f"开始执行板块信息更新：{trade_date}")

    update_sector_info_daily(trade_date)

    logger.info("✅ 板块信息更新执行完成")


if __name__ == '__main__':
    main()
