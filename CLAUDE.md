# CLAUDE.md

trading-system — QMT 量化交易系统。独立于复盘系统（`~/quant-system/`），共用数据采集但架构完全分离。

## 项目定位

趋势票中短线交易。量化规则做筛选 + AI 做精选和定价，人做最终决策和手动下单。**不做：** 自动下单、打板、T+0、高频。

## 架构总览

### 盯盘管线核心

```
数据(Collector/QMT) → 检测(Detect) → 情景(Scenario) → 决策(Decide) → 执行(Paper) → 消息(Telegram)
```

依赖单向，各模块独立。

### 目录结构

```
trading-system/
├── main.py                         CLI 入口
├── strategy/                       盘前策略线
│   ├── screening/                  选股因子（breadth/factors/profiles/trend）
│   ├── strategy_pipeline.py        趋势筛选管线 → AI → 信号入库
│   ├── morning.py                  早盘简报
│   └── advisor.py                  AI 选股顾问
├── trade/                          盘中盯盘线（检测→情景→板块→决策→风控→执行）
│   ├── core/                       主编排 + 运行时
│   │   ├── watcher.py              主编排器
│   │   ├── scan_state.py           运行时状态快照
│   │   ├── ai_queue.py             AI 异步队列
│   │   ├── health_checks.py        健康检查框架
│   │   ├── review_picks.py         盯复盘推荐
│   │   └── closeout.py             收盘处理
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
│   └── tracker.py                  早报追踪 + 准确率
├── audit/                          审计改进线（双轨：策略+盯盘）
│   ├── audit_pipeline.py           统一管线
│   ├── audit_base.py               审计基类
│   └── watcher_*.py / strategy_*.py
├── stock/                          个股分析线（独立能力 + 被各线 import）
│   ├── indicators.py               纯指标函数（MACD/RSI/KDJ/布林/ATR/形态）
│   ├── signals.py                  共享信号 dataclass（StockScore 等）
│   ├── analyzers/                  分析器（技术面/资金面/板块属性）
│   └── stock_*.py                  格式化/注册/schema
├── data/                           数据层（采集→加工→访问）
│   ├── collect/                    采集（live/market/events/macro/proxy）
│   ├── process/                    加工
│   ├── repo/                       CRUD
│   ├── readers/                    复杂查询
│   └── schema.py                   表结构 + 迁移
├── system/                         系统基础设施
│   ├── ai/                         AI 服务（多模型 + FC + 全部prompt）
│   ├── message/                    消息收发
│   ├── config/                     配置
│   ├── qmt/                        QMT 客户端
│   └── utils/                      工具（日志/DNS/股票代码）
├── tests/                          测试
├── ops/                            运维（scheduler/tools/pre_commit）
└── storage/                        运行时数据（DB/日志/PID/缓存）
```

## AI 调用规范

**唯一入口**：`from system.ai import ai`

```python
ai.chat(prompt, model="review", system_prompt="你是...")       # 同步
ai.chat_with_tools(msgs, model="morning")                     # FC多轮
ai.chat_with_tools_raw(msgs, model="review")                  # FC原始返回
ai.submit(key, prompt, model="watcher_chase", system_prompt=) # 异步
```

**多模型配置**（`.env`）：`AI_MODEL_REVIEW=qwen3.6-plus` `AI_MODEL_WATCHER=deepseek-v4-pro` 等。不设则回退到 `AI_MODEL`。

**Prompt 模版**：全部在 `system/ai/prompts/`，不允许散落其他地方。

**禁止**：直接 `new AIAnalyzer()`、硬编码模型名、内嵌 system_prompt 字符串。

## 数据库规范

- 所有数据访问走 `data/repo/` 包（`TradeRepository` 兼容入口，内部委托给拆分的 Repo）
- 业务代码**禁止**直接 `sqlite3.connect()`
- 热路径文件（watcher/buy_decision/position_risk）已全部消除直接 DB 调用

## 审计

- 所有审计功能在 `audit/`，文件前缀区分：`strategy_*` / `watcher_*`
- CLI: `python main.py audit [--domain strategy|watcher]` 或 `python main.py strategy-audit`

## 关键约定

