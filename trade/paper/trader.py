# -*- coding: utf-8 -*-
"""模拟盘自动交易 — 信号触发时自动买卖，20万初始资金

费率（A股实际标准）：
  佣金: 万0.85, 最低5元
  印花税: 万分之五（卖出减半征收）
"""

import json
import logging
import os
import re
import time
from datetime import datetime

import requests

from data.repo import TradeRepository
from system.config import settings
from trade.portfolio.portfolio import Portfolio

logger = logging.getLogger(__name__)

INITIAL_CAPITAL = 200_000
POSITION_PCT = 0.10  # 每只票占仓位 10%
MAX_POSITIONS = 5
COMMISSION_RATE = 0.000085  # 万0.85
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.0005  # 万分之五（2023年8月减半后标准，卖出单边征收）
SWAP_SCORE_GAP = 15  # 新信号比最弱持仓高 15 分以上才考虑换仓


class PaperTrader:
    """模拟盘自动交易器。信号买点触发自动买，止损止盈自动卖。"""

    def __init__(self, db_path: str, telegram_bot=None):
        self.portfolio = Portfolio(initial_cash=INITIAL_CAPITAL)
        self.db_path = db_path
        self.telegram = telegram_bot
        self.trade_date = datetime.now().strftime("%Y-%m-%d")
        self.repo = TradeRepository()

    # ------------------------------------------------------------------
    # 买入
    # ------------------------------------------------------------------

    def try_buy(self, code: str, name: str, price: float,
                buy_min: float, buy_max: float, sl: float, tp: float,
                score: float = 0, source: str = "signal",
                max_amount: float | None = None,
                sector: str = "") -> bool:
        """信号进入买入区间时尝试模拟买入。

        max_amount: 最大买入金额，None 表示用默认仓位比例。
        """
        if code in self.portfolio.positions:
            return False
        if len(self.portfolio.positions) >= MAX_POSITIONS:
            return self._try_swap(code, name, price, buy_min, buy_max, sl, tp,
                                  score, source, max_amount)

        # 动态仓位：max_amount 优先，否则用默认比例
        if max_amount is not None:
            capital = min(max_amount, self.portfolio.total_value * POSITION_PCT)
        else:
            capital = self.portfolio.total_value * POSITION_PCT

        volume = int(capital / price / 100) * 100
        if volume < 100:
            logger.info(f"模拟盘资金不足买入 {code}")
            return False

        # 买入佣金
        cost = volume * price
        commission = max(cost * COMMISSION_RATE, MIN_COMMISSION)
        total_cost = cost + commission
        if total_cost > self.portfolio.cash:
            volume = int((self.portfolio.cash * 0.9 - commission) / price / 100) * 100
            if volume < 100:
                return False

        ok = self.portfolio.open_position(
            stock_code=code, stock_name=name, volume=volume, price=price,
            sector_code=sector, entry_date=self.trade_date,
            stop_loss=sl, take_profit=tp, commission=commission,
        )
        if not ok:
            return False

        self._record_order(code, name, "buy", volume, price, source, score,
                           commission=commission)

        pos_count = len(self.portfolio.positions)
        pnl_str = self._portfolio_summary()
        if self.telegram:
            self.telegram.send(
                f"📝 模拟盘买入: {code} {name}\n"
                f"价格 {price:.2f}  {volume}股  金额 {cost:.0f}  佣金 {commission:.1f}\n"
                f"止损 {sl:.2f}  止盈 {tp:.2f}  评分 {score:.0f}\n"
                f"持仓 {pos_count}/{MAX_POSITIONS}  {pnl_str}"
            )
        logger.info(f"模拟盘买入: {code} {name} {volume}股 @{price:.2f}")
        return True

    # ------------------------------------------------------------------
    # 换仓
    # ------------------------------------------------------------------

    def _try_swap(self, code: str, name: str, price: float,
                  buy_min: float, buy_max: float, sl: float, tp: float,
                  score: float, source: str, max_amount: float | None,
                  candidates: list[dict] = None) -> bool:
        """持仓满时评估换仓：AI 实时判断优先，规则兜底。"""
        cand_list = candidates or [{"code": code, "name": name, "price": price,
                                     "score": score, "sl": sl, "tp": tp}]
        result = self._ai_evaluate_swap(cand_list)
        if not result:
            # fallback
            result = self._rule_swap_target(code, score)

        if not result:
            logger.info(f"模拟盘换仓评估: 无合适卖出标的，跳过 {code}")
            return False

        sell_code = result
        sell_pos = self.portfolio.positions[sell_code]
        sell_price = sell_pos.current_price or price
        logger.info(f"模拟盘换仓: 卖出 {sell_code} {sell_pos.stock_name}@{sell_price:.2f} → 买入 {code} {name}")

        self.close(sell_code, sell_price, f"换仓→{code}")
        if sell_code in self.portfolio.positions:
            return False

        return self.try_buy(code, name, price, buy_min, buy_max, sl, tp,
                           score, source, max_amount, sector=candidates[0].get("sector", "") if candidates else "")

    def evaluate_swaps(self, candidates: list[dict],
                       market_context: str = "",
                       price_info: dict = None,
                       sector_context: str = "") -> bool:
        """定时主动评估换仓（由 watcher 周期性调用）。

        candidates: 当前在买入区间内的候选信号列表，每项含:
            code, name, price, change_pct, score, sl, tp, buy_min, buy_max
        price_info: {code: {change_pct, ...}}  实时涨跌幅等附加数据
        返回 True 表示执行了换仓。
        """
        if len(self.portfolio.positions) < 3:
            return False
        if not candidates:
            return False

        result = self._ai_evaluate_swap(candidates, market_context, price_info, sector_context)
        if not result:
            return False

        sell_code = result["sell"]
        buy_code = result["buy"]

        sell_pos = self.portfolio.positions.get(sell_code)
        buy_cand = next((c for c in candidates if c["code"] == buy_code), None)
        if not sell_pos or not buy_cand:
            return False

        sell_price = sell_pos.current_price or buy_cand["price"]
        logger.info(f"定时换仓: 卖出 {sell_code} → 买入 {buy_code} {buy_cand['name']}")

        self.close(sell_code, sell_price, f"主动换仓→{buy_code}")
        if sell_code in self.portfolio.positions:
            return False

        return self.try_buy(
            buy_cand["code"], buy_cand["name"], buy_cand["price"],
            buy_cand.get("buy_min", buy_cand["price"] * 0.98),
            buy_cand.get("buy_max", buy_cand["price"] * 1.02),
            buy_cand.get("sl", 0), buy_cand.get("tp", 0),
            buy_cand.get("score", 0), "swap", None,
            sector=buy_cand.get("sector", ""),
        )

    def _ai_evaluate_swap(self, candidates: list[dict],
                          market_context: str = "",
                          price_info: dict = None,
                          sector_context: str = "") -> dict | None:
        """调用 AI 评估换仓：持仓 + 候选 + 盘面 → 卖出谁、买入谁。

        返回 {"sell": "stock_code", "buy": "stock_code"} 或 None。
        """
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            return None

        pinfo = price_info or {}

        # 持仓实时数据（含日内涨跌 + 板块）
        pos_lines = []
        for code, pos in self.portfolio.positions.items():
            extra = pinfo.get(code, {})
            chg_str = f" 日内{extra.get('change_pct', 0):+.1f}%" if extra.get('change_pct') else ""
            sec_str = f" [{pos.sector_code}]" if pos.sector_code else ""
            sec_trend = extra.get('sector_trend', '')
            if sec_trend:
                sec_str += f" 板块{sec_trend}"
            # 概念板块
            concepts = extra.get('concepts', [])
            if concepts:
                sec_str += f" 概念:{','.join(concepts)}"
            dist_sl = (pos.current_price - pos.stop_loss) / pos.current_price * 100 if pos.stop_loss > 0 and pos.current_price > 0 else 0
            dist_tp = (pos.take_profit - pos.current_price) / pos.current_price * 100 if pos.take_profit > 0 and pos.current_price > 0 else 0
            pos_lines.append(
                f"{code} {pos.stock_name}{sec_str} | 成本{pos.avg_cost:.2f} 现价{pos.current_price:.2f}{chg_str} "
                f"盈亏{pos.pnl_pct:+.1f}% | 市值{pos.market_value:.0f} | "
                f"止损{pos.stop_loss}(距现价{dist_sl:.1f}%) 止盈{pos.take_profit}(距现价{dist_tp:+.1f}%)"
            )
        pos_text = "\n".join(pos_lines)

        # 候选信号
        cand_lines = []
        for c in candidates:
            sec_str = f" [{c.get('sector','')}]" if c.get('sector') else ""
            sec_trend = c.get('sector_trend', '')
            if sec_trend:
                sec_str += f" 板块{sec_trend}"
            concepts = c.get('concepts', [])
            if concepts:
                sec_str += f" 概念:{','.join(concepts)}"
            cand_lines.append(
                f"{c['code']} {c.get('name','')}{sec_str} | 现价{c['price']:.2f} "
                f"今日{c.get('change_pct',0):+.1f}% | 评分{c.get('score',0):.0f} | "
                f"买入区{c.get('buy_min',0):.2f}-{c.get('buy_max',0):.2f}"
            )
        cand_text = "\n".join(cand_lines)

        # 大盘背景
        ctx_line = f"\n大盘: {market_context}" if market_context else ""

        # 板块行情上下文
        sec_ctx = f"\n{sector_context}" if sector_context else ""

        prompt = f"""当前模拟盘持仓（{len(self.portfolio.positions)}只，上限5只）：

{pos_text}

买点区候选信号：
{cand_text}{ctx_line}{sec_ctx}

请评估是否应该换仓。考虑：
1. 持仓盈亏、止损止盈距离、走势强弱
2. 候选信号的评分、今日涨跌、买点区间
3. 候选所处行业/概念是否比持仓更强
4. 如果候选显著优于某只持仓，建议换仓

只回复JSON：{{"sell": "要卖的代码", "buy": "要买的代码"}} 或 {{"sell": null, "buy": null}}。"""

        try:
            resp = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": "你是A股短线交易员。基于实时盘面判断换仓，只输出JSON。"},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 150,
                },
                timeout=20,
            )
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            content = re.sub(r"```\w*\n?|```", "", content).strip()

            result = json.loads(content)
            sell_code = result.get("sell")
            buy_code = result.get("buy")

            if sell_code and buy_code:
                pos_codes = {c for c in self.portfolio.positions}
                cand_codes = {c["code"] for c in candidates}
                if sell_code in pos_codes and buy_code in cand_codes:
                    logger.info(f"AI 换仓决策: 卖{sell_code} 买{buy_code}")
                    return {"sell": sell_code, "buy": buy_code}
                logger.warning(f"AI 换仓返回无效代码: sell={sell_code} buy={buy_code}")
            logger.info("AI 换仓决策: 不换仓")
            return None
        except Exception as e:
            logger.warning(f"AI 换仓评估异常 ({type(e).__name__}: {e})，fallback 规则")
            return None

    def _rule_swap_target(self, new_code: str, new_score: float) -> str | None:
        """规则兜底：优先 AI 审查 close > reduce > 分数差距。"""
        reviews = self._load_reviews()

        best_sell = None
        best_priority = 99
        for code in self.portfolio.positions:
            review = reviews.get(code, {})
            action = review.get("action", "")

            if action == "close":
                priority = 0
            elif action == "reduce":
                priority = 1
            elif new_score > (review.get("score", 0) or 0) + SWAP_SCORE_GAP:
                priority = 2
            else:
                continue

            if priority < best_priority:
                best_priority = priority
                best_sell = code

        if best_sell:
            logger.info(f"规则换仓: {best_sell} (priority={best_priority})")
        return best_sell

    def _load_reviews(self) -> dict:
        """加载最新 AI 持仓审查建议。"""
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT stock_code, action, tomorrow_outlook, reason
                   FROM trade_holdings_review
                   WHERE trade_date=(SELECT MAX(trade_date) FROM trade_holdings_review)
                     AND account='paper'"""
            ).fetchall()
            conn.close()
            conn2 = sqlite3.connect(self.db_path)
            scores = conn2.execute(
                """SELECT stock_code, signal_score FROM trade_signals
                   WHERE status='bought'"""
            ).fetchall()
            conn2.close()
            score_map = {r[0]: r[1] or 0 for r in scores}

            result = {}
            for r in rows:
                result[r[0]] = {"action": r[1], "tomorrow_outlook": r[2],
                                "reason": r[3], "score": score_map.get(r[0], 0)}
            return result
        except Exception as e:
            logger.warning(f"加载 AI 审查建议失败: {e}")
            return {}

    # ------------------------------------------------------------------
    # 卖出
    # ------------------------------------------------------------------

    def close(self, code: str, price: float, reason: str):
        """止损/止盈触发时平仓。"""
        pos = self.portfolio.positions.get(code)
        if not pos:
            return

        # 卖出佣金 + 印花税
        amount = price * pos.volume
        commission = max(amount * COMMISSION_RATE, MIN_COMMISSION) + amount * STAMP_TAX_RATE

        self.portfolio.close_position(code, price, reason, commission=commission)
        self._record_order(code, pos.stock_name, "sell", pos.volume, price, reason,
                           commission=commission)

        pnl = (price - pos.avg_cost) * pos.volume - commission
        pnl_pct = (price - pos.avg_cost) / pos.avg_cost * 100 if pos.avg_cost else 0
        pnl_str = self._portfolio_summary()
        emoji = "✅" if pnl > 0 else "⚠️"
        if self.telegram:
            self.telegram.send(
                f"{emoji} 模拟盘卖出: {code} {pos.stock_name}\n"
                f"价格 {price:.2f}  {pos.volume}股\n"
                f"成本 {pos.avg_cost:.2f}  盈亏 {pnl:+.0f}({pnl_pct:+.1f}%)\n"
                f"费用 {commission:.1f}  原因: {reason}  {pnl_str}"
            )
        logger.info(f"模拟盘卖出: {code} {pos.stock_name} 盈亏{pnl:+.0f}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_position_summary(self) -> list[str]:
        lines = []
        for code, pos in self.portfolio.positions.items():
            lines.append(
                f"  {code} {pos.stock_name} {pos.volume}股 "
                f"成本{pos.avg_cost:.2f} 现价{pos.current_price:.2f} "
                f"盈亏{pos.pnl:+.0f}({pos.pnl_pct:+.1f}%)"
            )
        return lines

    def _portfolio_summary(self) -> str:
        p = self.portfolio
        return (
            f"总资产 {p.total_value:.0f}  "
            f"现金 {p.cash:.0f}  "
            f"总盈亏 {p.total_pnl:+.0f}({p.total_pnl / INITIAL_CAPITAL * 100:+.1f}%)"
        )

    # ------------------------------------------------------------------
    # 订单记录
    # ------------------------------------------------------------------

    def _record_order(self, code: str, name: str, order_type: str,
                      volume: int, price: float, source: str = "",
                      score: float = 0, commission: float = 0):
        try:
            self.repo.insert_order({
                "trade_date": self.trade_date,
                "order_time": datetime.now().isoformat(),
                "stock_code": code,
                "stock_name": name,
                "order_type": order_type,
                "order_status": "filled",
                "filled_volume": volume,
                "filled_price": price,
                "commission": commission,
                "order_source": f"paper_{source}",
                "signal_id": None,
                "account": "paper",
            })
        except Exception as e:
            logger.warning(f"模拟盘订单记录失败: {e}")
