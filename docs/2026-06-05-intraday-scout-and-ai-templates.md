# 盘中机会发现引擎 + AI 模板场景化 设计方案

> 日期：2026-06-05
> 状态：设计完成，待实现

---

## 背景

2026-06-05 盘中复盘发现两个系统性问题：

1. **系统只防守不进攻**：所有买入候选来自前一日复盘，当日板块被隔夜利空打掉后，全天无任何进攻动作。航天装备 +5.3%、玻璃玻纤 +3.5%、电机 +3.2% 等强势板块完全错过。
2. **AI 模板一刀切**：所有 AI 调用使用同一套 prompt，不分追高/回踩/被套/突破场景，导致 AI 给出的建议泛化、不切实际（例如对趋势上升中的强势股反复建议「等回踩 MA20」）。

## 一、盘中机会发现引擎（Intraday Scout）

### 1.1 架构定位

```
引擎1（现有）: 复盘信号 → 早盘校准 → 趋势票等回踩
引擎2（新增）: 全市场快照 → 代码初筛 → 突破票不等回踩
```

双引擎并行运行，互补而非替代。

### 1.2 数据源

`market_snapshots` 表每轮（~15s）推送全市场约 4700 只股票的快照数据：

| 字段 | 含义 | 用途 |
|------|------|------|
| `code` | 股票代码 | 标识 |
| `change_pct` | 涨跌幅 | 涨幅区间过滤 |
| `price` | 当前价 | 价格位置判断 |
| `amount` | 成交额 | 量能对比（与前一轮比较） |

辅助数据：
- `_sector_stats`：板块涨跌幅 + 趋势方向（每 3 轮更新）
- `_industry_cache`：股票代码 → 申万行业映射
- `_snapshot_price_history`：个股近 10 分钟价格序列（Watcher 维护）
- `_market_breadth`：涨跌家数统计
- `_index_prices`：上证指数价格序列

**不依赖的数据（QMT collector 暂未推送）：**
- 个股日内量比（可用 amount 环比近似替代）
- 个股日内 MACD/KDJ/RSI（不需要，代码筛选不用技术指标）
- 大单净流向（暂无）

### 1.3 四层筛选管线

```
每 3 轮（~45s）触发一次

第一层：硬条件过滤（4700 → ~50）
─────────────────────────
① 涨跌幅 2% ~ 7%（有上行空间，非涨停已死）
② 非跌停、非 ST、非当日新股
③ 板块涨幅 TOP 15（板块共振，排除孤立异动）
④ amount > 前一轮 amount（放量确认）
⑤ 现价 > 前 5 分钟均价（价格动量向上）

第二层：多维打分排序（~50 → 10）
─────────────────────────
板块强度分（排名越靠前越高）      权重 35%
价格动量分（近 5 分钟涨幅）        权重 25%
量能分（amount 环比增幅）          权重 20%
大盘配合分（regime 允许买入时加分） 权重 20%
排序取 TOP 10

第三层：AI 二次过滤（10 → ~3）
─────────────────────────
使用「breakout」场景模板，异步提交 AI 判断：
- 题材是否有持续性（是否有消息面支撑）
- 是否诱多拉高出货（量价是否匹配）
- 板块是否已在退潮（龙头是否松动）
AI 返回：买入 / 观望 / 放弃 + 一句话理由

第四层：执行
─────────────────────────
AI「买入」→ 模拟盘小仓 3000~5000，推送 🔥 盘中机会
AI「观望」→ 仅推送预览，不自动买
同板块最多买 2 只（防集中度风险）
已持仓的同板块不再买（防过度集中）
```

### 1.4 与引擎 1 的协同规则

| 引擎 2 结果 | 引擎 1 状态 | 处理 |
|------------|------------|------|
| 高置信度买入 | 已在引擎 1 候选池 | 提升优先级，按引擎 1 仓位额度买 |
| 高置信度买入 | 引擎 1 已拒绝 | 仅当置信度 ≥ 80% 时重新考虑 |
| 高置信度买入 | 不在引擎 1 候选池 | 作为新信号生成，推送预览 |
| 中低置信度 | 任意 | 仅推送预览，不自动买 |
| 该票已在引擎 1 推送过 | 任意 | 跳过，不重复推送 |