1. **AI 模型**：默认 `deepseek-v4-pro`。复盘/早报/审计用千问
2. **实盘/模拟盘分离**：`trade_orders.account` 区分 paper/real。模拟盘自动执行，实盘手动确认
3. **Watcher 无 DB fallback**：拉不到 QMT 行情跳过该轮
4. **不下单**：策略交易权限未开通。模拟盘自动执行，实盘人工确认
5. **逐步骤异常保护**：每步独立 try/except
6. **买入后盯盘六类状态**：healthy/watching/at_risk/trapped/deep_trapped/add_opportunity
7. **深跌等待反弹**：亏损>7%且14:00前不立即止损
8. **利润回撤止盈三级**：≥15%/≥10%/≥5%，大盘极端多保留10%
9. **止损止盈统一函数**：`calc_unified_stop_loss/take_profit`（ATR+支撑阻力+策略类型+板块修正）
10. **买入区公式**：`buy_min=price*(1-zone_pct/100)`, `buy_max=price*(1+zone_pct/200)`（不对称，下方宽上方窄）
11. **情景引擎防坍缩**：每个情景概率≥2%（`_check_market_state`中）
12. **收盘清理**：`_cleanup_session_state()`清空13个运行时字典
13. **回踩买入区**：公式已修正（原来是 `*0.5` bug 导致买入区倒挂）
14. **缺sl/tp信号**：自动从支撑阻力位补算，不再死循环
15. **费率**：佣金万0.85最低5元，印花税万分之五卖出单边
16. **所有对话中文**，文件修改直接执行
17. **代码修改后主动更新 CLAUDE.md**

## 注意事项/坑

- **QMT 代码后缀**：`QuoteClient` 自动处理 .SH/.SZ
- **/quotes 不含 preClose**：需通过 `/quote/{code}` 获取
- **开盘恐慌扫止损**：开盘5分钟内亏损<5%跳过
- **insert_snapshot 用 INSERT OR REPLACE**：UNIQUE(trade_date, account)
- **FunctionCalling 工具注册**：`system/ai/stock_tools.py` TOOLS_DEFINITION + `function_calling.py` tool_functions
- **CLS API 已迁移**：`/nodeapi/telegraphList`→`/api/cache?name=telegraph`
- **dns_bypass**：绕过小火箭 DNS 劫持
- **telegram.py requests verify=False**：小火箭 HTTPS MITM
- **QMT 只用于行情**：下单功能不可用
- **stock_tracker 字段**：`star_rating`（不是 `score`）
- **build_state 引用传递**：性能优先，调用方不得修改 dict/list 内容
- **AIQueue 已委托给 system.ai**：不再自己管理线程
- **trade/scenario/templates/ 已删除**：模版迁移到 system/ai/prompts/
- **trade_signals.account 列 CREATE TABLE 缺失**：schema.py 中 `UNIQUE(trade_date, stock_code, account)` 引用了 account 列，但该列只在 ALTER TABLE 迁移中添加，CREATE TABLE 中未定义。2026-06-06 已修复：CREATE TABLE 直接包含 `account TEXT DEFAULT 'real'`
- **DB 迁移测试在**：`tests/test_schema_migration.py`，覆盖全量建表、幂等、增量补齐、ALTER TABLE、极端值 round-trip、索引/约束/外键

## 编码规范 — 写任何代码前必须遵守

> 详细版本见 skill: `coding-standards`（`/coding-standards` 加载）

### 注释

- **每 20-40 行至少 1 行注释**。2000 行文件不到 50 行注释就是不合格
- **每个函数必须有注释**，说明业务目的（不是功能描述）。每个 class 说明职责边界
- **每个魔法数字必须注释**，解释来源和原因
- **只写 WHY，不写 WHAT**。代码已经说了做什么，注释说为什么这样做
- **中文注释**

```
# ✅ 开盘 5 分钟内不触发止损，防止恐慌性扫止损（假突破占比 73%）
# ❌ 检查止损条件
```

### 文件命名

- **全项目文件名必须唯一**，有区分度
- **禁止** `utils.py`、`helper.py`、`common.py`、`base.py`、`misc.py` 等不传达信息的名字
- 用模块前缀或职责命名：`position_risk.py` ✅ / `utils.py` ❌

### 路径

新文件只能放这 7 个顶级目录，按业务领域对号入座：

| 目录 | 放什么 |
|------|--------|
| `strategy/` | 盘前策略管线、选股因子、早报 |
| `trade/` | 盘中盯盘（检测/情景/板块/决策/风控/执行） |
| `review/` | 盘后复盘、预测验证、统计 |
| `audit/` | 策略+盯盘双轨审计、改进建议 |
| `stock/` | 个股分析引擎、指标、信号模型 |
| `data/` | 数据采集、存储、查询 |
| `system/` | 基础设施（配置/AI/QMT/日志/消息） |

`tests/` 测试，`ops/` 运维脚本，`storage/` 运行时数据，`docs/` 文档。

### 日志

**禁止裸用 `logging.getLogger(__name__)`。** 必须用项目统一工具：

```python
# 顶层任务               → get_task_logger("name")       → logs/{date}/tasks/name.log
# 采集器                 → get_collector_logger("name")  → logs/{date}/collectors/name.log
# 系统工具               → get_core_logger("name")       → logs/{date}/core/name.log
# 盘中模块（已 setup_root_logger 的上下文）→ logging.getLogger(__name__) 可用
```

## 测试

```bash
python3 -m pytest tests/ -q          # 2028 tests
E2E_TEST_MODE=1 python3 tests/e2e/verify_comprehensive.py --day 2 --scans 240
```
