"""
重点监控名单数据采集器
功能：从交易所官网获取重点监控股票名单，保存到数据库

数据源：https://m.123.com.cn/wap2/abnormal_detect
"""

import random
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

from data.collect.proxy.proxy_base_collector import USER_AGENTS
from system.utils.logger import get_collector_logger

logger = get_collector_logger("stock_monitor")


# 反爬配置

REQUEST_TIMEOUT = 10  # 请求超时时间（秒）
REQUEST_DELAY = (8, 15)  # 请求延迟范围（秒，反爬优化）
MAX_RETRIES = 3  # 最大重试次数


class StockMonitorCollector:
    """重点监控名单采集器"""

    def __init__(self):
        # 真实 API 接口
        self.api_url = "https://stock.api.123.com.cn/tool/change_serious"
        self.base_url = "https://m.123.com.cn/wap2/abnormal_detect"
        self.session = None
        logger.info("重点监控名单采集器初始化完成")

    def _init_session(self):
        """初始化 Session（带反爬配置）"""
        self.session = requests.Session()

        # 随机选择 User-Agent
        user_agent = random.choice(USER_AGENTS)
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Cache-Control": "max-age=0",
            }
        )

        # 禁用代理（直连）
        self.session.trust_env = False
        self.session.proxies = {}

        logger.info(f"Session 初始化完成 (User-Agent: {user_agent[:50]}...)")

    def _random_delay(self):
        """随机延迟（反爬）"""
        delay = random.uniform(*REQUEST_DELAY)
        logger.debug(f"延迟 {delay:.2f} 秒...")
        time.sleep(delay)

    def fetch(self, trade_date: str = None) -> List[Dict]:
        """
        获取重点监控名单（从真实 API 接口）

        API: https://stock.api.123.com.cn/tool/change_serious?page=1&size=20&type=0

        Args:
            trade_date: 交易日期（默认今天）

        Returns:
            监控名单列表
        """
        try:
            # 初始化 Session
            if self.session is None:
                self._init_session()

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            logger.info(f"开始获取重点监控名单（日期：{trade_date}）...")

            # 重试机制
            for attempt in range(MAX_RETRIES):
                try:
                    # 随机延迟
                    self._random_delay()

                    # 真实 API 接口
                    logger.info(f"第 {attempt + 1}/{MAX_RETRIES} 次尝试...")

                    # 分页获取（最多 5 页，每页 20 条）
                    all_data = []
                    for page in range(1, 6):
                        params = {
                            "page": page,
                            "size": 20,
                            "type": 0,  # 0=全部，1=严重异动，2=重点监控
                        }

                        response = self.session.get(
                            self.api_url, params=params, timeout=REQUEST_TIMEOUT
                        )

                        if response.status_code == 200:
                            json_data = response.json()
                            page_data = self._parse_api_response(json_data, trade_date)

                            if page_data:
                                all_data.extend(page_data)
                                logger.info(f"第{page}页：{len(page_data)}只")

                            # 如果返回数据少于 20 条，说明是最后一页
                            if len(page_data) < 20:
                                break
                        else:
                            logger.warning(
                                f"第{page}页请求失败：{response.status_code}"
                            )
                            break

                    if all_data:
                        # 去重
                        result = self._deduplicate(all_data)
                        logger.info(f"✅ 获取成功：{len(result)}只（去重后）")
                        return result
                    else:
                        logger.warning("所有页数据为空")
                        return []

                except requests.exceptions.Timeout:
                    logger.error("请求超时，重试中...")
                    time.sleep(5)
                    continue

                except requests.exceptions.RequestException as e:
                    logger.error(f"网络异常：{e}")
                    return []

            logger.error("❌ 超过最大重试次数，获取失败")
            return []

        except Exception as e:
            logger.error(f"获取重点监控名单失败：{e}")
            return []

    def _parse_api_response(self, json_data: dict, trade_date: str) -> List[Dict]:
        """
        解析 API 响应

        实际 API 返回格式：
        {
            "code": "00000",
            "msg": "",
            "data": {
                "page": 1,
                "pageSize": 20,
                "count": 19899,
                "data": [
                    {
                        "name": "海川智能",
                        "code": "300720",
                        "rate": 1.82,
                        "days": 10,
                        "biasRatio": 74.5,
                        "finalRatio": 17.03,
                        "date": "2026-03-26",
                        "flag": 0,
                        "isSerious": 0
                    }
                ]
            }
        }

        Args:
            json_data: JSON 数据
            trade_date: 交易日期

        Returns:
            监控名单列表
        """
        try:
            result = []

            # 实际格式：data.data 是列表
            data_list = []

            if "data" in json_data:
                inner_data = json_data["data"]

                # 格式 1: data.data 是列表
                if isinstance(inner_data, dict) and "data" in inner_data:
                    data_list = inner_data["data"]

                # 格式 2: data 直接是列表
                elif isinstance(inner_data, list):
                    data_list = inner_data

            for item in data_list:
                try:
                    # 提取字段
                    stock_info = {
                        "stock_code": str(item.get("code", "")),
                        "stock_name": item.get("name", ""),
                        "monitor_type": self._normalize_monitor_type_by_flag(item),
                        "trigger_rule": f"{item.get('days', 0)}天内涨幅偏离值累计{item.get('biasRatio', 0):.2f}%",
                        "trigger_threshold": float(item.get("finalRatio", 0)),
                        "status": "监控中",
                        "trade_date": item.get("date", trade_date),
                        "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        # 额外字段
                        "rate": float(item.get("rate", 0)),
                        "days": int(item.get("days", 0)),
                        "bias_ratio": float(item.get("biasRatio", 0)),
                        "final_ratio": float(item.get("finalRatio", 0)),
                    }
                    result.append(stock_info)
                except Exception as e:
                    logger.debug(f"解析单条数据失败：{e}")
                    continue

            return result

        except Exception as e:
            logger.error(f"解析 API 响应失败：{e}")
            return []

    def _normalize_monitor_type_by_flag(self, item: dict) -> str:
        """根据 isSerious 字段判断监控类型"""
        is_serious = item.get("isSerious", 0)

        if is_serious == 1:
            return "严重异动"
        else:
            return "重点监控"

    def _fetch_with_selenium(self, trade_date: str) -> List[Dict]:
        """
        使用 Selenium 模拟浏览器（备用方案）

        Args:
            trade_date: 交易日期

        Returns:
            监控名单列表
        """
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait

            logger.info("启动 Chrome 浏览器（无头模式）...")

            # 配置 Chrome 选项
            chrome_options = Options()
            chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument(f"--user-agent={random.choice(USER_AGENTS)}")

            driver = webdriver.Chrome(options=chrome_options)

            try:
                # 访问页面
                driver.get(self.base_url)

                # 等待动态内容加载
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "stock-item"))
                )

                # 获取渲染后的 HTML
                html = driver.page_source

                # 解析 HTML
                result = self._parse_response(html, trade_date)

                logger.info(f"Selenium 获取成功：{len(result)}只")
                return result

            finally:
                driver.quit()

        except ImportError:
            logger.warning("Selenium 未安装，跳过模拟浏览器方案")
            return []
        except Exception as e:
            logger.error(f"Selenium 获取失败：{e}")
            return []

    def _parse_response(self, html: str, trade_date: str) -> List[Dict]:
        """
        解析 HTML 响应

        Args:
            html: HTML 内容
            trade_date: 交易日期

        Returns:
            监控名单列表
        """
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")

            result = []

            # 查找监控股票列表（根据实际 HTML 结构调整选择器）
            # 假设格式：<div class="stock-item">股票名称 (代码) - 监控类型</div>
            stock_items = soup.find_all("div", class_="stock-item")

            if not stock_items:
                # 尝试其他可能的选择器
                stock_items = soup.find_all("li", class_="stock")

            if not stock_items:
                # 尝试查找所有包含股票代码的元素
                stock_items = soup.find_all(
                    string=lambda t: t and ("sz" in t.lower() or "sh" in t.lower())
                )

            for item in stock_items:
                try:
                    # 提取股票信息
                    text = (
                        item.get_text().strip()
                        if hasattr(item, "get_text")
                        else str(item).strip()
                    )

                    # 解析股票代码和名称
                    stock_info = self._parse_stock_text(text)

                    if stock_info:
                        stock_info["trade_date"] = trade_date
                        stock_info["crawl_time"] = datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                        result.append(stock_info)

                except Exception as e:
                    logger.debug(f"解析单条数据失败：{e}")
                    continue

            # 去重
            result = self._deduplicate(result)

            logger.info(f"解析完成：{len(result)}只（去重后）")
            return result

        except Exception as e:
            logger.error(f"解析 HTML 失败：{e}")
            return []

    def _parse_stock_text(self, text: str) -> Optional[Dict]:
        """
        解析股票文本

        Args:
            text: 股票文本

        Returns:
            股票信息字典
        """
        import re

        # 匹配股票代码和名称
        # 格式示例："贵州茅台 (600519) - 严重异动" 或 "宁德时代 300750 重点监控"
        patterns = [
            r"(?P<name>[\u4e00-\u9fa5]+)\s*[\(\(]?(?P<code>\d{6})[\)\)]?\s*[-：:]\s*(?P<type>[\u4e00-\u9fa5]+)",
            r"(?P<code>\d{6})\s*(?P<name>[\u4e00-\u9fa5]+)\s*[-：:]\s*(?P<type>[\u4e00-\u9fa5]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                code = match.group("code")
                name = match.group("name").strip()
                monitor_type = match.group("type").strip()

                # 标准化监控类型
                if "严重" in monitor_type or "异动" in monitor_type:
                    monitor_type = "严重异动"
                elif "重点" in monitor_type or "监控" in monitor_type:
                    monitor_type = "重点监控"
                else:
                    monitor_type = "其他"

                return {
                    "stock_code": code,
                    "stock_name": name,
                    "monitor_type": monitor_type,
                    "trigger_rule": "",  # 触发规则（从页面提取）
                    "trigger_threshold": 0,  # 触发阈值
                    "status": "监控中",
                }

        return None

    def _deduplicate(self, data: List[Dict]) -> List[Dict]:
        """
        去重（保留最新数据）

        Args:
            data: 原始数据

        Returns:
            去重后数据
        """
        seen = set()
        result = []

        for item in data:
            key = f"{item['stock_code']}_{item['monitor_type']}"
            if key not in seen:
                seen.add(key)
                result.append(item)

        logger.info(f"去重：{len(data)} → {len(result)}")
        return result

    def fetch_and_save(self, trade_date: str = None) -> Dict:
        """
        标准接口：获取并保存重点监控名单

        Args:
            trade_date: 交易日期（格式：YYYY-MM-DD，默认今天）

        Returns:
            {
                'success': True/False,
                'count': 实际采集数量,
                'total': 实际采集数量（A 类统计）,
                'data': 监控名单列表
            }
        """
        try:
            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            logger.info("=" * 60)
            logger.info(f"🍎 {self.__class__.__name__} 开始采集")
            logger.info("=" * 60)

            # 采集数据
            data = self.fetch(trade_date)

            # 保存到数据库
            self.save_to_db(data, trade_date)

            # 统计数量
            actual_count = len(data)

            result = {
                "success": True,
                "count": actual_count,
                "total": actual_count,  # A 类统计
                "data": data,
            }

            logger.info(f"✅ {self.__class__.__name__} 采集完成：{actual_count}只")
            logger.info("=" * 60)
            return result

        except Exception as e:
            logger.error(f"❌ {self.__class__.__name__} 采集异常：{e}")
            logger.info("=" * 60)
            return {"success": False, "count": 0, "total": 0, "data": []}

    def save_to_db(self, data: List[Dict], trade_date: str = None):
        """
        保存到数据库（覆盖当天数据）

        Args:
            data: 监控名单列表
            trade_date: 交易日期（默认今天）
        """
        if not data:
            logger.warning("数据为空，跳过保存")
            return

        try:
            import sqlite3

            from system.config.settings import DATABASE_PATH

            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()

            # 删除当天数据（覆盖写入）
            cursor.execute(
                "DELETE FROM stock_monitor WHERE trade_date = ?", (trade_date,)
            )
            conn.commit()
            logger.info(f"已删除 {trade_date} 的旧数据")

            # 插入新数据（使用 INSERT OR REPLACE 避免 UNIQUE 冲突）
            insert_count = 0
            for stock in data:
                try:
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO stock_monitor (
                            trade_date, stock_code, stock_name,
                            monitor_type, trigger_rule, trigger_threshold,
                            status, crawl_time, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            trade_date,
                            stock["stock_code"],
                            stock["stock_name"],
                            stock["monitor_type"],
                            stock.get("trigger_rule", ""),
                            stock.get("trigger_threshold", 0),
                            stock.get("status", "监控中"),
                            stock.get("crawl_time", ""),
                            datetime.now(),
                        ),
                    )
                    insert_count += 1
                except Exception as e:
                    logger.warning(f"保存 {stock['stock_name']} 失败：{e}")

            conn.commit()
            conn.close()

            logger.info(f"✅ 保存到数据库成功：{insert_count}条")

        except Exception as e:
            logger.error(f"保存到数据库失败：{e}")

    def print_summary(self, data: List[Dict]):
        """打印数据统计"""
        if not data:
            print("无数据")
            return

        # 按监控类型分组
        from collections import Counter

        type_count = Counter(item["monitor_type"] for item in data)

        print("\n【重点监控名单】")
        print(f"{'监控类型':<12} {'数量':>6}")
        print("-" * 20)
        for monitor_type, count in type_count.items():
            print(f"{monitor_type:<12} {count:>6}")

        print(f"\n总计：{len(data)}只")

        if data:
            print("\n【监控股票列表】")
            print(f"{'股票名称':<12} {'股票代码':>10} {'监控类型':<12}")
            print("-" * 36)
            for stock in data[:20]:  # 只显示前 20 只
                print(
                    f"{stock['stock_name']:<12} {stock['stock_code']:>10} {stock['monitor_type']:<12}"
                )

            if len(data) > 20:
                print(f"... 还有 {len(data) - 20} 只")


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("股票异动监控采集器 - 测试运行")
    print("=" * 60)

    try:
        collector = StockMonitorCollector()
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
