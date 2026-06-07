"""
昨日涨停表现分析处理器（完整版）

功能：
1. 获取昨日涨停股列表
2. 计算今日表现（溢价率、连板率等）
3. 写入数据库（yesterday_zt_performance 表）
4. 提供统计指标（用于情绪温度计）
"""

import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from system.utils.logger import get_collect_logger

logger = get_collect_logger("process")


class ZTPerformanceProcessor:
    """昨日涨停表现分析处理器"""

    # 涨停阈值配置
    LIMIT_UP_THRESHOLDS = {
        "688": 0.19,  # 科创板 20%
        "300": 0.19,  # 创业板 20%
        "301": 0.19,  # 创业板 20%
        "920": 0.29,  # 北交所 30%
        "900": 0.29,  # 北交所 30%
        "default": 0.095,  # 主板 10%
    }

    @classmethod
    def run(cls, trade_date: str = None) -> Dict:
        """
        一键执行：获取上一交易日 → 计算表现 → 保存到数据库 → 返回统计

        Args:
            trade_date: 交易日期，默认今天

        Returns:
            统计指标字典（空字典表示无数据）
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        from system.config.trading_calendar import get_previous_trading_day

        yesterday = get_previous_trading_day(trade_date)
        logger.info(f"计算昨日({yesterday})涨停今日({trade_date})表现...")

        processor = cls()
        performance = processor.calculate_performance(yesterday, trade_date)

        if not performance:
            logger.warning("昨日涨停表现无数据")
            return {}

        processor.save_to_database(performance, trade_date)
        stats = processor.get_statistics(performance)
        logger.info(
            f"昨日涨停表现：{stats['total']}只，平均溢价{stats['avg_premium']}%，"
            f"胜率{stats['positive_rate']}%，连板率{stats['limit_up_rate']}%"
        )
        return stats

    def __init__(self):
        from system.config.settings import DATABASE_PATH

        self.db_path = DATABASE_PATH

    def get_yesterday_limit_up(self, trade_date: str = None) -> List[Dict]:
        """
        获取昨日涨停股列表（从 limit_pool 表读取）

        Args:
            trade_date: 交易日期（默认昨天）

        Returns:
            涨停股列表
        """
        try:
            import sqlite3

            if trade_date is None:
                # 获取上一个交易日（跳过周末）
                trade_date = self._get_prev_trade_date()

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    stock_code, stock_name, price, change_percent,
                    turnover_amount, float_market_cap, total_market_cap,
                    turnover_rate, first_seal_time, last_seal_time,
                    open_count, consecutive_boards, industry
                FROM limit_pool
                WHERE trade_date = ? AND (pool_type = '涨停' OR pool_type = 'zt')
                ORDER BY change_percent DESC
            """,
                (trade_date,),
            )

            result = [dict(row) for row in cursor.fetchall()]
            conn.close()

            logger.info(f"获取昨日涨停股成功：{len(result)}只")
            return result

        except Exception as e:
            logger.error(f"获取昨日涨停股失败：{e}")
            return []

    def get_today_data(
        self, stock_codes: List[str], trade_date: str = None
    ) -> Dict[str, Dict]:
        """
        获取股票今日表现（从 stock_basic 表读取）

        Args:
            stock_codes: 股票代码列表
            trade_date: 交易日期（默认今天）

        Returns:
            {stock_code: {open, close, change_percent, ...}}
        """
        try:
            import sqlite3

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            if not stock_codes:
                return {}

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 从 stock_basic 读取今日数据
            placeholders = ",".join("?" * len(stock_codes))
            cursor.execute(
                f"""
                SELECT
                    stock_code, stock_name,
                    open, price, high, low,
                    change_pct, change_amount,
                    volume, turnover, turnover_rate,
                    amplitude
                FROM stock_basic
                WHERE trade_date = ? AND stock_code IN ({placeholders})
            """,
                [trade_date] + stock_codes,
            )

            today_data = {}
            for row in cursor.fetchall():
                today_data[row["stock_code"]] = dict(row)

            conn.close()

            logger.info(f"获取今日数据成功：{len(today_data)}/{len(stock_codes)}只")
            return today_data

        except Exception as e:
            logger.error(f"获取今日数据失败：{e}")
            return {}

    def is_limit_up(self, stock_code: str, change_percent: float) -> bool:
        """
        判断是否涨停（根据股票代码判断阈值）

        Args:
            stock_code: 股票代码
            change_percent: 涨跌幅（%）

        Returns:
            是否涨停
        """
        # 获取涨停阈值
        threshold = self.LIMIT_UP_THRESHOLDS.get("default")
        for prefix, thresh in self.LIMIT_UP_THRESHOLDS.items():
            if stock_code.startswith(prefix):
                threshold = thresh
                break

        return change_percent >= threshold * 100  # 数据库存的是百分比

    def calculate_premium_rate(
        self, yesterday_close: float, today_open: float
    ) -> float:
        """
        计算溢价率（开盘价 - 昨日收盘价）/ 昨日收盘价

        Args:
            yesterday_close: 昨日收盘价
            today_open: 今日开盘价

        Returns:
            溢价率（%）
        """
        if yesterday_close <= 0:
            return 0.0
        return (today_open - yesterday_close) / yesterday_close * 100

    def calculate_performance(
        self, yesterday_date: str = None, today_date: str = None
    ) -> List[Dict]:
        """
        计算昨日涨停今日表现

        Args:
            yesterday_date: 昨日日期
            today_date: 今日日期

        Returns:
            表现数据列表
        """
        try:
            # 获取昨日涨停股
            yesterday_zt = self.get_yesterday_limit_up(yesterday_date)

            if not yesterday_zt:
                logger.warning("昨日涨停股为空")
                return []

            # 获取股票代码列表
            stock_codes = [zt["stock_code"] for zt in yesterday_zt]

            # 获取今日数据
            today_data = self.get_today_data(stock_codes, today_date)

            # 计算表现指标
            performance_list = []
            for zt in yesterday_zt:
                stock_code = zt["stock_code"]
                today = today_data.get(stock_code, {})

                # 基础数据
                yesterday_close = zt["price"]
                yesterday_change = zt["change_percent"]
                yesterday_seal_time = zt.get("first_seal_time", "")
                yesterday_board_count = zt.get("consecutive_boards", 1)

                # 今日数据
                today_open = today.get("open", 0)
                today_close = today.get("price", 0)
                today_high = today.get("high", 0)
                today_low = today.get("low", 0)
                today_change = today.get("change_pct", 0)  # 字段名是 change_pct
                today_turnover = today.get("turnover", 0)
                today_turnover_rate = today.get("turnover_rate", 0)
                today_amplitude = today.get("amplitude", 0)

                # 计算溢价率（使用开盘价）
                premium_rate = self.calculate_premium_rate(yesterday_close, today_open)

                # 判断是否连板
                is_limit_up = self.is_limit_up(stock_code, today_change)
                consecutive_boards = yesterday_board_count + (1 if is_limit_up else 0)

                # 判断状态
                status = self._get_status_label(premium_rate, is_limit_up, today_change)

                perf = {
                    "stock_code": stock_code,
                    "stock_name": zt["stock_name"],
                    "yesterday_close": yesterday_close,
                    "yesterday_change": yesterday_change,
                    "yesterday_seal_time": str(yesterday_seal_time)
                    if yesterday_seal_time
                    else "",
                    "yesterday_board_count": yesterday_board_count,
                    "today_open": today_open,
                    "today_close": today_close,
                    "today_high": today_high,
                    "today_low": today_low,
                    "today_change": today_change,
                    "premium_rate": round(premium_rate, 2),  # 溢价率
                    "is_limit_up": is_limit_up,
                    "consecutive_boards": consecutive_boards,
                    "turnover_amount": today_turnover,
                    "turnover_rate": round(today_turnover_rate, 2),
                    "amplitude": round(today_amplitude, 2),
                    "industry": zt.get("industry", ""),
                    "status": status,
                }
                performance_list.append(perf)

            # 按溢价率排序
            performance_list.sort(key=lambda x: x["premium_rate"], reverse=True)

            logger.info(f"计算表现完成：{len(performance_list)}只")
            return performance_list

        except Exception as e:
            logger.error(f"计算表现失败：{e}")
            import traceback

            traceback.print_exc()
            return []

    def format_report(self, performance_list: List[Dict]) -> str:
        """
        将昨日涨停表现格式化为文本

        Args:
            performance_list: calculate_performance() 的返回结果

        Returns:
            格式化后的文本
        """
        if not performance_list:
            return "昨日涨停表现：无数据"

        stats = self.get_statistics(performance_list)
        lines = []
        lines.append(f"昨日涨停今日表现：{stats.get('total', 0)}只")
        lines.append(f"平均溢价：{stats.get('avg_premium', 0):.2f}%")
        lines.append(f"胜率：{stats.get('positive_rate', 0):.1f}%")
        lines.append("")

        # 表现最好的 5 只
        top5 = performance_list[:5]
        if top5:
            lines.append("表现最好的 5 只:")
            for i, perf in enumerate(top5, 1):
                lines.append(
                    f"  {i}. {perf['stock_name']}({perf['stock_code']}) "
                    f"溢价{perf['premium_rate']:+.2f}%  涨跌幅{perf['today_change']:+.2f}%"
                )

        # 表现最差的 5 只
        bottom5 = performance_list[-5:]
        if bottom5:
            lines.append("\n表现最差的 5 只:")
            for i, perf in enumerate(bottom5, 1):
                lines.append(
                    f"  {i}. {perf['stock_name']}({perf['stock_code']}) "
                    f"溢价{perf['premium_rate']:+.2f}%  涨跌幅{perf['today_change']:+.2f}%"
                )

        return "\n".join(lines)

    def save_to_database(self, performance_list: List[Dict], trade_date: str = None):
        """
        保存到数据库（yesterday_zt_performance 表）

        Args:
            performance_list: 表现数据列表
            trade_date: 交易日期（默认今天）
        """
        try:
            if not performance_list:
                logger.warning("表现数据为空，跳过保存")
                return

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            import sqlite3

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 检查是否已存在
            cursor.execute(
                "SELECT COUNT(*) FROM yesterday_zt_performance WHERE trade_date = ?",
                (trade_date,),
            )
            count = cursor.fetchone()[0]

            if count > 0:
                logger.info(f"{trade_date} 数据已存在（{count}条），删除旧数据")
                cursor.execute(
                    "DELETE FROM yesterday_zt_performance WHERE trade_date = ?",
                    (trade_date,),
                )

            # 插入新数据
            insert_sql = """
                INSERT INTO yesterday_zt_performance (
                    trade_date, stock_code, stock_name,
                    change_percent, price, limit_up_price,
                    turnover_amount, float_market_cap, total_market_cap,
                    turnover_rate, speed, amplitude,
                    yesterday_seal_time, yesterday_board_count,
                    zt_statistics, industry
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            for perf in performance_list:
                # 映射字段
                limit_up_price = (
                    perf["yesterday_close"] * 1.2
                    if perf["stock_code"].startswith(("688", "300", "301"))
                    else perf["yesterday_close"] * 1.1
                )
                speed = (
                    perf["turnover_rate"] / 10 if perf["turnover_rate"] > 0 else 0
                )  # 封板速度（估算）
                zt_statistics = f"{perf['premium_rate']:.2f}%"

                cursor.execute(
                    insert_sql,
                    (
                        trade_date,
                        perf["stock_code"],
                        perf["stock_name"],
                        perf["today_change"],
                        perf["today_close"],
                        round(limit_up_price, 2),
                        perf["turnover_amount"],
                        0,  # float_market_cap（暂不填）
                        0,  # total_market_cap（暂不填）
                        perf["turnover_rate"],
                        round(speed, 2),
                        perf["amplitude"],
                        perf["yesterday_seal_time"],
                        perf["yesterday_board_count"],
                        zt_statistics,
                        perf["industry"],
                    ),
                )

            conn.commit()
            conn.close()

            logger.info(f"保存成功：{len(performance_list)}条记录")

        except Exception as e:
            logger.error(f"保存失败：{e}")
            import traceback

            traceback.print_exc()

    def get_statistics(self, performance_list: List[Dict]) -> Dict:
        """
        统计昨日涨停表现

        Args:
            performance_list: 表现数据列表

        Returns:
            统计指标
        """
        if not performance_list:
            return {}

        total = len(performance_list)
        limit_up_count = sum(1 for p in performance_list if p["is_limit_up"])
        positive_count = sum(1 for p in performance_list if p["premium_rate"] > 0)
        negative_count = sum(1 for p in performance_list if p["premium_rate"] < 0)

        avg_premium = sum(p["premium_rate"] for p in performance_list) / total
        max_premium = max(p["premium_rate"] for p in performance_list)
        min_premium = min(p["premium_rate"] for p in performance_list)

        # 连板统计
        consecutive_2 = sum(1 for p in performance_list if p["consecutive_boards"] >= 2)
        consecutive_3 = sum(1 for p in performance_list if p["consecutive_boards"] >= 3)
        consecutive_5 = sum(1 for p in performance_list if p["consecutive_boards"] >= 5)

        # 涨停股详情
        highest_stock = max(performance_list, key=lambda x: x["premium_rate"])
        lowest_stock = min(performance_list, key=lambda x: x["premium_rate"])

        stats = {
            "total": total,
            "limit_up_count": limit_up_count,
            "limit_up_rate": round(limit_up_count / total * 100, 2),
            "positive_count": positive_count,
            "positive_rate": round(positive_count / total * 100, 2),
            "negative_count": negative_count,
            "negative_rate": round(negative_count / total * 100, 2),
            "avg_premium": round(avg_premium, 2),
            "max_premium": round(max_premium, 2),
            "min_premium": round(min_premium, 2),
            "consecutive_2": consecutive_2,
            "consecutive_3": consecutive_3,
            "consecutive_5": consecutive_5,
            "highest_stock": highest_stock["stock_name"],
            "highest_premium": highest_stock["premium_rate"],
            "lowest_stock": lowest_stock["stock_name"],
            "lowest_premium": lowest_stock["premium_rate"],
        }

        return stats

    def get_emotion_metrics(self, trade_date: str = None) -> Dict:
        """
        获取情绪指标（用于情绪温度计）

        Args:
            trade_date: 交易日期

        Returns:
            情绪指标字典
        """
        try:
            import sqlite3

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            cursor.execute(
                """
                SELECT
                    COUNT(*) as total,
                    AVG(change_percent) as avg_change,
                    SUM(CASE WHEN change_percent > 0 THEN 1 ELSE 0 END) as positive_count,
                    SUM(CASE WHEN change_percent >= 9.5 THEN 1 ELSE 0 END) as limit_up_count
                FROM yesterday_zt_performance
                WHERE trade_date = ?
            """,
                (trade_date,),
            )

            row = cursor.fetchone()
            conn.close()

            if not row or row["total"] == 0:
                return {}

            total = row["total"]
            avg_change = row["avg_change"] or 0
            positive_rate = (row["positive_count"] or 0) / total * 100
            limit_up_rate = (row["limit_up_count"] or 0) / total * 100

            return {
                "total": total,
                "avg_premium": round(avg_change, 2),
                "positive_rate": round(positive_rate, 2),
                "limit_up_rate": round(limit_up_rate, 2),
                "limit_up_count": row["limit_up_count"] or 0,
            }

        except Exception as e:
            logger.error(f"获取情绪指标失败：{e}")
            return {}

    def print_report(self, performance_list: List[Dict], stats: Dict):
        """打印表现报告"""
        if not performance_list:
            print("无数据")
            return

        print("\n" + "=" * 80)
        print("昨日涨停今日表现报告")
        print("=" * 80)

        print("\n【统计概览】")
        print(f"  总数：{stats['total']}只")
        print(f"  涨停：{stats['limit_up_count']}只 ({stats['limit_up_rate']}%)")
        print(f"  上涨：{stats['positive_count']}只 ({stats['positive_rate']}%)")
        print(f"  下跌：{stats['negative_count']}只 ({stats['negative_rate']}%)")
        print(f"  平均溢价：{stats['avg_premium']}%")
        print(f"  最高溢价：{stats['max_premium']}% ({stats['highest_stock']})")
        print(f"  最低溢价：{stats['min_premium']}% ({stats['lowest_stock']})")

        print("\n【连板统计】")
        print(f"  2 连板及以上：{stats['consecutive_2']}只")
        print(f"  3 连板及以上：{stats['consecutive_3']}只")
        print(f"  5 连板及以上：{stats['consecutive_5']}只")

        print("\n【溢价率 TOP10】")
        print(f"{'股票名称':<12} {'昨收':>8} {'今开':>8} {'溢价率':>10} {'状态':>8}")
        print("-" * 60)
        for stock in performance_list[:10]:
            print(
                f"{stock['stock_name']:<12} {stock['yesterday_close']:>8.2f} {stock['today_open']:>8.2f} {stock['premium_rate']:>9.2f}% {stock['status']:>8}"
            )

        print("\n【跌幅 TOP10】")
        print(f"{'股票名称':<12} {'昨收':>8} {'今开':>8} {'溢价率':>10} {'状态':>8}")
        print("-" * 60)
        for stock in performance_list[-10:]:
            print(
                f"{stock['stock_name']:<12} {stock['yesterday_close']:>8.2f} {stock['today_open']:>8.2f} {stock['premium_rate']:>9.2f}% {stock['status']:>8}"
            )

    def _get_status_label(
        self, premium_rate: float, is_limit_up: bool, today_change: float
    ) -> str:
        """获取状态标签"""
        if is_limit_up:
            return "涨停"
        elif premium_rate >= 5:
            return "大涨"
        elif premium_rate >= 0:
            return "上涨"
        elif premium_rate >= -5:
            return "下跌"
        elif today_change <= -9.5:
            return "跌停"
        else:
            return "大跌"

    def _get_prev_trade_date(self) -> str:
        """获取上一个交易日"""
        today = datetime.now()

        # 往前推 1-3 天（跳过周末）
        for i in range(1, 4):
            date = today - timedelta(days=i)
            if date.weekday() < 5:  # 周一到周五
                return date.strftime("%Y-%m-%d")

        return today.strftime("%Y-%m-%d")

    def _is_trading_day(self, date_str: str) -> bool:
        """判断是否为交易日"""
        try:
            from system.config.trading_calendar import TradingCalendar

            calendar = TradingCalendar()
            return calendar.is_trading_day(date_str)
        except:
            # 如果没有交易日历，简单判断周末
            date = datetime.strptime(date_str, "%Y-%m-%d")
            return date.weekday() < 5


# ========== 测试入口 ==========

if __name__ == "__main__":
    print("=" * 80)
    print("昨日涨停表现分析服务（完整版）")
    print("=" * 80)

    try:
        processor = ZTPerformanceProcessor()

        # 计算表现
        print("\n【计算昨日涨停今日表现】")
        performance = processor.calculate_performance()

        if performance:
            # 获取统计
            stats = processor.get_statistics(performance)

            # 打印报告
            processor.print_report(performance, stats)

            # 保存到数据库
            print("\n【保存到数据库】")
            processor.save_to_database(performance)

            # 获取情绪指标
            print("\n【情绪指标】")
            emotion = processor.get_emotion_metrics()
            if emotion:
                print(f"  总数：{emotion['total']}只")
                print(f"  平均溢价：{emotion['avg_premium']}%")
                print(f"  胜率：{emotion['positive_rate']}%")
                print(f"  连板率：{emotion['limit_up_rate']}%")

            print("\n✅ 完成！")
        else:
            print("\n❌ 无数据（可能是非交易日或数据未更新）")

    except Exception as e:
        logger.error(f"执行异常：{e}")
        import traceback

        traceback.print_exc()
