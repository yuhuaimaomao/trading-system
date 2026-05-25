# Trading System 统一架构设计

> 日期：2026-05-22 | 状态：设计中

## 设计决策总结

| 决策 | 结论 |
|------|------|
| 系统形态 | 统一大系统，量化、复盘、交易在一个项目 |
| 架构风格 | 功能平铺，一个目录一件事 |
| 数据采集 | 保留 quant-system 全 15 个采集器 |
| 执行模式 | 实盘手动 + 模拟盘自动并行 |
| 工作节奏 | 盘后复盘+筛选 → 盘前简报 → 盘中盯盘 |
| AI 模型 | DeepSeek + 千问双模型并行比对 |
| 盯盘方式 | cron 拉起，进程自管理生命周期 |
| 交易日判断 | QMT /calendar 接口（待实测） |

## 目录结构

```
trading-system/
├── main.py                    # 唯一入口
├── config/                    # 统一配置
│   ├── settings.py
│   ├── akshare_config.py      # akshare 配置（迁入）
│   ├── proxy_config.py        # 代理配置（迁入）
│   └── prompts/               # AI Prompt 模板
│       ├── review.py          # 复盘 Prompt（迁入）
│       └── ai_advisor.py      # 交易 AI 分析 Prompt（新建）
├── qmt/                       # QMT 基础设施层（独立，被多模块共享）
│   ├── client.py              # HTTP 客户端（现有迁入）
│   ├── quotes.py              # 实时行情封装
│   ├── orders.py              # 下单接口（预留，待实测）
│   └── calendar.py            # 交易日历
├── collectors/                # 数据采集（15个采集器从 quant-system 迁入）
│   ├── proxy/                 # 代理基础设施
│   │   ├── proxy_manager.py
│   │   ├── proxy_requester.py
│   │   ├── ip_stats.py        # IP 统计
│   │   └── ...
│   ├── market/                # 行情/指数/板块
│   ├── events/                # 电报/公告/监管
│   └── macro/                 # 隔夜宏观
├── review/                    # 盘后复盘
│   ├── screening/             # 复盘筛选（18章用，不改动）
│   ├── chapters/              # 18章格式化
│   ├── readers/               # DB 查询
│   └── analyzer.py            # AI 复盘分析
├── strategy/                  # 交易策略层
│   ├── screening/             # 交易筛选（新逻辑，独立于复盘筛选）
│   ├── factors/               # 因子计算
│   ├── signals.py             # 信号生成
│   ├── ai_advisor.py          # AI 分析候选池
│   └── backtest/              # 回测引擎
├── risk/                      # 风控引擎（7级优先级）
├── execution/                 # 执行层
│   ├── manual.py              # 实盘手动（Telegram推送→用户确认）
│   ├── paper.py               # 模拟盘自动（模拟成交+滑点）
│   └── qmt.py                 # QMT 自动下单（预留）
├── portfolio/                 # 组合管理（多账户）
├── monitor/                   # 盘中盯盘
│   └── watcher.py             # 实时扫描+触发推送
├── db/                        # 统一数据访问层
│   ├── schema.py
│   └── repository.py
├── utils/                     # 通用工具
│   ├── logger.py              # 三层日志体系（迁入）
│   ├── telegram.py            # Telegram 推送
│   ├── stock_code_utils.py    # 代码格式转换/校验（迁入）
│   ├── decorators.py          # 重试/计时装饰器（迁入）
│   ├── function_calling.py    # FC 引擎（迁入）
│   └── stock_tools.py         # FC 工具定义（迁入，~54KB）
├── storage/                   # SQLite/日志/缓存/报告
├── scheduler/                 # cron 脚本
└── tests/
```

### 量化迁移清单

从 quant-system 迁入的文件一览：

