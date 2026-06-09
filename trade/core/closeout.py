"""收盘处理：保存快照 + 过期信号 + 推送持仓汇总。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

from data._base import connect
from system.config import settings
from system.utils.logger import get_trade_logger

logger = get_trade_logger("core")


class CloseSummaryMixin:
    """收盘处理：保存快照 + 过期信号 + 推送持仓汇总。"""

    # ---- 入口（Watcher.run 收盘段调用） ----

    def _finalize_close(self):
        """收盘后全部处理：等 15:00 后拉收盘价 → DB 快照 + 信号过期 + Telegram 推送。"""
        import time
        from datetime import datetime as _dt
        from datetime import time as _time

        # 等到 15:00:30 确保交易所收盘数据到位
        close_time = _dt.combine(_dt.today(), _time(15, 0, 30))
        wait = (close_time - _dt.now()).total_seconds()
        if wait > 0:
            logger.info(f"距收盘数据到位 {wait:.0f} 秒，等待中")
            time.sleep(wait)

        # 收盘后用 QMT 拉最新收盘价 + 日内最高价 + 昨收价刷新持仓
        if self.qmt and self.paper_account.positions:
            codes = list(self.paper_account.positions.keys())
            try:
                quotes = self.qmt.get_realtime(codes)
                for code, pos in self.paper_account.positions.items():
                    item = quotes.get(code)
                    if item:
                        new_price = None
                        try:
                            suffix = "SH" if code.startswith("6") else "SZ"
                            bars = self.qmt.get_history(
                                f"{code}.{suffix}",
                                period="1d",
                                end=self._trade_date,
                                count=1,
                            )
                            if bars:
                                new_price = bars[-1].get("close")
                        except Exception:
                            pass
                        if not new_price:
                            new_price = item.get("lastPrice") or item.get("last_price") or item.get("price")
                        if new_price:
                            pos.update_price(float(new_price))
                        day_high = item.get("high") or 0
                        if day_high:
                            pos.day_high = max(getattr(pos, "day_high", 0) or 0, float(day_high))
                        pre_close = item.get("preClose") or item.get("pre_close") or 0
                        if pre_close and not getattr(pos, "pre_close", 0):
                            pos.pre_close = float(pre_close)
                logger.info(f"收盘价刷新: {len(self.paper_account.positions)} 只持仓")
            except Exception as e:
                logger.warning(f"收盘价刷新失败: {e}")

        # 用统一的 persist 落盘（持仓明细+快照，含 daily_pnl/drawdown）
        self.paper_account._trade_date = self._trade_date
        self.paper_account._persist_state()

        # 把持仓+快照复制到下一交易日，确保下次启动时能读到
        from system.config.trading_calendar import get_next_trading_day

        next_date = get_next_trading_day(self._trade_date)
        if not next_date:
            from datetime import datetime as _dt
            from datetime import timedelta

            next_date = (_dt.strptime(self._trade_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            logger.warning(f"交易日历不可用，回退到自然日+1: {next_date}")
        try:
            conn = connect(self.db_path)
            # 快照
            snap = conn.execute(
                """SELECT total_value, cash, market_value, total_pnl, position_count, sector_exposure
                   FROM trade_portfolio_snapshots WHERE trade_date=? AND account='paper'
                   ORDER BY id DESC LIMIT 1""",
                (self._trade_date,),
            ).fetchone()
            if snap:
                conn.execute(
                    """INSERT INTO trade_portfolio_snapshots
                       (trade_date, total_value, cash, market_value, daily_pnl, total_pnl,
                        drawdown, position_count, sector_exposure, account, created_at)
                       VALUES (?, ?, ?, ?, 0, ?, 0, ?, ?, 'paper', ?)""",
                    (
                        next_date,
                        snap[0],
                        snap[1],
                        snap[2],
                        snap[3],
                        snap[4],
                        snap[5],
                        _dt.now().isoformat(),
                    ),
                )
            # 持仓
            for code, pos in self.paper_account.positions.items():
                conn.execute(
                    """INSERT OR REPLACE INTO trade_portfolio_positions
                    (trade_date, account, stock_code, stock_name, volume, avg_cost,
                     current_price, market_value, pnl, pnl_pct, pre_close, daily_pnl,
                     holding_days, entry_date, locked_volume, created_at)
                    VALUES (?, 'paper', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                    (
                        next_date,
                        code,
                        pos.stock_name,
                        pos.volume,
                        pos.avg_cost,
                        pos.current_price,
                        pos.market_value,
                        pos.pnl,
                        pos.pnl_pct,
                        getattr(pos, "pre_close", 0) or 0,
                        (getattr(pos, "holding_days", 0) or 0) + 1,
                        pos.entry_date,
                        0,  # 新交易日锁仓清零
                        _dt.now().isoformat(),
                    ),
                )
            conn.commit()
            conn.close()
            logger.info(f"持仓已复制到 {next_date}，{len(self.paper_account.positions)} 只")
        except Exception as e:
            logger.warning(f"持仓复制失败: {e}")

        # 过期信号
        self._expire_signals()

        # Telegram 推送
        try:
            paper_msg = self._build_paper_summary()
            if paper_msg:
                self._alert(paper_msg)
        except Exception as e:
            logger.warning(f"模拟盘收盘报告生成失败: {e}")

        # 实盘持仓报告（实盘未启用时跳过推送）
        if settings.REAL_TRADE_ENABLED:
            try:
                real_msg = self._build_real_summary()
                if real_msg:
                    self._alert_private(real_msg)
            except Exception as e:
                logger.warning(f"实盘收盘报告生成失败: {e}")

        # 收盘后自动审计
        self._run_post_close_audit()

        # 清理当天累积的运行时状态（防止内存泄漏）
        self._cleanup_session_state()

    def _run_post_close_audit(self):
        """收盘后自动运行盯盘自审计（如果启用）。"""
        from system.config.settings import AUDIT_ENABLED

        if not AUDIT_ENABLED:
            return
        try:
            logger.info("开始收盘审计...")

            from audit.watcher_rule_auditor import RuleAuditor

            rule = RuleAuditor(repo=self.repo)
            n_findings = len(rule.run_and_save(self._trade_date))
            logger.info(f"规则审计完成: {n_findings} 条发现")

            if n_findings > 0:
                from audit.watcher_ai_auditor import AIAuditor

                ai = AIAuditor(repo=self.repo)
                result = ai.run_and_save(self._trade_date)
                if result:
                    n_imps = len(result.get("improvements", []))
                    n_lessons = len(result.get("lessons", []))
                    logger.info(f"AI 审计完成: {n_imps} 改进, {n_lessons} 条教训")

                    imps = self.repo.get_pending_watcher_improvements()
                    if imps:
                        from audit.watcher_improvement import (
                            format_improvement_card,
                        )

                        cards = [format_improvement_card(i) for i in imps[-5:]]
                        msg = (
                            f"🔧 收盘审计 {self._trade_date}\n"
                            f"   规则审计发现 → AI 生成 {len(imps)} 条改进建议\n\n"
                            + "\n".join(cards)
                            + "\n\n   💡 使用 /apply N 应用具体改进"
                        )
                        self._alert_private(msg)
                        self._alert(f"🔧 收盘审计完成 → {len(imps)}条改进建议（详情私聊）")
        except Exception as e:
            logger.warning(f"收盘审计异常（不阻塞主流程）: {e}")

    def _get_today_open_value(self) -> float:
        """今日开盘基准 = 上个交易日最后一条快照的 total_value，无则初始资金。"""
        try:
            snap = self.repo.get_latest_snapshot_before(self._trade_date, "paper")
            if snap:
                return snap.get("total_value", 0) or 0
        except Exception:
            pass
        return self.paper_account.initial_cash

    # ---- 模拟盘持仓汇总 ----

    def _build_paper_summary(self) -> str:
        """生成模拟盘收盘报告。持仓 + 已平仓合并展示，一目了然。"""
        p = self.paper_account

        cash = p.cash
        total_mv = sum(pos.current_price * pos.volume for pos in p.positions.values())
        total_value = cash + total_mv
        total_pnl = total_value - p.initial_cash
        total_pnl_pct = total_pnl / p.initial_cash * 100
        position_ratio = total_mv / total_value * 100 if total_value > 0 else 0
        daily_pnl = total_value - self._get_today_open_value()
        daily_pnl_pct = daily_pnl / max(self._get_today_open_value(), 1) * 100

        lines = [
            f"📊 收盘持仓报告  {self._trade_date}",
            "   ─────────────────────────",
            (
                f"   总资产: {total_value:,.0f}  现金: {cash:,.0f}  "
                f"持仓: {len(p.positions)}/{settings.MAX_POSITIONS}  "
                f"仓位: {position_ratio:.0f}%"
            ),
            (
                f"   总盈亏: {total_pnl:+,.0f} ({total_pnl_pct:+.2f}%)  "
                f"当日盈亏: {daily_pnl:+,.0f} ({daily_pnl_pct:+.2f}%)"
            ),
        ]

        # ── 今日交易 ── 合并持仓 + 已平仓
        today_trades = self.repo.get_orders_by_date(self._trade_date, account="paper")
        filled = [t for t in today_trades if t.get("order_status") == "filled"]

        # 预加载：从昨日持仓表取已平仓股票的成本（避免跨轮次 FIFO 计算）
        all_traded_codes = set(t["stock_code"] for t in filled)
        all_traded_codes.update(p.positions.keys())
        prev_cost: dict[str, float] = {}
        try:
            conn = connect(self.db_path)
            prev_date = conn.execute(
                """SELECT trade_date FROM trade_portfolio_positions
                   WHERE trade_date < ? AND account='paper'
                   ORDER BY trade_date DESC LIMIT 1""",
                (self._trade_date,),
            ).fetchone()
            if prev_date:
                for code in all_traded_codes:
                    if code in p.positions:
                        continue
                    row = conn.execute(
                        """SELECT avg_cost FROM trade_portfolio_positions
                           WHERE trade_date=? AND account=? AND stock_code=?""",
                        (prev_date[0], "paper", code),
                    ).fetchone()
                    if row and row[0]:
                        prev_cost[code] = float(row[0])
            conn.close()
        except Exception:
            pass

        # 收集今日涉及的所有股票
        stocks: dict[str, dict] = {}  # code → {name, buys, sells, is_held}
        for code, pos in p.positions.items():
            stocks[code] = {
                "name": pos.stock_name or self._resolve_name(code),
                "buys": [],
                "sells": [],
                "is_held": True,
                "close": pos.current_price,
                "cost": pos.avg_cost,
                "vol": pos.volume,
            }
        for t in filled:
            code = t["stock_code"]
            if code not in stocks:
                stocks[code] = {
                    "name": self._resolve_name(code),
                    "buys": [],
                    "sells": [],
                    "is_held": False,
                    "close": t["filled_price"],
                    "cost": 0,
                    "vol": 0,
                }
            if t["order_type"] == "buy":
                stocks[code]["buys"].append(t)
            else:
                stocks[code]["sells"].append(t)

        if stocks:
            lines.append("")
            lines.append("   ── 今日交易 ──")
            for code, s in stocks.items():
                # 计算总盈亏
                sell_total = sum(r["filled_amount"] - (r.get("commission") or 0) for r in s["sells"])
                sell_vol = sum(r["filled_volume"] for r in s["sells"])

                if s["is_held"]:
                    held_cost = s["cost"] * s["vol"]
                    held_mv = s["close"] * s["vol"]
                    total_stock_pnl = held_mv - held_cost
                    avg_cost = s["cost"]
                    cost_basis = held_cost
                else:
                    # 已平仓：用昨日持仓成本 vs 今日卖出均价
                    if sell_vol > 0:
                        sell_avg = sell_total / sell_vol
                        cost = prev_cost.get(code, sell_avg)
                        total_stock_pnl = (sell_avg - cost) * sell_vol
                        avg_cost = cost
                        cost_basis = cost * sell_vol if cost > 0 else 1
                    else:
                        total_stock_pnl = 0
                        avg_cost = 0
                        cost_basis = 1

                # 日内涨跌
                if s["is_held"] and s["vol"] > 0:
                    day_pnl = (s["close"] - avg_cost) * s["vol"] if avg_cost > 0 else 0
                elif not s["is_held"] and sell_vol > 0:
                    day_pnl = total_stock_pnl
                else:
                    day_pnl = 0

                # PnL 百分比
                if cost_basis <= 0:
                    cost_basis = 1
                stock_pnl_pct = total_stock_pnl / cost_basis * 100
                day_pnl_pct = day_pnl / cost_basis * 100

                # 方向箭头
                if stock_pnl_pct > 0.5:
                    arrow = "↑"
                elif stock_pnl_pct < -0.5:
                    arrow = "↓"
                else:
                    arrow = "→"

                emoji = _pnl_emoji(stock_pnl_pct / 100)

                # 仓位占比（仅持仓）
                if s["is_held"] and s["vol"] > 0 and total_value > 0:
                    pos_pct = (s["close"] * s["vol"]) / total_value * 100
                    pos_str = f" | {pos_pct:.0f}%"
                else:
                    pos_str = ""

                lines.append(
                    f"   {emoji}{arrow} {code} {s['name']} "
                    f"| {day_pnl:+,.0f}({day_pnl_pct:+.1f}%) "
                    f"| {total_stock_pnl:+,.0f}({stock_pnl_pct:+.1f}%)"
                    f"{pos_str}"
                )

        return "\n".join(lines)

    # ---- 实盘持仓汇总 ----

    def _build_real_summary(self) -> str:
        """生成实盘持仓汇总消息（从成交记录推算）。"""
        lines = [
            f"📊 实盘持仓报告  {self._trade_date}",
            "   ─────────────────────────",
        ]

        real_positions = _derive_real_positions(self.db_path)

        if real_positions:
            lines.append("   📦 当前持仓（根据成交记录推算）")
            for rp in real_positions:
                code = rp["code"]
                vol = rp["volume"]
                cost = rp["avg_cost"]
                paper_pos = self.paper_account.positions.get(code)
                if paper_pos and paper_pos.current_price > 0:
                    price = paper_pos.current_price
                    pnl_pct = (price - cost) / cost if cost > 0 else 0
                    emoji = _pnl_emoji(pnl_pct)
                    lines.append(
                        f"   {emoji} {code}  {vol}股  成本: {cost:.2f}  现价: {price:.2f}  盈亏: {pnl_pct:+.2%}"
                    )
                else:
                    lines.append(f"   {code}  {vol}股  成本: {cost:.2f}  现价: ---")
        else:
            lines.append("   📦 无持仓记录")

        today_trades = self.repo.get_orders_by_date(self._trade_date, account="real")
        filled = [t for t in today_trades if t.get("order_status") == "filled"]
        if filled:
            lines.append("")
            lines.append(f"   📝 今日成交 {len(filled)} 笔")
            for t in filled:
                otype = "买入" if t["order_type"] == "buy" else "卖出"
                code = t["stock_code"]
                t_name = self.paper_account.positions[code].stock_name if code in self.paper_account.positions else code
                lines.append(
                    f"   📝 {otype} {code} {t_name}  "
                    f"{t['filled_price']:.2f} × {t['filled_volume']}股  "
                    f"金额: {t['filled_amount']:,.0f}"
                )

        lines.append("")
        lines.append("   ⚠️ 实盘持仓由手动回复推算，请自行核对")
        return "\n".join(lines)


