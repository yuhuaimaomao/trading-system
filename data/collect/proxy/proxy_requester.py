"""
代理 + 伪装请求基类

职责：代理 IP 获取、请求头伪装（UA/ClientHints/TLS）、单次请求、带重试的请求
不涉及分页/缓存/DB 保存。

所有需要走东方财富 API 的采集器都可继承此类。
"""

import random
import time
from datetime import datetime
from typing import Dict, Optional

from curl_cffi import requests as curl_requests

from data.collect.proxy.proxy_manager import ProxyManager
from system.config.proxy_config import REQUEST_TIMEOUT
from system.utils.logger import get_collect_logger

try:
    from data.collect.proxy.ip_stats import record_ip_usage

    IP_STATS_ENABLED = True
except ImportError:
    record_ip_usage = None
    IP_STATS_ENABLED = False

logger = get_collect_logger("proxy")

# ==================== UA 伪装池（18 个 Profile）====================
# 每个 profile 四元组: UA + Client Hints + impersonate 绑定一致
# curl_cffi 0.14 可用: chrome120/123/124/131, edge101, safari17_0

UA_PROFILES = [
    # ── Chrome 131 macOS ──
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="131", "Not.A/Brand";v="8", "Chromium";v="131"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "chrome131",
    },
    # ── Chrome 124 macOS ──
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="124", "Not.A/Brand";v="8", "Chromium";v="124"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "chrome124",
    },
    # ── Chrome 123 macOS ──
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="123", "Not.A/Brand";v="8", "Chromium";v="123"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "chrome123",
    },
    # ── Chrome 120 macOS ──
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="120", "Not.A/Brand";v="8", "Chromium";v="120"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "chrome120",
    },
    # ── Edge macOS ──
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
        "sec_ch_ua": '"Microsoft Edge";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "edge101",
    },
    # ── Safari 17.0 macOS (3 个) ──
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "sec_ch_ua": '"Not/A)Brand";v="8", "Chromium";v="130", "Safari";v="22"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "safari17_0",
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
        "sec_ch_ua": '"Not/A)Brand";v="8", "Chromium";v="130", "Safari";v="22"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "safari17_0",
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "sec_ch_ua": '"Not/A)Brand";v="8", "Chromium";v="130", "Safari";v="22"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"macOS"',
        "impersonate": "safari17_0",
    },
]

# 兼容旧代码：纯 UA 字符串列表
USER_AGENTS = [p["ua"] for p in UA_PROFILES]

# ==================== 请求头素材池 ====================

ACCEPT_POOL = [
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "application/json, text/plain, */*",
    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
]

ACCEPT_LANGUAGE_POOL = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9,en-US;q=0.8",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.9,en;q=0.7,ja;q=0.5",
    "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7",
]

ACCEPT_ENCODING_POOL = [
    "gzip, deflate, br, zstd",
    "gzip, deflate, br",
    "gzip, deflate",
    "gzip, deflate, br, zstd, identity",
]


# 启动时验证所有 profile 的 UA / sec-ch-ua 版本号一致性
# 如果这里报错，说明有人加了版本号不一致的 profile
for _p in UA_PROFILES:
    _ua = _p["ua"]
    _ch = _p["sec_ch_ua"]
    if "Chrome/" in _ua and "Chromium" in _ch:
        _v = _ua.split("Chrome/")[1].split(".")[0]
        assert f'v="{_v}"' in _ch, f"UA Chrome/{_v} 与 sec-ch-ua {_ch} 不匹配"
    if "Edg/" in _ua and "Edge" in _ch:
        _v = _ua.split("Edg/")[1].split(".")[0]
        assert f'v="{_v}"' in _ch, f"UA Edg/{_v} 与 sec-ch-ua {_ch} 不匹配"


