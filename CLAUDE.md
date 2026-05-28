# CLAUDE.md

trading-system — QMT 量化交易系统。独立于复盘系统（`~/quant-system/`），共用数据采集但架构完全分离。

## 项目定位

趋势票中短线交易。量化规则做筛选 + AI 做精选和定价，人做最终决策和手动下单。

**不做的事：** 自动下单、打板、T+0、高频。

## 当前状态

**交易管线全部完成**（2026-05-28），cron 全自动化，等盘中实跑验证。

### 已完成模块

**盘前管线：**
- 趋势筛选 → 画像富化 → AI（千问+持仓审查）→ 信号入库
- AI 注入实盘+模拟盘持仓，审查止损/止盈/持有周期，交叉分析板块集中度
- 复盘上下文注入 AI prompt（市场情绪周期/主线/次线/退潮/情景推演/仓位约束）
- 炸板未回封检测（limit_pool 查 pool_type='炸板'），自动添加风险标签，AI 区分试盘 vs 出货
- 复盘趋势精选结构化输出（buy_zone/sl/tp），合并到 trade_signals 统一盯盘
- 千问优先，异常时 fallback DeepSeek

**盯盘进程（Watcher）：**
- 四层扫描：大盘状态 / 持仓风控+信号触发+复盘跟踪 / 板块热度 / 异动检测
- 集合竞价后推送一条汇总「📋 开盘决策」（持仓+买入区+待观察+集中度预警），替代之前两条分开的参考消息
- 智能市场模式识别：5 种模式（normal/v_reversal/dead_cat/one_sided/panic）分层决策
- 止损提醒循环：触发→5分钟→再提醒→用户回复"成交 CODE"/"再等 N CODE"
- 利润回撤止盈：三级分级保护（≥15%浮盈→保留60%, ≥10%→保留55%, ≥5%→保留50%），跟踪 _bought_watch.max_profit_pct
- 涨跌停处理：涨停买不了/跌停卖不了，下一轮继续监控
- 智能仓位计算：根据市场模式+板块趋势+买入区位置动态计算 0-20000 元
- 买入上下文分析：布林带位置、均线偏离、回踩支撑检测
- 买入后盯盘：健康/观察/被套/补仓机会 四类状态，每~10分钟推送
- 复盘票买入区间优先用 trade_signals 结构化数据（来自策略管线），其次 fallback MA 动态计算
- 风控引擎集成：黑名单+市场环境+集中度+时间止损
- AI 大盘波动分析：急涨急跌≥0.5% 时自动调用分钟级技术指标研判

**模拟盘（PaperTrader）：**
- 20 万初始资金，最多 5 只票，动态仓位
- 佣金万 0.85 最低 5 元，印花税万分之五（减半征收，卖出单边）
- AI 驱动换仓：持仓满时 AI 实时评估卖出谁换入谁（DeepSeek API，20s 超时）
- 主动换仓评估：每 15 轮扫描评估是否换仓，结合板块实时行情
- 自动执行买卖，Telegram 推送成交通知

**手动成交：**
- Telegram getUpdates 长轮询，支持"实盘/模拟盘 CODE 1000股 12.50"格式
- 名称→代码自动转换
- 买入后持续盯盘（Watcher 接管）

**收盘比对：**
- 模拟盘 vs 实盘成交配对，价差/滑点/独有分析

**风控引擎（RiskEngine）：**
- 开仓前：黑名单 → 市场环境仓位上限 → 集中度（单票20%/板块30%）
- 持仓巡检：日内熔断（日亏>3%全清）→ 止损 → 移动止盈 → 目标止盈 → 时间止损（持有>10天仍在亏损）
- 布林带(20,2) + ATR(14) 技术指标支持

**技术指标（indicators.py）：**
- MACD(12,26,9)：EMA 递推，Wilder 平滑对齐同花顺
- RSI(14)：Wilder 平滑
- KDJ(9,3,3)：RSV→K→D→J 递推
- 布林带(20,2)：upper/mid/lower/width/pct_b
- ATR(14)：True Range + Wilder 平滑
- MACD 金叉/死叉检测、顶/底背离检测

### 待完成

- 盘中实跑验证（cron 已部署，等真实行情数据到位后实测）
- 板块热度/异动检测阈值调优（骨架已写好）
- QMT 策略交易权限（等券商开通）
- 收盘双线比对自动化（目前手动 `python main.py compare`）

## 架构总览

