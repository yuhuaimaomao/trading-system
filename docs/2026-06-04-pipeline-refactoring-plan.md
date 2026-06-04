# 盯盘管线架构重构方案

日期: 2026-06-04

## 动机

盯盘管线（Watcher Mixins）当前存在三个结构性问题：

1. **重复计算** — `buy_decision.py` 和 `position_risk.py` 各自内联了 RSI/MACD/KDJ/布林/均线/主力资金的计算和判断，同一段逻辑写了 2-3 遍
2. **无法独立测试** — 所有 Mixin 都绑在 Watcher 上，依赖 `self.qmt`、`self.db_path`、`self._industry_cache` 等实例属性，计算逻辑抽不出来
3. **个股分析引擎无法复用** — `analysis/stock/` 需要的计算能力（技术指标/资金流向/板块归因），已经在 Mixin 里写过了，但嵌在 `self.xxx` 里完全无法复用

重构目标：**把计算层从决策层分离出来，形成可独立使用、可测试的组件**。然后个股分析引擎（`analysis/stock/`）直接复用这些组件。

## 现状：计算 vs 决策 分布

| 计算（数据→指标） | 当前位置 | 决策（指标→判断） | 当前位置 |
|---|---|---|---|
| 分钟级 RSI/MACD/KDJ | `buy_decision._get_intraday_indicators` (233行) | RSI>80 拒绝买入 | `buy_decision._evaluate_buy_decision` |
| 日线布林带/均线/BBI | `buy_decision._analyze_buy_context` (231行) | 布林上轨超买拒绝 | `buy_decision._evaluate_buy_decision` |
| 主力资金流向 | `buy_decision._get_context_factors` (131行) | 主力流出>5%拒绝 | `buy_decision._evaluate_buy_decision` |
| 盘口买卖力量 | `buy_decision._get_order_book_imbalance` (35行) | 卖盘沉重拒绝 | `buy_decision._evaluate_buy_decision` |
| 大单流向 | `buy_decision._get_big_order_direction` (44行) | 大单卖出主导拒绝 | `buy_decision._evaluate_buy_decision` |
| 同上 RSI/MACD/KDJ | `position_risk._check_bought_signals` (288行) | 超卖→补仓机会 | `position_risk._check_add_opportunity` |
| 阻力/支撑位 | `position_risk._find_resistance_ceiling` (31行) + `_find_support_floor` (30行) | 阻力位→止盈下调 | `position_risk._check_dynamic_targets` |
| 被套离场分析 | `position_risk._analyze_exit_context` (142行) | MACD空头+板块弱→立即卖 | `position_risk._analyze_exit_context` |
| 板块趋势 | `sector_context._get_sector_trend` (120行) | 板块持续走弱→不买 | `buy_decision._evaluate_buy_decision` |
| 板块热度检测 | `sector_context._detect_hot_sectors` (93行) | 热门板块轮入 | `position_risk._check_stale_positions` |
| 概念板块评分 | `sector_context._get_concept_trend_score` (33行) | 多数概念走弱→不买 | `buy_decision._evaluate_buy_decision` |
| 大盘模式识别（16种） | `market_state._classify_market_pattern` (~500行) | 恐慌→禁止买入/收紧止损 | `market_state._assess_regime` |
| 大盘微观信号 | `market_state._detect_micro_signals` (200行) | 情景概率→前瞻调整 | `market_state._update_scenario_engine` |
| 大盘技术指标拐点 | `market_state._check_index_technicals` (130行) | RSI超卖/底背离→反转信号 | `market_state._index_tech_advice` |
| 大盘支撑/阻力位 | `market_state._compute_key_levels` (40行) | MA20/MA60→关键位判断 | `market_state._assess_regime` |
| 大盘MA基线 | `market_state._get_index_baseline` (45行) | 指数在MA20上方/下方→风险调整 | `market_state._assess_regime` |
| 市场宽度 | `market_state._compute_breadth` (20行) | 涨跌比恶化→禁止买入 | `market_state._assess_regime` |

**结论：计算层只有 7-8 种，决策层有 25+ 种。现在是每个 Mixin 都从头算一遍。market_state.py（2702行）是最大的单体文件，计算/决策高度耦合。**

## 目标架构：四层分离

