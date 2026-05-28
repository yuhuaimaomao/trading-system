# -*- coding: utf-8 -*-
"""
财联社汇总文章抓取器（HTTP 直接请求方案 + 自动查找最新）
功能：精准抓取"【早报】"等汇总文章
"""

import requests
import re
import sys
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from system.utils.stock_code_utils import normalize_stock_code

# 导入日志系统
from system.utils.logger import get_collector_logger


class CLSDigestCollector:
    """财联社汇总文章抓取器"""

    def __init__(self):
        self.logger = get_collector_logger('cls_digest')
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Referer": "https://www.cls.cn/"
        })
        self.session.trust_env = False
    
    def fetch_digest_article(self, article_type: str = "morning") -> Optional[Dict]:
        """
        精准获取财联社汇总文章（自动查找最新）
        
        Args:
            article_type: 文章类型
                - "morning": 早报（【早报】）
                - "risk": 投资避雷针（【投资避雷针】）
        
        Returns:
            汇总文章数据
        """
        try:
            # 【1】自动查找最新文章
            if article_type == "morning":
                self.logger.info("自动查找最新早报文章...")
                article_id = self._find_latest_article("【早报】")
                article_name = "早报"
            elif article_type == "risk":
                self.logger.info("自动查找最新投资避雷针文章...")
                # 投资避雷针标题格式："X 月 X 日投资避雷针：..."（不带【】）
                article_id = self._find_latest_article("投资避雷针")
                article_name = "投资避雷针"
            elif article_type == "morning_news":
                self.logger.info("自动查找最新早间新闻精选文章...")
                # 早间新闻精选标题格式："【财联社 X 月 X 日早间新闻精选】"
                article_id = self._find_latest_article("早间新闻精选", search_type="all")
                article_name = "早间新闻精选"
            elif article_type == "focus_review":
                self.logger.info("自动查找最新焦点复盘文章...")
                article_id = self._find_latest_article("焦点复盘")
                article_name = "焦点复盘"
            elif article_type == "daily_review":
                self.logger.info("自动查找最新每日收评文章...")
                article_id = self._find_latest_article("每日收评")
                article_name = "每日收评"
            elif article_type == "evening_news":
                self.logger.info("自动查找最新晚间新闻精选文章...")
                article_id = self._find_latest_article("晚间新闻精选", search_type="all")
                article_name = "晚间新闻精选"
            else:
                self.logger.error(f"未知的文章类型：{article_type}")
                return None
            
            if not article_id:
                self.logger.warning(f"未找到{article_name}文章")
                return None
            
            # 【2】访问文章详情
            article_url = f"https://www.cls.cn/detail/{article_id}"
            self.logger.info(f"访问{article_name}文章：{article_url}")
            
            resp = self.session.get(article_url, timeout=10)
            
            if resp.status_code != 200:
                self.logger.warning(f"访问文章失败：{resp.status_code}")
                return None
            
            html_content = resp.text
            
            # 【3】提取标题
            title_match = re.search(r'<title>([^<]+)</title>', html_content)
            title = title_match.group(1).strip() if title_match else f"财联社{article_name}"
            
            # 清理标题
            title = re.sub(r'\s*- 财联社.*', '', title)
            title = re.sub(r'\s*原创.*', '', title)
            title = title.strip()
            
            # 【4】提取正文内容
            # 先尝试用 <p> 标签提取（早报格式）
            paragraphs = re.findall(r'<p[^>]*>([^<]+)</p>', html_content)
            
            if paragraphs:
                # 早报格式：用 <p> 标签
                content_lines = []
                for p in paragraphs:
                    p = p.strip()
                    if len(p) > 20 and not any(x in p for x in ['举报', '©2018', '版权所有', '许可证']):
                        content_lines.append(p)
                content = '\n'.join(content_lines)
            else:
                # 早间新闻精选格式：从 HTML 中提取纯文本，然后清理
                # 移除 script、style 等标签
                html_clean = re.sub(r'<script[^>]*>[^<]*</script>', '', html_content)
                html_clean = re.sub(r'<style[^>]*>[^<]*</style>', '', html_clean)
                html_clean = re.sub(r'<nav[^>]*>[^<]*</nav>', '', html_clean)
                html_clean = re.sub(r'<header[^>]*>[^<]*</header>', '', html_clean)
                html_clean = re.sub(r'<footer[^>]*>[^<]*</footer>', '', html_clean)
                
                # 提取所有文本
                text_content = re.sub(r'<[^>]+>', ' ', html_clean)
                text_content = re.sub(r'\s+', ' ', text_content).strip()
                
                # 找到标题位置，从标题后面开始提取（跳过标题前的导航杂质）
                # 注意：标题可能出现多次，找到最后一个出现的位置
                title_pos = text_content.rfind(title)
                if title_pos >= 0:
                    # 只取标题之后的内容
                    content = text_content[title_pos + len(title):].strip()
                    # 清理头部杂质：移除日期时间（如"2026年05月08日 08:06:23"）
                    content = re.sub(r'^\s*\d{4}年\d{2}月\d{2}日\s+\d{2}:\d{2}:\d{2}', '', content)
                    content = content.strip()
                    # 按序号分割成段落（1、2、3...）
                    content_lines = re.split(r'(?=\d+、)', content)
                    content_lines = [line.strip() for line in content_lines if len(line.strip()) > 10]
                    content = '\n'.join(content_lines)
                else:
                    # Fallback：找不到标题，从第一个序号开始提取
                    first_num_pos = re.search(r'\d+、', text_content)
                    if first_num_pos:
                        content = text_content[first_num_pos.start():]
                        content_lines = re.split(r'(?=\d+、)', content)
                        content_lines = [line.strip() for line in content_lines if len(line.strip()) > 10]
                        content = '\n'.join(content_lines)
                    else:
                        content = text_content[:3000]  # Fallback：取前 3000 字
            
            # 【5】时间校验：提取文章发布时间，和当前交易日对比
            publish_date = self._extract_publish_date(html_content)
            if publish_date:
                self.logger.info(f"文章发布时间：{publish_date}")
                # 提取日期部分（YYYY-MM-DD）
                date_part = publish_date.split(' ')[0] if ' ' in publish_date else publish_date[:10]
                current_date = datetime.now().strftime('%Y-%m-%d')
                if date_part != current_date:
                    self.logger.warning(f"⚠️ 文章日期不匹配：文章日期={date_part}, 当前日期={current_date}")
                    return None
            else:
                self.logger.warning("⚠️ 未找到文章发布时间，跳过校验")
            
            if content:
                self.logger.info(f"成功抓取{article_name}文章：{title[:50]}... ({len(content)}字)")
                return {
                    "title": title,
                    "content": content,
                    "time": datetime.now().strftime("%H:%M"),
                    "source": f"财联社 - {article_name}",
                    "url": article_url,
                    "is_summary": True,
                    "word_count": len(content),
                    "paragraph_count": len(content_lines),
                    "type": article_type
                }
            else:
                self.logger.warning("提取正文失败")
                return None
                
        except Exception as e:
            self.logger.error(f"抓取汇总文章异常：{e}")
            return None
    
    def _find_latest_article(self, keyword: str, search_type: str = "depth") -> Optional[str]:
        """
        使用 agent-browser 打开搜索页面，获取最新文章 ID
        
        策略：
        1. 打开财联社搜索页面（带精确关键词，如"【早报】"）
        2. 等待页面加载
        3. 获取快照
        4. 解析快照，提取第一篇文章的 ID
        
        Args:
            keyword: 搜索关键词（如"【早报】"、"【投资避雷针】"），必须带括号
            search_type: 搜索类型（depth=深度，all=全部）
        
        Returns:
            文章 ID
        """
        try:
            import os
            import subprocess
            import time
            import shutil

            agent_browser = shutil.which('agent-browser')
            if not agent_browser:
                for p in ['/opt/homebrew/bin/agent-browser', '/usr/local/bin/agent-browser']:
                    if os.path.isfile(p):
                        agent_browser = p
                        break
            if not agent_browser:
                self.logger.error("❌ agent-browser 未安装或不在 PATH 中，无法搜索文章")
                return None

            search_url = f"https://www.cls.cn/searchPage?keyword={keyword}&type={search_type}"
            self.logger.info(f"打开搜索页面：{search_url}")

            subprocess.run([agent_browser, "open", search_url], capture_output=True, timeout=15)
            
            # 2. 等待页面加载
            time.sleep(5)
            
            # 3. 获取快照
            result = subprocess.run([agent_browser, "snapshot"], capture_output=True, text=True, timeout=10)
            snapshot = result.stdout
            
            if not snapshot:
                self.logger.warning("获取快照失败")
                return None
            
            # 4. 解析快照，提取第一篇文章 ID
            # 快照格式示例：ref=e123 text=【早报】标题...
            article_id = self._parse_article_id_from_snapshot(snapshot, keyword)
            
            if article_id:
                self.logger.info(f"从搜索页面解析到文章 ID: {article_id}")
                return article_id
            else:
                self.logger.warning(f"未从快照中解析到文章 ID")
                return None
            
        except Exception as e:
            self.logger.error(f"搜索页面抓取异常：{e}")
            return None
    
    def _parse_article_id_from_snapshot(self, snapshot: str, keyword: str) -> Optional[str]:
        """
        从快照中解析文章 ID
        
        策略：
        1. 查找包含完整关键词的链接标题（如"【早报】"）
        2. 提取该链接对应的文章 ID
        
        Args:
            snapshot: 页面快照文本
            keyword: 搜索关键词（如"【早报】"，必须带括号）
        
        Returns:
            文章 ID
        """
        try:
            import re
            
            # 查找文章链接（格式：link "标题" [ref=xxx]: /url: /detail/ID）
            lines = snapshot.split('\n')
            
            for i, line in enumerate(lines):
                # 查找包含 link 的行（文章标题行）
                # 格式：- link "标题" [ref=xxx]:
                if 'link "' in line:
                    # 提取标题内容
                    title_match = re.search(r'link "([^"]+)"', line)
                    if title_match:
                        title = title_match.group(1)
                        
                        # 严格匹配关键词（必须包含完整的"【早报】"）
                        if keyword in title:
                            self.logger.info(f"找到匹配标题：{title}")
                            
                            # 在后续行中查找对应的文章 ID
                            # 格式：/url: /detail/1234567
                            for j in range(i, min(i+3, len(lines))):
                                url_match = re.search(r'/url:\s*/detail/(\d+)', lines[j])
                                if url_match:
                                    article_id = url_match.group(1)
                                    self.logger.info(f"解析到文章 ID: {article_id}（标题：{title[:50]}）")
                                    return article_id
            
            self.logger.warning(f"未找到包含'{keyword}'的文章")
            return None
            
        except Exception as e:
            self.logger.error(f"解析快照失败：{e}")
            return None
    
    def _extract_publish_date(self, html_content: str) -> Optional[str]:
        """
        从 HTML 中提取文章发布时间
        
        支持两种格式：
        1. YYYY-MM-DD HH:MM（早报/焦点复盘/每日收评）
        2. YYYY年MM月DD日 HH:MM（晚间新闻精选）
        
        Args:
            html_content: HTML 内容
        
        Returns:
            发布时间字符串（YYYY-MM-DD HH:MM 格式），未找到返回 None
        """
        try:
            # 清理 HTML 提取文本
            html_clean = re.sub(r'<script[^>]*>[^<]*</script>', '', html_content)
            html_clean = re.sub(r'<style[^>]*>[^<]*</style>', '', html_clean)
            text_content = re.sub(r'<[^>]+>', ' ', html_clean)
            text_content = re.sub(r'\s+', ' ', text_content).strip()
            
            # 提取标题（用于定位）
            title_match = re.search(r'<title>([^<]+)</title>', html_content)
            title = title_match.group(1).strip() if title_match else ''
            title = re.sub(r'\s*- 财联社.*', '', title)
            title = re.sub(r'\s*原创.*', '', title)
            title = title.strip()
            
            # 在标题附近查找时间
            title_pos = text_content.find(title)
            if title_pos >= 0:
                # 取标题后 500 字范围
                end = min(len(text_content), title_pos + len(title) + 500)
                context = text_content[title_pos:end]
                
                # 格式 1: YYYY-MM-DD HH:MM
                date_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', context)
                if date_match:
                    return date_match.group(1)
                
                # 格式 2: YYYY年MM月DD日 HH:MM
                date_match2 = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}:\d{2})', context)
                if date_match2:
                    year, month, day, time = date_match2.groups()
                    return f"{year}-{month.zfill(2)}-{day.zfill(2)} {time}"
            
            self.logger.warning("未找到文章发布时间")
            return None
            
        except Exception as e:
            self.logger.error(f"提取发布时间失败：{e}")
            return None


    def fetch_and_save(self) -> Dict:
        return self.collect()

    def collect(self) -> Dict:
        """
        统一采集接口（早报服务调用）

        Returns:
            新闻数据：
            - morning: 早报文章
            - bileizhen: 投资避雷针
            - morning_news: 早间新闻精选
        """
        result = {}
        morning = self.fetch_digest_article("morning")
        if morning:
            result['morning'] = morning
        bileizhen = self.fetch_digest_article("risk")
        if bileizhen:
            result['bileizhen'] = bileizhen
        morning_news = self.fetch_digest_article("morning_news")
        if morning_news:
            result['morning_news'] = morning_news
        return result
    
    def collect_review(self) -> Dict:
        """
        复盘采集接口（复盘服务调用）
        
        Returns:
            新闻数据：
            - focus_review: 焦点复盘
            - daily_review: 每日收评
        """
        result = {}
        focus_review = self.fetch_digest_article("focus_review")
        if focus_review:
            result['focus_review'] = focus_review
        daily_review = self.fetch_digest_article("daily_review")
        if daily_review:
            result['daily_review'] = daily_review
        return result


if __name__ == "__main__":
    # 测试
    collector = CLSDigestCollector()
    
    print("=" * 70)
    print("📰 测试财联社早报抓取（自动查找最新）")
    print("=" * 70)
    print()
    
    article = collector.fetch_digest_article()
    
    if article:
        print(f"✅ 抓取成功")
        print(f"标题：{article['title']}")
        print(f"来源：{article['source']}")
        print(f"时间：{article['time']}")
        print(f"字数：{article['word_count']}字")
        print(f"段落：{article['paragraph_count']}段")
        print(f"URL: {article['url']}")
        print()
        print("【内容预览】")
        print(article['content'][:1000])
        print("...")
    else:
        print("❌ 抓取失败")
    
    print()
    print("=" * 70)
