# 盯盘自审计闭环 — 设计文档

日期: 2026-05-31

## 目标

让盯盘管线具备自我学习和自我进化能力。每天收盘后自动审计当日决策质量，发现模式缺陷，生成改进建议，经用户审核后自动应用到代码。

核心原则：**审计不是给人看的统计报告，是系统自我进化的燃料。**

## 范围

- Watcher 的六类决策：市场模式分类、买入信号触发/过滤、止损/止盈触发时机、仓位分配、板块热度判断、异动检测
- 审计管线独立于复盘管线（复盘有 review_lessons/review_predictions 审计自己）

## 不在范围

- 复盘管线的选股/定价策略调整（已有 review_lessons 机制）
- 实盘手动下单决策的评价
- 回测

---

## 架构总览

```
Watcher 盘中
  ├─ 每轮决策时 → watcher_decision_log（决策瞬间全景快照）
  │
15:00 收盘
  │
  ├─ 15:05  RuleAuditor（Python 规则引擎）
  │   ├─ 读 decision_log + market_snapshots + index_snapshots
  │   ├─ 逐决策回溯验证（后续走势 vs 当时判断）
  │   └─ 输出 audit_findings（结构化发现）
  │
  ├─ 15:10  AIAuditor（千问）
  │   ├─ 输入：audit_findings + 当日决策时间线 + 板块联动 + 市场结构演变
  │   ├─ 串联因果链 + 发现新条件 + 提炼通用经验
  │   └─ 输出 watcher_improvements（可执行的改进建议）
  │
  └─ Telegram 推送改进卡片
      └─ 用户审核 → "应用 #N" → 自动合入代码
```

---

## 新增数据表

### watcher_decision_log — 盯盘决策日志

```sql
CREATE TABLE watcher_decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    ts TEXT NOT NULL,                    -- 决策时间 ISO格式
    decision_type TEXT NOT NULL,         -- regime_change/buy_trigger/buy_filter/
                                         --   stop_trigger/tp_trigger/position_size/
                                         --   exit_analysis/swap_eval/sector_alert
    stock_code TEXT,                     -- 相关股票，大盘级决策为 NULL
    decision_data TEXT NOT NULL,         -- JSON: 决策瞬间的关键上下文
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_wdl_date_type ON watcher_decision_log(trade_date, decision_type);
```

`decision_data` JSON 示例（不同类型字段不同）：

```json
// regime_change
{"pattern": "one_sided", "confidence": "high", "prev_pattern": "normal",
 "index_price": 3350.5, "index_change": -0.8,
 "up_count": 890, "down_count": 3520, "limit_up": 12, "limit_down": 45,
 "top_sectors": [{"name": "银行", "chg": -1.8}, {"name": "科技", "chg": 0.3}],
 "worst_sectors": [{"name": "白酒", "chg": -2.1}],
 "sector_divergence": false, "breadth_ratio": 0.25, "volume_trend": "shrinking",
 "index_ma5": 3380, "index_ma20": 3405}

// buy_filter
{"signal_id": 123, "stock_code": "000001", "entry_rule": "confirm",
 "reason_filtered": "entry_rule not standard in cautious regime",
 "price": 12.50, "buy_zone_min": 12.20, "buy_zone_max": 12.80,
 "zone_pos": 0.5, "sector_trend": "weakening", "market_regime": "cautious",
 "risk_level": "cautious", "position_ratio": 0.35}

// buy_trigger
{"signal_id": 123, "stock_code": "000001",
 "price": 12.50, "buy_zone_min": 12.20, "buy_zone_max": 12.80,
 "position_size": 12000, "entry_rule": "standard",
 "sector_trend": "strengthening", "market_regime": "normal",
 "bollinger_pct_b": 0.35, "rsi6": 48, "ma5_bias": 1.2}
```

### audit_findings — 规则审计发现

