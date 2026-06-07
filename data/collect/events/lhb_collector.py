"""
龙虎榜数据采集器
功能：从东方财富获取龙虎榜数据，去重后保存到数据库

数据源：AkShare（东方财富接口）
目标数据库：stock_market.db
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from system.config.akshare_config import get_akshare

# 使用项目的日志系统（采集器日志）
from system.utils.logger import get_collect_logger


class LHBCollector:
    """龙虎榜数据采集器（从东方财富获取并入库）"""

    def __init__(self, db_path: str = None):
        """
        Args:
            db_path: 数据库路径（默认：~/quant-system/storage/stock_market.db）
        """
        self.logger = get_collect_logger("events")
        if db_path is None:
            from system.config.settings import DATABASE_PATH

            db_path = str(DATABASE_PATH)
        self.db_path = db_path
        self.logger.info(f"龙虎榜采集器初始化完成，数据库：{self.db_path}")

    def fetch_and_save(
        self, date: Optional[str] = None, trade_date: Optional[str] = None
    ) -> Dict:
        """
        获取龙虎榜数据并保存到数据库

        Args:
            date: 日期字符串（YYYYMMDD），默认今天
            trade_date: 兼容参数，同 date

        Returns:
            采集结果 {success: bool, count: int, message: str}
        """
        try:
            # 1. 确定日期（支持多种传入方式）
            if trade_date:
                date = trade_date

            if date is None:
                # 默认今天
                date = datetime.now().strftime("%Y%m%d")
            elif date == "yesterday":
                # 特殊值：昨天
                date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            elif date == "last_friday" and datetime.now().weekday() in [5, 6]:
                # 周末传入'last_friday'，自动获取上周五
                date = (
                    datetime.now() - timedelta(days=datetime.now().weekday() + 2)
                ).strftime("%Y%m%d")

            # 确保日期格式为 YYYYMMDD（去掉横杠）
            if isinstance(date, str) and "-" in date:
                date = date.replace("-", "")

            self.logger.info(f"开始获取 {date} 龙虎榜数据...")

            # 2. 获取龙虎榜个股列表
            self.logger.info("获取龙虎榜个股列表...")
            lhb_list_df = get_akshare().stock_lhb_detail_em(
                start_date=date, end_date=date
            )

            if lhb_list_df is None or lhb_list_df.empty:
                self.logger.warning(f"{date} 无龙虎榜数据")
                return {"success": False, "count": 0, "total": 0, "data": []}

            self.logger.info(f"获取到 {len(lhb_list_df)} 条记录")

            # 3. 去重处理（同一只股票多次上榜）
            self.logger.info("去重处理...")
            deduped_stocks = self._deduplicate_stocks(lhb_list_df)
            self.logger.info(f"去重后 {len(deduped_stocks)} 只个股")

            # 4. 批量保存到数据库
            self.logger.info("批量保存到数据库...")
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 批量收集数据
            stocks_to_save = []
            seats_to_save = []

            for stock in deduped_stocks:
                try:
                    # 收集个股数据
                    stocks_to_save.append(self._prepare_stock_data(stock, date))

                    # 获取并收集席位明细
                    seats = self._fetch_seats(stock["代码"], date)
                    seats_to_save.extend(seats)

                except Exception as e:
                    self.logger.warning(f"准备 {stock['名称']} 数据失败：{e}")
                    continue

            # 批量插入个股数据
            if stocks_to_save:
                cursor.executemany(
                    """
                    INSERT OR REPLACE INTO lhb_stocks (
                        trade_date, stock_code, stock_name, close_price, change_percent,
                        turnover_rate, turnover_amount, net_inflow,
                        buy_amount, sell_amount, reason, interpretation,
                        post_1d_change, post_2d_change, post_5d_change, post_10d_change, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    stocks_to_save,
                )
                self.logger.info(f"✅ 批量保存 {len(stocks_to_save)} 只个股")

            # 批量插入席位数据（先删除旧数据）
            if seats_to_save:
                # 收集所有股票代码
                stock_codes = set(seat[1] for seat in seats_to_save)
                date_formatted = seats_to_save[0][0]

                # 删除旧席位数据
                for stock_code in stock_codes:
                    cursor.execute(
                        """
                        DELETE FROM lhb_seats
                        WHERE trade_date = ? AND stock_code = ?
                    """,
                        (date_formatted, stock_code),
                    )

                # 批量插入新数据
                cursor.executemany(
                    """
                    INSERT INTO lhb_seats (
                        trade_date, stock_code, seat_type, seat_rank,
                        seat_name, buy_amount, sell_amount, seat_tags, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    seats_to_save,
                )
                self.logger.info(f"✅ 批量保存 {len(seats_to_save)} 个席位")

            conn.commit()

            self.logger.info(
                f"✅ 采集完成：{len(stocks_to_save)}只个股，{len(seats_to_save)}个席位"
            )
            result = {
                "success": True,
                "count": len(stocks_to_save),
                "total": len(stocks_to_save),
                "data": stocks_to_save,
            }

        except Exception as e:
            if "conn" in locals() and conn:
                conn.rollback()
            self.logger.error(f"❌ 保存失败：{e}")
            import traceback

            self.logger.error(traceback.format_exc())
            result = {"success": False, "count": 0, "total": 0, "data": []}
            raise

        finally:
            if "conn" in locals() and conn:
                conn.close()

        return result

    def get_daily_summary(self, date: Optional[str] = None) -> Dict:
        """
        从数据库获取指定日期的龙虎榜摘要

        Args:
            date: 日期字符串 YYYYMMDD，默认今天

        Returns:
            {summary: {...}, stocks: [...]}
        """
        try:
            if date is None:
                date = datetime.now().strftime("%Y%m%d")

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取个股数据
            cursor.execute(
                """
                SELECT * FROM lhb_stocks
                WHERE trade_date = ?
                ORDER BY net_inflow DESC
            """,
                (date,),
            )

            stocks = []
            total_inst_buy = 0
            total_inst_sell = 0
            total_hot_buy = 0
            total_hot_sell = 0

            for row in cursor.fetchall():
                stock = dict(row)
                stocks.append(stock)

                # 统计机构/游资净买入（从席位数据）
                cursor.execute(
                    """
                    SELECT buyer_seat, seller_seat, buy_amount, sell_amount
                    FROM lhb_seats
                    WHERE stock_code = ? AND trade_date = ?
                """,
                    (stock["stock_code"], date),
                )

                for seat in cursor.fetchall():
                    buyer = seat[4] or ""  # seat_name
                    seller = seat[4] or ""  # seat_name
                    buy_amt = seat[5] or 0  # buy_amount
                    sell_amt = seat[6] or 0  # sell_amount

                    if "机构" in buyer or "机构专用" in buyer:
                        total_inst_buy += buy_amt
                        total_inst_sell += sell_amt
                    else:
                        total_hot_buy += buy_amt
                        total_hot_sell += sell_amt

            conn.close()

            summary = {
                "total_stocks": len(stocks),
                "inst_net_buy": total_inst_buy - total_inst_sell,
                "hot_money_net_buy": total_hot_buy - total_hot_sell,
            }

            return {"summary": summary, "stocks": stocks}

        except Exception as e:
            self.logger.error(f"获取摘要失败：{e}")
            return {
                "summary": {
                    "total_stocks": 0,
                    "inst_net_buy": 0,
                    "hot_money_net_buy": 0,
                },
                "stocks": [],
            }

    def _deduplicate_stocks(self, df) -> List[Dict]:
        """
        去重处理（同一只股票多次上榜）

        优先级规则：
        1. 单日榜 > 三日榜（三日榜金额是累计的，会失真）
        2. 都是单日榜 → 保留席位更多的那条
        3. 都是三日榜 → 保留金额更大的那条

        Args:
            df: AkShare 返回的 DataFrame

        Returns:
            去重后的股票列表
        """
        stock_dict = {}

        for _, row in df.iterrows():
            code = str(row.get("代码", "")).zfill(6)
            name = row.get("名称", "")
            reason = row.get("上榜原因", "")
            key = f"{code}_{name}"

            # 判断是否为三日榜
            is_3day = "连续三个交易日" in reason

            stock_data = {
                "代码": code,
                "名称": name,
                "收盘价": float(row.get("收盘价", 0) or 0),
                "涨跌幅": float(row.get("涨跌幅", 0) or 0),
                "龙虎榜净买额": float(row.get("龙虎榜净买额", 0) or 0),
                "龙虎榜买入额": float(row.get("龙虎榜买入额", 0) or 0),
                "龙虎榜卖出额": float(row.get("龙虎榜卖出额", 0) or 0),
                "龙虎榜成交额": float(row.get("龙虎榜成交额", 0) or 0),
                "市场总成交额": float(row.get("市场总成交额", 0) or 0),
                "净买额占总成交比": float(row.get("净买额占总成交比", 0) or 0),
                "成交额占总成交比": float(row.get("成交额占总成交比", 0) or 0),
                "换手率": float(row.get("换手率", 0) or 0),
                "流通市值": float(row.get("流通市值", 0) or 0),
                "上榜原因": reason,
                "上榜后 1 日": float(row.get("上榜后 1 日", 0) or 0),
                "上榜后 2 日": float(row.get("上榜后 2 日", 0) or 0),
                "上榜后 5 日": float(row.get("上榜后 5 日", 0) or 0),
                "上榜后 10 日": float(row.get("上榜后 10 日", 0) or 0),
            }

            if key not in stock_dict:
                stock_dict[key] = stock_data
            else:
                existing = stock_dict[key]
                existing_reason = existing.get("上榜原因", "")
                existing_is_3day = "连续三个交易日" in existing_reason

                # 优先级比较
                should_replace = False

                if is_3day and not existing_is_3day:
                    # 新的是三日榜，旧的是单日榜 → 保留单日榜
                    should_replace = False
                elif not is_3day and existing_is_3day:
                    # 新的是单日榜，旧的是三日榜 → 替换为单日榜
                    should_replace = True
                elif is_3day and existing_is_3day:
                    # 都是三日榜，保留金额更大的
                    if abs(stock_data["龙虎榜净买额"]) > abs(existing["龙虎榜净买额"]):
                        should_replace = True
                else:
                    # 都是单日榜，保留原因更丰富的（后续合并）
                    if reason and reason not in existing_reason:
                        existing["上榜原因"] = existing_reason + "; " + reason

                if should_replace:
                    stock_dict[key] = stock_data

        return list(stock_dict.values())

    def _prepare_stock_data(self, stock: Dict, trade_date: str) -> tuple:
        """
        准备个股数据（用于批量保存）

        Args:
            stock: 个股数据字典
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            元组数据
        """
        date_formatted = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

        return (
            date_formatted,
            stock["代码"],
            stock["名称"],
            stock["收盘价"],
            stock["涨跌幅"],
            stock["换手率"],
            stock["市场总成交额"],
            stock["龙虎榜净买额"],
            stock["龙虎榜买入额"],
            stock["龙虎榜卖出额"],
            stock["上榜原因"],
            "",  # interpretation 留空
            stock["上榜后 1 日"],
            stock["上榜后 2 日"],
            stock["上榜后 5 日"],
            stock["上榜后 10 日"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # created_at
        )

    def _fetch_seats(self, stock_code: str, trade_date: str) -> List[tuple]:
        """
        获取席位明细（返回列表，用于批量保存）

        Args:
            stock_code: 股票代码
            trade_date: 交易日期（YYYYMMDD）

        Returns:
            席位数据列表（买五 + 卖五，共 10 个席位）
        """
        try:
            date_formatted = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
            seats = []

            # 分别获取买入席位和卖出席位（买五 + 卖五）
            try:
                buy_df = get_akshare().stock_lhb_stock_detail_em(
                    symbol=stock_code, date=trade_date, flag="买入"
                )
            except:
                buy_df = None

            try:
                sell_df = get_akshare().stock_lhb_stock_detail_em(
                    symbol=stock_code, date=trade_date, flag="卖出"
                )
            except:
                sell_df = None

            # 处理买入席位（买五）
            if buy_df is not None and not buy_df.empty:
                for idx, (_, row) in enumerate(buy_df.iterrows()):
                    seat_name = row.get("交易营业部名称", "")
                    if not seat_name:
                        continue

                    buy_amt = (
                        float(row.get("买入金额", 0))
                        if str(row.get("买入金额")) != "nan"
                        else 0
                    )
                    sell_amt = (
                        float(row.get("卖出金额", 0))
                        if str(row.get("卖出金额")) != "nan"
                        else 0
                    )

                    seats.append(
                        (
                            date_formatted,
                            stock_code,
                            "buy",
                            idx + 1,  # 买座排名 1-5
                            seat_name,
                            buy_amt,
                            sell_amt,
                            "",
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # created_at
                        )
                    )

            # 处理卖出席位（卖五）
            if sell_df is not None and not sell_df.empty:
                for idx, (_, row) in enumerate(sell_df.iterrows()):
                    seat_name = row.get("交易营业部名称", "")
                    if not seat_name:
                        continue

                    buy_amt = (
                        float(row.get("买入金额", 0))
                        if str(row.get("买入金额")) != "nan"
                        else 0
                    )
                    sell_amt = (
                        float(row.get("卖出金额", 0))
                        if str(row.get("卖出金额")) != "nan"
                        else 0
                    )

                    seats.append(
                        (
                            date_formatted,
                            stock_code,
                            "sell",
                            idx + 1,  # 卖座排名 1-5
                            seat_name,
                            buy_amt,
                            sell_amt,
                            "",
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # created_at
                        )
                    )

            return seats

        except Exception as e:
            self.logger.warning(f"获取{stock_code}席位失败：{e}")
            return []


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("龙虎榜数据采集器 - 测试运行")
    print("=" * 60)

    try:
        collector = LHBCollector()
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
