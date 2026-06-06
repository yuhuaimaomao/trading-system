"""情景概率状态机 — 根据微观信号更新情景概率分布。

独立于 Watcher state，可被任意模块实例化使用。
"""

from trade.monitor.state import MarketOutlook, MarketScenario, MicroSignals
from trade.scenario.definitions import PROBABILITY_URGENCY, SCENARIO_SIGNALS


class ScenarioEngine:
    """情景概率状态机。

    维护各情景的概率分布，每轮根据 MicroSignals 做贝叶斯式更新，
    产出 MarketOutlook（主情景 + 备选 + 关键关卡 + 行动建议）。
    """

    def __init__(self):
        self.probs: dict[str, float] = {
            "normal_stable": 0.50,
            "developing_uptrend": 0.10,
            "developing_downtrend": 0.10,
            "accelerating_down": 0.05,
            "accelerating_up": 0.05,
            "potential_reversal_up": 0.05,
            "potential_reversal_down": 0.05,
            "dead_bounce": 0.10,
        }
        self.scan_count: int = 0
        self.last_alert_scan: int = -100
        self.prev_outlook: MarketOutlook | None = None

    def update(
        self,
        micro: MicroSignals,
        key_support: list[float] | None = None,
        key_resistance: list[float] | None = None,
    ) -> MarketOutlook:
        """根据微观信号更新情景概率分布 — 状态机核心。"""
        self.scan_count += 1

        if key_support is None:
            key_support = []
        if key_resistance is None:
            key_resistance = []

        # 计算每个情景的原始得分
        scores = {}
        for name, cfg in SCENARIO_SIGNALS.items():
            score = 0.0
            for _, cond in cfg["confirm"]:
                try:
                    if cond(micro):
                        score += 0.15
                except Exception:
                    pass
            for _, cond in cfg["reject"]:
                try:
                    if cond(micro):
                        score -= 0.25
                except Exception:
                    pass
            scores[name] = score

        # 贝叶斯式更新
        raw = {}
        for name in SCENARIO_SIGNALS:
            prev = self.probs.get(name, 0.10)
            signal_adj = 1.0 + scores[name]
            raw[name] = prev * max(0.5, min(1.5, signal_adj))

        # 归一化
        total = sum(raw.values())
        if total > 0:
            for name in raw:
                raw[name] /= total

        # 时间衰减：无确认信号的场景向基准 0.10 靠近
        for name, cfg in SCENARIO_SIGNALS.items():
            has_confirm = any(cond(micro) for _, cond in cfg["confirm"])
            if not has_confirm:
                raw[name] = raw[name] * 0.92 + 0.10 * 0.08

        # 衰减后再次归一化
        total = sum(raw.values())
        if total > 0:
            for name in raw:
                raw[name] /= total

        self.probs = raw

        # 找出主情景和备选
        sorted_scenarios = sorted(raw.items(), key=lambda x: x[1], reverse=True)
        primary_name, primary_prob = sorted_scenarios[0]

        def build_scenario(name, prob):
            cfg = SCENARIO_SIGNALS[name]
            signals = [label for label, cond in cfg["confirm"] if cond(micro)]
            conf = "high" if prob > 0.50 else "medium" if prob > 0.25 else "low"
            return MarketScenario(
                name=name,
                label=cfg["label"],
                probability=prob,
                confidence=conf,
                direction=cfg["direction"],
                confirm_at=None,
                invalidate_at=None,
                signals=signals,
                pre_action=cfg["pre_action"] if prob >= cfg["threshold"] else "",
            )

        primary = build_scenario(primary_name, primary_prob)
        alternatives = [
            build_scenario(name, prob)
            for name, prob in sorted_scenarios[1:4]
            if prob > 0.10
        ]

        # 设置确认/否定关卡
        if primary.direction == "bearish":
            if key_resistance:
                primary.invalidate_at = key_resistance[0]
            if key_support:
                primary.confirm_at = key_support[0]
        elif primary.direction == "bullish":
            if key_resistance:
                primary.confirm_at = key_resistance[0]
            if key_support:
                primary.invalidate_at = key_support[0]

        # 紧急程度
        urgency = "none"
        for threshold, level, reason in PROBABILITY_URGENCY:
            if primary_prob >= threshold:
                urgency = level
                break

        # 一句话总结
        parts = [f"主情景: {primary.label} ({primary_prob:.0%})"]
        if primary.pre_action:
            parts.append(f"→ {primary.pre_action}")
        if primary.confirm_at:
            parts.append(f"确认: {primary.confirm_at:.2f}")
        if primary.invalidate_at:
            parts.append(f"否定: {primary.invalidate_at:.2f}")

        outlook = MarketOutlook(
            primary=primary,
            alternatives=alternatives,
            key_support=key_support,
            key_resistance=key_resistance,
            bias=primary.direction,
            urgency=urgency,
            summary=" | ".join(parts),
            last_alert_scan=self.last_alert_scan,
        )

        return outlook
