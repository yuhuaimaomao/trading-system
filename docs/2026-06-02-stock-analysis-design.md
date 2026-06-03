# 个股分析引擎 — 设计文档

日期: 2026-06-02

## 需求

个股综合分析能力，覆盖技术面、资金面、基本面、机构行为、事件驱动、板块归因、新闻舆情、历史规律等维度。同时支持 CLI/Telegram 查询和策略管线内部调用。

## 设计决策

| # | 决策 | 选项 |
|---|------|------|
| 1 | 架构模式 | A 方案：分析器注册表，维度独立、分阶段实现 |
| 2 | 位置 | `analysis/stock/`，独立子模块，不混入现有 analysis 顶层文件 |
| 3 | 分析器粒度 | 每个维度一个文件，统一 `analyze(symbol, **params) -> AnalysisResult` 接口 |
| 4 | 数据获取 | Provider 抽象层隔离，分析器不直接调 akshare/QMT/DB |
| 5 | 输出通道 | CLI / Telegram / 管线 dict 三种格式，同一个 Report 结构 |
| 6 | 查询模式 | `quick()` 技术+资金+板块（盘中用），`deep()` 全维度（盘后用） |
| 7 | 落地节奏 | 阶段1: 技术+资金+板块 → 阶段2: 基本面+机构+事件 → 阶段3: 舆情+规律 |
| 8 | Web 看板 | 预留，当前不做 |

## 目录结构

```
analysis/stock/
├── __init__.py            # StockAnalyzer 统一入口 + quick/deep 预设
├── schemas.py             # StockAnalysisRequest / AnalysisResult / StockAnalysisReport
├── registry.py            # 分析器注册表
├── formatter.py           # 多通道输出（CLI / Telegram / 管线 dict）
├── analyzers/
│   ├── __init__.py        # BaseAnalyzer 抽象类
│   ├── technical.py       # K线/均线/布林/RSI/MACD/量价关系
│   ├── money_flow.py      # 主力资金/北向/龙虎榜
│   ├── fundamental.py     # 财务指标/估值/财报（阶段2）
│   ├── institution.py     # 机构评级/调研/持仓变动（阶段2）
│   ├── event_driven.py    # 解禁/增减持/分红/公告（阶段2）
│   ├── sector_attr.py     # 板块归因/相关性
│   ├── news_sentiment.py  # 新闻舆情（阶段3）
│   └── history_pattern.py # 历史规律/季节性（阶段3）
└── providers/
    ├── __init__.py
    ├── base.py            # DataProvider 抽象接口
    ├── db_provider.py     # 从 SQLite/readers 取数据（已有）
    └── remote_provider.py # 实时调 akshare（新增数据）
```

## 核心数据结构

```python
# schemas.py

@dataclass
class StockAnalysisRequest:
    symbol: str                    # "600519"
    dimensions: list[str]          # ["technical", "money_flow"]
    params: dict                  # {"days": 60, "kline_type": "day"}

@dataclass
class AnalysisResult:
    dimension: str                 # "technical"
    ok: bool
    data: dict                     # 结构化数据，给管线消费
    conclusions: list[str]         # 结论短句，给输出消费
    risk_flags: list[str]          # 风险标签
    error: str = ""

@dataclass
class StockAnalysisReport:
    symbol: str
    name: str
    results: list[AnalysisResult]
    aggregated: dict               # 综合评分 + 关键风险汇总
```

## 核心接口

### BaseAnalyzer

```python
class BaseAnalyzer:
    name: str                      # 维度名，和 registry key 一致

    def analyze(self, symbol: str, **params) -> AnalysisResult:
        """拉数据 -> 计算指标 -> 生成结论，不碰输出格式化"""
        raise NotImplementedError
```

### Registry

```python
_registry: dict[str, BaseAnalyzer] = {}

def register(analyzer: BaseAnalyzer): ...
def get(name: str) -> BaseAnalyzer: ...
def list_all() -> list[str]: ...
def get_many(names: list[str]) -> list[BaseAnalyzer]: ...
```

### StockAnalyzer 入口

```python
class StockAnalyzer:
    def __init__(self, dimensions: list[str] | None = None):
        """不传 dimensions 则跑全部已注册分析器"""

    def analyze(self, symbol: str, **params) -> StockAnalysisReport: ...

    def quick(self, symbol: str) -> StockAnalysisReport:
        """快速模式：技术面 + 资金面 + 板块归因，给 Watcher/盘前用"""

    def deep(self, symbol: str) -> StockAnalysisReport:
        """深度模式：全维度，给盘后研究/AI prompt 用"""
```