### 1.5 文件结构

```
trade/monitor/intraday_scout.py   # 新建，Mixin 混入 Watcher
  ├── IntradayScoutMixin
  │   ├── _scout_intraday()       # 主入口，每 3 轮触发
  │   ├── _filter_candidates()    # 第一层硬筛
  │   ├── _rank_candidates()      # 第二层打分
  │   ├── _submit_scout_ai()      # 第三层 AI 异步提交
  │   └── _process_scout_ai()     # 第四层处理 AI 结果+执行
```

### 1.6 风险控制

- 引擎 2 总仓位上限：占总资金 20%（4 万/20 万），单只 3000~5000
- 冷却机制：同一板块 30 分钟内最多推送 2 次
- 止损统一走现有 `PositionRiskMixin`，不做特殊处理
- 大盘 `risk_level` 为 extreme/dangerous 时自动暂停引擎 2
- 日内累计亏损 > 2% → 引擎 2 暂停，仅保留引擎 1 持仓管理

---

## 二、AI 模板场景化

### 2.1 当前问题

所有 AI 调用共享同一套 system_prompt（「你是 A 股量化分析师」），user_prompt 是简单的 f-string 拼参数。无论追高、被套、突破还是止盈，AI 收到的上下文结构完全相同。

结果：
- 强势股突破时 AI 说「等回踩 MA20」（因为它只知道趋势回踩模板）
- 被套时 AI 说的和技术指标分析结果脱节
- 追高再判时 AI 缺乏盈亏比、板块持续性的量化输入

### 2.2 场景拆分

| 场景 key | 触发条件 | AI 角色 | 核心判断维度 |
|----------|---------|--------|------------|
| `pullback` | 复盘信号，价格接近/落入买入区 | 趋势跟踪分析师 | 支撑有效性、缩量程度、板块共振 |
| `breakout` | 引擎 2 发现的盘中强势股 | 动量交易分析师 | 量比持续性、突破有效性、是否诱多 |
| `chase` | 价格超出买入区上限 | 风险控制分析师 | 盈亏比、距区间偏差、追高历史胜率 |
| `trapped_exit` | 持仓浮亏 > 5% | 持仓风控分析师 | 反弹力度、阻力位、板块是否恶化 |
| `profit_exit` | 持仓浮盈 > 10% | 止盈策略分析师 | 趋势加速/衰竭、移动止盈位置 |

### 2.3 模板结构

每个场景一个 Python dataclass：

```python
@dataclass
class ScenarioTemplate:
    scenario: str              # 场景 key
    system_prompt: str         # AI 角色定义 + 核心判断原则
    user_prompt_template: str  # 带 {field} 占位的模板
    required_fields: list      # 模板必填字段
    max_tokens: int            # 返回长度
    temperature_note: str      # 给 AI API 的 temperature 建议
```

### 2.4 场景识别

在 AI 调用入口处，根据上下文自动选模板：

```python
def _detect_scenario(ctx: dict) -> str:
    if ctx.get("loss_pct", 0) < -5:
        return "trapped_exit"
    if ctx.get("pnl_pct", 0) > 10:
        return "profit_exit"
    if ctx.get("above_pct", 0) > 3:
        return "chase"
    if ctx.get("zone_type") == "breakout":
        return "breakout"
    return "pullback"
```

### 2.5 模板示例

#### pullback（趋势回踩）

```
system_prompt:
你是趋势跟踪分析师。核心原则：
1. 趋势票回踩均线是正常调整，缩量回踩是健康信号
2. 判断标准：是否缩量（量比<0.8）+ 是否在支撑位上方（MA5/MA10/MA20）
3. 放量跌破关键均线 = 趋势可能转弱，不建议买入
4. 回踩不破支撑且板块共振走强 = 买入信号
5. 开盘和尾盘回踩成功率高，盘中回踩需谨慎

user_prompt:
股票：{code} {name}，现价 {price}，买入区间 {buy_min}~{buy_max}
均线位置：MA5={ma5} MA10={ma10} MA20={ma20}
板块趋势：{sector_trend}，板块排名：{sector_rank}
日内：RSI6={rsi6} 量比≈{vol_ratio}
请判断：当前是否适合买入？给出买入/观望/放弃 + 不超过50字的理由。
```