```
┌─────────────────────────────────────────────────────┐
│                 Layer 4: 输出/推送                    │
│  Telegram 消息 / CLI 报告 / 管线 dict                 │
│  (formatter.py + 现有 _alert/_alert_private)         │
└─────────────────────────────────────────────────────┘
                         ↑
┌─────────────────────────────────────────────────────┐
│              Layer 3: 决策层（thin）                  │
│  MarketStateMixin  →   调用 Layer 2 组件，出 MarketRegime  │
│  BuyDecisionMixin   →   调用 Layer 2 组件，做判断     │
│  PositionRiskMixin  →   调用 Layer 2 组件，做判断     │
│  StockAnalyzer      →   调用 Layer 2 组件，出报告     │
└─────────────────────────────────────────────────────┘
                         ↑
┌─────────────────────────────────────────────────────┐
│           Layer 2: 分析组件（可复用）                  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ │
│  │TechnicalEngine│ │MoneyFlowEngine│ │SectorEngine  │ │MarketEngine  │ │
│  │ RSI/MACD/KDJ │ │ 主力/北向/   │ │ 板块趋势/    │ │ 大盘模式/    │ │
│  │ 布林/均线/   │ │ 龙虎/大单    │ │ 归因/热度    │ │ 微观信号/    │ │
│  │ 量价/形态    │ │              │ │              │ │ 关键位/宽度  │ │
│  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘ │
└─────────────────────────────────────────────────────┘
                         ↑
┌─────────────────────────────────────────────────────┐
│          Layer 1: 数据访问层（Provider）               │
│  DBProvider   →  stock_basic / stock_indicators /   │
│                  sector_snapshots（已有数据）          │
│  QMTProvider  →  get_minute_kline / get_ticks /     │
│                  get_quote_detail（实时行情）          │
│  RemoteProvider → akshare（新增数据，阶段2+）         │
└─────────────────────────────────────────────────────┘
                         ↑
┌─────────────────────────────────────────────────────┐
│           Layer 0: 纯计算函数（无状态）                 │
│  indicators.py — 已有 calc_rsi/calc_macd/calc_kdj    │
│  需新增: calc_bollinger / calc_atr / calc_ma_angle   │
│         detect_golden_cross / detect_divergence      │
└─────────────────────────────────────────────────────┘
```

**关键原则：Layer 2 的组件不依赖 Watcher state。** 输入是数据（DataFrame/dict），输出是结构化结果（dataclass）。Mixin 可以用，StockAnalyzer 也可以用，CLI 独立命令也可以用。

## Layer 0: 纯计算函数扩展

文件: `analysis/screening/indicators.py` 扩展

现有 `calc_rsi`、`calc_macd`、`calc_kdj` 已被多处 import 使用。以下函数**已存在**：

- `calc_bollinger` — 布林带（上层/中轨/下轨/pct_b/width）
- `calc_atr` — ATR（Wilder 平滑）
- `detect_divergence` — 顶/底背离检测（返回 list[dict]）
- `detect_macd_cross` — MACD 金叉/死叉检测（返回 list[dict]，含 days_ago）

需**新增**的函数：

```python
def calc_ma(prices: list[float], period: int) -> float:
    """简单移动平均 — 目前仅作为局部闭包存在（analyzer.py:254、market_state.py:2533），
       未被独立导出"""

def calc_ma_angle(prices: list[float], period: int = 5) -> float:
    """MA 斜率（度）— ma5_angle 目前是数据采集阶段预计算存 DB 的字段，
       指标层缺少对应的实时计算函数"""

def detect_golden_cross(dif: list[float], dea: list[float]) -> bool:
    """MACD 金叉 — detect_macd_cross 的薄包装，返回最近一次是否为金叉"""

def detect_death_cross(dif: list[float], dea: list[float]) -> bool:
    """MACD 死叉 — 同上"""
```

改动风险最低，纯函数测试极快。

## Layer 1: 数据访问层

目录: `analysis/stock/providers/` (与个股分析设计文档一致)

```python
# base.py
class DataProvider(ABC):
    def get_daily_indicators(self, symbol: str) -> DailyIndicators | None: ...
    def get_intraday_kline(self, symbol: str, count: int,
                           period: str = "1m") -> list[dict]: ...
    def get_realtime_quote(self, symbol: str) -> RealtimeQuote | None: ...
    def get_money_flow(self, symbol: str) -> MoneyFlowData | None: ...
    def get_sector_info(self, symbol: str) -> SectorInfo | None: ...
    def get_sector_stats(self) -> dict[str, SectorStats]: ...
    def get_market_snapshot(self) -> dict[str, dict]: ...
```

