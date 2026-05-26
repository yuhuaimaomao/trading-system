# Phase 4: 交易链路

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development

**Goal:** 构建交易系统的核心差异链路：趋势筛选 → AI 分析 → 模拟执行 → 盯盘 → 早盘简报

**Architecture:** 每层独立，通过 OrderSignal + DB 通信

---

### Task 1: 交易趋势筛选 (strategy/screening/trend.py)

借鉴 quant-system 双模式筛选（5日强趋势 + 20日稳健趋势），但独立实现——复盘筛选用来看盘，交易筛选用来做盘。

### Task 2: AI 选股顾问 (strategy/ai_advisor.py)

双模型并行分析（DeepSeek + 千问），输入候选池 StockScore 列表，输出 OrderSignal（含买卖区间+止损止盈+评分+理由）

### Task 3: 完善执行层 (execution/)

manual.py 对接 portfolio + Telegram，paper.py 实现模拟成交

### Task 4: 盯盘进程 (monitor/watcher.py)

cron拉起，自管理盘中生命周期。扫描候选池+持仓，触发条件推送到 Telegram

### Task 5: 早盘简报 (main.py morning)

隔夜宏观 + 候选池确认 + Telegram推送

### Task 6: 回测框架 (strategy/backtest/)

简单回测引擎骨架
