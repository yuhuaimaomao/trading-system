# CLAUDE.md

trading-system — QMT 量化交易系统。独立于复盘系统（`~/quant-system/`），共用数据采集但架构完全分离。

## 项目定位

趋势票中短线交易。量化规则做筛选 + AI 做精选和定价，人做最终决策和手动下单。**不做：** 自动下单、打板、T+0、高频。

## 架构总览

### 领域架构（2026-06-05 重构后）

```
analysis/indicators.py        纯指标函数，零依赖
    ↓
data/                         DB查询 + QMT调用 + 缓存
    ↓
trade/detect/                 从数据中发现信号/模式/异动
    ↓
trade/scenario/               情景概率状态机 + AI模板
    ↓
trade/decision/               规则判断：买/卖/持
    ↓
trade/risk/ + trade/paper/   风控约束 → 模拟盘下单
    ↓
trade/monitor/watcher.py      编排调度（薄层）
```

### 盘中管线（cron: 9:24 → 9:30-15:00）

```
Watcher.run()
  ├─ _recv_collector_data()         TCP 接收全市场快照
  ├─ _check_market_state()          大盘模式分类 → 情景引擎 → MarketRegime
  ├─ _update_sector_trends()        板块趋势 + 热度 + 共振
  ├─ _check_positions()             止损/止盈/移动止盈/回撤止盈/熔断
  ├─ _check_signals()               pending信号 → 买入评估 → 执行
  ├─ _check_bought_signals()        买入后盯盘（六类状态分类）
  ├─ _scout_intraday()              引擎2：盘中机会发现（3轮一次）
  ├─ _check_review_picks()          复盘精选跟踪
  └─ _alert() / _alert_private()  推送去重冷却
```

### 盘前/盘后管线

```
T-1 18:00  review → strategy       复盘报告 → 趋势筛选 → AI分析 → 信号入库
T   9:00  morning                  早盘简报（AI 盘前校准）
T  15:00  close_summary            收盘持仓报告（模拟盘群聊 + 实盘私聊）
```

## 目录结构

```
trading-system/
├── main.py                         CLI 入口
├── analysis/                       【盘后分析 + 纯计算】
│   ├── indicators.py               纯指标函数（RSI/MACD/KDJ/布林/ATR/形态）
│   ├── stock/                      个股分析引擎（schemas/registry/analyzers）
│   ├── screening/                  选股因子（breadth/factors/profiles/trend）
│   ├── review/                     复盘系统（analyzer/formatter/service）
│   ├── audit/                      策略审计（审盘前AI决策）
│   ├── strategy.py                 盘前策略管线
│   ├── morning.py                  早盘简报
│   └── signals.py                  信号 dataclass
├── trade/                          【实时交易】
│   ├── monitor/                    盯盘编排（watcher + state + health + audit）
│   │   ├── watcher.py              主进程（编排调度）
│   │   ├── state.py                运行时状态 + 共享dataclass（ScanState/MarketRegime）
│   │   ├── close_summary.py        收盘汇总
│   │   ├── closing.py              尾盘决策
│   │   ├── sector_resonance.py     板块共振
│   │   ├── sector_heat.py          板块热度
│   │   ├── review_picks.py         复盘精选管理
│   │   ├── health_checks.py        健康检查
│   │   ├── ai_queue.py             AI 异步队列
│   │   └── audit/                  盯盘审计（审watcher决策）
│   ├── detect/                     检测发现
│   │   ├── market_pattern.py       16种大盘模式分类
│   │   └── sector_trend.py         板块趋势/热度/概念评分
│   ├── scenario/                   情景引擎
│   │   ├── engine.py               ScenarioEngine 概率状态机
│   │   ├── signals.py              SCENARIO_SIGNALS 定义
│   │   └── templates/              AI场景模板（breakout/trapped_exit）
│   ├── decision/                   决策判断（纯规则函数）
│   │   ├── buy.py                  买入多维评估 + 回调评估
│   │   ├── sell.py                 离场信号分析 + 持仓状态分类
│   │   ├── sizing.py               仓位计算 + 买入区修正
│   │   └── regime.py               MarketRegime 组装
│   ├── risk/                       风控（engine + rules: stop_loss/take_profit/...）
│   ├── paper/                      模拟盘（PaperAccount + buy执行 + 告警）
│   ├── portfolio/                  持仓数据结构
│   └── execution/                  手动成交/双线比对（manual/comparator）
├── data/                           【数据访问】
│   ├── repo.py                     TradeRepository（统一DB读写）
│   ├── schema.py                   表结构 + 迁移
│   ├── live/                       实时数据（盯盘用）
│   │   ├── quotes.py               QuoteClient（QMT行情）
│   │   ├── collector_client.py     TCP客户端（接收全市场快照）
│   │   ├── qmt_collector.py        采集进程（独立进程，TCP server）
│   │   ├── intraday.py             日内指标快照
│   │   ├── order_book.py           盘口+大单流向
│   │   └── cache.py                盘中缓存管理
│   ├── readers/                    离线数据读取（StockReader/SectorReader）
│   ├── collectors/                 离线采集（market/events/macro）
│   └── processors/                 数据处理
├── system/                         【基础设施】
│   ├── config/                     配置 + prompts
│   ├── qmt/                        QMT HTTP 客户端（行情，不含下单）
│   ├── utils/                      工具（telegram/function_calling/stock_tools）
│   └── services/                   后台服务
└── tests/                          测试（单元 + E2E）
```