**DBProvider** — 内部复用现有 `StockReader`/`SectorReader` 的 SQL 查询，包装成统一接口。

**QMTProvider** — 封装 `self.qmt` 的 `get_minute_kline`、`get_ticks`、`get_quote_detail` 等调用。

Provider **不缓存**（缓存是上层的事），只做数据获取。Provider **不判断**（不写「主力大幅流出」之类的结论）。

## Layer 2: 分析组件

### TechnicalEngine

文件: `analysis/engine/technical.py`

```python
@dataclass
class DailyTech:
    """日线级技术指标快照"""
    ma5: float; ma10: float; ma20: float; ma60: float; ma120: float
    bb_upper: float; bb_mid: float; bb_lower: float
    bb_pct_b: float; bb_width: float
    macd_dif: float; macd_dea: float; macd_bar: float
    macd_direction: str  # "bullish" / "bearish" / "neutral"
    kdj_k: float; kdj_d: float; kdj_j: float
    rsi6: float; rsi12: float; rsi24: float
    bbi: float
    ma5_angle: float

@dataclass
class IntradayTech:
    """分钟级技术指标（从 1min kline 计算）"""
    rsi6: float; rsi12: float
    macd_dif: float; macd_dea: float; macd_bar: float
    macd_direction: str
    kdj_k: float; kdj_d: float; kdj_j: float
    price_vs_ma5: float  # 偏离百分比

@dataclass
class M5Tech:
    """5分钟级技术指标"""
    macd_dif: float; macd_dea: float; macd_bar: float
    macd_direction: str
    rsi6: float
    price_vs_ma20: float

@dataclass
class TechnicalSnapshot:
    symbol: str
    daily: DailyTech
    intraday: IntradayTech | None   # None = 无 QMT 数据
    m5: M5Tech | None

@dataclass
class SupportResistance:
    symbol: str
    supports: list[tuple[float, str]]    # [(price, label), ...] 近→远
    resistances: list[tuple[float, str]] # [(price, label), ...] 近→远

@dataclass
class TrendHealth:
    symbol: str
    ma_alignment: str        # "bullish" / "bearish" / "mixed"
    above_ma5: bool
    above_ma20: bool
    near_support: bool       # 接近关键支撑 <3%
    near_resistance: bool    # 接近关键阻力 <3%
    bollinger_position: str  # "oversold" / "lower" / "mid" / "upper" / "overbought"
    macd_healthy: bool       # 日线 MACD 多头
    risks: list[str]
```

方法：

| 方法 | 用途 | 当前代码来源 |
|------|------|-------------|
| `snapshot(symbol)` | 一次性取日线+日内+5min全部指标 | `buy_decision._get_intraday_indicators` + `_get_context_factors` 的技术部分 |
| `support_resistance(symbol, price)` | 找最近支撑/阻力位 | `position_risk._find_resistance_ceiling` + `_find_support_floor` |
| `trend_health(symbol, price)` | 判断趋势健康度 | `buy_decision._analyze_buy_context` 的均线/布林部分 |
| `exit_signals(symbol, price)` | 被套离场信号 | `position_risk._analyze_exit_context` 的个股技术部分 |
| `add_opportunity(symbol, price)` | 补仓机会判断 | `position_risk._check_add_opportunity` |

### MoneyFlowEngine

文件: `analysis/engine/money_flow.py`

```python
@dataclass
class MoneyFlowSnapshot:
    symbol: str
    # 昨日（DB）
    yesterday_main_force: float     # 主力净额
    yesterday_mf_ratio: float       # 主力占比 %
    yesterday_sl_net: float         # 超大单净额
    yesterday_l_net: float          # 大单净额
    # 盘中实时（QMT）
    big_order_buy_ratio: float | None   # 大单买入占比
    big_order_label: str                # "大单买入主导" / "大单均衡" / ...
    order_book_bid_ratio: float | None  # 盘口买盘占比
    order_book_label: str               # "买盘强劲" / "买卖均衡" / ...
```

方法：

| 方法 | 用途 | 当前代码来源 |
|------|------|-------------|
| `snapshot(symbol)` | 昨日资金+今日盘口+大单 | `buy_decision._get_context_factors` 资金部分 + `_get_order_book_imbalance` + `_get_big_order_direction` |

### SectorEngine

文件: `analysis/engine/sector.py`

