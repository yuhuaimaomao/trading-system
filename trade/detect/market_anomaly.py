"""异动检测 + 换仓评估 + 板块热度。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import time
from datetime import datetime

from data._base import connect
from system.config import settings
from system.utils.logger import get_trade_logger

logger = get_trade_logger("detect")


class AbnormalMonitorMixin:
    """异动检测 + 换仓评估 + 板块热度。"""

    def _check_sector_heat(self, snapshot: dict[str, dict], resonance_labels: dict[str, str] | None = None):
        monitor = self._get_sector_monitor()
        if monitor is None:
            return
        try:
            messages = monitor.check(snapshot, resonance_labels)
            if not messages:
                return

            # 组装：指数 → 排名 → 持仓板块
            now = datetime.now().strftime("%H:%M")
            header = f"📊 板块热度  {now}"
            index_line = self._format_market_header()
            pos_line = self._format_my_sectors_line()

            parts = [header]
            if index_line:
                parts.append(index_line)
            parts.append("─" * 30)
            parts.extend(messages)
            if pos_line:
                parts.append("─" * 30)
                parts.append(pos_line)

            self._alert("\n".join(parts))
        except Exception as e:
            logger.warning(f"板块热度检查异常: {e}")

    def _format_market_header(self) -> str:
        """多指数 + 涨跌比，放头部。

        不直接用 collector 消息里的 change_pct（可能和最新价格不同步），
        用最新价 + 昨收自己算，保证显示的是当前真实涨跌幅。
        """
        im = getattr(self, "_index_map", {})
        parts = []
        for code in ["000001.SH", "399006.SZ", "399303.SZ"]:
            info = im.get(code, {})
            if info:
                name = info.get("name", code)
                short = {
                    "上证指数": "上证",
                    "创业板指": "创业",
                    "国证2000": "国证",
                }.get(name, name[:2])
                price = info.get("last_price", 0)
                pre_close = info.get("pre_close", 0)
                if price > 0 and pre_close > 0:
                    chg = (price - pre_close) / pre_close
                else:
                    chg = info.get("change_pct", 0)
                parts.append(f"{short} {chg:+.2%}")
        bb = getattr(self, "_market_breadth", {})
        up, down = bb.get("up", 0), bb.get("down", 0)
        if up + down > 0:
            parts.append(f"涨跌比 {up}:{down}")
        return "  ".join(parts) if parts else ""

    def _format_my_sectors_line(self) -> str:
        """持仓板块一句话。"""
        positions = getattr(self, "paper_account", None)
        if not positions:
            return ""
        sector_stats = getattr(self, "_sector_stats", {})
        industry_cache = getattr(self, "_industry_cache", {})
        seen = set()
        parts = []
        for code in positions.positions:
            ind = industry_cache.get(code, "")
            if ind and ind not in seen:
                seen.add(ind)
                s = sector_stats.get(ind, {})
                chg = s.get("change_pct", 0)
                parts.append(f"{ind} {chg:+.1f}%")
        return f"持仓板块: {'  '.join(parts)}" if parts else ""

    def _format_market_judgment(self) -> str:
        """综合多指数 + 宽度，给一句话盘面判断。"""
        im = getattr(self, "_index_map", {})
        bb = getattr(self, "_market_breadth", {})
        up, down = bb.get("up", 0), bb.get("down", 0)
        total = up + down
        if total == 0:
            return ""

        down_ratio = down / total
        sh = im.get("000001.SH", {})
        cy = im.get("399006.SZ", {})
        gz = im.get("399303.SZ", {})
        sh_chg = sh.get("change_pct", 0)
        cy_chg = cy.get("change_pct", 0)
        gz_chg = gz.get("change_pct", 0)

        if down_ratio > 0.7:
            return f"普跌行情（跌{down_ratio:.0%}），观望为主，暂停买入"
        if cy_chg > 0.01 and down_ratio > 0.55:
            return f"创业板领涨 {cy_chg:+.1%}，但个股普跌（跌{down_ratio:.0%}），指数失真，谨慎参与"
        if cy_chg > 0.01 and down_ratio <= 0.45:
            return f"创业板领涨 {cy_chg:+.1%}，个股活跃，精选强势板块"
        if abs(sh_chg) < 0.003 and down_ratio > 0.55:
            return f"上证横盘但个股普跌（跌{down_ratio:.0%}），权重护盘掩盖抛压，谨慎"
        if sh_chg < -0.005 and down_ratio > 0.6:
            return "指数与个股共振下跌，市场恐慌，暂停买入"
        if gz_chg < -0.01 and down_ratio > 0.6:
            return f"中小盘领跌 {gz_chg:+.1%}，市场情绪弱，观望"
        if down_ratio <= 0.4 and sh_chg > 0.003:
            return "普涨行情，顺势参与"

        return ""

    def _get_sector_monitor(self):
        if self._sector_monitor is None:
            try:
                from trade.sector.sector_heat import SectorHeatMonitor

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
                    messages, rapid_hits = detector.detect_sector(
                        current_snapshot,
                        self._prev_snapshot,
                        industry_cache=self._industry_cache,
                        resolve_name=self._resolve_name,
                    )
                    # 急拉放量结构化数据：每轮都更新（买入管线时效性优先）
                    self._last_rapid_hits = rapid_hits if rapid_hits else []
                    if rapid_hits:
                        logger.info(
                            f"异动检测: {len(rapid_hits)}只急拉放量 (首只: {rapid_hits[0].get('name', '')} +{rapid_hits[0].get('change_pct', 0):.1f}%)"
                        )
                    # 告警冷却：10分钟内不重复推 Telegram
                    last_alert = getattr(self, "_last_abnormal_alert", 0)
                    if messages and now_ts - last_alert >= 600:
                        self._alert("\n".join(messages))
                        self._last_abnormal_alert = now_ts

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
                snap = self._market_snapshot.get(code, {}) if self._market_snapshot else {}
                industry = self._industry_cache.get(code, "") if hasattr(self, "_industry_cache") else ""
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
                        key=lambda c: abs(self._concept_stats.get(c, {}).get("change_pct", 0)),
                        reverse=True,
                    )[:3]
                    candidates[-1]["concepts"] = top

        if not candidates:
            return

        ctx = ""
        if self._index_prices and len(self._index_prices) >= 2:
            idx_chg = (self._index_prices[-1] - self._index_prices[-2]) / self._index_prices[-2] * 100
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
                        key=lambda c: abs(self._concept_stats.get(c, {}).get("change_pct", 0)),
                        reverse=True,
                    )[:3]
                    info["concepts"] = top
                price_info[code] = info

        all_codes = set(c["code"] for c in candidates) | set(self.paper_account.positions.keys())
        sector_context = self._build_sector_context(all_codes)

        logger.info(f"主动换仓评估: {len(candidates)} 个候选, {len(self.paper_account.positions)} 个持仓")
        try:
            swapped = _do_evaluate_swaps(
                self,
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
        return {code: {"price": p, "timestamp": time.time()} for code, p in prices.items()}

    # ======================== 收盘收尾 ========================


class AbnormalDetector:
    """盘中异动检测器 — 急速拉升 / 量比暴增 / 逼近涨停。"""

    def detect_sector(
        self,
        current: dict,
        previous: dict,
        industry_cache: dict = None,
        resolve_name=None,
    ) -> tuple[list[str], list[dict]]:
        """对比两轮快照，返回 (文本告警, 急拉放量结构化数据)。"""
        alerts = []
        if not current or not previous:
            return alerts

        rapid_rise = getattr(settings, "ABNORMAL_RAPID_RISE_PCT", 1.0)
        vol_surge = getattr(settings, "ABNORMAL_VOLUME_SURGE_RATIO", 3.0)
        near_limit_ratio = getattr(settings, "ABNORMAL_NEAR_LIMIT_RATIO", 0.85)
        TOP_N = 10

        rapid_hits = []  # (code, delta_pct, vol_ratio, name, industry, price, change_pct, amount)
        limit_hits = []

        for code, info in current.items():
            prev = previous.get(code, {})
            cur_chg = float(info.get("changePct", 0))
            prev_chg = float(prev.get("changePct", 0))
            name = resolve_name(code) if resolve_name else code
            industry = industry_cache.get(code, "") if industry_cache else ""

            price_delta = cur_chg - prev_chg
            cur_vol = float(info.get("amount", 0))
            prev_vol = float(prev.get("amount", 0))
            vol_ratio = cur_vol / prev_vol if prev_vol > 0 else 0
            price = float(info.get("price", 0))

            # 急速拉升且放量：必须同时满足
            if price_delta > rapid_rise and vol_ratio > vol_surge:
                rapid_hits.append((code, price_delta, vol_ratio, name, industry, price, cur_chg, cur_vol))

            # 逼近涨停：按涨跌幅限制比例计算（主板10%→8.5%，双创20%→17%）
            limit_pct = 0.20 if code.startswith(("300", "688")) else 0.10
            near_limit = limit_pct * near_limit_ratio
            if cur_chg >= near_limit and prev_chg < near_limit:
                limit_hits.append((code, cur_chg, name))

        rapid_hits.sort(key=lambda x: x[1], reverse=True)
        limit_hits.sort(key=lambda x: x[1], reverse=True)

        now = datetime.now().strftime("%H:%M")

        # 板块异动：同行业 3+ 只同时拉升 → 板块级告警
        if rapid_hits and industry_cache:
            ind_groups: dict[str, list] = {}
            for code, delta, vol_r, name, ind in rapid_hits:
                if ind:
                    ind_groups.setdefault(ind, []).append((code, delta, vol_r, name))
            for ind, stocks in ind_groups.items():
                if len(stocks) >= 3:
                    top_s = sorted(stocks, key=lambda x: x[1], reverse=True)[:5]
                    lines = " ".join(f"{n}({d:+.1f}% {v:.0f}×)" for c, d, v, n in top_s)
                    alerts.append(f"🔥 板块异动 {ind} {now}\n   {lines}")

        if rapid_hits:
            top = rapid_hits[:TOP_N]
            lines = " ".join(f"{n} {d:+.1f}% {v:.0f}×" for c, d, v, n, *_ in top)
            suffix = f" 等{len(rapid_hits)}只" if len(rapid_hits) > TOP_N else ""
            alerts.append(f"🏭 急拉放量 {now}\n   {lines}{suffix}")
        if limit_hits:
            top = limit_hits[:TOP_N]
            lines = " ".join(f"{n} {d:+.1f}%" for c, d, n in top)
            alerts.append(f"🚀 逼近涨停 {now}\n   {lines}")

        # 结构化输出：供买入管线使用
        structured = [
            {
                "code": c,
                "delta_pct": d,
                "vol_ratio": v,
                "name": n,
                "industry": i,
                "price": p,
                "change_pct": chg,
                "amount": amt,
            }
            for c, d, v, n, i, p, chg, amt in rapid_hits
        ]

        return alerts, structured


# ======================== 换仓评估（从 PaperTrader 迁入）========================


def _do_evaluate_swaps(
    self,
    candidates: list[dict],
    market_context: str = "",
    price_info: dict = None,
    sector_context: str = "",
) -> bool:
    """换仓评估入口：规则兜底 + AI 异步评估。"""
    # 提交 AI 评估（异步，不阻塞）
    _submit_swap_ai(self, candidates, market_context, price_info, sector_context)

    # 规则兜底：立即执行评分最高的换仓候选
    best_cand = max(candidates, key=lambda c: c.get("score", 0))
    sell_code = _rule_swap_target(self, best_cand["code"], best_cand.get("score", 0))
    if not sell_code:
        return False
    buy_code = best_cand["code"]

    buy_cand = next((c for c in candidates if c["code"] == buy_code), None)
    if not buy_cand:
        return False

    sell_price = self.paper_account.positions.get(sell_code)
    sell_price = sell_price.current_price if sell_price else (buy_cand.get("price", 0))
    logger.info(f"换仓: 卖出 {sell_code} → 买入 {buy_code} {buy_cand.get('name', '')}")

    from trade.exec.paper.executor import execute_paper_sell

    result = execute_paper_sell(
        sell_code,
        "",
        sell_price,
        f"主动换仓→{buy_code}",
        paper_account=self.paper_account,
        pos_meta=self._pos_meta,
        bought_watch=self._bought_watch,
    )
    if not result["success"]:
        return False

    price = buy_cand["price"]
    max_affordable = int(self.paper_account.cash * 0.9 / price / 100) * 100
    volume = min(
        int(self.paper_account.total_value * settings.DEFAULT_POSITION_PCT / price / 100) * 100,
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
        # 同步到持仓对象（持久化止损止盈）
        pos = self.paper_account.positions.get(buy_code)
        if pos:
            pos.stop_loss = buy_cand.get("sl", 0)
            pos.take_profit = buy_cand.get("tp", 0)
    return buy_result.success


def _submit_swap_ai(
    self,
    candidates: list[dict],
    market_context: str = "",
    price_info: dict = None,
    sector_context: str = "",
):
    """异步提交 AI 换仓评估（不阻塞扫描）。结果由 _process_pending_ai 处理。"""
    aiq = getattr(self, "_ai_queue", None)
    if aiq is None:
        return
    # 委托给全局 ai_service

    pinfo = price_info or {}
    pos_lines = []
    for code, pos in self.paper_account.positions.items():
        meta = self._pos_meta.get(code, {})
        extra = pinfo.get(code, {})
        chg_str = f" 日内{extra.get('change_pct', 0):+.1f}%" if extra.get("change_pct") else ""
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
        dist_sl = (pos.current_price - sl) / pos.current_price * 100 if sl > 0 and pos.current_price > 0 else 0
        dist_tp = (tp - pos.current_price) / pos.current_price * 100 if tp > 0 and pos.current_price > 0 else 0
        pos_lines.append(
            f"{code} {pos.stock_name}{sec_str} | 成本{pos.avg_cost:.2f} 现价{pos.current_price:.2f}{chg_str} "
            f"盈亏{pos.pnl_pct:+.1f}% | 止损{sl}(距现价{dist_sl:.1f}%) 止盈{tp}(距现价{dist_tp:+.1f}%)"
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

    from system.ai.prompts.watcher import SWAP_EVAL_SYSTEM, SWAP_EVAL_TEMPLATE

    prompt = SWAP_EVAL_TEMPLATE.format(
        pos_count=len(self.paper_account.positions),
        pos_text=pos_text,
        cand_text=cand_text,
        ctx_line=ctx_line,
        sec_ctx=sec_ctx,
    )

    ok = aiq.submit(
        "swap_eval",
        prompt,
        system_prompt=SWAP_EVAL_SYSTEM,
        dedupe=True,
    )
    if not ok:
        self._alert_private("⚠️ AI 队列已满，换仓评估被跳过\n   规则决策不受影响，但缺少 AI 辅助判断")
    if ok:
        self._swap_ctx = {
            "candidates": candidates,
            "market_context": market_context,
            "price_info": price_info,
            "sector_context": sector_context,
        }


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
        conn = connect(self.db_path)
        rows = conn.execute(
            """SELECT stock_code, action, tomorrow_outlook, reason
               FROM trade_holdings_review
               WHERE trade_date=(SELECT MAX(trade_date) FROM trade_holdings_review)
                 AND account='paper'"""
        ).fetchall()
        conn.close()
        conn2 = connect(self.db_path)
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
