# 重构方案更新：验证优先，架构紧随

> 日期：2026-06-05
> 前置文档：[2026-06-04-pipeline-refactoring-plan.md](./2026-06-04-pipeline-refactoring-plan.md)
>
> 原方案的四层分离架构（Layer 0-4）仍是目标，本更新调整的是**路线和优先级**，不是架构方向。

---

## 自原方案以来的变化

### 新增模块

| 模块 | 行数 | 架构质量 | 说明 |
|------|------|---------|------|
| `trade/monitor/prompts/` | ~120 | ✅ 独立模块 | 场景模板系统，无 Watcher 依赖，可直接复用 |
| `trade/monitor/intraday_scout.py` | 460 | ⚠️ Mixin | 引擎2，可独立运行但绑定 Watcher state |

### 膨胀的现有文件

| 文件 | 原行数 | 现行数 | 新增内容 |
|------|--------|--------|---------|
| `position_risk.py` | 1711 | 1840 | 反弹失败状态机、被套 AI 调用、`_deep_rebound_improving` |
| `watcher.py` | ~1902 | 2055 | 消息去重、`_submit_scenario_ai`、scout 集成 |
| `market_state.py` | 2702 | 2702 | 仅 `_index_alerted_ma20` 初始化修复，无新增逻辑 |
| `buy_decision.py` | 2403 | 2403 | 无改动 |
| `health_checks.py` | ~660 | 666 | `deep_state.failed` 感知 |

### 验证过的结论

1. **模板系统做到了「新模块不依赖 Watcher」**——`prompts/` 只依赖自己的 `schemas.py`。这是 Engine 模式的可行证明。
2. **intraday_scout 作为 Mixin 写的体验**——功能能跑，但 `_scout_layer1_filter` 里到处是 `getattr(self, "_market_snapshot", {})`、`getattr(self, "_industry_cache", {})`。如果拆成独立 Engine，测试会容易得多。
3. **position_risk 的深跌逻辑在变复杂**——反弹失败检测 + 被套 AI 调用加进来后，`_check_positions` 单方法超过 200 行。这块是下次出 bug 的高危区。

---

## 调整后的路线

### 原则

1. **先验证再固化**——功能在实盘跑过确认有用，才提取为正式 Engine
2. **新功能不污染老代码**——能独立写的就不写进 Mixin（prompts 做到了，scout 没做到，要改）
3. **按痛点优先级**——最复杂、最容易出 bug 的模块先拆
4. **每次重构一个 Engine**——不改逻辑只搬家，每步跑测试验证

### 路线图

```
阶段1: 把已验证的新模块提取为 Engine（本周可做）
─────────────────────────────────────────────
1a. IntradayScout → ScoutEngine
    从 Mixin 改成独立类，构造函数收 Provider
    这是「从 Mixin 搬家到 Engine」的一次练兵
    风险低（新代码，没有历史包袱）

1b. prompts/ 加 re-export
    已经是独立模块，不需要改动
    只需确认 import 路径对上层友好


阶段2: 按复杂度拆 position_risk（下周）
─────────────────────────────────────────────
position_risk.py 里最值得拆的部分（按自包含程度排序）:

2a. _find_resistance_ceiling + _find_support_floor
    → TechnicalEngine.support_resistance(symbol, price)
    纯 DB 查询，输入股票代码+当前价，输出最近支撑/阻力位
    ~60 行搬家，改动范围最小

2b. _analyze_exit_context 的 DB 查询部分
    → TechnicalEngine.exit_signals(symbol, price, trend)
    输入股票代码+当前价+板块趋势，输出离场信号列表
    ~80 行搬家，决策逻辑保留在 Mixin

2c. _calc_exit_target
    → TechnicalEngine.exit_target(symbol, price, entry_price)
    纯 DB 查询，找最近阻力位
    ~50 行搬家

2d. _check_retracement_stop
    → 保留在 Mixin（这是纯决策，不涉及 DB/计算）

2e. 深跌反弹失败状态机
    → 保留在 Mixin（强依赖 _scan_count / _alert / paper_account）
    但 _deep_rebound_improving 可移到 TechnicalEngine


阶段3: 拆 buy_decision（下周）
─────────────────────────────────────────────
最值得拆的部分:

3a. _get_intraday_indicators (233行)
    → TechnicalEngine.intraday_snapshot(symbol)
    从 QMT 取分钟 K 线 → 算 RSI/MACD/KDJ
    这是重复计算的重灾区（position_risk 也有类似逻辑）

3b. _analyze_buy_context (231行) 的 DB 查询部分
    → TechnicalEngine.daily_snapshot(symbol)
    从 stock_indicators 取日线指标 → 返回 DailyTech dataclass

3c. _get_context_factors (131行) 的资金部分
    → MoneyFlowEngine.snapshot(symbol)（如果 QMT 数据可用）

3d. _evaluate_buy_decision / _evaluate_below_zone
    → 保留在 Mixin（纯规则判断）


阶段4: 拆 market_state（最后，最复杂）
─────────────────────────────────────────────
market_state.py 2702 行，是最肥的文件。但它是「计算+决策耦合最紧」的，
先拆计算层：

4a. _classify_market_pattern + 子方法
    → MarketEngine.classify_pattern(prices, highs, lows)

4b. _detect_micro_signals
    → MarketEngine.detect_micro_signals(prices, volumes, breadth)

4c. _check_index_technicals
    → MarketEngine.index_technicals(prices)

4d. _compute_key_levels / _get_index_baseline / _compute_breadth
    → MarketEngine 对应方法

决策层（_assess_regime / _update_scenario_engine / 告警推送）保留在 Mixin。


阶段5: 个股分析引擎（阶段2完成后即可开始）
─────────────────────────────────────────────
TechnicalEngine 就绪后，第一个 analyzer（technical）直接复用:

class TechnicalAnalyzer:
    def __init__(self, tech_engine: TechnicalEngine):
        self.engine = tech_engine

    def analyze(self, symbol: str) -> AnalysisResult:
        snap = self.engine.daily_snapshot(symbol)
        sr = self.engine.support_resistance(symbol, snap.ma20)
        # → 生成结论，不查 DB
```