```
                            ┌─ 复盘管线（不改动）────────────┐
                            │  collectors → AI报告 → tracker │
                            │                     ↓         │
                            │              第二天盯盘用       │
                            └────────────────────────────────┘

  ┌─ 交易管线 ────────────────────────────────────────────────────────┐
  │                                                                   │
  │  T-1 18:00 cron: review → strategy（自动串联）                     │
  │         │                                                         │
  │         ├─ MarketBreadth         市场宽度（涨跌家数/涨跌停/指数）   │
  │         ├─ _load_holdings()      加载持仓（实盘+模拟盘独立统计）    │
  │         ├─ _load_review_context()加载复盘上下文（情绪/主线/精选）   │
  │         ├─ TrendScreener.screen() 趋势筛选（强+稳健）              │
  │         ├─ ProfileBuilder.build() 画像富化（60天历史+指标+板块+炸板）│
  │         ├─ AIAdvisor.analyze()    AI分析（候选+持仓审查+复盘校准）  │
  │         └─ _build_review_signals()复盘精选→结构化OrderSignal       │
  │               ↓                                                   │
  │         trade_signals (status='pending', source=AI_ENHANCED/REVIEW)│
  │         Telegram 推送「📋 今日交易信号」                           │
  │                                                                   │
  │  T 9:00 cron: morning             早盘简报（AI 盘前校准）          │
  │                                                                   │
  │  T 9:24 cron: monitor → 等到 9:25 → 盯盘直到 15:00                │
  │         │                                                        │
  │         ├─ [第1轮 9:25] _send_opening_decision() 开盘决策汇总      │
  │         │                                                        │
  │         ├─ [每轮 60s]                                            │
  │         │   ├─ _check_market_state()    智能模式识别+分层决策      │
  │         │   ├─ _check_index_technicals() 分钟级MACD/RSI/KDJ拐点   │
  │         │   ├─ _check_positions()       止损/止盈/移动止盈/回撤止盈│
  │         │   ├─ _check_signals()         pending信号→买入区→通知   │
  │         │   ├─ _check_bought_signals()  买入后盯盘（状态+补仓）    │
  │         │   ├─ _check_review_picks()    复盘推荐跟踪（去重后）     │
  │         │   ├─ _check_sl_reminders()    止损提醒循环（5分钟）      │
  │         │   └─ _check_replies()         Telegram用户回复处理       │
  │         │                                                        │
  │         ├─ [每3轮] _refresh_market_snapshot() + _update_sector_trends()
  │         ├─ [每3轮] _check_abnormal()    异动检测                   │
	  │         ├─ [每15轮] _evaluate_swaps()    主动换仓评估（AI+板块）                   │
  │         └─ [每50轮] _check_sector_heat() 板块热度                  │
  │         ↓                                                         │
  │  信号触发 → Telegram 通知 → 模拟盘自动执行 + 实盘等用户确认        │
  │         ↓                                                         │
  │  用户回复成交 → ManualExecutor → trade_orders (account=real)       │
  │         ↓                                                         │
  │  15:00 收盘: pending→expired                                       │
  │         ↓                                                         │
  │  15:00 OrderComparator (手动)     收盘双线比对（实盘 vs 模拟盘）    │
  └───────────────────────────────────────────────────────────────────┘
```

## 目录结构

```
trading-system/
├── main.py                     # CLI 入口，12 个命令
├── analysis/                   # 分析层
│   ├── advisor.py              #   AI 顾问：双模型并行分析 + 持仓格式化 + 复盘上下文注入 + AI原始返回到reports
│   ├── strategy.py             #   盘前管线：筛选→AI→入库 + 持仓加载 + 复盘上下文 + 复盘精选结构化
│   ├── morning.py              #   盘前简报：AI 盘前校准
│   ├── tracker.py              #   推荐追踪：Excel+DB，日收益计算
│   ├── signals.py              #   StockScore/StockProfile/OrderSignal/HoldingReview/HoldingInfo/AccountSummary/ReviewContext
│   ├── screening/
│   │   ├── trend.py            #     趋势筛选：强趋势(MA5)+稳健(MA20)
│   │   ├── breadth.py          #     市场宽度：涨跌家数+大盘状态
│   │   ├── profiles.py         #     画像富化：60天历史+板块+RPS+指标+估值+炸板检测
│   │   ├── factors.py          #     19个因子 + 硬关卡 + 场景匹配
│   │   └── indicators.py       #     技术指标：MACD/RSI/KDJ/布林带/ATR/背离/交叉
│   ├── review/                 #   盘后复盘（analyzer/formatter/service/stats）
│   └── backtest/               #   回测框架（引擎/数据/指标）
├── trade/                      # 交易层
│   ├── monitor/                #   盯盘子系统
│   │   ├── watcher.py          #     主进程：时间管理+四层扫描（~1500行）
│   │   ├── review_picks.py     #     复盘推荐跟踪
│   │   ├── sector_heat.py      #     板块热度监控
│   │   └── abnormal.py         #     异动检测
│   ├── execution/              #   执行层
│   │   ├── manual.py           #     手动执行器+消息解析（Telegram回复）
│   │   ├── comparator.py       #     双线比对器（收盘）
│   │   ├── paper.py            #     模拟盘接口存根
│   │   ├── qmt.py              #     QMT执行器存根（等权限）
│   │   └── orders.py           #     下单接口存根
│   ├── paper/
│   │   └── trader.py           #     模拟盘自动交易（20万初始+费率+订单记录）
│   ├── portfolio/
│   │   ├── portfolio.py        #     持仓管理（开仓/平仓/快照/价格更新）
│   │   └── performance.py      #     绩效计算（夏普/回撤/波动率）
│   └── risk/                   #   风控引擎
│       ├── engine.py           #     统一编排（开盘前+持仓巡检）
│       └── rules/              #     规则模块
│           ├── stop_loss.py    #       止损/时间止损
│           ├── take_profit.py  #       止盈/移动止盈
│           ├── max_drawdown.py #       日内熔断
│           ├── concentration.py#       集中度检查
│           ├── market_env.py   #       市场环境+仓位上限
│           └── blacklist.py    #       黑名单+风险前缀检测
├── data/                       # 数据层
│   ├── collectors/             #   16个采集器（market/events/macro），带代理IP池
│   ├── live/quotes.py          #   QMT 行情客户端（自动处理代码后缀）
│   ├── readers/                #   DB 读取器（板块/涨停池/股票）
│   ├── processors/             #   数据加工（板块/涨停表现）
│   ├── schema.py               #   建表+幂等迁移
│   └── repo.py                 #   TradeRepository CRUD（带account过滤）
├── system/                     # 基础设施
│   ├── config/
│   │   ├── settings.py         #     统一配置
│   │   ├── trading_calendar.py #     交易日历
│   │   ├── prompts/            #     AI Prompt 模板
│   │   │   ├── ai_advisor.py   #       策略管线prompt（含持仓审查）
│   │   │   ├── review.py       #       复盘报告prompt
│   │   │   ├── morning.py      #       早盘简报prompt
│   │   │   └── telegraph.py    #       电报分析prompt
│   │   ├── proxy_config.py     #     代理配置
│   │   └── akshare_config.py   #     AkShare配置
│   ├── qmt/client.py           #   QMT HTTP 客户端（15个端点）
│   ├── services/               #   独立服务（监管函分析）
│   └── utils/                  #   工具（telegram/logger/function_calling/stock_tools/dns_bypass）
├── ops/                        # 运维
│   └── scheduler/              #   cron 脚本（start_listen/stop_listen/monitor/morning/...）
├── storage/                    # DB + 日志 + 缓存（gitignore）
└── tests/                      # 231 个测试
```

