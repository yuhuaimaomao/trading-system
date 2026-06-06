"""交易信号数据模型"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


@dataclass
class StockScore:
    """趋势筛选输出数据模型"""

    stock_code: str
    stock_name: str
    trend_mode: str  # 'strong' | 'normal'
    score: float  # 0-100
    price: float
    change_pct: float
    mcap: float  # 亿
    circ_mcap: float  # 亿
    turnover_rate: float
    volume_ratio: float
    ma5: float
    ma10: float
    ma20: float
    ma5_angle: float
    industry: str
    mf_wan: float  # 主力净流入(万)
    mf_ratio: float  # 主力净流入占比
    bias_ma5: float = 0.0  # 偏离MA5百分比 (仅strong)
    bias_ma20: float = 0.0  # 偏离MA20百分比 (仅normal)
    tags: list[str] = field(default_factory=list)  # 命中的因子标签
    scenarios: list[str] = field(default_factory=list)  # 命中的场景


class SignalType(Enum):
    BUY = auto()
    SELL = auto()
    HOLD = auto()


class SignalSource(Enum):
    RULE = auto()  # 纯量化规则
    AI_ENHANCED = auto()  # AI 精选 + 规则
    RISK = auto()  # 风控触发
    REVIEW = auto()  # 复盘趋势精选


@dataclass
class StockProfile:
    """富化后的个股完整画像 — 给 AI 做精选判断"""

    code: str
    name: str
    trade_date: str

    score: float = 0.0
    trend_mode: str = ""  # "strong"=5日线强趋势 | "normal"=20日线稳健趋势

    scenarios: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    snapshot: dict = field(default_factory=dict)
    history: dict = field(default_factory=dict)
    rps: dict = field(default_factory=dict)
    sectors: list[dict] = field(default_factory=list)
    sector_resonance: dict = field(default_factory=dict)
    valuation: dict = field(default_factory=dict)
    market_state: str = ""
    telegraphs: list[dict] = field(default_factory=list)
    indicators: dict = field(default_factory=dict)
    risks: list[dict] = field(default_factory=list)
    legacy_note: str = ""  # 昨日遗留推荐的原始理由

    def to_text(self) -> str:
        """生成给 AI 的文本画像 — 原始数据为主，标签为辅助"""
        s = self.snapshot
        h = self.history
        r = self.rps
        v = self.valuation
        price = s.get("price", 0)
        ma5, ma10, ma20 = h.get("ma5", 0), h.get("ma10", 0), h.get("ma20", 0)

        # 偏离均线百分比
        bias5 = (price - ma5) / ma5 * 100 if ma5 else 0
        bias10 = (price - ma10) / ma10 * 100 if ma10 else 0
        bias20 = (price - ma20) / ma20 * 100 if ma20 else 0
        bias_str = f"MA5:{bias5:+.2f}%  MA10:{bias10:+.2f}%  MA20:{bias20:+.2f}%"

        trend_label = "5日线强趋势" if self.trend_mode == "strong" else "20日线稳健趋势"

        lines = [
            f"{'=' * 50}",
            f"{self.code} {self.name} | {s.get('industry', '')}",
            f"趋势类型: {trend_label} | {bias_str}",
        ]
        if self.legacy_note:
            lines.append(f"⚠️ 昨日遗留推荐 | 原推荐理由: {self.legacy_note}")
        lines.append(f"{'=' * 50}")
        lines.append("")
        lines.append("▼ 今日盘面")
        lines.append(
            f"  收盘: {price:.2f}  涨跌: {s.get('change_pct', 0):+.2f}%  振幅: {s.get('amplitude', 0):.2f}%"
        )
        lines.append(
            f"  量比: {s.get('volume_ratio', 0):.2f}  换手: {s.get('turnover_rate', 0):.2f}%"
        )
        lines.append(
            f"  主力净买: {s.get('main_force_net', 0) / 10000:.0f}万  占比: {s.get('main_force_ratio', 0):.1f}%"
        )
        lines.append(f"  均线: MA5 {ma5:.2f}  MA10 {ma10:.2f}  MA20 {ma20:.2f}")
        lines.append("")
        lines.append("▼ 最近10日走势 (日期 开 高 低 收 涨跌 量比 主力/万)")

        daily = h.get("daily", [])
        if daily:
            for d in daily:
                lines.append(
                    f"  {d['date']}  {d['open']:7.2f}  {d['high']:7.2f}  {d['low']:7.2f}  "
                    f"{d['close']:7.2f}  {d['chg']:+6.2f}%  {d['vol_ratio']:4.2f}  {d['mf_net']:8.0f}"
                )
        else:
            lines.append("  (无历史数据)")

        # 今日加一行，标记 ★
        lines.append(
            f"★ {self.trade_date[-5:]}  {s.get('open', 0):7.2f}  {s.get('high', 0):7.2f}  "
            f"{s.get('low', 0):7.2f}  {price:7.2f}  {s.get('change_pct', 0):+6.2f}%  "
            f"{s.get('volume_ratio', 0):4.2f}  {s.get('main_force_net', 0) / 10000:8.0f}"
        )

        lines.append("")
        lines.append("▼ 趋势特征")
        lines.append(
            f"  连阳: {h.get('consecutive_yang', 0)}日  多头排列: {h.get('ma_bull_days', 0)}日"
        )
        lines.append(
            f"  20日最高: {h.get('high_20d', 0):.2f}  20日最低: {h.get('low_20d', 0):.2f}"
        )
        lines.append(
            f"  5日主力累计: {h.get('mf_5d_cum', 0) / 10000:.0f}万  连续流入: {h.get('mf_consec_inflow', 0)}日"
        )

        # 技术指标
        ind = self.indicators
        if ind:
            macd = ind.get("macd", {})
            rsi6 = ind.get("rsi6", 0)
            rsi12 = ind.get("rsi12", 0)
            rsi24 = ind.get("rsi24", 0)
            kdj = ind.get("kdj", {})
            t5 = ind.get("trend_5d", {})
            patterns = ind.get("patterns", [])
            lines.append("")
            lines.append("▼ 技术指标 (5日趋势)")
            lines.append(
                f"  MACD(12,26,9): DIF={macd.get('dif', 0):.2f}  DEA={macd.get('dea', 0):.2f}  BAR={macd.get('bar', 0):.2f}"
            )
            if t5:
                lines.append(
                    f"    5日前 → 今日: DIF {t5.get('macd_dif', '')}  BAR {t5.get('macd_bar', '')}"
                )
            lines.append(f"  RSI: 6日={rsi6:.1f}  12日={rsi12:.1f}  24日={rsi24:.1f}")
            if t5:
                lines.append(
                    f"    5日前 → 今日: RSI6 {t5.get('rsi6', '')}  RSI12 {t5.get('rsi12', '')}"
                )
            lines.append(
                f"  KDJ(9,3,3): K={kdj.get('k', 0):.1f}  D={kdj.get('d', 0):.1f}  J={kdj.get('j', 0):.1f}"
            )
            if t5:
                lines.append(f"    5日前 → 今日: K {t5.get('kdj_k', '')}")
            boll = ind.get("boll", {})
            if boll and boll.get("mid", 0) > 0:
                pct_b = boll.get("pct_b", 50)
                pos_desc = (
                    "触及上轨"
                    if pct_b >= 95
                    else "上轨附近"
                    if pct_b >= 80
                    else "中轨上方"
                    if pct_b >= 50
                    else "中轨下方"
                    if pct_b >= 20
                    else "下轨附近"
                    if pct_b >= 5
                    else "触及下轨"
                )
                lines.append(
                    f"  布林(20,2): 上{boll['upper']:.2f} 中{boll['mid']:.2f} 下{boll['lower']:.2f} "
                    f"带宽{boll['width']:.1f}% %b={pct_b:.1f}({pos_desc})"
                )
            if patterns:
                lines.append("  ⚡ 检测到的形态:")
                for p in patterns:
                    ago = f"{p['days_ago']}天前 " if p.get("days_ago") else ""
                    desc = f" — {p['desc']}" if p.get("desc") else ""
                    lines.append(f"    {ago}{p['type']}{desc}")

        lines.append("")
        lines.append("▼ 系统标签 (仅供参考)")
        lines.append(f"  场景: {', '.join(self.scenarios) if self.scenarios else '无'}")
        lines.append(f"  因子: {', '.join(self.tags) if self.tags else '无'}")

        if r:
            parts = []
            for k in ("rps_20", "rps_60", "rps_120"):
                if k in r and r[k]:
                    parts.append(f"{k.split('_')[1]}日:{r[k] * 100:.0f}%分位")
            if parts:
                lines.append(f"  RPS: {' | '.join(parts)}")

        if self.sectors:
            lines.append("")
            lines.append("▼ 板块参照")
            for sec in self.sectors[:4]:
                lines.append(
                    f"  {sec.get('name', '')}({sec.get('code', '')}): {sec.get('change_pct', 0):+.2f}%"
                )

        if v and v.get("pe_ttm"):
            lines.append("")
            lines.append(
                f"▼ 估值: PE(TTM){v['pe_ttm']:.1f}  PB{v.get('pb', 0):.1f}  市值{v.get('mcap_yi', 0):.0f}亿"
            )

        # 电报
        if self.telegraphs:
            lines.append("▼ 今日相关电报:")
            for t in self.telegraphs[:3]:
                sentiment = t.get("sentiment", "")
                sentiment_icon = {"利好": "+", "利空": "-"}.get(sentiment, "")
                lines.append(f"  [{sentiment_icon}] {t.get('summary', '')[:80]}")

        # 风险警示
        if self.risks:
            lines.append("")
            lines.append("⚠ 风险警示:")
            for r in self.risks[:5]:
                lines.append(f"  [{r.get('type', '')}] {r.get('title', '')}")
                if r.get("risk_type"):
                    lines.append(f"    分类: {r['risk_type']}")

        return "\n".join(lines)


@dataclass
class HoldingInfo:
    """持仓信息 — 盘前注入 AI prompt"""

    stock_code: str
    stock_name: str
    account: str  # 'paper' | 'real'
    entry_date: str
    holding_days: int
    avg_cost: float
    volume: int
    current_price: float
    pnl_pct: float
    market_value: float
    stop_loss: float
    take_profit: float
    industry: str = ""
    ma5: float = 0
    ma10: float = 0
    ma20: float = 0
    highest_price: float = 0
    signal_score: float = 0
    is_today_buy: bool = False  # T+1 锁定期
    profile: Optional["StockProfile"] = None  # 完整画像（含技术指标、板块、电报等）


@dataclass
class AccountSummary:
    """账户概况"""

    account: str
    label: str  # '实盘' | '模拟盘'
    initial_capital: float
    total_value: float
    cash: float
    market_value: float
    position_ratio: float
    daily_pnl: float
    position_count: int


@dataclass
class ReviewContext:
    """复盘报告提取的上下文 — 注入策略管线 AI prompt"""

    trade_date: str = ""
    # 三、市场情绪周期
    sentiment_cycle: str = ""
    # 四、核心主线与资金暗流
    main_lines: str = ""  # 绝对主线
    sub_lines: str = ""  # 次线/轮动
    retreating_sectors: str = ""  # 退潮方向
    # 五、明日大局推演
    outlook: str = ""
    # 七、趋势交易者精选 codes + 结构化数据
    review_picks: list[str] = field(default_factory=list)
    review_stocks_raw: list = field(
        default_factory=list
    )  # STOCKS JSON 中趋势票的原始数据
    # 八、早盘监控雷达
    monitor_conditions: str = ""
    # 十、仓位与策略
    suggested_position: float = 0.0
    position_cap: float = 0.0
    main_attack: str = ""
    avoid_direction: str = ""

    def to_text(self) -> str:
        """格式化给 AI 的上下文文本"""
        parts = []
        if self.sentiment_cycle:
            parts.append(f"【复盘·市场情绪周期】\n{self.sentiment_cycle}")
        if self.main_lines:
            parts.append(f"【复盘·绝对主线】\n{self.main_lines}")
        if self.sub_lines:
            parts.append(f"【复盘·次线/轮动】\n{self.sub_lines}")
        if self.retreating_sectors:
            parts.append(f"【复盘·退潮方向】\n{self.retreating_sectors}")
        if self.outlook:
            parts.append(f"【复盘·明日大局推演】\n{self.outlook}")
        if self.review_picks:
            parts.append(
                f"【复盘·趋势精选参考】\n  复盘精选股票: {', '.join(self.review_picks)}\n  这些股票如出现在今日候选池中，可给予额外加分（趋势延续性好），但不强制推荐。"
            )
        if self.monitor_conditions:
            parts.append(f"【复盘·早盘监控条件】\n{self.monitor_conditions}")
        if self.main_attack or self.avoid_direction:
            pos_parts = []
            if self.suggested_position > 0:
                pos_parts.append(f"建议仓位: {self.suggested_position:.0%}")
            if self.position_cap > 0:
                pos_parts.append(f"仓位上限: {self.position_cap:.0%}")
            if self.main_attack:
                pos_parts.append(f"主攻方向: {self.main_attack}")
            if self.avoid_direction:
                pos_parts.append(f"回避方向: {self.avoid_direction}")
            parts.append(f"【复盘·仓位与策略】\n  {' | '.join(pos_parts)}")
        return "\n\n".join(parts) if parts else ""


@dataclass
class HoldingReview:
    """AI 持仓审查建议"""

    stock_code: str
    account: str = ""
    action: str = ""  # hold | add | reduce | close
    new_stop_loss: Optional[float] = None
    new_take_profit: Optional[float] = None
    expected_holding_days: Optional[int] = None
    tomorrow_outlook: str = ""
    reason: str = ""

    def to_summary(self) -> str:
        action_icon = {
            "add": "➕加仓",
            "reduce": "➖减仓",
            "close": "🔴清仓",
            "hold": "🟢持有",
        }
        icon = action_icon.get(self.action, "❓")
        parts = [f"{icon} {self.stock_code}"]
        if self.new_stop_loss:
            parts.append(f"止损→{self.new_stop_loss:.2f}")
        if self.new_take_profit:
            parts.append(f"止盈→{self.new_take_profit:.2f}")
        if self.expected_holding_days:
            parts.append(f"预计{self.expected_holding_days}天")
        if self.tomorrow_outlook:
            parts.append(self.tomorrow_outlook)
        if self.reason:
            parts.append(self.reason)
        return " | ".join(parts)


@dataclass
class OrderSignal:
    stock_code: str
    stock_name: str
    signal_type: SignalType
    source: SignalSource
    timestamp: str = ""

    # 买入
    buy_zone_min: Optional[float] = None
    buy_zone_max: Optional[float] = None
    target_position: Optional[float] = None

    # 卖出
    sell_reason: str = ""

    # 风控
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop: Optional[float] = None

    # 元数据
    strategy_name: str = ""
    signal_score: float = 0.0
    reason: str = ""
    expected_trend: str = ""  # AI 预期走势描述
    trend_mode: str = ""  # "strong"=5日线强趋势 | "normal"=20日线稳健趋势
    sector_name: str = ""  # 主要板块名称

    def to_dict(self) -> dict:
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "signal_type": self.signal_type.name,
            "source": self.source.name,
            "buy_zone_min": self.buy_zone_min,
            "buy_zone_max": self.buy_zone_max,
            "target_position": self.target_position,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "trailing_stop": self.trailing_stop,
            "signal_score": self.signal_score,
            "strategy_name": self.strategy_name,
            "reason": self.reason,
            "expected_trend": self.expected_trend,
            "trend_mode": self.trend_mode,
            "sector_name": self.sector_name,
        }

    def __repr__(self) -> str:
        trend_label = (
            "强趋势"
            if self.trend_mode == "strong"
            else "稳健趋势"
            if self.trend_mode == "normal"
            else ""
        )
        sector_str = f" [{self.sector_name}]" if self.sector_name else ""
        if self.signal_type == SignalType.BUY:
            return (
                f"BUY  {self.stock_code} {self.stock_name}{sector_str} "
                f"zone={self.buy_zone_min:.2f}-{self.buy_zone_max:.2f} "
                f"pos={self.target_position:.0%} sl={self.stop_loss:.2f} "
                f"({trend_label})"
            )
        elif self.signal_type == SignalType.SELL:
            return f"SELL {self.stock_code} {self.stock_name} reason={self.sell_reason}"
        return f"HOLD {self.stock_code}"


@dataclass
class StrategyAiDecision:
    """AI 对单只股票的完整决策（buy 和 skip 都必须有）"""

    stock_code: str
    stock_name: str
    rank_in_prompt: int
    verdict: str  # "buy" | "skip"
    confidence: str = ""  # "high" | "medium" | "low"（仅 buy）

    # 推理块
    what_i_see: str = ""
    what_concerns_me: str = ""
    decisive_factor: str = ""

    # skip 特有
    skip_reason: str = ""
    would_reconsider_if: str = ""

    # buy 特有
    buy_zone_min: Optional[float] = None
    buy_zone_max: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    pricing_logic: str = ""

    # 技术回填
    signal_id: Optional[int] = None
    day_change_pct: Optional[float] = None
    day_pnl_pct: Optional[float] = None


@dataclass
class StrategyAiResult:
    """AI 分析完整结果（一次 AI 调用）"""

    model_used: str
    decisions: list[StrategyAiDecision] = field(default_factory=list)
    holdings_review: list[HoldingReview] = field(default_factory=list)
    self_assessment: str = ""
    raw_response: str = ""