class ProxyRequester:
    """代理 + 伪装请求基类

    子类可直接调用 _request_with_retry() 发送 HTTP 请求，
    代理获取、头伪装、失败重试均封装在内。
    """

    MAX_PROXY_FAILS = 3
    USE_JSONP = True  # 加 cb + _ 参数模拟 jQuery AJAX，降低被反爬识别概率

    def __init__(self, trade_date: str = None, collector_name: str = None):
        self.trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        self.collector_name = collector_name or self.__class__.__name__

        self.pm = ProxyManager(
            trade_date=self.trade_date,
            collector_name=self.collector_name,
        )

        self._page_seq = 0

        # 日志：子类（ProxyBaseCollector）会用 get_collector_logger 覆盖
        self.logger = logger

    # ── 代理 ──

    def _get_proxy(self) -> Optional[dict]:
        p = self.pm.get_proxy()
        if p:
            self.logger.debug(f"代理: {p['http']}")
        else:
            self.logger.error("无可用代理")
        return p

    @staticmethod
    def _extract_ip_port(proxy: dict) -> tuple:
        addr = proxy.get("http", "").replace("http://", "")
        if ":" in addr:
            ip, port = addr.split(":", 1)
            return ip, int(port)
        return addr, 0

    def _record_ip(self, proxy: dict, page: int, status: str, error: str = None):
        if not IP_STATS_ENABLED or not record_ip_usage:
            return
        try:
            ip, port = self._extract_ip_port(proxy)
            record_ip_usage(
                ip=ip,
                port=port,
                trade_date=self.trade_date,
                collector_name=self.collector_name,
                page=page,
                status=status,
                error=error,
            )
        except Exception:
            pass

    # ── 请求头构建 ──

    def _build_headers_from_profile(
        self, profile: dict, referer: str = "", api_call: bool = True
    ) -> Dict:
        """用指定 profile 构建 headers（不随机选 profile）

        Args:
            profile: UA_PROFILES 中的一项
            referer: Referer URL
            api_call: True=API 请求，False=页面导航
        """
        if api_call:
            accept = random.choice(ACCEPT_POOL)
            headers = {
                "User-Agent": profile["ua"],
                "Accept": accept,
                "Accept-Language": random.choice(ACCEPT_LANGUAGE_POOL),
                "Accept-Encoding": random.choice(ACCEPT_ENCODING_POOL),
                "Connection": "keep-alive",
                "sec-ch-ua": profile["sec_ch_ua"],
                "sec-ch-ua-mobile": profile["sec_ch_ua_mobile"],
                "sec-ch-ua-platform": profile["sec_ch_ua_platform"],
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
            }
        else:
            # 预热：固定浏览器导航型 Accept，模拟真实首次访问
            headers = {
                "User-Agent": profile["ua"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "sec-ch-ua": profile["sec_ch_ua"],
                "sec-ch-ua-mobile": profile["sec_ch_ua_mobile"],
                "sec-ch-ua-platform": profile["sec_ch_ua_platform"],
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }

        if referer:
            headers["Referer"] = referer

        return headers

    # ── 请求执行 ──

    def _request(
        self,
        url: str,
        params: dict,
        headers: dict,
        proxy: dict,
        impersonate: str,
        timeout: int = None,
        session=None,
    ) -> Optional[dict]:
        """单次 HTTP GET，返回解析后的 JSON 或 None

        Args:
            session: 可选的外部 Session（用于预热+API 同一连接）。
                     如果为 None，则内部创建临时 Session。
        """
        own_session = session is None
        if own_session:
            s = curl_requests.Session()
            s.trust_env = False
        else:
            s = session

        timeout = timeout or REQUEST_TIMEOUT
        try:
            resp = s.get(
                url,
                params=params,
                headers=headers,
                proxies=proxy,
                impersonate=impersonate,
                timeout=timeout,
            )
            if resp.status_code != 200:
                self.logger.warning(f"HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            text = resp.text
            # JSONP 响应检测：东财支持 cb 回调参数，返回 jQuery...(...) 格式
            if text.startswith("jQuery"):
                left = text.find("(") + 1
                right = text.rfind(")")
                if left > 0 and right > left:
                    import json as _json

                    return _json.loads(text[left:right])
            return resp.json()
        except Exception as e:
            self.logger.warning(f"请求失败: {type(e).__name__}: {e}")
            return None
        finally:
            if own_session:
                s.close()

    def _request_with_retry(
        self,
        url: str,
        params: dict,
        referer: str = "",
        desc: str = "",
        timeout: int = None,
    ) -> Optional[dict]:
        """请求 + 自动换代理重试（最多 MAX_PROXY_FAILS 次）

        Returns:
            API 返回的完整 JSON dict，或 None（全部代理失败）
        """
        self._page_seq += 1
        page = self._page_seq
        current_proxy = self._get_proxy()
        if not current_proxy:
            return None

        for attempt in range(1, self.MAX_PROXY_FAILS + 1):
            # 创建新 Session（每次 attempt 独立）
            s = curl_requests.Session()
            s.trust_env = False

            # 随机选择 UA profile
            profile = random.choice(UA_PROFILES)
            impersonate = profile["impersonate"]

            headers = self._build_headers_from_profile(profile, referer, api_call=True)

            # JSONP 参数：模拟 jQuery AJAX（cb + _），降低被反爬识别概率
            _api_params = dict(params)  # 不修改调用方的 dict
            if self.USE_JSONP:
                _api_params["cb"] = (
                    f"jQuery{random.randint(1000000000000, 9999999999999)}_{int(time.time() * 1000)}"
                )
                _api_params["_"] = str(int(time.time() * 1000))

            self.logger.debug(
                f"{desc} 第{attempt}次尝试 impersonate={impersonate} proxy={current_proxy['http']}"
            )

            # ── Cookie 预热（访问东财主页获取 st_si/st_asi 等 Cookie）──
            try:
                warmup_headers = self._build_headers_from_profile(
                    profile, api_call=False
                )
                s.get(
                    "https://quote.eastmoney.com/",
                    headers=warmup_headers,
                    proxies=current_proxy,
                    impersonate=impersonate,
                    timeout=8,
                )
                self.logger.debug(f"Cookie 预热成功: {list(s.cookies.keys())}")
            except Exception:
                self.logger.debug("Cookie 预热失败，跳过（不影响主流程）")

            # ── 主请求 ──
            result = self._request(
                url,
                _api_params,
                headers,
                current_proxy,
                impersonate,
                timeout,
                session=s,
            )
            s.close()

            if result is not None:
                self._record_ip(current_proxy, page, "success")
                # 记录返回的数据量
                data = result.get("data")
                if data:
                    total = data.get("total", 0)
                    diff = data.get("diff", [])
                    cnt = len(diff) if diff else total
                    self.logger.debug(f"{desc} 成功: {cnt}条")
                return result

            self._record_ip(current_proxy, page, "failed", "connection_error")
            self.logger.warning(
                f"{desc} 代理失败 ({attempt}/{self.MAX_PROXY_FAILS})，切换 IP"
            )

            if attempt < self.MAX_PROXY_FAILS:
                time.sleep(0.5)
                current_proxy = self._get_proxy()
                if not current_proxy:
                    break

        self.logger.error(f"{desc} 尝试{self.MAX_PROXY_FAILS}个代理均失败")
        return None