```python
@dataclass
class SectorContext:
    industry: str
    trend: str                    # "持续走强" / "走弱" / "横盘" / ...
    change_pct: float
    relative_strength: float      # 相对大盘
    continuity: int               # 连续同向轮数
    breadth: float                # 涨跌比
    vol_ratio: float              # 量比
    accel: str                    # "加速" / "趋缓" / ""
    concept_score: int            # 概念板块综合评分 (-3 ~ +3)
    concept_detail: str           # 概念板块详情文本
```

方法：

| 方法 | 用途 | 当前代码来源 |
|------|------|-------------|
| `get_context(symbol)` | 个股板块上下文 | `sector_context._get_sector_trend` + `_get_concept_trend_score` |
| `get_hot_sectors()` | 热门板块 | `sector_context._detect_hot_sectors` |
| `get_cooling_sectors()` | 降温板块 | `sector_context._detect_cooling_sectors` |

注意：SectorEngine 需要访问全局板块快照数据（`_sector_stats`），通过 Provider 和 Watcher 盘中的定期更新获取。Engine 本身不负责更新板块数据，只做查询和判断。

### MarketEngine

文件: `analysis/engine/market.py`

```python
@dataclass
class MarketMicroSignals:
    """微观信号 — 情景引擎输入层"""
    price_velocity: float           # 短期速率 (%/scan)
    price_accel: float              # 加速度
    ema12_pos: str                  # above / below / on
    ema12_just_crossed: str         # crossed_up / crossed_down / ""
    vol_pulse: str                  # expanding / contracting / normal
    vol_price_confirm: str          # yes / no / neutral
    breadth_pct: float              # 涨家占比
    breadth_trend: str              # improving / deteriorating / stable
    higher_highs: bool
    bounce_from_low: float          # 从日内低点反弹幅度
    bounce_quality: str             # strong / weak / failed / ""
    lower_highs: bool
    higher_lows: bool

@dataclass
class MarketKeyLevels:
    supports: list[float]           # 近→远
    resistances: list[float]        # 近→远

@dataclass
class MarketBreadth:
    up: int; down: int; flat: int
    up_ratio: float
    down_ratio: float
    healthy: bool

@dataclass
class IndexTechnicals:
    rsi6: float; rsi12: float
    kdj_k: float; kdj_d: float; kdj_j: float
    macd_dif: float; macd_dea: float; macd_bar: float
    macd_cross: str                 # "golden" / "death" / ""
    kdj_cross: str                  # "golden" / "death" / ""
    rsi6_zone: str                  # "oversold" / "overbought" / "normal"
    rsi12_zone: str
    kdj_j_zone: str
    divergence: str                 # "top" / "bottom" / ""
    divergence_desc: str
    alerts: list[str]
```

方法：

| 方法 | 用途 | 当前代码来源 |
|------|------|-------------|
| `classify_pattern(prices, highs, lows)` | 16种模式识别 | `_classify_market_pattern` |
| `detect_micro_signals(prices, vols, breadth)` | 微观信号提取 | `_detect_micro_signals` |
| `compute_key_levels(prices, pre_close, ma20, ma60)` | 支撑/阻力位 | `_compute_key_levels` |
| `compute_breadth(market_snapshot)` | 涨跌比 | `_compute_breadth` |
| `index_technicals(prices)` | 指数技术指标拐点 | `_check_index_technicals` |
| `index_baseline(db)` | MA5/MA10/MA20/MA60 | `_get_index_baseline` + `_get_index_ma60` |
| `intraday_ema(prices, period)` | 日内EMA | `_calc_intraday_ema` |

MarketEngine 的输入是**行情序列**（prices/highs/lows/vols）和**市场快照**，不依赖 Watcher state。输出是结构化 dataclass。可被以下场景复用：
- Watcher 的大盘状态检测（当前用途）
- 个股分析引擎的「大盘环境」维度
- 复盘系统的市场环境回放

## Layer 3: 决策层（Mixin 瘦身）

核心变化：Mixin 不再直接连 DB、不再内联计算。只做「调用 Engine → 应用阈值 → 做出决策」。

### buy_decision.py 改造

```
当前: 2403 行
目标: ~1200 行
```

改造点：