# ---- 模块级辅助函数（不依赖 self） ----


def _derive_real_positions(db_path: str) -> list[dict]:
    """从 trade_orders 推算实盘当前持仓（所有历史 filled 订单 net）。"""
    conn = connect(db_path)
    rows = conn.execute(
        """SELECT stock_code, order_type,
                  SUM(filled_volume) as total_vol,
                  SUM(filled_amount) as total_amount
           FROM trade_orders
           WHERE account='real' AND order_status='filled'
           GROUP BY stock_code, order_type""",
    ).fetchall()
    conn.close()

    stocks: dict[str, dict] = {}
    for code, otype, vol, amt in rows:
        if code not in stocks:
            stocks[code] = {"buy_vol": 0, "buy_amt": 0.0, "sell_vol": 0}
        if otype == "buy":
            stocks[code]["buy_vol"] += vol or 0
            stocks[code]["buy_amt"] += amt or 0
        else:
            stocks[code]["sell_vol"] += vol or 0

    positions = []
    for code, s in stocks.items():
        net_vol = s["buy_vol"] - s["sell_vol"]
        if net_vol > 0:
            avg_cost = s["buy_amt"] / s["buy_vol"] if s["buy_vol"] > 0 else 0
            positions.append({"code": code, "volume": net_vol, "avg_cost": avg_cost})

    return sorted(positions, key=lambda x: x["code"])


def _pnl_emoji(pnl_pct: float) -> str:
    """盈亏对应的表情符号。红涨绿跌，A股惯例。"""
    if pnl_pct > 0.005:
        return "🔴"
    elif pnl_pct < -0.005:
        return "🟢"
    else:
        return "🟡"
