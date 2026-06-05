"""市场状态评估 — 模式 + 技术/宽度/时间/板块 → MarketRegime。"""

from trade.monitor.state import MarketOutlook, MarketRegime

# ━━━━━━━━ 模式→Regime 基础映射 ━━━━━━━━

PATTERN_REGIME = {
    "normal": dict(risk_level="safe", risk_bias="neutral", opportunity="trend_follow",
                   allow_buy=True, position_mult=1.0, stop_mult=1.0, entry_rule="standard",
                   urgent_action="", alert_level="info"),
    "uptrend": dict(risk_level="safe", risk_bias="upside", opportunity="trend_follow",
                    allow_buy=True, position_mult=1.0, stop_mult=1.0, entry_rule="pullback",
                    urgent_action="", alert_level="info"),
    "v_reversal": dict(risk_level="cautious", risk_bias="upside", opportunity="reversal",
                       allow_buy=True, position_mult=0.5, stop_mult=0.8, entry_rule="confirm",
                       urgent_action="", alert_level="warn"),
    "w_bottom": dict(risk_level="cautious", risk_bias="upside", opportunity="reversal",
                     allow_buy=True, position_mult=0.7, stop_mult=1.0, entry_rule="confirm",
                     urgent_action="", alert_level="warn"),
    "melt_up": dict(risk_level="dangerous", risk_bias="both", opportunity="chase",
                    allow_buy=True, position_mult=0.3, stop_mult=0.7, entry_rule="pullback",
                    urgent_action="tighten_stops", alert_level="warn"),
    "gap_down_recover": dict(risk_level="cautious", risk_bias="upside", opportunity="reversal",
                             allow_buy=True, position_mult=0.5, stop_mult=0.8, entry_rule="confirm",
                             urgent_action="", alert_level="warn"),
    "late_rally": dict(risk_level="dangerous", risk_bias="upside", opportunity="chase",
                       allow_buy=True, position_mult=0.3, stop_mult=0.8, entry_rule="next_day",
                       urgent_action="", alert_level="warn"),
    "wide_choppy": dict(risk_level="dangerous", risk_bias="both", opportunity="defensive",
                        allow_buy=True, position_mult=0.3, stop_mult=1.3, entry_rule="range_boundary",
                        urgent_action="", alert_level="warn"),
    "one_sided": dict(risk_level="dangerous", risk_bias="downside", opportunity="stand_aside",
                      allow_buy=False, position_mult=0.0, stop_mult=1.2, entry_rule="none",
                      urgent_action="tighten_stops", alert_level="warn"),
    "inverted_v": dict(risk_level="dangerous", risk_bias="downside", opportunity="stand_aside",
                       allow_buy=False, position_mult=0.0, stop_mult=1.2, entry_rule="none",
                       urgent_action="tighten_stops", alert_level="warn"),
    "panic": dict(risk_level="extreme", risk_bias="downside", opportunity="stand_aside",
                  allow_buy=False, position_mult=0.0, stop_mult=1.5, entry_rule="none",
                  urgent_action="reduce_positions", alert_level="critical"),
    "dead_cat": dict(risk_level="dangerous", risk_bias="downside", opportunity="stand_aside",
                     allow_buy=False, position_mult=0.0, stop_mult=1.2, entry_rule="none",
                     urgent_action="tighten_stops", alert_level="warn"),
    "m_top": dict(risk_level="dangerous", risk_bias="downside", opportunity="stand_aside",
                  allow_buy=False, position_mult=0.0, stop_mult=1.2, entry_rule="none",
                  urgent_action="tighten_stops", alert_level="warn"),
    "gap_up_fade": dict(risk_level="dangerous", risk_bias="downside", opportunity="stand_aside",
                        allow_buy=False, position_mult=0.0, stop_mult=1.2, entry_rule="none",
                        urgent_action="tighten_stops", alert_level="warn"),
    "late_dump": dict(risk_level="extreme", risk_bias="downside", opportunity="stand_aside",
                      allow_buy=False, position_mult=0.0, stop_mult=1.5, entry_rule="none",
                      urgent_action="emergency_exit", alert_level="critical"),
    "fishing_line": dict(risk_level="extreme", risk_bias="downside", opportunity="stand_aside",
                         allow_buy=False, position_mult=0.0, stop_mult=1.5, entry_rule="none",
                         urgent_action="emergency_exit", alert_level="critical"),
}