## CLI 命令

```bash
# 盘后（T-1 日 18:00 cron 自动执行）
python main.py review              # 采集→AI报告→Telegram，成功后自动调 strategy
python main.py review --analyze-only  # 同上，跳过采集（成功也会触发 strategy）
python main.py strategy            # 策略管线（通常由 review 自动调用，也可单独跑）

# 盘前
python main.py morning             #  9:00 cron — 早盘简报

# 盘中
python main.py monitor             #  9:24 cron → 9:25 启动盯盘，自管理生命周期到 15:00
python main.py listen              #  Telegram 消息监听（cron 管理生命周期）
python main.py collect --module news  # 盘中电报（每5分钟 cron）

# 盘后（手动）
python main.py compare             # 收盘双线比对
python main.py track               # 股票追踪统计

# 手动
python main.py trade --text '000001 1000股 12.50'  # 录入成交（默认实盘）
python main.py portfolio           # 持仓查询
python main.py test                # 配置检查
python main.py cleanup             # 周日清理
python main.py collect             # 全量采集
python main.py collect --module market  # 按模块采集
```

## 核心数据流

### 信号生成（strategy 命令）

```
StrategyPipeline.run(trade_date)
  │
  ├─ 步骤0: MarketBreadth.compute()
  │   └─ 输出: market_state = "普涨（涨3500/跌500，涨停80/跌停5，指数+1.20%）"
  │
  ├─ 步骤0.5: _load_holdings()
  │   ├─ 查 trade_orders 按 stock_code+account 汇总持仓
  │   ├─ 从 stock_basic 取最新行情+均线
  │   ├─ 从 trade_signals 取止盈止损
  │   ├─ 输出: [HoldingInfo × N] 每只票的成本/现价/盈亏/止损/止盈/持有天数/均线/T+1锁定
  │   └─ 输出: [AccountSummary] 实盘+模拟盘的各自总资产/现金/仓位/当日盈亏
  │
  ├─ 步骤0.8: _load_review_context()
  │   ├─ 解析复盘报告 markdown（提取三/四/五/七/八/十节）
  │   ├─ 解析 STOCKS JSON 块（<<<STOCKS>>>...<<<END>>>），含 buy_condition/stop_loss/target/role
  │   └─ 输出: ReviewContext（sentiment_cycle/main_lines/outlook/review_picks/monitor_conditions/仓位建议）
  │
  ├─ 步骤1: TrendScreener.screen()
  │   ├─ 数据源: stock_basic 表
  │   ├─ 过滤: 非ST, 非688, 市值>50亿, 排除白酒/银行/保险/证券, 涨跌停排除
  │   ├─ 强趋势: price>MA5 AND MA5>MA10>MA20 AND 偏离<5% AND 分离度>3%
  │   ├─ 稳健趋势: price>MA20 AND 偏离<10% AND MA5向上
  │   ├─ 因子评分: 19个因子（量价/资金/多日/RPS/板块/周线），≥2个标签保留
  │   └─ 场景匹配: 突破追涨/回踩MA5/MA10/MA20/底部反弹/趋势加速等
  │
  ├─ 步骤1.5: _load_legacy()
  │   └─ 加载昨日 status='expired' 的 AI 信号，构建 StockScore（标签="昨日遗留"）
  │
  ├─ 步骤2: ProfileBuilder.build()
  │   ├─ 60天OHLCV+主力+板块参照+RPS+估值+电报+技术指标
  │   ├─ 炸板检测: 查 limit_pool WHERE pool_type='炸板' → 添加风险标签 type="炸板未回封"
  │   └─ 富化为 StockProfile
  │
  ├─ 步骤3: AIAdvisor.analyze()
  │   ├─ _format_holdings() → holdings_text（注入持仓上下文）
  │   ├─ review_context.to_text() → review_text（注入复盘上下文到 prompt）
  │   ├─ 候选池 to_text() → candidates_text
  │   ├─ prompt = AI_ADVISOR_PROMPT.format(review_context, holdings_data, candidates_data)
  │   ├─ 千问(qwen3.6-plus)分析 → JSON:
  │   │   ├─ stocks: [{action, buy_zone, stop_loss, take_profit, reason, expected_trend...}]
  │   │   └─ holdings_review: [{action, new_stop_loss, new_take_profit, tomorrow_outlook...}]
  │   ├─ 千问异常 → fallback DeepSeek(deepseek-chat)
  │   ├─ trend_mode 不从 AI 取，从筛选结果直接回填
  │   └─ 只保留 action='buy' 的结果
  │
  ├─ 步骤3.5: _build_review_signals()
  │   ├─ 从 ReviewContext.review_stocks_raw 提取所有角色（主线龙头/中军/补涨/次线龙头/趋势票）
  │   ├─ 解析 buy_condition 文本提取参考价：正则 r'约(\d+\.?\d*)' 支持区间格式"约8.2-8.4元"
  │   └─ 生成 OrderSignal（source=REVIEW, buy_zone_min/max, stop_loss, take_profit）
  │
  └─ _save_signals() → TradeRepository.insert_signal()
      ├─ AI_ENHANCED 信号: _validate_signal() 过安全网（re-check 硬关卡）
      ├─ REVIEW 信号: 跳过安全网（来自复盘，已人工筛选）
      └─ status='pending', account='paper'
→ Telegram 推送「📋 今日交易信号」摘要
```

