# -*- coding: utf-8 -*-
"""AI 选股顾问 — 双模型并行分析

职责：输入 TrendScreener.screen() 输出的 StockScore 列表，
      调用 AI（DeepSeek + 千问）并行分析，
      输出 OrderSignal 列表（含买卖区间+止损止盈+评分+理由）。

AI 不替代量化规则，而是叠加一层判断层。
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from system.config.prompts.ai_advisor import AI_ADVISOR_PROMPT
from analysis.signals import StockProfile, OrderSignal, SignalType, SignalSource, HoldingInfo, AccountSummary
from system.utils.logger import get_system_logger

logger = get_system_logger("advisor")

logger = get_system_logger('ai_advisor')

# ============================================================
# 模型配置
# ============================================================

_MODEL_QWEN = "qwen3.6-plus"
_MODEL_DEEPSEEK = "deepseek-chat"


# ============================================================
# AIAdvisor
# ============================================================


class AIAdvisor:
    """双模型并行分析，输入 StockScore 列表，输出 OrderSignal 列表。"""

    def __init__(self, model: Optional[str] = None):
        """
        Args:
            model: None 使用双模型，'deepseek' 只用 DeepSeek，'qwen' 只用千问
        """
        self._analyzers: List[Tuple[str, "AIAnalyzer"]] = []  # (name, analyzer)

        if model in (None, "qwen"):
            qwen = self._create_analyzer(_MODEL_QWEN)
            if qwen:
                self._analyzers.append(("qwen", qwen))

        if model in (None, "deepseek"):
            ds = self._create_analyzer(_MODEL_DEEPSEEK)
            if ds:
                self._analyzers.append(("deepseek", ds))

        if not self._analyzers:
            logger.warning("没有可用 AI 分析器，请检查 API Key 配置")

    # ------------------------------------------------------------------
    # 内部：创建 AIAnalyzer 实例
    # ------------------------------------------------------------------

    @staticmethod
    def _create_analyzer(model_name: str) -> Optional["AIAnalyzer"]:
        """创建 AIAnalyzer 实例并覆盖模型名。"""
        from analysis.review.analyzer import AIAnalyzer

        # AIAnalyzer.__init__ 要求 DASHSCOPE_API_KEY。
        # 对于 DeepSeek-only 场景，用 DEEPSEEK_API_KEY 的存在作为 fallback。
        if model_name.startswith("deepseek"):
            if not os.getenv("DEEPSEEK_API_KEY"):
                logger.warning("DEEPSEEK_API_KEY 未配置，跳过 DeepSeek 分析器")
                return None
            # AIAnalyzer.__init__ 仍会检查 DASHSCOPE_API_KEY，
            # 设置一个占位避免 init 失败（_call_ai 时用 DEEPSEEK_API_KEY）
            if not os.getenv("DASHSCOPE_API_KEY"):
                os.environ["DASHSCOPE_API_KEY"] = "placeholder"
        else:
            if not os.getenv("DASHSCOPE_API_KEY"):
                logger.warning("DASHSCOPE_API_KEY 未配置，跳过千问分析器")
                return None

        try:
            analyzer = AIAnalyzer()
            analyzer.model = model_name
            logger.info(f"AI 分析器初始化完成（模型: {model_name}）")
            return analyzer
        except ValueError as e:
            logger.warning(f"{model_name} 分析器初始化失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def analyze(
        self,
        candidates: List[StockProfile],
        trade_date: Optional[str] = None,
        holdings: Optional[List[HoldingInfo]] = None,
        account_summaries: Optional[List[AccountSummary]] = None,
    ) -> List[OrderSignal]:
        """
        分析候选股票画像，返回 OrderSignal 列表。

        流程：
        1. 将 StockProfile 列表格式化为 prompt（使用 to_text()）
        2. 并行调用所有可用模型
        3. 解析 JSON 响应
        4. 合并多模型结果
        """
        if not candidates:
            logger.info("候选股票池为空，跳过分析")
            return []

        if not self._analyzers:
            logger.error("没有可用 AI 分析器，无法分析")
            return []

        prompt = self._build_prompt(candidates, trade_date, holdings, account_summaries)
        self._save_prompt(prompt, trade_date)

        # 并行调用各个模型
        raw_results: List[List[OrderSignal]] = []
        with ThreadPoolExecutor(max_workers=len(self._analyzers)) as executor:
            future_map = {
                executor.submit(self._call_and_parse, name, analyzer, prompt): name
                for name, analyzer in self._analyzers
            }
            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    result = future.result(timeout=620)
                    if result:
                        raw_results.append(result)
                        logger.info(f"{name} 分析完成: 生成 {len(result)} 个信号")
                    else:
                        logger.warning(f"{name} 分析结果为空")
                except Exception as e:
                    logger.error(f"{name} 分析失败: {e}")

        if not raw_results:
            logger.error("所有模型分析均失败，返回空列表")
            return []

        # 单模型直接返回，多模型合并
        if len(raw_results) == 1:
            return raw_results[0]
        return self._merge_results(*raw_results)

    # ------------------------------------------------------------------
    # Prompt 构建
    # ------------------------------------------------------------------

    @staticmethod
    def _build_prompt(
        candidates: List[StockProfile],
        trade_date: Optional[str] = None,
        holdings: Optional[List[HoldingInfo]] = None,
        account_summaries: Optional[List[AccountSummary]] = None,
    ) -> str:
        """使用 StockProfile.to_text() 生成每只股票的完整画像文本。"""
        header_date = f"交易日期: {trade_date}" if trade_date else ""

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
                time_warn = " ⚠️时间止损预警" if (h.holding_days > 10 and h.pnl_pct < 0) else ""

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
                    detail_parts.append(f"止损 {h.stop_loss:.2f} (距现价{sl_dist:.1f}%)")
                if h.take_profit > 0:
                    tp_dist = (h.take_profit - h.current_price) / h.current_price * 100
                    detail_parts.append(f"止盈 {h.take_profit:.2f} (距现价{tp_dist:+.1f}%)")
                if h.ma5 > 0:
                    detail_parts.append(f"MA5={h.ma5:.2f}")
                if h.ma20 > 0:
                    detail_parts.append(f"MA20={h.ma20:.2f}")
                if h.highest_price > 0:
                    detail_parts.append(f"日内最高 {h.highest_price:.2f}")
                lines.append(f"    {' | '.join(detail_parts)}")

            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Prompt 落盘
    # ------------------------------------------------------------------

    @staticmethod
    def _save_prompt(prompt: str, trade_date: Optional[str] = None):
        """保存发给 AI 的完整 prompt 到日志目录，方便调试"""
        from pathlib import Path
        from datetime import datetime
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
        name: str,
        analyzer: "AIAnalyzer",
        prompt: str,
    ) -> Optional[List[OrderSignal]]:
        """调用单个模型并解析结果。"""
        system_prompt = (
            "你是专业的A股趋势交易分析师。请严格按要求的JSON格式输出，"
            "只输出JSON（用```json包裹），不要额外解释。"
        )
        try:
            text = analyzer._call_ai(
                prompt=prompt,
                system_prompt=system_prompt,
                max_tokens=4096,
            )
            if not text:
                logger.warning(f"{name} 返回空内容")
                return None
            return AIAdvisor._parse_json_response(text, name)
        except Exception as e:
            logger.error(f"{name} 调用异常: {e}")
            return None

    @staticmethod
    def _parse_json_response(text: str, model_name: str) -> Optional[List[OrderSignal]]:
        """从 AI 回复中提取 JSON 并解析为 OrderSignal 列表。"""
        # 提取 ```json {...} ``` 包裹的 JSON
        json_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL
        )
        json_str = json_match.group(1) if json_match else text.strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            # 尝试找第一个 { 和最后一个 } 截取
            start = json_str.find("{")
            end = json_str.rfind("}")
            if start != -1 and end > start:
                try:
                    data = json.loads(json_str[start : end + 1])
                except json.JSONDecodeError as e:
                    logger.error(f"{model_name} JSON 解析失败: {e}")
                    return None
            else:
                logger.error(f"{model_name} 响应中未找到 JSON: {text[:200]}")
                return None

        stocks = data.get("stocks", [])
        if not stocks:
            logger.warning(f"{model_name} 响应中 stocks 为空")
            return None

        signals = []
        for item in stocks:
            signal = AIAdvisor._parse_stock_result(item, model_name)
            if signal:
                signals.append(signal)

        if not signals:
            logger.warning(f"{model_name} 无有效解析结果")
            return None

        return signals

    @staticmethod
    def _parse_stock_result(
        item: dict,
        model_name: str,
    ) -> Optional[OrderSignal]:
        """将单个股票 JSON 对象解析为 OrderSignal。"""
        action = item.get("action", "skip")
        if action != "buy":
            return None

        stock_code = str(item.get("stock_code", ""))
        if not stock_code:
            return None

        confidence = item.get("confidence", 0)
        if not isinstance(confidence, (int, float)) or confidence <= 0:
            return None

        return OrderSignal(
            stock_code=stock_code,
            stock_name=str(item.get("stock_name", "")),
            signal_type=SignalType.BUY,
            source=SignalSource.AI_ENHANCED,
            buy_zone_min=AIAdvisor._safe_float(item, "buy_zone_min"),
            buy_zone_max=AIAdvisor._safe_float(item, "buy_zone_max"),
            stop_loss=AIAdvisor._safe_float(item, "stop_loss"),
            take_profit=AIAdvisor._safe_float(item, "take_profit"),
            target_position=0.10,  # 默认 10% 仓位
            signal_score=float(confidence),
            strategy_name=f"ai_advisor_{model_name}",
            reason=str(item.get("reason", "")),
            expected_trend=str(item.get("expected_trend", "")),
            sector_name=str(item.get("sector_name", "")),
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

            merged.append(OrderSignal(
                stock_code=code,
                stock_name=s1.stock_name or s2.stock_name,
                signal_type=SignalType.BUY,
                source=SignalSource.AI_ENHANCED,
                buy_zone_min=buy_min,
                buy_zone_max=buy_max,
                stop_loss=sl,
                take_profit=tp,
                target_position=s1.target_position or s2.target_position or 0.10,
                signal_score=round(score, 1),
                strategy_name="ai_advisor_merged",
                reason=reason,
                sector_name=s1.sector_name or s2.sector_name,
            ))

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