PATTERN_ALERT = {
    "panic": "🚨 恐慌下跌\n   上证: {price:.2f}  跌幅: {change:+.2%}  加速下探\n   → 暂停所有买入，考虑减仓",
    "one_sided": "⚠️ 单边下跌\n   上证: {price:.2f}  重心持续下移\n   → 暂停买入，等待止跌信号",
    "inverted_v": "⚠️ 冲高回落\n   上证: {price:.2f}  高位回落\n   → 暂停买入",
    "dead_cat": "⚠️ 弱势反弹\n   上证: {price:.2f}  反弹未过50%分位\n   → 暂不跟进",
    "melt_up": "🔥 加速冲顶\n   上证: {price:.2f}  短期加速上冲\n   → 追高风险极大，建议等待回调",
    "uptrend": "", "v_reversal": "🔄 V型反转\n   上证: {price:.2f}  {change:+.2%}  回升至50%分位以上\n   → 恢复买入信号，谨慎参与",
    "w_bottom": "🔄 W型双底\n   上证: {price:.2f}  两底接近+颈线突破\n   → 做多信号，观察量能持续性",
    "m_top": "⚠️ M型双顶\n   上证: {price:.2f}  两次冲高失败\n   → 风险大于普通倒V",
    "gap_up_fade": "⚠️ 高开低走\n   上证: {price:.2f}  跳空高开后持续回落\n   → 追高盘全线套牢",
    "gap_down_recover": "📈 低开高走\n   上证: {price:.2f}  跳空低开后持续上行\n   → 恐慌情绪修复中",
    "late_rally": "⚡ 尾盘拉升\n   上证: {price:.2f}  警惕次日低开风险\n   → 不宜追高",
    "late_dump": "🚨 尾盘跳水\n   上证: {price:.2f}  次日大概率低开\n   → 建议紧急评估持仓",
    "fishing_line": "🚨 钓鱼线出货\n   上证: {price:.2f}  全天推升后尾盘急剧下跌\n   → 典型出货信号",
    "wide_choppy": "⚠️ 宽幅震荡\n   上证: {price:.2f}  多空分歧大，方向不明\n   → 建议减仓观望",
    "normal": "",
}


def _upgrade_risk(current: str) -> str:
    """风险等级升级：safe→cautious→dangerous→extreme。"""
    order = {"safe": 0, "cautious": 1, "dangerous": 2, "extreme": 3}
    levels = ["safe", "cautious", "dangerous", "extreme"]
    cur = order.get(current, 0)
    return levels[min(cur + 1, 3)]