### 盯盘（monitor 命令）— 详细

```
Watcher.run()
  ├─ 9:25 前等待（cron 9:24 拉起，睡到 9:25）
  │
  ├─ 盘中循环（9:30-11:30, 13:00-15:00），每轮 60s
  │   │
  │   ├─ [初始化] _restore_positions()
  │   │   └─ 从 trade_orders 恢复持仓（按 stock_code 汇总买入）
  │   │
  │   ├─ [每轮] _get_watch_codes()
  │   │   └─ pending 信号票 + 复盘票 + 持仓票（合并去重）
  │   │
  │   ├─ [每轮] _get_realtime_prices()
  │   │   ├─ QMT /quotes 批量获取（自动 .SH/.SZ 后缀）
  │   │   └─ 同时缓存涨跌停价到 _limit_cache
  │   │
  │   ├─ [每3轮] _refresh_market_snapshot()
  │   │   ├─ QMT /all_quotes → 全市场 price/changePct
  │   │   └─ 用于板块热度+异动检测
  │   │
  │   ├─ [每3轮] _update_sector_trends()
  │   │   ├─ 按行业分组计算日内涨跌均值 → _sector_trend_history
  │   │   ├─ 行业实时统计 → _sector_stats（涨跌家数+平均涨跌幅）
  │   │   └─ 概念实时统计 → _concept_stats（同上，从 _concept_cache 聚合）
  │   │
  │   ├─ [第1轮] _send_opening_decision()
  │   │   └─ 集合竞价后推送一条汇总：持仓状态+买入区信号+待观察+板块集中度预警
  │   │     （替代之前分开的「📋复盘开盘参考」和「📋策略信号」两条消息）
  │   │
  │   ├─ [第一层 每轮] _check_market_state()
  │   │   ├─ _classify_market_pattern(): 五模式识别
  │   │   │   ├─ panic: 加速下跌+价格在日内低点 → 🚨暂停+建议减仓
  │   │   │   ├─ one_sided: 三段均价逐次走低 → ⚠️暂停
  │   │   │   ├─ dead_cat: 有反弹未过50%分位 → ⚠️暂不跟进
  │   │   │   ├─ v_reversal: 深跌后回升至50%分位以上 → 🔄恢复买入
  │   │   │   └─ normal: 正常模式 → 允许买入
  │   │   ├─ 传统阈值补充: 上证跌破MA20+跌幅>1% → 暂停
  │   │   ├─ 单边下跌检测: 价格在下1/3区间+重心下移+跌家数>2×涨家数
  │   │   └─ ≥0.5%波动: 触发 AI _analyze_index_fluctuation() 分钟级技术研判
  │   │
  │   ├─ [第一层 每轮] _check_index_technicals()
  │   │   └─ 分钟K线 MACD交叉/RSI极值/KDJ交叉/背离 → 技术拐点提醒
  │   │
  │   ├─ [第一层 每轮] _check_positions()
  │   │   ├─ 遍历 portfolio.positions
  │   │   ├─ T+1 检查: 今日买入不触发止损止盈
  │   │   ├─ 止损触发 → _handle_stop_signal()
  │   │   ├─ 止盈触发 → _handle_stop_signal()
  │   │   ├─ 移动止盈触发 → _handle_stop_signal()
  │   │   ├─ 利润回撤止盈 → _check_retracement_stop()
  │   │   │   ├─ 最高浮盈≥15%: 保留60%利润（回撤40%触发）
  │   │   │   ├─ 最高浮盈≥10%: 保留55%利润（回撤45%触发）
  │   │   │   └─ 最高浮盈≥5%:  保留50%利润（回撤50%触发）
  │   │   ├─ 更新 _bought_watch.max_profit_pct（即使T+1锁定也记录）
  │   │   └─ RiskEngine.check_positions() → 日内熔断+时间止损
  │   │
  │   ├─ [第一层 每轮] _handle_stop_signal()
  │   │   ├─ 跌停检查: 跌停不推送"卖出"
  │   │   ├─ Telegram 推送: 触发价+盈亏+确认指令格式
  │   │   ├─ 加入 _sl_reminders 队列
  │   │   └─ 模拟盘自动执行: PaperTrader.close()
  │   │
  │   ├─ [第一层 每轮] _check_signals()
  │   │   ├─ 遍历 pending 信号（含 AI_ENHANCED 和 REVIEW）
  │   │   ├─ 涨停检查 → 跳过
  │   │   ├─ _calculate_position_size(): 智能仓位计算
  │   │   │   ├─ panic/one_sided/dead_cat → 0（不买）
  │   │   │   ├─ v_reversal → base=10000, normal → base=20000
  │   │   │   ├─ 板块走强 → +20%, 板块走弱 → -40%
  │   │   │   └─ 买入区下沿1/3 → +10%, 上沿1/3 → -30%
  │   │   ├─ RiskEngine.can_open(): 风控检查
  │   │   ├─ _analyze_buy_context(): 布林带/均线/回踩支撑分析
  │   │   ├─ Telegram 推送: 买入信号+仓位理由+上下文分析
  │   │   └─ PaperTrader.try_buy(max_amount) + 加入 _bought_watch
  │   │
  │   ├─ [第一层 每轮] _check_bought_signals()
  │   │   ├─ 查 trade_signals WHERE status='bought'
  │   │   ├─ 止损/止盈检查（T+1前不触发）
  │   │   ├─ 利润回撤止盈检查（同 _check_positions 逻辑）
  │   │   ├─ _classify_holding_status(): 四类状态
  │   │   │   ├─ healthy: 盈利>2%
  │   │   │   ├─ watching: 小亏<2%
  │   │   │   ├─ trapped: 距止损<3%或亏损>5%
  │   │   │   └─ add_opportunity: 亏损但布林下轨/RSI超卖反弹
  │   │   └─ 每50轮推送持仓状态 + 补仓分析
  │   │
  │   ├─ [第一层 每轮] _check_sl_reminders()
  │   │   ├─ 5分钟未确认 → 重新推送
  │   │   ├─ "再等 N CODE" → waiting 状态，N分钟后恢复
  │   │   └─ "成交 CODE" → 移除提醒
  │   │
  │   ├─ [第一层 每轮] _check_review_picks()
  │   │   ├─ 优先用 _load_review_signal_zones() 从 trade_signals 取结构化买入区间
  │   │   ├─ fallback: ReviewPickMonitor MA10/MA20 动态计算
  │   │   ├─ 已在 trade_signals 中的 REVIEW 信号跳过（_check_signals 处理，防止重复）
  │   │   └─ 进入买入区间 → Telegram 通知 + 模拟盘执行
  │   │
  │   ├─ [第二层 每50轮] _check_sector_heat()
  │   │   └─ 板块涨跌排名 + 持仓板块标记
  │   │
  │   └─ [第三层 每3轮] _check_abnormal()
  │       └─ 急速拉升/逼近涨停/放量异动检测
  │
  └─ 15:00 收盘: portfolio.snapshot() + pending→expired
```

