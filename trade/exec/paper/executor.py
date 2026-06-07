"""模拟盘买卖执行 — 纯执行逻辑：计算股数、调 paper_account、更新状态。"""

from system.utils.logger import get_trade_logger

logger = get_trade_logger("exec")


# ═══════════════════════════════════════════════════════════════
# 买入
# ═══════════════════════════════════════════════════════════════


def calculate_buy_volume(
    price: float,
    max_amount: int,
    total_value: float,
    cash: float,
    max_position_pct: float = 0.15,
) -> int:
    """计算可买入股数（整百股）。

    Args:
        price: 当前价格
        max_amount: 决策层算出的最大买入金额
        total_value: 账户总资产
        cash: 账户现金
        max_position_pct: 单只最大仓位占比
    """
    capital = min(max_amount, total_value * max_position_pct)
    max_affordable = int(cash * 0.9 / price / 100) * 100
    volume = min(int(capital / price / 100) * 100, max_affordable)
    return max(volume, 0)


def execute_paper_buy(
    code: str,
    name: str,
    price: float,
    volume: int,
    sl: float,
    tp: float,
    signal_id: int | None,
    source: str,
    paper_account,
    repo=None,
) -> dict:
    """执行模拟盘买入。返回 {success, cost, commission, reason, pnl_meta}。"""
    if volume < 100:
        return {"success": False, "reason": f"资金不足买入 {code}", "pnl_meta": {}}

    result = paper_account.buy(
        code, name, price, volume, signal_id=signal_id, source=source
    )

    if result.success and repo and signal_id:
        try:
            repo.update_signal_status(signal_id, "bought")
        except Exception:
            pass

    # 构建止损止盈元数据
    pnl_meta = {}
    if result.success:
        pnl_meta = {
            "sl": sl,
            "tp": tp,
            "trailing_stop": 0.05,
            "highest_price": price,
            "signal_id": signal_id,
        }

    return {
        "success": result.success,
        "cost": result.cost,
        "commission": result.commission,
        "reason": result.reason,
        "pnl_meta": pnl_meta,
    }


# ═══════════════════════════════════════════════════════════════
# 卖出
# ═══════════════════════════════════════════════════════════════


def execute_paper_sell(
    code: str,
    name: str,
    price: float,
    stype: str,
    paper_account,
    pos_meta: dict,
    bought_watch: dict,
    signal_id: int | None = None,
) -> dict:
    """执行模拟盘卖出。返回 {success, pnl, pnl_pct, reason}。

    卖出成功后自动清理 pos_meta 和 bought_watch。
    """
    result = paper_account.sell(code, price, stype, signal_id=signal_id)
    if result.success:
        pos_meta.pop(code, None)
        bought_watch.pop(code, None)
    return {
        "success": result.success,
        "pnl": result.pnl,
        "pnl_pct": result.pnl_pct,
        "reason": result.reason,
    }
