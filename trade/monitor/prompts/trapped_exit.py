"""被套离场场景模板 — 持仓浮亏 > 5% 时辅助判断离场时机。

适用场景：深跌被套，等待反弹，需要判断是继续等还是立即止损。
AI 角色：持仓风控分析师，综合考虑个股技术+板块+大盘+消息面。
"""

from trade.monitor.prompts.schemas import PromptTemplate

TRAPPED_EXIT_TEMPLATE = PromptTemplate(
    scenario="trapped_exit",
    system_prompt=(
        "你是持仓风控分析师。核心原则：\n"
        "1. 被套后不要等解套——找最优离场点，不是找成本价\n"
        "2. 反弹到最近阻力位（BBI/MA60/布林中轨/成本价）= 减仓窗口，不要贪\n"
        "3. 板块加速走弱时，任何反弹都应减仓，不要等更高\n"
        "4. 大盘恐慌/极端风险时，反弹不可靠，优先保本\n"
        "5. 从最低点反弹超过3%后回落超过2% = 反弹失败，立即离场\n"
        "6. 板块走强 + 大盘企稳 = 可稍耐心等待更好卖点\n"
        "用一句话给出结论：立即止损 / 等反弹减仓 / 继续持有，加关键理由（不超过50字）。"
    ),
    user_template=(
        "持仓被套：{code} {name}\n"
        "成本 {cost:.2f} 现价 {price:.2f} 浮亏 {loss_pct:.1f}%\n"
        "止损线 {sl:.2f} 止盈线 {tp:.2f}\n"
        "日内：最低 {lowest:.2f} 反弹高点 {rebound_high:.2f} 当前距低点 +{rebound_pct:.1f}%\n"
        "最近阻力：{resistance_label} {resistance_price:.2f}\n"
        "板块趋势：{sector_trend}\n"
        "大盘：{market_env}（{risk_level}）\n"
        "请判断：立即止损 / 等反弹减仓 / 继续持有？一句话理由（不超过50字）。"
    ),
    required_fields=[
        "code", "name", "price", "cost", "loss_pct",
        "sl", "tp",
        "lowest", "rebound_high", "rebound_pct",
        "resistance_label", "resistance_price",
        "sector_trend", "market_env", "risk_level",
    ],
    max_tokens=80,
)
