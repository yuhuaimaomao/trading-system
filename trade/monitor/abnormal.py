"""异动检测 + 换仓评估 + 板块热度。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

import requests

from system.config import settings

logger = logging.getLogger(__name__)


class AbnormalMonitorMixin:
    """异动检测 + 换仓评估 + 板块热度。"""

    def _check_sector_heat(
        self, snapshot: dict[str, dict], resonance_labels: dict[str, str] | None = None
    ):
        monitor = self._get_sector_monitor()
        if monitor is None:
            return
        try:
            messages = monitor.check(snapshot, resonance_labels)
            for msg in messages:
                self._alert(msg)
        except Exception as e:
            logger.warning(f"板块热度检查异常: {e}")

    def _get_sector_monitor(self):
        if self._sector_monitor is None:
            try:
                from trade.monitor.sector_heat import SectorHeatMonitor

                self._sector_monitor = SectorHeatMonitor(
                    db_path=self.db_path,
                    telegram_bot=self.telegram,
                )
            except Exception as e:
                logger.warning(f"板块热度监控器初始化失败: {e}")
        return self._sector_monitor

    # ======================== 第三层：异动检测 ========================

    def _check_abnormal(self, prices: dict[str, float]):
        detector = self._get_abnormal_detector()
        if detector is None:
            return
        try:
            if self._market_snapshot:
                current_snapshot = self._market_snapshot
            else:
                current_snapshot = self._build_market_snapshot(prices)

            now_ts = time.time()
            prev_ts = getattr(self, "_prev_snapshot_ts", 0)
            gap_sec = now_ts - prev_ts if prev_ts > 0 else 0

            # 快照间隔超过 2 分钟 → 数据断层，跳过本轮避免虚假异动
            if self._prev_snapshot and gap_sec <= 120:
                # 快照未变化时跳过（collector 推送周期 > 检测周期）
                if id(current_snapshot) != id(self._prev_snapshot):
                    self._ensure_industry_cache()
                    messages = detector.detect_sector(
                        current_snapshot,
                        self._prev_snapshot,
                        industry_cache=self._industry_cache,
                        resolve_name=self._resolve_name,
                    )
                    if messages:
                        self._alert("\n".join(messages))

            self._prev_snapshot = current_snapshot
            self._prev_snapshot_ts = now_ts
        except Exception as e:
            logger.warning(f"异动检测异常: {e}")

    def _evaluate_swaps(self, prices: dict[str, float]):
        """每15分钟主动评估换仓：AI 实时判断是否卖出某持仓换入候选。"""
        if len(self.paper_account.positions) < 3:
            return

        try:
            signals = self.repo.get_pending_signals(account="paper")
        except Exception as e:
            logger.warning(f"换仓评估获取信号失败: {e}")
            return

        candidates = []
        for s in signals:
            code = s["stock_code"]
            price = prices.get(code)
            if price is None:
                continue
            buy_min = s.get("buy_zone_min") or 0
            buy_max = s.get("buy_zone_max") or 0
            if buy_min <= 0:
                continue
            in_or_near = buy_min * 0.95 <= price <= buy_max
            if in_or_near:
                snap = (
                    self._market_snapshot.get(code, {}) if self._market_snapshot else {}
                )
                industry = (
                    self._industry_cache.get(code, "")
                    if hasattr(self, "_industry_cache")
                    else ""
                )
                sec_trend = ""
                if industry and hasattr(self, "_sector_trend_history"):
                    history = self._sector_trend_history.get(industry, [])
                    if history:
                        sec_trend = f"{history[-1]:+.1f}%"
                candidates.append(
                    {
                        "code": code,
                        "name": s.get("stock_name", ""),
                        "price": price,
                        "change_pct": snap.get("changePct", 0),
                        "score": s.get("signal_score", 0) or 0,
                        "sl": s.get("stop_loss", 0) or 0,
                        "tp": s.get("take_profit", 0) or 0,
                        "buy_min": buy_min,
                        "buy_max": buy_max,
                        "sector": industry,
                        "sector_trend": sec_trend,
                    }
                )
                concepts = self._concept_cache.get(code, [])
                if concepts and self._concept_stats:
                    top = sorted(
                        concepts,
                        key=lambda c: abs(
                            self._concept_stats.get(c, {}).get("change_pct", 0)
                        ),
                        reverse=True,
                    )[:3]
                    candidates[-1]["concepts"] = top

        if not candidates:
            return

        ctx = ""
        if self._index_prices and len(self._index_prices) >= 2:
            idx_chg = (
                (self._index_prices[-1] - self._index_prices[-2])
                / self._index_prices[-2]
                * 100
            )
            ctx = f"上证指数 日内变动{idx_chg:+.2f}%"

        price_info = {}
        if self._market_snapshot:
            for code, pos in self.paper_account.positions.items():
                snap = self._market_snapshot.get(code, {})
                info = {"change_pct": snap.get("changePct", 0)}
                meta = self._pos_meta.get(code, {})
                industry = meta.get("sector", "")
                if industry and hasattr(self, "_sector_trend_history"):
                    history = self._sector_trend_history.get(industry, [])
                    if history:
                        info["sector_trend"] = f"{history[-1]:+.1f}%"
                concepts = self._concept_cache.get(code, [])
                if concepts and self._concept_stats:
                    top = sorted(
                        concepts,
                        key=lambda c: abs(
                            self._concept_stats.get(c, {}).get("change_pct", 0)
                        ),
                        reverse=True,
                    )[:3]
                    info["concepts"] = top
                price_info[code] = info

        all_codes = set(c["code"] for c in candidates) | set(
            self.paper_account.positions.keys()
        )
        sector_context = self._build_sector_context(all_codes)

        logger.info(
            f"主动换仓评估: {len(candidates)} 个候选, {len(self.paper_account.positions)} 个持仓"
        )
        try:
            swapped = _do_evaluate_swaps(
                candidates,
                market_context=ctx,
                price_info=price_info,
                sector_context=sector_context,
            )
            if swapped:
                self._invalidate_watch_codes_cache()
        except Exception as e:
            logger.warning(f"换仓评估异常: {e}")

    def _get_abnormal_detector(self):
        if self._abnormal_detector is None:
            self._abnormal_detector = AbnormalDetector()
        return self._abnormal_detector

    @staticmethod
    def _build_market_snapshot(prices: dict[str, float]) -> dict:
        """将当前价格字典转为 snapshot 格式供异动检测器使用。"""
        return {
            code: {"price": p, "timestamp": time.time()} for code, p in prices.items()
        }

    # ======================== 收盘收尾 ========================


class AbnormalDetector:
    """盘中异动检测器 — 急速拉升 / 量比暴增 / 逼近涨停。"""

    def detect_sector(
        self,
        current: dict,
        previous: dict,
        industry_cache: dict = None,
        resolve_name=None,
    ) -> list[str]:
        """对比两轮快照，返回异动告警消息列表。"""
        alerts = []
        if not current or not previous:
            return alerts

        rapid_rise = getattr(settings, "ABNORMAL_RAPID_RISE_PCT", 1.0)
        vol_surge = getattr(settings, "ABNORMAL_VOLUME_SURGE_RATIO", 3.0)
        near_limit = getattr(settings, "ABNORMAL_NEAR_LIMIT_PCT", 7.0)
        TOP_N = 10

        rapid_hits = []  # (code, delta_pct, name, industry)
        vol_hits = []
        limit_hits = []

        for code, info in current.items():
            prev = previous.get(code, {})
            cur_chg = float(info.get("changePct", 0))
            prev_chg = float(prev.get("changePct", 0))
            name = resolve_name(code) if resolve_name else code
            industry = industry_cache.get(code, "") if industry_cache else ""

            if cur_chg - prev_chg > rapid_rise:
                rapid_hits.append((code, cur_chg - prev_chg, name, industry))

            cur_vol = float(info.get("amount", 0))
            prev_vol = float(prev.get("amount", 0))
            if prev_vol > 0 and cur_vol > prev_vol * vol_surge:
                vol_hits.append((code, cur_vol / prev_vol, name))

            if cur_chg >= near_limit and prev_chg < near_limit:
                limit_hits.append((code, cur_chg, name))

        # 按幅度降序排列
        rapid_hits.sort(key=lambda x: x[1], reverse=True)
        vol_hits.sort(key=lambda x: x[1], reverse=True)
        limit_hits.sort(key=lambda x: x[1], reverse=True)

        now = datetime.now().strftime("%H:%M")

        # 板块异动：同行业 3+ 只同时拉升 → 板块级告警
        if rapid_hits and industry_cache:
            ind_groups: dict[str, list] = {}
            for code, delta, name, ind in rapid_hits:
                if ind:
                    ind_groups.setdefault(ind, []).append((code, delta, name))
            for ind, stocks in ind_groups.items():
                if len(stocks) >= 3:
                    top_s = sorted(stocks, key=lambda x: x[1], reverse=True)[:5]
                    lines = " ".join(f"{n}({d:+.1f}%)" for c, d, n in top_s)
                    alerts.append(f"🔥 板块异动 {ind} {now}\n   {lines}")

        if rapid_hits:
            top = rapid_hits[:TOP_N]
            lines = " ".join(f"{n} {d:+.1f}%" for c, d, n, _ in top)
            suffix = f" 等{len(rapid_hits)}只" if len(rapid_hits) > TOP_N else ""
            alerts.append(f"🏭 急速拉升 {now}\n   {lines}{suffix}")
        if vol_hits:
            top = vol_hits[:TOP_N]
            lines = " ".join(f"{n} {r:.0f}×" for c, r, n in top)
            alerts.append(f"📊 量比暴增 {now}\n   {lines}")
        if limit_hits:
            top = limit_hits[:TOP_N]
            lines = " ".join(f"{n} {d:+.1f}%" for c, d, n in top)
            alerts.append(f"🚀 逼近涨停 {now}\n   {lines}")

        return alerts


# ======================== 换仓评估（从 PaperTrader 迁入）========================


def _do_evaluate_swaps(
    self,
    candidates: list[dict],
    market_context: str = "",
    price_info: dict = None,
    sector_context: str = "",
) -> bool:
    """换仓评估入口：AI 优先，规则兜底。"""
    result = _ai_evaluate_swap(
        self, candidates, market_context, price_info, sector_context
    )
    if not result:
        best_cand = max(candidates, key=lambda c: c.get("score", 0))
        sell_code = _rule_swap_target(
            self, best_cand["code"], best_cand.get("score", 0)
        )
        if not sell_code:
            return False
        buy_code = best_cand["code"]
    else:
        sell_code = result["sell"]
        buy_code = result["buy"]

    buy_cand = next((c for c in candidates if c["code"] == buy_code), None)
    if not buy_cand:
        return False

    sell_price = self.paper_account.positions.get(sell_code)
    sell_price = sell_price.current_price if sell_price else (buy_cand.get("price", 0))
    logger.info(f"换仓: 卖出 {sell_code} → 买入 {buy_code} {buy_cand.get('name', '')}")

    result = self.paper_account.sell(sell_code, sell_price, f"主动换仓→{buy_code}")
    if result.success:
        self._pos_meta.pop(sell_code, None)
    else:
        return False

    price = buy_cand["price"]
    max_affordable = int(self.paper_account.cash * 0.9 / price / 100) * 100
    volume = min(
        int(
            self.paper_account.total_value * settings.DEFAULT_POSITION_PCT / price / 100
        )
        * 100,
        max_affordable,
    )
    if volume < 100:
        return False

    buy_result = self.paper_account.buy(
        buy_cand["code"],
        buy_cand.get("name", ""),
        price,
        volume,
        source="swap",
    )
    if buy_result.success:
        self._pos_meta[buy_code] = {
            "sl": buy_cand.get("sl", 0),
            "tp": buy_cand.get("tp", 0),
            "trailing_stop": 0.05,
            "highest_price": price,
            "sector": buy_cand.get("sector", ""),
            "score": buy_cand.get("score", 0),
            "signal_id": None,
        }
    return buy_result.success


def _ai_evaluate_swap(
    self,
    candidates: list[dict],
    market_context: str = "",
    price_info: dict = None,
    sector_context: str = "",
) -> dict | None:
    """AI 评估换仓，返回 {"sell": code, "buy": code} 或 None。"""
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None

    pinfo = price_info or {}
    pos_lines = []
    for code, pos in self.paper_account.positions.items():
        meta = self._pos_meta.get(code, {})
        extra = pinfo.get(code, {})
        chg_str = (
            f" 日内{extra.get('change_pct', 0):+.1f}%"
            if extra.get("change_pct")
            else ""
        )
        sector = meta.get("sector", "")
        sec_str = f" [{sector}]" if sector else ""
        sec_trend = extra.get("sector_trend", "")
        if sec_trend:
            sec_str += f" 板块{sec_trend}"
        concepts = extra.get("concepts", [])
        if concepts:
            sec_str += f" 概念:{','.join(concepts)}"
        sl = meta.get("sl", 0)
        tp = meta.get("tp", 0)
        dist_sl = (
            (pos.current_price - sl) / pos.current_price * 100
            if sl > 0 and pos.current_price > 0
            else 0
        )
        dist_tp = (
            (tp - pos.current_price) / pos.current_price * 100
            if tp > 0 and pos.current_price > 0
            else 0
        )
        pos_lines.append(
            f"{code} {pos.stock_name}{sec_str} | 成本{pos.avg_cost:.2f} 现价{pos.current_price:.2f}{chg_str} "
            f"盈亏{pos.pnl_pct:+.1f}% | 市值{pos.market_value:.0f} | "
            f"止损{sl}(距现价{dist_sl:.1f}%) 止盈{tp}(距现价{dist_tp:+.1f}%)"
        )
    pos_text = "\n".join(pos_lines)

    cand_lines = []
    for c in candidates:
        sec_str = f" [{c.get('sector', '')}]" if c.get("sector") else ""
        sec_trend = c.get("sector_trend", "")
        if sec_trend:
            sec_str += f" 板块{sec_trend}"
        cand_lines.append(
            f"{c['code']} {c.get('name', '')}{sec_str} | 现价{c['price']:.2f} "
            f"今日{c.get('change_pct', 0):+.1f}% | 评分{c.get('score', 0):.0f} | "
            f"买入区{c.get('buy_min', 0):.2f}-{c.get('buy_max', 0):.2f}"
        )
    cand_text = "\n".join(cand_lines)

    ctx_line = f"\n大盘: {market_context}" if market_context else ""
    sec_ctx = f"\n{sector_context}" if sector_context else ""

    prompt = f"""当前模拟盘持仓（{len(self.paper_account.positions)}只，上限5只）：

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
                    {
                        "role": "system",
                        "content": "你是A股短线交易员。基于实时盘面判断换仓，只输出JSON。",
                    },
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
            pos_codes = set(self.paper_account.positions.keys())
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
    """规则兜底：优先 AI 审查 close > reduce > 分数差距（跳过当日买入）。"""
    reviews = _load_reviews(self)
    best_sell = None
    best_priority = 99
    for code, pos in self.paper_account.positions.items():
        if pos.entry_date == self._trade_date:
            continue
        review = reviews.get(code, {})
        action = review.get("action", "")
        if action == "close":
            priority = 0
        elif action == "reduce":
            priority = 1
        elif new_score > (review.get("score", 0) or 0) + settings.SWAP_SCORE_GAP:
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
            result[r[0]] = {
                "action": r[1],
                "tomorrow_outlook": r[2],
                "reason": r[3],
                "score": score_map.get(r[0], 0),
            }
        return result
    except Exception as e:
        logger.warning(f"加载 AI 审查建议失败: {e}")
        return {}
