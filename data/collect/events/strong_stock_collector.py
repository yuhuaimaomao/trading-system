"""
强势股数据采集器
功能：从东方财富获取强势股池数据，保存到数据库

数据源：东方财富强势股池接口
目标数据库：stock_market.db
目标表：strong_stock
"""

import sqlite3
from datetime import datetime
from typing import Dict, List

from system.config.akshare_config import get_akshare
from system.config.settings import DATABASE_PATH
from system.utils.logger import get_collect_logger


class StrongStockCollector:
    """强势股数据采集器"""

    def __init__(self, trade_date: str = None):
        """
        初始化采集器

        Args:
            trade_date: 交易日期（默认今天）
        """
        self.logger = get_collect_logger("events")
        if trade_date is None:
            self.trade_date = datetime.now().strftime("%Y-%m-%d")
        else:
            self.trade_date = trade_date

        # 数据库路径
        self.db_path = str(DATABASE_PATH)
        self.table_name = "strong_stock"

        self.logger.info("强势股采集器初始化完成")
        self.logger.info(f"交易日期：{self.trade_date}")
        self.logger.info(f"数据库表：{self.table_name}")

    def fetch_strong_stocks(self, trade_date: str = None) -> List[Dict]:
        """
        获取强势股池数据（东方财富接口）

        Args:
            trade_date: 交易日期（YYYY-MM-DD）

        Returns:
            强势股列表
        """
        try:
            if trade_date is None:
                trade_date = self.trade_date

            # 标准化日期格式（YYYYMMDD）
            trade_date_db = trade_date.replace("-", "")

            self.logger.info(f"开始获取强势股数据（日期：{trade_date}）...")

            # 东方财富强势股池接口
            df = get_akshare().stock_zt_pool_strong_em(date=trade_date_db)

            if df is None or df.empty:
                self.logger.warning("强势股数据为空")
                return []

            result = []
            for _, row in df.iterrows():
                # 解析涨停统计（格式："N/M"）
                # N = N 天内涨停次数，M = M 天内涨停次数
                # N/M 且 N=M 表示连续涨停，N≠M 表示 N 天内 M 次涨停
                limit_up_ratio = row.get("涨停统计", "0/0")
                if limit_up_ratio and "/" in limit_up_ratio:
                    parts = limit_up_ratio.split("/")
                    days = int(parts[0])  # N 天
                    count = int(parts[1])  # M 次
                else:
                    days = 0
                    count = 0

                # 判断是否今日涨停（N=M 且 N>0）
                is_limit_up = 1 if (days == count and days > 0) else 0

                stock_data = {
                    "trade_date": trade_date,
                    "stock_code": str(row.get("代码", "")),
                    "stock_name": str(row.get("名称", "")),
                    "limit_up_price": float(row.get("涨停价", 0)),
                    "limit_up_days": days,  # N 天
                    "limit_up_count": count,  # M 次
                    "limit_up_ratio": limit_up_ratio,
                    "is_limit_up": 1 if is_limit_up else 0,  # 是否今日涨停
                    "reason": str(row.get("入选理由", "")),
                }
                result.append(stock_data)

            self.logger.info(f"✅ 强势股获取成功：{len(result)}只")
            return result

        except Exception as e:
            self.logger.error(f"获取强势股失败：{e}")
            return []

    def save_to_db(self, data: list):
        """
        保存到数据库

        Args:
            data: 强势股列表
        """
        if not data or len(data) == 0:
            self.logger.warning("⚠️ 数据为空，跳过保存")
            return

        self.logger.info(f"保存 {len(data)} 条数据到数据库表 {self.table_name}...")

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            trade_date = self.trade_date

            # 删除当天旧数据
            cursor.execute(
                f"DELETE FROM {self.table_name} WHERE trade_date = ?", (trade_date,)
            )
            conn.commit()
            self.logger.info(f"已删除 {trade_date} 的旧数据")

            # 批量保存数据
            insert_count = 0
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for stock in data:
                try:
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO strong_stock (
                            trade_date, stock_code, stock_name,
                            limit_up_price, limit_up_days, limit_up_count,
                            limit_up_ratio, is_limit_up, reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            stock["trade_date"],
                            stock["stock_code"],
                            stock["stock_name"],
                            stock["limit_up_price"],
                            stock["limit_up_days"],
                            stock["limit_up_count"],
                            stock["limit_up_ratio"],
                            stock["is_limit_up"],
                            stock["reason"],
                            created_at,
                        ),
                    )
                    insert_count += 1
                except Exception as e:
                    self.logger.warning(f"保存 {stock['stock_name']} 失败：{e}")

            conn.commit()
            conn.close()

            self.logger.info(f"✅ 保存到数据库成功：{insert_count}/{len(data)}条")

        except Exception as e:
            self.logger.error(f"❌ 保存到数据库失败：{e}")

    def fetch_and_save(self) -> Dict:
        """【新方法】采集并保存（一次执行）"""
        self.logger.info("=" * 60)
        self.logger.info(f"🍎 {self.__class__.__name__} 开始采集")
        self.logger.info("=" * 60)

        try:
            data = self.fetch_strong_stocks()

            if not data or len(data) == 0:
                self.logger.error("❌ 采集失败：数据为空")
                return {"success": False, "count": 0, "total": 0, "data": []}

            # 保存数据
            self.save_to_db(data)

            result = {
                "success": True,
                "count": len(data),
                "total": len(data),
                "data": data,
            }

            self.logger.info(f"✅ {self.__class__.__name__} 采集完成：{len(data)}只")
            self.logger.info("=" * 60)
            return result

        except Exception as e:
            self.logger.error(f"❌ {self.__class__.__name__} 采集异常：{e}")
            self.logger.info("=" * 60)
            return {"success": False, "count": 0, "total": 0, "data": []}

    def collect(self):
        """完整采集流程"""
        self.logger.info("=" * 60)
        self.logger.info("开始采集强势股数据")
        self.logger.info("=" * 60)

        # 获取数据
        data = self.fetch_strong_stocks()

        # 保存到数据库
        self.save_to_db(data)

        self.logger.info("=" * 60)
        self.logger.info("强势股采集完成")
        self.logger.info("=" * 60)

        return data


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("强势股数据采集器 - 测试运行")
    print("=" * 60)

    try:
        collector = StrongStockCollector()
        result = collector.fetch_and_save()

        if result.get("success"):
            print(f"\n✅ 采集成功：{result['count']}条数据")
        else:
            print("\n❌ 采集失败")

    except Exception as e:
        print(f"\n❌ 执行异常：{e}")
        import traceback

        traceback.print_exc()

    print("=" * 60)
