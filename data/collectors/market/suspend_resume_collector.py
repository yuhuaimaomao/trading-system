"""
东方财富停复牌采集器（代理版）

基于 ProxyBaseCollector 实现，支持动态 IP 切换
"""

import sqlite3
from datetime import datetime
from typing import Dict, List

from data.collectors.proxy.proxy_base_collector import USER_AGENTS, ProxyBaseCollector


class SuspendResumeCollector(ProxyBaseCollector):
    """停复牌采集器"""

    # API 配置
    API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    PAGE_SIZE = 500
    MAX_PAGES = 1

    # 使用基类的 USER_AGENTS
    USER_AGENTS = USER_AGENTS

    # 数据库配置
    TABLE_NAME = "stock_suspend_resume"

    # 请求参数
    API_PARAMS = {
        "sortColumns": "SUSPEND_START_DATE",
        "sortTypes": "-1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": "RPT_CUSTOM_SUSPEND_DATA_INTERFACE",
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
    }

    # Referer
    REFERER_URL = "https://datacenter-web.eastmoney.com/"

    def __init__(self, trade_date: str = None, task_mgr=None):
        super().__init__(
            logger_name="SuspendResumeCollector",
            trade_date=trade_date,
            task_mgr=task_mgr,
        )
        self.logger.info("✅ 停复牌采集器初始化完成（代理版）")

    def fetch_all(self, max_retries=3):
        """
        重写父类的 fetch_all，使用带 filter 的自定义请求
        """
        return self.fetch(self.trade_date)

    def fetch(self, trade_date: str = None) -> List[Dict]:
        """
        获取停复牌数据（带 filter 参数）

        Args:
            trade_date: 交易日期（默认今天）

        Returns:
            数据列表
        """
        if trade_date is None:
            trade_date = self.trade_date

        self.logger.info(f"开始获取停复牌数据（日期：{trade_date}）")

        try:
            params = self.API_PARAMS.copy()
            params["filter"] = f"(MARKET=\"全部\")(DATETIME='{trade_date}')"

            data = self._request_with_retry(
                self.API_URL,
                params,
                referer=self.REFERER_URL,
                desc="停复牌",
            )

            if data is None:
                return []

            result = data.get("result", {}).get("data", [])
            self.logger.info(f"获取到 {len(result)} 条数据")

            return result

        except Exception as e:
            self.logger.error(f"❌ 获取失败：{e}")
            return []

    def _parse_data(self, data: Dict) -> List[Dict]:
        """解析 API 返回数据"""
        if not data.get("result") or not data["result"].get("data"):
            return []
        return data["result"]["data"]

    def _save_to_db(self, data: List[Dict]):
        """保存到数据库（停牌和复牌分别保存）"""
        if not data or len(data) == 0:
            self.logger.warning("数据为空，跳过保存")
            return

        try:
            from system.config.settings import DATABASE_PATH

            trade_date = self.trade_date
            trade_time = datetime.now().strftime("%H:%M:%S")

            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()

            # 删除旧数据
            cursor.execute(
                "DELETE FROM stock_suspend_resume WHERE trade_date = ?", (trade_date,)
            )
            conn.commit()

            save_count = 0

            for item in data:
                try:
                    # 判断状态
                    suspend_reason = item.get("SUSPEND_REASON", "")
                    resume_reason = item.get("RESUME_REASON", "")

                    if resume_reason:
                        status = "已复牌"
                    elif suspend_reason:
                        status = "停牌中"
                    else:
                        continue  # 没有停牌或复牌原因，跳过

                    # 保存到合并表
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO stock_suspend_resume (
                            trade_date, stock_code, stock_name,
                            suspend_start_date, suspend_end_date,
                            suspend_reason, resume_reason, status,
                            market, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            trade_date,
                            str(item.get("SECURITY_CODE", "")),
                            str(item.get("SECURITY_NAME_ABBR", "")),
                            str(item.get("SUSPEND_START_DATE", "")),
                            str(item.get("SUSPEND_END_DATE", "")),
                            str(suspend_reason),
                            str(resume_reason),
                            status,
                            str(item.get("TRADE_MARKET", "")),
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    save_count += 1

                except Exception as e:
                    self.logger.debug(f"保存失败：{e}")

            conn.commit()
            conn.close()
            self.logger.info(f"✅ 保存成功：{save_count}条")

        except Exception as e:
            self.logger.error(f"❌ 保存失败：{e}")

    def fetch_and_save(self) -> Dict:
        """【新方法】采集并保存（一次执行）"""
        self.logger.info("=" * 60)
        self.logger.info(f"🍎 {self.__class__.__name__} 开始采集")
        self.logger.info("=" * 60)

        try:
            # 调用自定义的 fetch 方法（带 filter 参数）
            data = self.fetch(self.trade_date)

            if not data or len(data) == 0:
                self.logger.error("❌ 采集失败：数据为空")
                return {"success": False, "count": 0, "total": 0, "data": []}

            self._save_to_db(data)

            result = {
                "success": True,
                "count": len(data),
                "total": len(data),  # A 类统计
                "data": data,
            }

            self.logger.info(f"✅ {self.__class__.__name__} 采集完成：{len(data)}条")
            self.logger.info("=" * 60)
            return result

        except Exception as e:
            self.logger.error(f"❌ {self.__class__.__name__} 采集异常：{e}")
            self.logger.info("=" * 60)
            return {"success": False, "count": 0, "total": 0, "data": []}


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("停复牌数据采集器 - 测试运行")
    print("=" * 60)

    try:
        collector = SuspendResumeCollector()
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
