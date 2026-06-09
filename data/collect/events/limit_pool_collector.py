"""
涨跌停池数据采集器
功能:实时采集 A 股涨跌停数据,保存到数据库
"""

from datetime import datetime
from typing import Dict, List

from system.config.akshare_config import get_akshare
from system.utils.logger import get_collect_logger
from system.utils.stock_code_utils import strip_stock_code

logger = get_collect_logger("events")


def format_seal_time(time_str):
    """
    格式化封板时间
    "092500" → "09:25:00"
    "95227"  → "09:52:27"
    """
    if not time_str:
        return ""

    # 转为字符串并补齐 6 位
    time_str = str(time_str).zfill(6)

    # 格式化
    if len(time_str) == 6:
        return f"{time_str[0:2]}:{time_str[2:4]}:{time_str[4:6]}"
    else:
        return time_str


class LimitPoolCollector:
    """涨跌停池采集器"""

    def __init__(self):
        self.expected_count = 0  # 应采集数量(动态估算)
        logger.info("涨跌停池采集器初始化完成")

    def fetch_limit_up(self, trade_date: str = None) -> List[Dict]:
        """
        获取涨停池数据(东方财富接口)

        Args:
            trade_date: 交易日期(默认今天)

        Returns:
            涨停股列表
        """
        try:
            ak = get_akshare()

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y%m%d")
            elif "-" in trade_date:
                # 将 YYYY-MM-DD 转换为 YYYYMMDD
                trade_date = trade_date.replace("-", "")

            # 标准化日期格式(YYYY-MM-DD 用于数据库,YYYYMMDD 用于接口)
            trade_date_db = trade_date[:4] + "-" + trade_date[4:6] + "-" + trade_date[6:]

            logger.info(f"开始获取涨停池数据(日期:{trade_date})...")

            # 东方财富涨停池接口
            df = ak.stock_zt_pool_em(date=trade_date)

            if df is None or df.empty:
                logger.warning("涨停池数据为空(可能是非交易日或数据未更新)")
                return []

            result = []
            for _, row in df.iterrows():
                stock_data = {
                    "trade_date": trade_date_db,  # 使用标准格式
                    "stock_code": strip_stock_code(str(row.get("代码", ""))),
                    "stock_name": str(row.get("名称", "")),
                    "close_price": float(row.get("最新价", 0)),
                    "change_percent": float(row.get("涨跌幅", 0)),
                    "limit_price": float(row.get("涨停价", 0)),
                    "turnover_amount": float(row.get("成交额", 0)) if "成交额" in row else 0,  # 成交额(元)
                    "float_market_cap": float(row.get("流通市值", 0)) * 100000000 if "流通市值" in row else 0,  # 亿→万
                    "total_market_cap": float(row.get("总市值", 0)) * 100000000 if "总市值" in row else 0,
                    "turnover_rate": float(row.get("换手率", 0)),
                    "seal_amount": float(row.get("封板资金", 0)) if "封板资金" in row else 0,  # 封板资金(元)
                    "zt_stat": str(row.get("涨停统计", "0/0")) if "涨停统计" in row else "0/0",  # 涨停统计（近N天M板）
                    "first_seal_time": format_seal_time(row.get("首次封板时间", "")),
                    "last_seal_time": format_seal_time(row.get("最后封板时间", "")),
                    "open_count": int(row.get("炸板次数", 0)) if "炸板次数" in row else 0,  # 炸板次数
                    "consecutive_boards": int(row.get("连板数", 0)) if "连板数" in row else 0,  # 连板数
                    "industry": str(row.get("所属行业", "")),
                    "reason": str(row.get("涨停原因", "")),
                }
                result.append(stock_data)

            logger.info(f"✅ 涨停池获取成功:{len(result)}只")
            return result

        except Exception as e:
            logger.error(f"获取涨停池失败:{e}")
            return []

    def fetch_zhapa(self, trade_date: str = None) -> List[Dict]:
        """
        获取炸板池数据(东方财富接口)

        Args:
            trade_date: 交易日期(默认今天)

        Returns:
            炸板股列表
        """
        try:
            ak = get_akshare()

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y%m%d")
            elif "-" in trade_date:
                trade_date = trade_date.replace("-", "")

            trade_date_db = trade_date[:4] + "-" + trade_date[4:6] + "-" + trade_date[6:]

            logger.info(f"开始获取炸板池数据(日期:{trade_date})...")

            df = ak.stock_zt_pool_zbgc_em(date=trade_date)

            if df is None or df.empty:
                logger.info("炸板池数据为空")
                return []

            result = []
            for _, row in df.iterrows():
                stock_data = {
                    "trade_date": trade_date_db,
                    "stock_code": strip_stock_code(str(row.get("代码", ""))),
                    "stock_name": str(row.get("名称", "")),
                    "close_price": float(row.get("最新价", 0)),
                    "change_percent": float(row.get("涨跌幅", 0)),
                    "limit_price": float(row.get("涨停价", 0)),
                    "turnover_amount": float(row.get("成交额", 0)) * 10000,
                    "turnover_rate": float(row.get("换手率", 0)),
                    "first_seal_time": format_seal_time(row.get("首次封板时间", "")),
                    "open_count": int(row.get("炸板次数", 0)),
                    "consecutive_boards": int(str(row.get("涨停统计", "0/0")).split("/")[0]),
                    "industry": str(row.get("所属行业", "")),
                }
                result.append(stock_data)

            logger.info(f"✅ 炸板池获取成功:{len(result)}只")
            return result

        except Exception as e:
            logger.error(f"获取炸板池失败:{e}")
            return []

    def fetch_limit_down(self, trade_date: str = None) -> List[Dict]:
        """
        获取跌停池数据(东方财富接口)

        Args:
            trade_date: 交易日期(默认今天)

        Returns:
            跌停股列表
        """
        try:
            ak = get_akshare()

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y%m%d")
            elif "-" in trade_date:
                # 将 YYYY-MM-DD 转换为 YYYYMMDD
                trade_date = trade_date.replace("-", "")

            # 标准化日期格式
            trade_date_db = trade_date[:4] + "-" + trade_date[4:6] + "-" + trade_date[6:]

            logger.info(f"开始获取跌停池数据(日期:{trade_date})...")

            # 东方财富跌停池接口(正确名称)
            df = ak.stock_zt_pool_dtgc_em(date=trade_date)

            if df is None or df.empty:
                logger.warning("跌停池数据为空")
                return []

            result = []
            for _, row in df.iterrows():
                stock_data = {
                    "trade_date": trade_date_db,  # 使用标准格式
                    "stock_code": strip_stock_code(str(row.get("代码", ""))),
                    "stock_name": str(row.get("名称", "")),
                    "close_price": float(row.get("最新价", 0)),
                    "change_percent": float(row.get("涨跌幅", 0)),
                    "limit_down_price": float(row.get("跌停价", 0)) if "跌停价" in row else 0,
                    "turnover_amount": float(row.get("成交额", 0)) * 10000 if "成交额" in row else 0,
                    "float_market_cap": float(row.get("流通市值", 0)) * 100000000 if "流通市值" in row else 0,
                    "total_market_cap": float(row.get("总市值", 0)) * 100000000 if "总市值" in row else 0,
                    "turnover_rate": float(row.get("换手率", 0)),
                    "industry": str(row.get("所属行业", "")),
                }
                result.append(stock_data)

            logger.info(f"✅ 跌停池获取成功:{len(result)}只")
            return result

        except Exception as e:
            logger.error(f"获取跌停池失败：{e}")
            return []

    def save_to_db(
        self,
        limit_up_data: List[Dict],
        zhapa_data: List[Dict],
        limit_down_data: List[Dict],
        trade_date: str = None,
    ):
        """
        保存到数据库（覆盖当天数据）

        Args:
            limit_up_data: 涨停股列表
            limit_down_data: 跌停股列表
            zhapa_data: 炸板股列表
            trade_date: 交易日期（默认今天）
        """
        if not limit_up_data and not limit_down_data and not zhapa_data:
            logger.warning("数据为空，跳过保存")
            return

        try:
            import sqlite3

            from system.config.settings import DATABASE_PATH

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()

            # 删除当天数据(覆盖写入)
            cursor.execute("DELETE FROM limit_pool WHERE trade_date = ?", (trade_date,))
            conn.commit()
            logger.info(f"已删除 {trade_date} 的旧数据")

            # 保存涨停股到 limit_pool
            insert_count = 0
            for stock in limit_up_data:
                try:
                    cursor.execute(
                        """
                        INSERT INTO limit_pool (
                            trade_date, pool_type, stock_code, stock_name,
                            change_percent, price, turnover_amount,
                            float_market_cap, total_market_cap, turnover_rate,
                            seal_amount, zt_stat, first_seal_time, last_seal_time,
                            open_count, consecutive_boards, industry, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            stock["trade_date"],
                            "涨停",
                            stock["stock_code"],
                            stock["stock_name"],
                            stock["change_percent"],
                            stock["close_price"],
                            stock["turnover_amount"],
                            stock.get("float_market_cap", 0),
                            stock.get("total_market_cap", 0),
                            stock["turnover_rate"],
                            stock.get("seal_amount", 0),
                            stock.get("zt_stat", "0/0"),
                            stock["first_seal_time"],
                            stock.get("last_seal_time", stock["first_seal_time"]),
                            stock["open_count"],
                            stock["consecutive_boards"],
                            stock["industry"],
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    insert_count += 1
                except Exception as e:
                    logger.warning(f"保存涨停股 {stock['stock_name']} 失败:{e}")

            logger.info(f"✅ 涨停股保存成功:{insert_count}/{len(limit_up_data)}条")

            # 保存炸板股到 limit_pool
            insert_count = 0
            for stock in zhapa_data:
                try:
                    cursor.execute(
                        """
                        INSERT INTO limit_pool (
                            trade_date, pool_type, stock_code, stock_name,
                            change_percent, price, turnover_amount,
                            float_market_cap, total_market_cap, turnover_rate,
                            seal_amount, first_seal_time, last_seal_time,
                            open_count, consecutive_boards, industry, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            stock["trade_date"],
                            "炸板",
                            stock["stock_code"],
                            stock["stock_name"],
                            stock["change_percent"],
                            stock["close_price"],
                            stock["turnover_amount"],
                            stock.get("float_market_cap", 0),
                            stock.get("total_market_cap", 0),
                            stock["turnover_rate"],
                            stock.get("seal_amount", 0),
                            stock["first_seal_time"],
                            stock.get("last_seal_time", stock["first_seal_time"]),
                            stock["open_count"],
                            stock["consecutive_boards"],
                            stock["industry"],
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    insert_count += 1
                except Exception as e:
                    logger.warning(f"保存炸板股 {stock['stock_name']} 失败:{e}")

            logger.info(f"✅ 炸板股保存成功:{insert_count}/{len(zhapa_data)}条")

            # 保存跌停股到 limit_pool
            insert_count = 0
            for stock in limit_down_data:
                try:
                    cursor.execute(
                        """
                        INSERT INTO limit_pool (
                            trade_date, pool_type, stock_code, stock_name,
                            change_percent, price, turnover_amount,
                            float_market_cap, total_market_cap, turnover_rate,
                            seal_amount, first_seal_time, last_seal_time,
                            open_count, consecutive_boards, industry, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            stock["trade_date"],
                            "跌停",
                            stock["stock_code"],
                            stock["stock_name"],
                            stock["change_percent"],
                            stock["close_price"],
                            stock["turnover_amount"],
                            stock.get("float_market_cap", 0),
                            stock.get("total_market_cap", 0),
                            stock["turnover_rate"],
                            stock.get("seal_amount", 0),
                            stock.get("first_seal_time", ""),
                            stock.get("last_seal_time", ""),
                            stock.get("open_count", 0),
                            stock.get("consecutive_boards", 0),
                            stock["industry"],
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    insert_count += 1
                except Exception as e:
                    logger.warning(f"保存跌停股 {stock['stock_name']} 失败:{e}")

            logger.info(f"✅ 跌停股保存成功:{insert_count}/{len(limit_down_data)}条")

            conn.commit()
            logger.info("✅ 涨跌停池保存到数据库完成")

        except Exception as e:
            if "conn" in locals() and conn:
                conn.rollback()
            logger.error(f"保存到数据库失败:{e}")
        finally:
            if "conn" in locals() and conn:
                conn.close()

    def fetch_and_save(self, trade_date: str = None) -> Dict:
        """
        标准接口:获取并保存涨跌停数据

        Args:
            trade_date: 交易日期(格式:YYYY-MM-DD,默认今天)

        Returns:
            {
                'success': True/False,
                'count': 实际采集数量(涨停 + 跌停 + 连板),
                'total': 实际采集数量(A 类统计,只显示数量),
                'data': {
                    'limit_up': 涨停列表,
                    'limit_down': 跌停列表,
                    'ladder': 连板列表
                }
            }
        """
        try:
            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            logger.info("=" * 60)
            logger.info(f"🍎 {self.__class__.__name__} 开始采集")
            logger.info("=" * 60)

            # 采集数据
            limit_up_data = self.fetch_limit_up(trade_date)
            zhapa_data = self.fetch_zhapa(trade_date)  # 炸板池
            limit_down_data = self.fetch_limit_down(trade_date)

            # 保存到数据库
            self.save_to_db(limit_up_data, zhapa_data, limit_down_data, trade_date)

            # 统计数量(A 类:只显示数量)
            actual_count = len(limit_up_data) + len(zhapa_data) + len(limit_down_data)

            # 计算封板率
            sealed = len(limit_up_data)
            touched = len(limit_up_data) + len(zhapa_data)
            seal_rate = sealed / touched * 100 if touched > 0 else 0

            result = {
                "success": True,
                "count": actual_count,
                "total": actual_count,
                "data": {
                    "limit_up": limit_up_data,
                    "zhapa": zhapa_data,
                    "limit_down": limit_down_data,
                    "seal_rate": seal_rate,  # 封板率
                },
            }

            logger.info(
                f"✅ {self.__class__.__name__} 采集完成:涨停{sealed}只,炸板{len(zhapa_data)}只,跌停{len(limit_down_data)}只,封板率{seal_rate:.1f}%"
            )
            logger.info("=" * 60)
            return result

        except Exception as e:
            logger.error(f"❌ {self.__class__.__name__} 采集异常:{e}")
            logger.info("=" * 60)
            return {
                "success": False,
                "count": 0,
                "total": 0,
                "data": {"limit_up": [], "zhapa": [], "limit_down": []},
            }

    def print_summary(
        self,
        limit_up_data: List[Dict],
        limit_down_data: List[Dict],
        ladder_data: List[Dict],
    ):
        """打印数据统计"""
        print("\n【涨跌停池统计】")
        print(f"{'类型':<8} {'数量':>6}")
        print("-" * 16)
        print(f"{'涨停':<8} {len(limit_up_data):>6}")
        print(f"{'跌停':<8} {len(limit_down_data):>6}")
        print(f"{'连板':<8} {len(ladder_data):>6}")

        if ladder_data:
            print("\n【连板梯队】")
            print(f"{'股票名称':<12} {'连板数':>6} {'所属行业':<15}")
            print("-" * 35)
            for stock in sorted(ladder_data, key=lambda x: x.get("consecutive_boards", 0), reverse=True)[:10]:
                print(f"{stock['stock_name']:<12} {stock['consecutive_boards']:>6}板 {stock.get('industry', ''):<15}")


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("涨跌停数据采集器 - 测试运行")
    print("=" * 60)

    try:
        collector = LimitPoolCollector()
        result = collector.fetch_and_save()

        if result.get("success"):
            print(f"\n✅ 采集成功:{result['count']}条数据")
        else:
            print("\n❌ 采集失败")

    except Exception as e:
        print(f"\n❌ 执行异常:{e}")
        import traceback

        traceback.print_exc()

    print("=" * 60)
