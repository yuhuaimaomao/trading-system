# Phase 3: 迁移复盘模块

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development

**Goal:** 将 quant-system 的复盘模块迁移到 trading-system 的 review/ 目录

**Architecture:** readers（DB查询）→ processors（数据处理）→ analyzer（AI分析）→ formatter（格式化）→ service（编排+推送）

---

### Task 1: 迁移 DB Readers (review/readers/)

从 `data/readers/` 迁入，纯静态方法，无外部依赖（只读 DB）。

### Task 2: 迁移 AI 分析 (review/analyzer + formatter)

AIAnalyzer（API调用）+ ReviewAnalyzer（章节编排）+ review_formatter（格式化输出）

### Task 3: 迁移 Processors + Service

zt_performance_processor, sector_info_processor + ReviewService（编排+Telegram推送）

### Task 4: 注册到 main.py review 命令
