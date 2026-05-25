#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A 股公告采集器（测试版）

数据源：akshare stock_notice_report（东方财富公告大全）
写入表：future_announcements

字段映射：
  akshare → DB
  代码 → stock_code
  名称 → stock_name
  公告标题 → announcement_title
  公告类型 → announcement_type
  交易日期 → trade_date
  网址 → announcement_url
"""

import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

from system.config.settings import DATABASE_PATH
from system.config.akshare_config import get_akshare, get_random_user_agent, get_headers
from system.utils.logger import get_collector_logger


# 公告类型分类（用于重要性评分）
HIGH_IMPORTANCE = [
    '业绩预告', '业绩预告修正', '业绩快报', '业绩大幅上升', '业绩大幅下滑',
    '一季度报告全文', '半年度报告全文', '年度报告全文', '年度报告摘要',
    '季度报告全文', '季度报告摘要',
    '分配预案', '高送转',
    '资产重组', '重组进展', '重组预案', '重组草案', '重组实施',
    '收购报告书', '出售资产', '重大合同', '重大投资',
    '退市风险警示', '其他风险警示', '实施退市风险警示',
    '风险提示', '其他风险提示',
    '停复牌', '停牌', '复牌',
    '增发', '配股', '可转债', '公司债',
    '股权激励', '员工持股计划',
    '诉讼', '仲裁', '行政处罚',
    '股东减持', '股东增持', '大股东减持', '大股东增持',
]

MEDIUM_IMPORTANCE = [
    '股东大会决议公告', '董事会决议', '监事会决议',
    '关联交易', '对外投资', '对外担保',
    '变更公司名称', '变更经营范围', '变更注册资本',
    '变更注册地址', '变更董监高',
    '聘任审计机构', '解聘审计机构',
    '募集资金', '募集资金使用',
    '股份质押', '股份冻结',
    '回购', '回购进展', '回购完成',
]

LOW_IMPORTANCE = [
    '法律意见书', '独立董事意见', '专项说明',
    '独立董事述职报告', '内部控制报告',
    'ESG报告', '社会责任报告',
    '股东大会通知', '股东大会资料',
    '管理制度', '管理办法',
    '保荐核查意见', '持续督导',
]

# 值得存入 DB 的公告类型（HIGH_IMPORTANCE + 交易相关的 MEDIUM）
KEEP_TYPES = HIGH_IMPORTANCE + [
    '股份质押', '股份冻结', '回购', '回购完成', '回购进展',
    '变更董监高', '关联交易',
]

# 标题黑名单（即使类型匹配，标题含以下关键词也丢弃）
TITLE_BLACKLIST = [
    '法律意见书', '独立董事意见', '独立董事述职',
    '管理办法', '管理制度', '管理细则',
    '专项说明', '专项核查意见', '专项审计',
    '保荐核查', '保荐机构', '持续督导', '持续督导报告',
    'ESG报告', '社会责任报告',
    '内部控制', '内控评价', '内控审计',
    '股东大会通知', '股东大会资料', '股东大会决议公告',
    '募集资金存放', '募集资金使用',
    '公司章程', '议事规则',
    '更正公告', '补充公告', '说明公告',
    '投资者关系', '调研活动',
    '定期报告更正', '半年度报告更正', '年度报告更正',
]


def calc_importance(announcement_type: str) -> float:
    """
    根据公告类型计算重要性评分（1-10）

    - 10：退市/重大资产重组/业绩预告大幅变化
    - 8-9：定期报告/分配预案/股东增减持
    - 5-7：日常公告/决议/担保
    - 1-3：法律意见/制度文件/通知
    """
    if not announcement_type:
        return 1.0

    for kw in HIGH_IMPORTANCE:
        if kw in announcement_type:
            return 9.0

    for kw in MEDIUM_IMPORTANCE:
        if kw in announcement_type:
            return 6.0

    for kw in LOW_IMPORTANCE:
        if kw in announcement_type:
            return 2.0

    # 默认中等
    return 5.0


def should_keep(announcement_type: str, title: str) -> bool:
    """
    判断公告是否值得保留。

    两层过滤：
    1. 类型必须命中 KEEP_TYPES
    2. 标题不能命中 TITLE_BLACKLIST
    """
    if not announcement_type:
        return False

    # 第一层：类型白名单
    type_ok = any(kw in announcement_type for kw in KEEP_TYPES)
    if not type_ok:
        return False

    # 第二层：标题黑名单
    title_hit = any(kw in title for kw in TITLE_BLACKLIST) if title else False
    if title_hit:
        return False

    return True


class NoticeCollector:
    """A 股公告采集器"""

    def __init__(self):
        self.logger = get_collector_logger('notice_collector')
        self.trade_date = datetime.now().strftime('%Y%m%d')
        self.logger.info("公告采集器初始化完成")

    def fetch(self, trade_date: str = None) -> List[Dict]:
        """
        采集当日公告

        Args:
            trade_date: 日期 YYYY-MM-DD

        Returns:
            公告列表
        """
        if trade_date:
            self.trade_date = trade_date.replace('-', '')

        self.logger.info(f"开始采集公告（{self.trade_date}）...")

        # 使用统一入口获取 akshare
        ak = get_akshare()

        try:
            # 设置 akshare 请求头伪装
            import requests as req
            original_get = req.Session.get
            original_post = req.Session.post

            ua = get_random_user_agent()
            headers = {
                'User-Agent': ua,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'zh-CN,zh;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
            }

            # 临时 patch
            def patched_get(self, url, *args, **kwargs):
                kwargs.setdefault('headers', {}).update(headers)
                return original_get(self, url, *args, **kwargs)

            def patched_post(self, url, *args, **kwargs):
                kwargs.setdefault('headers', {}).update(headers)
                return original_post(self, url, *args, **kwargs)

            req.Session.get = patched_get
            req.Session.post = patched_post

            # 调用 akshare
            df = ak.stock_notice_report(symbol='全部', date=self.trade_date)

            # 恢复原始方法
            req.Session.get = original_get
            req.Session.post = original_post

            if df is None or df.empty:
                self.logger.warning(f"⚠️ {self.trade_date} 无公告数据")
                return []

            # 转换为字典列表 + 过滤
            results = []
            skipped = 0
            for _, row in df.iterrows():
                announcement_type = str(row.get('公告类型', '')).strip()
                announcement_title = str(row.get('公告标题', '')).strip()
                if not should_keep(announcement_type, announcement_title):
                    skipped += 1
                    continue
                results.append({
                    'stock_code': str(row.get('代码', '')).strip(),
                    'stock_name': str(row.get('名称', '')).strip(),
                    'announcement_title': announcement_title,
                    'announcement_type': announcement_type,
                    'trade_date': trade_date if trade_date else datetime.now().strftime('%Y-%m-%d'),
                    'announcement_url': str(row.get('网址', '')).strip(),
                    'importance_score': calc_importance(announcement_type),
                })

            self.logger.info(f"✅ 采集完成：{len(results)}条公告（过滤掉 {skipped} 条无用公告）")
            return results

        except Exception as e:
            self.logger.error(f"❌ 采集失败：{e}")
            return []

    def save_to_db(self, data: List[Dict], trade_date: str = None):
        """
        保存到数据库（去重插入，当天重复采集不覆盖已有数据）

        Args:
            data: 公告列表
            trade_date: 交易日期
        """
        if not data:
            self.logger.warning("数据为空，跳过保存")
            return

        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        conn = sqlite3.connect(str(DATABASE_PATH))
        cursor = conn.cursor()

        # 查询已存在的记录（按 stock_code + announcement_title + trade_date 去重）
        cursor.execute("""
            SELECT stock_code || '||' || announcement_title
            FROM future_announcements
            WHERE trade_date = ?
        """, (trade_date,))
        existing_keys = {row[0] for row in cursor.fetchall()}

        insert_count = 0
        skip_count = 0
        for item in data:
            key = item['stock_code'] + '||' + item['announcement_title']
            if key in existing_keys:
                skip_count += 1
                continue
            try:
                cursor.execute("""
                    INSERT INTO future_announcements (
                        stock_code, stock_name, announcement_title,
                        announcement_type, trade_date,
                        announcement_url, importance_score, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    item['stock_code'],
                    item['stock_name'],
                    item['announcement_title'],
                    item['announcement_type'],
                    item['trade_date'],
                    item['announcement_url'],
                    item['importance_score'],
                    datetime.now(),
                ))
                insert_count += 1
            except Exception as e:
                self.logger.warning(f"保存公告失败 {item['stock_code']}: {e}")

        conn.commit()
        conn.close()
        self.logger.info(f"✅ 保存到数据库：新增 {insert_count} 条，跳过 {skip_count} 条（已存在）")

    def fetch_and_save(self, trade_date: str = None) -> Dict:
        """
        标准接口：采集并保存

        Args:
            trade_date: 交易日期 YYYY-MM-DD

        Returns:
            {
                'success': True/False,
                'count': 实际采集数量,
                'total': 实际采集数量,
                'data': 公告列表
            }
        """
        self.logger.info("=" * 60)
        self.logger.info(f"🍎 NoticeCollector 开始采集")
        self.logger.info("=" * 60)

        try:
            data = self.fetch(trade_date)
            self.save_to_db(data, trade_date)

            result = {
                'success': True,
                'count': len(data),
                'total': len(data),
                'data': data,
            }

            self.logger.info(f"✅ NoticeCollector 采集完成：{len(data)}条")
            self.logger.info("=" * 60)
            return result

        except Exception as e:
            self.logger.error(f"❌ NoticeCollector 采集异常：{e}")
            self.logger.info("=" * 60)
            return {
                'success': False,
                'count': 0,
                'total': 0,
                'data': [],
            }


if __name__ == '__main__':
    print("=" * 60)
    print("公告采集器 - 测试运行")
    print("=" * 60)

    collector = NoticeCollector()
    result = collector.fetch_and_save()

    if result.get('success'):
        print(f"\n✅ 采集成功：{result['count']}条")
    else:
        print("\n❌ 采集失败")

    print("=" * 60)
