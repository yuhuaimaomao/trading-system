"""
代理版采集器基类

功能：
- 代理 IP 管理
- 自动重试 + IP 切换
- 分页采集
- 数据库保存
- 详细日志
"""

import json
import logging
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# ==================== 配置导入 ====================

try:
    from system.config.proxy_config import (
        CACHE_DIR,
        CACHE_ENABLED,
        CACHE_EXPIRE_HOURS,
        MAX_RETRIES,
        REQUEST_TIMEOUT,
        RETRY_DELAY,
    )
    from system.utils.logger import get_collect_logger
except ImportError:
    from proxy_config import (
        CACHE_DIR,
        CACHE_ENABLED,
        CACHE_EXPIRE_HOURS,
        MAX_RETRIES,
        REQUEST_TIMEOUT,
        RETRY_DELAY,
    )

    get_collect_logger = None

from curl_cffi import requests as curl_requests

from data.collect.proxy.proxy_requester import (
    UA_PROFILES,
    ProxyRequester,
)

# ==================== 基类定义 ====================


class ProxyBaseCollector(ProxyRequester):
    """代理版采集器基类 — 继承伪装/代理能力，增加分页+缓存+DB保存"""

    # ===== 子类必须配置 =====
    API_URL: str = ""
    API_PARAMS: Dict = {}
    PAGE_SIZE: int = 100
    REFERER_URL: str = ""
    FIELD_MAP: Dict = {}

    # ===== 数据库配置 =====
    TABLE_NAME: str = ""
    DATABASE_PATH: str = ""

    # ===== 缓存配置 =====
    CACHE_FILE: str = ""

    def __init__(
        self,
        logger_name: str = "ProxyBaseCollector",
        trade_date: str = None,
        task_mgr=None,
    ):
        self.task_mgr = task_mgr
        self.collector_name = self.TABLE_NAME

        # 初始化代理/伪装基类
        super().__init__(
            trade_date=trade_date,
            collector_name=self.collector_name or logger_name,
        )

        # 日志系统
        if get_collect_logger:
            self.logger = get_collect_logger(
                collector_name=self.collector_name or logger_name,
                trade_date=self.trade_date,
            )
        else:
            self.logger = logging.getLogger(logger_name)

        # 确保目录存在
        self._ensure_dirs()

        # 数据库路径
        if not self.DATABASE_PATH:
            from system.config.settings import DATABASE_PATH

            self.DATABASE_PATH = DATABASE_PATH

        # 缓存文件路径（带交易日期）
        if not self.CACHE_FILE:
            self.CACHE_FILE = f"{CACHE_DIR}/{self.TABLE_NAME}_{self.trade_date}.json"

        # 缓存数据
        self.cache_data = {
            "trade_date": self.trade_date,
            "status": "incomplete",
            "total_pages": 0,
            "completed_pages": 0,
            "failed_pages": [],
            "updated_at": "",
            "data": [],
        }

        self.logger.info("=" * 60)
        self.logger.info(f"{self.__class__.__name__} 初始化完成")
        self.logger.info(f"API URL: {self.API_URL}")
        self.logger.info(f"数据库路径：{self.DATABASE_PATH}")
        self.logger.info(f"表名：{self.TABLE_NAME}")
        self.logger.info(f"交易日期：{self.trade_date}")
        self.logger.info(f"缓存文件：{self.CACHE_FILE}")
        if self.task_mgr:
            self.logger.info("任务状态：已关联")
        self.logger.info("=" * 60)

    def _ensure_dirs(self):
        """确保目录存在"""
        Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

    # _build_headers_from_profile() 由 ProxyRequester 提供

    def _fetch_page(self, page: int, proxy: Dict) -> Optional[Dict]:
        """获取单页数据（单次请求，重试由 fetch_all 统一控制）"""
        self.logger.info(f"请求第 {page} 页...")

        # 随机选择 UA profile
        profile = random.choice(UA_PROFILES)
        impersonate = profile["impersonate"]
        headers = self._build_headers_from_profile(
            profile, referer=self.REFERER_URL, api_call=True
        )

        params = self.API_PARAMS.copy()
        params["pn"] = str(page)
        params["pz"] = str(self.PAGE_SIZE)

        # JSONP 参数
        if self.USE_JSONP:
            params["cb"] = (
                f"jQuery{random.randint(1000000000000, 9999999999999)}_{int(time.time() * 1000)}"
            )
            params["_"] = str(int(time.time() * 1000))

        self.logger.info(
            f"请求参数：pn={page}, pz={self.PAGE_SIZE}, impersonate={impersonate}"
        )

        # 创建 Session + Cookie 预热
        s = curl_requests.Session()
        s.trust_env = False

        try:
            warmup_headers = self._build_headers_from_profile(profile, api_call=False)
            s.get(
                "https://quote.eastmoney.com/",
                headers=warmup_headers,
                proxies=proxy,
                impersonate=impersonate,
                timeout=8,
            )
        except Exception:
            pass  # 预热失败不影响主流程

        try:
            data = self._request(
                self.API_URL,
                params,
                headers,
                proxy,
                impersonate,
                REQUEST_TIMEOUT,
                session=s,
            )
        finally:
            s.close()

        if data is None:
            self._record_ip(proxy, page, "failed", "request_failed")
            return None

        result = None
        if data.get("data") and data["data"].get("diff"):
            result = data["data"]["diff"]
        elif data.get("result") and data["result"].get("data"):
            result = data["result"]["data"]

        if result is None:
            keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            self.logger.error(f"❌ 数据格式异常，响应 keys={keys}")
            self._record_ip(proxy, page, "failed", "empty_data")
            return None

        self.logger.info(f"✅ 数据提取成功，记录数量：{len(result)}")
        self._record_ip(proxy, page, "success")
        return data

    def fetch_all(self, max_retries: int = MAX_RETRIES) -> Optional[List[Dict]]:
        """采集所有数据（分页 + 缓存 + 任务状态）"""
        self.logger.info("=" * 60)
        self.logger.info(f"开始采集 {self.__class__.__name__}")
        self.logger.info(f"交易日期：{self.trade_date}")
        self.logger.info(f"每页数量：{self.PAGE_SIZE}")
        self.logger.info("=" * 60)

        # 1. 初始化任务状态
        if self.task_mgr:
            self.task_mgr.init_collector(self.collector_name)
            self.task_mgr.set_running(self.collector_name)
            self.logger.info(f"📋 任务状态：{self.collector_name} = running")

        # 2. 读取缓存
        self.logger.info("步骤 1: 读取缓存")
        self.cache_data = self._load_cache()

        # 3. 检查缓存有效性
        if not self._is_cache_valid():
            self.logger.info("缓存无效，从头开始采集")
            self.cache_data = {
                "trade_date": self.trade_date,
                "status": "incomplete",
                "total_pages": 0,
                "completed_pages": 0,
                "failed_pages": [],
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": [],
            }
        else:
            completed = self.cache_data.get("completed_pages", 0)
            total_pages = self.cache_data.get("total_pages", 0)
            failed_pages = self.cache_data.get("failed_pages", [])

            # 检查是否已经完成
            if total_pages > 0 and completed >= total_pages:
                if not failed_pages:
                    self.logger.info(
                        f"✅ 缓存已完成（{completed}/{total_pages}页），直接返回"
                    )
                    return self.cache_data["data"]
                else:
                    self.logger.info(
                        f"⚠️ 缓存已完成，但有{len(failed_pages)}页失败：{failed_pages}"
                    )

            # 显示进度
            if total_pages > 0:
                if failed_pages:
                    self.logger.info(
                        f"✅ 缓存有效，已采集{completed}/{total_pages}页，{len(failed_pages)}页失败"
                    )
                else:
                    self.logger.info(
                        f"✅ 缓存有效，已从第 {completed + 1} 页/共 {total_pages} 页继续"
                    )
            else:
                self.logger.info(f"✅ 缓存有效，已从第 {completed + 1} 页继续")

        start_time = datetime.now()
        self.logger.info(
            f"开始时间：{start_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}"
        )

        current_proxy = None
        success = True

        # 从缓存读取总页数（如果有）
        total_pages = self.cache_data.get("total_pages", 0)
        completed_pages = self.cache_data.get("completed_pages", 0)
        failed_pages = self.cache_data.get("failed_pages", [])

        if total_pages > 0:
            self.logger.info(f"总页数：{total_pages} (缓存)")

        # 恢复场景：先重试失败页（不等到主循环结束后），再继续未采集页
        if failed_pages and completed_pages > 0:
            self.logger.info(
                f"🔄 恢复模式：先重试 {len(failed_pages)} 个失败页 {failed_pages}"
            )
            for fp in failed_pages[:]:
                self._retry_single_page(fp)

        # 从已采集页+1开始，不重复采第1页
        page = max(1, self.cache_data.get("completed_pages", 0) + 1)
        while True:
            # 检查是否已采集完所有页
            total_pages = self.cache_data.get("total_pages", 0)
            if total_pages > 0 and page > total_pages:
                self.logger.info(f"✅ 已采集完所有{total_pages}页")
                break

            self.logger.info("=" * 60)
            self.logger.info(f"采集第 {page} 页")
            self.logger.info("=" * 60)

            if not current_proxy:
                self.logger.info("步骤 1: 获取代理 IP")
                current_proxy = self._get_proxy()
                if not current_proxy:
                    self.logger.error("❌ 获取代理失败")
                    success = False
                    break

            self.logger.info("步骤 2: 获取单页数据")
            page_data = None

            # 单页重试逻辑：第 1 页用 FIRST_PAGE_MAX_RETRIES（默认= max_retries），其他页用 max_retries
            page_max_retries = (
                getattr(self, "FIRST_PAGE_MAX_RETRIES", max_retries)
                if page == 1
                else max_retries
            )
            for attempt in range(1, page_max_retries + 1):
                page_data = self._fetch_page(page, current_proxy)

                if page_data and (
                    page_data.get("data", {}).get("diff")
                    or page_data.get("result", {}).get("data")
                ):
                    self.logger.info(f"✅ 第 {page} 页获取成功（第{attempt}次尝试）")
                    break
                else:
                    self.logger.warning(f"⚠️ 第 {page} 页第{attempt}次重试失败")
                    if attempt < page_max_retries:
                        self.logger.info(f"等待 {RETRY_DELAY} 秒后切换 IP 重试...")
                        time.sleep(RETRY_DELAY)
                        current_proxy = self._get_proxy()

            # 检查是否所有重试都失败了
            if not page_data:
                # 第 1 页失败 → 拿不到 total 无法分页，跳过整个采集器
                if page == 1:
                    self.logger.error(
                        f"❌ 第 1 页采集失败（已重试{page_max_retries}次），跳过此采集器"
                    )
                    # 重置缓存，避免下次从损坏的缓存恢复
                    self.cache_data = {
                        "trade_date": self.trade_date,
                        "status": "incomplete",
                        "total_pages": 0,
                        "completed_pages": 0,
                        "failed_pages": [],
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "data": [],
                    }
                    self._save_cache()
                    success = False
                    break

                # 非第 1 页失败 → 记录失败页，继续下一页
                self.logger.error(
                    f"❌ 第 {page} 页采集失败（已重试{max_retries}次），跳过并继续第{page + 1}页"
                )
                if "failed_pages" not in self.cache_data:
                    self.cache_data["failed_pages"] = []
                # 避免重复记录
                if page not in self.cache_data["failed_pages"]:
                    self.cache_data["failed_pages"].append(page)
                    self._save_cache()
                page += 1
                continue

            # 追加数据到缓存
            data_section = page_data.get("data") or {}
            result_section = page_data.get("result") or {}
            diff_list = data_section.get("diff") or result_section.get("data", [])

            # 记录每页 total（末页用于数据源变动校验）
            page_total = data_section.get("total")
            if page_total is not None:
                self.cache_data["last_page_total"] = page_total

            # 如果数据为空，说明已是最后一页
            if not diff_list:
                self.logger.info("✅ 已是最后一页（API 返回空数据），采集完成")
                break

            # 第 1 页采集后，智能判断总页数
            if page == 1 and data_section.get("total") is not None:
                total = data_section.get("total", 0)

                # 如果 total=0，说明没有数据，直接停止
                if total <= 0:
                    self.logger.info("📊 共 0 条数据，采集完成")
                    break

                # 保存 total 到缓存
                self.cache_data["total"] = total

                # 有数据，计算总页数
                total_pages = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE
                self.cache_data["total_pages"] = total_pages
                # 更新循环上限
                max_pages = total_pages
                self.logger.info(
                    f"📊 共 {total} 条数据，共 {total_pages} 页（动态调整上限）"
                )

                # 如果只有 1 页，直接停止
                if total_pages == 1:
                    self.logger.info("✅ 只有 1 页，采集完成")
                    break

            self.cache_data["data"].extend(diff_list)

            # 更新 completed_pages：记录当前采集完成的页码（不管中间有没有失败页）
            self.cache_data["completed_pages"] = page

            self.cache_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 保存到缓存
            self._save_cache()

            self.logger.info(f"✅ 第 {page} 页：{len(diff_list)} 条数据")
            self.logger.info(f"累计数据：{len(self.cache_data['data'])} 条")

            # 判断是否已是最后一页
            total_pages = self.cache_data.get("total_pages", 0)
            if total_pages > 0:
                self.logger.info(f"进度：{page}/{total_pages} 页")

            if len(diff_list) < self.PAGE_SIZE:
                self.logger.info("✅ 已是最后一页，采集完成")
                break

            # 如果已采集完所有页数，停止采集
            if total_pages > 0 and page >= total_pages:
                self.logger.info(f"✅ 已采集完所有 {total_pages} 页，采集完成")
                break

            # 准备采集下一页
            page += 1

            # 反爬延时
            delay = random.uniform(1, 3)
            self.logger.info(f"等待 {delay:.1f} 秒（反爬）...")
            time.sleep(delay)

        # 多轮重试失败页（最多 retry_rounds 轮）
        retry_rounds = 2  # 最多重试 2 轮
        for round_num in range(2, retry_rounds + 1):
            failed_pages = self.cache_data.get("failed_pages", [])
            if not failed_pages:
                self.logger.info(f"✅ 所有失败页已在第{round_num - 1}轮重试成功")
                break

            self.logger.warning(f"\n{'=' * 60}")
            self.logger.warning(
                f"第{round_num - 1}轮结束，{len(failed_pages)}页采集失败，开始第{round_num}轮重试..."
            )
            self.logger.warning(f"{'=' * 60}\n")

            # 复制失败页列表，避免遍历中修改
            pages_to_retry = failed_pages[:]
            round_success_count = 0

            for page in pages_to_retry:
                self.logger.info(f"重试第{page}页...")

                success = False
                for retry in range(self.MAX_RETRIES):
                    proxy_dict = self._get_proxy()
                    if not proxy_dict:
                        time.sleep(2)
                        continue

                    page_data = self._fetch_page(page, proxy_dict)

                    if page_data and (
                        page_data.get("data", {}).get("diff")
                        or page_data.get("result", {}).get("data")
                    ):
                        # 解析数据
                        data_section = page_data.get("data") or {}
                        diff_list = data_section.get("diff") or page_data.get(
                            "result", {}
                        ).get("data", [])

                        page_total = data_section.get("total")
                        if page_total is not None:
                            self.cache_data["last_page_total"] = page_total

                        if diff_list:
                            # 追加数据到缓存
                            self.cache_data["data"].extend(diff_list)
                            self.cache_data["completed_pages"] = max(
                                self.cache_data.get("completed_pages", 0), page
                            )
                            self.cache_data["updated_at"] = datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            self._save_cache()

                            self.logger.info(f"第{page}页重试成功：{len(diff_list)}条")

                            # ✅ 从 failed_pages 中移除该页
                            if page in self.cache_data["failed_pages"]:
                                self.cache_data["failed_pages"].remove(page)
                                self._save_cache()
                                self.logger.info(f"✅ 已从缓存中清除第{page}页失败记录")

                            success = True
                            round_success_count += 1
                            break

                    self.logger.warning(f"第{page}页第{retry + 1}次重试失败")
                    time.sleep(
                        self.RETRY_DELAYS[retry]
                        if retry < len(self.RETRY_DELAYS)
                        else 10
                    )

                if not success:
                    self.logger.error(f"❌ 第{page}页重试失败，保留在失败列表中")

            self.logger.info(
                f"第{round_num}轮重试完成：成功{round_success_count}页，失败{len(failed_pages) - round_success_count}页"
            )

        # 数据完整性校验（所有重试轮次结束后）
        if success:
            collected = len(self.cache_data.get("data", []))
            expected = self.cache_data.get("total", 0)
            last_page_total = self.cache_data.get("last_page_total")

            if expected > 0 and collected != expected:
                diff = abs(expected - collected)
                self.logger.warning(
                    f"数据完整性校验：count={collected} != total={expected}（差{diff}条）"
                )

                # 差 < 一页 → 数据源变动，用末页 total 二次校验
                if (
                    diff < self.PAGE_SIZE
                    and last_page_total is not None
                    and collected == last_page_total
                ):
                    self.logger.info(
                        f"数据源变动：首页total={expected} → 末页total={last_page_total}，"
                        f"实际采集={collected}，以末页为准"
                    )
                    self.cache_data["total"] = last_page_total
                else:
                    # 不全量重跑，只针对差异重试失败页
                    self._handle_integrity_gap(expected, collected, total_pages)
                    # 重试后重新校验
                    collected = len(self.cache_data.get("data", []))
                    if collected != expected:
                        diff = abs(expected - collected)
                        self.logger.warning(
                            f"完整性修复后仍不完整：count={collected} != total={expected}（差{diff}条），"
                            f"保留已采集数据，等待手工检查"
                        )

        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        self.logger.info("=" * 60)

        # 更新任务状态
        if self.task_mgr:
            if success:
                self.task_mgr.set_complete(self.collector_name)
                self.logger.info(f"📋 任务状态：{self.collector_name} = complete")
            else:
                self.task_mgr.set_failed(self.collector_name, "采集失败")
                self.logger.info(f"📋 任务状态：{self.collector_name} = failed")

        if success:
            self.logger.info("✅ 采集完成！")
            self.logger.info(
                f"结束时间：{end_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}"
            )
            self.logger.info(f"总耗时：{duration:.2f}秒")
            self.logger.info(f"总数据量：{len(self.cache_data['data'])} 条")
            self.logger.info("=" * 60)

            # 清空缓存
            self.logger.info("步骤 3: 清空缓存")
            self._clear_cache()

            return self.cache_data["data"]
        else:
            self.logger.error("❌ 采集失败")
            return None

    def _handle_integrity_gap(self, expected: int, collected: int, total_pages: int):
        """数据完整性缺口修复 — 只重试失败页，不全量重跑"""
        failed_pages = self.cache_data.get("failed_pages", [])
        diff = expected - collected

        self.logger.warning(
            f"完整性缺口：期望{expected}条，实际{collected}条，差{diff}条，"
            f"失败页={failed_pages or '无'}"
        )

        if failed_pages:
            # 有明确失败页 → 只重试这些页
            self.logger.info(f"对 {len(failed_pages)} 个失败页做第3轮重试...")
            for page in failed_pages[:]:
                self._retry_single_page(page)
        else:
            # 没有记录失败页但数据量不对 → 可能是中间页返回空但没被标记为失败
            # 根据 diff 估算缺失页数，逐页检查是否有数据空洞
            missing_pages_estimate = max(
                1, (diff + self.PAGE_SIZE - 1) // self.PAGE_SIZE
            )
            self.logger.info(
                f"无失败页记录，估计缺失{missing_pages_estimate}页，"
                f"扫描已采集页找空洞..."
            )
            self._retry_missing_gaps(total_pages, expected, collected)

    def _retry_single_page(self, page: int):
        """重试单个页面，成功则追加数据并从 failed_pages 移除"""
        self.logger.info(f"针对第{page}页补充重试...")
        proxy = self._get_proxy()
        if not proxy:
            self.logger.error(f"第{page}页补充重试：获取代理失败")
            return

        page_data = self._fetch_page(page, proxy)
        if not page_data:
            self.logger.error(f"第{page}页补充重试：请求失败")
            return

        data_section = page_data.get("data") or {}
        diff_list = data_section.get("diff") or page_data.get("result", {}).get(
            "data", []
        )

        if diff_list:
            self.cache_data["data"].extend(diff_list)
            if page in self.cache_data.get("failed_pages", []):
                self.cache_data["failed_pages"].remove(page)
            # 更新 completed_pages，防止主循环重复采集该页
            self.cache_data["completed_pages"] = max(
                self.cache_data.get("completed_pages", 0), page
            )
            self.cache_data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_cache()
            self.logger.info(f"✅ 第{page}页补充重试成功：+{len(diff_list)}条")
        else:
            self.logger.warning(f"第{page}页补充重试返回空数据")

    def _retry_missing_gaps(self, total_pages: int, expected: int, collected: int):
        """扫描页范围，找出未采集的页码并重试"""
        if total_pages <= 0:
            return

        for page in range(1, total_pages + 1):
            if page in self.cache_data.get("failed_pages", []):
                continue  # 已在上一步处理

            # 检查是否已完成：completed_pages >= page 说明该页应该已采过
            if page <= self.cache_data.get("completed_pages", 0):
                continue

            # 该页未被采集过 → 重试
            self._retry_single_page(page)

    def _safe_float(self, value, default=0.0):
        """
        安全转换 float，处理空值和 '-'

        Args:
            value: 要转换的值
            default: 默认值（默认 0.0）

        Returns:
            float: 转换后的值，或默认值
        """
        try:
            return float(value) if value and value != "-" else default
        except (ValueError, TypeError):
            return default

    # ==================== 缓存方法 ====================

    def _load_cache(self) -> Dict:
        """读取缓存文件"""
        if not CACHE_ENABLED:
            return {
                "trade_date": self.trade_date,
                "status": "incomplete",
                "total_pages": 0,
                "completed_pages": 0,
                "failed_pages": [],
                "updated_at": "",
                "data": [],
            }

        try:
            if not os.path.exists(self.CACHE_FILE):
                self.logger.info("缓存文件不存在")
                return {
                    "trade_date": self.trade_date,
                    "status": "incomplete",
                    "total_pages": 0,
                    "completed_pages": 0,
                    "failed_pages": [],
                    "updated_at": "",
                    "data": [],
                }

            with open(self.CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)

            # 确保缓存包含 failed_pages 字段
            if "failed_pages" not in cache:
                cache["failed_pages"] = []

            self.logger.info(
                f"✅ 读取缓存成功：{cache.get('completed_pages', 0)}页，{len(cache.get('failed_pages', []))}页失败"
            )
            return cache

        except json.JSONDecodeError:
            self.logger.error("❌ 缓存文件损坏，删除重建")
            try:
                os.remove(self.CACHE_FILE)
            except:
                pass
            return {
                "trade_date": self.trade_date,
                "status": "incomplete",
                "total_pages": 0,
                "completed_pages": 0,
                "failed_pages": [],
                "updated_at": "",
                "data": [],
            }
        except Exception as e:
            self.logger.error(f"❌ 读取缓存失败：{e}")
            return {
                "trade_date": self.trade_date,
                "status": "incomplete",
                "total_pages": 0,
                "completed_pages": 0,
                "failed_pages": [],
                "updated_at": "",
                "data": [],
            }

    def _save_cache(self):
        """保存缓存（原子写入）"""
        if not CACHE_ENABLED:
            return

        try:
            # 确保缓存目录存在
            Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

            # 写入临时文件
            temp_file = f"{self.CACHE_FILE}.tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.cache_data, f, ensure_ascii=False, indent=2)

            # 原子重命名
            os.rename(temp_file, self.CACHE_FILE)

            self.logger.info(f"💾 保存缓存成功：{self.cache_data['completed_pages']}页")

        except Exception as e:
            self.logger.error(f"❌ 保存缓存失败：{e}")

    def _clear_cache(self):
        """清理 7 天前的缓存文件"""
        if not CACHE_ENABLED:
            return

        try:
            if not os.path.exists(self.CACHE_FILE):
                return

            # 检查文件修改时间
            file_mtime = datetime.fromtimestamp(os.path.getmtime(self.CACHE_FILE))
            file_age = datetime.now() - file_mtime

            # 超过 7 天才删除
            if file_age.days >= 7:
                os.remove(self.CACHE_FILE)
                self.logger.info(f"🗑️ 已删除 7 天前的缓存 ({file_age.days}天)")
            else:
                self.logger.info(f"⏭️ 缓存文件 {file_age.days}天，保留")

        except Exception as e:
            self.logger.debug(f"清理缓存失败：{e}")

    def _is_cache_valid(self) -> bool:
        """检查缓存是否有效"""
        if not CACHE_ENABLED:
            return False

        try:
            # 检查缓存文件
            if not os.path.exists(self.CACHE_FILE):
                self.logger.info("缓存文件不存在")
                return False

            # 检查交易日期（必须是同一个交易日）
            if self.cache_data.get("trade_date") != self.trade_date:
                self.logger.info(
                    f"缓存交易日期不匹配：{self.cache_data.get('trade_date')} != {self.trade_date}"
                )
                return False

            # 检查状态
            if self.cache_data.get("status") == "complete":
                self.logger.info("缓存已完成，无需恢复")
                return False

            # 检查时间（不超过 7 天，即 168 小时）
            updated_at = self.cache_data.get("updated_at", "")
            if updated_at:
                cache_time = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                hours = (datetime.now() - cache_time).total_seconds() / 3600
                if hours > CACHE_EXPIRE_HOURS:
                    self.logger.info(
                        f"缓存超时：{hours:.1f}小时 > {CACHE_EXPIRE_HOURS}小时"
                    )
                    return False

            self.logger.info(
                f"✅ 缓存有效：{self.cache_data.get('completed_pages', 0)}页"
            )
            return True

        except Exception as e:
            self.logger.error(f"❌ 检查缓存失败：{e}")
            return False
