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


class SignalType(Enum):
    BUY = auto()
    SELL = auto()
    HOLD = auto()


class SignalSource(Enum):
    RULE = auto()         # 纯量化规则
    AI_ENHANCED = auto()  # AI 精选 + 规则
    RISK = auto()         # 风控触发


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
        }

    def __repr__(self) -> str:
        if self.signal_type == SignalType.BUY:
            return (
                f"BUY  {self.stock_code} {self.stock_name} "
                f"zone={self.buy_zone_min:.2f}-{self.buy_zone_max:.2f} "
                f"pos={self.target_position:.0%} sl={self.stop_loss:.2f}"
            )
        elif self.signal_type == SignalType.SELL:
            return f"SELL {self.stock_code} {self.stock_name} reason={self.sell_reason}"
        return f"HOLD {self.stock_code}"
