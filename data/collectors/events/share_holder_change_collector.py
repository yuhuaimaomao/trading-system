# -*- coding: utf-8 -*-
"""
东方财富股东增减持采集器（代理版）

基于 ProxyBaseCollector 实现，支持动态 IP 切换
"""

import sqlite3
from datetime import datetime
from typing import List, Dict
from system.utils.stock_code_utils import normalize_stock_code

from data.collectors.proxy.proxy_base_collector import ProxyBaseCollector

class ShareHolderChangeCollector(ProxyBaseCollector):
    """股东增减持采集器"""
    
    # API 配置
    API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    PAGE_SIZE = 50
    MAX_PAGES = 1  # 只采 1 页（50 条），只要公告日期是当天或明天的
    
    # User-Agent 池
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    ]
    
    # 数据库配置
    TABLE_NAME = "share_holder_change"
    
    # 请求参数
    API_PARAMS = {
        "sortColumns": "END_DATE,SECURITY_CODE,EITIME",
        "sortTypes": "-1,-1,-1",
        "pageSize": "50",
        "pageNumber": "1",
        "reportName": "RPT_SHARE_HOLDER_INCREASE",
        "quoteColumns": "f2~01~SECURITY_CODE~NEWEST_PRICE,f3~01~SECURITY_CODE~CHANGE_RATE_QUOTES",
        "quoteType": "0",
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
    }
    
    # Referer
    REFERER_URL = "https://datacenter-web.eastmoney.com/"
    
    def __init__(self, trade_date: str = None, task_mgr = None):
        super().__init__(
            logger_name="ShareHolderChangeCollector",
            trade_date=trade_date,
            task_mgr=task_mgr
        )
        self.logger.info("✅ 股东增减持采集器初始化完成（代理版）")
    
    def fetch_all(self, max_retries=3):
        """
        重写父类的 fetch_all，使用带 filter 的自定义请求
        """
        return self.fetch(self.trade_date)
    
    def fetch(self, trade_date: str = None) -> List[Dict]:
        """
        获取股东增减持数据（带 filter 参数，只采集当天公告）
        
        Args:
            trade_date: 交易日期（默认今天）
        
        Returns:
            数据列表
        """
        if trade_date is None:
            trade_date = self.trade_date
        
        self.logger.info(f"开始获取股东增减持数据（日期：{trade_date}）")
        
        try:
            params = self.API_PARAMS.copy()
            params['filter'] = f'(NOTICE_DATE=\'{trade_date}\')'

            data = self._request_with_retry(
                self.API_URL, params,
                referer=self.REFERER_URL,
                desc="股东增减持",
            )

            if data is None:
                return []

            result = data.get('result', {}).get('data', [])
            self.logger.info(f"获取到 {len(result)} 条数据")

            return result
            
        except Exception as e:
            self.logger.error(f"❌ 获取失败：{e}")
            return []
    
    def _save_to_db(self, data: List[Dict]):
        """保存到数据库"""
        if not data or len(data) == 0:
            self.logger.warning("数据为空，跳过保存")
            return
        
        try:
            from system.config.settings import DATABASE_PATH
            
            trade_date = self.trade_date
            crawl_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()
            
            cursor.execute(f"DELETE FROM {self.TABLE_NAME} WHERE trade_date = ?", (trade_date,))
            conn.commit()
            
            insert_count = 0
            for item in data:
                try:
                    cursor.execute(f"""
                        INSERT INTO {self.TABLE_NAME} (
                            trade_date, stock_code, stock_name,
                            holder_name, change_type, change_direction, change_num, change_num_symbol,
                            change_rate, after_change_rate, end_date, notice_date,
                            newest_price, change_rate_quotes, crawl_time, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        trade_date,
                        str(item.get('SECURITY_CODE', '')),
                        str(item.get('SECURITY_NAME_ABBR', '')),
                        str(item.get('HOLDER_NAME', '')),
                        str(item.get('DIRECTION', '')),
                        str(item.get('DIRECTION', '')),
                        self._safe_float(item.get('CHANGE_NUM', 0)),
                        self._safe_float(item.get('CHANGE_NUM_SYMBOL', 0)),
                        self._safe_float(item.get('CHANGE_RATE', 0)),
                        self._safe_float(item.get('AFTER_CHANGE_RATE', 0)),
                        str(item.get('END_DATE', '')),
                        str(item.get('NOTICE_DATE', '')),
                        self._safe_float(item.get('NEWEST_PRICE', 0)),
                        self._safe_float(item.get('CHANGE_RATE_QUOTES', 0)),
                        crawl_time,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ))
                    insert_count += 1
                except Exception as e:
                    self.logger.debug(f"保存失败：{e}")
            
            conn.commit()
            conn.close()
            self.logger.info(f"✅ 保存成功：{insert_count}条")
            
        except Exception as e:
            self.logger.error(f"❌ 保存失败：{e}")
    
    def fetch_and_save(self) -> Dict:
        """【新方法】采集并保存（一次执行）"""
        self.logger.info("="*60)
        self.logger.info(f"🍎 {self.__class__.__name__} 开始采集")
        self.logger.info("="*60)
        
        try:
            # 调用基类的 fetch_all 方法
            data = self.fetch_all()
            
            if not data or len(data) == 0:
                self.logger.error("❌ 采集失败：数据为空")
                return {
                    'success': False,
                    'count': 0,
                    'total': 0,
                    'data': []
                }
            
            self._save_to_db(data)
            
            result = {
                'success': True,
                'count': len(data),
                'total': len(data),  # A 类统计
                'data': data
            }
            
            self.logger.info(f"✅ {self.__class__.__name__} 采集完成：{len(data)}条")
            self.logger.info("="*60)
            return result
            
        except Exception as e:
            self.logger.error(f"❌ {self.__class__.__name__} 采集异常：{e}")
            self.logger.info("="*60)
            return {
                'success': False,
                'count': 0,
                'total': 0,
                'data': []
            }


# ==================== 测试入口 ====================

if __name__ == '__main__':
    print('='*60)
    print('股东增减持采集器 - 测试运行')
    print('='*60)
    
    try:
        collector = ShareHolderChangeCollector()
        result = collector.fetch_and_save()
        
        if result.get('success'):
            print(f"\n✅ 采集成功：{result['count']}条数据")
        else:
            print("\n❌ 采集失败")
            
    except Exception as e:
        print(f"\n❌ 执行异常：{e}")
        import traceback
        traceback.print_exc()
    
    print('='*60)