```sql
CREATE TABLE audit_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    finding_type TEXT NOT NULL,         -- regime_misclass/buy_missed/buy_bad/
                                        --   stop_early/stop_late/tp_early/tp_late/
                                        --   size_mismatch/sector_misjudge
    severity TEXT NOT NULL,             -- P0/P1/P2/P3
    stock_code TEXT,
    decision_log_ids TEXT,              -- JSON array of decision_log ids
    pattern_desc TEXT NOT NULL,         -- 发现描述
    evidence TEXT NOT NULL,             -- JSON: {decision_context, actual_outcome, deviation}
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### watcher_lessons — 盯盘经验教训库

```sql
CREATE TABLE watcher_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_type TEXT NOT NULL,          -- regime_detection/signal_filter/stop_timing/
                                        --   tp_timing/sizing/sector_heat/abnormal
    lesson_key TEXT NOT NULL,           -- 唯一标识（type + 特征哈希）
    lesson_content TEXT NOT NULL,       -- 教训描述
    trigger_conditions TEXT,            -- JSON: 触发条件
    occurrence_count INTEGER DEFAULT 1,
    first_date DATE NOT NULL,
    last_date DATE NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lesson_type, lesson_key)
);
```

### watcher_improvements — 改进建议

```sql
CREATE TABLE watcher_improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    improvement_type TEXT NOT NULL,     -- param_tune/rule_add/rule_modify/watch_add
    target_module TEXT NOT NULL,        -- market_state/buy_decision/position_risk/...
    target_param TEXT,                  -- 具体参数名/方法名
    suggested_change TEXT NOT NULL,     -- 改进描述
    code_diff TEXT,                     -- 建议的代码 diff（AI 生成）
    rationale TEXT NOT NULL,            -- 理由
    evidence_ids TEXT,                  -- JSON array of audit_finding ids
    status TEXT DEFAULT 'pending',      -- pending/applied/ignored/superseded
    applied_date DATE,
    effectiveness_check TEXT,           -- 下次审计验证结果
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## RuleAuditor（规则审计引擎）

纯 Python，不做 AI 推理，只做数据对照。每个维度独立可测。

### 审计维度

**1. 市场模式回溯验证**

对每次 `regime_change`，取变更后 30 分钟的 index_snapshots：
- 判 `one_sided`（单边下跌）→ 后续 30min 跌幅是否 > 前 30min？重心是否继续下移？
- 判 `v_reversal` → 后续是否持续回升？30min 后价格是否在当日 50% 分位以上？
- 判 `dead_cat` → 后续反弹是否失败（回落到反弹起点下方）？
- 判 `normal` → 后续是否确实无极端走势？

输出：模式 vs 实际走势的吻合度。不是 binary 对/错，而是方向一致/方向相反/方向不明。

**2. 买入信号质量**

对每次 `buy_trigger`：
- T+0：以收盘价 vs 买入价计算当日盈亏
- T+N：需要后续交易日数据（先做 T+0，后续扩展）

对每次 `buy_filter`：
- 反事实推演：如果当时买入，收盘盈亏多少？
- 重点标记「过滤掉但收盘涨 > 3%」的信号 → 可能过滤过严

按 `entry_rule` 分组统计：standard/pullback/confirm/range_boundary 各等级的命中率。

**3. 止损触发时机**

对每次 `stop_trigger`：
- 触发后 30min：价格继续跌（真止损）还是反弹（假摔）？
- 如果反弹且超过止损价 2%+，标记为「过早止损」
- 如果继续跌且跌幅 > 止损价的 3%+，说明止损设得太宽

同时检查：触发时距止损价还有多远？如果刚触及就弹回，可能是止损位设得太精确（没给噪音容忍）。

**4. 止盈触发时机**

对每次 `tp_trigger`：
- 触发后 30min：价格继续涨（卖飞）还是回落（卖对）？
- 触发后到收盘：最高价 vs 止盈价差多少？

**5. 仓位分配效率**

按 position_size 将持仓分三组（大/中/小），对比各组平均盈亏：
- 如果大仓位组平均亏损而小仓位组平均盈利 → 仓位分配方向反了
- 统计仓位最高的票和收益最高的票是否重叠

**6. 板块热度准确度**

对 `sector_alert`，对比收盘时板块实际排名和盘中判断。

### 输出

所有发现写入 `audit_findings`，附带完整 evidence JSON（含决策时刻数据和事后验证数据）。筛选 severity=P0/P1 的发现传给 AIAuditor。

---

## AIAuditor（AI 审计引擎）

### 定位

RuleAuditor 输出的是"点"——单个决策的偏差。AIAuditor 的工作是把点串成"链"——发现反复出现的模式和因果。

### Prompt 结构

```
你是一个量化交易系统的盯盘审计 AI。

## 今日决策时间线
{decision_timeline}  — 按时间排列的所有 decision_log，附带关键快照

## 规则审计发现
{rule_findings}      — RuleAuditor 输出的 P0/P1 发现

## 市场结构演变
{market_structure}   — 从 market_snapshots + sector_snapshots 提取的
                        板块轮动/宽度变化/量能变化 时间序列

## 历史教训
{watcher_lessons}    — 历史同类教训，用于对照

## 当前策略参数
{current_params}     — 各模块当前阈值/系数/权重

请完成以下分析：

1. **因果串联** — 把分散的发现串成因果链。
   "你在 X 时判了 A，是因为当时 Y 指标显示 B，
    但实际上 Z 板块在悄悄 C，导致 D 结果。
    这种模式今天出现了 N 次。"

2. **模式提炼** — 有没有 RuleAuditor 单个发现看不出的规律？
   "今天的 3 次误过滤都发生在 cautious 模式下、
    板块趋势刚由弱转强的拐点时刻。"

3. **改进建议** — 对每条建议：
   - 定位到具体模块和方法
   - 描述新增什么条件/调整什么参数
   - 给出理由和预期效果
   - 判断 auto_applicable（param_tune 类可直接应用，rule 类需审核）

4. **经验入库** — 提炼模式级教训写入 watcher_lessons
   （与已有教训合并：lesson_key 相同则 occurrence_count+1）
```

