# -*- coding: utf-8 -*-
"""盘前交易管线 — 趋势筛选 → AI 分析 → 信号入库

用法:
    from analysis.strategy import StrategyPipeline
    pipeline = StrategyPipeline()
    pipeline.run(trade_date="2026-05-26")

CLI:
    python main.py strategy
"""

from datetime import datetime
from typing import Optional

from analysis.screening.trend import TrendScreener
from analysis.screening.breadth import MarketBreadth
from analysis.screening.profiles import ProfileBuilder
from analysis.advisor import AIAdvisor
from analysis.signals import StockScore, StockProfile, OrderSignal, HoldingInfo, AccountSummary, ReviewContext, SignalType, SignalSource
from data.repo import TradeRepository
from system.utils.logger import get_system_logger

logger = get_system_logger("strategy")


class StrategyPipeline:
    """盘前管线：市场宽度 → 趋势筛选 → 画像富化 → AI 分析 → 信号入库"""

    def __init__(self, telegram_bot=None):
        self.breadth = MarketBreadth()
        self.screener = TrendScreener()
        self.profiler = ProfileBuilder()
        self.repo = TradeRepository()
        self.telegram = telegram_bot

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self, trade_date: Optional[str] = None) -> list[OrderSignal]:
        """执行完整策略管线，返回入库的信号列表。"""
        trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        logger.info(f"策略管线开始 {trade_date}")

        # 步骤 0: 市场宽度 → 大盘状态
        market_state = self._compute_market_state(trade_date)

        # 步骤 0.5: 加载持仓（实盘 + 模拟盘）+ 保存快照
        holdings, account_summaries = self._load_holdings(trade_date)
        if holdings:
            logger.info(f"当前持仓: {len(holdings)} 只 "
                        f"(模拟盘{sum(1 for h in holdings if h.account=='paper')}只 "
                        f"实盘{sum(1 for h in holdings if h.account=='real')}只)")

        # 保存实盘/模拟盘快照到 trade_portfolio_snapshots
        if account_summaries:
            self._save_portfolio_snapshots(account_summaries, trade_date)
            self._save_portfolio_positions(holdings, trade_date)

        # 为持仓构建完整技术画像（供 AI 审查用）
        if holdings:
            self._enrich_holdings(holdings, trade_date, market_state)

        # 步骤 0.8: 加载复盘上下文
        review_ctx = self._load_review_context(trade_date)

        # 步骤 1: 趋势筛选（传入大盘状态）
        candidates = self._screen(trade_date, market_state)

        # 步骤 1.5: 加载昨日遗留推荐
        legacy_candidates, legacy_reasons = self._load_legacy(trade_date)
        if legacy_candidates:
            logger.info(f"昨日遗留: {len(legacy_candidates)} 只")
            # 去重（同日已筛选出的不再重复）
            screened_codes = {c.stock_code for c in candidates}
            legacy_candidates = [c for c in legacy_candidates if c.stock_code not in screened_codes]
            candidates = candidates + legacy_candidates

        if not candidates:
            logger.warning("无候选票，管线结束")
            return []

        # 步骤 2: 画像富化 → StockProfile
        profiles = self._enrich(candidates, trade_date, market_state)
        # 回填昨日遗留理由到画像
        if legacy_candidates:
            legacy_codes = {c.stock_code for c in legacy_candidates}
            for p in profiles:
                if p.code in legacy_codes:
                    p.tags.insert(0, "昨日遗留")
                    p.legacy_note = legacy_reasons.get(p.code, "")
        logger.info(f"画像富化完成，候选详情:")
        for p in profiles:
            bias = ""
            if p.snapshot.get("price") and p.history.get("ma5"):
                b5 = (p.snapshot["price"] - p.history["ma5"]) / p.history["ma5"] * 100
                bias = f"bias5:{b5:+.1f}%"
            logger.info(f"  {p.code} {p.name} "
                        f"趋势:{'5日强' if p.trend_mode == 'strong' else '20日稳'} "
                        f"评分{p.score:.0f} {bias} "
                        f"场景:{','.join(p.scenarios) if p.scenarios else '无'} "
                        f"标签:{','.join(p.tags) if p.tags else '无'}")

        # 步骤 3: AI 分析
        signals, holdings_review = self._analyze(profiles, trade_date, holdings, account_summaries, review_ctx)

        # 步骤 3.1: 持仓审查落库 + 应用止损止盈
        if holdings_review:
            self._save_holdings_review(holdings_review, trade_date)

        # 步骤 3.5: 复盘趋势精选 → 结构化信号（与 AI 信号合并，统一盯盘）
        if review_ctx and review_ctx.review_stocks_raw:
            review_signals = self._build_review_signals(
                review_ctx.review_stocks_raw, trade_date)
            # 去重：复盘信号中与 AI 信号重复的股票跳过
            ai_codes = {s.stock_code for s in signals}
            new_review = [rs for rs in review_signals if rs.stock_code not in ai_codes]
            signals = signals + new_review
            logger.info(f"复盘结构化信号: {len(review_signals)} 只 (新增{len(new_review)}只)")

        # 即使没有买入信号，持仓审查也要推送
        if not signals and not holdings_review:
            logger.warning("AI 未生成任何买入信号或持仓审查，管线结束")
            return []

        if signals:
            # AI 返回后，从原始 profile 回填 trend_mode（确定性数据，不依赖 AI）
            profile_map = {p.code: p for p in profiles}
            for s in signals:
                if s.stock_code in profile_map:
                    s.trend_mode = profile_map[s.stock_code].trend_mode

            saved = self._save_signals(signals, trade_date)
            logger.info(f"策略管线完成: 候选{len(candidates)} → 画像{len(profiles)} → AI信号{len(signals)} → 入库{saved}")
            for s in signals:
                logger.info(f"  → 入库: {s.stock_code} {s.stock_name} "
                            f"买入{s.buy_zone_min}-{s.buy_zone_max} "
                            f"止损{s.stop_loss} 止盈{s.take_profit} "
                            f"评分{s.signal_score:.0f}")

        # 有信号或有持仓审查就推送
        if signals or holdings_review:
            self._push_summary(signals, profiles, trade_date, holdings_review)

        return signals

    # ------------------------------------------------------------------
    # 步骤 0: 市场宽度
    # ------------------------------------------------------------------

    def _compute_market_state(self, trade_date: str) -> str:
        result = self.breadth.compute(trade_date)
        state = result.get("market_state", "")
        # 构建带数据的大盘描述，给 AI 做判断依据
        market_desc = (
            f"{state}（涨{result['up_count']}/跌{result['down_count']}，"
            f"涨停{result['limit_up_count']}/跌停{result['limit_down_count']}，"
            f"指数{result['index_change_pct']:+.2f}%）"
        )
        logger.info(f"市场宽度: {market_desc}")
        self.breadth.save(trade_date)
        return market_desc

    # ------------------------------------------------------------------
    # 步骤 0.5: 加载持仓（实盘 + 模拟盘独立统计）
    # ------------------------------------------------------------------

    PAPER_INITIAL = 200_000
    REAL_INITIAL_DEFAULT = 200_000  # 实盘初始资金，可在 config 覆盖

    def _load_holdings(self, trade_date: str) -> tuple[list[HoldingInfo], list[AccountSummary]]:
        """查询当前持仓（实盘+模拟盘），返回 (持仓列表, 账户概况)。"""
        import sqlite3
        from datetime import date

        holdings: list[HoldingInfo] = []
        summaries: list[AccountSummary] = []

        try:
            conn = sqlite3.connect(self.screener.db_path)

            # 1. 按 stock_code + account 汇总持仓
            rows = conn.execute(
                """SELECT o.stock_code, o.account,
                          MIN(o.order_time) as entry_time,
                          SUM(CASE WHEN o.order_type='buy' THEN o.filled_volume ELSE -o.filled_volume END) as net_vol,
                          SUM(CASE WHEN o.order_type='buy' THEN o.filled_price * o.filled_volume ELSE 0 END) as buy_amount,
                          SUM(CASE WHEN o.order_type='buy' THEN o.filled_volume ELSE 0 END) as buy_vol,
                          SUM(CASE WHEN o.order_type='buy' THEN o.commission ELSE 0 END) as buy_comm,
                          SUM(CASE WHEN o.order_type='sell' THEN o.filled_price * o.filled_volume ELSE 0 END) as sell_amount,
                          SUM(CASE WHEN o.order_type='sell' THEN o.commission ELSE 0 END) as sell_comm
                   FROM trade_orders o
                   WHERE o.order_status='filled' AND o.filled_volume > 0
                   GROUP BY o.stock_code, o.account
                   HAVING net_vol > 0""",
            ).fetchall()

            if not rows:
                conn.close()
                return [], []

            codes = list({r[0] for r in rows})
            placeholders = ",".join("?" for _ in codes)

            # 2. 当前行情 + 均线（用最新的 trade_date，盘前可能只有昨天的数据）
            price_rows = conn.execute(
                f"""SELECT stock_code, stock_name, price, ma5, ma10, ma20, industry
                    FROM stock_basic
                    WHERE trade_date=(SELECT MAX(trade_date) FROM stock_basic)
                      AND stock_code IN ({placeholders})""",
                codes,
            ).fetchall()
            price_map = {r[0]: r for r in price_rows}

            # 3. 止盈止损（从 trade_signals）
            sl_rows = conn.execute(
                f"""SELECT stock_code, stop_loss, take_profit, signal_score
                    FROM trade_signals
                    WHERE status='bought' AND stock_code IN ({placeholders})
                    ORDER BY id DESC""",
                codes,
            ).fetchall()
            sl_map: dict[str, dict] = {}
            for r in sl_rows:
                if r[0] not in sl_map:
                    sl_map[r[0]] = {"stop_loss": r[1] or 0, "take_profit": r[2] or 0, "score": r[3] or 0}

            # 4. 日内最高价（从 stock_indicators 的高点近似，或从当日行情）
            hi_rows = conn.execute(
                f"""SELECT stock_code, high FROM stock_basic
                    WHERE trade_date=? AND stock_code IN ({placeholders})""",
                [trade_date] + codes,
            ).fetchall()
            hi_map = {r[0]: r[1] or 0 for r in hi_rows}

            conn.close()

            today = date.today()
            td = date.fromisoformat(trade_date) if trade_date else today

            # 5. 组装 HoldingInfo
            for row in rows:
                code, account, entry_time, net_vol, buy_amt, buy_vol, buy_comm, sell_amt, sell_comm = row
                if net_vol <= 0 or buy_vol <= 0:
                    continue

                avg_cost = (buy_amt + (buy_comm or 0)) / buy_vol if buy_vol > 0 else 0
                pinfo = price_map.get(code)
                if not pinfo:
                    continue

                name = pinfo[1]
                cur_price = pinfo[2] or 0
                if cur_price <= 0:
                    continue

                pnl_pct = (cur_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0
                market_val = cur_price * net_vol
                entry_date_str = str(entry_time)[:10] if entry_time else trade_date
                hold_days = (td - date.fromisoformat(entry_date_str)).days if entry_date_str else 0

                sl_info = sl_map.get(code, {})
                holdings.append(HoldingInfo(
                    stock_code=code,
                    stock_name=name,
                    account=account,
                    entry_date=entry_date_str,
                    holding_days=hold_days,
                    avg_cost=round(avg_cost, 3),
                    volume=int(net_vol),
                    current_price=cur_price,
                    pnl_pct=round(pnl_pct, 2),
                    market_value=round(market_val, 2),
                    stop_loss=sl_info.get("stop_loss", 0),
                    take_profit=sl_info.get("take_profit", 0),
                    industry=pinfo[6] or "",
                    ma5=pinfo[3] or 0,
                    ma10=pinfo[4] or 0,
                    ma20=pinfo[5] or 0,
                    highest_price=hi_map.get(code, 0),
                    signal_score=sl_info.get("score", 0),
                    is_today_buy=(entry_date_str == trade_date),
                ))

            # 6. 账户概况
            for account, label, initial in [
                ("paper", "模拟盘", self.PAPER_INITIAL),
                ("real", "实盘", self.REAL_INITIAL_DEFAULT),
            ]:
                acct_holdings = [h for h in holdings if h.account == account]
                if not acct_holdings:
                    continue

                # 从 trade_orders 汇总该账户的全部成交
                conn2 = sqlite3.connect(self.screener.db_path)
                cash_flow = conn2.execute(
                    """SELECT SUM(CASE WHEN order_type='buy'
                                  THEN -filled_price * filled_volume - COALESCE(commission,0)
                                  ELSE filled_price * filled_volume - COALESCE(commission,0) END)
                       FROM trade_orders
                       WHERE order_status='filled' AND filled_volume > 0
                         AND account=?""",
                    (account,),
                ).fetchone()
                conn2.close()

                net_flow = cash_flow[0] or 0
                mkt_val = sum(h.market_value for h in acct_holdings)
                cash_est = initial + net_flow
                total_val = cash_est + mkt_val
                pos_ratio = mkt_val / total_val if total_val > 0 else 0

                # 尝试从 snapshot 取日内盈亏
                daily_pnl = 0.0
                try:
                    conn3 = sqlite3.connect(self.screener.db_path)
                    snap = conn3.execute(
                        "SELECT daily_pnl FROM trade_portfolio_snapshots WHERE trade_date=? AND account=? ORDER BY id DESC LIMIT 1",
                        (trade_date, account),
                    ).fetchone()
                    conn3.close()
                    if snap and snap[0]:
                        daily_pnl = snap[0]
                except Exception:
                    pass

                summaries.append(AccountSummary(
                    account=account,
                    label=label,
                    initial_capital=initial,
                    total_value=round(total_val, 2),
                    cash=round(cash_est, 2),
                    market_value=round(mkt_val, 2),
                    position_ratio=round(pos_ratio, 4),
                    daily_pnl=round(daily_pnl, 2),
                    position_count=len(acct_holdings),
                ))

        except Exception as e:
            logger.warning(f"加载持仓失败: {e}")

        return holdings, summaries

    # ------------------------------------------------------------------
    # 步骤 0.8: 加载复盘上下文
    # ------------------------------------------------------------------

    def _load_review_context(self, trade_date: str) -> Optional[ReviewContext]:
        """解析复盘报告，提取策略管线需要的上下文。"""
        import json as _json
        from pathlib import Path
        from system.config.settings import STORAGE_PATH

        report_dir = Path(STORAGE_PATH) / "reports"
        pattern = f"review_reports_{trade_date}_*.txt"
        files = sorted(report_dir.glob(pattern))
        if not files:
            logger.info(f"未找到复盘报告: {pattern}")
            return None

        report_path = files[0]
        logger.info(f"加载复盘报告: {report_path.name}")
        text = report_path.read_text(encoding="utf-8")

        ctx = ReviewContext(trade_date=trade_date)

        # 提取各节
        ctx.sentiment_cycle = self._extract_report_section(text, "三", "四")

        section_4 = self._extract_report_section(text, "四", "五")
        if section_4:
            ctx.main_lines = self._extract_sub_section(section_4, "绝对主线")
            ctx.sub_lines = self._extract_sub_section(section_4, "次线")
            ctx.retreating_sectors = self._extract_sub_section(section_4, "退潮方向")

        ctx.outlook = self._extract_report_section(text, "五", "六")
        ctx.monitor_conditions = self._extract_report_section(text, "八", "九")

        # 第十节: 解析仓位数字
        section_10 = self._extract_report_section(text, "十", None)
        if section_10:
            import re
            cap_m = re.search(r'仓位上限[：:]?\s*(\d+)%', section_10)
            if cap_m:
                ctx.position_cap = int(cap_m.group(1)) / 100
            sug_m = re.search(r'建议仓位[：:]?\s*(\d+)%', section_10)
            if sug_m:
                ctx.suggested_position = int(sug_m.group(1)) / 100
            attack_m = re.search(r'主攻方向[：:]?\s*(.+?)(?:\n|$)', section_10)
            if attack_m:
                ctx.main_attack = attack_m.group(1).strip()
            avoid_m = re.search(r'回避方向[：:]?\s*(.+?)(?:\n|$)', section_10)
            if avoid_m:
                ctx.avoid_direction = avoid_m.group(1).strip()

        # 第七节: 从 STOCKS JSON 提取趋势精选 codes + 结构化数据
        ctx.review_picks, ctx.review_stocks_raw = self._extract_review_picks(text, with_raw=True)

        logger.info(
            f"复盘上下文提取完成: 情绪={ctx.sentiment_cycle[:30] if ctx.sentiment_cycle else '无'}..., "
            f"精选票={len(ctx.review_picks)}只, 仓位={ctx.suggested_position:.0%}"
        )
        return ctx

    @staticmethod
    def _extract_report_section(text: str, section_num: str, next_num: Optional[str]) -> str:
        """提取复盘报告中两个节标题之间的内容。"""
        import re
        # 节标题: 行首 emoji + 空格 + 中文数字 + 、
        start_pattern = re.compile(
            rf'^[^\x00-\x7F].*?{re.escape(section_num)}、', re.MULTILINE
        )
        start_m = start_pattern.search(text)
        if not start_m:
            return ""
        content_start = start_m.end()
        nl = text.find("\n", content_start)
        if nl > 0:
            content_start = nl + 1

        if next_num:
            next_pattern = re.compile(
                rf'^[^\x00-\x7F].*?{re.escape(next_num)}、', re.MULTILINE
            )
            next_m = next_pattern.search(text, content_start)
            content_end = next_m.start() if next_m else len(text)
        else:
            stocks_m = re.search(r"<<<STOCKS>>>", text[content_start:])
            content_end = content_start + stocks_m.start() if stocks_m else len(text)

        return text[content_start:content_end].strip()

    @staticmethod
    def _extract_sub_section(text: str, keyword: str) -> str:
        """从第四节中提取子节（绝对主线/次线/退潮方向）。"""
        import re
        pattern = rf'•\s*{keyword}[：:]?\s*(.+)'
        m = re.search(pattern, text)
        if not m:
            return ""
        start = m.start()
        rest = text[m.end():]
        next_bullet = re.search(r"\n•\s", rest)
        end = m.end() + next_bullet.start() if next_bullet else len(text)
        return text[start:end].strip()

    @staticmethod
    def _extract_review_picks(text: str, with_raw: bool = False):
        """从 STOCKS JSON 块提取复盘推荐的全部股票（去重，不限角色）。
        with_raw=True 时返回 (codes, raw_dicts) 元组。
        """
        import json as _json
        import re
        stocks_m = re.search(
            r"<<<STOCKS>>>\s*(\{.*?\})\s*<<<END>>>", text, re.DOTALL
        )
        if not stocks_m:
            return ([], []) if with_raw else []
        try:
            data = _json.loads(stocks_m.group(1))
            codes = []
            seen = set()
            raw_list = []
            for s in data.get("stocks", []):
                code = s.get("code", "")
                if code and code not in seen:
                    codes.append(code)
                    seen.add(code)
                    if with_raw:
                        raw_list.append(s)
            if with_raw:
                return codes, raw_list
            return codes
        except _json.JSONDecodeError:
            return ([], []) if with_raw else []

    # ------------------------------------------------------------------
    # 步骤 1: 趋势筛选
    # ------------------------------------------------------------------

    def _screen(self, trade_date: str, market_state: str) -> list[StockScore]:
        candidates = self.screener.screen(trade_date, market_state=market_state)
        if candidates:
            strong = sum(1 for c in candidates if c.trend_mode == "strong")
            normal = len(candidates) - strong
            logger.info(f"趋势筛选: {len(candidates)} 只 (5日强:{strong} 20日稳:{normal})")
        else:
            logger.info("趋势筛选: 0 只候选")
        return candidates

    # ------------------------------------------------------------------
    # 步骤 1.5: 昨日遗留
    # ------------------------------------------------------------------

    def _load_legacy(self, trade_date: str) -> tuple[list[StockScore], dict[str, str]]:
        """加载昨日 expired 的 AI 信号，构建 StockScore 供 AI 重新评估。
        返回 (候选列表, {code: 昨日推荐理由})。"""
        try:
            legacy_signals = self.repo.get_expired_signals(trade_date)
        except Exception as e:
            logger.warning(f"加载昨日遗留失败: {e}")
            return [], {}

        if not legacy_signals:
            return [], {}

        reasons = {s["stock_code"]: s.get("reason", "") for s in legacy_signals}

        codes = [s["stock_code"] for s in legacy_signals]
        # 从 stock_basic 取今日基础数据
        try:
            import sqlite3
            conn = sqlite3.connect(self.screener.db_path)
            placeholders = ",".join(["?" for _ in codes])
            rows = conn.execute(
                f"""SELECT stock_code, stock_name, price, change_pct, total_market_cap,
                           circ_market_cap, turnover_rate, volume_ratio,
                           ma5, ma10, ma20, ma5_angle, industry,
                           main_force_net, main_force_ratio
                    FROM stock_basic
                    WHERE trade_date=? AND stock_code IN ({placeholders})""",
                [trade_date] + codes,
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.warning(f"加载遗留股票基础数据失败: {e}")
            return [], {}

        row_map = {r[0]: r for r in rows}
        candidates = []
        for sig in legacy_signals:
            code = sig["stock_code"]
            row = row_map.get(code)
            if not row or not row[2]:
                continue
            ss = StockScore(
                stock_code=code,
                stock_name=row[1],
                trend_mode="normal",
                score=sig.get("signal_score") or 50,
                price=float(row[2]) if row[2] else 0,
                change_pct=float(row[3]) if row[3] else 0,
                mcap=(float(row[4]) / 1e8) if row[4] else 0,
                circ_mcap=(float(row[5]) / 1e8) if row[5] else 0,
                turnover_rate=float(row[6]) if row[6] else 0,
                volume_ratio=float(row[7]) if row[7] else 0,
                ma5=float(row[8]) if row[8] else 0,
                ma10=float(row[9]) if row[9] else 0,
                ma20=float(row[10]) if row[10] else 0,
                ma5_angle=float(row[11]) if row[11] else 0,
                industry=row[12] or "",
                mf_wan=float(row[13]) if row[13] else 0,
                mf_ratio=float(row[14]) if row[14] else 0,
                tags=["昨日遗留"],
                scenarios=[],
            )
            candidates.append(ss)
        return candidates, reasons

    # ------------------------------------------------------------------
    # 复盘趋势精选 → OrderSignal
    # ------------------------------------------------------------------

    @staticmethod
    def _build_review_signals(raw_stocks: list[dict], trade_date: str) -> list[OrderSignal]:
        """将复盘 STOCKS JSON 中的全部推荐票（龙头/中军/补涨/趋势票）转为 OrderSignal。"""
        import re
        signals = []
        for s in raw_stocks:
            code = s.get("code", "")
            if not code:
                continue
            name = s.get("name", "")
            sl = s.get("stop_loss", 0) or 0
            tp = s.get("target", 0) or 0
            buy_cond = s.get("buy_condition", "")

            # 从买入条件文字提取参考价格
            buy_min, buy_max = None, None
            ma_match = re.search(r'约(\d+\.?\d*)', buy_cond)
            if ma_match:
                ref = float(ma_match.group(1))
                buy_min = round(ref * 0.99, 2)
                buy_max = round(ref * 1.02, 2)
            elif sl > 0:
                buy_min = round(sl * 1.02, 2)
                buy_max = round(sl * 1.06, 2)

            signals.append(OrderSignal(
                stock_code=code,
                stock_name=name,
                signal_type=SignalType.BUY,
                source=SignalSource.REVIEW,
                buy_zone_min=buy_min,
                buy_zone_max=buy_max,
                stop_loss=sl if sl > 0 else None,
                take_profit=tp if tp > 0 else None,
                target_position=0.08,
                signal_score=70,
                strategy_name="review_trend_pick",
                reason=f"复盘趋势精选: {buy_cond[:50]}",
                sector_name=s.get("sector_name", ""),
            ))
        return signals

    # ------------------------------------------------------------------
    # 步骤 2: 画像富化
    # ------------------------------------------------------------------

    def _enrich(
        self, candidates: list[StockScore], trade_date: str, market_state: str,
    ) -> list[StockProfile]:
        return self.profiler.build(candidates, trade_date, market_state=market_state)

    def _enrich_holdings(
        self, holdings: list[HoldingInfo], trade_date: str, market_state: str = "",
    ):
        """为持仓构建完整 StockProfile 画像，注入 AI 审查用技术数据。"""
        import sqlite3

        codes = [h.stock_code for h in holdings]
        if not codes:
            return

        conn = sqlite3.connect(self.screener.db_path)
        try:
            placeholders = ",".join("?" for _ in codes)
            rows = conn.execute(
                f"""SELECT stock_code, stock_name, price, change_pct,
                          total_market_cap, turnover_rate, volume_ratio,
                          ma5, ma10, ma20, ma5_angle, industry,
                          main_force_net, main_force_ratio
                   FROM stock_basic
                   WHERE trade_date=(SELECT MAX(trade_date) FROM stock_basic)
                     AND stock_code IN ({placeholders})""",
                codes,
            ).fetchall()
            basic_map = {r[0]: r for r in rows}
        finally:
            conn.close()

        scores = []
        for h in holdings:
            row = basic_map.get(h.stock_code)
            if not row:
                continue
            _, name, price, chg, mcap, turnover, vol_ratio, ma5, ma10, ma20, ma5_angle, industry, mf_net, mf_ratio = row
            mcap_yi = (mcap or 0) / 1_0000_0000
            scores.append(StockScore(
                stock_code=h.stock_code,
                stock_name=name or h.stock_name,
                trend_mode="",
                score=0,
                price=price or 0,
                change_pct=chg or 0,
                mcap=round(mcap_yi, 1),
                circ_mcap=round(mcap_yi, 1),
                turnover_rate=turnover or 0,
                volume_ratio=vol_ratio or 0,
                ma5=ma5 or 0,
                ma10=ma10 or 0,
                ma20=ma20 or 0,
                ma5_angle=ma5_angle or 0,
                industry=industry or h.industry,
                mf_wan=(mf_net or 0) / 10000,
                mf_ratio=mf_ratio or 0,
            ))

        if scores:
            holding_profiles = self.profiler.build(scores, trade_date, market_state=market_state)
            profile_map = {p.code: p for p in holding_profiles}
            for h in holdings:
                h.profile = profile_map.get(h.stock_code)

        logger.info(f"持仓画像富化: {len(scores)} 只 → {len([h for h in holdings if h.profile])} 个完整画像")

    # ------------------------------------------------------------------
    # 步骤 3: AI 分析（千问优先，DeepSeek fallback）
    # ------------------------------------------------------------------

    def _analyze(self, profiles: list[StockProfile], trade_date: str,
                 holdings: list[HoldingInfo] = None,
                 summaries: list[AccountSummary] = None,
                 review_ctx: Optional[ReviewContext] = None) -> tuple[list[OrderSignal], list]:
        # 千问优先
        advisor = AIAdvisor(model="qwen")
        if advisor._analyzers:
            try:
                signals, holdings_review = advisor.analyze(profiles, trade_date=trade_date,
                                          holdings=holdings, account_summaries=summaries,
                                          review_context=review_ctx)
                if signals:
                    logger.info(f"千问分析: {len(signals)} 个买入信号, {len(holdings_review)} 条持仓审查")
                    return signals, holdings_review
                logger.warning("千问分析返回空结果")
            except Exception as e:
                logger.warning(f"千问分析异常: {e}")

        # fallback 到 DeepSeek
        ds_advisor = AIAdvisor(model="deepseek")
        if ds_advisor._analyzers:
            logger.info("fallback 到 DeepSeek 分析")
            try:
                signals, holdings_review = ds_advisor.analyze(profiles, trade_date=trade_date,
                                             holdings=holdings, account_summaries=summaries,
                                             review_context=review_ctx)
                if signals:
                    logger.info(f"DeepSeek 分析: {len(signals)} 个买入信号, {len(holdings_review)} 条持仓审查")
                    return signals, holdings_review
                logger.warning("DeepSeek 分析返回空结果")
            except Exception as e:
                logger.error(f"DeepSeek 分析异常: {e}")

        logger.error("所有 AI 模型分析均失败")
        return [], []

    # ------------------------------------------------------------------
    # 安全网: 验证 AI 输出的股票确实通过硬关卡
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_signal(signal: OrderSignal) -> bool:
        """防止 AI 幻觉生成未通过筛选的股票。"""
        import sqlite3
        from system.config import settings
        from analysis.screening.factors import check_hard_gates
        try:
            conn = sqlite3.connect(settings.DATABASE_PATH)
            row = conn.execute(
                """SELECT * FROM stock_basic
                   WHERE stock_code=? AND trade_date=(SELECT MAX(trade_date) FROM stock_basic)""",
                (signal.stock_code,),
            ).fetchone()
            conn.close()
            if not row:
                return False
            cols = [d[0] for d in row.cursor.description]
            data = dict(zip(cols, row))
            return check_hard_gates(data)
        except Exception:
            return True  # 无法验证时放行，避免阻塞管线

    # ------------------------------------------------------------------
    # 步骤 0.6: 账户快照落库
    # ------------------------------------------------------------------

    @staticmethod
    def _save_portfolio_snapshots(summaries: list, trade_date: str):
        """保存实盘/模拟盘账户快照到 DB"""
        import json as _json
        from datetime import datetime
        from data.repo import TradeRepository

        repo = TradeRepository()
        now = datetime.now().isoformat()
        for s in summaries:
            snap_dict = {
                "trade_date": trade_date,
                "total_value": s.total_value,
                "cash": s.cash,
                "market_value": s.market_value,
                "daily_pnl": s.daily_pnl,
                "total_pnl": s.total_value - s.initial_capital,
                "drawdown": 0,
                "position_count": s.position_count,
                "sector_exposure": "{}",
                "account": s.account,
                "created_at": now,
            }
            repo.insert_snapshot(snap_dict)
            logger.info(f"{s.label}快照已保存: 总资产{s.total_value:,.0f} 仓位{s.position_ratio:.1%}")

    @staticmethod
    def _save_portfolio_positions(holdings: list, trade_date: str):
        """保存持仓明细到 trade_portfolio_positions（按账户分组落库）。"""
        from data.repo import TradeRepository

        repo = TradeRepository()
        for account in ("paper", "real"):
            acct_holdings = [h for h in holdings if h.account == account]
            if not acct_holdings:
                continue
            rows = []
            for h in acct_holdings:
                rows.append({
                    "stock_code": h.stock_code,
                    "stock_name": h.stock_name,
                    "volume": h.volume,
                    "avg_cost": h.avg_cost,
                    "current_price": h.current_price,
                    "market_value": h.market_value,
                    "pnl": (h.current_price - h.avg_cost) * h.volume,
                    "pnl_pct": h.pnl_pct,
                    "stop_loss": h.stop_loss,
                    "take_profit": h.take_profit,
                    "holding_days": h.holding_days,
                    "sector_code": h.industry or "",
                })
            repo.insert_positions(trade_date, account, rows)
        logger.info(f"持仓明细已保存: {len(holdings)} 只")

    # ------------------------------------------------------------------
    # 步骤 3.1: 持仓审查落库
    # ------------------------------------------------------------------

    def _save_holdings_review(self, holdings_review: list, trade_date: str):
        """AI 持仓审查落库 + 应用止损止盈到 bought 信号"""
        now = datetime.now().isoformat()
        for hr in holdings_review:
            review_dict = {
                "trade_date": trade_date,
                "created_at": now,
                "stock_code": hr.stock_code,
                "account": hr.account or "paper",
                "action": hr.action,
                "new_stop_loss": hr.new_stop_loss,
                "new_take_profit": hr.new_take_profit,
                "expected_holding_days": hr.expected_holding_days,
                "tomorrow_outlook": hr.tomorrow_outlook,
                "reason": hr.reason,
                "applied": 0,
            }
            self.repo.insert_holdings_review(review_dict)
            logger.info(f"持仓审查入库: {hr.stock_code} {hr.action}")

            # 应用止损止盈（实盘 + 模拟盘都需要）
            if hr.new_stop_loss or hr.new_take_profit:
                self.repo.apply_holdings_review_sl_tp(
                    trade_date, hr.stock_code,
                    new_stop_loss=hr.new_stop_loss,
                    new_take_profit=hr.new_take_profit,
                )
                logger.info(f"  已应用止损止盈: {hr.stock_code} sl={hr.new_stop_loss} tp={hr.new_take_profit}")

    # ------------------------------------------------------------------
    # 步骤 4: 信号入库
    # ------------------------------------------------------------------

    def _save_signals(self, signals: list[OrderSignal], trade_date: str) -> int:
        now = datetime.now().isoformat()
        saved = 0
        # AI 精选信号后保存，确保 REPLACE 时覆盖复盘精选的同票记录
        ordered = sorted(signals, key=lambda s: 0 if s.source == SignalSource.REVIEW else 1)
        for s in ordered:
            if s.source != SignalSource.REVIEW:
                if not self._validate_signal(s):
                    logger.warning(f"  安全网拦截: {s.stock_code} {s.stock_name} 未通过硬关卡，跳过")
                    continue
            signal_dict = {
                "trade_date": trade_date,
                "created_at": now,
                "signal_type": "BUY",
                "signal_source": s.source.name,
                "stock_code": s.stock_code,
                "stock_name": s.stock_name,
                "buy_zone_min": s.buy_zone_min,
                "buy_zone_max": s.buy_zone_max,
                "target_position": s.target_position or 0.10,
                "stop_loss": s.stop_loss,
                "take_profit": s.take_profit,
                "trailing_stop": s.trailing_stop or 0.05,
                "signal_score": s.signal_score,
                "strategy_name": s.strategy_name or "ai_advisor",
                "reason": s.reason or "",
                "status": "pending",
                "account": "paper",
            }
            sid = self.repo.insert_signal(signal_dict)
            if sid:
                saved += 1
                logger.info(f"  信号入库: {s.stock_code} {s.stock_name} zone={s.buy_zone_min}-{s.buy_zone_max}")

        logger.info(f"信号入库: {saved}/{len(signals)}")
        return saved

    # ------------------------------------------------------------------
    # 推送摘要
    # ------------------------------------------------------------------

    def _push_summary(
        self, signals: list[OrderSignal], profiles: list[StockProfile],
        trade_date: str, holdings_review: list = None,
    ):
        from system.utils.telegram import MessageSender
        from system.config.settings import TELEGRAM_REPORT_CHAT_ID, TELEGRAM_PRIVATE_CHAT_ID, TELEGRAM_REPORT_BOT_TOKEN

        pmap = {p.code: p for p in profiles}
        lines = [f"📋 明天交易信号 ({trade_date})", ""]
        for i, s in enumerate(signals, 1):
            p = pmap.get(s.stock_code)
            lines.append(self._format_signal(i, s, p))

        # 按账户拆分持仓审查
        paper_reviews = [hr for hr in (holdings_review or []) if hr.account != "real"]
        real_reviews = [hr for hr in (holdings_review or []) if hr.account == "real"]

        # 群消息：信号 + 模拟盘持仓审查（不含实盘）
        group_lines = list(lines)
        if paper_reviews:
            group_lines.append("")
            group_lines.append("---")
            group_lines.append("📊 持仓审查建议（模拟盘）")
            for hr in paper_reviews:
                group_lines.append(hr.to_summary())
        group_msg = "\n".join(group_lines)

        # 私聊消息：信号 + 全部持仓审查
        private_lines = list(lines)
        if holdings_review:
            private_lines.append("")
            private_lines.append("---")
            private_lines.append("📊 持仓审查建议")
            for hr in holdings_review:
                tag = "🔴实盘" if hr.account == "real" else "🟡模拟"
                private_lines.append(f"{tag} {hr.to_summary()}")
        private_msg = "\n".join(private_lines)

        # 推送到群（仅模拟盘内容）
        if TELEGRAM_REPORT_CHAT_ID:
            try:
                sender = MessageSender(
                    chat_id=TELEGRAM_REPORT_CHAT_ID,
                    bot_token=TELEGRAM_REPORT_BOT_TOKEN
                )
                sender.send(group_msg)
                logger.info("交易信号推送成功 (群)")
            except Exception as e:
                logger.warning(f"交易信号推送失败 (群): {e}")

        # 推送到私聊（含实盘持仓审查）
        if TELEGRAM_PRIVATE_CHAT_ID:
            try:
                sender = MessageSender(
                    chat_id=TELEGRAM_PRIVATE_CHAT_ID,
                    bot_token=TELEGRAM_REPORT_BOT_TOKEN
                )
                sender.send(private_msg)
                logger.info("交易信号推送成功 (私聊)")
            except Exception as e:
                logger.warning(f"交易信号推送失败 (私聊): {e}")

    @staticmethod
    def _format_signal(index: int, s: OrderSignal, p: Optional[StockProfile]) -> str:
        sec = s.sector_name or ""
        mcap_str = ""
        if p and p.valuation:
            mcap = p.valuation.get("mcap_yi", 0)
            if mcap:
                mcap_str = f"，市值{mcap:.0f}亿"

        source_tag = "🤖AI精选" if s.source == SignalSource.AI_ENHANCED else "📊复盘精选"
        lines = [f"{index}. {s.stock_name}（{s.stock_code}，{sec}{mcap_str}）{source_tag}"]

        # 趋势定级
        trend_rating = StrategyPipeline._derive_trend_rating(s, p)
        lines.append(f"    • 趋势定级：{trend_rating}")

        # 趋势依据
        trend_basis = StrategyPipeline._build_trend_basis(s, p)
        if trend_basis:
            lines.append(f"    • 趋势依据：{trend_basis}")

        # 理想买点
        buy_note = StrategyPipeline._build_buy_note(s, p)
        lines.append(f"    • 理想买点：{buy_note}")

        # 止损
        sl_note = StrategyPipeline._build_sl_note(s, p)
        lines.append(f"    • 止损：{sl_note}")

        # 止盈
        if s.take_profit:
            lines.append(f"    • 止盈：{s.take_profit:.2f}")

        # 持仓周期
        hold = StrategyPipeline._derive_hold_period(s, p)
        lines.append(f"    • 持仓周期：{hold}")

        if s.expected_trend:
            lines.append(f"    • 预期走势：{s.expected_trend}")
        if s.reason:
            lines.append(f"    • 分析：{s.reason}")

        return "\n".join(lines)

    @staticmethod
    def _derive_trend_rating(s: OrderSignal, p: Optional[StockProfile]) -> str:
        scenarios = p.scenarios if p else []
        if s.trend_mode == "strong":
            if "新高突破" in scenarios:
                return "主升突破"
            if "趋势加速" in scenarios:
                return "主升加速"
            if "突破追涨" in scenarios:
                return "主升初期"
            return "强趋势"
        if "强势横盘" in scenarios:
            return "强势整理"
        if any(x in scenarios for x in ("回踩MA5", "回踩MA10", "回踩MA20")):
            return "趋势回踩"
        if "底部反弹" in scenarios:
            return "底部反转"
        return "稳健趋势"

    @staticmethod
    def _build_trend_basis(s: OrderSignal, p: Optional[StockProfile]) -> str:
        if not p:
            return ""
        h = p.history
        ma5, ma10, ma20 = h.get("ma5", 0), h.get("ma10", 0), h.get("ma20", 0)
        if not ma5:
            return ""
        parts = [f"MA5={ma5:.2f} > MA10={ma10:.2f} > MA20={ma20:.2f}"]
        if ma5 > ma10 > ma20:
            parts.append("多头排列")
        yang = h.get("consecutive_yang", 0)
        if yang >= 3:
            parts.append(f"连阳{yang}日")
        return "，".join(parts)

    @staticmethod
    def _build_buy_note(s: OrderSignal, p: Optional[StockProfile]) -> str:
        zone = f"{s.buy_zone_min:.2f}-{s.buy_zone_max:.2f}" if s.buy_zone_min and s.buy_zone_max else "待定"
        if not p:
            return f"买入区间 {zone}"
        h = p.history
        price = p.snapshot.get("price", 0)
        ma5 = h.get("ma5", 0)
        ma10 = h.get("ma10", 0)
        ma20 = h.get("ma20", 0)
        # 判断买点锚定哪条均线
        if s.buy_zone_min and ma5 and abs(s.buy_zone_min - ma5) / ma5 < 0.03:
            return f"回踩 MA5（约{ma5:.2f}）附近缩量企稳可低吸，区间{zone}"
        if s.buy_zone_min and ma10 and abs(s.buy_zone_min - ma10) / ma10 < 0.03:
            return f"回踩 MA10（约{ma10:.2f}）附近缩量企稳可低吸，区间{zone}"
        if s.buy_zone_min and ma20 and abs(s.buy_zone_min - ma20) / ma20 < 0.03:
            return f"回踩 MA20（约{ma20:.2f}）附近缩量企稳可低吸，区间{zone}"
        return f"买入区间 {zone}"

    @staticmethod
    def _build_sl_note(s: OrderSignal, p: Optional[StockProfile]) -> str:
        sl_str = f"{s.stop_loss:.2f}" if s.stop_loss else "待定"
        if not p:
            return f"收盘跌破止损价 {sl_str} 止损"
        h = p.history
        ma10 = h.get("ma10", 0)
        ma20 = h.get("ma20", 0)
        # 判断止损锚定哪条均线
        if s.stop_loss and ma10 and abs(s.stop_loss - ma10) / ma10 < 0.03:
            return f"收盘跌破 MA10（约{ma10:.2f}）止损"
        if s.stop_loss and ma20 and abs(s.stop_loss - ma20) / ma20 < 0.03:
            return f"收盘跌破 MA20（约{ma20:.2f}）止损"
        # 强趋势参考MA10，稳健参考MA20
        ref_ma = ma10 if s.trend_mode == "strong" else ma20
        if ref_ma:
            return f"收盘跌破 MA{10 if s.trend_mode == 'strong' else 20}（约{ref_ma:.2f}）止损，参考价{sl_str}"
        return f"收盘跌破 {sl_str} 止损"

    @staticmethod
    def _derive_hold_period(s: OrderSignal, p: Optional[StockProfile]) -> str:
        if s.trend_mode == "strong":
            return "3天 ~ 1周"
        return "1周 ~ 2周"
