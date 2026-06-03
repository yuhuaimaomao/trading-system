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

        # 7. 调用 AI（支持 FC，AI 自行决定是否调用 get_pending_signals）
        brief, adjustments = self._call_ai_with_fc(prompt, yesterday)

        if not brief:
            self.logger.error("AI 生成早盘简报失败")
            return

        # 8. 应用修正
        if adjustments:
            applied = self._apply_adjustments(adjustments, yesterday)
            self.logger.info(f"早盘校准: {applied} 条修正已应用")

        # 9. 清理修正块 + 添加标题行 + 推送
        clean_text = self._remove_adjustments(brief)
        full_text = f"⚔️ 刺客早盘 {trade_date}\n\n{clean_text}"
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
    # AI 调用（支持 FC）
    # ================================================================

    def _call_ai_with_fc(self, prompt: str, yesterday: str) -> tuple:
        """调用 AI 生成早报，支持 FC 自主判断是否校准。返回 (文本, adjustments列表)。"""

        from analysis.review.analyzer import AIAnalyzer
        from system.utils.function_calling import FunctionCallingEngine
        from system.utils.stock_tools import TOOLS_DEFINITION

        ai = AIAnalyzer()
        fc_engine = FunctionCallingEngine()
        system_prompt = (
            "你是一个顶级游资操盘手，做盘前晨会分析。"
            "风格犀利直接，像交易员之间的对话。"
            "所有数值用阿拉伯数字，不要用中文数字。"
            "你可以调用 get_pending_signals 工具获取昨日推荐标的的精确列表。"
        )

        # 只给早报相关的工具（避免 tool_choice=auto 时乱调）
        morning_tools = [
            t
            for t in TOOLS_DEFINITION
            if t["function"]["name"] == "get_pending_signals"
        ]

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        content = ""
        round_num = 0

        while round_num < 4:  # 最多4轮，防止死循环
            round_num += 1
            response = ai._call_ai_with_tools(
                messages,
                tools=morning_tools,
                tool_choice="auto",
                max_tokens=6000,  # 文字简报800字 + ADJUSTMENTS JSON(可能200-500 token) + FC 中间回复
            )

            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])

            if tool_calls:
                self.logger.info(f"FC 第{round_num}轮: {len(tool_calls)} 个工具调用")
                assistant_msg = {"role": "assistant", "content": content or ""}
                assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)

                tool_messages = fc_engine.process_tool_calls(tool_calls)
                messages.extend(tool_messages)
                continue

            # 无工具调用 = AI 认为不需要校准，或者已经完成
            if content:
                break

        if not content:
            self.logger.error("AI 返回空")
            return None, []

        # 解析修正块
        adjustments = self._parse_adjustments(content)
        return content, adjustments

    def _parse_adjustments(self, text: str) -> list:
        """从 AI 响应中解析 <<<ADJUSTMENTS>>> 结构化修正指令。"""
        import json as _json
        import re

        # 标准模式：<<<ADJUSTMENTS>>> ... <<<END>>>
        match = re.search(r"<<<ADJUSTMENTS>>>(.*?)<<<END>>>", text, re.DOTALL)
        # 兜底：没有 <<<END>>>，取到文末
        if not match:
            match = re.search(r"<<<ADJUSTMENTS>>>(.*)", text, re.DOTALL)
        if not match:
            return []

        raw = match.group(1).strip()
        # 清理 markdown 代码块标记
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)

        try:
            data = _json.loads(raw)
            if isinstance(data, list):
                self.logger.info(f"✅ 解析到 {len(data)} 条修正指令")
                return data
        except _json.JSONDecodeError as e:
            # DeepSeek 有时在 JSON 后追加说明文字，用 raw_decode 只取第一个完整值
            if "Extra data" in str(e) or "extra data" in str(e):
                try:
                    decoder = _json.JSONDecoder()
                    data, _ = decoder.raw_decode(raw)
                    if isinstance(data, list):
                        self.logger.info(
                            f"✅ raw_decode 解析到 {len(data)} 条修正指令（忽略尾部文字）"
                        )
                        return data
                except _json.JSONDecodeError:
                    pass
            self.logger.warning(f"ADJUSTMENTS JSON 解析失败: {e}")

        return []

    def _apply_adjustments(self, adjustments: list, trade_date: str) -> int:
        """应用修正指令到 trade_signals 表。"""
        import sqlite3

        conn = sqlite3.connect(DATABASE_PATH)
        applied = 0

        for adj in adjustments:
            code = adj.get("stock_code", "")
            action = adj.get("action", "")
            reason = adj.get("reason", "")

            if action == "cancel":
                conn.execute(
                    """UPDATE trade_signals SET status='cancelled',
                       reason=reason || ' [早盘校准: ' || ? || ']'
                       WHERE stock_code=? AND trade_date=? AND status='pending'""",
                    (reason, code, trade_date),
                )
                self.logger.info(f"  ❌ 移除: {code} ({reason})")
                applied += 1

            elif action == "adjust":
                changes = {
                    k: v
                    for k, v in adj.items()
                    if k
                    in (
                        "new_buy_zone_min",
                        "new_buy_zone_max",
                        "new_stop_loss",
                        "new_take_profit",
                        "new_score",
                    )
                }
                if not changes:
                    continue
                # 构建 SET 子句
                field_map = {
                    "new_buy_zone_min": "buy_zone_min",
                    "new_buy_zone_max": "buy_zone_max",
                    "new_stop_loss": "stop_loss",
                    "new_take_profit": "take_profit",
                    "new_score": "signal_score",
                }
                sets = []
                params = []
                for adj_key, adj_val in changes.items():
                    db_col = field_map.get(adj_key)
                    if db_col:
                        sets.append(f"{db_col}=?")
                        params.append(adj_val)
                if not sets:
                    continue
                sets.append("reason=reason || ' [早盘校准: ' || ? || ']'")
                params.append(reason)
                params.extend([code, trade_date])
                conn.execute(
                    f"UPDATE trade_signals SET {', '.join(sets)} "
                    f"WHERE stock_code=? AND trade_date=? AND status='pending'",
                    params,
                )
                self.logger.info(f"  🔧 调整: {code} {list(changes.keys())} ({reason})")
                applied += 1

            elif action == "downgrade":
                new_score = adj.get("new_score")
                if new_score is not None:
                    conn.execute(
                        """UPDATE trade_signals SET signal_score=?,
                           reason=reason || ' [早盘校准: ' || ? || ']'
                           WHERE stock_code=? AND trade_date=? AND status='pending'""",
                        (new_score, reason, code, trade_date),
                    )
                    self.logger.info(f"  ⬇ 降级: {code} score→{new_score} ({reason})")
                    applied += 1

            elif action in ("focus", "avoid", "selective"):
                sector = adj.get("sector", "")
                if not sector:
                    continue
                import json

                conn.execute(
                    """INSERT OR REPLACE INTO morning_sector_bias
                       (trade_date, sector_name, bias, priority, size_multiplier, stock_codes, reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trade_date,
                        sector,
                        action,
                        adj.get("priority", 3),
                        adj.get("size_multiplier", 1.0),
                        json.dumps(adj.get("stock_codes", [])),
                        reason,
                    ),
                )
                emoji = {"focus": "🎯", "avoid": "🚫", "selective": "🔍"}.get(
                    action, ""
                )
                self.logger.info(
                    f"  {emoji} 板块倾向: {sector} {action} "
                    f"priority={adj.get('priority', 3)} mult={adj.get('size_multiplier', 1.0)} ({reason})"
                )
                applied += 1

        conn.commit()
        conn.close()
        return applied

    def _remove_adjustments(self, text: str) -> str:
        """从推送文本中删除修正块（仅系统解析用，不推送给用户）。"""
        import re

        # 标准模式：<<<ADJUSTMENTS>>> ... <<<END>>>
        cleaned = re.sub(r"<<<ADJUSTMENTS>>>.*?<<<END>>>", "", text, flags=re.DOTALL)
        # 兜底：如果 AI 漏掉了 <<<END>>>，从标记截断到文末
        idx = cleaned.find("<<<ADJUSTMENTS>>>")
        if idx != -1:
            cleaned = cleaned[:idx]
        cleaned = re.sub(r"\n\s*\n\s*\n", "\n\n", cleaned)
        return cleaned.strip()

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
