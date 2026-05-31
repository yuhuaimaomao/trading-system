"""
早盘简报 v2：AI 驱动盘前校准

数据流：
  昨日复盘报告 + 隔夜宏观 + CLS早报 + 隔夜电报 + 避雷针
  → AI 分析
  → 预期校准 + 新催化剂 + 风险/操作更新
  → Telegram 推送
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from system.config.prompts.morning import MORNING_BRIEF_PROMPT
from system.config.settings import DATABASE_PATH
from system.utils.logger import get_task_logger


class MorningBrief:
    """早盘简报：AI 驱动的盘前校准"""

    def __init__(self, telegram_bot=None):
        self.telegram = telegram_bot
        self.logger = get_task_logger("morning")

    # ================================================================
    # 主流程
    # ================================================================

    def generate_and_send(self, trade_date: str = None):
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        yesterday = (
            datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=1)
        ).strftime("%Y-%m-%d")

        # 1. 加载昨日复盘报告
        review_text = self._load_review_report(yesterday)

        # 2. 获取隔夜宏观
        macro_text = self._get_macro_text()

        # 3. 采集 CLS 早报文章
        morning_articles = self._get_morning_articles()

        # 4. 查询隔夜重要电报
        telegraph_text = self._get_overnight_telegraphs(yesterday)

        # 5. 提取避雷针并预匹配昨日推荐标的
        risk_text = ""
        if isinstance(morning_articles, dict):
            blz = morning_articles.pop("bileizhen", None)
            if blz and isinstance(blz, dict):
                raw_risk = blz.get("content", "")
                if raw_risk:
                    risk_text = self._match_risk_to_picks(raw_risk, yesterday)

        # 6. 拼装 Prompt
        prompt = MORNING_BRIEF_PROMPT.format(
            yesterday_review=review_text or "（昨日无复盘报告）",
            macro_data=macro_text or "（暂无宏观数据）",
            morning_articles=self._fmt_articles(morning_articles),
            telegraphs=telegraph_text or "（暂无隔夜重要电报）",
            risk_warnings=risk_text if risk_text else "（今日无避雷针内容）",
        )

        # 7. 调用 AI
        brief = self._call_ai(prompt)

        if not brief:
            self.logger.error("AI 生成早盘简报失败")
            return

        # 8. 添加标题行
        full_text = f"⚔️ 刺客早盘 {trade_date}\n\n{brief}"

        # 9. 推送
        self._send(full_text)
        self.logger.info("早盘简报已生成并推送")

    # ================================================================
    # 数据加载
    # ================================================================

    def _load_review_report(self, yesterday: str) -> str:
        """加载昨日复盘报告全文"""
        reports_dir = Path(__file__).parent.parent / "storage" / "reports"
        matches = sorted(reports_dir.glob(f"review_reports_{yesterday}_*.txt"))
        if matches:
            text = matches[-1].read_text(encoding="utf-8")
            self.logger.info(f"已加载昨日复盘报告（{len(text)}字）")
            return text
        self.logger.info(f"昨日（{yesterday}）无复盘报告")
        return ""

    def _get_macro_text(self) -> str:
        """获取隔夜宏观数据：先尝试更新，再读最新"""
        try:
            from data.collectors.macro.macro_collector import MacroCollector

            collector = MacroCollector(timeout=15)
            collector.fetch_and_save()
        except Exception as e:
            self.logger.warning(f"宏观数据更新失败（将使用缓存）: {e}")

        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM macro_daily ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
            if not row:
                return ""
            d = dict(row)
            lines = []
            if d.get("nasdaq_change") is not None:
                lines.append(f"纳斯达克: {d['nasdaq_change']:+.2f}%")
            if d.get("kweb_change") is not None:
                lines.append(f"中概股KWEB: {d['kweb_change']:+.2f}%")
            if d.get("a50_price") is not None:
                chg = (
                    f" ({d['a50_change']:+.2f}%)"
                    if d.get("a50_change") is not None
                    else ""
                )
                lines.append(f"A50期货: {d['a50_price']:.2f}{chg}")
            if d.get("crude_oil_price") is not None:
                chg = (
                    f" ({d['crude_oil_change']:+.2f}%)"
                    if d.get("crude_oil_change") is not None
                    else ""
                )
                lines.append(f"WTI原油: {d['crude_oil_price']:.2f}{chg}")
            if d.get("gold_price") is not None:
                chg = (
                    f" ({d['gold_change']:+.2f}%)"
                    if d.get("gold_change") is not None
                    else ""
                )
                lines.append(f"黄金: {d['gold_price']:.2f}{chg}")
            if d.get("usd_cny_rate") is not None:
                lines.append(f"美元/人民币: {d['usd_cny_rate']:.4f}")
            return "\n".join(lines)
        finally:
            conn.close()

    def _get_morning_articles(self) -> dict:
        """采集 CLS 早报 + 早间新闻精选 + 避雷针"""
        try:
            from data.collectors.events.cls_digest_collector import CLSDigestCollector

            collector = CLSDigestCollector()
            result = collector.collect()
            if result:
                self.logger.info(f"CLS 文章已采集: {list(result.keys())}")
            return result or {}
        except Exception as e:
            self.logger.warning(f"CLS 文章采集失败: {e}")
            return {}

    def _get_overnight_telegraphs(self, yesterday: str) -> str:
        """查询隔夜重要电报（昨日15:00后，按 AI 重要度排序）"""
        cutoff_dt = datetime.strptime(yesterday, "%Y-%m-%d").replace(
            hour=15, minute=0, second=0
        )
        cutoff_ts = int(cutoff_dt.timestamp())

        try:
            conn = sqlite3.connect(DATABASE_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT ai_summary, ai_importance, ai_sectors, ai_sentiment, title, ctime
                FROM cls_telegraph
                WHERE trade_date = ? AND ctime >= ?
                  AND ai_status != 'skipped'
                  AND ai_importance >= 3
                ORDER BY ai_importance DESC, ctime DESC
                LIMIT 30
            """,
                (yesterday, cutoff_ts),
            ).fetchall()
            conn.close()

            if not rows:
                return ""

            lines = []
            for r in rows:
                summary = r["ai_summary"] or r["title"] or ""
                if not summary:
                    continue
                imp = r["ai_importance"] or 0
                sentiment = r["ai_sentiment"] or ""
                sentiment_tag = {"利好": "🟢", "利空": "🔴", "中性": "⚪"}.get(
                    sentiment, ""
                )
                sectors = r["ai_sectors"] or ""
                sector_tag = f" [{sectors}]" if sectors else ""
                lines.append(f"• {sentiment_tag}[P{imp}]{sector_tag} {summary}")

            return "\n".join(lines)
        except Exception as e:
            self.logger.warning(f"电报查询失败: {e}")
            return ""

    @staticmethod
    def _fmt_articles(articles: dict) -> str:
        """格式化 CLS 文章为 prompt 文本"""
        if not articles:
            return "（暂无早报文章）"
        parts = []
        for key, label in [("morning", "早报"), ("morning_news", "早间新闻精选")]:
            article = articles.get(key)
            if article and isinstance(article, dict):
                content = article.get("content", "")
                if content:
                    parts.append(f"=== {label} ===\n{content}")
        return "\n\n".join(parts) if parts else "（暂无早报文章）"

    def _match_risk_to_picks(self, risk_text: str, yesterday: str) -> str:
        """交叉匹配：昨日推荐标的 vs 避雷针文本，标注被点名的票"""
        # 查昨日推荐标的
        picks = {}
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT stock_code, stock_name FROM stock_tracker
                WHERE push_date = ? AND source = '复盘'
            """,
                (yesterday,),
            ).fetchall()
            conn.close()
            picks = {r["stock_code"]: r["stock_name"] for r in rows}
        except Exception:
            pass

        if not picks or not risk_text:
            return risk_text

        # 检查每只推荐标的是否出现在避雷针文本中
        hit_codes = []
        for code, name in picks.items():
            if code in risk_text or name in risk_text:
                hit_codes.append(f"{name}({code})")

        if hit_codes:
            header = f"⚠️ 昨日推荐标的被避雷针点名：{', '.join(hit_codes)}\n\n"
            return header + risk_text

        return risk_text

    # ================================================================
    # AI 调用
    # ================================================================

    def _call_ai(self, prompt: str) -> str:
        """调用 AI 生成早盘简报"""
        try:
            from analysis.review.analyzer import AIAnalyzer

            ai = AIAnalyzer()
            system_prompt = (
                "你是一个顶级游资操盘手，做盘前晨会分析。"
                "风格犀利直接，像交易员之间的对话。"
                "所有数值用阿拉伯数字，不要用中文数字。"
            )
            result = ai._call_ai(prompt, system_prompt=system_prompt, max_tokens=4000)
            if result:
                self.logger.info(f"AI 生成成功（{len(result)}字）")
            return result
        except Exception as e:
            self.logger.error(f"AI 调用失败: {e}")
            return None

    # ================================================================
    # 推送
    # ================================================================

    def _send(self, text: str):
        """推送简报。优先 Telegram，降级到 print。"""
        if self.telegram:
            try:
                self.telegram.send(text)
                self.logger.info("早盘简报已推送至 Telegram")
            except Exception as e:
                self.logger.warning(f"Telegram 推送失败: {e}")
                print(f"\n{'=' * 60}\n{text}\n{'=' * 60}")
        else:
            print(f"\n{'=' * 60}\n{text}\n{'=' * 60}")