## CLI 命令

```bash
# 盘后
python main.py review              # 采集→AI报告→Telegram → 自动调 strategy
python main.py strategy            # 策略管线（筛选→AI→入库）

# 盘前
python main.py morning             # 9:00 早盘简报

# 盘中
python main.py monitor             # 9:24 cron → 9:30 启动盯盘 → 15:00 自动收盘
python main.py listen              # Telegram 消息监听
python main.py collect --module news  # 盘中电报（每5分钟）

# 盘后手动
python main.py compare             # 收盘双线比对
python main.py strategy-audit      # 选股审计
python main.py track               # 股票追踪统计

# 手动
python main.py trade --text '000001 1000股 12.50'
python main.py portfolio           # 持仓查询
```

## 数据库

`storage/stock_market.db`（与 quant-system 共用部分表）

### 交易系统独有表

| 表 | 用途 |
|----|------|
| `trade_signals` | 交易信号（pending/bought/expired） |
| `trade_orders` | 成交记录（buy/sell, paper/real） |
| `trade_portfolio_positions` | 每日持仓明细 |
| `trade_portfolio_snapshots` | 每日快照 |
| `strategy_funnel` | 选股漏斗全记录 |
| `strategy_ai_log` / `strategy_ai_decisions` | AI调用原文 + 每票决策 |
| `strategy_lessons` / `strategy_improvements` | 经验教训 + 改进建议 |
| `watcher_decision_log` | 盯盘决策日志（审计用） |
| `market_breadth` / `sector_snapshots` / `index_snapshots` | 市场数据 |

### 共用表

- `stock_basic` — 全市场日线（含 industry/concepts/ma5/ma10/ma20/主力）
- `stock_indicators` — 技术指标（MACD/RSI/KDJ/布林带）
- `stock_tracker` — 复盘推荐标的

## 关键约定

1. **AI 模型**：`deepseek-v4-pro`（DeepSeek provider），永不使用 `deepseek-chat`。`.env` 设 `AI_MODEL=deepseek-v4-pro`
2. **实盘/模拟盘分离**：`trade_orders.account` 区分 paper/real。模拟盘自动执行，实盘用户 Telegram 确认后录入
3. **Watcher 无 DB fallback**：拉不到 QMT 行情跳过该轮，不用 DB 收盘价
4. **不下单**：策略交易权限未开通。模拟盘自动执行，实盘人工确认
5. **_scan() 逐步骤异常保护**：每步独立 try/except，单步失败不阻塞后续
6. **买入后盯盘不丢**：六类状态（healthy/watching/at_risk/trapped/deep_trapped/add_opportunity），每~20分钟推送
7. **止损循环人工确认**：触发后推送提醒 + 5分钟循环 + 支持"再等 N"延迟
8. **深跌等待反弹**：亏损 > 7% 且 14:00 前不立即止损，等反弹机会
9. **智能仓位**：根据市场模式（0-20000）+ 板块趋势（±20-40%）+ 买入区位置动态计算
10. **利润回撤止盈分级 + 大盘加成**：三级保护（≥15%/≥10%/≥5%），极端行情多保留 10%
11. **entry_rule 六级入场**：standard/pullback/confirm/range_boundary/next_day/none
12. **情景引擎双轨制**：16 种事后模式 + 8 种预测情景，概率融合到 MarketRegime
13. **模拟盘费率**：佣金万 0.85 最低 5 元，印花税万分之五（减半征收）卖出单边
14. **所有对话中文**，文件修改直接执行，不新建 README/文档除非明确要求
15. **绝对服从**：完全按命令执行，不确定就说不知道，不替用户做决定
16. **代码修改后主动更新 CLAUDE.md**：涉及架构/API/数据流/配置/新增模块时必须更新
17. **CLAUDE.md 更新原则：就地修正，不追加流水账**。改了架构就删旧写新，新增约定合并到已有分类，过时内容直接删除。保持文档始终反映当前状态，不做 git log。旧版这份 911 行的流水账就是反面教材

