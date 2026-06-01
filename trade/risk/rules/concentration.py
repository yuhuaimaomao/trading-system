"""集中度控制"""

from trade.portfolio.portfolio import Portfolio


def check_concentration(
    stock_code: str,
    target_pct: float,
    sector_code: str,
    portfolio: Portfolio,
    max_single: float = 0.20,
    max_sector: float = 0.70,
) -> tuple[bool, str]:
    """检查单票和板块集中度"""
    # 计算加仓后的总占比（已有持仓 + 本次买入）
    current_pct = 0.0
    if stock_code in portfolio.positions:
        current_pct = portfolio.positions[stock_code].market_value / max(portfolio.total_value, 1)

    new_total_pct = current_pct + target_pct

    # 单票检查（含加仓场景）
    if new_total_pct > max_single:
        return False, f"单票 {new_total_pct:.0%} 超上限 {max_single:.0%}"

    # 板块检查
    if sector_code:
        exposure = portfolio.get_sector_exposure()
        current = exposure.get(sector_code, 0)
        if current + target_pct > max_sector:
            return False, (
                f"板块 {sector_code} {current + target_pct:.0%} 超上限 {max_sector:.0%}"
            )

    return True, ""


def get_sector_overexposure(
    portfolio: Portfolio, max_sector: float = 0.50
) -> list[str]:
    """返回超限的板块列表"""
    exposure = portfolio.get_sector_exposure()
    return [sector for sector, pct in exposure.items() if pct > max_sector]