### 手动成交

```
信号触发 → Watcher 推送 Telegram:
  🔴 买入信号: 000001 平安银行
  现价 12.50 进入买入区间 12.20-12.80
  止损 11.80  止盈 14.00
  💰 仓位: 15000元 (大盘正常 板块走强 买入区下沿)
  ┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉┉
  📍 价格在买入区下沿，安全边际较高
  📊 布林带：偏下部运行，接近支撑
  📈 均线: MA5=12.30(上1.6%) MA20=11.80(上5.9%)
  ✅ 板块走强，顺势买入

用户回复 → MessageReceiver.getUpdates 拉取 →
  ManualExecutor.handle_user_reply(text):
    实盘 000001 1000股 12.50     → account='real', status='filled'
    模拟盘 000001 1000股 12.50   → account='paper', status='filled'
    000001 1000股 12.50          → account='real' (默认), status='filled'
    拓普集团 72.77 买了500股     → stock_name→code 查询, status='filled'
    000001 没成交                → status='rejected'

止损提醒回复：
  成交 000001                    → 确认已执行，停止提醒
  再等 10 000001                → 等待10分钟后再提醒

→ handle_user_reply():
  ├─ 名称自动转代码（查 stock_basic）
  ├─ 写 trade_orders (account 区分 paper/real)
  ├─ 找到对应 signal → trade_signals.status='bought'  # 注意: 'bought' 不是 'executed'
  └─ Telegram 回复确认消息
```

### 收盘比对（compare 命令）

```
OrderComparator.compare(trade_date)
  ├─ 读 trade_orders (account=paper + account=real)
  ├─ 按 stock_code 配对
  ├─ 算: 价差、滑点、模拟独有/实盘独有
  └─ format_report() → Telegram 推送
```

## 模拟盘（PaperTrader）

```python
# 费率：佣金万0.85 最低5元 + 印花税万分之五（减半征收，卖出单边）

INITIAL_CAPITAL = 200_000
POSITION_PCT = 0.10    # 默认每只10%，但 Watcher 会用 smart sizing 覆盖
MAX_POSITIONS = 5      # 最多5只持仓
SWAP_SCORE_GAP = 15    # 新信号比最弱持仓高15分才考虑换仓
COMMISSION_RATE = 0.000085
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.0005  # 万分之五（减半征收）

# try_buy() 流程：
#   1. 检查持仓上限 + 重复买入
#   2. 持仓满 → _try_swap() AI 实时换仓评估
#   3. 动态仓位: max_amount 优先，否则 total_value * POSITION_PCT
#   4. 按 price 算 volume（100股整数倍）
#   5. 扣佣金 → 算可用资金 → 不够则缩量 → <100股放弃
#   6. portfolio.open_position(sector_code=...) + 记录 trade_orders (account='paper')

# try_buy → _try_swap() 换仓流程：
#   1. _ai_evaluate_swap(candidates) → DeepSeek API 实时判断卖谁买谁
#   2. AI 失败 → _rule_swap_target() 规则兜底（AI审查 close > reduce > 分差）
#   3. 卖出 → 确认已平仓 → 再买入

# evaluate_swaps() 主动换仓（每15轮扫描触发）：
#   1. 收集买点区内候选信号
#   2. 构建板块上下文（行业+概念实时统计）
#   3. _ai_evaluate_swap(持仓+候选+板块上下文+大盘) → 换仓决策

# close() 流程：
#   1. 算金额 → 佣金(万0.85+最低5元) + 印花税(万分之五)
#   2. portfolio.close_position() + 记录 trade_orders
#   3. Telegram 推送成交通知

# avg_cost 含佣金：open_position 时 (price * volume + commission) / volume
```

## 风控引擎（RiskEngine）

