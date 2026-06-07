"""AI 选股顾问 — 单模型分析

职责：输入 TrendScreener.screen() 输出的 StockScore 列表，
      调用 AI 分析，
      输出 OrderSignal 列表（含买卖区间+止损止盈+评分+理由）。

AI 不替代量化规则，而是叠加一层判断层。
"""

from __future__ import annotations

import json
import re

from stock.signals import (
    AccountSummary,
    HoldingInfo,
    HoldingReview,
    OrderSignal,
    ReviewContext,
    SignalSource,
    SignalType,
    StockProfile,
    StrategyAiDecision,
    StrategyAiResult,
)
from system.ai.prompts.strategy import AI_ADVISOR_PROMPT
from system.config import settings
from system.utils.logger import get_strategy_logger

logger = get_strategy_logger("pipeline")

# ============================================================
# 模型配置
# ============================================================

_MODEL = settings.AI_MODEL


# ============================================================
# AIAdvisor
# ============================================================


class AIAdvisor:
    """单模型分析，输入 StockScore 列表，输出 OrderSignal 列表。"""

    def __init__(self, model: Optional[str] = None, db_path: str = None):
        """
        Args:
            model: 模型名，None 时使用 settings.AI_MODEL
            db_path: 测试时传入临时库路径，None 则用生产库
        """
        self._db_path = db_path
        model_name = model or _MODEL
        if model_name:
            self._model = model_name
            logger.info(f"AI 分析器就绪（模型: {model_name}）")
        else:
            logger.warning("没有可用 AI 模型，请检查 AI_MODEL 配置")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def analyze(
        self,
        candidates: List[StockProfile],
        trade_date: Optional[str] = None,
        holdings: Optional[List[HoldingInfo]] = None,
        account_summaries: Optional[List[AccountSummary]] = None,
        review_context: Optional[ReviewContext] = None,
    ) -> tuple[List[OrderSignal], List[HoldingReview]]:
        """
        分析候选股票画像，返回 (OrderSignal 列表, 持仓审查列表)。

        流程：
        1. 将 StockProfile 列表格式化为 prompt（使用 to_text()）
        2. 并行调用所有可用模型
        3. 解析 JSON 响应
        4. 合并多模型结果
        """
        if not candidates:
            logger.info("候选股票池为空，跳过分析")
            return [], []

        prompt = self._build_prompt(
            candidates, trade_date, holdings, account_summaries, review_context
        )
        self._save_prompt(prompt, trade_date)

        # 调用 AI
        raw_signals: List[List[OrderSignal]] = []
        all_holdings_reviews: List[HoldingReview] = []
        result = self._call_and_parse(prompt, trade_date, self._model)
        if result:
            signals, hr_list, ai_result = result
            if signals:
                raw_signals.append(signals)
                logger.info(
                    f"AI 分析完成: 生成 {len(signals)} 个信号, "
                    f"决策 {len(ai_result.decisions)} 条"
                )
            if hr_list:
                all_holdings_reviews.extend(hr_list)

        if not raw_signals:
            logger.error("所有模型分析均失败，返回空列表")
            return [], all_holdings_reviews

        return raw_signals[0] if raw_signals else ([], all_holdings_reviews)

    # ------------------------------------------------------------------
    # Prompt 构建
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        candidates: List[StockProfile],
        trade_date: Optional[str] = None,
        holdings: Optional[List[HoldingInfo]] = None,
        account_summaries: Optional[List[AccountSummary]] = None,
        review_context: Optional[ReviewContext] = None,
    ) -> str:
        """使用 StockProfile.to_text() 生成每只股票的完整画像文本。"""
        header_date = f"交易日期: {trade_date}" if trade_date else ""

        # 复盘上下文
        review_text = review_context.to_text() if review_context else ""

        # 持仓数据
        holdings_text = AIAdvisor._format_holdings(holdings, account_summaries)

        # 候选股票池
        header = f"## 候选股票池 {header_date}".strip()
        market_state = candidates[0].market_state if candidates else ""
        if market_state:
            header += f"\n大盘环境: {market_state}"

        profile_texts = [header, ""]
        for p in candidates:
            profile_texts.append(p.to_text())
            profile_texts.append("")

        candidates_text = "\n".join(profile_texts)

        prompt = AI_ADVISOR_PROMPT.format(
            review_context=review_text,
            holdings_data=holdings_text,
            candidates_data=candidates_text,
        )
        return prompt

    @staticmethod
    def _format_holdings(
        holdings: Optional[List[HoldingInfo]],
        summaries: Optional[List[AccountSummary]],
    ) -> str:
        """格式化持仓数据为 prompt 文本。"""
        if not holdings:
            return ""

        lines = ["## 当前持仓（需要你审查）", ""]

        # 账户概况
        if summaries:
            for s in summaries:
                emoji = "📊" if s.account == "paper" else "💰"
                lines.append(
                    f"{emoji} {s.label}账户: 总资产 {s.total_value:,.0f} | "
                    f"现金 {s.cash:,.0f} | 仓位 {s.position_ratio:.1%} | "
                    f"今日盈亏 {s.daily_pnl:+,.0f} | 持仓 {s.position_count} 只"
                )
            lines.append("")

        # 按账户分组展示
        for account in ("real", "paper"):
            acct_holdings = [h for h in holdings if h.account == account]
            if not acct_holdings:
                continue

            label = "实盘" if account == "real" else "模拟盘"
            lines.append(f"--- {label}持仓 ---")

            for h in acct_holdings:
                pnl_emoji = "🟢" if h.pnl_pct > 2 else "🟡" if h.pnl_pct > -2 else "🔴"
                t1_lock = " 🔒T+1" if h.is_today_buy else ""
                time_warn = (
                    " ⚠️时间止损预警" if (h.holding_days > 10 and h.pnl_pct < 0) else ""
                )

                lines.append(
                    f"  {h.stock_code} {h.stock_name} | {h.industry or '未知行业'} | "
                    f"持有{h.holding_days}天{t1_lock}{time_warn}"
                )
                lines.append(
                    f"    成本 {h.avg_cost:.2f} | 现价 {h.current_price:.2f} | "
                    f"盈亏 {pnl_emoji}{h.pnl_pct:+.1f}% | 市值 {h.market_value:,.0f}"
                )
                detail_parts = []
                if h.stop_loss > 0:
                    sl_dist = (h.current_price - h.stop_loss) / h.current_price * 100
                    detail_parts.append(
                        f"止损 {h.stop_loss:.2f} (距现价{sl_dist:.1f}%)"
                    )
                if h.take_profit > 0:
                    tp_dist = (h.take_profit - h.current_price) / h.current_price * 100
                    detail_parts.append(
                        f"止盈 {h.take_profit:.2f} (距现价{tp_dist:+.1f}%)"
                    )
                if h.ma5 > 0:
                    detail_parts.append(f"MA5={h.ma5:.2f}")
                if h.ma20 > 0:
                    detail_parts.append(f"MA20={h.ma20:.2f}")
                if h.highest_price > 0:
                    detail_parts.append(f"日内最高 {h.highest_price:.2f}")
                lines.append(f"    {' | '.join(detail_parts)}")

                # 完整技术画像
                if h.profile:
                    lines.append("")
                    lines.append(h.profile.to_text())
                    lines.append("")

            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Prompt 落盘
    # ------------------------------------------------------------------

    @staticmethod
    def _save_prompt(prompt: str, trade_date: Optional[str] = None):
        """保存发给 AI 的完整 prompt 到日志目录，方便调试"""
        from datetime import datetime
        from pathlib import Path

        from system.config.settings import LOGS_DIR

        date_str = trade_date or datetime.now().strftime("%Y-%m-%d")
        prompt_dir = Path(LOGS_DIR) / date_str / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompt_dir / "strategy_prompt.txt"
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"策略管线 AI Prompt - {current_time}\n")
            f.write("=" * 80 + "\n\n")
            f.write(prompt)
            f.write("\n\n" + "=" * 80 + "\n")
            f.write(f"Prompt 总字数：{len(prompt)}字\n")
        logger.info(f"Prompt 已落盘: {prompt_path}")

    # ------------------------------------------------------------------
    # 调用 & 解析
    # ------------------------------------------------------------------

    @staticmethod
    def _call_and_parse(
        prompt: str,
        trade_date: Optional[str] = None,
        model_name: str = "strategy",
    ) -> Optional[tuple[List[OrderSignal], List[HoldingReview], StrategyAiResult]]:
        import time

        from system.ai import ai

        start = time.time()
        system_prompt = (
            "你是专业的A股趋势交易分析师。请严格按要求的JSON格式输出，"
            "只输出JSON（用```json包裹），不要额外解释。"
        )
        try:
            text = ai.chat(
                prompt=prompt,
                model="screening",
                system_prompt=system_prompt,
                max_tokens=16384,
            )
            elapsed = int((time.time() - start) * 1000)
            if not text:
                logger.warning("AI 返回空内容")
                return None

            signals, holdings_review, ai_result = AIAdvisor._parse_json_response(
                text, "strategy"
            )
            ai_result.model_used = model_name
            ai_result.raw_response = text

            # 保存 AI 原始返回结果到 reports
            AIAdvisor._save_response(text, model_name, trade_date, holdings_review)

            AIAdvisor._save_ai_decisions(model_name, trade_date, ai_result.decisions)

            if not signals:
                logger.warning(f"{model_name} 无有效信号")
                return None

            return signals, holdings_review, ai_result
        except Exception as e:
            logger.error(f"{model_name} 调用异常: {e}")
            return None

    @staticmethod
    def _save_ai_decisions(
        model_name: str,
        trade_date: Optional[str],
        decisions: List,
    ):
        from datetime import datetime

        from data.repo import TradeRepository
        from system.config.settings import DATABASE_PATH

        if not decisions:
            return

        repo = TradeRepository(db_path=DATABASE_PATH)
        trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().isoformat()

        decision_rows = []
        for d in decisions:
            decision_rows.append(
                {
                    "push_date": trade_date,
                    "trade_date": trade_date,
                    "stock_code": d.stock_code,
                    "stock_name": d.stock_name,
                    "rank_in_prompt": d.rank_in_prompt,
                    "verdict": d.verdict,
                    "confidence": d.confidence,
                    "what_i_see": d.what_i_see,
                    "what_concerns_me": d.what_concerns_me,
                    "decisive_factor": d.decisive_factor,
                    "skip_reason": d.skip_reason,
                    "would_reconsider_if": d.would_reconsider_if,
                    "buy_zone_min": d.buy_zone_min,
                    "buy_zone_max": d.buy_zone_max,
                    "stop_loss": d.stop_loss,
                    "take_profit": d.take_profit,
                    "pricing_logic": d.pricing_logic,
                    "signal_id": d.signal_id,
                    "created_at": now,
                }
            )
        repo.insert_ai_decisions_batch(decision_rows)
        logger.info(f"AI 决策已入库: {len(decisions)} 条")

    @staticmethod
    def _save_response(
        text: str,
        model_name: str,
        trade_date: Optional[str] = None,
        holdings_review: Optional[List[HoldingReview]] = None,
    ):
        """保存 AI 原始返回结果到 reports 文件夹"""
        from datetime import datetime
        from pathlib import Path

        from system.config.settings import STORAGE_PATH

        date_str = trade_date or datetime.now().strftime("%Y-%m-%d")
        report_dir = Path(STORAGE_PATH) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")

        report_path = (
            report_dir / f"strategy_ai_response_{date_str}_{model_name}_{ts}.txt"
        )
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"模型: {model_name}\n")
            f.write(f"日期: {date_str}\n")
            f.write(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 60 + "\n\n")
            f.write(text)
        logger.info(f"AI 原始返回已落盘: {report_path}")

    @staticmethod
    def _parse_json_response(
        text: str, model_name: str
    ) -> tuple[List[OrderSignal], List[HoldingReview], StrategyAiResult]:
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        json_str = json_match.group(1) if json_match else text.strip()

        start = json_str.find("{")
        end = json_str.rfind("}")
        if start == -1 or end <= start:
            logger.error(f"{model_name} 响应中未找到 JSON: {text[:200]}")
            return (
                [],
                [],
                StrategyAiResult(
                    model_used=model_name, decisions=[], holdings_review=[]
                ),
            )

        json_str = json_str[start : end + 1]

        # 尝试完整解析
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            # 截断恢复：从最后一个完整的 stock 对象处截断
            logger.warning(f"{model_name} JSON 不完整，尝试截断恢复")
            data = AIAdvisor._recover_truncated_json(json_str, model_name)
            if data is None:
                return (
                    [],
                    [],
                    StrategyAiResult(
                        model_used=model_name, decisions=[], holdings_review=[]
                    ),
                )

        stocks = data.get("stocks", [])
        holdings_raw = data.get("holdings_review", [])
        meta = data.get("meta", {})

        decisions = []
        for i, item in enumerate(stocks):
            decision = AIAdvisor._parse_ai_decision(item, i + 1)
            decisions.append(decision)

        # 从 decisions 中提取 buy 的 OrderSignal（保持向后兼容）
        signals = []
        for d in decisions:
            if d.verdict == "buy":
                confidence_map = {"high": 85, "medium": 70, "low": 55}
                score = confidence_map.get(d.confidence, 65)
                signals.append(
                    OrderSignal(
                        stock_code=d.stock_code,
                        stock_name=d.stock_name,
                        signal_type=SignalType.BUY,
                        source=SignalSource.AI_ENHANCED,
                        buy_zone_min=d.buy_zone_min,
                        buy_zone_max=d.buy_zone_max,
                        stop_loss=d.stop_loss,
                        take_profit=d.take_profit,
                        target_position=settings.DEFAULT_POSITION_PCT,
                        signal_score=float(score),
                        strategy_name=f"ai_advisor_{model_name}",
                        reason=d.decisive_factor,
                    )
                )

        holdings_review = []
        for item in holdings_raw:
            hr = AIAdvisor._parse_holding_review(item)
            if hr:
                holdings_review.append(hr)

        if holdings_review:
            logger.info(f"{model_name} 持仓审查: {len(holdings_review)} 条建议")

        ai_result = StrategyAiResult(
            model_used=model_name,
            decisions=decisions,
            holdings_review=holdings_review,
            self_assessment=meta.get("self_assessment", ""),
            raw_response=text,
        )
        return signals, holdings_review, ai_result

    @staticmethod
    def _recover_truncated_json(json_str: str, model_name: str) -> Optional[dict]:
        """JSON 截断恢复：回退到最后一个完整的 stock 对象。"""
        # 找到最后一个完整的 "stock_code" 之前的 }, 处截断
        last_complete = 0
        depth = 0
        for i, ch in enumerate(json_str):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 1:  # 回到 stocks 数组层级，一个 stock 对象结束
                    last_complete = i + 1

        if last_complete == 0:
            logger.error(f"{model_name} 截断恢复失败：无完整 stock 对象")
            return None

        repaired = json_str[:last_complete] + "\n    ]\n  }"
        try:
            data = json.loads(repaired)
            n = len(data.get("stocks", []))
            logger.info(f"{model_name} 截断恢复成功：保留 {n} 个完整决策")
            return data
        except json.JSONDecodeError as e:
            logger.error(f"{model_name} 截断恢复失败: {e}")
            return None

    @staticmethod
    def _parse_ai_decision(item: dict, rank: int) -> StrategyAiDecision:
        # --- 向后兼容：同时支持新旧 prompt 格式 ---
        # 旧格式: action + 数字 confidence + 平铺 pricing 字段
        # 新格式: verdict + 字符串 confidence + reasoning/pricing 子对象

        # verdict: 新格式用 verdict，旧格式用 action
        verdict = item.get("verdict") or item.get("action", "skip")
        # 标准化旧格式中非 buy/skip 的值（hold/reduce/close）
        if verdict not in ("buy", "skip"):
            verdict = "skip"

        # confidence: 数字(旧)映射到字符串(新)
        confidence = item.get("confidence", "")
        if isinstance(confidence, (int, float)):
            if confidence > 80:
                confidence = "high"
            elif confidence > 65:
                confidence = "medium"
            else:
                confidence = "low"
        confidence = str(confidence)

        # reasoning: 新格式有子对象，旧格式用 reason 字段
        reasoning = item.get("reasoning", {})
        if reasoning:
            what_i_see = reasoning.get("what_i_see", "")
            what_concerns_me = reasoning.get("what_concerns_me", "")
            decisive_factor = reasoning.get("decisive_factor", "")
        else:
            what_i_see = item.get("reason", "")
            what_concerns_me = item.get("key_risk", "")
            decisive_factor = ""

        # skip 相关字段
        skip_reason = item.get("skip_reason", "")
        would_reconsider_if = item.get("would_reconsider_if", "")

        # pricing: 新格式嵌套在 pricing 子对象，旧格式平铺在 item 上
        pricing = item.get("pricing", {})
        if pricing:
            buy_zone_min = AIAdvisor._safe_float(pricing, "buy_zone_min")
            buy_zone_max = AIAdvisor._safe_float(pricing, "buy_zone_max")
            stop_loss = AIAdvisor._safe_float(pricing, "stop_loss")
            take_profit = AIAdvisor._safe_float(pricing, "take_profit")
            pricing_logic = pricing.get("pricing_logic", "")
        else:
            buy_zone_min = AIAdvisor._safe_float(item, "buy_zone_min")
            buy_zone_max = AIAdvisor._safe_float(item, "buy_zone_max")
            stop_loss = AIAdvisor._safe_float(item, "stop_loss")
            take_profit = AIAdvisor._safe_float(item, "take_profit")
            # 旧格式用 expected_trend 作为定价逻辑
            pricing_logic = item.get("expected_trend", "") or item.get(
                "pricing_logic", ""
            )

        return StrategyAiDecision(
            stock_code=str(item.get("stock_code", "")),
            stock_name=str(item.get("stock_name", "")),
            rank_in_prompt=rank,
            verdict=verdict,
            confidence=confidence,
            what_i_see=what_i_see,
            what_concerns_me=what_concerns_me,
            decisive_factor=decisive_factor,
            skip_reason=skip_reason,
            would_reconsider_if=would_reconsider_if,
            buy_zone_min=buy_zone_min,
            buy_zone_max=buy_zone_max,
            stop_loss=stop_loss,
            take_profit=take_profit,
            pricing_logic=pricing_logic,
        )

    @staticmethod
    def _safe_float(item: dict, key: str) -> Optional[float]:
        """安全提取浮点数。"""
        val = item.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_holding_review(item: dict) -> Optional[HoldingReview]:
        """解析持仓审查条目。"""
        code = str(item.get("stock_code", ""))
        action = item.get("action", "")
        if not code or action not in ("hold", "add", "reduce", "close"):
            return None
        return HoldingReview(
            stock_code=code,
            account=str(item.get("account", "")),
            action=action,
            new_stop_loss=AIAdvisor._safe_float(item, "new_stop_loss"),
            new_take_profit=AIAdvisor._safe_float(item, "new_take_profit"),
            expected_holding_days=item.get("expected_holding_days"),
            tomorrow_outlook=str(item.get("tomorrow_outlook", "")),
            reason=str(item.get("reason", "")),
        )

    # ------------------------------------------------------------------
    # 结果合并
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_results(
        *result_lists: List[OrderSignal],
    ) -> List[OrderSignal]:
        """
        合并多个模型的分析结果。

        合并策略：
        - 买入区间：取两者的平均值
        - 止损：取较高值（更严格）
        - 止盈：取较低值（更保守）
        - 信号分：(score1 + score2) / 2
        - 只出现在一个模型中的股票，原样保留
        """
        if not result_lists:
            return []

        if len(result_lists) == 1:
            return result_lists[0]

        # 按 stock_code 索引
        indexed: List[dict] = []
        for signals in result_lists:
            indexed.append({s.stock_code: s for s in signals})

        all_codes = set()
        for idx in indexed:
            all_codes.update(idx.keys())

        merged: List[OrderSignal] = []
        for code in all_codes:
            signals_present = [idx.get(code) for idx in indexed]
            signals_present = [s for s in signals_present if s is not None]

            if not signals_present:
                continue

            if len(signals_present) == 1:
                merged.append(signals_present[0])
                continue

            # 多个模型结果合并
            s1, s2 = signals_present[0], signals_present[1]

            buy_min = AIAdvisor._avg_optional(s1.buy_zone_min, s2.buy_zone_min)
            buy_max = AIAdvisor._avg_optional(s1.buy_zone_max, s2.buy_zone_max)
            sl = AIAdvisor._max_optional(s1.stop_loss, s2.stop_loss)
            tp = AIAdvisor._min_optional(s1.take_profit, s2.take_profit)
            score = (s1.signal_score + s2.signal_score) / 2
            reason = f"{s1.reason} | {s2.reason}"

            merged.append(
                OrderSignal(
                    stock_code=code,
                    stock_name=s1.stock_name or s2.stock_name,
                    signal_type=SignalType.BUY,
                    source=SignalSource.AI_ENHANCED,
                    buy_zone_min=buy_min,
                    buy_zone_max=buy_max,
                    stop_loss=sl,
                    take_profit=tp,
                    target_position=s1.target_position
                    or s2.target_position
                    or settings.DEFAULT_POSITION_PCT,
                    signal_score=round(score, 1),
                    strategy_name="ai_advisor_merged",
                    reason=reason,
                    sector_name=s1.sector_name or s2.sector_name,
                )
            )

        return merged

    @staticmethod
    def _avg_optional(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is not None and b is not None:
            return round((a + b) / 2, 2)
        return a if a is not None else b

    @staticmethod
    def _max_optional(a: Optional[float], b: Optional[float]) -> Optional[float]:
        """取较高值（更严格的止损，因为止损价越高越早触发）。"""
        if a is not None and b is not None:
            return round(max(a, b), 2)
        return a if a is not None else b

    @staticmethod
    def _min_optional(a: Optional[float], b: Optional[float]) -> Optional[float]:
        """取较低值（更保守的止盈）。"""
        if a is not None and b is not None:
            return round(min(a, b), 2)
        return a if a is not None else b
