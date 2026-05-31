"""
A 股大盘指数数据采集器

数据源：腾讯财经接口
不需要认证
"""

from datetime import datetime
from typing import Dict, List

import requests

from system.utils.logger import get_collector_logger

logger = get_collector_logger("main_index_collector")


# 大盘指数列表
MAIN_INDICES = [
    {"code": "sh000001", "name": "上证指数"},
    {"code": "sz399001", "name": "深证成指"},
    {"code": "sz399006", "name": "创业板指"},
    {"code": "sh000300", "name": "沪深 300"},
    {"code": "sh000016", "name": "上证 50"},
    {"code": "sh000852", "name": "中证 1000"},
    {"code": "sh000855", "name": "中证 2000"},
    {"code": "sh000905", "name": "中证 500"},
    {"code": "sh000688", "name": "科创 50"},
    {"code": "sz399637", "name": "国证微盘"},
]


class MainIndexCollector:
    """大盘指数采集器（腾讯接口）"""

    def __init__(self):
        self.indices = MAIN_INDICES
        self.session = requests.Session()
        self.session.trust_env = False  # 禁用代理
        logger.info("大盘指数采集器初始化完成")

    def fetch(self) -> List[Dict]:
        """
        获取大盘指数实时行情（腾讯接口）

        Returns:
            指数数据列表
        """
        logger.info(f"开始获取大盘指数（{len(self.indices)}个）...")

        results = []

        for idx_info in self.indices:
            code = idx_info["code"]
            name = idx_info["name"]

            try:
                # 腾讯财经接口
                url = f"http://qt.gtimg.cn/q={code}"
                resp = self.session.get(url, timeout=3)
                resp.encoding = "gbk"  # 腾讯接口返回 GBK 编码
                data = resp.text

                # 解析腾讯格式：v_sh000001="51~上证指数~000001~3310.34~..."
                parts = data.split("~")

                if len(parts) > 50:
                    index_data = {
                        "code": code,
                        "name": name,
                        "open": float(parts[5]) if len(parts) > 5 else 0,
                        "close": float(parts[3]) if len(parts) > 3 else 0,  # 当前价
                        "high": float(parts[33]) if len(parts) > 33 else 0,
                        "low": float(parts[34]) if len(parts) > 34 else 0,
                        "prev_close": float(parts[4]) if len(parts) > 4 else 0,
                        "change_amount": float(parts[31]) if len(parts) > 31 else 0,
                        "change_percent": float(parts[32]) if len(parts) > 32 else 0,
                        "volume": int(float(parts[6]) * 100)
                        if len(parts) > 6
                        else 0,  # 手→股
                        "turnover_amount": float(parts[37])
                        if len(parts) > 37
                        else 0,  # 成交额（万元）
                    }
                    results.append(index_data)
                    logger.info(
                        f"✅ {name}: {index_data['close']:.2f} ({index_data['change_percent']:+.2f}%)"
                    )
                else:
                    logger.warning(f"❌ {name}: 数据格式异常")

            except Exception as e:
                logger.error(f"❌ {name}: {e}")

        logger.info(f"大盘指数获取完成：{len(results)}/{len(self.indices)}个")
        return results

    def fetch_and_save(self, trade_date: str = None, trade_time: str = None) -> Dict:
        """
        标准接口：获取并保存大盘指数数据

        Args:
            trade_date: 交易日期（格式：YYYY-MM-DD，默认今天）
            trade_time: 交易时间（格式：HH:MM:SS，默认现在）

        Returns:
            {
                'success': True/False,
                'count': 实际采集数量,
                'total': 实际采集数量（A 类统计）,
                'data': 指数数据列表
            }
        """
        try:
            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")
            if trade_time is None:
                trade_time = datetime.now().strftime("%H:%M:%S")

            logger.info("=" * 60)
            logger.info(f"🍎 {self.__class__.__name__} 开始采集")
            logger.info("=" * 60)

            # 采集数据
            data = self.fetch()

            # 保存到数据库
            self.save_to_db(data, trade_date, trade_time)

            # 统计数量
            actual_count = len(data)

            result = {
                "success": True,
                "count": actual_count,
                "total": actual_count,  # A 类统计
                "data": data,
            }

            logger.info(f"✅ {self.__class__.__name__} 采集完成：{actual_count}个指数")
            logger.info("=" * 60)
            return result

        except Exception as e:
            logger.error(f"❌ {self.__class__.__name__} 采集异常：{e}")
            logger.info("=" * 60)
            return {"success": False, "count": 0, "total": 0, "data": []}

    def save_to_db(
        self, data: List[Dict], trade_date: str = None, trade_time: str = None
    ):
        """
        保存到数据库（覆盖当天数据）

        Args:
            data: 指数数据列表
            trade_date: 交易日期（默认今天）
            trade_time: 交易时间（默认现在）
        """
        if not data:
            logger.warning("数据为空，跳过保存")
            return

        try:
            import sqlite3

            from system.config.settings import DATABASE_PATH

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            if trade_time is None:
                trade_time = datetime.now().strftime("%H:%M:%S")

            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()

            # 删除当天数据（覆盖写入）
            cursor.execute(
                "DELETE FROM index_realtime_data WHERE trade_date = ?", (trade_date,)
            )
            conn.commit()
            logger.info(f"已删除 {trade_date} 的旧数据")

            # 插入新数据
            insert_count = 0
            for idx in data:
                try:
                    cursor.execute(
                        """
                        INSERT INTO index_realtime_data (
                            index_code, index_name, trade_date, trade_time,
                            open_price, high_price, low_price, close_price,
                            volume, turnover_amount, change_amount, change_percent,
                            prev_close, data_source, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            idx["code"],
                            idx["name"],
                            trade_date,
                            trade_time,
                            idx["open"],
                            idx["high"],
                            idx["low"],
                            idx["close"],
                            idx["volume"],
                            idx["turnover_amount"],
                            idx["change_amount"],
                            idx["change_percent"],
                            idx["prev_close"],
                            "tencent",
                            datetime.now(),
                        ),
                    )
                    insert_count += 1
                except Exception as e:
                    logger.warning(f"保存 {idx['name']} 失败：{e}")
                    continue

            conn.commit()
            conn.close()

            logger.info(f"✅ 保存到数据库成功：{insert_count}条数据")

        except Exception as e:
            logger.error(f"保存到数据库失败：{e}")


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("大盘指数数据采集器 - 测试运行")
    print("=" * 60)

    try:
        collector = MainIndexCollector()
        result = collector.fetch_and_save()

        if result.get("success"):
            print(f"\n✅ 采集成功：{result['count']}个指数")
        else:
            print("\n❌ 采集失败")

    except Exception as e:
        print(f"\n❌ 执行异常：{e}")
        import traceback

        traceback.print_exc()

    print("=" * 60)
