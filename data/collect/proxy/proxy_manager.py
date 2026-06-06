"""
代理管理器

功能：
- 从天启 API 获取代理 IP
- 记录获取日志
"""

from datetime import datetime
from typing import Dict, Optional

import requests

from system.config.proxy_config import (
    PROXY_TIMEOUT,
    TIANQI_API_PARAMS,
    TIANQI_API_URL,
)
from system.utils.logger import get_collector_logger

# IP 统计（可选导入）
try:
    from data.collect.proxy.ip_stats import record_ip_usage

    IP_STATS_ENABLED = True
except ImportError:
    record_ip_usage = None
    IP_STATS_ENABLED = False


# ==================== 代理管理器类 ====================


class ProxyManager:
    """代理 IP 管理器"""

    def __init__(self, trade_date: str = None, collector_name: str = None):
        """
        初始化代理管理器

        Args:
            trade_date: 交易日期（用于 IP 统计）
            collector_name: 采集器名称（用于 IP 统计）
        """
        self.logger = get_collector_logger("proxy_manager")
        self.trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        self.collector_name = collector_name or "unknown"

        self.logger.debug(
            f"代理管理器初始化: collector={self.collector_name} timeout={PROXY_TIMEOUT}s"
        )

    def get_proxy(self) -> Optional[Dict[str, str]]:
        """
        从天启 API 获取一个代理 IP

        Returns:
            proxies 字典：{"http": "http://ip:port", "https": "http://ip:port"}
            或 None（获取失败）

        天启 API 返回格式：
        [
            {
                "ip": "123.45.67.89",
                "port": 8080,
                ...
            }
        ]
        """
        self.logger.debug("-" * 60)
        self.logger.debug("开始从天启 API 获取代理 IP...")
        self.logger.debug(f"请求参数：{TIANQI_API_PARAMS}")

        try:
            # 记录请求时间
            request_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self.logger.debug(f"请求时间：{request_time}")

            # 发送请求
            self.logger.debug(f"发送 GET 请求到 {TIANQI_API_URL}")
            response = requests.get(
                TIANQI_API_URL, params=TIANQI_API_PARAMS, timeout=PROXY_TIMEOUT
            )

            # 记录响应状态
            self.logger.debug(f"响应状态码：{response.status_code}")
            self.logger.debug(f"响应内容：{response.text[:200]}")

            # 检查状态码
            if response.status_code != 200:
                self.logger.error(f"❌ HTTP 状态码异常：{response.status_code}")
                return None

            # 解析 JSON
            try:
                data = response.json()
                self.logger.debug("✅ JSON 解析成功")
                self.logger.debug(f"返回数据类型：{type(data)}")
            except ValueError as e:
                self.logger.error(f"❌ JSON 解析失败：{e}")
                self.logger.error(f"原始响应：{response.text}")
                return None

            # 天启 API 返回格式：{"code":1000,"data":[{...}]} 或 纯列表 [...]
            if isinstance(data, dict):
                self.logger.debug("返回格式：字典格式")
                if data.get("code") != 1000:
                    self.logger.error(f"❌ 天启 API 返回错误码：{data.get('code')}")
                    self.logger.error(f"错误信息：{data}")
                    return None
                ip_list = data.get("data", [])
                self.logger.debug(f"✅ 解析成功，IP 数量：{len(ip_list)}")
            elif isinstance(data, list):
                self.logger.debug("返回格式：列表格式")
                ip_list = data
                self.logger.debug(f"✅ 解析成功，IP 数量：{len(ip_list)}")
            else:
                self.logger.error(f"❌ 返回格式异常：{type(data)}")
                return None

            # 检查是否为空
            if not ip_list or len(ip_list) == 0:
                self.logger.warning("⚠️ 天启 API 返回空数据")
                return None

            # 解析第一个 IP
            ip_info = ip_list[0]
            ip = ip_info.get("ip")
            port = ip_info.get("port")

            self.logger.debug(f"解析 IP 信息：{ip_info}")

            if not ip or not port:
                self.logger.error(f"❌ IP 信息格式异常：{ip_info}")
                return None

            # 构建 proxies 字典
            proxy_url = f"http://{ip}:{port}"
            proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }

            # 记录成功
            self.logger.debug("=" * 60)
            self.logger.debug(f"✅ 获取到代理 IP: {ip}:{port}")
            self.logger.debug(f"代理地址：{proxy_url}")
            self.logger.debug(f"proxies 字典：{proxies}")
            self.logger.debug("=" * 60)

            # 记录 IP 使用统计
            if IP_STATS_ENABLED and record_ip_usage:
                try:
                    record_ip_usage(
                        ip=ip,
                        port=int(port),
                        trade_date=self.trade_date,
                        collector_name=self.collector_name,
                        page=0,  # 获取 IP 时还不知道页码
                        status="success",
                        error=None,
                    )
                except Exception as e:
                    self.logger.debug(f"IP 统计记录失败：{e}")

            return proxies

        except requests.exceptions.Timeout:
            self.logger.error(f"❌ 天启 API 请求超时（>{PROXY_TIMEOUT}秒）")
            return None
        except requests.exceptions.RequestException as e:
            self.logger.error(f"❌ 天启 API 请求失败：{e}")
            self.logger.error(f"异常类型：{type(e).__name__}")
            return None
        except Exception as e:
            self.logger.error(f"❌ 获取代理 IP 异常：{e}")
            self.logger.error(f"异常类型：{type(e).__name__}")
            import traceback

            self.logger.error(f"堆栈跟踪：{traceback.format_exc()}")
            return None

    def test_proxy(
        self, proxies: Dict[str, str], test_url: str = "https://www.baidu.com"
    ) -> bool:
        """
        测试代理 IP 是否可用

        Args:
            proxies: 代理字典
            test_url: 测试 URL

        Returns:
            bool: 是否可用
        """
        self.logger.debug("-" * 60)
        self.logger.debug(f"开始测试代理 IP: {proxies.get('http')}")
        self.logger.debug(f"测试 URL: {test_url}")

        try:
            response = requests.get(test_url, proxies=proxies, timeout=10)

            self.logger.debug(f"响应状态码：{response.status_code}")

            if response.status_code == 200:
                self.logger.debug("✅ 代理 IP 测试成功")
                return True
            else:
                self.logger.warning(f"⚠️ 代理 IP 测试失败：HTTP {response.status_code}")
                return False

        except Exception as e:
            self.logger.error(f"❌ 代理 IP 测试失败：{e}")
            return False


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("代理管理器测试")
    print("=" * 60)

    manager = ProxyManager()
    proxy = manager.get_proxy()

    if proxy:
        print(f"\n✅ 获取成功：{proxy}")

        # 测试代理
        test_result = manager.test_proxy(proxy)
        print(f"代理测试结果：{'✅ 可用' if test_result else '❌ 不可用'}")
    else:
        print("\n❌ 获取失败")
