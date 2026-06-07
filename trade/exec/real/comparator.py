"""双线比对器 — 收盘后比对实盘 vs 模拟盘成交

用法:
    from trade.exec.real.comparator import OrderComparator
    c = OrderComparator()
    report = c.compare("2026-05-26")
    print(c.format_report(report))
"""

from datetime import datetime
from typing import Optional

from data.repo import TradeRepository
from system.utils.logger import get_trade_logger

logger = get_trade_logger("exec")


class OrderComparator:
    """收盘后比对实盘 vs 模拟盘成交。"""

    def __init__(self, telegram_bot=None, db_path: str = None):
        self.repo = TradeRepository(db_path=db_path)
        self.telegram = telegram_bot

    def compare(self, trade_date: Optional[str] = None) -> dict:
        """比对指定日期的双线成交。

        Returns:
          {
            "trade_date": str,
            "paired": [{code, name, paper_price, real_price, paper_vol, real_vol, price_diff, diff_pct}],
            "paper_only": [{code, name, price, volume}],
            "real_only": [{code, name, price, volume}],
            "avg_slippage": float | None,
          }
        """
        trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        logger.info(f"双线比对 {trade_date}")

        paper_orders = self.repo.get_orders_by_date(trade_date, account="paper")
        real_orders = self.repo.get_orders_by_date(trade_date, account="real")

        # 按 stock_code 索引
        paper_by_code: dict[str, list[dict]] = {}
        for o in paper_orders:
            code = o["stock_code"]
            paper_by_code.setdefault(code, []).append(o)

        real_by_code: dict[str, list[dict]] = {}
        for o in real_orders:
            code = o["stock_code"]
            real_by_code.setdefault(code, []).append(o)

        all_codes = set(paper_by_code.keys()) | set(real_by_code.keys())

        paired: list[dict] = []
        paper_only: list[dict] = []
        real_only: list[dict] = []

        for code in sorted(all_codes):
            p_orders = paper_by_code.get(code, [])
            r_orders = real_by_code.get(code, [])

            if p_orders and r_orders:
                p_avg = sum(o["filled_price"] or 0 for o in p_orders) / len(p_orders)
                r_avg = sum(o["filled_price"] or 0 for o in r_orders) / len(r_orders)
                p_vol = sum(o["filled_volume"] or 0 for o in p_orders)
                r_vol = sum(o["filled_volume"] or 0 for o in r_orders)
                price_diff = round(r_avg - p_avg, 2)
                diff_pct = round(price_diff / p_avg * 100, 2) if p_avg else 0
                paired.append(
                    {
                        "code": code,
                        "name": p_orders[0].get("stock_name", code),
                        "paper_price": round(p_avg, 2),
                        "real_price": round(r_avg, 2),
                        "paper_vol": p_vol,
                        "real_vol": r_vol,
                        "price_diff": price_diff,
                        "diff_pct": diff_pct,
                    }
                )
            elif p_orders:
                for o in p_orders:
                    paper_only.append(
                        {
                            "code": code,
                            "name": o.get("stock_name", code),
                            "price": o.get("filled_price"),
                            "volume": o.get("filled_volume"),
                        }
                    )
            else:
                for o in r_orders:
                    real_only.append(
                        {
                            "code": code,
                            "name": o.get("stock_name", code),
                            "price": o.get("filled_price"),
                            "volume": o.get("filled_volume"),
                        }
                    )

        avg_slippage = None
        if paired:
            avg_slippage = round(sum(p["diff_pct"] for p in paired) / len(paired), 2)

        report = {
            "trade_date": trade_date,
            "paired": paired,
            "paper_only": paper_only,
            "real_only": real_only,
            "avg_slippage": avg_slippage,
        }
        logger.info(
            f"比对完成: 配对{len(paired)} 模拟独有{len(paper_only)} 实盘独有{len(real_only)}"
        )
        return report

    def format_report(self, report: dict) -> str:
        """格式化比对报告为 Telegram 消息。"""
        trade_date = report["trade_date"]
        paired = report["paired"]
        paper_only = report["paper_only"]
        real_only = report["real_only"]
        avg_slippage = report["avg_slippage"]

        lines = [f"📊 双线比对 {trade_date}", ""]

        if not paired and not paper_only and not real_only:
            lines.append("  今日无成交记录")
            return "\n".join(lines)

        if paired:
            slip_tag = "🟢" if avg_slippage and abs(avg_slippage) < 0.5 else "🟡"
            lines.append(
                f"配对成交: {len(paired)} 笔  平均滑点: {avg_slippage or 0:+.2f}% {slip_tag}"
            )
            for p in paired:
                lines.append(
                    f"  {p['code']}: 模拟{p['paper_price']:.2f} vs 实盘{p['real_price']:.2f}"
                    f"  差{p['price_diff']:+.2f} ({p['diff_pct']:+.2f}%)"
                    f"  量{p['paper_vol']}/{p['real_vol']}"
                )
            lines.append("")

        if paper_only:
            lines.append(f"⚠️ 模拟盘独有 ({len(paper_only)} 笔，你忘了做模拟盘):")
            for o in paper_only:
                lines.append(f"  {o['code']} {o['volume']}股 @{o['price']}")
            lines.append("")

        if real_only:
            lines.append(f"⚠️ 实盘独有 ({len(real_only)} 笔，手动入场):")
            for o in real_only:
                lines.append(f"  {o['code']} {o['volume']}股 @{o['price']}")

        return "\n".join(lines)

    def send_report(self, report: dict):
        """推送比对报告到 Telegram。"""
        if not self.telegram:
            try:
                from system.message import MessageSender

                self.telegram = MessageSender()
            except Exception as e:
                logger.warning(f"Telegram 不可用: {e}")
                return

        msg = self.format_report(report)
        try:
            self.telegram.send(msg)
        except Exception as e:
            logger.warning(f"Telegram 推送失败: {e}")
