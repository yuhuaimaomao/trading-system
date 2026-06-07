"""
财联社电报采集器

数据源：财联社 /api/cache（直调 HTTP，不经过 akshare）
写入表：cls_telegraph
用途：为复盘/早盘/趋势筛选提供盘中消息

字段维度：
- level (A/B/C)：财联社编辑标记的重要性
- score：level × reading_num 综合评分
- stock_tags / subject_tags / plate_tags：CLS 编辑标注，直接可用
- 盘面直播噪声在采集阶段过滤，不存库
"""

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

import requests

from system.config.akshare_config import get_headers
from system.config.settings import DATABASE_PATH
from system.utils.logger import get_collect_logger

# ========== 常量 ==========
TELEGRAPH_LIST_URL = "https://www.cls.cn/api/cache"
TELEGRAPH_LIST_PARAMS = {"name": "telegraph", "rn": 20}
ARTICLE_URL = "https://api3.cls.cn/share/article/{}"
ARTICLE_PARAMS = "?os=web&sv=8.4.6&app=CailianpressWeb"
DEFAULT_TIMEOUT = 20
ARTICLE_FETCH_DELAY = 0.3  # 补全文章时的请求间隔（秒）
RETENTION_HOURS = 72  # 电报保留时长

# 盘面直播噪声标题模式：纯市场/指数描述，无个股信息
_TELEGRAPH_NOISE_PATTERNS = [
    "收评",
    "午评",  # 市场总结
    "涨逾",
    "涨超",
    "涨近",  # 指数涨幅描述
    "下跌",
    "下挫",  # 指数跌幅描述
    "成交额突破",
    "成交额超",  # 成交量播报
    "主力资金监控",  # 资金流向播报
]
_TELEGRAPH_KEEP_PATTERNS = [
    "涨停分析",
    "连板股分析",
    "竞价看龙头",
    "舆情热点",
]


