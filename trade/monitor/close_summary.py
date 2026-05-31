# -*- coding: utf-8 -*-
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
        """收盘后全部处理：DB 快照 + 信号过期 + Telegram 持仓汇总推送。"""
        # DB 快照
        snap = self.portfolio.snapshot(self._trade_date)
        self.repo.insert_snapshot(snap.to_db_dict(account="paper"))

        pos_rows = []
        for code, pos in self.portfolio.positions.items():
            pos_rows.append({
                "stock_code": code,
                "stock_name": pos.stock_name,
                "volume": pos.volume,
                "avg_cost": pos.avg_cost,
                "current_price": pos.current_price,
                "market_value": pos.market_value,
                "pnl": pos.pnl,
                "pnl_pct": pos.pnl_pct,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "holding_days": 0,
                "sector_code": pos.sector_code,
            })
        if pos_rows:
            self.repo.insert_positions(self._trade_date, "paper", pos_rows)

        logger.info(f"模拟盘快照已保存: 总资产{snap.total_value:.0f} 仓位{snap.position_count}只")

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

    # ---- 模拟盘持仓汇总 ----

    def _build_paper_summary(self) -> str:
        """生成模拟盘持仓汇总消息。"""
        p = self.portfolio
        total_pnl_pct = p.total_pnl / p.initial_cash * 100 if p.initial_cash > 0 else 0
        lines = [
            f"📊 收盘持仓报告  {self._trade_date}",
            "   ─────────────────────────",
            f"   总资产: {p.total_value:,.0f}  现金: {p.cash:,.0f}  总盈亏: {p.total_pnl:+,.0f} ({total_pnl_pct:+.2f}%)",
            f"   持仓: {len(p.positions)}/{settings.MAX_POSITIONS}  仓位: {p.position_ratio:.0%}  当日盈亏: {p.daily_pnl:+,.0f}  回撤: {p.drawdown:.2%}",
        ]

        if p.positions:
            lines.append("")
            lines.append("   📦 持仓明细")
            for code, pos in p.positions.items():
                emoji = _pnl_emoji(pos.pnl_pct)
                lines.append(
                    f"   {emoji} {code} {pos.stock_name}  现价: {pos.current_price:.2f}  "
                    f"成本: {pos.avg_cost:.2f}  盈亏: {pos.pnl_pct:+.2%}"
                )
                lines.append(
                    f"      止损: {pos.stop_loss:.2f}  止盈: {pos.take_profit:.2f}  "
                    f"市值: {pos.market_value:,.0f}"
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
                lines.append(
                    f"   📝 {otype} {t['stock_code']}  "
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
                paper_pos = self.portfolio.positions.get(code)
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
                lines.append(
                    f"   📝 {otype} {t['stock_code']}  "
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
            stocks[code]["buy_vol"] += (vol or 0)
            stocks[code]["buy_amt"] += (amt or 0)
        else:
            stocks[code]["sell_vol"] += (vol or 0)

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