```
RiskEngine.check_positions() 优先级:

  优先级4: 日内熔断
    daily_loss_ratio = abs(daily_pnl) / total_value
    if daily_loss_ratio > 0.03 → 清仓所有亏损持仓

  优先级5: 止损
    check_stop_loss(pos) → price <= stop_loss

  优先级6: 移动止盈
    check_trailing_stop(pos) → price <= highest_price * (1 - trailing_stop)

  优先级7: 目标止盈
    check_take_profit(pos) → price >= take_profit

  优先级8: 时间止损
    holding_days > 10 and pnl_pct < 0 → 触发

RiskEngine.can_open() 优先级:

  优先级1: 黑名单 → is_blacklisted(stock_code)
  优先级2: 市场环境 → portfolio.position_ratio + target_pct <= max_position(market_env)
     swing: 60%, bull: 80%, bear: 30%
  优先级3: 集中度 → 单票≤20%, 板块≤30%
```

## 关键技术细节

### 智能市场模式识别 (_classify_market_pattern)

基于日内价格轨迹三段分析（每段 >= 10 个数据点）：

| 模式 | 判断条件 | 决策 |
|------|----------|------|
| panic | 振幅>1.5% + 第三段加速下跌 + 价格在日内低点10%内 | 暂停买入，建议减仓 |
| v_reversal | 中段低谷 + 后段回升>0.3% + 价格>50%分位 | 恢复买入 |
| dead_cat | 反弹但未过50%分位 + 均价<前段 | 暂不跟进 |
| one_sided | 三段均价逐次走低 | 暂停买入 |
| normal | 以上都不满足 | 正常 |

### 涨跌停处理

```python
# 涨停幅度: 688/300 开头 → 20%, 其余 → 10%
_is_limit_up(code, price):  price >= limit_up * 0.995
_is_limit_down(code, price): price <= limit_down * 1.005

# 涨停 → 无法买入，跳过
# 跌停 → 无法卖出，继续监控
```

### Telegram 止损提醒循环

```
用户回复格式:
  成交 CODE        → 确认已手动执行，从 _sl_reminders 删除
  再等 N CODE      → 暂停N分钟后恢复提醒
  再等 N           → 暂停所有止损提醒N分钟

循环逻辑:
  pending 状态 + elapsed > 300s → 重新推送
  waiting 状态 → 等 wake_at 到达后恢复为 pending
```

### 布林带 + ATR

```python
calc_bollinger(closes, period=20, std_mult=2.0)
  → {upper, mid, lower, width(带宽%), pct_b(价格带内位置%)}

calc_atr(highs, lows, closes, period=14)
  → TR = max(H-L, |H-prevC|, |L-prevC|)
  → Wilder 平滑递推

# %b 使用: 0=下轨, 50=中轨, 100=上轨
# 价格沿上轨走 → 强趋势; 价格沿下轨走 → 弱趋势
# 带宽<5% → 缩口酝酿突破; 带宽>15% → 趋势扩张
```

## QMT API 实测（2026-05-26）

服务地址: `http://192.168.1.33:5000`（Windows 机器，xtdata 自动连接）

| 端点 | 速度 | 数据 | preClose | 限制 |
|------|------|------|----------|------|
| `/all_quotes` | 4.0s | 4818只, 959KB | ✅ | 每5分钟以上调用一次 |
| `/quotes?codes=` | 0.1s (7只) | 批量化 | ❌ | 必须带 .SH/.SZ 后缀 |
| `/quote/{code}` | <0.1s | 单只 | ✅ | 含 name + 5档盘口 |
| `/history?period=1m` | 0.25s | OHLCV | - | 日内分钟线 |
| `/history?period=1d` | 0.25s | OHLCV | - | 日K线 |
| `/tick` | - | - | - | 仅盘中，盘后返回"无数据" |
| `/sector/{name}` | 快 | 列表 | - | 只认"上证A股"，不认"银行" |
| `/calendar` | <0.1s | 时间戳 | - | - |

**关键限制：**
- 代码必须带后缀，`QuoteClient.get_realtime()` 已自动处理（同时试 .SH/.SZ/.BJ）
- `/quotes` 不含 preClose → 板块热度计算需缓存 `/all_quotes` 数据
- `/sector` 不支持行业名 → 板块监控用 `stock_basic.industry` 列
- 启发式后缀规则准确率 95.4%（221/4818 错在 000xxx 上海票）
- `/minute_kline` 端点不存在，已映射到 `/history?period=1m`

## 数据库

`storage/stock_market.db`（与 quant-system 共用 stock_basic / stock_tracker / cls_telegraph 等表）

### trading-system 独有表

| 表名 | 用途 | 关键字段 |
|------|------|----------|
| `trade_signals` | 交易信号 | signal_type, stock_code, buy_zone_min/max, stop_loss, take_profit, status(pending/bought/expired), account |
| `trade_orders` | 成交记录 | signal_id, stock_code, order_type(buy/sell), filled_price/volume, commission, account(paper/real) |
| `trade_portfolio_positions` | 每日持仓明细 | stock_code, volume, avg_cost(含佣金), current_price, pnl, pnl_pct, stop_loss, take_profit, holding_days, sector_code, account(paper/real) |
| `trade_portfolio_snapshots` | 每日快照 | total_value, cash, market_value, daily_pnl, drawdown, position_count, sector_exposure, account |
| `trade_holdings_review` | AI 持仓审查 | stock_code, action(close/reduce/hold), new_stop_loss, new_take_profit, tomorrow_outlook, reason, account |
| `trade_factor_values` | 因子值 | factor_name, factor_value, factor_zscore |
| `trade_strategy_metrics` | 策略表现 | win_rate, avg_profit, sharpe_ratio, max_drawdown |
| `market_breadth` | 市场宽度 | up_count, down_count, limit_up/down_count, index_change_pct, market_state |