| 源文件 | 新位置 | 说明 |
|--------|--------|------|
| `utils/logger.py` | `utils/logger.py` | 三层日志，替换 trading-system 简化版 |
| `utils/proxy_manager.py` | `collectors/proxy/proxy_manager.py` | 代理池管理 |
| `utils/stock_code_utils.py` | `utils/stock_code_utils.py` | 代码格式转换/校验 |
| `utils/decorators.py` | `utils/decorators.py` | 重试/计时装饰器 |
| `utils/function_calling.py` | `utils/function_calling.py` | FC 引擎 |
| `utils/stock_tools.py` | `utils/stock_tools.py` | FC 工具定义 |
| `utils/ip_stats.py` | `collectors/proxy/ip_stats.py` | IP 统计 |
| `utils/telegram_bot.py` | 已就位 | 两边一致 |
| `config/review_report_prompt.py` | `config/prompts/review.py` | 复盘 Prompt |
| `config/akshare_config.py` | `config/akshare_config.py` | akshare 配置 |
| `config/proxy_config.py` | `config/proxy_config.py` | 代理配置 |
| `config/trading_calendar.py` | **废弃** | QMT `/calendar` 替代 |
| `config/db_config.py` | **废弃** | 之前就废弃了 |

### qmt/ — QMT 基础设施层

独立于所有业务模块，被 collectors、monitor、execution 三模块共享：

- `client.py`：HTTP 客户端封装，连接 QMT Server，统一超时/重试/异常处理
- `quotes.py`：实时行情接口（快照、Tick、分钟K线、历史K线）
- `orders.py`：下单/撤单/查单接口（预留，待 QMT 实测）
- `calendar.py`：交易日历查询，替代静态 `trading_calendar.py`

放在顶层而非 utils/ 的理由：QMT 不是"通用小工具"，它是一个独立的基础设施服务，有自己的一套接口和状态。

## 模块职责

### collectors/ — 数据采集

- `proxy/`：代理池管理、UA伪装、请求重试
- `market/`：个股行情、指数、行业/概念板块、板块成分股、停复牌
- `events/`：涨跌停池、龙虎榜、强势股、电报、公告、监管函、监控、增减持
- `macro/`：隔夜宏观（美股/A50/商品）

规则：每个采集器独立 try/except，一个挂不影响其他。采集器之间不互相调用。

### review/ — 盘后复盘

复盘筛选（`review/screening/`）服务于 18 章报告生成，与交易筛选完全独立。
输出是报告里的文字，不改动 quant-system 原有逻辑。

### strategy/ — 交易策略层

- `screening/`：交易用趋势票筛选，借鉴但不等于复盘筛选，更复杂
- `factors/`：因子计算（MA/量能/动量/市值等）
- `signals.py`：生成 OrderSignal
- `ai_advisor.py`：AI 分析候选池，输出买卖区间和理由
- `backtest/`：历史回测

AI 定位：在量化规则之上加判断层，精选+定买卖区间，不是替代量化规则。

### risk/ — 风控引擎

7级优先级：黑名单→市场环境→集中度→日内熔断→止损→移动止盈→目标止盈

### execution/ — 执行层

信号同源，执行分流：

```
OrderSignal ─┬─→ manual.py  → Telegram推送 → 用户确认 → 记录持仓
             └─→ paper.py   → 模拟成交（当前价+滑点）→ 自动执行
             └─→ qmt.py     → QMT自动下单（预留）
```

配置文件 `ACCOUNT_MODE` 控制：`"manual"` | `"paper"` | `"live"`

### monitor/ — 盘中盯盘

盯盘扫描两个来源：

- **strategy picks**（交易筛选）：有买入区间、止损止盈，触发→推送买卖建议
- **review picks**（复盘推荐）：仅监控价格+异动提醒，不自触发买卖建议。复盘推荐自带买点参考，但必须经交易筛选二次确认才能转为买入信号

cron 每天 9:25（集合竞价结束）拉起一次，进程自管理：9:25-11:30 循环→11:30-13:00 午休→13:00-15:00 循环→15:00 退出。

### db/ — 数据访问层

统一管理所有表，提供 Repository 类。所有模块通过 `db/` 读写。

### utils/ — 通用工具

`logger.py`、`telegram.py`、`proxy_manager.py`。不包含业务逻辑。

## CLI 命令

```bash
python main.py review          # 盘后全流程（采集+筛选+AI+报告+推送）
python main.py morning         # 盘前简报（隔夜宏观+候选池确认+推送）
python main.py monitor         # 盯盘（cron拉起，进程自管理）
python main.py collect         # 单独采集（盘中电报用）
python main.py cleanup         # 周清理
python main.py portfolio       # 查看持仓
python main.py trade           # 手动录入交易
python main.py backtest        # 回测
python main.py test            # 配置检查
```