### 输出格式

AI 以 JSON 结构化输出：

```json
{
  "causal_chains": [
    {"pattern": "...", "events": [...], "root_cause": "...", "impact": "..."}
  ],
  "new_patterns": [
    {"description": "...", "frequency": 3, "conditions": {...}}
  ],
  "improvements": [
    {
      "type": "rule_add",
      "target_module": "market_state",
      "target_method": "_classify_market_pattern",
      "suggested_change": "one_sided 判定时增加 sector_divergence 检查...",
      "code_diff": "```diff\n...\n```",
      "rationale": "...",
      "auto_applicable": false
    }
  ],
  "lessons": [
    {"type": "signal_filter", "key": "cautious_sector_reversal_miss",
     "content": "板块由弱转强拐点时刻，cautious 模式下 confirm entry_rule 容易误杀",
     "trigger_conditions": {...}}
  ]
}
```

---

## 改进应用闭环

### 应用流程

1. AIAuditor 输出 → 写入 `watcher_improvements` → 生成 Telegram 改进卡片
2. 卡片包含：类型/模块/严重度/发现描述/改进建议/证据链接
3. 三个操作按钮：`[应用] [忽略] [稍后]`
4. 用户回复 "应用 #N"：
   - `param_tune`：更新对应模块的常量/默认值
   - `rule_add/rule_modify`：插入新条件到对应方法
   - `watch_add`：注册新的盯盘维度，加入 _scan() 循环
5. 记录 `status='applied'` + `applied_date`

### 效果追踪

后续审计时 AI 会引用已应用的改进：
- 同类 finding 减少 → "改进 #7 生效，同类误判从 3→1 ✅"
- 同类 finding 未变 → "改进 #7 未见效，建议回滚或调整"
- 带来新问题 → "改进 #7 虽然减少了误判，但增加了漏判"

---

## Watcher 改动点

最小侵入，每个 `_check_xxx()` 方法关键决策处加一行日志：

| 位置 | 决策类型 | 写入时机 |
|------|---------|---------|
| `_check_market_state` → regime 变化时 | `regime_change` | pattern 切换时 |
| `_check_signals` → 触发买入 | `buy_trigger` | try_buy 成功后 |
| `_check_signals` → entry_rule 过滤 | `buy_filter` | 被过滤时 |
| `_check_positions` → 止损触发 | `stop_trigger` | _handle_stop_signal 调用前 |
| `_check_positions` → 止盈触发 | `tp_trigger` | _handle_stop_signal 调用前 |
| `_calculate_position_size` | `position_size` | 每次计算 |
| `_analyze_exit_context` | `exit_analysis` | 被套分析输出时 |
| `_evaluate_swaps` | `swap_eval` | AI 换仓决策时 |
| `_check_sector_heat` | `sector_alert` | 板块预警推送时 |

---

## 文件规划

```
trade/monitor/
├── audit/
│   ├── __init__.py
│   ├── rule_auditor.py       # RuleAuditor 规则引擎
│   ├── ai_auditor.py         # AIAuditor（千问调用 + prompt）
│   ├── decision_logger.py    # 决策日志写入（Watcher mixin）
│   ├── improvement_applier.py# 改进建议自动应用
│   └── prompts.py            # AI 审计 prompt 模板
├── watcher.py                # 混入 DecisionLoggerMixin
...

system/config/prompts/
└── watcher_audit.py          # AIAuditor 的 prompt（或放 trade/monitor/audit/）

system/config/settings.py     # 新增审计相关配置
```

## CLI 命令

```bash
python main.py audit               # 手动触发完整审计
python main.py audit --rule-only   # 仅规则审计
python main.py audit --ai-only     # 仅 AI 审计（前提：已有 audit_findings）
python main.py audit --apply N     # 应用第 N 条改进建议
python main.py audit --list        # 列出待处理的改进建议
```

---

## 配置项

```python
# settings.py 新增
AUDIT_ENABLED = True              # 是否启用审计（cron 收盘后自动跑）
AUDIT_AUTO_APPLY_PARAM = True     # param_tune 类改进是否自动应用（不等待审核）
AUDIT_AI_MODEL = "qwen3.6-plus"   # AI 审计模型
AUDIT_RETENTION_DAYS = 90         # decision_log 保留天数
```
