"""
监管函/问询函采集器
功能：从巨潮资讯网采集监管函数据，下载 PDF，保存到数据库

数据源：http://www.cninfo.com.cn
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

from system.config.settings import STORAGE_PATH
from system.utils.logger import get_collect_logger

logger = get_collect_logger("events")


# 搜索关键词
SEARCH_KEYWORDS = [
    "监管函",
    "问询函",
    "关注函",
    "警示函",
    "责令改正",
]

PDF_STORAGE_DIR = os.path.join(STORAGE_PATH, "pdf")


class RegulatoryLetterCollector:
    """监管函/问询函采集器"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }
        )
        self.session.trust_env = False

        # 确保 PDF 目录存在
        os.makedirs(PDF_STORAGE_DIR, exist_ok=True)

        # 分析服务（懒加载，避免反向依赖）
        try:
            from system.utils.regulatory_analysis import (
                RegulatoryAnalysisService,
            )

            self.analysis_service = RegulatoryAnalysisService()
        except ImportError:
            self.analysis_service = None

        # 数据库连接
        self._init_db()

        logger.info("监管函采集器初始化完成")

    def _init_db(self):
        """初始化数据库连接"""
        from system.config.settings import DATABASE_PATH

        self.db_path = DATABASE_PATH
        logger.info(f"数据库路径：{self.db_path}")

    def _save_to_db(self, announcement: Dict) -> Optional[int]:
        """
        保存公告到数据库

        Args:
            announcement: 公告数据

        Returns:
            公告 ID，失败返回 None
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # 检查是否已存在
            cursor.execute(
                "SELECT id FROM regulatory_letter WHERE announcement_id = ?",
                (announcement.get("announcement_id"),),
            )
            existing = cursor.fetchone()

            if existing:
                logger.info(f"公告已存在：{announcement.get('announcement_id')}")
                conn.close()
                return existing[0]

            # 插入新数据（只插入基本信息，分析结果后续更新）
            cursor.execute(
                """
                INSERT INTO regulatory_letter (
                    stock_code, stock_name, org_name, announcement_id,
                    title, title_html, trade_date, trade_time,
                    column_name, announcement_type,
                    pdf_url, pdf_file, pdf_downloaded, pdf_analyzed,
                    risk_level, risk_stars, risk_summary, risk_keywords,
                    crawl_time, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    announcement.get("stock_code", ""),
                    announcement.get("stock_name", ""),
                    announcement.get("org_name", ""),
                    announcement.get("announcement_id", ""),
                    announcement.get("title", ""),
                    announcement.get("title_html", ""),
                    announcement.get("trade_date", "")[:10],
                    announcement.get("trade_time", ""),
                    announcement.get("column", ""),
                    announcement.get("announcement_type", ""),
                    announcement.get("pdf_url", ""),
                    announcement.get("pdf_file", ""),
                    False,
                    False,
                    announcement.get("risk_level", 1),
                    announcement.get("risk_stars", "⭐"),
                    announcement.get("risk_summary", ""),
                    json.dumps(announcement.get("risk_keywords", []), ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

            announcement_id = cursor.lastrowid
            conn.commit()
            conn.close()

            logger.info(f"✅ 公告已保存：{announcement.get('stock_name')} (ID: {announcement_id})")
            return announcement_id

        except Exception as e:
            logger.error(f"保存数据库失败：{e}")
            return None

    def _update_analysis_result(self, announcement_id: int, analysis_result: Dict):
        """
        更新分析结果到数据库

        Args:
            announcement_id: 公告 ID
            analysis_result: 分析结果
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                """
                UPDATE regulatory_letter SET
                    pdf_summary = ?,
                    word_count = ?,
                    risk_type = ?,
                    issuer = ?,
                    issuer_short = ?,
                    recipient = ?,
                    issue_date = ?,
                    pdf_analyzed = TRUE,
                    analyzed_time = ?,
                    updated_at = ?
                WHERE id = ?
            """,
                (
                    analysis_result.get("pdf_summary", ""),
                    analysis_result.get("word_count", 0),
                    analysis_result.get("risk_type", ""),
                    analysis_result.get("issuer", ""),
                    analysis_result.get("issuer_short", ""),
                    analysis_result.get("recipient", ""),
                    analysis_result.get("issue_date", ""),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    announcement_id,
                ),
            )

            conn.commit()
            conn.close()

            logger.info(f"✅ 分析结果已更新：ID {announcement_id}")

        except Exception as e:
            logger.error(f"更新分析结果失败：{e}")

    def search(self, keyword: str, trade_date: str = None, pages: int = 3) -> List[Dict]:
        """
        搜索监管函/问询函

        Args:
            keyword: 搜索关键词
            trade_date: 交易日期（默认今天）
            pages: 页数

        Returns:
            公告列表
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        logger.info(f"开始搜索：{keyword}（日期：{trade_date}，{pages}页）...")

        all_announcements = []

        for page in range(1, pages + 1):
            logger.info(f"第{page}页...")

            try:
                # 构建搜索结果 URL（GET 请求）
                url = f"http://www.cninfo.com.cn/new/fulltextSearch/full?searchkey={keyword}&sdate={trade_date}&edate={trade_date}&isfulltext=false&sortName=pubdate&sortType=desc&pageNum={page}&pageSize=20&type="

                response = self.session.get(url, timeout=30)

                if response.status_code == 200:
                    try:
                        result = response.json()
                        announcements = result.get("announcements", [])
                        total = result.get("totalAnnouncement", 0)

                        logger.info(f"✅ 第{page}页：{len(announcements)}条 (总记录：{total})")

                        if announcements:
                            all_announcements.extend(announcements)

                            # 如果返回数据少于 20 条，说明是最后一页
                            if len(announcements) < 20:
                                logger.info("最后一页")
                                break
                        else:
                            logger.info(f"第{page}页无数据")
                            break
                    except json.JSONDecodeError:
                        logger.error(f"第{page}页：JSON 解析失败")
                        break
                else:
                    logger.error(f"第{page}页：HTTP {response.status_code}")
                    break

                # 延迟，避免请求过快
                time.sleep(1)

            except Exception as e:
                logger.error(f"第{page}页请求失败：{e}")
                break

        logger.info(f"搜索完成：共{len(all_announcements)}条")
        return all_announcements

    def format_announcement(self, announcement: Dict) -> Dict:
        """格式化公告数据"""
        title = announcement.get("announcementTitle", "")
        title_clean = re.sub(r"<[^>]+>", "", title)  # 去除 HTML 标签

        # 处理时间戳（毫秒）
        announce_time = announcement.get("announcementTime", 0)
        if isinstance(announce_time, int) and announce_time > 0:
            announce_datetime = datetime.fromtimestamp(announce_time / 1000)
            announce_date = announce_datetime.strftime("%Y-%m-%d")
            announce_time_str = announce_datetime.strftime("%H:%M")
        else:
            announce_date = str(announce_time)[:10]
            announce_time_str = ""

        # 补全 PDF 链接
        adjunct_url = announcement.get("adjunctUrl", "")
        pdf_url = f"http://static.cninfo.com.cn/{adjunct_url}" if adjunct_url else ""

        # 生成 PDF 文件名（日期_股票名称_ID）
        stock_name = announcement.get("secName", "")
        announcement_id = announcement.get("announcementId", "")

        # 使用已格式化的 announce_date 生成文件名（去掉横杠）
        announce_date_for_filename = announce_date.replace("-", "") if announce_date else ""

        # 清理股票名称中的特殊字符
        stock_name_clean = re.sub(r'[\\/:*?"<>|]', "", stock_name)

        if announcement_id and announce_date_for_filename:
            pdf_filename = f"{announce_date_for_filename}_{stock_name_clean}_{announcement_id}.pdf"
        else:
            pdf_filename = ""

        return {
            "stock_code": announcement.get("secCode", ""),
            "stock_name": announcement.get("secName", ""),
            "org_name": announcement.get("orgName", ""),
            "announcement_id": announcement_id,
            "title": title_clean,
            "title_html": title,
            "trade_date": announce_date,
            "trade_time": announce_time_str,
            "column": announcement.get("pageColumn", ""),
            "announcement_type": announcement.get("announcementType", ""),
            "pdf_url": pdf_url,
            "pdf_file": pdf_filename,
        }

    def download_pdf(self, announcement: Dict) -> Optional[str]:
        """
        下载 PDF 文件

        Args:
            announcement: 公告数据

        Returns:
            PDF 文件路径，失败返回 None
        """
        pdf_url = announcement.get("pdf_url", "")
        pdf_filename = announcement.get("pdf_file", "")

        if not pdf_url or not pdf_filename:
            return None

        filepath = os.path.join(PDF_STORAGE_DIR, pdf_filename)

        # 如果文件已存在，跳过
        if os.path.exists(filepath):
            logger.info(f"PDF 已存在：{pdf_filename}")
            return filepath

        try:
            # 下载 PDF
            response = self.session.get(pdf_url, timeout=30)

            if response.status_code == 200:
                with open(filepath, "wb") as f:
                    f.write(response.content)

                logger.info(f"✅ PDF 下载成功：{pdf_filename}")
                return filepath
            else:
                logger.error(f"PDF 下载失败：HTTP {response.status_code}")
                return None

        except Exception as e:
            logger.error(f"PDF 下载异常：{e}")
            return None

    def cleanup_old_pdfs(self, days: int = 7):
        """
        清理过期 PDF 文件

        Args:
            days: 保留天数（默认 7 天）
        """
        try:
            cutoff_time = datetime.now() - timedelta(days=days)
            cutoff_timestamp = cutoff_time.timestamp()

            deleted_count = 0

            for filename in os.listdir(PDF_STORAGE_DIR):
                if not filename.endswith(".pdf"):
                    continue

                filepath = os.path.join(PDF_STORAGE_DIR, filename)

                # 检查文件修改时间
                if os.path.getmtime(filepath) < cutoff_timestamp:
                    os.remove(filepath)
                    deleted_count += 1
                    logger.info(f"删除过期 PDF: {filename}")

            logger.info(f"清理完成：删除 {deleted_count} 个过期 PDF")

        except Exception as e:
            logger.error(f"清理过期 PDF 失败：{e}")

    def fetch_and_save(self, trade_date: str = None) -> Dict:
        """
        标准接口：获取并保存监管函数据（完整版 - 采集 + 下载 PDF+ 分析）

        Args:
            trade_date: 交易日期（格式：YYYY-MM-DD，默认今天）

        Returns:
            {
                'success': True/False,
                'count': 实际采集数量,
                'total': 实际采集数量（A 类统计）,
                'data': 公告列表
            }
        """
        try:
            if trade_date is None:
                trade_date = datetime.now().strftime("%Y-%m-%d")

            logger.info("=" * 60)
            logger.info(f"🍎 {self.__class__.__name__} 开始采集")
            logger.info("=" * 60)

            # 搜索"监管函"和"问询函"两个核心关键词，各 1 页
            keywords = ["监管函", "问询函"]
            total_count = 0
            downloaded_count = 0
            analyzed_count = 0
            all_announcements = []

            for keyword in keywords:
                logger.info(f"搜索关键词：{keyword}...")
                announcements = self.search(keyword, trade_date=trade_date, pages=1)

                if not announcements:
                    logger.info(f"{keyword}：无数据")
                    continue

                # 处理每条公告
                for ann in announcements:
                    total_count += 1

                    # 格式化
                    formatted = self.format_announcement(ann)

                    # 保存到数据库
                    announcement_id = self._save_to_db(formatted)

                    if announcement_id:
                        all_announcements.append(formatted)

                        # 下载 PDF
                        pdf_path = self.download_pdf(formatted)

                        if pdf_path:
                            downloaded_count += 1

                            # 更新数据库（标记 PDF 已下载）
                            conn = sqlite3.connect(self.db_path)
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE regulatory_letter SET pdf_downloaded = TRUE, pdf_file = ? WHERE id = ?",
                                (formatted["pdf_file"], announcement_id),
                            )
                            conn.commit()
                            conn.close()

                            # 分析 PDF
                            if self.analysis_service:
                                logger.info(f"分析 PDF: {formatted['pdf_file']}...")
                                analysis_result = self.analysis_service.analyze_pdf(pdf_path)
                            else:
                                analysis_result = None

                            if analysis_result:
                                analyzed_count += 1

                                # 更新数据库
                                self._update_analysis_result(announcement_id, analysis_result)

                    # 延迟，避免请求过快
                    time.sleep(1)

            # 统计数量
            actual_count = len(all_announcements)

            result = {
                "success": True,
                "count": actual_count,
                "total": actual_count,  # A 类统计
                "data": all_announcements,
            }

            logger.info(f"✅ {self.__class__.__name__} 采集完成：{actual_count}条")
            logger.info(f"   PDF 下载：{downloaded_count}/{total_count}")
            logger.info(f"   PDF 分析：{analyzed_count}/{downloaded_count}")

            # 清理过期 PDF
            self.cleanup_old_pdfs(days=7)

            logger.info("=" * 60)
            return result

        except Exception as e:
            logger.error(f"❌ {self.__class__.__name__} 采集异常：{e}")
            logger.info("=" * 60)
            return {"success": False, "count": 0, "total": 0, "data": []}

    def run(self, keywords: List[str] = None, trade_date: str = None, pages: int = 3):
        """
        运行采集流程（采集 + 下载 + 分析）

        Args:
            keywords: 搜索关键词列表（默认全部）
            trade_date: 交易日期（默认今天）
            pages: 页数
        """
        if keywords is None:
            keywords = SEARCH_KEYWORDS

        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("=" * 60)
        logger.info(f"开始采集监管函（日期：{trade_date}）")
        logger.info("=" * 60)

        total_count = 0
        downloaded_count = 0
        analyzed_count = 0

        for keyword in keywords:
            logger.info(f"\n【搜索关键词：{keyword}】")

            # 搜索
            announcements = self.search(keyword, trade_date=trade_date, pages=pages)

            if not announcements:
                logger.info(f"{keyword}：无数据")
                continue

            # 处理每条公告
            for ann in announcements:
                total_count += 1

                # 格式化
                formatted = self.format_announcement(ann)

                # 保存到数据库
                announcement_id = self._save_to_db(formatted)

                if announcement_id:
                    # 下载 PDF
                    pdf_path = self.download_pdf(formatted)

                    if pdf_path:
                        downloaded_count += 1

                        # 更新数据库（标记 PDF 已下载）
                        conn = sqlite3.connect(self.db_path)
                        cursor = conn.cursor()
                        cursor.execute(
                            "UPDATE regulatory_letter SET pdf_downloaded = TRUE, pdf_file = ? WHERE id = ?",
                            (formatted["pdf_file"], announcement_id),
                        )
                        conn.commit()
                        conn.close()

                        # 分析 PDF
                        if self.analysis_service:
                            logger.info(f"分析 PDF: {formatted['pdf_file']}")
                            analysis_result = self.analysis_service.analyze_pdf(pdf_path)
                        else:
                            analysis_result = None

                        if analysis_result:
                            analyzed_count += 1

                            # 更新数据库
                            self._update_analysis_result(announcement_id, analysis_result)

                # 延迟
                time.sleep(1)

        # 清理过期 PDF
        logger.info("\n清理过期 PDF...")
        self.cleanup_old_pdfs(days=7)

        logger.info("\n" + "=" * 60)
        logger.info("采集完成")
        logger.info(f"总公告数：{total_count}")
        logger.info(f"PDF 下载：{downloaded_count}")
        logger.info(f"PDF 分析：{analyzed_count}")
        logger.info("=" * 60)


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("监管函数据采集器 - 测试运行（简化版 - 不下载 PDF）")
    print("=" * 60)

    try:
        collector = RegulatoryLetterCollector()
        result = collector.fetch_and_save()

        if result.get("success"):
            print(f"\n✅ 采集成功：{result['count']}条公告")
        else:
            print("\n❌ 采集失败")

    except Exception as e:
        print(f"\n❌ 执行异常：{e}")
        import traceback

        traceback.print_exc()

    print("=" * 60)