| 方法 | 当前逻辑 | 改造后 |
|------|---------|--------|
| `_get_intraday_indicators` (233行) | 自己调 QMT → 自己算 RSI/MACD/KDJ | → `tech_engine.snapshot(code).intraday` |
| `_get_context_factors` (131行) | 自己查 DB + 自己调 QMT | → `tech_engine.snapshot()` + `mf_engine.snapshot()` |
| `_analyze_buy_context` (231行) | 自己查布林/均线 | → `tech_engine.snapshot()` + `tech_engine.trend_health()` |
| `_get_order_book_imbalance` (35行) | 自己调 QMT 算盘口 | → `mf_engine.snapshot().order_book_*` |
| `_get_big_order_direction` (44行) | 自己调 QMT 算大单 | → `mf_engine.snapshot().big_order_*` |
| `_evaluate_buy_decision` (332行) | 混计算+判断 | → 纯判断（阈值/规则/打分），数据从 Engine 取 |
| `_evaluate_below_zone` (246行) | 混计算+判断 | → 同上 |

### position_risk.py 改造

```
当前: 1711 行
目标: ~900 行
```

改造点：

| 方法 | 当前逻辑 | 改造后 |
|------|---------|--------|
| `_find_resistance_ceiling` (31行) | 自己查 DB | → `tech_engine.support_resistance()` |
| `_find_support_floor` (30行) | 自己查 DB | → `tech_engine.support_resistance()` |
| `_analyze_exit_context` (142行) | 自己查 DB + 混判断 | → `tech_engine.exit_signals()` + 决策 |
| `_analyze_add_context` (27行) | 自己查 DB | → `tech_engine.add_opportunity()` |
| `_check_add_opportunity` (17行) | 自己查 DB | → `tech_engine.add_opportunity()` |
| `_check_bought_signals` (288行) | 内联技术指标计算 | → 调用 `tech_engine.snapshot()` |
| `_check_dynamic_targets` (112行) | 混计算+判断 | → 纯判断层 |

### sector_context.py 改造

```
当前: 907 行
目标: ~500 行
```

| 方法 | 当前逻辑 | 改造后 |
|------|---------|--------|
| `_get_sector_trend` (120行) | 全在 Mixin 里 | → `sector_engine.get_context()` |
| `_get_concept_trend_score` (33行) | 全在 Mixin 里 | → `sector_engine.get_context().concept_score` |
| `_detect_hot_sectors` (93行) | 全在 Mixin 里 | → `sector_engine.get_hot_sectors()` |
| `_detect_cooling_sectors` (22行) | 全在 Mixin 里 | → `sector_engine.get_cooling_sectors()` |

数据更新部分（`_update_sector_trends`、`_save_sector_snapshots` 等）保留在 Mixin，因为这是 Watcher 的职责（定时采集+落盘），不是 Engine 的。

### market_state.py 改造

```
当前: 2702 行
目标: ~1500 行
```

market_state.py 是 `trade/monitor/` 下最大的文件，同样是「计算+判断混在一起」。表层的 16 种模式检测方法、微观信号提取、技术指标拐点检测、MA 基线计算都是纯计算逻辑，应该迁到 MarketEngine。

| 方法 | 当前逻辑 | 改造后 |
|------|---------|--------|
| `_classify_market_pattern` (~450行) | 16 种模式 + 所有 `_detect_*` 子方法 | → `market_engine.classify_pattern()` |
| `_detect_micro_signals` (200行) | EMA/量能/价格速度/反弹质量 | → `market_engine.detect_micro_signals()` |
| `_compute_key_levels` (40行) | 支撑/阻力位计算 | → `market_engine.compute_key_levels()` |
| `_compute_breadth` (20行) | 涨跌比统计 | → `market_engine.compute_breadth()` |
| `_check_index_technicals` (130行) | MACD/RSI/KDJ 拐点检测 | → `market_engine.index_technicals()` |
| `_get_index_baseline` (45行) | MA5/MA10/MA20 从 DB | → `market_engine.index_baseline()` |
| `_get_index_ma60` (25行) | MA60 从 DB | → 合并入 `index_baseline()` |
| `_calc_intraday_ema` (12行) | 日内EMA | → `market_engine.intraday_ema()` |
| `_confirm_reversal_tech` (40行) | V型反转技术确认 | → `market_engine.classify_pattern()` 内部调用 |
| `_check_multi_day_downtrend` (30行) | 连续多日下跌检测 | → `market_engine` 新增 `multi_day_downtrend()` |
| `_index_trend_desc` (25行) | 趋势描述文本 | → 保留 Mixin（格式化/推送逻辑） |
| `_index_tech_advice` (25行) | 从指标信号生成建议 | → 保留 Mixin（决策逻辑） |
| `_assess_regime` (145行) | 模式+上下文→MarketRegime | → 保留 Mixin（核心决策，调用 MarketEngine） |
| `_update_scenario_engine` (160行) | 状态机概率更新 | → 保留 Mixin（状态管理，但计算委托给 MarketEngine） |
| 告警推送方法 | Telegram 推送 | → 保留 Mixin |

