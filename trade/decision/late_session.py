"""尾盘决策：14:30 后持仓过夜判断 + 尾盘异动。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

from datetime import datetime
from datetime import time as dt_time

from system.utils.logger import get_trade_logger

logger = get_trade_logger("decision")


class ClosingDecisionMixin:
    """尾盘决策：14:30 后持仓过夜判断 + 尾盘异动。"""

    def _check_closing(self, state, prices: dict[str, float]):
        """尾盘决策（14:30 后触发一次）：持仓过夜判断 + 尾盘异动提醒。

        综合大盘环境、板块走势和个股盈亏做过夜判断。
        """
        now = datetime.now().time()
        if now < dt_time(14, 30) or self._closing_decision_done:
            return
        if not self.paper_account.positions:
            self._closing_decision_done = True
            return

        # 大盘环境
        regime = getattr(self, "_regime", None)
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"
        pattern = getattr(regime, "pattern", "normal") if regime else "normal"
        is_market_extreme = risk_level == "extreme"
        is_market_dangerous = risk_level == "dangerous"

        now = datetime.now()
        header = f"🔔 尾盘决策  {now.strftime('%H:%M')}"
        if is_market_extreme:
            header += "  ⚠️ 大盘极端"
        elif is_market_dangerous:
            header += "  ⚠️ 大盘危险"

        lines = [header, "   ─────────────────────────"]
        has_action = False

        for code, pos in self.paper_account.positions.items():
            meta = self._pos_meta.get(code, {})
            sl = meta.get("sl", 0)
            price = prices.get(code)
            if price is None:
                continue

            pnl_pct = (price - pos.avg_cost) / pos.avg_cost * 100 if pos.avg_cost else 0
            is_today = pos.entry_date == self._trade_date
            dist_sl = (price - sl) / price * 100 if sl > 0 and price > 0 else 999

            # 板块趋势
            trend = ""
            if hasattr(self, "_get_sector_trend"):
                trend = self._get_sector_trend(code)

            if is_today:
                lines.append(
                    f"   🔒 {code} {pos.stock_name}  T+1 锁定  盈亏: {pnl_pct:+.1f}%"
                )
                continue

            # 大盘极端/恐慌 → 任何亏损都建议清仓
            if is_market_extreme and pnl_pct < 0:
                lines.append(
                    f"   🚨 {code} {pos.stock_name}  盈亏: {pnl_pct:.1f}%  现价: {price:.2f}{trend}\n"
                    f"      → 大盘极端，建议清仓，不持亏过夜"
                )
                has_action = True
            elif pnl_pct < -3:
                lines.append(
                    f"   🔴 {code} {pos.stock_name}  盈亏: {pnl_pct:.1f}%  现价: {price:.2f}  距止损: {dist_sl:.1f}%{trend}\n"
                    f"      → 建议尾盘止损，不持亏过夜"
                )
                has_action = True
            elif pnl_pct < -1 and dist_sl < 3:
                lines.append(
                    f"   🟡 {code} {pos.stock_name}  盈亏: {pnl_pct:.1f}%  现价: {price:.2f}  距止损: {dist_sl:.1f}%{trend}\n"
                    f"      → 关注是否触发止损"
                )
                has_action = True
            elif pnl_pct > 5:
                # 大盘危险时，利润可观更应减仓
                if is_market_dangerous or is_market_extreme:
                    lines.append(
                        f"   🟢 {code} {pos.stock_name}  盈亏: {pnl_pct:+.1f}%  现价: {price:.2f}{trend}\n"
                        f"      → 利润可观+大盘风险，建议减仓锁定"
                    )
                else:
                    lines.append(
                        f"   🟢 {code} {pos.stock_name}  盈亏: {pnl_pct:+.1f}%  现价: {price:.2f}{trend}\n"
                        f"      → 利润可观，可考虑减仓"
                    )
                has_action = True
            else:
                # 大盘危险时，即使微利也提示风险
                if is_market_dangerous or is_market_extreme:
                    lines.append(
                        f"   ⚠️ {code} {pos.stock_name}  盈亏: {pnl_pct:+.1f}%  现价: {price:.2f}{trend}\n"
                        f"      → 大盘风险较高，注意仓位"
                    )
                    has_action = True
                else:
                    lines.append(
                        f"   ✅ {code} {pos.stock_name}  盈亏: {pnl_pct:+.1f}%  可持过夜{trend}"
                    )

        if has_action:
            self._alert("\n".join(lines))
        self._closing_decision_done = True
