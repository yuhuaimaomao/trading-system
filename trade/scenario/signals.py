"""情景信号定义 — 每个情景的加分/扣分条件 + 概率阈值映射。

从 market_state.py 提取，纯数据配置，零依赖。
"""

# ━━━━━━━━ 情景信号定义 ━━━━━━━━

SCENARIO_SIGNALS = {
    "developing_downtrend": {
        "label": "正在形成下跌结构",
        "direction": "bearish",
        "confirm": [
            ("price < EMA12", lambda m: m.ema12_pos == "below"),
            ("短期下跌", lambda m: m.price_velocity < -0.03),
            ("宽度恶化", lambda m: m.breadth_trend == "deteriorating"),
            ("下降高点", lambda m: m.lower_highs),
        ],
        "reject": [
            ("价格 > EMA12", lambda m: m.ema12_pos == "above"),
            ("宽度改善", lambda m: m.breadth_trend == "improving"),
        ],
        "threshold": 0.40,
        "pre_action": "收紧止损，暂停新买入",
    },
    "accelerating_down": {
        "label": "下跌加速 → 可能恐慌",
        "direction": "bearish",
        "confirm": [
            ("加速下跌", lambda m: m.price_accel < -0.02),
            ("放量下跌", lambda m: m.vol_pulse == "expanding" and m.vol_price_confirm == "yes"),
            ("宽度恶化", lambda m: m.breadth_pct < 0.35),
            ("振幅扩大", lambda m: m.range_expanding),
            ("价格在低位", lambda m: m.ema12_pos == "below"),
        ],
        "reject": [
            ("反弹质量强", lambda m: m.bounce_quality == "strong"),
            ("宽度改善", lambda m: m.breadth_trend == "improving"),
        ],
        "threshold": 0.35,
        "pre_action": "阻止所有买入，建议减仓",
    },
    "developing_uptrend": {
        "label": "正在形成上涨结构",
        "direction": "bullish",
        "confirm": [
            ("价格 > EMA12", lambda m: m.ema12_pos == "above"),
            ("短期上涨", lambda m: m.price_velocity > 0.03),
            ("宽度改善", lambda m: m.breadth_trend == "improving"),
            ("上升低点", lambda m: m.higher_lows),
            ("创新高", lambda m: m.higher_highs),
        ],
        "reject": [
            ("价格 < EMA12", lambda m: m.ema12_pos == "below"),
            ("宽度恶化", lambda m: m.breadth_trend == "deteriorating"),
        ],
        "threshold": 0.40,
        "pre_action": "正常买入，回调入场",
    },
    "accelerating_up": {
        "label": "上涨加速 → 可能冲顶",
        "direction": "bullish",
        "confirm": [
            ("加速上涨", lambda m: m.price_accel > 0.02),
            ("价格在高位", lambda m: m.ema12_pos == "above"),
            ("RSI超买", lambda m: m.rsi_signal in ("overbought",)),
            ("振幅扩大", lambda m: m.range_expanding),
        ],
        "reject": [
            ("量价背离", lambda m: m.vol_price_confirm == "no"),
            ("跌破EMA12", lambda m: m.ema12_just_crossed == "crossed_down"),
        ],
        "threshold": 0.30,
        "pre_action": "追高风险大，收紧止损，控制仓位",
    },
    "potential_reversal_up": {
        "label": "底部迹象 → 可能反转",
        "direction": "bullish",
        "confirm": [
            ("超卖反弹", lambda m: m.bounce_from_low > 0.2),
            ("RSI底背离", lambda m: m.rsi_signal == "divergence_up"),
            ("从低点反弹", lambda m: m.bounce_quality in ("strong",)),
            ("宽度改善", lambda m: m.breadth_trend == "improving"),
        ],
        "reject": [
            ("反弹失败", lambda m: m.bounce_quality == "failed"),
            ("继续新低", lambda m: m.price_velocity < -0.05),
        ],
        "threshold": 0.30,
        "pre_action": "关注反转确认，准备试探仓位",
    },
    "potential_reversal_down": {
        "label": "顶部迹象 → 可能反转",
        "direction": "bearish",
        "confirm": [
            ("RSI顶背离", lambda m: m.rsi_signal == "divergence_down"),
            ("测试阻力", lambda m: m.testing_resistance),
            ("量价背离", lambda m: m.vol_price_confirm == "no"),
            ("宽度恶化", lambda m: m.breadth_trend == "deteriorating"),
        ],
        "reject": [
            ("突破阻力", lambda m: m.bounce_quality == "strong"),
            ("量价确认", lambda m: m.vol_price_confirm == "yes" and m.price_velocity > 0.03),
        ],
        "threshold": 0.30,
        "pre_action": "减仓观望，不宜追高",
    },
    "dead_bounce": {
        "label": "弱反弹 → 可能死猫跳",
        "direction": "bearish",
        "confirm": [
            ("前期大跌", lambda m: m.bounce_from_low > 0.5),
            ("弱势反弹", lambda m: m.bounce_quality == "weak"),
            ("量缩反弹", lambda m: m.vol_pulse == "contracting" and m.price_velocity < 0.02),
            ("价格 < EMA12", lambda m: m.ema12_pos == "below"),
            ("宽度恶化", lambda m: m.breadth_trend == "deteriorating"),
        ],
        "reject": [
            ("放量突破", lambda m: m.vol_price_confirm == "yes" and m.price_velocity > 0.05),
            ("站上EMA12", lambda m: m.ema12_just_crossed == "crossed_up"),
            ("持续在EMA12上方", lambda m: m.ema12_pos == "above" and m.price_velocity > 0.02),
            ("创新高趋势", lambda m: m.higher_highs),
            ("日内明显上涨", lambda m: m.ema12_pos == "above" and m.breadth_trend == "improving"),
            ("无前置大跌", lambda m: m.bounce_from_low < 0.15),
        ],
        "threshold": 0.35,
        "pre_action": "不要追反弹，等确认",
    },
    "normal_stable": {
        "label": "横盘稳定",
        "direction": "neutral",
        "confirm": [
            ("振幅收缩", lambda m: m.range_contracting),
            ("宽度均衡", lambda m: 0.4 < m.breadth_pct < 0.6),
            ("速率平稳", lambda m: abs(m.price_velocity) < 0.02),
        ],
        "reject": [
            ("方向性突破", lambda m: abs(m.price_velocity) > 0.06),
            ("振幅扩大", lambda m: m.range_expanding),
        ],
        "threshold": 0.50,
        "pre_action": "正常交易，标准入场",
    },
}

# 概率阈值 → 行动级别
PROBABILITY_URGENCY = [
    (0.70, "critical", "高概率情景，立即执行预设行动"),
    (0.55, "act", "概率偏高，提前调整策略"),
    (0.35, "watch", "需要关注，做好准备"),
    (0.00, "none", "概率较低，保持观察"),
]
