"""大盘状态检测：模式分类、量价背离、技术拐点、熔断/波动预警。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time

from system.config import settings

logger = logging.getLogger(__name__)

# 大盘熔断阈值 — 上证跌幅超过此值暂停所有买入
INDEX_HALT_PCT = -0.02  # 上证跌幅 > 2%
INDEX_DANGER_PCT = -0.01  # 上证跌破 MA20 且跌幅 > 1%

# 交易时段
MORNING_START = dt_time(9, 30)
MORNING_END = dt_time(11, 30)
AFTERNOON_START = dt_time(13, 0)
AFTERNOON_END = dt_time(15, 0)
LATE_SESSION = dt_time(14, 30)  # 尾盘起点

# ━━━━━━━━ MarketRegime — 统一市场状态 ━━━━━━━━


@dataclass
class MarketRegime:
    """四层决策模型：DETECT→ASSESS→DECIDE 的结果。

    不再用 bool market_ok 压扁模式信息，而是把模式+上下文→评估→决策全链路
    编码到一个对象里，下游按需读取。
    """

    pattern: str = "normal"  # DETECT: 价格形态名
    risk_level: str = "safe"  # ASSESS: safe/cautious/dangerous/extreme
    risk_bias: str = "neutral"  # ASSESS: upside/downside/both/neutral
    confidence: str = "medium"  # ASSESS: high/medium/low
    opportunity: str = (
        "trend_follow"  # ASSESS: trend_follow/reversal/chase/defensive/stand_aside
    )
    allow_buy: bool = True  # DECIDE: 新开仓
    position_mult: float = 1.0  # DECIDE: 仓位乘数 (0=禁止, 1=满仓)
    entry_rule: str = "standard"  # DECIDE: standard/pullback/confirm/next_day/none
    stop_mult: float = 1.0  # DECIDE: 止损宽度乘数
    urgent_action: str = ""  # DECIDE: ""/tighten_stops/reduce_positions/emergency_exit
    alert_level: str = "info"  # DECIDE: info/warn/critical
    alert_msg: str = ""  # DECIDE: 告警内容
    # Session context
    session_phase: str = "morning"  # morning/midday/afternoon/late
    gap_direction: str = ""  # ""/gap_up/gap_down
    breadth_healthy: bool = True  # 涨跌比健康
    ma20_above: bool = True  # 指数在MA20上方
    multi_day_downtrend: bool = False  # 连续多日下跌


# ━━━━━━━━ 情景预测：MicroSignals / Scenario / Outlook ━━━━━━━━


@dataclass
class MicroSignals:
    """每轮扫描的微观信号 — 情景引擎的输入层。"""

    price_velocity: float = 0.0  # 短期速率 (%/scan)
    price_accel: float = 0.0  # 加速度（速率的变化）
    ema12_pos: str = "on"  # above / below / on
    ema12_just_crossed: str = ""  # crossed_up / crossed_down / ""
    vol_pulse: str = "normal"  # expanding / contracting / normal
    vol_price_confirm: str = "yes"  # yes=同向 / no=背离 / neutral
    breadth_pct: float = 0.5  # 涨家占比
    breadth_trend: str = "stable"  # improving / deteriorating / stable
    higher_highs: bool = False  # 持续创新高
    bounce_from_low: float = 0.0  # 从日内低点反弹幅度 (%)
    bounce_quality: str = ""  # strong / weak / failed / ""
    lower_highs: bool = False  # 高点下移
    higher_lows: bool = False  # 低点上移
    rsi_signal: str = ""  # oversold / overbought / divergence / ""
    testing_support: bool = False  # 正在测试支撑位
    testing_resistance: bool = False  # 正在测试阻力位
    range_expanding: bool = False  # 振幅在扩大
    range_contracting: bool = False  # 振幅在缩小


@dataclass
class MarketScenario:
    """单个情景：名称 + 概率 + 确认/否定关卡 + 预设行动。"""

    name: str
    label: str  # 中文标签
    probability: float = 0.0
    confidence: str = "low"  # high / medium / low
    direction: str = "neutral"  # bullish / bearish / neutral
    confirm_at: float | None = None  # 确认关卡
    invalidate_at: float | None = None  # 否定关卡
    signals: list = None  # 当前观察到的支持信号
    pre_action: str = ""  # 概率过阈值时的预设行动

    def __post_init__(self):
        if self.signals is None:
            self.signals = []


@dataclass
class MarketOutlook:
    """每轮更新的情景分布 + 关键关卡 + 行动建议。"""

    primary: MarketScenario
    alternatives: list  # [MarketScenario]
    key_support: list  # [float] 下方支撑位
    key_resistance: list  # [float] 上方阻力位
    bias: str = "neutral"  # bearish / neutral / bullish
    urgency: str = "none"  # none / watch / act / critical
    summary: str = ""  # 一句话总结
    last_alert_scan: int = 0


# ━━━━━━━━ 情景信号定义：每个情景的加分/扣分条件 ━━━━━━━━

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
            (
                "放量下跌",
                lambda m: m.vol_pulse == "expanding" and m.vol_price_confirm == "yes",
            ),
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
            (
                "量价确认",
                lambda m: m.vol_price_confirm == "yes" and m.price_velocity > 0.03,
            ),
        ],
        "threshold": 0.30,
        "pre_action": "减仓观望，不宜追高",
    },
    "dead_bounce": {
        "label": "弱反弹 → 可能死猫跳",
        "direction": "bearish",
        "confirm": [
            ("弱势反弹", lambda m: m.bounce_quality == "weak"),
            (
                "量缩反弹",
                lambda m: m.vol_pulse == "contracting" and m.price_velocity < 0.02,
            ),
            ("价格 < EMA12", lambda m: m.ema12_pos == "below"),
            ("宽度恶化", lambda m: m.breadth_trend == "deteriorating"),
        ],
        "reject": [
            (
                "放量突破",
                lambda m: m.vol_price_confirm == "yes" and m.price_velocity > 0.05,
            ),
            ("站上EMA12", lambda m: m.ema12_just_crossed == "crossed_up"),
            (
                "持续在EMA12上方",
                lambda m: m.ema12_pos == "above" and m.price_velocity > 0.02,
            ),
            ("创新高趋势", lambda m: m.higher_highs),
            (
                "日内明显上涨",
                lambda m: m.ema12_pos == "above" and m.breadth_trend == "improving",
            ),
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


# ━━━━━━━━ 模式→Regime 基础映射 ━━━━━━━━

PATTERN_REGIME = {
    "normal": dict(
        risk_level="safe",
        risk_bias="neutral",
        opportunity="trend_follow",
        allow_buy=True,
        position_mult=1.0,
        stop_mult=1.0,
        entry_rule="standard",
        urgent_action="",
        alert_level="info",
    ),
    "uptrend": dict(
        risk_level="safe",
        risk_bias="upside",
        opportunity="trend_follow",
        allow_buy=True,
        position_mult=1.0,
        stop_mult=1.0,
        entry_rule="pullback",
        urgent_action="",
        alert_level="info",
    ),
    "v_reversal": dict(
        risk_level="cautious",
        risk_bias="upside",
        opportunity="reversal",
        allow_buy=True,
        position_mult=0.5,
        stop_mult=0.8,
        entry_rule="confirm",
        urgent_action="",
        alert_level="warn",
    ),
    "w_bottom": dict(
        risk_level="cautious",
        risk_bias="upside",
        opportunity="reversal",
        allow_buy=True,
        position_mult=0.7,
        stop_mult=1.0,
        entry_rule="confirm",
        urgent_action="",
        alert_level="warn",
    ),
    "melt_up": dict(
        risk_level="dangerous",
        risk_bias="both",
        opportunity="chase",
        allow_buy=True,
        position_mult=0.3,
        stop_mult=0.7,
        entry_rule="pullback",
        urgent_action="tighten_stops",
        alert_level="warn",
    ),
    "gap_down_recover": dict(
        risk_level="cautious",
        risk_bias="upside",
        opportunity="reversal",
        allow_buy=True,
        position_mult=0.5,
        stop_mult=0.8,
        entry_rule="confirm",
        urgent_action="",
        alert_level="warn",
    ),
    "late_rally": dict(
        risk_level="dangerous",
        risk_bias="upside",
        opportunity="chase",
        allow_buy=True,
        position_mult=0.3,
        stop_mult=0.8,
        entry_rule="next_day",
        urgent_action="",
        alert_level="warn",
    ),
    "wide_choppy": dict(
        risk_level="dangerous",
        risk_bias="both",
        opportunity="defensive",
        allow_buy=True,
        position_mult=0.3,
        stop_mult=1.3,
        entry_rule="range_boundary",
        urgent_action="",
        alert_level="warn",
    ),
    "one_sided": dict(
        risk_level="dangerous",
        risk_bias="downside",
        opportunity="stand_aside",
        allow_buy=False,
        position_mult=0.0,
        stop_mult=1.2,
        entry_rule="none",
        urgent_action="tighten_stops",
        alert_level="warn",
    ),
    "inverted_v": dict(
        risk_level="dangerous",
        risk_bias="downside",
        opportunity="stand_aside",
        allow_buy=False,
        position_mult=0.0,
        stop_mult=1.2,
        entry_rule="none",
        urgent_action="tighten_stops",
        alert_level="warn",
    ),
    "panic": dict(
        risk_level="extreme",
        risk_bias="downside",
        opportunity="stand_aside",
        allow_buy=False,
        position_mult=0.0,
        stop_mult=1.5,
        entry_rule="none",
        urgent_action="reduce_positions",
        alert_level="critical",
    ),
    "dead_cat": dict(
        risk_level="dangerous",
        risk_bias="downside",
        opportunity="stand_aside",
        allow_buy=False,
        position_mult=0.0,
        stop_mult=1.2,
        entry_rule="none",
        urgent_action="tighten_stops",
        alert_level="warn",
    ),
    "m_top": dict(
        risk_level="dangerous",
        risk_bias="downside",
        opportunity="stand_aside",
        allow_buy=False,
        position_mult=0.0,
        stop_mult=1.2,
        entry_rule="none",
        urgent_action="tighten_stops",
        alert_level="warn",
    ),
    "gap_up_fade": dict(
        risk_level="dangerous",
        risk_bias="downside",
        opportunity="stand_aside",
        allow_buy=False,
        position_mult=0.0,
        stop_mult=1.2,
        entry_rule="none",
        urgent_action="tighten_stops",
        alert_level="warn",
    ),
    "late_dump": dict(
        risk_level="extreme",
        risk_bias="downside",
        opportunity="stand_aside",
        allow_buy=False,
        position_mult=0.0,
        stop_mult=1.5,
        entry_rule="none",
        urgent_action="emergency_exit",
        alert_level="critical",
    ),
    "fishing_line": dict(
        risk_level="extreme",
        risk_bias="downside",
        opportunity="stand_aside",
        allow_buy=False,
        position_mult=0.0,
        stop_mult=1.5,
        entry_rule="none",
        urgent_action="emergency_exit",
        alert_level="critical",
    ),
}

PATTERN_ALERT = {
    "panic": "🚨 恐慌下跌\n   上证: {price:.2f}  跌幅: {change:+.2%}  加速下探\n   → 暂停所有买入，考虑减仓",
    "one_sided": "⚠️ 单边下跌\n   上证: {price:.2f}  重心持续下移\n   → 暂停买入，等待止跌信号",
    "inverted_v": "⚠️ 冲高回落\n   上证: {price:.2f}  高位回落\n   → 暂停买入",
    "dead_cat": "⚠️ 弱势反弹\n   上证: {price:.2f}  反弹未过50%分位\n   → 暂不跟进",
    "melt_up": "🔥 加速冲顶\n   上证: {price:.2f}  短期加速上冲\n   → 追高风险极大，建议等待回调",
    "uptrend": "",
    "v_reversal": "🔄 V型反转\n   上证: {price:.2f}  {change:+.2%}  回升至50%分位以上\n   → 恢复买入信号，谨慎参与",
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


def _session_phase() -> str:
    """判断当前处于哪个交易时段。"""
    now = datetime.now().time()
    if now < MORNING_START:
        return "pre_open"
    if now < dt_time(10, 0):
        return "opening"  # 开盘30分钟
    if now < dt_time(11, 0):
        return "morning"
    if now < MORNING_END:
        return "late_morning"
    if now < AFTERNOON_START:
        return "lunch"
    if now < dt_time(14, 0):
        return "afternoon"
    if now < LATE_SESSION:
        return "late_afternoon"
    return "closing"  # 尾盘30分钟


class MarketStateMixin:
    """大盘状态检测：模式分类、量价背离、技术拐点、熔断/波动预警。"""

    # ━━━━━━━━ 回撤保护 ━━━━━━━━

    def _check_max_drawdown(self):
        """双重回撤保护：日亏损超 3% 或总账户回撤超 15% 时发出警报。"""
        if self._max_drawdown_alerted:
            return
        p = self.paper_account
        if p.daily_pnl < 0 and p.total_value > 0:
            daily_loss_ratio = abs(p.daily_pnl) / p.total_value
            if daily_loss_ratio > settings.MAX_DAILY_LOSS:
                self._max_drawdown_alerted = True
                self._alert(
                    f"🚨 日内熔断\n"
                    f"   日亏损: {daily_loss_ratio:.1%}  总资产: {p.total_value:.0f}  当日浮亏: {p.daily_pnl:.0f}\n"
                    f"   → 暂停所有买入，评估是否减仓"
                )
                return
        if p.drawdown > 0.15:
            self._max_drawdown_alerted = True
            self._alert(
                f"🚨 最大回撤警报\n"
                f"   总资产: {p.total_value:.0f}  回撤: {p.drawdown:.1%}\n"
                f"   → 建议立即清仓所有持仓"
            )

    # ━━━━━━━━ 市场宽度 ━━━━━━━━

    def _compute_breadth(self) -> dict:
        """从全市场快照计算涨跌家数。"""
        if not self._market_snapshot:
            return {}
        up = down = flat = 0
        for code, item in self._market_snapshot.items():
            chg = item.get("changePct", 0)
            try:
                chg = float(chg)
            except (ValueError, TypeError):
                continue
            if chg > 0:
                up += 1
            elif chg < 0:
                down += 1
            else:
                flat += 1
        return {"up": up, "down": down, "flat": flat}

    # ━━━━━━━━ 指数行情 ━━━━━━━━

    def _get_index_quote(self) -> dict | None:
        """从 collector 推送的缓存获取上证指数实时行情。"""
        return self._last_index_quote

    def _calc_intraday_ema(self, prices: list, period: int) -> float:
        """从价格序列计算日内EMA最新值。"""
        if len(prices) < period:
            return sum(prices) / len(prices) if prices else 0
        k = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        for p in prices[period:]:
            ema = p * k + ema * (1 - k)
        return ema

    # ━━━━━━━━ 模式识别（16种）━━━━━━━━━

    def _classify_market_pattern(self) -> str:
        """识别市场模式：基于多时间窗口滚动对比 + 日内EMA + 分时结构。

        已支持 16 种模式：
        趋势类: normal, uptrend, one_sided
        反转类: v_reversal, inverted_v, w_bottom, m_top
        极端类: panic, melt_up
        陷阱类: dead_cat, fishing_line
        跳空类: gap_up_fade, gap_down_recover
        尾盘类: late_rally, late_dump
        震荡类: wide_choppy
        """
        px = self._index_prices
        if len(px) < 20:
            return "normal"

        n = len(px)
        hi, lo = self._index_high, self._index_low
        if hi <= lo:
            return "normal"

        cur = px[-1]
        range_pct = (hi - lo) / lo
        pos_in_range = (cur - lo) / (hi - lo)

        # 日内EMA
        ema12 = self._calc_intraday_ema(px, 12)
        ema26 = self._calc_intraday_ema(px, 26)

        # 时间窗口
        short_n = min(15, max(5, n // 4))
        medium_n = min(60, max(20, n // 2))

        # 短期窗口
        short_recent = px[-short_n:]
        short_prev = px[-2 * short_n : -short_n] if n >= 2 * short_n else px[:short_n]
        avg_short = sum(short_recent) / (len(short_recent) or 1)
        avg_short_prev = sum(short_prev) / (len(short_prev) or 1)
        short_chg = (
            (avg_short - avg_short_prev) / avg_short_prev if avg_short_prev > 0 else 0
        )

        # 中期窗口
        medium_recent = px[-medium_n:]
        avg_medium = sum(medium_recent) / (len(medium_recent) or 1)

        # 时段
        phase = _session_phase()

        # ━━ 尾盘时段优先检测（钓鱼线最具体，优先于尾盘跳水/拉升）━━
        if phase in ("late_afternoon", "closing"):
            if self._detect_fishing_line(px, n, medium_n, short_n, hi, lo, phase):
                return "fishing_line"
            if self._detect_late_dump(px, n, short_n, short_chg, range_pct):
                return "late_dump"
            if self._detect_late_rally(px, n, short_n, short_chg, range_pct):
                return "late_rally"

        # ━━ 跳空检测（最早检查 — 开盘位置是最确定的信号）━━
        if self._detect_gap_up_fade(px, n, short_chg, pos_in_range, range_pct):
            return "gap_up_fade"
        if self._detect_gap_down_recover(px, n, short_chg, pos_in_range, range_pct):
            return "gap_down_recover"

        # ━━ 恐慌 ━━
        if range_pct > 0.01 and pos_in_range < 0.2:
            drop_short = abs(short_chg) if short_chg < -0.002 else 0
            if n >= 2 * medium_n:
                medium_prev_px = px[-2 * medium_n : -medium_n]
                avg_medium_prev = (
                    sum(medium_prev_px) / len(medium_prev_px)
                    if medium_prev_px
                    else avg_medium
                )
                drop_medium = (
                    max(0, (avg_medium_prev - avg_medium) / avg_medium_prev)
                    if avg_medium_prev > 0
                    else 0
                )
                if drop_short > drop_medium * 0.8 and drop_short > 0.003:
                    return "panic"
            elif drop_short > 0.004:
                return "panic"

        # ━━ 加速上涨(melt-up) / V型反转 / 死猫跳 ━━
        # melt-up 优先：在日内高位且加速 → 追高风险
        if range_pct > 0.01 and pos_in_range > 0.8:
            rise_short = short_chg if short_chg > 0.002 else 0
            if n >= 2 * medium_n:
                medium_prev_px = px[-2 * medium_n : -medium_n]
                avg_medium_prev = (
                    sum(medium_prev_px) / len(medium_prev_px)
                    if medium_prev_px
                    else avg_medium
                )
                rise_medium = (
                    max(0, (avg_medium - avg_medium_prev) / avg_medium_prev)
                    if avg_medium_prev > 0
                    else 0
                )
                if rise_short > rise_medium * 0.8 and rise_short > 0.002:
                    return "melt_up"
            elif rise_short > 0.003:
                return "melt_up"

        if short_chg > 0.002 and pos_in_range > 0.3:
            mid_low = min(px[-medium_n:])
            mid_start = px[-medium_n] if n >= medium_n else px[0]
            recovery = (cur - mid_low) / mid_low if mid_low > 0 else 0
            # V型需要先跌后涨：窗口起点显著高于低点(≥0.3%)
            if recovery > 0.002 and mid_start > mid_low * 1.003:
                if pos_in_range > 0.5 and cur > ema12:
                    return "v_reversal"
                if pos_in_range <= 0.5:  # 未过50%分位 = 弱势反弹
                    return "dead_cat"

        # ━━ 单边下跌 ━━
        if ema12 > 0 and cur < ema12 and short_chg < 0:
            if n >= 2 * medium_n:
                medium_prev_px = px[-2 * medium_n : -medium_n]
                avg_medium_prev = (
                    sum(medium_prev_px) / len(medium_prev_px)
                    if medium_prev_px
                    else avg_medium
                )
                if avg_medium < avg_medium_prev:
                    decline = (avg_medium_prev - avg_medium) / avg_medium_prev
                    if decline > 0.005:
                        return "one_sided"
            elif avg_medium < ema12 and short_chg < -0.003:
                return "one_sided"

        # ━━ 倒V/A型 ━━
        if range_pct > 0.01 and pos_in_range < 0.3 and short_chg < -0.002:
            open_zone = px[: min(short_n, len(px))]
            avg_open = sum(open_zone) / len(open_zone) if open_zone else lo
            if hi - avg_open > (hi - lo) * 0.35:
                return "inverted_v"

        # ━━ 单边上涨 ━━
        # 辅助判断：每 20 分钟窗口是否持续创新高
        uptrend_strengthening = self._detect_higher_highs(px)
        if uptrend_strengthening and cur > ema12:
            return "uptrend"

        if ema12 > 0 and cur > ema12:
            if abs(short_chg) < 0.001 and n >= 2 * medium_n:
                # 短周期走平但中期趋势向上 → 仍是上涨（下午开盘常见）
                medium_prev_px = px[-2 * medium_n : -medium_n]
                avg_medium_prev = (
                    sum(medium_prev_px) / len(medium_prev_px)
                    if medium_prev_px
                    else avg_medium
                )
                if avg_medium > avg_medium_prev:
                    rise = (avg_medium - avg_medium_prev) / avg_medium_prev
                    if rise > 0.005:  # 短周期平但中期涨 >0.5%，仍是上涨
                        return "uptrend"
            elif short_chg > 0:
                if n >= 2 * medium_n:
                    medium_prev_px = px[-2 * medium_n : -medium_n]
                    avg_medium_prev = (
                        sum(medium_prev_px) / len(medium_prev_px)
                        if medium_prev_px
                        else avg_medium
                    )
                    if avg_medium > avg_medium_prev:
                        rise = (avg_medium - avg_medium_prev) / avg_medium_prev
                        if rise > 0.003:
                            return "uptrend"
                elif avg_medium > ema12 and short_chg > 0.002:
                    return "uptrend"

        # ━━ 宽幅震荡 ━━ (优先于W/M型，避免震荡中的局部形状误判)
        if self._detect_wide_choppy(px, n, medium_n, ema12, ema26, range_pct):
            return "wide_choppy"

        # ━━ W型双底 ━━
        if self._detect_w_bottom(px, n, medium_n, lo, hi):
            return "w_bottom"

        # ━━ M型双顶 ━━
        if self._detect_m_top(px, n, medium_n, lo, hi):
            return "m_top"

        return "normal"

    # ━━━━━━━━ 创新高检测 ━━━━━━━━

    def _detect_higher_highs(self, px: list[float]) -> bool:
        """每 ~20 分钟窗口做一次比较，连续 3 个窗口创新高 → 强势单边上涨。"""
        if len(px) < 60:
            return False
        # 每 20 个 tick ≈ 20 分钟
        window = 20
        windows = [px[i : i + window] for i in range(0, len(px) - window + 1, window)]
        if len(windows) < 3:
            return False
        # 看最后 3 个窗口的最高点是否递增
        recent = windows[-3:]
        highs = [max(w) for w in recent]
        return highs[0] < highs[1] < highs[2]

    # ━━━━━━━━ W型双底 ━━━━━━━━

    def _detect_w_bottom(self, px, n, medium_n, lo, hi) -> bool:
        """跌→涨→再跌→再涨，两个底部低点接近，第二次探底后放量突破颈线。

        严格版 W 底：n≥60、两底差<0.8%、中间反弹>1%、二底回调深度>0.5%、
        价格已突破颈线、二底量缩。仅在下跌/震荡市中有效，单边上涨不触发。
        """
        if n < 60:
            return False

        # 单边上涨市中 W 底不成立（反转形态只在下跌后有意义）
        ema12 = self._calc_intraday_ema(px, 12)
        cur = px[-1]
        first_third_avg = sum(px[: n // 3]) / (n // 3) if n >= 3 else px[0]
        # 前 1/3 均价 < 当前价 → 整体趋势向上，非反转
        if first_third_avg < cur * 0.998 and cur > ema12:
            return False

        mid = n // 2
        first_half = px[:mid]
        second_half = px[mid:]
        cur = px[-1]

        def find_valleys(arr):
            valleys = []
            for i in range(1, len(arr) - 1):
                if arr[i] <= arr[i - 1] and arr[i] < arr[i + 1]:
                    valleys.append((i, arr[i]))
            return valleys

        v1 = find_valleys(first_half)
        v2 = find_valleys(second_half)
        if not v1 or not v2:
            return False

        bottom1 = min(v1, key=lambda x: x[1])
        bottom2 = min(v2, key=lambda x: x[1])

        # 两个底部接近（差异<0.8%）
        b1, b2 = bottom1[1], bottom2[1]
        if abs(b1 - b2) / b1 > 0.008:
            return False

        # 中间有一波反弹（两底之间的高点>底部的1%）
        mid_section = px[bottom1[0] : mid + bottom2[0]]
        if not mid_section:
            return False
        peak = max(mid_section)
        if peak <= 0 or (peak - min(b1, b2)) / min(b1, b2) < 0.01:
            return False

        # 价格必须突破颈线（中间反弹高点），否则未确认
        if cur <= peak:
            return False

        # 第二底部必须是真实的谷（周围有显著下跌），不是上升趋势中的微小回调
        valley_pos = bottom2[0]
        surrounding = second_half[
            max(0, valley_pos - 3) : min(len(second_half), valley_pos + 4)
        ]
        if surrounding:
            valley_depth = (max(surrounding) - b2) / b2 if b2 > 0 else 0
            if valley_depth < 0.005:
                return False

        # 量能确认：二底量应小于一底量（卖压衰竭）
        if self._market_turnovers and len(self._market_turnovers) >= n:
            vol1_idx = bottom1[0]
            vol2_idx = mid + bottom2[0]
            vol_data = (
                self._market_turnovers[-n:]
                if len(self._market_turnovers) >= n
                else self._market_turnovers
            )
            if vol1_idx < len(vol_data) and vol2_idx < len(vol_data):
                vol_around_b1 = vol_data[
                    max(0, vol1_idx - 2) : min(len(vol_data), vol1_idx + 3)
                ]
                vol_around_b2 = vol_data[
                    max(0, vol2_idx - 2) : min(len(vol_data), vol2_idx + 3)
                ]
                avg_vol1 = (
                    sum(vol_around_b1) / len(vol_around_b1) if vol_around_b1 else 0
                )
                avg_vol2 = (
                    sum(vol_around_b2) / len(vol_around_b2) if vol_around_b2 else 0
                )
                # 二底量高于一底 = 卖压未衰竭，假双底
                if avg_vol1 > 0 and avg_vol2 > avg_vol1 * 1.1:
                    return False

        return True

    # ━━━━━━━━ M型双顶 ━━━━━━━━

    def _detect_m_top(self, px, n, medium_n, lo, hi) -> bool:
        """涨→跌→再涨→再跌，两个顶部高点接近，第二次冲高后回落。"""
        if n < 40:
            return False
        mid = n // 2
        first_half = px[:mid]
        second_half = px[mid:]

        def find_peaks(arr):
            peaks = []
            for i in range(1, len(arr) - 1):
                if arr[i] >= arr[i - 1] and arr[i] > arr[i + 1]:
                    peaks.append((i, arr[i]))
            return peaks

        p1 = find_peaks(first_half)
        p2 = find_peaks(second_half)
        if not p1 or not p2:
            return False

        top1 = max(p1, key=lambda x: x[1])
        top2 = max(p2, key=lambda x: x[1])

        # 两个顶部接近（差异<1%）
        t1, t2 = top1[1], top2[1]
        if abs(t1 - t2) / t1 > 0.01:
            return False

        # 中间有显著回落（两顶之间的低点<顶部的1%）
        mid_section = px[top1[0] : mid + top2[0]]
        if not mid_section:
            return False
        valley = min(mid_section)
        if (max(t1, t2) - valley) / max(t1, t2) < 0.01:
            return False

        # 第二次冲高后已在回落
        cur = px[-1]
        pos_in_range = (cur - lo) / (hi - lo)
        return cur < t2 * 0.997 and pos_in_range < 0.5

    # ━━━━━━━━ 高开低走 ━━━━━━━━

    def _detect_gap_up_fade(self, px, n, short_chg, pos_in_range, range_pct) -> bool:
        """跳空高开后持续回落。需要真正的跳空高开（开盘>前收0.5%+）。"""
        if range_pct < 0.008:
            return False
        open_price = px[0]
        hi = self._index_high
        lo = self._index_low
        # 开盘必须在日内高位
        open_zone = (open_price - lo) / (hi - lo) if hi > lo else 0.5
        if open_zone < 0.6:
            return False
        # 验证真正的跳空：必须有 pre_close 且开盘 > 前收 + 0.5%
        quote = self._get_index_quote()
        if not quote:
            return False  # 无前收数据，无法确认跳空
        prev = quote.get("pre_close", 0)
        if prev <= 0 or (open_price - prev) / prev < 0.005:
            return False  # 没有真正的跳空
        # 当前在低位 + 持续下行
        return pos_in_range < 0.3 and short_chg < -0.0015

    # ━━━━━━━━ 低开高走 ━━━━━━━━

    def _detect_gap_down_recover(
        self, px, n, short_chg, pos_in_range, range_pct
    ) -> bool:
        """跳空低开后持续回升，开盘价接近日内低点，当前价接近日内高点。"""
        if range_pct < 0.008:
            return False
        open_price = px[0]
        hi = self._index_high
        lo = self._index_low
        # 开盘在低位
        open_zone = (open_price - lo) / (hi - lo) if hi > lo else 0.5
        if open_zone > 0.3:
            return False
        # 验证真正的跳空低开：必须有 pre_close 且开盘 < 前收 - 0.5%
        quote = self._get_index_quote()
        if not quote:
            return False  # 无前收数据，无法确认跳空
        prev = quote.get("pre_close", 0)
        if prev <= 0 or (prev - open_price) / prev < 0.005:
            return False  # 没有真正的跳空低开
        # 当前在高位 + 持续上行
        return pos_in_range > 0.7 and short_chg > 0.0015

    # ━━━━━━━━ 尾盘跳水 ━━━━━━━━

    def _detect_late_dump(self, px, n, short_n, short_chg, range_pct) -> bool:
        """尾盘时段快速下跌（最近窗口跌幅>0.3%）。"""
        if n < short_n * 2:
            return False
        recent = px[-short_n:]
        prev = px[-2 * short_n : -short_n]
        avg_recent = sum(recent) / len(recent)
        avg_prev = sum(prev) / len(prev)
        drop = (avg_recent - avg_prev) / avg_prev if avg_prev > 0 else 0
        return drop < -0.003

    # ━━━━━━━━ 尾盘拉升 ━━━━━━━━

    def _detect_late_rally(self, px, n, short_n, short_chg, range_pct) -> bool:
        """尾盘时段快速拉升。前80%不能已有明显涨幅（排除全天持续上涨/V型反弹）。"""
        if n < short_n * 2:
            return False
        # 前80%价格变动不能超过0.5%（排除全天上涨/V型反弹）
        early = px[: int(n * 0.8)]
        if len(early) >= 10:
            early_chg = (early[-1] - early[0]) / early[0] if early[0] > 0 else 0
            if early_chg > 0.005:
                return False
        recent = px[-short_n:]
        prev = px[-2 * short_n : -short_n]
        avg_recent = sum(recent) / len(recent)
        avg_prev = sum(prev) / len(prev)
        rise = (avg_recent - avg_prev) / avg_prev if avg_prev > 0 else 0
        return rise > 0.002

    # ━━━━━━━━ 钓鱼线 ━━━━━━━━

    def _detect_fishing_line(self, px, n, medium_n, short_n, hi, lo, phase) -> bool:
        """全天缓慢推升→尾盘急剧下跌，典型出货信号。"""
        if n < 40 or phase not in ("late_afternoon", "closing"):
            return False
        # 前半段：缓慢上涨
        first_80pct = px[: int(n * 0.8)]
        if len(first_80pct) < 15:
            return False
        first_chg = (
            (first_80pct[-1] - first_80pct[0]) / first_80pct[0]
            if first_80pct[0] > 0
            else 0
        )
        if first_chg < 0.005:  # 前半段涨幅不够
            return False
        # 后半段：急剧下跌
        last_20pct = px[int(n * 0.8) :]
        if len(last_20pct) < 5:
            return False
        last_chg = (
            (last_20pct[-1] - last_20pct[0]) / last_20pct[0] if last_20pct[0] > 0 else 0
        )
        return last_chg < -0.005

    # ━━━━━━━━ 宽幅震荡 ━━━━━━━━

    def _detect_wide_choppy(self, px, n, medium_n, ema12, ema26, range_pct) -> bool:
        """振幅>1%但无方向，价格多次穿越EMA12。"""
        if range_pct < 0.01 or n < 30:
            return False
        # 统计穿越EMA12的次数
        crosses = 0
        prev_above = px[0] > ema12 if ema12 > 0 else None
        for p in px[1:]:
            if ema12 <= 0:
                break
            cur_above = p > ema12
            if prev_above is not None and cur_above != prev_above:
                crosses += 1
            prev_above = cur_above
        # 多次穿越+最终价格靠近中心
        pos_in_range = (
            (px[-1] - self._index_low) / (self._index_high - self._index_low)
            if self._index_high > self._index_low
            else 0.5
        )
        return crosses >= 3 and 0.3 < pos_in_range < 0.7

    # ━━━━━━━━ V型反转技术确认 ━━━━━━━━

    def _confirm_reversal_tech(self) -> bool:
        """V型反转的技术确认：日内MACD金叉/RSI从超卖回升/KDJ金叉。"""
        px = self._index_prices
        if len(px) < 30:
            return False

        try:
            window = 5
            closes = []
            highs = []
            lows = []
            for i in range(0, len(px), window):
                chunk = px[i : i + window]
                closes.append(chunk[-1])
                highs.append(max(chunk))
                lows.append(min(chunk))
            if len(closes) < 26:
                return False

            from analysis.screening.indicators import calc_kdj, calc_macd, calc_rsi

            macd = calc_macd(closes)
            rsi6 = calc_rsi(closes, 6)
            kdj = calc_kdj(highs, lows, closes)

            if macd["dif"] > macd["dea"]:
                return True
            rsi_prev = calc_rsi(closes[:-1], 6) if len(closes) > 27 else 50
            if rsi6 < 40 and rsi6 > rsi_prev:
                return True
            if kdj["k"] > kdj["d"] and kdj["j"] < 50:
                return True

        except Exception:
            pass

        return False

    # ━━━━━━━━ ASSESS：上下文评估 ━━━━━━━━

    def _assess_regime(
        self,
        pattern: str,
        index_price: float,
        prev_close: float,
        change_pct: float,
        ma20: float = 0,
        ma60: float = 0,
        outlook: MarketOutlook | None = None,
    ) -> MarketRegime:
        """Layer 2-3: 模式 + 技术/宽度/时间/板块 → 完整 MarketRegime。

        同一个模式在不同上下文下可能有不同的风险等级和置信度。
        """
        base = PATTERN_REGIME.get(pattern, PATTERN_REGIME["normal"]).copy()
        phase = _session_phase()

        # —— 技术上下文调整 ——
        if ma20 > 0 and index_price < ma20:
            deviation = (ma20 - index_price) / ma20
            if deviation > 0.01:  # 显著低于MA20才升级风险
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
        breadth = getattr(self, "_market_breadth", {}) or self._compute_breadth()
        breadth_healthy = True
        if breadth:
            up, down = breadth.get("up", 0), breadth.get("down", 0)
            total = up + down
            if total > 0:
                up_ratio = up / total
                down_ratio = down / total
                # 跌多涨少 → 宽度恶化
                if down_ratio > 0.7:
                    breadth_healthy = False
                    base["risk_level"] = _upgrade_risk(base["risk_level"])
                    if base["allow_buy"]:
                        base["position_mult"] = max(0.2, base["position_mult"] * 0.5)
                elif down_ratio > 0.6:
                    breadth_healthy = False
                    if base["allow_buy"]:
                        base["position_mult"] = max(0.4, base["position_mult"] * 0.7)
                # 涨多跌少 + 指数横盘 → 个股活跃，可以正常交易
                elif up_ratio > 0.6 and abs(change_pct) < 0.005:
                    breadth_healthy = True  # 指数不涨但个股普涨，好现象

        # —— 时段调整 ——
        if phase in ("opening", "pre_open"):
            base["confidence"] = "low"
            if base["allow_buy"]:
                base["position_mult"] = max(0.5, base["position_mult"] * 0.6)
                base["entry_rule"] = "confirm"
        elif phase in ("closing",):
            # 尾盘不新开仓
            if base["allow_buy"] and base["entry_rule"] == "standard":
                base["entry_rule"] = "next_day"

        # —— 多日连续下跌调整 ——
        multi_day = self._check_multi_day_downtrend()

        # —— 跳空方向 ——
        gap_dir = ""
        if prev_close > 0:
            gap_pct = (index_price - prev_close) / prev_close
            if gap_pct >= 0.01:
                gap_dir = "gap_up"
            elif gap_pct <= -0.01:
                gap_dir = "gap_down"

        # —— 情景预测融合：前瞻性概率调整 ——
        if outlook is not None:
            primary = outlook.primary
            prob = primary.probability

            if primary.direction == "bearish" and outlook.urgency in (
                "critical",
                "act",
            ):
                # 高概率下跌 → 提前收紧
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
                # 加速冲顶 → 追高风险，提前收紧止损
                if outlook.urgency in ("critical", "act"):
                    base["risk_level"] = _upgrade_risk(base["risk_level"])
                    base["stop_mult"] = base.get("stop_mult", 1.0) * 0.7
                    base["position_mult"] = max(0.3, base["position_mult"] * 0.5)
                    if not base["urgent_action"]:
                        base["urgent_action"] = "tighten_stops"

            elif (
                primary.direction == "bearish"
                and outlook.urgency == "watch"
                and prob > 0.35
            ):
                # 需关注级别 → 轻微收紧
                base["stop_mult"] = base.get("stop_mult", 1.0) * 1.1
                if base["entry_rule"] == "standard":
                    base["entry_rule"] = "confirm"

        # —— 构建最终 Regime ——
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
            session_phase=phase,
            gap_direction=gap_dir,
            breadth_healthy=breadth_healthy,
            ma20_above=(ma20 > 0 and index_price >= ma20),
            multi_day_downtrend=multi_day,
        )

    def _check_multi_day_downtrend(self) -> bool:
        """检查是否连续多日下跌（从 index_snapshots 查近3天）。"""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT DISTINCT trade_date FROM index_snapshots
                   ORDER BY trade_date DESC LIMIT 3"""
            ).fetchall()
            conn.close()
            if len(rows) < 3:
                return False
            # 检查最近3天每日的收盘价变化
            prices = []
            for (td,) in rows:
                c2 = sqlite3.connect(self.db_path)
                r = c2.execute(
                    "SELECT price FROM index_snapshots WHERE trade_date=? ORDER BY ts DESC LIMIT 1",
                    (td,),
                ).fetchone()
                c2.close()
                if r:
                    prices.append(r[0])
            if len(prices) < 3:
                return False
            return prices[0] < prices[1] < prices[2]  # 每天都在跌(越近越低)
        except Exception:
            return False

    # ━━━━━━━━ 情景预测引擎（预测 → 关卡 → 预设行动）━━━━━━━━━

    def _init_scenario_state(self):
        """初始化情景引擎状态变量（在 Watcher.__init__ 中调用）。"""
        self._scenario_probs: dict[str, float] = {
            "normal_stable": 0.50,
            "developing_uptrend": 0.10,
            "developing_downtrend": 0.10,
            "accelerating_down": 0.05,
            "accelerating_up": 0.05,
            "potential_reversal_up": 0.05,
            "potential_reversal_down": 0.05,
            "dead_bounce": 0.10,
        }
        self._scenario_scan_count: int = 0
        self._scenario_last_alert_scan: int = -100  # 首次可告警
        self._scenario_prev_velocity: float = 0.0
        self._scenario_recent_lows: list[float] = []  # 近期低点
        self._scenario_recent_highs: list[float] = []  # 近期高点
        self._scenario_prev_breadth: float = 0.5
        self._scenario_prev_vol: float = 0.0
        self._scenario_prev_outlook: MarketOutlook | None = None

    def _detect_micro_signals(self) -> MicroSignals:
        """从当前盘面数据提取微观信号 — 情景引擎的输入层。"""
        px = self._index_prices
        if len(px) < 5:
            return MicroSignals()

        cur = px[-1]
        prev = px[-2] if len(px) >= 2 else cur
        n = len(px)

        # 价格速率（%/scan）和加速度
        velocity = (cur - prev) / prev * 100 if prev > 0 else 0
        accel = velocity - self._scenario_prev_velocity
        self._scenario_prev_velocity = velocity

        # EMA12 关系
        ema12 = self._calc_intraday_ema(px, 12)
        ema12_pos = "above" if cur > ema12 else "below" if cur < ema12 else "on"
        ema12_crossed = ""
        if len(px) >= 3:
            prev_cur = px[-2]
            prev_ema12 = self._calc_intraday_ema(px[:-1], 12)
            if prev_cur <= prev_ema12 and cur > ema12:
                ema12_crossed = "crossed_up"
            elif prev_cur >= prev_ema12 and cur < ema12:
                ema12_crossed = "crossed_down"

        # 量能脉冲
        vols = self._market_turnovers
        vol_pulse = "normal"
        vol_confirm = "neutral"
        if len(vols) >= 6:
            recent_vol = sum(vols[-3:]) / 3 if vols[-3:] else 0
            prev_vol = sum(vols[-6:-3]) / 3 if len(vols) >= 6 and vols[-6:-3] else 0
            if prev_vol > 0:
                vol_ratio = recent_vol / prev_vol
                if vol_ratio > 1.3:
                    vol_pulse = "expanding"
                elif vol_ratio < 0.7:
                    vol_pulse = "contracting"
            # 量价配合
            if vol_pulse == "expanding" and abs(velocity) > 0.02:
                vol_confirm = "yes"  # 放量同向
            elif vol_pulse == "contracting" and abs(velocity) > 0.02:
                vol_confirm = "no"  # 缩量异动=背离

        # 宽度（直接用 watcher 缓存的实时数据）
        breadth = getattr(self, "_market_breadth", {}) or self._compute_breadth()
        up, down = breadth.get("up", 0), breadth.get("down", 0)
        total = up + down
        breadth_pct = up / total if total > 0 else 0.5
        breadth_trend = "stable"
        if self._scenario_prev_breadth > 0:
            delta = breadth_pct - self._scenario_prev_breadth
            if delta > 0.05:
                breadth_trend = "improving"
            elif delta < -0.05:
                breadth_trend = "deteriorating"
        self._scenario_prev_breadth = breadth_pct

        # 反弹质量
        hi, lo = self._index_high, self._index_low
        bounce_pct = (cur - lo) / lo * 100 if lo > 0 else 0
        bounce_quality = ""
        if bounce_pct > 0:
            # 跟踪低点序列
            self._scenario_recent_lows.append(cur)
            if len(self._scenario_recent_lows) > 30:
                self._scenario_recent_lows.pop(0)
            # 反弹持续性评估（最近5个采样点中连续上行的比例）
            if len(px) >= 5:
                recent_5 = px[-5:]
                up_count = sum(
                    1 for i in range(1, len(recent_5)) if recent_5[i] > recent_5[i - 1]
                )
                # 日内涨跌方向：明显上涨时不判为弱反弹
                day_open = px[0] if len(px) > 30 else hi  # 用早期价格近似开盘价
                day_direction_up = (
                    (cur - day_open) / day_open > 0.001 if day_open > 0 else False
                )

                if up_count >= 4 and bounce_pct > 0.3:
                    bounce_quality = "strong"
                elif up_count >= 3 and not day_direction_up:
                    # 3/4 上行但日内整体不涨 → 弱势反弹
                    bounce_quality = "weak"
                elif up_count <= 1 and velocity < 0:
                    bounce_quality = "failed"

        # 高低点结构
        lower_highs = False
        higher_lows = False
        self._scenario_recent_highs.append(cur)
        if len(self._scenario_recent_highs) > 20:
            self._scenario_recent_highs.pop(0)
        if len(self._scenario_recent_highs) >= 10:
            first_half = self._scenario_recent_highs[:5]
            second_half = self._scenario_recent_highs[-5:]
            if max(second_half) < max(first_half) * 0.998:
                lower_highs = True
            if min(second_half) > min(first_half) * 1.002:
                higher_lows = True

        # RSI 信号
        rsi_signal = ""
        if len(px) >= 30:
            try:
                window = 5
                closes = []
                for i in range(0, len(px), window):
                    closes.append(px[i + window - 1])
                if len(closes) >= 14:
                    from analysis.screening.indicators import calc_rsi

                    rsi6 = calc_rsi(closes, 6)
                    if rsi6 < 25:
                        rsi_signal = "oversold"
                    elif rsi6 > 80:
                        rsi_signal = "overbought"
                    # 简易底背离：价格新低但RSI没有新低
                    if len(closes) >= 20:
                        prev_closes = closes[:-5]
                        prev_rsi = (
                            calc_rsi(prev_closes, 6) if len(prev_closes) >= 14 else 50
                        )
                        if closes[-1] < prev_closes[-1] and rsi6 > prev_rsi:
                            rsi_signal = "divergence_up"
                        elif closes[-1] > prev_closes[-1] and rsi6 < prev_rsi:
                            rsi_signal = "divergence_down"
            except Exception:
                pass

        # 振幅变化
        range_expanding = False
        range_contracting = False
        if len(px) >= 20 and hi > lo:
            current_range = (hi - lo) / lo
            # 比较前后半段
            mid = len(px) // 2
            early_hi = max(px[:mid])
            early_lo = min(px[:mid])
            if early_hi > early_lo:
                early_range = (early_hi - early_lo) / early_lo
                if current_range > early_range * 1.3:
                    range_expanding = True
                elif current_range < early_range * 0.7:
                    range_contracting = True

        # 关键位测试
        key_support, key_resistance = self._compute_key_levels()
        testing_support = any(abs(cur - s) / s < 0.003 for s in key_support)
        testing_resistance = any(abs(cur - r) / r < 0.003 for r in key_resistance)

        # 创新高检测
        higher_highs = self._detect_higher_highs(self._index_prices)

        return MicroSignals(
            price_velocity=velocity,
            price_accel=accel,
            ema12_pos=ema12_pos,
            ema12_just_crossed=ema12_crossed,
            vol_pulse=vol_pulse,
            vol_price_confirm=vol_confirm,
            breadth_pct=breadth_pct,
            breadth_trend=breadth_trend,
            higher_highs=higher_highs,
            bounce_from_low=bounce_pct,
            bounce_quality=bounce_quality,
            lower_highs=lower_highs,
            higher_lows=higher_lows,
            rsi_signal=rsi_signal,
            testing_support=testing_support,
            testing_resistance=testing_resistance,
            range_expanding=range_expanding,
            range_contracting=range_contracting,
        )

    def _compute_key_levels(self) -> tuple[list[float], list[float]]:
        """计算日内关键支撑/阻力位。

        支撑：日内低点 > 昨收 > MA20 > MA60（取在现价下方的）
        阻力：日内高点 > MA20 > MA60 > 昨收（取在现价上方的）
        """
        px = self._index_prices
        hi, lo = self._index_high, self._index_low
        if len(px) < 5 or hi <= lo:
            return [], []

        cur = px[-1]

        # 昨收（从 collector 推送或 QMT 查询）
        pre_close = 0.0
        idx_q = getattr(self, "_last_index_quote", None) or {}
        pre_close = idx_q.get("pre_close", 0) or 0

        # MA20 / MA60（从 DB 日线缓存）
        _, _, ma20 = self._get_index_baseline()
        ma60 = self._get_index_ma60()

        # 候选位：(值, 标签)
        candidates = []
        if lo > 0 and lo < cur:
            candidates.append((lo, "日内低点"))
        if pre_close > 0:
            candidates.append((pre_close, "昨收"))
        if ma20 > 0:
            candidates.append((ma20, "MA20"))
        if ma60 > 0:
            candidates.append((ma60, "MA60"))
        if hi > 0 and hi > cur:
            candidates.append((hi, "日内高点"))

        support = sorted(
            set(round(v, 2) for v, _ in candidates if v < cur), reverse=True
        )
        resistance = sorted(set(round(v, 2) for v, _ in candidates if v > cur))
        return support[:3], resistance[:3]

    def _update_scenario_engine(self, micro: MicroSignals) -> MarketOutlook:
        """根据微观信号更新情景概率分布 — 状态机核心。"""
        self._scenario_scan_count += 1

        # 计算每个情景的原始得分
        scores = {}
        for name, cfg in SCENARIO_SIGNALS.items():
            score = 0.0
            # 确认信号加分
            for _, cond in cfg["confirm"]:
                try:
                    if cond(micro):
                        score += 0.15
                except Exception:
                    pass
            # 否定信号扣分
            for _, cond in cfg["reject"]:
                try:
                    if cond(micro):
                        score -= 0.25
                except Exception:
                    pass
            scores[name] = score

        # 从上一轮概率做贝叶斯式更新
        prev_probs = getattr(self, "_scenario_probs", {})
        if not prev_probs:
            self._init_scenario_state()
            prev_probs = self._scenario_probs

        raw = {}
        for name in SCENARIO_SIGNALS:
            prev = prev_probs.get(name, 0.10)
            # 新概率 = 旧概率 × 信号调整 + 回归均值
            signal_adj = 1.0 + scores[name]  # 0.85~1.15 range
            raw[name] = prev * max(0.5, min(1.5, signal_adj))

        # 归一化
        total = sum(raw.values())
        if total > 0:
            for name in raw:
                raw[name] /= total

        # 时间衰减：归一化后，无确认信号的场景向基准 0.10 靠近
        for name, cfg in SCENARIO_SIGNALS.items():
            has_confirm = any(cond(micro) for _, cond in cfg["confirm"])
            if not has_confirm:
                raw[name] = raw[name] * 0.92 + 0.10 * 0.08

        # 衰减后再次归一化
        total = sum(raw.values())
        if total > 0:
            for name in raw:
                raw[name] /= total

        self._scenario_probs = raw

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

        # 关键关卡
        support, resistance = self._compute_key_levels()

        # 如果主情景有明确方向，设置确认/否定关卡
        if primary.direction == "bearish":
            # 下跌情景：否定关卡 = 最近阻力，确认关卡 = 最近支撑下破
            if resistance:
                primary.invalidate_at = resistance[0]
            if support:
                primary.confirm_at = support[0]
        elif primary.direction == "bullish":
            if resistance:
                primary.confirm_at = resistance[0]
            if support:
                primary.invalidate_at = support[0]

        # 紧急程度
        urgency = "none"
        urgency_reason = ""
        for threshold, level, reason in PROBABILITY_URGENCY:
            if primary_prob >= threshold:
                urgency = level
                urgency_reason = reason
                break

        bias = primary.direction

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
            key_support=support,
            key_resistance=resistance,
            bias=bias,
            urgency=urgency,
            summary=" | ".join(parts),
            last_alert_scan=self._scenario_last_alert_scan,
        )

        return outlook

    def _push_scenario_alert(self, outlook: MarketOutlook):
        """情景预判告警：只在概率变化显著或主情景切换时推送。"""
        prev = self._scenario_prev_outlook
        scan = self._scenario_scan_count

        # 首次不告警，但记住当前状态供下次比较
        if prev is None:
            self._scenario_prev_outlook = outlook
            return

        # 判断是否需要告警
        should_alert = False
        reason = ""

        # 1. 主情景切换（跳过正常→横盘这类无意义切换）
        if outlook.primary.name != prev.primary.name:
            boring = {"normal_stable", "developing_uptrend", "wide_choppy"}
            if not (outlook.primary.name in boring and prev.primary.name in boring):
                should_alert = True
                reason = "主情景切换"

        # 2. 主情景概率大幅变化 (>25%)
        prob_delta = outlook.primary.probability - prev.primary.probability
        if abs(prob_delta) > 0.25:
            should_alert = True
            reason = f"概率{'上升' if prob_delta > 0 else '下降'} {abs(prob_delta):.0%}"

        # 3. 紧急程度升级
        if outlook.urgency in ("critical", "act") and prev.urgency in ("none", "watch"):
            should_alert = True
            reason = f"紧急程度升级 → {outlook.urgency}"

        # 无变化且非紧急，不推送
        if not should_alert:
            self._scenario_prev_outlook = outlook
            return

        # 4. 关键关卡接近（距最近关卡 < 0.3%）
        if outlook.key_support and outlook.key_resistance:
            price = self._index_prices[-1] if self._index_prices else 0
            nearest_support = outlook.key_support[0] if outlook.key_support else 0
            nearest_resistance = (
                outlook.key_resistance[0] if outlook.key_resistance else 0
            )
            if nearest_support > 0 and (price - nearest_support) / price < 0.003:
                should_alert = True
                reason = f"接近支撑 {nearest_support:.2f}"

        # 去重：至少间隔 20 轮
        if should_alert and scan - self._scenario_last_alert_scan < 20:
            should_alert = False

        # 每轮更新 prev_outlook（用于概率变化对比），告警去重用单独的 last_alert_scan
        self._scenario_prev_outlook = outlook

        if not should_alert:
            return

        self._scenario_last_alert_scan = scan

        # 构建消息
        alt_parts = []
        for alt in outlook.alternatives:
            alt_parts.append(f"{alt.label} ({alt.probability:.0%})")

        # 主情景 + 备选并一行
        primary_str = f"{outlook.primary.label} ({outlook.primary.probability:.0%})"
        if alt_parts:
            primary_str += f"  |  {'  '.join(alt_parts)}"

        confirm_at = outlook.primary.confirm_at
        invalidate_at = outlook.primary.invalidate_at
        gate_line = ""
        if confirm_at or invalidate_at:
            parts = []
            direction = outlook.primary.direction
            if confirm_at:
                verb = "跌破" if direction == "bearish" else "突破"
                parts.append(f"{verb} {confirm_at:.2f} 确认")
            if invalidate_at:
                verb = "突破" if direction == "bearish" else "跌破"
                parts.append(f"{verb} {invalidate_at:.2f} 否定")
            gate_line = f"   {' / '.join(parts)}\n"

        self._alert(
            f"🔮 市场预判  {datetime.now().strftime('%H:%M')}\n"
            f"   {primary_str}\n"
            + (f"{gate_line}" if gate_line else "")
            + f"   → {outlook.primary.pre_action or '保持观察'}"
        )

        self._scenario_prev_outlook = outlook

    # ━━━━━━━━ 三层联动调整因子（大盘→板块→个股）━━━━━━━━━

    def _get_market_adjustment(self, code: str, sector_trend: str = "") -> dict:
        """从情景引擎 + 板块趋势 计算三层联动调整因子。

        大盘、板块、个股不是割裂的三层，而是一体化联动：
        - 大盘偏空 + 板块走弱 = 调整放大
        - 大盘偏空 + 板块走强 = 调整减弱（个股可能抵抗大盘）
        - 大盘偏多 + 板块走强 = 正常/激进

        返回 dict:
            direction: 市场方向
            urgency: 紧急程度
            tp_ceil_factor: 止盈天花板乘数（<1 = 下调目标，默认 1.0）
            sl_tighten: 止损收紧比例（>1 = 收紧，默认 1.0）
            buy_zone_shift: 买入区下移比例（默认 0 = 不动）
            reason: 调整理由（中文）
        """
        outlook = getattr(self, "_scenario_prev_outlook", None)
        if outlook is None:
            return {
                "direction": "neutral",
                "urgency": "none",
                "tp_ceil_factor": 1.0,
                "sl_tighten": 1.0,
                "buy_zone_shift": 0.0,
                "reason": "",
            }

        primary = outlook.primary
        direction = primary.direction
        urgency = outlook.urgency
        prob = primary.probability

        # ━━ 基础因子：从情景引擎 ━━
        if direction == "bearish":
            if urgency == "critical":
                tp_ceil = 0.85  # 止盈目标打 85 折
                sl_tighten = 1.30  # 止损收紧 30%
                buy_shift = 0.08  # 买入区下移 8%
            elif urgency == "act":
                tp_ceil = 0.90
                sl_tighten = 1.20
                buy_shift = 0.05
            elif urgency == "watch":
                tp_ceil = 0.94
                sl_tighten = 1.10
                buy_shift = 0.02
            else:
                tp_ceil = 1.0
                sl_tighten = 1.0
                buy_shift = 0.0
        elif direction == "bullish":
            if primary.name == "accelerating_up" and urgency in ("critical", "act"):
                # 加速冲顶 → 追高风险，反而要保守
                tp_ceil = 0.88
                sl_tighten = 1.25
                buy_shift = 0.0
            else:
                tp_ceil = 1.0
                sl_tighten = 1.0
                buy_shift = 0.0
        else:
            tp_ceil = 1.0
            sl_tighten = 1.0
            buy_shift = 0.0

        # ━━ 板块联动修正 ━━
        sector_amplify = 1.0
        sector_reason = ""

        is_sector_accel_down = "持续走弱" in sector_trend and "加速" in sector_trend
        is_sector_weak = "持续走弱" in sector_trend or "走弱" in sector_trend
        is_sector_strong = "持续走强" in sector_trend or "走强" in sector_trend
        is_sector_accel_up = "持续走强" in sector_trend and "加速" in sector_trend

        if direction == "bearish":
            if is_sector_accel_down:
                # 大盘偏空 + 板块加速走弱 = 共振放大
                sector_amplify = 1.40
                sector_reason = "板块加速走弱，与大盘共振"
            elif is_sector_weak:
                sector_amplify = 1.20
                sector_reason = "板块走弱，叠加市场偏空"
            elif is_sector_strong:
                # 大盘偏空但板块走强 → 减弱大盘影响
                sector_amplify = 0.60
                sector_reason = "板块走强，部分抵消大盘偏空"
            elif is_sector_accel_up:
                sector_amplify = 0.40
                sector_reason = "板块持续走强，抵抗大盘下跌"

        elif direction == "bullish":
            if is_sector_strong:
                sector_amplify = 0.0  # 无额外调整
                sector_reason = "板块走强，顺应大盘"
            elif is_sector_weak:
                # 大盘涨但板块弱 → 个股天花板降低
                sector_amplify = 0.70
                sector_reason = "板块走弱，拖累个股上行空间"

        elif direction == "neutral":
            if is_sector_accel_down:
                sector_amplify = 0.80
                sector_reason = "板块加速走弱"
            elif is_sector_weak:
                sector_amplify = 0.50
                sector_reason = "板块走弱"

        # ━━ 应用板块放大/缩小 ━━
        if sector_amplify > 0 and tp_ceil < 1.0:
            # 板块放大调整：把折扣加深
            delta = (1.0 - tp_ceil) * sector_amplify
            tp_ceil = max(0.70, 1.0 - delta)
        if sector_amplify > 0 and sl_tighten > 1.0:
            delta = (sl_tighten - 1.0) * sector_amplify
            sl_tighten = min(1.50, 1.0 + delta)
        if sector_amplify > 0 and buy_shift > 0:
            buy_shift = min(0.15, buy_shift * sector_amplify)

        # ━━ 组装 ━━
        parts = []
        if prob >= 0.35:
            parts.append(f"市场预判: {primary.label}({prob:.0%})")
        if sector_reason:
            parts.append(sector_reason)

        return {
            "direction": direction,
            "urgency": urgency,
            "tp_ceil_factor": tp_ceil,
            "sl_tighten": sl_tighten,
            "buy_zone_shift": buy_shift,
            "reason": " | ".join(parts),
        }

    # ━━━━━━━━ _check_market_state（重构）━━━━━━━━━

    def _check_market_state(self, prices: dict[str, float]) -> MarketRegime:
        """检测上证指数，返回 MarketRegime（替代原来的 bool market_ok）。

        调用方从 `market_ok` bool 升级到完整的四层决策对象。
        """
        idx = self._get_index_quote()
        if idx is None:
            return MarketRegime(
                pattern="unknown",
                confidence="low",
                allow_buy=False,
                position_mult=0.0,
                entry_rule="none",
                risk_level="dangerous",
            )

        index_price = idx["price"]

        # 更新日内高低点（价格序列已由 _handle_collector_index 追加，这里不重复）
        if self._index_high == 0 or index_price > self._index_high:
            self._index_high = index_price
        if self._index_low == 0 or index_price < self._index_low:
            self._index_low = index_price

        # 追踪成交额（累计值每次不同，两处追加不影响正确性）

        # —— 情景预测引擎（先于模式分类，提供前瞻性判断）——
        if not hasattr(self, "_scenario_probs"):
            self._init_scenario_state()
        micro = self._detect_micro_signals()
        outlook = self._update_scenario_engine(micro)

        # 多指数背离：上证横盘但小盘指数大跌 → 市场实际偏弱
        divergence_risk = self._check_index_divergence()

        prev_close = idx["pre_close"]
        change_pct = idx["change_pct"]
        if prev_close <= 0:
            return MarketRegime(
                pattern="unknown",
                confidence="low",
                allow_buy=False,
                position_mult=0.0,
                entry_rule="none",
                risk_level="dangerous",
            )

        _, _, ma20 = self._get_index_baseline()
        ma60 = self._get_index_ma60() if hasattr(self, "_get_index_ma60") else 0

        # —— 跳空检测（首轮扫描） ——
        if len(self._index_prices) == 1:
            gap_pct = (index_price - prev_close) / prev_close
            if gap_pct <= -0.015:
                self._alert(
                    f"⚠️ 跳空低开  {gap_pct:.1%}\n"
                    f"   上证开盘: {index_price:.2f}  昨收: {prev_close:.2f}\n"
                    f"   → 注意系统性风险，开盘保持观望"
                )
            elif gap_pct >= 0.02:
                self._alert(
                    f"📈 跳空高开  {gap_pct:.1%}\n"
                    f"   上证开盘: {index_price:.2f}  昨收: {prev_close:.2f}\n"
                    f"   → 关注高开低走风险，不宜追高"
                )

        # —— 熔断 ——
        if change_pct < INDEX_HALT_PCT:
            self._alert(f"🚨 大盘熔断\n   上证跌幅: {change_pct:.1%}  暂停所有买入信号")
            return MarketRegime(
                pattern="halt",
                risk_level="extreme",
                allow_buy=False,
                position_mult=0.0,
                entry_rule="none",
                urgent_action="reduce_positions",
                alert_level="critical",
                confidence="high",
            )

        # —— 模式识别 + 评估 ——
        raw_pattern = self._classify_market_pattern()
        # 多指数背离：实际偏弱时修正 pattern 为更保守
        if divergence_risk and raw_pattern == "normal":
            breadth = self._market_breadth
            if breadth.get("up", 0) < breadth.get("down", 0):
                raw_pattern = "one_sided"  # 一个方向失衡

        # —— regime 确认延迟：新 pattern 需连续 N 轮一致才切换 ——
        pattern = self._apply_regime_confirmation(raw_pattern)

        # —— regime 抖动告警：短期切换过频时记录 ——
        self._check_regime_jitter(pattern)

        regime = self._assess_regime(
            pattern,
            index_price,
            prev_close,
            change_pct,
            ma20=ma20,
            ma60=ma60,
            outlook=outlook,
        )

        # —— 推送告警 ——
        self._push_regime_alert(regime, pattern, index_price, change_pct, prev_close)

        # —— 情景预判告警（前瞻性关卡和概率变化）——
        self._push_scenario_alert(outlook)

        # —— 涨跌比两极分化追加检测 ——
        if regime.allow_buy and regime.breadth_healthy:
            breadth = self._compute_breadth()
            if breadth:
                up, down = breadth.get("up", 0), breadth.get("down", 0)
                total = up + down
                if total > 0 and down / total > 0.75:
                    if not self._index_alerted_downtrend:
                        self._index_alerted_downtrend = True
                        self._alert(
                            f"⚠️ 两极分化\n"
                            f"   上证: {index_price:.2f}  {change_pct:+.2%}  下跌家数: {down}/{total} ({down / total:.0%})\n"
                            f"   → 指数平稳但多数个股下跌，暂停买入"
                        )
                    regime.allow_buy = False
                    regime.position_mult = 0.0
                    regime.entry_rule = "none"
                    regime.risk_level = "dangerous"
                elif down / total <= 0.55 and self._index_alerted_downtrend:
                    self._index_alerted_downtrend = False

        # —— 传统阈值补充（MA20+跌幅） ——
        if index_price < ma20 and change_pct < INDEX_DANGER_PCT:
            self._alert(
                f"⚠️ 大盘偏弱\n"
                f"   上证: {index_price:.2f}  跌破 MA20: {ma20:.2f}  跌幅: {change_pct:.1%}\n"
                f"   → 暂停买入"
            )
            regime.allow_buy = False
            regime.position_mult = 0.0
            regime.entry_rule = "none"
            regime.risk_level = "dangerous"

        # —— 单边下跌结构检测 ——
        if self._is_index_downtrend():
            if not self._index_alerted_downtrend:
                self._index_alerted_downtrend = True
                self._alert(
                    f"⚠️ 单边下跌\n"
                    f"   上证: {index_price:.2f}  日内高: {self._index_high:.2f}  日内低: {self._index_low:.2f}  重心持续下移\n"
                    f"   → 暂停买入，等待止跌信号"
                )
            regime.allow_buy = False
            regime.position_mult = 0.0
            regime.entry_rule = "none"

        # —— 波动预警 ——
        if len(self._index_prices) >= 4:
            price_3_ago = self._index_prices[-4]
            fluctuation = (index_price - price_3_ago) / price_3_ago
            if abs(fluctuation) >= 0.005:
                last = self._index_last_fluctuation_price
                if last == 0 or abs((index_price - last) / last) >= 0.003:
                    self._index_last_fluctuation_price = index_price
                    direction = "急拉" if fluctuation > 0 else "急跌"
                    base_msg = f"⚡ 上证: {index_price:.2f}  盘中{direction}: {fluctuation:+.2%}"
                    # 规则建议立即推送，AI 分析异步处理（不阻塞扫描）
                    advice = (
                        "急拉追高风险大，等回落确认再考虑；急跌关注企稳信号"
                        if direction == "急拉"
                        else "急跌关注是否加速赶底，等缩量止跌再考虑低吸"
                    )
                    self._alert(f"{base_msg}\n   → {advice}")
                    self._submit_index_fluctuation_ai()

        # —— 量价背离 ——
        self._check_volume_divergence(index_price)

        # —— 决策日志（pattern 变更时写入） ——
        try:
            prev = getattr(self, "_last_logged_pattern", "")
            if pattern != prev and self._scan_count > 0:
                top3 = sorted(
                    [
                        (k, v[-1])
                        for k, v in self._sector_trend_history.items()
                        if len(v) >= 3
                    ],
                    key=lambda x: -x[1],
                )[:3]
                bottom3 = sorted(
                    [
                        (k, v[-1])
                        for k, v in self._sector_trend_history.items()
                        if len(v) >= 3
                    ],
                    key=lambda x: x[1],
                )[:3]
                self._log_regime_change(
                    pattern=pattern,
                    confidence=getattr(regime, "confidence", "medium"),
                    prev_pattern=prev,
                    index_price=index_price,
                    index_change=change_pct,
                    up_count=sum(
                        1
                        for s in self._market_snapshot.values()
                        if float(s.get("changePct", 0)) > 0
                    ),
                    down_count=sum(
                        1
                        for s in self._market_snapshot.values()
                        if float(s.get("changePct", 0)) < 0
                    ),
                    top_sectors=[[n, round(v, 2)] for n, v in top3],
                    worst_sectors=[[n, round(v, 2)] for n, v in bottom3],
                )
                self._last_logged_pattern = pattern
        except Exception:
            pass

        return regime

    def _apply_regime_confirmation(self, raw_pattern: str) -> str:
        """新 pattern 需连续 REGIME_STABLE_SCANS 轮一致才确认切换。

        返回确认后的 pattern（可能仍是旧 pattern）。
        """
        from system.config import settings

        current = getattr(self, "_regime", None)
        current_pattern = current.pattern if current else "normal"

        if raw_pattern == current_pattern:
            # 一致：清零待确认状态
            self._regime_pending_pattern = ""
            self._regime_confirm_count = 0
            return current_pattern

        # 不一致：检查是否与待确认的 pattern 相同
        pending = getattr(self, "_regime_pending_pattern", "")
        count = getattr(self, "_regime_confirm_count", 0)

        if raw_pattern == pending:
            count += 1
        else:
            pending = raw_pattern
            count = 1

        self._regime_pending_pattern = pending
        self._regime_confirm_count = count

        if count >= getattr(settings, "REGIME_STABLE_SCANS", 5):
            self._regime_pending_pattern = ""
            self._regime_confirm_count = 0
            logger.info(
                f"regime 确认切换: {current_pattern} → {raw_pattern} "
                f"(经 {count} 轮确认)"
            )
            return raw_pattern

        return current_pattern

    def _check_regime_jitter(self, pattern: str):
        """5 分钟内 regime 切换超过 REGIME_JITTER_MAX 次时记录告警。"""
        from system.config import settings

        current_pattern = (
            getattr(self._regime, "pattern", "") if hasattr(self, "_regime") else ""
        )

        if pattern == current_pattern:
            return

        # 记录切换时间点
        times = getattr(self, "_regime_switch_times", [])
        times.append((self._scan_count, current_pattern, pattern))
        # 只保留最近 REGIME_JITTER_WINDOW 轮内的记录
        window = getattr(settings, "REGIME_JITTER_WINDOW", 5)
        times = [(s, f, t) for s, f, t in times if self._scan_count - s <= window]
        self._regime_switch_times = times

        max_switches = getattr(settings, "REGIME_JITTER_MAX", 3)
        if len(times) > max_switches:
            logger.warning(
                f"regime 抖动: {len(times)} 次切换 / {window} 轮 "
                f"最近: {' → '.join(f'{f}→{t}' for _, f, t in times[-4:])}"
            )

    def _push_regime_alert(
        self,
        regime: MarketRegime,
        pattern: str,
        index_price: float,
        change_pct: float,
        prev_close: float,
    ):
        """按 regime 的告警级别推送消息，避免重复推送。"""
        if not regime.alert_msg:
            return

        # 安全/单边上涨不告警
        if pattern in ("normal", "uptrend"):
            return

        # 反转信号（V/W底）同 pattern 间隔 30 轮
        if pattern in ("v_reversal", "w_bottom", "gap_down_recover"):
            last_scan = getattr(self, "_pattern_last_alert", {}).get(pattern, 0)
            if self._scan_count - last_scan >= 30:
                if not hasattr(self, "_pattern_last_alert"):
                    self._pattern_last_alert = {}
                self._pattern_last_alert[pattern] = self._scan_count
                self._alert(regime.alert_msg)
            return

        # 所有告警类：同 pattern 至少间隔 50 轮才重发（形态可能因数据抖动反复切换）
        last_scan = getattr(self, "_pattern_last_alert", {}).get(pattern, 0)
        if self._scan_count - last_scan >= 50:
            if not hasattr(self, "_pattern_last_alert"):
                self._pattern_last_alert = {}
            self._pattern_last_alert[pattern] = self._scan_count
            self._alert(regime.alert_msg)

    # ━━━━━━━━ 量价背离 ━━━━━━━━

    def _check_volume_divergence(self, current_price: float):
        """检测量价背离：价升量缩=诱多，价跌量增=恐慌放量。"""
        prices = self._index_prices
        volumes = self._market_turnovers
        if len(prices) < 12 or len(volumes) < 12:
            return

        n = 12
        recent_prices = prices[-n:]
        recent_volumes = volumes[-n:]

        price_change = (recent_prices[-1] - recent_prices[0]) / recent_prices[0]

        increments = []
        for i in range(1, len(recent_volumes)):
            inc = recent_volumes[i] - recent_volumes[i - 1]
            if inc > 0:
                increments.append(inc)
        if len(increments) < 6:
            return

        half = len(increments) // 2
        vol_start = sum(increments[:half]) / half
        vol_end = sum(increments[-half:]) / half
        vol_change = (vol_end - vol_start) / vol_start if vol_start > 0 else 0

        if abs(price_change) < 0.003 or abs(vol_change) < 0.15:
            self._volume_alerted_divergence = False
            return

        minutes = max(1, (n - 1) * 5 // 60)

        if price_change > 0 and vol_change < -0.15:
            if not self._volume_alerted_divergence:
                self._volume_alerted_divergence = True
                self._alert(
                    f"⚠️ 量价背离 · 诱多\n"
                    f"   上证 {minutes}分钟  涨: {price_change * 100:.1f}%  成交额: {vol_change * 100:.0f}%\n"
                    f"   → 上涨缺量，谨防诱多"
                )

        elif price_change < 0 and vol_change > 0.15:
            if not self._volume_alerted_divergence:
                self._volume_alerted_divergence = True
                self._alert(
                    f"⚠️ 量价背离 · 恐慌\n"
                    f"   上证 {minutes}分钟  跌: {abs(price_change) * 100:.1f}%  成交额: +{vol_change * 100:.0f}%\n"
                    f"   → 恐慌盘涌出，关注是否加速赶底"
                )

    # ━━━━━━━━ 单边下跌检测 ━━━━━━━━

    def _is_index_downtrend(self) -> bool:
        """结构性判断单边下跌。"""
        prices = self._index_prices
        if len(prices) < 20:
            return False

        hi = self._index_high
        lo = self._index_low
        if hi <= lo:
            return False

        cur = prices[-1]
        if cur > lo + (hi - lo) / 3:
            self._index_alerted_downtrend = False
            return False

        first_avg = sum(prices[-20:-10]) / 10
        second_avg = sum(prices[-10:]) / 10
        if second_avg >= first_avg:
            self._index_alerted_downtrend = False
            return False

        breadth = self._compute_breadth()
        if breadth:
            up, down = breadth.get("up", 0), breadth.get("down", 0)
            if up > 0 and down <= up * 2:
                return False

        return True

    # ━━━━━━━━ 指数技术指标 ━━━━━━━━

    def _check_index_divergence(self) -> str:
        """多指数背离检测。上证横盘 + 小盘指数大跌 → 实际偏弱，返回风险描述。"""
        index_map = getattr(self, "_index_map", {})
        sh = index_map.get("000001.SH", {})
        cy = index_map.get("399006.SZ", {})
        gz = index_map.get("399303.SZ", {})

        sh_chg = abs(sh.get("change_pct", 0))
        cy_chg = cy.get("change_pct", 0)
        gz_chg = gz.get("change_pct", 0)

        if sh_chg < 0.003 and (cy_chg < -0.01 or gz_chg < -0.01):
            return "上证横盘但小盘股领跌，市场情绪弱，权重护盘掩盖真实抛压"
        if sh_chg < 0.003 and cy_chg > 0.01:
            return "上证横盘但创业板领涨，个股活跃，指数滞后"

        # 所有指数同向大幅下跌
        if sh_chg > 0.01 and cy_chg < -0.01 and gz_chg < -0.01:
            return "全市场共振下跌，风险加剧"

        return ""

    def _check_index_technicals(self):
        """检测指数分钟级技术指标拐点。"""
        prices = self._index_prices
        if len(prices) < 30:
            return

        window = 5
        closes, highs, lows = [], [], []
        for i in range(0, len(prices), window):
            chunk = prices[i : i + window]
            closes.append(chunk[-1])
            highs.append(max(chunk))
            lows.append(min(chunk))
        if len(closes) < 26:
            return

        from analysis.screening.indicators import (
            calc_kdj,
            calc_macd_series,
            calc_rsi,
            detect_divergence,
            detect_macd_cross,
        )

        rsi6 = calc_rsi(closes, 6)
        rsi12 = calc_rsi(closes, 12)
        kdj = calc_kdj(highs, lows, closes)

        macd_series = calc_macd_series(closes)
        macd = {
            "dif": macd_series["dif"][-1],
            "dea": macd_series["dea"][-1],
            "bar": 2 * (macd_series["dif"][-1] - macd_series["dea"][-1]),
        }
        crosses = detect_macd_cross(macd_series["dif"], macd_series["dea"], lookback=5)
        divergences = detect_divergence(closes, macd_series["dif"], lookback=30)

        st = self._index_tech_state
        alerts = []

        recent_cross = crosses[-1] if crosses else None
        if recent_cross:
            cross_type = "golden" if "金叉" in recent_cross["type"] else "death"
            if st["macd_cross"] != cross_type:
                st["macd_cross"] = cross_type
                days = recent_cross["days_ago"]
                label = "金叉" if cross_type == "golden" else "死叉"
                alerts.append(
                    f"MACD{label}({days}根前) DIF={macd['dif']:.2f} DEA={macd['dea']:.2f}"
                )

        for period, val, key in [(6, rsi6, "rsi6_zone"), (12, rsi12, "rsi12_zone")]:
            if val < 20:
                zone = "oversold"
                label = f"RSI{period}超卖({val:.1f})"
            elif val > 80:
                zone = "overbought"
                label = f"RSI{period}超买({val:.1f})"
            else:
                zone = "normal"
            if st[key] != zone and zone != "normal":
                st[key] = zone
                alerts.append(label)
            elif zone == "normal":
                st[key] = "normal"

        if kdj["j"] < 0:
            j_zone = "oversold"
            j_label = f"KDJ J值超卖(K={kdj['k']:.1f} D={kdj['d']:.1f} J={kdj['j']:.1f})"
        elif kdj["j"] > 100:
            j_zone = "overbought"
            j_label = f"KDJ J值超买(K={kdj['k']:.1f} D={kdj['d']:.1f} J={kdj['j']:.1f})"
        else:
            j_zone = "normal"
        if st["kdj_j_zone"] != j_zone and j_zone != "normal":
            st["kdj_j_zone"] = j_zone
            alerts.append(j_label)
        elif j_zone == "normal":
            st["kdj_j_zone"] = "normal"

        if len(closes) >= 2:
            k_now, d_now = kdj["k"], kdj["d"]
            if k_now > d_now:
                kd_cross = "golden"
            elif k_now < d_now:
                kd_cross = "death"
            else:
                kd_cross = None
            if kd_cross and st["kdj_cross"] != kd_cross:
                st["kdj_cross"] = kd_cross
                label = "KDJ金叉" if kd_cross == "golden" else "KDJ死叉"
                alerts.append(
                    f"{label} K={kdj['k']:.1f} D={kdj['d']:.1f} J={kdj['j']:.1f}"
                )

        recent_div = divergences[-1] if divergences else None
        if recent_div:
            div_type = "top" if "顶背离" in recent_div["type"] else "bottom"
            if st["divergence"] != div_type:
                st["divergence"] = div_type
                alerts.append(f"{recent_div['type']}: {recent_div['desc']}")

        if alerts:
            current = prices[-1]
            trend_desc = self._index_trend_desc(
                prices,
                (self._last_index_quote or {}).get("pre_close", 0),
            )
            advice = self._index_tech_advice(alerts, st)
            if not advice:
                return  # 无实质信号，不推送

            # 去重：至少 40 轮才再推（约 40 分钟），同一条建议不重复
            last_idx_scan = getattr(self, "_last_index_alert_scan", 0)
            last_idx_advice = getattr(self, "_last_index_alert_advice", "")
            if self._scan_count - last_idx_scan < 40 and advice == last_idx_advice:
                return
            self._last_index_alert_scan = self._scan_count
            self._last_index_alert_advice = advice

            # 只推送有实质信号的告警（背离、RSI极端、MACD交叉等）
            div_lines = [a for a in alerts if "背离" in a]

            msg = f"📈 上证指数  {current:.2f}  {trend_desc}"
            for a in div_lines:
                msg += f"\n   {a}"
            msg += f"\n   → {advice}"
            self._alert(msg)

    def _index_trend_desc(self, prices: list[float], pre_close: float = 0) -> str:
        if len(prices) < 10:
            return "数据不足"
        # 用最近 ~20 分钟数据判断短线方向，不用全量历史（全量掩盖尾盘跳水）
        window = min(30, len(prices) // 3)
        if window < 5:
            window = len(prices) // 2
        recent = prices[-window:]
        earlier = (
            prices[-2 * window : -window]
            if len(prices) >= 2 * window
            else prices[:window]
        )
        avg_recent = sum(recent) / len(recent)
        avg_earlier = sum(earlier) / len(earlier) if earlier else avg_recent
        # 涨跌幅基准应是昨收而非 prices[0]（开盘跳空会导致偏差）
        base = pre_close if pre_close else prices[0]
        chg = (prices[-1] - base) / base * 100
        if avg_recent > avg_earlier * 1.001:
            direction = "持续上行"
        elif avg_recent < avg_earlier * 0.999:
            direction = "持续下行"
        else:
            direction = "横盘震荡"
        return f"{direction} {chg:+.2f}%"

    def _index_tech_advice(self, alerts: list[str], st: dict) -> str:
        has_div_bottom = any("底背离" in a for a in alerts)
        has_div_top = any("顶背离" in a for a in alerts)
        has_rsi_os = (
            st.get("rsi6_zone") == "oversold" or st.get("rsi12_zone") == "oversold"
        )
        has_rsi_ob = (
            st.get("rsi6_zone") == "overbought" or st.get("rsi12_zone") == "overbought"
        )
        macd_cross = st.get("macd_cross")

        if has_div_bottom:
            return "底背离，下跌动能衰竭，关注反转确认后可小仓位试探"
        if has_div_top:
            return "顶背离，上涨动能衰竭，建议减仓或观望"
        if has_rsi_os and macd_cross == "golden":
            return "超卖+金叉共振，可考虑分批低吸"
        if has_rsi_ob and macd_cross == "death":
            return "超买+死叉共振，建议减仓避险"
        if has_rsi_os:
            return "超卖区，关注企稳信号"
        if has_rsi_ob:
            return "超买区，追高风险大，等回调"
        # MACD/KDJ 单独交叉太常规，不推送，避免刷屏
        return ""

    # ━━━━━━━━ 指数分析 ━━━━━━━━

    def _get_index_baseline(self) -> tuple:
        """获取上证指数 MA5/MA10/MA20（从 index_realtime_data 日线收盘价计算）。"""
        if self._ma_baseline_cache is not None:
            return self._ma_baseline_cache

        try:
            conn = sqlite3.connect(self.db_path)
            # 取每个交易日的收盘价（当日最后一条记录），最近 30 天
            rows = conn.execute(
                """SELECT trade_date, close_price
                   FROM index_realtime_data
                   WHERE index_code='sh000001'
                     AND trade_time IN (
                       SELECT MAX(trade_time)
                       FROM index_realtime_data
                       WHERE index_code='sh000001'
                       GROUP BY trade_date
                     )
                   ORDER BY trade_date DESC
                   LIMIT 30"""
            ).fetchall()
            conn.close()

            if not rows or len(rows) < 3:
                return (0, 0, 0)

            closes = [r[1] for r in reversed(rows)]  # 最早在前

            def _ma(data, n):
                if len(data) >= n:
                    return sum(data[-n:]) / n
                return sum(data) / len(data)

            ma5 = _ma(closes, 5)
            ma10 = _ma(closes, 10)
            ma20 = _ma(closes, 20)

            self._ma_baseline_cache = (ma5, ma10, ma20)
            return self._ma_baseline_cache
        except Exception:
            pass
        return (0, 0, 0)

    def _get_index_ma60(self) -> float:
        """获取上证指数 MA60（从 index_realtime_data 日线收盘价计算）。"""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT trade_date, close_price
                   FROM index_realtime_data
                   WHERE index_code='sh000001'
                     AND trade_time IN (
                       SELECT MAX(trade_time)
                       FROM index_realtime_data
                       WHERE index_code='sh000001'
                       GROUP BY trade_date
                     )
                   ORDER BY trade_date DESC
                   LIMIT 90"""
            ).fetchall()
            conn.close()

            if not rows or len(rows) < 5:
                return 0

            closes = [r[1] for r in rows]
            return sum(closes) / len(closes)
        except Exception:
            return 0

    def _submit_index_fluctuation_ai(self):
        """大盘波动≥0.5%时，异步提交 AI 预判任务（不阻塞扫描）。"""
        prices = self._index_prices
        if len(prices) < 30:
            return

        window = 5
        closes = []
        highs = []
        lows = []
        for i in range(0, len(prices), window):
            chunk = prices[i : i + window]
            closes.append(chunk[-1])
            highs.append(max(chunk))
            lows.append(min(chunk))

        if len(closes) < 26:
            return

        from analysis.screening.indicators import (
            calc_kdj,
            calc_macd,
            calc_macd_series,
            calc_rsi,
            detect_divergence,
            detect_macd_cross,
        )

        macd = calc_macd(closes)
        rsi6 = calc_rsi(closes, 6)
        rsi12 = calc_rsi(closes, 12)
        rsi24 = calc_rsi(closes, 24)
        kdj = calc_kdj(highs, lows, closes)

        macd_series = calc_macd_series(closes)
        crosses = detect_macd_cross(macd_series["dif"], macd_series["dea"])
        divergences = detect_divergence(closes, macd_series["dif"])

        ma5, ma10, ma20 = self._get_index_baseline()

        current = prices[-1]
        idx = self._last_index_quote
        pre_close = idx.get("pre_close", 0) if idx else 0
        if pre_close > 0:
            change_from_first = (current - pre_close) / pre_close * 100
        else:
            change_from_first = (
                (prices[-1] - prices[0]) / prices[0] * 100 if prices[0] else 0
            )

        bar_count = min(10, len(closes))
        recent_bars = []
        for i in range(len(closes) - bar_count + 1, len(closes)):
            chg = (
                (closes[i] - closes[i - 1]) / closes[i - 1] * 100
                if closes[i - 1]
                else 0
            )
            recent_bars.append(f"{closes[i]:.2f}({chg:+.1f}%)")

        ma_parts = []
        for label, ma_val in [("MA5", ma5), ("MA10", ma10), ("MA20", ma20)]:
            if ma_val and ma_val > 0:
                pos = "上方" if current > ma_val else "下方"
                ma_parts.append(
                    f"{label}={ma_val:.0f}({pos}{abs(current - ma_val):.0f})"
                )

        cross_info = (
            ", ".join([f"{c['days_ago']}根前{c['type']}" for c in crosses])
            if crosses
            else "近期无交叉"
        )
        div_info = (
            ", ".join([d["type"] for d in divergences]) if divergences else "无背离"
        )

        prompt = f"""分析上证指数当前走势，预判方向和企稳点位。

## 当前状态
指数现价: {current:.2f}
近{len(prices)}轮(约{len(closes)}分钟)总变动: {change_from_first:+.2f}%
日线均线: {", ".join(ma_parts) if ma_parts else "无数据"}

## 分钟级技术指标
MACD: DIF={macd["dif"]:.2f} DEA={macd["dea"]:.2f} BAR={macd["bar"]:.2f}
RSI(6/12/24): {rsi6:.1f}/{rsi12:.1f}/{rsi24:.1f}
KDJ: K={kdj["k"]:.1f} D={kdj["d"]:.1f} J={kdj["j"]:.1f}
交叉: {cross_info}
背离: {div_info}

## 近{bar_count}分钟走势
{", ".join(recent_bars)}

请分析:
1. 这波急跌/急拉会继续还是会反转?
2. 如果继续，到什么点位可能企稳?
3. 当前应该追/等/减/守?

用中文简洁回复，不超过150字。格式:
方向: [继续下跌/继续上涨/即将反弹/即将回调]
企稳点位: [具体点位或区间]
建议: [追/等/减/守]
理由: [一句话]"""

        aiq = getattr(self, "_ai_queue", None)
        if aiq is None:
            return
        ok = aiq.submit(
            "index_fluctuation",
            prompt,
            system_prompt="你是A股大盘技术分析专家，基于MACD/RSI/KDJ和均线系统做短线预判。简洁、准确、可操作。",
            max_tokens=300,
            dedupe=True,
        )
        if ok:
            self._pending_index_ai["index_fluctuation"] = {
                "change_pct": change_from_first,
                "submitted_at": time.time(),
            }


def _upgrade_risk(current: str) -> str:
    """风险升级：safe→cautious→dangerous→extreme。"""
    order = {"safe": 0, "cautious": 1, "dangerous": 2, "extreme": 3}
    levels = ["safe", "cautious", "dangerous", "extreme"]
    idx = order.get(current, 0)
    return levels[min(idx + 1, 3)]