### 共用表

- `stock_basic` — 全市场日线（stock_code 无后缀，含 industry/concepts/ma5/ma10/ma20/主力/量比/换手）
- `stock_indicators` — 技术指标（MACD/RSI/KDJ/布林带 bb_upper/mid/lower/width/pct_b）
- `stock_tracker` — 复盘推荐标的（push_date, star_rating, target_price, stop_loss）
- `cls_telegraph` — AI 结构化电报（ai_stocks/ai_summary/ai_sectors 等字段）
- `sector_hot_history` — 板块热度历史

### 账户字段

所有 trade_ 表都有 `account` 字段（幂等迁移添加，默认 'real'）：
- `paper` — 模拟盘（PaperTrader 自动执行）
- `real` — 实盘（用户手动确认后录入）

## 关键设计决策

1. **不下单** — 策略交易权限未开通。管线只发信号，模拟盘自动执行，实盘用户手动下单
2. **复盘/交易两条管线独立** — 唯一交汇点：stock_tracker → Watcher 做跟踪提醒。盘后 review 成功（AI 出报告+解析出股票池）自动触发 strategy，失败则跳过
3. **实盘/模拟盘账户分离** — trade_orders 用 account 字段区分。模拟盘初始 20 万自动执行，实盘用户 Telegram 确认后录入。两个账户独立结算（各自现金/市值/盈亏），但在 AI 持仓审查中同屏展示
4. **AI 双模型** — 千问主用(qwen3.6-plus)，DeepSeek 备选。策略管线只用千问（失败才 fallback），复盘双模型合并
5. **Watcher 无 DB fallback** — 拉不到 QMT 行情直接跳过该轮，不用 DB 收盘价
6. **启发式后缀映射** — 胜率 95.4%，000xxx 会错判（实际是 .SH 不是 .SZ），但 QuoteClient 同时试多个后缀，不影响正确性
7. **Telegram 接收用 getUpdates 长轮询** — 和 Open Claw MCP 用不同 bot token，不冲突。Watcher 每轮扫描调一次，也支持独立 `listen` 命令
8. **AI prompt 原则导向** — 不给止损止盈公式，给趋势判断原则让 AI 自主定价。系统标签标"仅供参考"，AI 以逐日走势数据为准。数据给全（10日 OHLCV+主力+MA 偏离+板块），判断交给 AI
9. **趋势分类纯数据驱动** — `_determine_mode()` 只看价格与均线位置关系，不看场景标签。5 日线强趋势：价格贴 MA5（bias5≤3%）且 MA5 陡峭向上；20 日线稳健：价格在 MA20 上方、近期回踩过 MA20 或偏离 MA20 不远
10. **硬关卡放松** — `ma5>ma10>ma20` 改为 `price>ma20 and ma10>ma20`，允许健康回踩的票进门
11. **市场分层智能决策** — 5 种模式识别（panic/v_reversal/dead_cat/one_sided/normal），不是机械的"跌幅>2%就暂停"，而是根据价格轨迹三段分析判断市场性质
12. **止损循环人工确认** — 止损触发后不自动执行实盘，而是推送提醒 + 5 分钟循环 + 支持"再等 N"延迟，确保人在回路上
13. **智能仓位替代固定比例** — 不再所有票买 10%，而是根据市场模式（0-20000）+ 板块趋势（±20-40%）+ 买入区位置（±10-30%）动态计算
14. **模拟盘费率对齐实际** — 佣金万 0.85 最低 5 元，印花税万分之五（减半征收）卖出单边。avg_cost 含佣金
15. **买入后盯盘不丢** — 买入后每 ~10 分钟推送持仓状态（健康/观察/被套/补仓机会），不再像以前买入就不管了
16. **复盘上下文注入 AI** — 盘后复盘报告的结论（情绪周期/主线/次线/退潮/情景推演/仓位建议）注入次日策略管线的 AI prompt，AI 据此调整 confidence 和选股方向
17. **复盘精选统一盯盘** — 所有复盘角色（主线龙头/中军/补涨/次线龙头/趋势票）统一转为结构化 OrderSignal（source=REVIEW），合并到 trade_signals，和 AI 信号用同一套 buy_zone/sl/tp 格式，Watcher 统一盯盘
18. **利润回撤止盈分级** — 不是简单的"回撤 X% 就卖"，而是根据最高浮盈分三级：≥15%→保留60%，≥10%→保留55%，≥5%→保留50%。浮盈越大的票给更多回撤容忍空间
19. **炸板区分试盘/出货** — 自动检测炸板未回封，添加风险标签。AI 根据量价和主力流向判断：缩量+主力未出逃→试盘（降 confidence 保留），放量+主力出逃→出货（直接 skip）
20. **开盘决策替代开盘参考** — 集合竞价后推送一条汇总（持仓+买入区+待观察+集中度预警），替代之前两条分开的参考消息，减少噪音
21. **AI 驱动换仓而非硬编码** — 持仓满时由 AI（DeepSeek）实时评估卖出谁换入谁，20 秒超时。规则兜底仅作 fallback（AI 审查 close > reduce > 分数差距）。主动换仓评估每 15 轮扫描触发，发送全部持仓+全部候选+板块上下文给 AI 综合判断
22. **板块上下文实时计算** — 行业和概念的涨跌幅/涨跌家数从 QMT 全市场快照实时聚合（`_update_sector_trends` 每 3 轮更新），不用 DB 的 `sector_industry`/`sector_concept` 表（盘中是昨天收盘数据）。概念映射来自 `stock_basic.concepts`（逗号/竖线分隔），`_concept_cache` 懒加载

