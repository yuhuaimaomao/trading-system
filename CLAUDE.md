# CLAUDE.md

trading-system — QMT 量化交易系统。

## 项目定位

趋势票中短线交易。量化规则做筛选 + AI 做精选和定价，人做最终决策和手动下单。
**不做：** 自动下单、打板、T+0、高频。

## 系统功能

| 功能 | 入口 | 说明 |
|------|------|------|
| 数据采集 | `python main.py collect` | 16 个采集器，从东方财富/财联社等获取全市场行情、板块、龙虎榜、电报 |
| 盘后复盘 | `python main.py review` | 采集当日数据 → AI 多维度分析 → 生成复盘报告 → 推 Telegram |
| 策略选股 | `python main.py strategy` | 市场宽度 → 趋势筛选 → 因子评分 → AI 研判 → 买入信号入库 |
| 盘前简报 | `python main.py morning` | 汇总隔夜宏观+电报+外围 → AI 评估风险 → 推 Telegram |
| 盯盘交易 | `python main.py monitor` | 盘中每分钟扫描，检测机会 → 判断买卖 → 模拟盘自动执行 → 推 Telegram |
| 消息监听 | `python main.py listen` | 长轮询 Telegram，接收实盘确认/延迟指令 |
| 策略审计 | `python main.py strategy-audit` | 规则+AI 双轨审计 → 发现决策偏差 → 生成改进建议 |
| 个股追踪 | `python main.py track` | 早报推荐股票追踪，记录到 Excel + DB，统计准确率 |
| 收盘处理 | `python main.py closeout` | 保存持仓快照、过期信号、推送收盘报告 |
| 实盘比对 | `python main.py compare` | 模拟盘 vs 实盘持仓差异比对 |

## 架构总览

### 盯盘管线

```
数据(Collector/QMT) → 检测(Detect) → 情景(Scenario) → 决策(Decide) → 执行(Paper) → 消息(Telegram)
```

依赖单向，各模块独立。

**数据流**：QMT (Windows) → Collector (Mac, TCP 15555) → Watcher。全市场 4717 只快照每 7 秒推送到 `_market_snapshot`，`_get_realtime_prices` 直接读内存，不走 QMT HTTP。

**双引擎**：引擎1 信号跟踪（`_check_buy_candidates` → `evaluate_buy`）+ 引擎2 盘中发现（`_scout_intraday`）。详解见 [交易系统说明文档](docs/project/交易系统说明文档.md)

### 目录结构

```
trading-system/
├── main.py                         CLI 入口
├── strategy/                       盘前策略线
│   ├── screening/                  选股因子（breadth/factors/profiles/trend）
│   ├── strategy_pipeline.py        趋势筛选管线 → AI → 信号入库
│   ├── morning_brief.py            早盘简报
│   └── ai_advisor.py               AI 选股顾问
├── trade/                          盘中盯盘线
│   ├── core/                       主编排（watcher/closeout/review_picks/scan_state）
│   ├── detect/                     检测发现
│   ├── scenario/                   情景判断
│   ├── sector/                     板块分析
│   ├── decision/                   决策判断
│   ├── risk/                       风控
│   └── exec/                       执行（paper/ + real/）
├── review/                         盘后复盘线
│   ├── review_service.py           编排入口
│   ├── review_analyzer.py          核心分析
│   ├── review_formatter.py         格式化输出
│   ├── review_stats.py             统计计算
│   ├── prediction_verifier.py      预测验证
│   └── stock_tracker.py            早报追踪 + 准确率
├── audit/                          审计改进线（双轨：策略+盯盘）
├── stock/                          个股分析线（indicators/signals/analyzers）
├── data/                           数据层（按业务线组织，详见下方）
├── system/                         系统基础设施
│   ├── ai/                         AI 服务（多模型 + FC + 全部 prompt）
│   ├── message/                    消息收发（Telegram）
│   ├── config/                     配置（settings/trading_calendar）
│   ├── qmt/                        QMT 客户端
│   └── utils/                      工具（日志 v4.0/DNS/股票代码/监管函PDF分析含OCR回退）
├── tests/                          测试
├── ops/                            运维（scheduler/tools/pre_commit）
├── docs/                           文档（约定/更新记录/设计文档）
└── storage/                        运行时数据（DB/日志/PID/缓存）
```

## 数据层

```
data/
├── _base.py              # 共享基类 + connect() + get_db_conn()
├── schema.py             # DDL + 迁移
├── market/               # 行情基础数据（跨域共享）
│   ├── stock_basic.py    #   StockReader — 个股查询/资金流趋势/波动率异动
│   ├── sector_data.py    #   SectorReader — 板块查询
│   └── events_data.py    #   LimitPoolReader — 涨跌停/龙虎榜
├── trade/                # 交易线
│   ├── signals.py        #   SignalRepo — trade_signals
│   ├── orders.py         #   OrderRepo — trade_orders
│   └── portfolio.py      #   PortfolioRepo — 持仓/快照
├── strategy/             # 策略线
│   ├── funnel.py         #   StrategyRepo — 漏斗/决策/改进
│   ├── morning.py        #   MorningReader — 早报查询
│   └── screening.py      #   ScreeningReader — 筛选因子查询
├── review/               # 复盘线
│   ├── tracker.py        #   TrackerRepo — stock_tracker
│   ├── predictions.py    #   PredictionRepo — 预测/教训
│   └── analysis.py       #   AnalysisReader — 复盘分析查询
├── audit/                # 审计线
│   └── decision_log.py   #   AuditRepo — 决策日志/发现/改进
├── collect/              # 数据采集
├── process/              # 数据加工
├── repo/                 # [兼容层] → 实际代码已迁至上述各业务线目录
└── readers/              # [兼容层] → 同上
```

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/project/交易系统说明文档.md](docs/project/交易系统说明文档.md) | 系统说明书，双引擎详解、模块接口、数据流 |
| [docs/project/数据库字典.md](docs/project/数据库字典.md) | 全部 46 张表的结构、索引、用途 |
| [docs/project/约定与规范.md](docs/project/约定与规范.md) | 关键约定、编码规范、日志、AI调用、审计、测试 |
| [docs/project/已知陷阱.md](docs/project/已知陷阱.md) | 踩过的坑，QMT行情/Watcher稳定性/数据一致性/代码路径/交易逻辑 |
| [docs/project/更新记录.md](docs/project/更新记录.md) | 重大变更、bug 修复、架构调整时间线 |
| `docs/` 下其他文件 | 各功能模块设计文档（中文文件名） |