改造后 `_check_market_state`（当前 1891行开始，约 200行编排逻辑）精简为：

```python
def _check_market_state(self, prices):
    engine = self.market_engine
    pattern = engine.classify_pattern(
        self._index_prices, self._index_high, self._index_low)
    micro = engine.detect_micro_signals(
        self._index_prices, self._market_turnovers,
        engine.compute_breadth(self._market_snapshot))
    outlook = self._update_scenario_engine(micro)  # 状态机保留
    ma5, ma10, ma20 = engine.index_baseline()
    regime = self._assess_regime(pattern, price, pre_close, chg_pct, ma20, ma60, outlook)
    return regime
```

### 改造后 Mixin 示例

```python
# buy_decision.py 改造后

def _evaluate_buy_decision(self, code, price, buy_min, buy_max):
    """多维买入决策评估 — 只做规则判断，不做数据计算"""
    tech = self.tech_engine.snapshot(code)
    mf = self.mf_engine.snapshot(code)
    sector = self.sector_engine.get_context(code)
    health = self.tech_engine.trend_health(code, price)

    reject_reasons = []
    warn_reasons = []
    size_mul = 1.0

    # 板块趋势
    if "持续走弱" in sector.trend:
        reject_reasons.append(f"板块持续走弱，不买入")
        size_mul = 0.0
    elif "走弱" in sector.trend:
        warn_reasons.append(f"板块偏弱")
        size_mul *= 0.5

    # 布林带
    if health.bollinger_position == "overbought":
        reject_reasons.append(f"布林带超买(%B={tech.daily.bb_pct_b:.0f})")

    # 日内 RSI
    if tech.intraday and tech.intraday.rsi6 >= 85:
        reject_reasons.append(f"日内RSI6极度超买({tech.intraday.rsi6:.0f})")

    # 大单
    if mf.big_order_buy_ratio is not None and mf.big_order_buy_ratio <= 0.35:
        reject_reasons.append(mf.big_order_label)

    # ... 其余规则判断 ...

    if reject_reasons:
        return False, "; ".join(reject_reasons), 0
    return True, "条件符合", size_mul
```

## Layer 4: 个股分析引擎

目录: `analysis/stock/` (与 `2026-06-02-stock-analysis-design.md` 一致)

直接复用 Layer 2 Engine：

```python
class TechnicalAnalyzer(BaseAnalyzer):
    name = "technical"

    def __init__(self, tech_engine: TechnicalEngine):
        self.engine = tech_engine

    def analyze(self, symbol: str, **params) -> AnalysisResult:
        snap = self.engine.snapshot(symbol)
        sr = self.engine.support_resistance(symbol, snap.daily.ma20)
        health = self.engine.trend_health(symbol, snap.daily.ma20)

        conclusions = []
        risk_flags = []

        # 均线排列
        if health.ma_alignment == "bullish":
            conclusions.append("均线多头排列，趋势向上")
        elif health.ma_alignment == "bearish":
            risk_flags.append("均线空头排列")
            conclusions.append("均线空头排列，趋势偏弱")

        # MACD
        if health.macd_healthy:
            conclusions.append("MACD 日线多头，中期趋势健康")
        else:
            risk_flags.append("MACD 日线空头")

        # ... 更多判断 ...

        return AnalysisResult(
            dimension="technical",
            ok=len(risk_flags) == 0,
            data={"snapshot": snap, "sr": sr, "health": health},
            conclusions=conclusions,
            risk_flags=risk_flags,
        )
```

**个股分析引擎的开发成本大幅降低** —— Layer 2 已经把计算层做好，analyzer 只需要「读数据 → 生成结论」。

## 实施路线图