class TelegraphCollector:
    """财联社电报采集器"""

    def __init__(self, db_path: str = None):
        self.logger = get_collect_logger("events")
        self.db_path = db_path or str(DATABASE_PATH)
        self.session = requests.Session()
        self.session.headers.update(get_headers())
        # /api/cache 返回 Brotli，requests 默认不支持，去掉 br
        self.session.headers["Accept-Encoding"] = "gzip, deflate"
        self.session.headers.update({"Referer": "https://www.cls.cn/telegraph"})
        self.logger.info("电报采集器初始化完成")

    # ========== 数据采集 ==========

    def _fetch_telegraph_list(self) -> list:
        """获取电报列表（单次返回约 20 条，覆盖约 35 分钟窗口）"""
        try:
            params = dict(TELEGRAPH_LIST_PARAMS)
            params["lastTime"] = int(time.time())
            resp = self.session.get(TELEGRAPH_LIST_URL, params=params, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data", {}).get("roll_data", [])
            return items if isinstance(items, list) else []
        except Exception as e:
            self.logger.error(f"获取电报列表失败：{e}")
            return []

    def _fetch_article_detail(self, article_id) -> str:
        """获取文章完整内容（HTML → 纯文本）"""
        try:
            url = ARTICLE_URL.format(article_id) + ARTICLE_PARAMS
            resp = self.session.get(url, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            html = resp.text

            # 提取正文（三种模式，按优先级）
            for pattern in [
                r'<div class="detail-content"[^>]*>(.*?)</div>',
                r"<article[^>]*>(.*?)</article>",
                r'<div class="content"[^>]*>(.*?)</div>',
            ]:
                m = re.search(pattern, html, re.DOTALL)
                if m:
                    text = m.group(1)
                    text = re.sub(r"<br\s*/?>", "\n", text)
                    text = re.sub(r"<[^>]+>", "", text)
                    text = re.sub(r"\n{3,}", "\n\n", text)
                    return text.strip()
            return ""
        except Exception as e:
            self.logger.debug(f"获取文章 {article_id} 详情失败：{e}")
            return ""

    # ========== 标签格式化 ==========

    @staticmethod
    def _format_stock_tags(stock_list: list) -> list:
        """提取股票代码（纯数字）和名称，丢弃行情快照"""
        result = []
        for s in stock_list or []:
            if isinstance(s, dict):
                raw = s.get("StockID", "")
                name = s.get("name", "")
                if raw and name:
                    # 去 sh/sz 前缀，只保留 6 位数字代码
                    code = raw.replace("sh", "").replace("sz", "")
                    result.append({"code": code, "name": name})
        return result

    @staticmethod
    def _format_subject_tags(subject_list: list) -> list:
        """提取主题名称，丢弃元数据"""
        result = []
        for s in subject_list or []:
            if isinstance(s, dict):
                name = s.get("subject_name", "")
                if name:
                    result.append(name)
        return result

    @staticmethod
    def _format_plate_tags(plate_list: list) -> list:
        """提取板块名称"""
        result = []
        for p in plate_list or []:
            if isinstance(p, dict):
                name = p.get("plate_name", "") or p.get("name", "")
                if name:
                    result.append(name)
        return result

    @staticmethod
    def _derive_category(subject_names: list) -> str:
        """从 subject_tags 取第一个非元信息标签作为分类"""
        # 这些是平台元信息标签，不是内容分类
        meta_tags = {
            "互动平台精选",
            "期货市场情报",
            "环球市场情报",
            "TMT行业观察",
            "A股IPO动态",
            "能源行业新闻",
        }
        for name in subject_names or []:
            if name not in meta_tags:
                return name
        return subject_names[0] if subject_names else "其他"

    # ========== 评分 ==========

    @staticmethod
    def _score(level: str, reading_num: int) -> int:
        """
        基础评分：level + 阅读量

        score >= 2 → 进入复盘
        """
        score = 0
        level = (level or "").upper()

        if level == "A":
            score += 5
        elif level == "B":
            score += 3
        # C 不加分

        if reading_num and reading_num >= 500000:
            score += 3
        elif reading_num and reading_num >= 200000:
            score += 2
        elif reading_num and reading_num >= 50000:
            score += 1

        return score

    # ========== 主流程 ==========

    def fetch_and_save(self, trade_date: str = None) -> Dict[str, Any]:
        return self.collect(trade_date)

    def collect(self, trade_date: str = None) -> Dict[str, Any]:
        """
        采集电报并入库。盘面直播噪声直接跳过不存。

        Args:
            trade_date: 日期 YYYY-MM-DD（用于日志，实际按 ctime 归类）

        Returns:
            采集结果
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        self.logger.info(f"开始采集电报（{trade_date}）...")

        try:
            items = self._fetch_telegraph_list()

            if not items:
                self.logger.warning("电报列表为空")
                return {"success": False, "count": 0, "data": []}

            self.logger.info(f"获取到 {len(items)} 条电报")

            # 批量处理
            records = []
            new_count = 0
            articles_fetched = 0
            noise_skipped = 0

            for item in items:
                try:
                    telegraph_id = str(item.get("id", ""))
                    if not telegraph_id:
                        continue

                    level = str(item.get("level", "C")).upper()
                    title = item.get("title", "") or ""
                    brief = item.get("brief", "") or ""
                    content = item.get("content", "") or brief
                    ctime = item.get("ctime", 0) or 0
                    reading_num = item.get("reading_num", 0) or 0

                    # 提取原始标签，提前做分类（噪声过滤需要 category）
                    subject_list = item.get("subjects", []) or []
                    subject_names = self._format_subject_tags(subject_list)
                    category = self._derive_category(subject_names)

                    # 盘面直播噪声直接跳过，不存库
                    if self._is_noise_telegraph(category, title):
                        noise_skipped += 1
                        continue

                    # 日期归一化
                    record_date = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d") if ctime else trade_date

                    # 提取原始标签数据
                    stock_list = item.get("stock_list", []) or []
                    plate_list = item.get("plate_list", []) or []

                    # 格式化标签
                    stock_tags = self._format_stock_tags(stock_list)
                    plate_names = self._format_plate_tags(plate_list)

                    # content_hash（用于判断是否需要更新）
                    content_hash = hashlib.md5((title + (content or brief)).encode("utf-8")).hexdigest()

                    # A/B 级电报补全完整内容
                    if level in ("A", "B") and not content:
                        time.sleep(ARTICLE_FETCH_DELAY)
                        full_content = self._fetch_article_detail(telegraph_id)
                        if full_content:
                            content = full_content
                            articles_fetched += 1
                    else:
                        content = content or brief

                    # 评分
                    score = self._score(level, reading_num)

                    records.append(
                        {
                            "telegraph_id": telegraph_id,
                            "trade_date": record_date,
                            "ctime": ctime,
                            "level": level,
                            "title": title,
                            "content": content,
                            "reading_num": reading_num,
                            "stock_tags": json.dumps(stock_tags, ensure_ascii=False) if stock_tags else None,
                            "subject_tags": json.dumps(subject_names, ensure_ascii=False) if subject_names else None,
                            "plate_tags": json.dumps(plate_names, ensure_ascii=False) if plate_names else None,
                            "category": category,
                            "score": score,
                            "content_hash": content_hash,
                        }
                    )

                except Exception as e:
                    self.logger.warning(f"处理电报条目失败：{e}")
                    continue

            # 入库
            new_telegraph_ids = []
            if records:
                new_telegraph_ids = self._save_to_db(records)

            self.logger.info(
                f"采集完成：{len(records)} 条（跳过 {noise_skipped} 条噪声），"
                f"新增 {len(new_telegraph_ids)} 条，补全 {articles_fetched} 篇"
            )

            return {
                "success": True,
                "count": new_count,
                "total": len(records),
                "data": records,
            }

        except Exception as e:
            self.logger.error(f"电报采集失败：{e}", exc_info=True)
            return {"success": False, "count": 0, "data": []}

    # ========== 存储 ==========

    def _save_to_db(self, records: List[Dict]) -> List[str]:
        """批量保存到 cls_telegraph 表，返回新增的 telegraph_id 列表"""
        conn = sqlite3.connect(self.db_path)
        new_ids = []

        try:
            for r in records:
                cursor = conn.execute(
                    "SELECT content_hash FROM cls_telegraph WHERE telegraph_id = ?",
                    (r["telegraph_id"],),
                )
                existing = cursor.fetchone()

                if existing:
                    # 已存在：内容有变化则更新
                    if existing[0] != r["content_hash"]:
                        conn.execute(
                            """
                            UPDATE cls_telegraph SET
                                title=?, content=?, reading_num=?, score=?,
                                stock_tags=?, subject_tags=?, plate_tags=?,
                                category=?, content_hash=?
                            WHERE telegraph_id=?
                        """,
                            (
                                r["title"],
                                r["content"],
                                r["reading_num"],
                                r["score"],
                                r["stock_tags"],
                                r["subject_tags"],
                                r["plate_tags"],
                                r["category"],
                                r["content_hash"],
                                r["telegraph_id"],
                            ),
                        )
                else:
                    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        """
                        INSERT INTO cls_telegraph (
                            telegraph_id, trade_date, ctime, level, title, content,
                            reading_num, stock_tags, subject_tags, plate_tags,
                            category, score, content_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            r["telegraph_id"],
                            r["trade_date"],
                            r["ctime"],
                            r["level"],
                            r["title"],
                            r["content"],
                            r["reading_num"],
                            r["stock_tags"],
                            r["subject_tags"],
                            r["plate_tags"],
                            r["category"],
                            r["score"],
                            r["content_hash"],
                            created_at,
                        ),
                    )
                    new_ids.append(r["telegraph_id"])

            conn.commit()

        except Exception as e:
            conn.rollback()
            self.logger.error(f"保存电报失败：{e}")
            raise
        finally:
            conn.close()

        # 清理 72 小时前的旧电报（独立事务，不影响已提交的插入）
        try:
            conn = sqlite3.connect(self.db_path)
            cutoff = (datetime.now() - timedelta(hours=RETENTION_HOURS)).strftime("%Y-%m-%d")
            conn.execute("DELETE FROM cls_telegraph WHERE trade_date < ?", (cutoff,))
            conn.commit()
        except Exception as e:
            self.logger.warning(f"清理旧电报失败：{e}")
        finally:
            conn.close()

        return new_ids

    # ========== 过滤 ==========

    @staticmethod
    def _is_noise_telegraph(category: str, title: str) -> bool:
        """盘面直播噪声：收评/午评/指数播报/成交额/资金监控，采集时直接跳过不存"""
        if category != "盘面直播":
            return False
        if any(kw in title for kw in _TELEGRAPH_KEEP_PATTERNS):
            return False
        return any(kw in title for kw in _TELEGRAPH_NOISE_PATTERNS)

    # ========== 查询（供复盘使用） ==========

    def get_for_review(self, trade_date: str, min_score: int = 3, limit: int = 80) -> List[Dict]:
        """获取复盘用的电报列表（按评分筛选）"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        cursor = conn.execute(
            """
            SELECT * FROM cls_telegraph
            WHERE trade_date = ? AND score >= ?
            ORDER BY score DESC, reading_num DESC
            LIMIT ?
        """,
            (trade_date, min_score, limit),
        )

        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()

        for r in rows:
            for field in ("stock_tags", "subject_tags", "plate_tags"):
                try:
                    r[field] = json.loads(r[field]) if r[field] else []
                except (json.JSONDecodeError, TypeError):
                    r[field] = []

        return rows

    def get_stats(self, trade_date: str) -> Dict:
        """获取当日电报统计"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        cursor = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN level = 'A' THEN 1 ELSE 0 END) as a_count,
                SUM(CASE WHEN level = 'B' THEN 1 ELSE 0 END) as b_count,
                SUM(CASE WHEN level = 'C' THEN 1 ELSE 0 END) as c_count,
                SUM(CASE WHEN score >= 3 THEN 1 ELSE 0 END) as high_score_count
            FROM cls_telegraph
            WHERE trade_date = ?
        """,
            (trade_date,),
        )

        row = cursor.fetchone()
        conn.close()

        return dict(row) if row else {}


# ========== 命令行入口 ==========


def main():
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("--once", "-o"):
        collector = TelegraphCollector()
        result = collector.collect()
        sys.exit(0 if result["success"] else 1)

    if len(sys.argv) > 2 and sys.argv[1] == "--date":
        collector = TelegraphCollector()
        result = collector.collect(sys.argv[2])
        _print_report(result)
        return

    # 默认：单次采集 + 打印报告
    collector = TelegraphCollector()
    result = collector.collect()
    _print_report(result)


def _print_report(result):
    print(f"\n{'=' * 60}")
    print("财联社电报采集报告")
    print(f"{'=' * 60}")
    print(f"总数：{result['total']} 条，新增：{result['count']} 条")

    if result["data"]:
        stats = {}
        for r in result["data"]:
            cat = r.get("category", "其他")
            stats[cat] = stats.get(cat, 0) + 1
        print("\n分类统计：")
        for cat, cnt in sorted(stats.items(), key=lambda x: x[1], reverse=True):
            print(f"  {cat}: {cnt} 条")

        print("\n高评分电报（score >= 3）：")
        high = [r for r in result["data"] if r.get("score", 0) >= 3]
        for i, r in enumerate(high[:10], 1):
            print(f"  {i}. [{r['level']}] {r['title'][:60]}")
            print(f"     分类：{r['category']}，评分：{r['score']}，阅读：{r['reading_num']}")

    print(f"\n{'=' * 60}")


if __name__ == "__main__":
    main()
