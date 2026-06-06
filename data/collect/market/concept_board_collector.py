"""
东方财富概念板块数据采集器（精简版）

按用户要求只保留必要字段
"""

import sqlite3
from datetime import datetime
from typing import Dict, List

from data.collect.proxy.proxy_base_collector import USER_AGENTS, ProxyBaseCollector


class ConceptBoardCollector(ProxyBaseCollector):
    """概念板块采集器（精简版）"""

    # API 配置
    API_URL = "https://push2.eastmoney.com/api/qt/clist/get"
    PAGE_SIZE = 100
    MAX_PAGES = 10
    MAX_RETRIES = 3
    RETRY_DELAYS = [2, 5, 10]
    FIRST_PAGE_MAX_RETRIES = 5  # 第 1 页多试 2 次，拿到 total 才能分页
    REQUEST_TIMEOUT = 10

    # 只请求需要的字段（26 个 API 字段）
    # 概念板块：fs="m:90 t:3"（行业板块是 t:2）
    API_PARAMS = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "fid": "f3",
        "fs": "m:90 t:3",  # 概念板块
        "fields": "f2,f3,f4,f7,f12,f14,f15,f16,f17,f18,f62,f66,f69,f72,f78,f81,f84,f87,f94,f104,f105,f128,f136,f140,f141,f184",
    }

    REFERER_URL = "https://quote.eastmoney.com/center/boardlist.html#concept_board"
    USER_AGENTS = USER_AGENTS
    TABLE_NAME = "sector_concept"

    def __init__(self, trade_date: str = None, task_mgr=None):
        super().__init__(
            logger_name="ConceptBoardCollector",
            trade_date=trade_date,
            task_mgr=task_mgr,
        )
        self.logger.info("概念板块采集器初始化完成（精简版）")
        self.logger.info(f"数据库表：{self.TABLE_NAME}")

    def _parse_data(self, data: Dict) -> List[Dict]:
        """解析 API 返回数据"""
        if not data.get("data") or not data["data"].get("diff"):
            return []
        return data["data"]["diff"]

    def _save_to_db(self, data: list):
        """保存到数据库（精简版 - 31 个字段）"""
        if not data or len(data) == 0:
            self.logger.warning("⚠️ 数据为空，跳过保存")
            return

        self.logger.info(f"保存 {len(data)} 条数据到数据库表 {self.TABLE_NAME}...")

        try:
            from system.config.settings import DATABASE_PATH

            trade_date = self.trade_date
            trade_time = datetime.now().strftime("%H:%M:%S")  # 实际采集时间
            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()

            cursor.execute(
                f"DELETE FROM {self.TABLE_NAME} WHERE trade_date = ?", (trade_date,)
            )
            conn.commit()
            self.logger.info(f"已删除 {trade_date} 的旧数据")

            # 按涨幅（f3）降序排序，计算 rank
            data.sort(key=lambda x: x.get("f3", 0), reverse=True)

            insert_data = []
            for i, row in enumerate(data, 1):
                insert_data.append(
                    (
                        trade_date,  # trade_date
                        trade_time,  # trade_time
                        i,  # rank (按涨幅排序)
                        str(row.get("f12", "")),  # sector_code
                        str(row.get("f14", "")),  # sector_name
                        self._safe_float(row.get("f2"), 0),  # latest_price
                        self._safe_float(row.get("f3"), 0),  # change_percent
                        self._safe_float(row.get("f4"), 0),  # change_amount
                        self._safe_float(row.get("f7"), 0),  # amplitude
                        self._safe_float(row.get("f15"), 0),  # high_price
                        self._safe_float(row.get("f16"), 0),  # low_price
                        self._safe_float(row.get("f17"), 0),  # open_price
                        self._safe_float(row.get("f18"), 0),  # prev_close
                        int(row.get("f104", 0)) if row.get("f104") else 0,  # up_count
                        int(row.get("f105", 0)) if row.get("f105") else 0,  # down_count
                        self._safe_float(row.get("f62"), 0),  # main_force_net
                        self._safe_float(row.get("f66"), 0),  # super_large_net
                        self._safe_float(row.get("f72"), 0),  # large_net
                        self._safe_float(row.get("f78"), 0),  # medium_net
                        self._safe_float(row.get("f84"), 0),  # small_net
                        self._safe_float(row.get("f184"), 0),  # main_force_ratio
                        self._safe_float(row.get("f69"), 0),  # super_large_ratio
                        self._safe_float(row.get("f81"), 0),  # medium_ratio
                        self._safe_float(row.get("f87"), 0),  # small_ratio
                        self._safe_float(row.get("f94"), 0),  # large_ratio
                        row.get("f128", ""),  # top_stock
                        self._safe_float(row.get("f136"), 0),  # top_stock_change
                        row.get("f140", ""),  # top_stock_code
                        int(row.get("f141", 0))
                        if row.get("f141")
                        else 0,  # top_stock_rank
                        "eastmoney",  # data_source
                        created_at,  # created_at
                    )
                )

            cursor.executemany(
                f"""
                INSERT INTO {self.TABLE_NAME} (
                    trade_date, trade_time, rank, sector_code, sector_name,
                    latest_price, change_percent, change_amount, amplitude,
                    high_price, low_price, open_price, prev_close,
                    up_count, down_count,
                    main_force_net, super_large_net, large_net, medium_net, small_net,
                    main_force_ratio, super_large_ratio, medium_ratio, small_ratio, large_ratio,
                    top_stock, top_stock_change, top_stock_code, top_stock_rank,
                    data_source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                insert_data,
            )

            conn.commit()
            conn.close()
            self.logger.info(f"✅ 保存到数据库成功：{len(insert_data)}条数据")

        except Exception as e:
            if "conn" in locals() and conn:
                conn.rollback()
            self.logger.error(f"❌ 保存到数据库失败：{e}")
            raise

    def fetch_and_save(self) -> Dict:
        """【新方法】采集并保存（一次执行）"""
        self.logger.info("=" * 60)
        self.logger.info(f"🍎 {self.__class__.__name__} 开始采集")
        self.logger.info("=" * 60)

        try:
            data = self.fetch_all()

            if not data or len(data) == 0:
                self.logger.error("❌ 采集失败：数据为空")
                return {"success": False, "count": 0, "total": 0, "data": []}

            self._save_to_db(data)

            # 获取应采集总数（从缓存获取 total 字段）
            total = self.cache_data.get("total", len(data))

            result = {"success": True, "count": len(data), "total": total, "data": data}

            self.logger.info(
                f"✅ {self.__class__.__name__} 采集完成：{len(data)}条（应采集{total}条）"
            )
            self.logger.info("=" * 60)
            return result

        except Exception as e:
            self.logger.error(f"❌ {self.__class__.__name__} 采集异常：{e}")
            self.logger.info("=" * 60)
            return {"success": False, "count": 0, "total": 0, "data": []}


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("概念板块数据采集器 - 测试运行")
    print("=" * 60)

    try:
        collector = ConceptBoardCollector()
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