## Cron 调度

```bash
# 盘后复盘 — 20:00（东财反爬考虑）
0 20 * * 1-5  python main.py review

# 早盘简报 — 9:00
0  9 * * 1-5  python main.py morning

# 盘中电报 — 每5分钟
*/5 9-17 * * 1-5  python main.py collect --module news

# 盯盘拉起 — 9:25（集合竞价结束）
25 9 * * 1-5  python main.py monitor

# 周清理
0  9 * * 0    python main.py cleanup
```

所有任务启动时先查交易日历，非交易日直接退出。

## 数据流

### 盘后（20:00）

```
cron → main.py review
  ├─→ 15 个采集器 fetch_and_save() → DB
  ├─→ review/screening/ → 18章格式化 → AI 复盘 → Telegram
  ├─→ strategy/screening/ → 交易趋势筛选
  └─→ strategy/ai_advisor.py → 双模型并行分析 → 存入 trade_signals
```

### 盘前（9:00）

```
cron → main.py morning
  ├─→ collectors/macro/ → 更新隔夜数据
  └─→ trade_signals 取候选池 → 对比隔夜变化 → Telegram 推送
```

### 盘中（9:30-15:00）

```
cron 9:25拉起 → main.py monitor
  while 交易时段:
    QMT实时行情 → 扫描候选池+持仓
      ├─→ 触及买入区间 → Telegram: "进入买入区间"
      │     └─→ 用户确认 → execution/manual.py 记录
      ├─→ 触发止损/止盈 → Telegram: "建议卖出"
      │     └─→ 用户确认 → 平仓记录
      ├─→ 复盘推荐异动 → Telegram: "异动提醒"（不触发买卖）
      └─→ 模拟仓自动执行 → paper.py 自动成交
```

## 信号生命周期

```
pending → confirmed(用户确认) → executed(已成交) → closed(已平仓)
                                             ↘ rejected(用户拒绝) → 归档
```

## 账户模型

```python
Account:
  - account_id: "paper" | "real"
  - mode: "manual" | "paper" | "live"
  - portfolio: 独立持仓
  - 独立绩效统计
```

同一批信号，两个账户独立执行，独立状态机，绩效分开统计。

## DB 表规划

复用 quant-system 现有 28 张表（不做删减），新增交易系统 5 张表（已建），均加 `account` 字段：

- `trade_factor_values` — 因子值
- `trade_signals` — 交易信号
- `trade_orders` — 订单记录
- `trade_portfolio_snapshots` — 组合快照
- `trade_strategy_metrics` — 策略绩效

## 迁移策略

### 阶段 1：基础架构
建目录骨架 + main.py 入口 + 策略/执行/监控新写。quant-system 不碰。

### 阶段 2：迁移采集器
代理基础设施→个股行情→指数→板块→事件→宏观。搬一个验证一个。

### 阶段 3：迁移复盘
采集器就位后搬复盘模块，停用 quant-system review。

### 阶段 4：交易链路
全新写：交易筛选/AI分析/手动执行/模拟仓/盯盘/早盘。

### 阶段 5：quant-system 退役
全部跑通后备份→删除旧目录。

原则：每步独立运行、quant-system 不提前删、DB 共用不迁移。

## QMT 待实测项

- `/calendar` 接口返回的交易日格式和准确性
- 实时行情延迟和轮询最优间隔
- 下单接口可用性和限制
- 模拟仓接口（如果 QMT 原生支持）

架构中 QMT 接口先留接口定义，实测后再填充具体实现。

## 错误处理与日志

沿用 quant-system 三层日志体系：

```
task.review           → tasks/review.log
  ├── task.review.collectors.xxx  → collectors/xxx.log
  ├── task.review.core.analyzer   → core/analyzer.log
  └── task.review.core.telegram   → core/telegram.log
```

规则：logger 在 `__init__` 中创建，采集器用 get_collector_logger，Analyzer 用 get_core_logger，Service 用 get_task_logger。
