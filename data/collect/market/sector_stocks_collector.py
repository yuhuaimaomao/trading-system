"""
成分股采集器 (行业 + 概念统一版)

继承 ProxyBaseCollector，复用代理管理、重试机制、日志系统

支持:
1. 采集所有行业板块成分股
2. 采集所有概念板块成分股
3. 采集单个板块成分股 (指定板块代码)
"""

import sqlite3
import time
from datetime import datetime, timedelta
from math import ceil
from typing import Dict, List, Tuple

from data.collect.proxy.proxy_base_collector import ProxyBaseCollector


class SectorStocksCollector(ProxyBaseCollector):
    """成分股采集器 (行业 + 概念统一版)"""

    # 数据库配置
    TABLE_NAME = "sector_stocks"

    # API 配置
    API_URL = "https://push2.eastmoney.com/api/qt/clist/get"
    PAGE_SIZE = 100  # 每页 100 条
    MAX_RETRIES = 3  # 每页最多重试 3 次
    RETRY_DELAYS = [2, 5, 10]  # 重试延时（秒）
    REQUEST_TIMEOUT = 10  # 请求超时（秒）

    def __init__(self, trade_date: str = None, task_mgr=None):
        """
        初始化采集器

        Args:
            trade_date: 交易日期 (默认今天)
            task_mgr: 任务状态管理器
        """
        super().__init__(
            logger_name="market",
            trade_date=trade_date,
            task_mgr=task_mgr,
        )
        self.trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        self.logger.info("成分股采集器初始化完成")
        self.logger.info(f"交易日期：{self.trade_date}")

        # 清理 7 天前的进度
        self._cleanup_old_progress(days=7)

    # ==================== 进度管理方法 ====================

    def _cleanup_old_progress(self, days: int = 7):
        """
        清理 N 天前的进度记录

        Args:
            days: 保留天数 (默认 7 天)
        """
        from system.config.settings import DATABASE_PATH

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        cursor.execute(
            """
            DELETE FROM sector_collect_progress
            WHERE trade_date < ? AND status IN ('complete', 'failed')
        """,
            (cutoff_date,),
        )

        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted > 0:
            self.logger.info(f"🗑️ 已清理 {deleted} 条{cutoff_date}前的进度记录")

    def _get_progress(self, sector_code: str, sector_type: str) -> dict:
        """
        查询板块采集进度

        Args:
            sector_code: 板块代码
            sector_type: 板块类型 (industry/concept)

        Returns:
            进度字典，不存在返回 None
        """
        from system.config.settings import DATABASE_PATH

        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT * FROM sector_collect_progress
            WHERE trade_date = ? AND sector_code = ? AND sector_type = ?
        """,
            (self.trade_date, sector_code, sector_type),
        )

        row = cursor.fetchone()
        conn.close()

        if row:
            return dict(row)
        return None

    def _save_progress(self, sector_code: str, sector_name: str, sector_type: str, **kwargs):
        """
        保存/更新板块采集进度

        Args:
            sector_code: 板块代码
            sector_name: 板块名称
            sector_type: 板块类型 (industry/concept)
            **kwargs: 要更新的字段 (total_pages, completed_pages, status 等)
        """
        from system.config.settings import DATABASE_PATH

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        # 检查是否存在
        cursor.execute(
            """
            SELECT 1 FROM sector_collect_progress
            WHERE trade_date = ? AND sector_code = ? AND sector_type = ?
        """,
            (self.trade_date, sector_code, sector_type),
        )

        exists = cursor.fetchone()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if exists:
            # 更新
            updates = []
            values = []
            for key, value in kwargs.items():
                updates.append(f"{key} = ?")
                values.append(value)
            updates.append("updated_at = ?")
            values.append(now)
            values.extend([self.trade_date, sector_code, sector_type])

            sql = f"""
                UPDATE sector_collect_progress
                SET {", ".join(updates)}
                WHERE trade_date = ? AND sector_code = ? AND sector_type = ?
            """
            cursor.execute(sql, values)
        else:
            # 插入
            cursor.execute(
                """
                INSERT INTO sector_collect_progress
                (trade_date, sector_code, sector_name, sector_type, status, started_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
                (self.trade_date, sector_code, sector_name, sector_type, now, now),
            )

        conn.commit()
        conn.close()

    # ==================== 辅助方法 ====================

    def _get_sector_info(self, sector_code: str) -> Tuple[str, str]:
        """
        查询板块信息

        Args:
            sector_code: 板块代码

        Returns:
            (sector_type, sector_name) 元组
        """
        from system.config.settings import DATABASE_PATH

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT sector_type, sector_name FROM sector_info
            WHERE sector_code = ?
        """,
            (sector_code,),
        )

        row = cursor.fetchone()
        conn.close()

        if row:
            return (row[0], row[1])
        raise ValueError(f"未知的板块代码：{sector_code}")

    def _get_sector_list(self, sector_type: str) -> List[Dict]:
        """
        从 sector_info 表获取板块列表

        Args:
            sector_type: 板块类型 ('industry' 或 'concept')

        Returns:
            板块列表 [{'sector_code': 'BK0421', 'sector_name': '铁路公路'}, ...]
        """
        from system.config.settings import DATABASE_PATH

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        # 查询需要采集的板块
        cursor.execute(
            """
            SELECT sector_code, sector_name FROM sector_info
            WHERE sector_type = ? AND need_collect = 1
            ORDER BY sector_code
        """,
            (sector_type,),
        )

        sectors = [{"sector_code": row[0], "sector_name": row[1]} for row in cursor.fetchall()]

        conn.close()
        self.logger.info(f"从数据库获取到 {len(sectors)} 个{sector_type}板块")
        return sectors

    def _check_and_calibrate(self, sector_type: str):
        """
        采集前校准 sector_info 表

        Args:
            sector_type: 板块类型 ('industry' 或 'concept')
        """
        from system.config.settings import DATABASE_PATH

        self.logger.info(f"开始校准 {sector_type} 板块信息表...")

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        # 查询对应的行情表获取最新板块
        table_name = f"sector_{sector_type}"
        cursor.execute(f"""
            SELECT DISTINCT sector_code, sector_name FROM {table_name}
            WHERE trade_date = (SELECT MAX(trade_date) FROM {table_name})
        """)
        latest_sectors = {row[0]: row[1] for row in cursor.fetchall()}

        # 查询 sector_info 表已有板块
        cursor.execute(
            """
            SELECT sector_code, sector_name FROM sector_info
            WHERE sector_type = ?
        """,
            (sector_type,),
        )
        info_sectors = {row[0]: row[1] for row in cursor.fetchall()}

        # 发现删除的板块 → 标记为 need_collect = 0
        deleted_codes = set(info_sectors.keys()) - set(latest_sectors.keys())
        for code in deleted_codes:
            cursor.execute(
                """
                UPDATE sector_info SET need_collect = 0
                WHERE sector_code = ? AND sector_type = ?
            """,
                (code, sector_type),
            )
            self.logger.info(f"标记删除的板块：{code}")

        # 发现新增的板块 → 插入 sector_info 表
        new_codes = set(latest_sectors.keys()) - set(info_sectors.keys())
        for code in new_codes:
            name = latest_sectors[code]
            cursor.execute(
                """
                INSERT OR IGNORE INTO sector_info
                (sector_code, sector_name, sector_type, need_collect, created_at)
                VALUES (?, ?, ?, 1, ?)
            """,
                (code, name, sector_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            self.logger.info(f"新增板块：{code} - {name}")

        conn.commit()
        conn.close()
        self.logger.info(f"校准完成：删除{len(deleted_codes)}个，新增{len(new_codes)}个")

    def _fetch_page(self, sector_code: str, page_num: int, proxy: Dict) -> Dict:
        params = {
            "pn": str(page_num),
            "pz": str(self.PAGE_SIZE),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "ut": "8dec03ba335b81bf4ebdf7b29ec27d15",
            "fid": "f62",
            "fs": f"b:{sector_code}",
            "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124,f1,f13",
        }

        return self._request_with_retry(
            self.API_URL,
            params,
            referer="https://quote.eastmoney.com/center/boardlist.html",
            desc=f"板块成分股 {sector_code} 第{page_num}页",
        )

    def _parse_stock_data(self, data: Dict, sector_code: str) -> List[Dict]:
        """
        解析成分股数据

        Args:
            data: API 返回数据
            sector_code: 板块代码

        Returns:
            成分股列表
        """
        stocks = []

        if not data.get("data") or not data["data"].get("diff"):
            return stocks

        for item in data["data"]["diff"]:
            stock = {
                "sector_code": sector_code,
                "stock_code": item.get("f12", ""),
                "stock_name": item.get("f14", ""),
            }
            stocks.append(stock)

        return stocks

    def _save_page_to_db(self, stocks: List[Dict], sector_code: str):
        """
        保存单页数据到数据库（实时保存）

        Args:
            stocks: 成分股列表
            sector_code: 板块代码
        """
        from system.config.settings import DATABASE_PATH

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        try:
            # 使用 INSERT OR IGNORE，重复就跳过
            for stock in stocks:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO sector_stocks
                    (sector_code, stock_code, stock_name)
                    VALUES (?, ?, ?)
                """,
                    (stock["sector_code"], stock["stock_code"], stock["stock_name"]),
                )

            conn.commit()
            self.logger.debug(f"保存{len(stocks)}条成分股到数据库")

        except Exception as e:
            conn.rollback()
            self.logger.error(f"保存失败：{e}")

        finally:
            conn.close()

    def _check_db_count(self, sector_code: str) -> int:
        """
        查询数据库已有成分股数量

        Args:
            sector_code: 板块代码

        Returns:
            成分股数量
        """
        from system.config.settings import DATABASE_PATH

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT COUNT(*) FROM sector_stocks WHERE sector_code = ?
        """,
            (sector_code,),
        )

        count = cursor.fetchone()[0]
        conn.close()

        return count

    def _delete_sector_stocks(self, sector_code: str):
        """
        删除板块的成分股数据

        Args:
            sector_code: 板块代码
        """
        from system.config.settings import DATABASE_PATH

        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()

        cursor.execute(
            """
            DELETE FROM sector_stocks WHERE sector_code = ?
        """,
            (sector_code,),
        )

        conn.commit()
        conn.close()
        self.logger.info(f"删除{sector_code}的成分股数据")

    # ==================== 核心采集方法 ====================

    def _collect_single_sector(self, sector_type: str, sector_code: str, sector_name: str) -> bool:
        """
        采集单个板块的成分股 (支持断点续传)

        Args:
            sector_type: 板块类型 (industry/concept)
            sector_code: 板块代码
            sector_name: 板块名称

        Returns:
            是否成功
        """
        self.logger.info(f"开始采集：{sector_name}({sector_code})")

        # 1. 查询进度
        progress = self._get_progress(sector_code, sector_type)

        if progress:
            if progress["status"] == "complete":
                self.logger.info(f"✅ {sector_name} 已完成，跳过")
                return True

            # 恢复进度
            start_page = progress["completed_pages"] + 1
            total_pages = progress["total_pages"]
            self.logger.info(f"📋 恢复进度：{sector_name} 第{start_page}/{total_pages}页")

            # 如果已经采完所有页，标记完成
            if start_page > total_pages:
                self.logger.info(f"✅ {sector_name} 进度已完成，跳过")
                return True
        else:
            # 新建进度
            start_page = 1
            total_pages = 0
            self._save_progress(sector_code, sector_name, sector_type, status="pending")

        # 2. 第 1 页：获取总数量 (如果还没获取)
        if total_pages == 0:
            for retry in range(self.MAX_RETRIES):
                proxy_dict = self.proxy_manager.get_proxy()
                if not proxy_dict:
                    self.logger.warning("获取代理失败，等待 2 秒后重试...")
                    time.sleep(2)
                    continue

                data = self._fetch_page(sector_code, 1, proxy_dict)

                if data and data.get("rc") == 0 and data.get("data"):
                    total = data["data"].get("total", 0)
                    self.logger.info(f"总数量：{total}")

                    # 检查数据库已有数量
                    db_count = self._check_db_count(sector_code)
                    self.logger.info(f"数据库已有：{db_count}条")

                    # 数据一致性检查
                    if db_count == total and total > 0:
                        self.logger.info("✅ 数据完整，跳过采集")
                        self._save_progress(
                            sector_code,
                            sector_name,
                            sector_type,
                            total_pages=1,
                            total_stocks=total,
                            collected_stocks=db_count,
                            status="complete",
                            completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        )
                        return True

                    # 数据不完整 → 删除旧数据 → 重新采集
                    if db_count > 0:
                        self.logger.info(f"数据不完整（{db_count}/{total}），删除旧数据...")
                        self._delete_sector_stocks(sector_code)

                    # 解析第 1 页数据
                    stocks = self._parse_stock_data(data, sector_code)

                    # 先保存数据 ⬅️ 关键：先入库
                    self._save_page_to_db(stocks, sector_code)
                    self.logger.info(f"第 1 页采集成功：{len(stocks)}条")

                    # 计算总页数
                    total_pages = ceil(total / self.PAGE_SIZE)

                    # 再更新进度 ⬅️ 关键：入库后再更新
                    self._save_progress(
                        sector_code,
                        sector_name,
                        sector_type,
                        total_pages=total_pages,
                        total_stocks=total,
                        completed_pages=1,
                        collected_stocks=self._check_db_count(sector_code),
                        status="running",
                        started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )

                    # 如果只有 1 页，采集完成
                    if total_pages == 1:
                        self._save_progress(
                            sector_code,
                            sector_name,
                            sector_type,
                            status="complete",
                            completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        )
                        self.logger.info(f"✅ {sector_name} 采集完成，共{total}条")
                        return True

                    # 设置起始页为第 2 页
                    start_page = 2
                    break
                elif data and data.get("rc") == 102:
                    # API 返回 rc:102（无成分股数据）
                    self.logger.warning(f"{sector_name} 无成分股数据（rc:102），跳过")
                    self._save_progress(
                        sector_code,
                        sector_name,
                        sector_type,
                        status="complete",
                        completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    return True
                else:
                    self.logger.warning(f"第 1 页第{retry + 1}次失败，等待后重试...")
                    time.sleep(self.RETRY_DELAYS[retry] if retry < len(self.RETRY_DELAYS) else 10)
            else:
                self.logger.error(f"{sector_name} 第 1 页采集失败")
                self._save_progress(
                    sector_code,
                    sector_name,
                    sector_type,
                    status="failed",
                    last_error="第 1 页采集失败",
                )
                return False

        # 3. 采集第 start_page 到 total_pages 页
        failed_pages = []

        self.logger.info(f"开始采集第 {start_page}-{total_pages}页...")
        for page in range(start_page, total_pages + 1):
            success = False

            for retry in range(self.MAX_RETRIES):
                proxy_dict = self.proxy_manager.get_proxy()
                if not proxy_dict:
                    time.sleep(2)
                    continue

                data = self._fetch_page(sector_code, page, proxy_dict)

                if data and data.get("rc") == 0 and data.get("data"):
                    stocks = self._parse_stock_data(data, sector_code)

                    # 先保存数据 ⬅️ 关键：先入库
                    self._save_page_to_db(stocks, sector_code)
                    self.logger.info(f"第{page}页采集成功：{len(stocks)}条")

                    # 再更新进度 ⬅️ 关键：入库后再更新
                    self._save_progress(
                        sector_code,
                        sector_name,
                        sector_type,
                        completed_pages=page,
                        collected_stocks=self._check_db_count(sector_code),
                    )

                    success = True
                    break
                else:
                    self.logger.warning(f"第{page}页第{retry + 1}次失败，等待后重试...")
                    time.sleep(self.RETRY_DELAYS[retry] if retry < len(self.RETRY_DELAYS) else 10)

            if not success:
                self.logger.error(f"第{page}页采集失败，记录到失败列表")
                failed_pages.append(page)

        # 4. 第 2 轮：重试失败页
        if failed_pages:
            self.logger.warning(f"\n{'=' * 60}")
            self.logger.warning(f"第 1 轮结束，{len(failed_pages)}页采集失败，开始第 2 轮重试...")
            self.logger.warning(f"{'=' * 60}\n")

            for page in failed_pages[:]:
                self.logger.info(f"重试第{page}页...")

                for retry in range(self.MAX_RETRIES):
                    proxy_dict = self.proxy_manager.get_proxy()
                    if not proxy_dict:
                        time.sleep(2)
                        continue

                    data = self._fetch_page(sector_code, page, proxy_dict)

                    if data and data.get("rc") == 0 and data.get("data"):
                        stocks = self._parse_stock_data(data, sector_code)

                        # 先保存数据
                        self._save_page_to_db(stocks, sector_code)
                        self.logger.info(f"第{page}页重试成功：{len(stocks)}条")

                        # 再更新进度
                        self._save_progress(
                            sector_code,
                            sector_name,
                            sector_type,
                            completed_pages=page,
                            collected_stocks=self._check_db_count(sector_code),
                        )

                        failed_pages.remove(page)
                        break
                    else:
                        time.sleep(self.RETRY_DELAYS[retry] if retry < len(self.RETRY_DELAYS) else 10)

        # 5. 最终检查
        if failed_pages:
            self.logger.error(f"\n{'=' * 60}")
            self.logger.error(f"⚠️ {sector_name} 仍有{len(failed_pages)}页采集失败")
            self.logger.error(f"失败页：{failed_pages}")
            self.logger.error(f"{'=' * 60}")

            self._save_progress(
                sector_code,
                sector_name,
                sector_type,
                status="failed",
                last_error=f"失败页：{failed_pages}",
            )
            return False
        else:
            self._save_progress(
                sector_code,
                sector_name,
                sector_type,
                status="complete",
                completed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

            self.logger.info(f"\n{'=' * 60}")
            self.logger.info(f"✅ {sector_name} 全部采集完成！")
            self.logger.info(f"{'=' * 60}")
            return True

    def _collect_type(self, sector_type: str):
        """
        采集某类型的所有板块

        Args:
            sector_type: 板块类型 ('industry' 或 'concept')
        """
        self.logger.info("=" * 60)
        self.logger.info(f"🍎 开始采集{sector_type}板块成分股")
        self.logger.info("=" * 60)

        # 采集前校准
        self._check_and_calibrate(sector_type)

        # 获取板块列表
        sectors = self._get_sector_list(sector_type)
        self.logger.info(f"待采集板块：{len(sectors)}个")
        self.logger.info("=" * 60)

        # 遍历采集
        success_count = 0
        failed_count = 0

        for i, sector in enumerate(sectors, 1):
            self.logger.info(f"\n[{i}/{len(sectors)}] {sector['sector_name']}({sector['sector_code']})")

            result = self._collect_single_sector(sector_type, sector["sector_code"], sector["sector_name"])

            if result:
                success_count += 1
            else:
                failed_count += 1

            # 板块之间延时 1-2 秒
            if i < len(sectors):
                time.sleep(1.5)

        # 完成
        self.logger.info("\n" + "=" * 60)
        self.logger.info(f"✅ {sector_type}板块采集完成！")
        self.logger.info(f"总板块数：{len(sectors)}")
        self.logger.info(f"成功：{success_count}")
        self.logger.info(f"失败：{failed_count}")
        self.logger.info("=" * 60)

    # ==================== 公共接口方法 ====================

    def collect_all(self, sector_type: str = "all"):
        """
        采集所有板块成分股

        Args:
            sector_type:
                - 'all': 采集所有 (行业 + 概念)
                - 'industry': 只采集行业
                - 'concept': 只采集概念
        """
        start_time = datetime.now()

        self.logger.info("\n" + "=" * 70)
        self.logger.info("🍎 股票量化系统 - 成分股采集器 (统一版)")
        self.logger.info(f"交易日期：{self.trade_date}")
        self.logger.info(f"采集类型：{sector_type}")
        self.logger.info("=" * 70)

        if sector_type in ["industry", "all"]:
            self._collect_type("industry")

        if sector_type in ["concept", "all"]:
            self._collect_type("concept")

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        self.logger.info("\n" + "=" * 70)
        self.logger.info("✅ 全部采集任务完成！")
        self.logger.info(f"总耗时：{duration:.2f}秒 ({duration / 60:.1f}分钟)")
        self.logger.info("=" * 70)

    def collect_sector(self, sector_code: str):
        """
        采集单个板块成分股

        Args:
            sector_code: 板块代码 (如 BK0401 银行，BK0800 人工智能)
        """
        start_time = datetime.now()

        self.logger.info("\n" + "=" * 70)
        self.logger.info("🍎 股票量化系统 - 采集单个板块成分股")
        self.logger.info(f"交易日期：{self.trade_date}")
        self.logger.info(f"板块代码：{sector_code}")
        self.logger.info("=" * 70)

        # 查询板块信息
        try:
            sector_type, sector_name = self._get_sector_info(sector_code)
            self.logger.info(f"板块名称：{sector_name}")
            self.logger.info(f"板块类型：{sector_type}")
        except ValueError as e:
            self.logger.error(f"❌ {e}")
            return False

        # 采集这个板块
        result = self._collect_single_sector(sector_type, sector_code, sector_name)

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        self.logger.info("\n" + "=" * 70)
        if result:
            self.logger.info(f"✅ {sector_name} 采集完成！")
        else:
            self.logger.error(f"❌ {sector_name} 采集失败！")
        self.logger.info(f"总耗时：{duration:.2f}秒")
        self.logger.info("=" * 70)

        return result


# ==================== 主入口 ====================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("🍎 成分股采集器 (统一版)")
        print()
        print("用法：python sector_stocks_collector.py <命令> [参数]")
        print()
        print("命令:")
        print("  all              采集所有 (行业 + 概念)")
        print("  industry         采集所有行业")
        print("  concept          采集所有概念")
        print("  sector <代码>    采集单个板块")
        print()
        print("示例:")
        print("  python sector_stocks_collector.py all")
        print("  python sector_stocks_collector.py industry")
        print("  python sector_stocks_collector.py concept")
        print("  python sector_stocks_collector.py sector BK0401")
        print("  python sector_stocks_collector.py sector BK0800")
        sys.exit(1)

    command = sys.argv[1]
    collector = SectorStocksCollector()

    if command == "all":
        collector.collect_all(sector_type="all")
    elif command == "industry":
        collector.collect_all(sector_type="industry")
    elif command == "concept":
        collector.collect_all(sector_type="concept")
    elif command == "sector":
        if len(sys.argv) < 3:
            print("❌ 缺少板块代码")
            print("用法：python sector_stocks_collector.py sector <板块代码>")
            sys.exit(1)
        sector_code = sys.argv[2]
        collector.collect_sector(sector_code)
    else:
        print(f"❌ 未知命令：{command}")
        sys.exit(1)
