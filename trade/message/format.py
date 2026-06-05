"""消息格式化 — Telegram/CLI/管线 dict 三种输出通道。"""


def format_buy_alert(code: str, name: str, price: float, buy_min: float,
                     buy_max: float, sl: float, tp: float, score: float,
                     source: str, trend: str, context: str = "",
                     tag: str = "") -> str:
    """格式化买入告警消息。"""
    prefix = f"{tag} " if tag else ""
    lines = [
        f"🔔 {prefix}买入信号 — {code} {name}",
        f"   现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}",
        f"   止损: {sl:.2f}  止盈: {tp:.2f}  评分: {score:.0f}",
        f"   板块: {trend}",
    ]
    if context:
        lines.append(context)
    return "\n".join(lines)


def format_buy_executed(code: str, name: str, price: float, volume: int,
                        amount: float, source: str, trend: str) -> str:
    """格式化买入成交消息。"""
    return (
        f"✅ 模拟买入 {code} {name}\n"
        f"   价格: {price:.2f}  数量: {volume}股  金额: {amount:.0f}\n"
        f"   板块: {trend}  {source}"
    )


def format_sell_executed(code: str, name: str, price: float, volume: int,
                         pnl: float, pnl_pct: float, reason: str) -> str:
    """格式化卖出成交消息。"""
    emoji = "🟢" if pnl_pct > 0 else "🔴"
    return (
        f"{emoji} 模拟卖出 {code} {name}\n"
        f"   价格: {price:.2f}  数量: {volume}股\n"
        f"   盈亏: {pnl:+.0f} ({pnl_pct:+.1f}%)\n"
        f"   原因: {reason}"
    )


def format_position_watch(code: str, name: str, price: float,
                          entry_price: float, pnl_pct: float,
                          status: str, sl: float, tp: float) -> str:
    """格式化持仓盯盘消息。"""
    status_emoji = {"healthy": "🟢", "watching": "🟡", "at_risk": "🟠",
                    "trapped": "🔴", "deep_trapped": "💀"}.get(status, "⚪")
    return (
        f"{status_emoji} 持仓监控 — {code} {name}\n"
        f"   现价: {price:.2f}  成本: {entry_price:.2f}  盈亏: {pnl_pct:+.1f}%\n"
        f"   止损: {sl:.2f}  止盈: {tp:.2f}"
    )
