# 个股分析引擎 — 设计方案（2026-06-06 更新）

## 当前状态

Phase 1 已完成：技术面 + 资金面 + 板块归因，CLI 可用。

```
python main.py stock 600519   # 贵州茅台
python main.py stock 000001   # 平安银行
```

---

## 架构（与原方案对比）

### 原方案

```
analysis/stock/
├── StockAnalyzer 入口
├── schemas.py / registry.py / formatter.py
├── analyzers/ (8个)
│   ├── technical / money_flow / fundamental / institution
│   ├── event_driven / sector_attr / news_sentiment / history_pattern
└── providers/
    ├── base.py (DataProvider ABC)
    ├── db_provider.py / remote_provider.py
```

### 实际落地

```
analysis/stock/
├── __init__.py          StockAnalyzer（自动注册 + 名称解析 + quick/deep）
├── schemas.py           数据模型（StockAnalysisRequest/AnalysisResult/Report）
├── registry.py          注册表
├── formatter.py         三通道输出（CLI / Telegram / Dict）
└── analyzers/
    ├── __init__.py       BaseAnalyzer
    ├── technical.py      ✅ 技术面（均线/MACD/RSI/KDJ/布林）
    ├── money_flow.py     ✅ 资金面（主力/超大单/大单/市值）
    └── sector_attr.py    ✅ 板块归因（行业/概念）
```

**取消的部分：**
- `providers/` 目录 — `DataProvider` ABC 在当前只有一个 DB 数据源的情况下是过度抽象，直接复用 `data/readers/StockReader`。未来数据源多了再加不迟
- `remote_provider.py` — akshare 实时拉数据的需求未验证，跳过

---

## 维度进度

### ✅ 已完成（Phase 1）

| 维度 | 分析器 | 数据源 | 分析内容 |
|------|--------|--------|---------|
| 技术面 | `technical.py` | `stock_indicators` JOIN `stock_basic` | 均线排列、MACD多空、RSI超买超卖、KDJ、布林带位置 |
| 资金面 | `money_flow.py` | `stock_basic` | 主力净额/占比、超大单vs大单方向、MA5斜率、流通市值 |
| 板块归因 | `sector_attr.py` | `stock_basic` | 行业归属、概念板块覆盖度、概念集中度 |

### ❌ 未完成 — 缺数据

| 维度 | 需要的数据 | 当前状态 |
|------|-----------|---------|
| 基本面 | 财报（营收/利润/现金流/负债率）、行业研报（增速/格局/政策） | `stock_basic` 只有 PE/PB/市值，不够做分析 |
| 机构行为 | 机构评级明细、持仓变动、调研记录 | DB 有 `lhb_stocks`(龙虎榜3811行) 但只覆盖游资 |
| 事件驱动 | 解禁日程、增减持公告、分红方案 | 无数据 |
| 新闻舆情 | 多源新闻 + NLP 情感分析 | `cls_telegraph`(1368条) 仅电报，远不够 |
| 历史规律 | 多周期K线（周/月）、季节性、日历效应 | 数据够但分析逻辑复杂 |

### 新增分析器——数据到位后实现

当采集系统补充以下数据后，可以新增对应的 analyzer：

| 分析器 | 数据需求 | 采集方案 |
|--------|---------|---------|
| `fundamental.py` | ROE、营收增速、毛利率、负债率、现金流 | akshare `stock_financial_abstract` |
| `institution.py` | 机构评级(买入/增持/中性)、目标价、调研次数 | akshare `stock_rating_detail` |
| `event_driven.py` | 解禁日期+数量、减持计划、分红预案 | 已有 collector 框架，新增采集器 |
| `news_sentiment.py` | 多源新闻标题+内容，情感标签 | 多源采集 → AI 情感分析 |
| `history_pattern.py` | 60日K线形态、历史回踩成功率、季节性 | 已有数据，纯计算 |

---

## 数据源映射（更新）

| 数据 | 来源 | 状态 |
|------|------|------|
| 日线技术指标 | DB `stock_indicators` | ✅ 已有 |
| 均线 MA5/10/20 | DB `stock_basic` | ✅ 已有 |
| 主力资金 | DB `stock_basic`（main_force_net/ratio） | ✅ 已有 |
| PE/PB/市值 | DB `stock_basic` | ✅ 已有 |
| 行业/概念 | DB `stock_basic`（industry/concepts） | ✅ 已有 |
| 龙虎榜 | DB `lhb_stocks` | ⚠️ 有数据但只覆盖游资 |
| 财报 | akshare `stock_financial_abstract` | ❌ 需新增采集 |
| 机构评级 | akshare `stock_rating_detail` | ❌ 需新增采集 |
| 增减持/解禁 | akshare + 公告解析 | ❌ 需新增采集 |
| 新闻舆情 | 多源爬取 + AI 情感 | ❌ 需新增采集 |

---

## 输出通道

| 通道 | 实现 | 状态 |
|------|------|------|
| CLI | `formatter.to_cli()` | ✅ |
| Telegram | `formatter.to_telegram()` | ✅ |
| 管线 dict | `formatter.to_dict()` | ✅ |
| Telegram 交互 | `listen` 命令扩展 "分析 600519" | ❌ 未实现 |

---

## 未完成的集成点

1. **Telegram 交互**：`listen` 命令解析 "分析 600519" → 调 `StockAnalyzer.quick()` → 私聊回复
2. **Watcher 持仓诊断**：盯盘每 N 轮对持仓票调 `StockAnalyzer.quick()`，附在盯盘消息里
3. **盘前简报**：`morning.py` 加载信号票时调用 `StockAnalyzer.quick()` 做快速筛查
4. **复盘集成**：盘后 `review` 报告中嵌入个股技术分析结果
5. **多股票支持**：`StockAnalyzer.batch(["000001", "600519"])` 批量分析

---

## 设计决策（更新）

| # | 决策 | 原方案 | 实际 |
|---|------|--------|------|
| 1 | 架构模式 | 分析器注册表 | 同原方案 ✅ |
| 2 | Provider 层 | DataProvider ABC + db/remote | 取消，直接用 StockReader |
| 3 | 查询模式 | quick() / deep() | 同原方案 ✅ |
| 4 | 落地节奏 | 三阶段 | Phase1 完成，Phase2/3 等数据 |
| 5 | remote_provider | akshare 实时拉数据 | 跳过，等需求明确 |
| 6 | 数据目录 | `data/readers/` | `StockReader.get_daily_indicators/money_flow/support_resistance` |

---

## 下一步行动

### 必须做的
- [ ] 采集财报数据（akshare → DB）→ `fundamental.py`
- [ ] 采集机构评级数据 → `institution.py`

### 应该做的
- [ ] Telegram `listen` 集成 "分析 CODE"
- [ ] Watcher 持仓诊断集成

### 可选的
- [ ] 事件驱动数据采集 → `event_driven.py`
- [ ] 新闻舆情多源采集 → `news_sentiment.py`
- [ ] 历史规律分析 → `history_pattern.py`
- [ ] 多股票批量分析