## 注意事项 / 坑

- `system/config/settings.py` 的 `PROJECT_ROOT` 用了 `.parent.parent.parent`（相对于 system/config/ 三层上）
- **费率已修正**: `STAMP_TAX_RATE = 0.0005`（减半征收）, `DEFAULT_COMMISSION_RATE = 0.000085`
- QMT 被拆到三处：`data/live/quotes.py`（行情）、`trade/execution/orders.py`（下单存根）、`system/qmt/`（连接+日历）
- `trade_orders` 表 `get_orders_by_date()` 返回的列已包含 `account`（迁移添加，默认 'real'）
- 电报 AI 结构化：`TelegraphCollector._ai_structure_batch()` 的 pending 查询必须带 trade_date 过滤
- **CLS API 迁移 (2026-05-28)**：财联社废弃了 `/nodeapi/telegraphList`（返回 404），新端点为 `/api/cache?name=telegraph&rn=20&lastTime=<ts>`，数据格式不变（`data.roll_data`）。注意 `/api/cache` 返回 Brotli 压缩，已修改 `telegraph_collector.py` 的 `Accept-Encoding` 去掉 `br`
- **天启代理整点劣化 (2026-05-28)**：18:00 cron review 时行业/概念/个股三个代理采集器页1全部失败，8-10 分钟后手动重跑正常。根因是天启代理在整点附近返回劣质 IP。`review/service.py` 已加代理采集器失败后等 60s 重试一次的逻辑
- `sector_hot_history` 2026-05-19 前 rank 全为 0（旧版不写 rank），filter 已加 `rank > 0`
- 复盘 Prompt 交叉验证：第六节选股对应第四节主线/次线，第七节趋势票须来自当日热点板块
- `agent-browser` 在 cron 环境 PATH 不可用，`cls_digest_collector.py` 已加 fallback
- `stock_tracker` 表字段含 `star_rating`（不是 `score`），`target_price`/`stop_loss` 存在
- `MessageReceiver` 用 `TELEGRAM_REPORT_BOT_TOKEN`（唯一 bot），Bot B (Open Claw/AshareGet) 已弃用
- 推送路由（双 chat_id + 同 bot）:
  - 数据采集统计报告 → 仅 `TELEGRAM_PRIVATE_CHAT_ID`（私聊）
  - AI 分析报告 → `TELEGRAM_REPORT_CHAT_ID`（群）+ 私聊
  - 明天交易信号 + 持仓审查 → 群 + 私聊；持仓审查中实盘部分仅私聊可见
- 手动成交默认 account='real'，不再区分模拟盘/实盘。用户回复只需 `代码 股数 价格`
- `parse_reply` 支持股票名称（2-4 中文字符），`handle_user_reply` 会自动查 `stock_basic` 转代码
- cron 脚本日志路径格式: `storage/logs/<date>/tasks/cron_<task>.log`
- **cron 完整调度**:
  ```
  18 0 * * 1-5  review     → 复盘+AI报告 → 成功则自动跑 strategy → 推送「📋今日交易信号」
  0  9 * * 1-5  morning    → 早盘简报
  */5 9-17 * * 1-5 collect --module news → 盘中电报
  24 9 * * 1-5  monitor    → 盯盘进程（9:25开始扫描，9:25推送「📋开盘决策」）
  0  9 * * 0   cleanup    → 周清理
  ```
- **Telegram 推送时间线**:
  ```
  T-1 18:00 ~ 18:30  📋 今日交易信号（策略管线输出）
  T    9:00         早盘简报
  T    9:25         📋 开盘决策（集合竞价后汇总：持仓+买入区+待观察+集中度）
  T    9:30-15:00   盘中消息（🔴买入/⚠️止损/✅止盈/📊板块/🏭异动/📈技术拐点...）
  T   15:00         盯盘结束
  ```
- `system/utils/dns_bypass.py` 绕过 Shadowrocket/Surge/Clash 的 DNS 劫持（patch `socket.getaddrinfo`，检测 198.18.x.x 假 IP 后通过 dig @8.8.8.8 解析真实 IP），`main.py` 和 `analysis/review/analyzer.py` 启动时自动安装
- `system/utils/telegram.py` 的 `requests` 调用加了 `verify=False`，因为小火箭 HTTPS 解密（MITM）会导致证书验证失败
- 板块上榜次数 `hot_days` 从 `sector_hot_history`（综合打分）取数，不再用原始表涨幅排名。复盘时先查历史再保存今日再 +1
- 所有对话使用中文，文件修改直接执行，不新建 README/文档除非明确要求
- **Watcher 状态变量均为实例变量**（`_signal_alert_state`、`_review_alert_state`、`_prev_snapshot` 等），之前是类变量导致多个实例共享状态
- **`_bought_watch` 的 entry 来源**：① `_check_signals` 买入成功后加入，② `_check_bought_signals` 用 `setdefault` 从 DB 恢复的持仓自动初始化
- **`_check_bought_signals` 和 `_check_positions` 共享止损 key**（`{code}:sl`），`_handle_stop_signal` 通过 `_sl_reminders` 去重防止双重推送
- **`_load_holdings` 的价格查询用 `MAX(trade_date)`** 而非传入的 trade_date，因为盘前 stock_basic 只有昨天数据
- **`_handle_stop_signal` 的 timedelta 溢出**：已用 `datetime + timedelta(minutes=N)` 替代 `wake.replace(minute=wake.minute + N)`，避免分钟溢出
- **Watcher `_get_realtime_prices` 的 `lastPrice` 检查**：`if price is None` 而非 `if not price`，因为 0.0 是合法价格