---

## 与原始方案的区别

| | 原始方案（6/4） | 更新方案（6/5） |
|---|---|---|
| **顺序** | 先全拆完再写新功能 | 新功能先验证，再按痛点逐模块拆 |
| **intraday_scout** | 不存在 | 先写成 Mixin 验证，阶段1 拆成 Engine |
| **prompts** | 不存在 | 已独立，零改动 |
| **个股分析** | 阶段7（最后） | 阶段2 完成后即可并行开始 |
| **position_risk** | 一批全拆 | 分 5 个子步骤，每步可独立验证 |
| **风险** | 一次大重构 7-8 天，中间不可运行 | 每次搬一个方法，行为不变，随时可停 |

---

## 第一个要做的：ScoutEngine（阶段 1a）

这是最合适的起点：
- 新代码，没有历史耦合
- 逻辑已经验证过（虽然还没实盘跑）
- 从 Mixin → Engine 的改动是纯机械的：把 `self._xxx` 换成 `self.provider.xxx`
- 做完后就有了「如何拆一个 Engine」的模板，后续阶段照搬

具体改动：

```python
# 现在（Mixin）
class IntradayScoutMixin:
    def _scout_layer1_filter(self, snapshot):
        sector_stats = getattr(self, "_sector_stats", {}) or {}
        industry_cache = getattr(self, "_industry_cache", {}) or {}
        ...

# 改后（独立 Engine）
class ScoutEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def scan(
        self,
        market_snapshot: dict,
        sector_stats: dict,
        industry_cache: dict,
        positions: set[str],
        recently_sold: dict,
        regime_risk: str,
        daily_pnl_pct: float,
    ) -> list[ScoutCandidate]:
        """纯计算，返回候选列表。不调 _alert，不改 self 状态。"""
        ...

# Watcher 里只保留薄调用
class Watcher(...):
    def _scout_intraday(self):
        candidates = self.scout_engine.scan(
            self._market_snapshot,
            self._sector_stats,
            self._industry_cache,
            set(self.paper_account.positions.keys()),
            getattr(self, "_recently_sold", {}),
            getattr(self._regime, "risk_level", "safe"),
            ...
        )
        # AI 提交 + 执行保留在 Watcher（决策层）
        for c in candidates:
            self._scout_submit_ai(c)
```

---

## 总结

| 阶段 | 内容 | 预计 | 前提 |
|------|------|------|------|
| 1a | ScoutEngine 提取 | 0.5 天 | 无 |
| 1b | prompts re-export | 0 天 | 无 |
| 2a-d | position_risk 计算提取 | 1 天 | 阶段 1 完成 |
| 3a-b | buy_decision 指标提取 | 1 天 | 阶段 2 完成 |
| 5 | 个股分析 MVP | 1 天 | 阶段 2 完成 |
| 4 | market_state 计算提取 | 1.5 天 | 阶段 3 完成 |
| 3c-3d | buy_decision 资金 + 决策保留 | 1 天 | 阶段 4 完成 |

**总计约 5-6 天**，比原方案少了 2 天（因为 prompts 已完成、scout 只需要搬家、个股分析可以直接复用 TechnicalEngine）。

每一步只搬计算逻辑，不改变行为。每步跑测试 + 模拟盘验证。