#### breakout（盘中突破）

```
system_prompt:
你是动量交易分析师。核心原则：
1. 盘中强势股不需要等均线回踩——真正强势的票不会盘中回踩
2. 判断标准：量能持续放大（amount 环比递增）+ 板块共振 TOP 5 + 价格持续创新高
3. 开盘 30 分钟内急拉需警惕诱多
4. 尾盘 14:30 后突破可追，但仓位减半
5. 板块龙头松动（板块内涨幅最大股开板）立即放弃

user_prompt:
股票：{code} {name}，现价 {price}，涨幅 {change_pct}%
板块：{sector_name}，板块涨幅 {sector_pct}%，排名 {sector_rank}/{total_sectors}
量能变化：前一轮 {prev_amount} → 当前 {amount}（环比 {amount_delta:+.1%}）
近 5 分钟价格走势：{price_trend}
大盘环境：{market_env}（{risk_level}）
请判断：是否适合追入？给出买入/观望/放弃 + 不超过50字的理由。
```

#### trapped_exit（被套离场）

```
system_prompt:
你是持仓风控分析师。核心原则：
1. 被套后不要等解套——找最优离场点
2. 反弹到阻力位（布林中轨/MA60/BBI/成本价）就是减仓窗口，不要贪
3. 板块加速走弱时，任何反弹都应减仓，不要等更高
4. 大盘恐慌/极端风险时，反弹不可靠，优先保本
5. 从最低点反弹 >3% 后回落 >2% = 反弹失败，立即离场

user_prompt:
持仓：{code} {name}，成本 {cost}，现价 {price}，浮亏 {loss_pct}%
反弹情况：最低 {lowest}，反弹高点 {rebound_high}，当前距低点 +{rebound}%
最近阻力：{nearest_resistance}（{resistance_label}）
板块趋势：{sector_trend}
大盘环境：{market_env}（{risk_level}）
请判断：继续等反弹还是立即止损？给出一句话建议（不超过50字）。
```

### 2.6 改动范围

```
新建: trade/monitor/prompts/__init__.py     # 模板注册 + 场景识别
新建: trade/monitor/prompts/pullback.py      # 趋势回踩模板
新建: trade/monitor/prompts/breakout.py      # 盘中突破模板
新建: trade/monitor/prompts/chase.py          # 追高再判模板
新建: trade/monitor/prompts/trapped_exit.py   # 被套离场模板
新建: trade/monitor/prompts/profit_exit.py    # 止盈模板

修改: trade/monitor/watcher.py               # _ai_chase_opinion 使用模板
修改: trade/monitor/position_risk.py          # 被套 AI 调用使用模板
修改: trade/monitor/ai_queue.py               # 透传 system_prompt（当前已有参数，基本不需改）

注意: ai_queue._work() 当前硬编码了默认 system_prompt（第156行），
      改为：如果 task.system_prompt 非空则用它，否则用默认值。
```

---

## 三、实现计划

| 阶段 | 内容 | 文件 | 风险 |
|------|------|------|------|
| 1 | 模板系统：5 个场景模板 + 场景识别 + ai_queue 透传 | prompts/*.py, ai_queue.py | 低，只改 prompt 文本 |
| 2 | Watcher 接入模板：_ai_chase_opinion 根据场景选模板 | watcher.py, position_risk.py | 低 |
| 3 | 引擎 2 代码筛选层：硬筛 + 打分 + 排序 | intraday_scout.py | 中，新逻辑需要实测 |
| 4 | 引擎 2 AI 层 + 执行层：接入 breakout 模板 + 模拟盘买入 | intraday_scout.py | 中，涉及新买入路径 |
| 5 | 引擎 1+2 协同规则 + 风控集成 | watcher.py | 中，需要小心交互 |

阶段 1-2 可独立验收（调用 AI 看返回质量是否提升）。
阶段 3-5 需要在交易时段实测筛选效果。