## QMT API

服务地址: `http://192.168.1.33:5000`（Windows 机器，xtdata 自动连接）

| 端点 | 用途 | 备注 |
|------|------|------|
| `/all_quotes` | 全市场快照（4818只） | 每5分钟以上调一次 |
| `/quotes?codes=` | 批量行情 | 必须带 .SH/.SZ 后缀 |
| `/quote/{code}` | 单只行情 + 5档盘口 | 含 preClose |
| `/history?period=1m` | 日内分钟K线 | 通过 `/minute_kline` 映射 |
| `/history?period=1d` | 日K线 | |
| `/tick` | 逐笔成交 | 仅盘中可用 |
| `/instrument/{code}` | 合约信息 | 涨跌停价/股本 |

**注意**：代码必须带后缀，`QuoteClient` 已自动处理。`/quotes` 不含 preClose。

## 注意事项 / 坑

- **开盘恐慌扫止损**：开盘 5 分钟内亏损 < 5% 的止损跳过（`position_risk.py` 开盘缓冲期 300s）
- **死猫跳前置条件**：必须有日内大跌（跌幅 > 0.5%）才判死猫跳/弱反弹
- **单边下跌宽度验证**：涨多跌少（down/total < 55%）不判单边下跌
- **费率已修正**：`STAMP_TAX_RATE = 0.0005`（减半征收）
- **已持仓不推送买入信号**：`_check_buy_candidates` 入口检查 `code in self._bought_watch`
- **同价格不反复推送**：去重比较价格变化 < 0.5% 时跳过
- **insert_snapshot 用 INSERT OR REPLACE**：表有 UNIQUE(trade_date, account) 约束
- **Watcher 实例变量非类变量**：`_signal_alert_state` 等必须是实例属性
- **Watcher 不 sleep**：每轮扫描完立即开始下一轮
- **FunctionCallingEngine 工具注册双写**：`stock_tools.py` TOOLS_DEFINITION + `function_calling.py` tool_functions 字典
- **CLS API 迁移**：`/nodeapi/telegraphList` 已废弃 → `/api/cache?name=telegraph`
- **天启代理整点劣化**：整点附近代理采集器易失败，review/service.py 已加重试
- **dns_bypass**：绕过小火箭 DNS 劫持（198.18.x.x 假 IP）
- **telegram.py requests verify=False**：小火箭 HTTPS MITM 导致证书验证失败
- **QMT 只用于行情**：`data/live/quotes.py`（行情）、`system/qmt/`（连接）。下单功能不可用，已删除
- `stock_tracker` 字段 `star_rating`（不是 `score`）
- `PROJECT_ROOT` 用了 `.parent.parent.parent`（相对 system/config/）

## 测试

```bash
# 单元测试（每次改代码后必须跑）
python3 -m pytest tests/ -q

# E2E 全量验证（改 market_state/position_risk/buy_decision/watcher 后必须跑）
E2E_TEST_MODE=1 python3 tests/e2e/verify_comprehensive.py --day 2 --scans 240
```

- E2E 测试 DB 从生产库 `shutil.copy2` 复制，完全物理隔离
- `E2E_TEST_MODE=1` 下 `TradeRepository()` 无参构造直接报错
- 217 条检查清单，覆盖 A-N 共 14 大类
