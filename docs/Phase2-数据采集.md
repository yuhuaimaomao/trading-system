# Phase 2: 迁移采集器

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans

**Goal:** 将 quant-system 的 15 个采集器逐步迁移到 trading-system 的 collectors/ 目录

**Architecture:** 代理基础设施先行（所有东财采集器依赖），然后按 market → events → macro 顺序迁移。每迁一个验证一个。

**Tech Stack:** Python 3.10+, akshare, requests, curl_cffi

---

### Task 1: 迁移代理基础设施 (collectors/proxy/)

**说明:** 代理池管理、UA伪装、请求重试、代理基类。这是所有东财采集器的依赖，必须先搬。

**Files:**
- Create: `collectors/proxy/proxy_manager.py` (from `utils/proxy_manager.py`)
- Create: `collectors/proxy/proxy_requester.py` (from `data/collectors/proxy_requester.py`)
- Create: `collectors/proxy/ip_stats.py` (from `utils/ip_stats.py`)
- Create: `collectors/proxy/proxy_base_collector.py` (from `data/collectors/proxy_base_collector.py`)

关键: 更新所有 import 路径——`from utils.proxy_manager` → `from collectors.proxy.proxy_manager`

### Task 2: 迁移行情采集器 (collectors/market/)

- `stock_basic_collector.py` — 全市场个股日线（最重要）
- `main_index_collector.py` — 八大指数
- `industry_board_collector.py` — 行业板块
- `concept_board_collector.py` — 概念板块
- `sector_stocks_collector.py` — 板块成分股
- `suspend_resume_collector.py` — 停复牌

### Task 3: 迁移事件采集器 (collectors/events/)

- `telegraph_collector.py` — 财联社电报
- `cls_digest_collector.py` — 财联社精华
- `lhb_collector.py` — 龙虎榜
- `limit_pool_collector.py` — 涨跌停池
- `strong_stock_collector.py` — 强势股
- `regulatory_letter_collector.py` — 监管函
- `stock_monitor_collector.py` — 重点监控
- `share_holder_change_collector.py` — 股东增减持
- `notice_collector.py` — 重要公告

### Task 4: 迁移宏观采集器 (collectors/macro/)

- `macro_collector.py` — 隔夜宏观

### Task 5: 验证 + 注册到 main.py collect 命令
