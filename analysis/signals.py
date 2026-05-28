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
    tags: list[str] = field(default_factory=list)        # 命中的因子标签
    scenarios: list[str] = field(default_factory=list)    # 命中的场景


class SignalType(Enum):
    BUY = auto()
    SELL = auto()
    HOLD = auto()


class SignalSource(Enum):
    RULE = auto()         # 纯量化规则
    AI_ENHANCED = auto()  # AI 精选 + 规则
    RISK = auto()         # 风控触发


@dataclass
class StockProfile:
    """富化后的个股完整画像 — 给 AI 做精选判断"""

    code: str
    name: str
    trade_date: str

    score: float = 0.0
    trend_mode: str = ""       # "strong"=5日线强趋势 | "normal"=20日线稳健趋势

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
    legacy_note: str = ""      # 昨日遗留推荐的原始理由

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
            f"{'='*50}",
            f"{self.code} {self.name} | {s.get('industry', '')}",
            f"趋势类型: {trend_label} | {bias_str}",
        ]
        if self.legacy_note:
            lines.append(f"⚠️ 昨日遗留推荐 | 原推荐理由: {self.legacy_note}")
        lines.append(f"{'='*50}")
        lines.append(f"")
        lines.append(f"▼ 今日盘面")
        lines.append(f"  收盘: {price:.2f}  涨跌: {s.get('change_pct', 0):+.2f}%  振幅: {s.get('amplitude', 0):.2f}%")
        lines.append(f"  量比: {s.get('volume_ratio', 0):.2f}  换手: {s.get('turnover_rate', 0):.2f}%")
        lines.append(f"  主力净买: {s.get('main_force_net', 0)/10000:.0f}万  占比: {s.get('main_force_ratio', 0):.1f}%")
        lines.append(f"  均线: MA5 {ma5:.2f}  MA10 {ma10:.2f}  MA20 {ma20:.2f}")
        lines.append(f"")
        lines.append(f"▼ 最近10日走势 (日期 开 高 低 收 涨跌 量比 主力/万)")

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
            f"{s.get('volume_ratio', 0):4.2f}  {s.get('main_force_net', 0)/10000:8.0f}"
        )

        lines.append(f"")
        lines.append(f"▼ 趋势特征")
        lines.append(f"  连阳: {h.get('consecutive_yang', 0)}日  多头排列: {h.get('ma_bull_days', 0)}日")
        lines.append(f"  20日最高: {h.get('high_20d', 0):.2f}  20日最低: {h.get('low_20d', 0):.2f}")
        lines.append(f"  5日主力累计: {h.get('mf_5d_cum', 0)/10000:.0f}万  连续流入: {h.get('mf_consec_inflow', 0)}日")

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
            lines.append(f"")
            lines.append(f"▼ 技术指标 (5日趋势)")
            lines.append(f"  MACD(12,26,9): DIF={macd.get('dif', 0):.2f}  DEA={macd.get('dea', 0):.2f}  BAR={macd.get('bar', 0):.2f}")
            if t5:
                lines.append(f"    5日前 → 今日: DIF {t5.get('macd_dif', '')}  BAR {t5.get('macd_bar', '')}")
            lines.append(f"  RSI: 6日={rsi6:.1f}  12日={rsi12:.1f}  24日={rsi24:.1f}")
            if t5:
                lines.append(f"    5日前 → 今日: RSI6 {t5.get('rsi6', '')}  RSI12 {t5.get('rsi12', '')}")
            lines.append(f"  KDJ(9,3,3): K={kdj.get('k', 0):.1f}  D={kdj.get('d', 0):.1f}  J={kdj.get('j', 0):.1f}")
            if t5:
                lines.append(f"    5日前 → 今日: K {t5.get('kdj_k', '')}")
            boll = ind.get("boll", {})
            if boll and boll.get("mid", 0) > 0:
                pct_b = boll.get("pct_b", 50)
                pos_desc = "触及上轨" if pct_b >= 95 else "上轨附近" if pct_b >= 80 else "中轨上方" if pct_b >= 50 else "中轨下方" if pct_b >= 20 else "下轨附近" if pct_b >= 5 else "触及下轨"
                lines.append(f"  布林(20,2): 上{boll['upper']:.2f} 中{boll['mid']:.2f} 下{boll['lower']:.2f} "
                            f"带宽{boll['width']:.1f}% %b={pct_b:.1f}({pos_desc})")
            if patterns:
                lines.append(f"  ⚡ 检测到的形态:")
                for p in patterns:
                    ago = f"{p['days_ago']}天前 " if p.get('days_ago') else ""
                    desc = f" — {p['desc']}" if p.get('desc') else ""
                    lines.append(f"    {ago}{p['type']}{desc}")

        lines.append(f"")
        lines.append(f"▼ 系统标签 (仅供参考)")
        lines.append(f"  场景: {', '.join(self.scenarios) if self.scenarios else '无'}")
        lines.append(f"  因子: {', '.join(self.tags) if self.tags else '无'}")

        if r:
            parts = []
            for k in ("rps_20", "rps_60", "rps_120"):
                if k in r and r[k]:
                    parts.append(f"{k.split('_')[1]}日:{r[k]*100:.0f}%分位")
            if parts:
                lines.append(f"  RPS: {' | '.join(parts)}")

        if self.sectors:
            lines.append(f"")
            lines.append(f"▼ 板块参照")
            for sec in self.sectors[:4]:
                lines.append(f"  {sec.get('name', '')}({sec.get('code', '')}): {sec.get('change_pct', 0):+.2f}%")

        if v and v.get("pe_ttm"):
            lines.append(f"")
            lines.append(f"▼ 估值: PE(TTM){v['pe_ttm']:.1f}  PB{v.get('pb', 0):.1f}  市值{v.get('mcap_yi', 0):.0f}亿")

        # 电报
        if self.telegraphs:
            lines.append(f"▼ 今日相关电报:")
            for t in self.telegraphs[:3]:
                sentiment = t.get("sentiment", "")
                sentiment_icon = {"利好": "+", "利空": "-"}.get(sentiment, "")
                lines.append(f"  [{sentiment_icon}] {t.get('summary', '')[:80]}")

        # 风险警示
        if self.risks:
            lines.append(f"")
            lines.append(f"⚠ 风险警示:")
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
    expected_trend: str = ""   # AI 预期走势描述
    trend_mode: str = ""       # "strong"=5日线强趋势 | "normal"=20日线稳健趋势
    sector_name: str = ""      # 主要板块名称

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
        trend_label = "强趋势" if self.trend_mode == "strong" else "稳健趋势" if self.trend_mode == "normal" else ""
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