| 步骤 | 内容 | 预估 | 验证方式 |
|------|------|------|---------|
| 1 | **Layer 0 扩展** — indicators.py 补充 calc_ma/calc_ma_angle + 薄包装 + 单元测试 | 0.3天 | pytest |
| 2 | **Layer 1 Provider** — base + db_provider + qmt_provider | 1天 | 集成测试：provider 返回正确数据 |
| 3 | **Layer 2 TechnicalEngine** — 从 buy_decision/position_risk 搬家计算逻辑 | 1.5天 | 改造前后 Watcher 行为不变 |
| 4 | **Layer 2 MoneyFlowEngine + SectorEngine** — 同上 | 1天 | 改造前后 Watcher 行为不变 |
| 5 | **Layer 2 MarketEngine** — 从 market_state 搬家计算逻辑 | 1.5天 | 改造前后模式识别+技术指标检测结果一致 |
| 6 | **Mixin 瘦身** — 删除 Mixin 内 DB 直连和内联计算（buy_decision / position_risk / sector_context / market_state） | 1.5天 | Watcher 完整跑一轮无报错 |
| 7 | **个股分析引擎阶段1** — 按设计文档建 analysis/stock/，复用 Layer 2 | 1-1.5天 | CLI `python main.py stock 600519` 出报告 |

**总计约 7-8 天。** 每一步都有可验证的里程碑，不会出现长时间无法运行的情况。

## 设计约束

1. **Engine 不缓存** — 缓存由 Watcher 的 `_intraday_cache`、`_daily_factor_cache` 负责。Engine 是纯计算单元，每次调用重算。这样 Engine 可用于 CLI/管线/Watcher，各自决定缓存策略。

2. **Engine 不推送消息** — Engine 只返回 dataclass，不调 `self._alert()`。消息推送是决策层的事。

3. **Provider 不判断** — Provider 只返回原始数据，不做「主力大幅流出」「板块走弱」之类的判断。判断是 Engine 和决策层的事。

4. **Engine 不依赖 Watcher state** — Engine 构造时只接收 Provider，不接收 `self`。Engine 方法签名不出现 `self._scan_count`、`self._regime` 等 Watcher 属性。

5. **每步可验证** — 每次搬家后跑现有测试（`tests/`），确保行为不变。改造是「挪代码」不是「改逻辑」。

## 目录结构变更

```
analysis/
├── engine/                      # [新增] Layer 2 分析组件
│   ├── __init__.py
│   ├── technical.py             # TechnicalEngine + dataclasses
│   ├── money_flow.py            # MoneyFlowEngine + dataclasses
│   ├── sector.py                # SectorEngine + dataclasses
│   └── market.py                # MarketEngine + dataclasses (大盘模式/信号/关键位)
├── stock/                       # [新增] 个股分析引擎（Layer 4）
│   ├── __init__.py
│   ├── schemas.py
│   ├── registry.py
│   ├── formatter.py
│   ├── analyzers/
│   │   ├── __init__.py
│   │   ├── technical.py         # 直接复用 analysis.engine.technical
│   │   ├── money_flow.py        # 直接复用 analysis.engine.money_flow
│   │   └── sector_attr.py       # 直接复用 analysis.engine.sector
│   └── providers/
│       ├── __init__.py
│       ├── base.py              # DataProvider 抽象
│       ├── db_provider.py       # 复用 StockReader/SectorReader
│       └── qmt_provider.py      # 封装 QMT 调用
├── screening/
│   └── indicators.py            # [扩展] 补充 calc_bollinger/calc_atr 等
├── ... (现有文件不变)
```

## 与个股分析设计文档的关系

本方案是 [2026-06-02-stock-analysis-design.md](./2026-06-02-stock-analysis-design.md) 的**前置重构**。重构完成后，个股分析引擎的阶段1实现可以直接复用 Layer 2 的 TechnicalEngine / MoneyFlowEngine / SectorEngine，analyzer 只需写结论生成逻辑。

设计文档中定义的以下组件，在本方案中对应：

| 设计文档 | 本方案 |
|---------|--------|
| `analyzers/technical.py` | 复用 `engine/technical.py` (TechnicalEngine) |
| `analyzers/money_flow.py` | 复用 `engine/money_flow.py` (MoneyFlowEngine) |
| `analyzers/sector_attr.py` | 复用 `engine/sector.py` (SectorEngine) |
| `analyzers/market_env.py`（如有） | 复用 `engine/market.py` (MarketEngine) |
| `providers/base.py` | 完全一致 |
| `providers/db_provider.py` | 完全一致 |
| `providers/remote_provider.py` | 阶段2+ |