### DataProvider 接口

```python
class DataProvider(ABC):
    def kline(self, symbol: str, days: int, freq: str = "day") -> pd.DataFrame: ...
    def realtime_quote(self, symbol: str) -> dict: ...
    def money_flow(self, symbol: str, days: int) -> pd.DataFrame: ...
    def financial(self, symbol: str) -> dict: ...
    def institution(self, symbol: str) -> dict: ...
    def events(self, symbol: str, days: int) -> list[dict]: ...
    def sector_membership(self, symbol: str) -> list[str]: ...
```

## 数据来源映射

| 数据 | 来源 | 备注 |
|------|------|------|
| K线/均线/量价 | DB `stock_basic` + QMT 实时 | 已有 |
| 资金流 | DB + akshare `stock_individual_fund_flow` | 已有部分 |
| 板块归因 | DB `sector_stocks` + `sector_info` | 已有 |
| 财务指标 | akshare `stock_financial_abstract` | 新增 |
| 机构评级 | akshare `stock_rating_detail` | 新增 |
| 龙虎榜 | DB `lhb_stocks` | 已有 |
| 增减持/解禁 | DB (collectors 已采) + akshare 补充 | 已有部分 |
| 新闻舆情 | akshare `stock_news_em` / 外部爬虫 | 阶段3 |

原则：**已有数据走 DB（快），没有的由 remote_provider 实时拉。** Provider 层负责缓存和 fallback，分析器只看到数据。

## 输出通道

同一个 `StockAnalysisReport`，三种格式：

| 通道 | 方法 | 用途 |
|------|------|------|
| Telegram | `Formatter.to_telegram(report)` | 紧凑短句 + emoji，<= 1000 字符 |
| CLI | `Formatter.to_cli(report)` | 终端展示，和 Telegram 类似但可稍宽 |
| 管线 | `Formatter.to_dict(report)` | 结构化 dict，给策略/Watcher 程序消费 |

## 管线集成点

```
现有管线                              + 个股分析
────────────────────────────────────────────────
盘前 morning.py
  └─ 候选池确认                      -> StockAnalyzer.quick()
                                      结果附到 briefing

策略管线 strategy.py
  └─ 画像富化                        -> quick() 作为新的画像数据源
  └─ 信号入库                        -> 分析结论写入 signals.reason

盯盘 Watcher
  └─ 持仓巡检                        -> 持仓票 quick() 定时刷新（~30分钟）
  └─ 异动检测                        -> 异动票 on-demand deep() 诊断
  └─ 买入决策 buy_decision.py        -> 候选买点前 quick() 校验

盘后 review/
  └─ AI 分析                          -> deep() 全维度数据注入 AI prompt

CLI 独立命令
  └─ python main.py stock 600519     -> 终端输出全维度报告
```

## Telegram 集成

两种触发方式：

1. **主动查询**：`listen` 命令扩展关键词 → `分析 600519` / `600519 技术面` → quick/deep → 私聊回复
2. **管线推送**：Watcher/morning 内部调用 → 结果拼入已有推送消息

## 阶段1 实现清单

| 文件 | 内容 | 预估行数 |
|------|------|----------|
| `analysis/stock/__init__.py` | StockAnalyzer + quick/deep 预设 | ~80 |
| `analysis/stock/schemas.py` | 3 个 dataclass | ~40 |
| `analysis/stock/registry.py` | 注册表 | ~30 |
| `analysis/stock/formatter.py` | 三通道输出 | ~120 |
| `analysis/stock/analyzers/__init__.py` | BaseAnalyzer | ~20 |
| `analysis/stock/analyzers/technical.py` | 技术面分析 | ~300 |
| `analysis/stock/analyzers/money_flow.py` | 资金面分析 | ~200 |
| `analysis/stock/analyzers/sector_attr.py` | 板块归因 | ~150 |
| `analysis/stock/providers/__init__.py` | 导出 | ~10 |
| `analysis/stock/providers/base.py` | DataProvider 抽象 | ~40 |
| `analysis/stock/providers/db_provider.py` | DB 数据源 | ~150 |
| `analysis/stock/providers/remote_provider.py` | akshare 数据源 | ~100 |

阶段1 共 12 个文件，约 1240 行。先跑通技术面+资金面+板块归因三个维度。