def assess_regime(
    pattern: str,
    index_price: float,
    prev_close: float,
    change_pct: float,
    *,
    session_phase: str = "morning",
    ma20: float = 0,
    ma60: float = 0,
    market_breadth: dict | None = None,
    multi_day_downtrend: bool = False,
    outlook: MarketOutlook | None = None,
) -> MarketRegime:
    """模式 + 技术/宽度/时间/板块 → 完整 MarketRegime。"""
    base = PATTERN_REGIME.get(pattern, PATTERN_REGIME["normal"]).copy()

    # —— 技术上下文调整 ——
    if ma20 > 0 and index_price < ma20:
        deviation = (ma20 - index_price) / ma20
        if deviation > 0.01:
            base["risk_level"] = _upgrade_risk(base["risk_level"])
            base["confidence"] = "low"
            if base["allow_buy"]:
                base["position_mult"] = max(0.3, base["position_mult"] * 0.6)
        elif deviation > 0.005 and base["allow_buy"]:
            base["position_mult"] = max(0.4, base["position_mult"] * 0.8)

    if ma60 > 0 and index_price < ma60:
        base["risk_level"] = _upgrade_risk(base["risk_level"])
        base["confidence"] = "low"

    # —— 市场宽度调整 ——
    breadth_healthy = True
    breadth = market_breadth or {}
    if breadth:
        up, down = breadth.get("up", 0), breadth.get("down", 0)
        total = up + down
        if total > 0:
            down_ratio = down / total
            if down_ratio > 0.7:
                breadth_healthy = False
                base["risk_level"] = _upgrade_risk(base["risk_level"])
                if base["allow_buy"]:
                    base["position_mult"] = max(0.2, base["position_mult"] * 0.5)
            elif down_ratio > 0.6:
                breadth_healthy = False
                if base["allow_buy"]:
                    base["position_mult"] = max(0.4, base["position_mult"] * 0.7)
            elif up / total > 0.6 and abs(change_pct) < 0.005:
                breadth_healthy = True

    # —— 时段调整 ——
    if session_phase in ("opening", "pre_open"):
        base["confidence"] = "low"
        if base["allow_buy"]:
            base["position_mult"] = max(0.5, base["position_mult"] * 0.6)
            base["entry_rule"] = "confirm"
    elif session_phase == "closing":
        if base["allow_buy"] and base["entry_rule"] == "standard":
            base["entry_rule"] = "next_day"

    # —— 跳空方向 ——
    gap_dir = ""
    if prev_close > 0:
        gap_pct = (index_price - prev_close) / prev_close
        if gap_pct >= 0.01:
            gap_dir = "gap_up"
        elif gap_pct <= -0.01:
            gap_dir = "gap_down"

    # —— 情景预测融合 ——
    if outlook is not None:
        primary = outlook.primary
        prob = primary.probability
        if primary.direction == "bearish" and outlook.urgency in ("critical", "act"):
            base["risk_level"] = _upgrade_risk(base["risk_level"])
            base["stop_mult"] = base.get("stop_mult", 1.0) * 1.2
            if prob > 0.55:
                base["allow_buy"] = False
                base["position_mult"] = 0.0
                base["entry_rule"] = "none"
                if not base["urgent_action"]:
                    base["urgent_action"] = "tighten_stops"
            elif prob > 0.35:
                base["position_mult"] = max(0.3, base["position_mult"] * 0.5)
                if base["entry_rule"] == "standard":
                    base["entry_rule"] = "confirm"
        elif primary.direction == "bullish" and primary.name == "accelerating_up":
            if outlook.urgency in ("critical", "act"):
                base["risk_level"] = _upgrade_risk(base["risk_level"])
                base["stop_mult"] = base.get("stop_mult", 1.0) * 0.7
                base["position_mult"] = max(0.3, base["position_mult"] * 0.5)
                if not base["urgent_action"]:
                    base["urgent_action"] = "tighten_stops"
        elif primary.direction == "bearish" and outlook.urgency == "watch" and prob > 0.35:
            base["stop_mult"] = base.get("stop_mult", 1.0) * 1.1
            if base["entry_rule"] == "standard":
                base["entry_rule"] = "confirm"

    # —— 构建 ——
    alert_msg = PATTERN_ALERT.get(pattern, "")
    if alert_msg:
        alert_msg = alert_msg.format(price=index_price, change=change_pct)

    return MarketRegime(
        pattern=pattern,
        risk_level=base["risk_level"],
        risk_bias=base["risk_bias"],
        confidence=base.get("confidence", "medium"),
        opportunity=base["opportunity"],
        allow_buy=base["allow_buy"],
        position_mult=base["position_mult"],
        entry_rule=base["entry_rule"],
        stop_mult=base["stop_mult"],
        urgent_action=base["urgent_action"],
        alert_level=base["alert_level"],
        alert_msg=alert_msg,
        session_phase=session_phase,
        gap_direction=gap_dir,
        breadth_healthy=breadth_healthy,
        ma20_above=(ma20 > 0 and index_price >= ma20),
        multi_day_downtrend=multi_day_downtrend,
    )
