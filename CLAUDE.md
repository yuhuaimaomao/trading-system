# CLAUDE.md

trading-system — QMT 量化交易系统，独立于复盘系统。

## 项目定位

数据采集 → 趋势筛选 → AI 分析 → 盯盘 → 执行。
量化规则 + AI 增强，趋势票交易。

## 目录结构

```
trading-system/
├── data/                     # 数据层
│   ├── collectors/           #   离线采集（market/events/macro）
│   ├── live/                 #   实时行情（QMT quotes）
│   ├── proxy/                #   代理 IP 池
│   ├── readers/              #   DB 读取器
│   ├── processors/           #   数据加工
│   ├── schema.py             #   建表 SQL
│   └── repo.py               #   通用查询
├── analysis/                 # 分析层（盘前盘后）
│   ├── review/               #   盘后复盘
│   ├── screening/            #   趋势票筛选
│   ├── advisor.py            #   AI 选股顾问（双模型）
│   ├── morning.py            #   盘前简报
│   ├── tracker.py            #   推荐追踪
│   ├── backtest/             #   回测框架
│   └── signals.py            #   信号模型（StockScore/OrderSignal）
├── trade/                    # 交易层（盘中）
│   ├── monitor/              #   盯盘
│   ├── execution/            #   下单执行（manual/paper/qmt orders）
│   ├── portfolio/            #   持仓+业绩
│   └── risk/                 #   风控引擎
├── system/                   # 基础设施
│   ├── config/               #   配置（settings/proxy/calendar/prompts）
│   ├── qmt/                  #   QMT 连接
│   ├── services/             #   独立外部服务
│   └── utils/                #   通用工具（logger/telegram/stock_code）
├── ops/                      # 运维
│   ├── scheduler/            #   cron 脚本
│   └── scripts/              #   独立工具（cleanup/update_sector）
├── storage/                  # 持久化文件（DB/日志/缓存/PDF/报告）
├── tests/
└── main.py                   # CLI 入口
```

## 设计原则

- 风控优先于选股
- 量化规则必须可回测、可验证
- AI 定位：「从候选池精选 + 给操作条件」，不是「凭直觉选股」
- 不与复盘系统耦合
- 不做过度抽象，三个类似的行好过一个提前的抽象
- 注释只写 WHY，不写 WHAT

## 已知注意事项

- `system/config/settings.py` 中 PROJECT_ROOT 用了 `.parent.parent.parent`（相对于 system/config/）
- QMT 被拆到三处：data/live/quotes.py（行情）、trade/execution/orders.py（下单）、system/qmt/（连接配置）
- 数据库默认路径：`PROJECT_ROOT / "storage" / "stock_market.db"`
- 176 个测试，重构后全部通过
- 电报 AI 结构化方案：`docs/电报AI结构化方案.md`（已讨论，未实现）
- 趋势股板块过滤方案：`docs/趋势股板块过滤方案.md`（已讨论，待实现）

## 个人偏好

- 所有对话使用中文
- 文件修改、测试命令直接执行，无需反复确认
- 不要新建 README 或文档文件，除非明确要求
