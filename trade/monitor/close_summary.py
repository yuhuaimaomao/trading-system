"""收盘处理：保存快照 + 过期信号 + 推送持仓汇总。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import logging
import sqlite3

from system.config import settings

logger = logging.getLogger(__name__)


class CloseSummaryMixin:
    """收盘处理：保存快照 + 过期信号 + 推送持仓汇总。"""

    # ---- 入口（Watcher.run 收盘段调用） ----

    def _finalize_close(self):
        """收盘后全部处理：等 15:00 后拉收盘价 → DB 快照 + 信号过期 + Telegram 推送。"""
        import time
        from datetime import datetime as _dt, time as _time

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

        # 把持仓+快照复制到明天，确保明天重启时能读到
        from datetime import datetime as _dt, timedelta
        next_date = (_dt.strptime(self._trade_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            conn = sqlite3.connect(self.db_path)
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
                    (next_date, snap[0], snap[1], snap[2], snap[3], snap[4], snap[5], _dt.now().isoformat()),
                )
            # 持仓
            for code, pos in self.paper_account.positions.items():
                conn.execute("""INSERT OR REPLACE INTO trade_portfolio_positions
                    (trade_date, account, stock_code, stock_name, volume, avg_cost, current_price,
                     market_value, pnl, pnl_pct, pre_close, daily_pnl, holding_days, entry_date, locked_volume, created_at)
                    VALUES (?, 'paper', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)""",
                    (next_date, code, pos.stock_name, pos.volume, pos.avg_cost, pos.current_price,
                     pos.market_value, pos.pnl, pos.pnl_pct,
                     getattr(pos, 'pre_close', 0) or 0,
                     (getattr(pos, 'holding_days', 0) or 0) + 1,
                     pos.entry_date, 0,  # 新交易日锁仓清零
                     _dt.now().isoformat()))
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

        try:
            real_msg = self._build_real_summary()
            if real_msg:
                self._alert_private(real_msg)
        except Exception as e:
            logger.warning(f"实盘收盘报告生成失败: {e}")

        # 收盘后自动审计
        self._run_post_close_audit()

    def _run_post_close_audit(self):
        """收盘后自动运行盯盘自审计（如果启用）。"""
        from system.config.settings import AUDIT_ENABLED
        if not AUDIT_ENABLED:
            return
        try:
            logger.info("开始收盘审计...")

            from trade.monitor.audit.rule_auditor import RuleAuditor
            rule = RuleAuditor(repo=self.repo)
            n_findings = len(rule.run_and_save(self._trade_date))
            logger.info(f"规则审计完成: {n_findings} 条发现")

            if n_findings > 0:
                from trade.monitor.audit.ai_auditor import AIAuditor
                ai = AIAuditor(repo=self.repo)
                result = ai.run_and_save(self._trade_date)
                if result:
                    n_imps = len(result.get("improvements", []))
                    n_lessons = len(result.get("lessons", []))
                    logger.info(f"AI 审计完成: {n_imps} 改进, {n_lessons} 条教训")

                    imps = self.repo.get_pending_watcher_improvements()
                    for imp in imps[-3:]:
                        from trade.monitor.audit.improvement_applier import format_improvement_card
                        self._alert(format_improvement_card(imp))
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
        """生成模拟盘持仓汇总消息。所有金额从订单表+QMT实时算，不依赖内存状态。"""
        p = self.paper_account

        # 现金从账户读取（买卖时实时更新+落库，重启从快照恢复）
        cash = p.cash

        # 持仓市值 + 回撤
        drawdown = 0.0
        total_mv = 0.0
        pos_list = []
        for code, pos in p.positions.items():
            close = pos.current_price
            cost = pos.avg_cost
            vol = pos.volume
            mv = close * vol
            total_mv += mv
            day_high = getattr(pos, "day_high", 0) or close
            dd = (day_high - close) * vol
            if dd > 0:
                drawdown += dd
            is_today = pos.entry_date == self._trade_date
            daily_per_stock = (close - cost) * vol if is_today else 0
            pos_list.append((code, pos.stock_name, close, cost, pos.pnl_pct, is_today, daily_per_stock))

        total_value = cash + total_mv
        total_pnl = total_value - p.initial_cash
        total_pnl_pct = total_pnl / p.initial_cash * 100
        position_ratio = total_mv / total_value * 100 if total_value > 0 else 0

        # 当日盈亏 = 当前总资产 - 今日开盘总资产
        daily_pnl = total_value - self._get_today_open_value()

        lines = [
            f"📊 收盘持仓报告  {self._trade_date}",
            "   ─────────────────────────",
            f"   总资产: {total_value:,.0f}  现金: {cash:,.0f}  总盈亏: {total_pnl:+,.0f} ({total_pnl_pct:+.2f}%)",
            f"   持仓: {len(p.positions)}/{settings.MAX_POSITIONS}  仓位: {position_ratio:.0f}%  当日盈亏: {daily_pnl:+,.0f}  回撤: {drawdown:,.0f}",
        ]

        if pos_list:
            lines.append("")
            for code, name, close, cost, pnl_pct, is_today, daily in pos_list:
                emoji = _pnl_emoji(pnl_pct)
                tag = f"当日 {daily:+,.0f}" if is_today else ""
                lines.append(
                    f"   {emoji} {code} {name}  收盘 {close:.2f}  "
                    f"成本 {cost:.2f}  盈亏 {pnl_pct:+.2f}%  {tag}"
                )
        else:
            lines.append("")
            lines.append("   📦 空仓")

        today_trades = self.repo.get_orders_by_date(self._trade_date, account="paper")
        filled = [t for t in today_trades if t.get("order_status") == "filled"]
        if filled:
            lines.append("")
            lines.append(f"   📝 今日成交 {len(filled)} 笔")
            for t in filled:
                otype = "买入" if t["order_type"] == "buy" else "卖出"
                code = t["stock_code"]
                t_name = p.positions[code].stock_name if code in p.positions else code
                lines.append(
                    f"   📝 {otype} {code} {t_name}  "
                    f"{t['filled_price']:.2f} × {t['filled_volume']}股  "
                    f"金额: {t['filled_amount']:,.0f}"
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
                        f"   {emoji} {code}  {vol}股  成本: {cost:.2f}  "
                        f"现价: {price:.2f}  盈亏: {pnl_pct:+.2%}"
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
                t_name = p.positions[code].stock_name if code in p.positions else code
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
    conn = sqlite3.connect(db_path)
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
    """盈亏对应的表情符号。"""
    if pnl_pct > 0.03:
        return "✅"
    elif pnl_pct > 0:
        return "🟢"
    elif pnl_pct > -0.03:
        return "🟡"
    else:
        return "🔴"
