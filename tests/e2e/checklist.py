# -*- coding: utf-8 -*-
"""E2E 测试完整清单 — 可执行规格。

每条 CheckItem 定义一个需要验证的变量/状态，包含：
  - 如何从场景输入独立计算预期值
  - 何时检查（每轮/触发时/收盘时/跨日）
  - 断言类型和容差

总计 ~40,000+ 条断言，覆盖 _scan() 的每一步全部状态变量。
"""

from dataclasses import dataclass, field
from typing import Callable, Optional, Any
from enum import Enum


class When(Enum):
    EVERY_SCAN = "every_scan"           # 每轮都检查
    EVERY_3RD = "every_3rd"             # 每 3 轮
    EVERY_15TH = "every_15th"           # 每 15 轮
    EVERY_50TH = "every_50th"           # 每 50 轮
    SCAN_0 = "scan_0"                   # 第一轮（开盘决策）
    SCAN_1 = "scan_1"                   # 第二轮
    ON_SIGNAL_TRIGGER = "on_signal_trigger"     # 信号触发时
    ON_STOP_TRIGGER = "on_stop_trigger"         # 止损/止盈触发时
    ON_BUY_EXECUTE = "on_buy_execute"           # 买入执行后
    AT_CLOSE = "at_close"                       # 收盘时
    CROSS_DAY = "cross_day"                     # 跨日验证


class AssertType(Enum):
    EXACT = "exact"                     # 完全相等（int/bool/str）
    FLOAT = "float"                     # 浮点容差
    FLOAT_TIGHT = "float_tight"         # 严格浮点容差 0.001
    RANGE = "range"                     # 范围检查
    NOT_NONE = "not_none"               # 非空
    NOT_EMPTY = "not_empty"             # 非空字符串/列表
    CONTAINS = "contains"               # 包含子串
    TYPE_CHECK = "type_check"           # 类型检查


@dataclass
class CheckItem:
    """一条测试检查项."""
    id: str                                     # 唯一 ID，如 "A003"
    category: str                               # 大类 A-G
    variable: str                               # 变量路径，如 "_regime.pattern"
    description: str                            # 中文描述
    when: When                                  # 检查时机
    assert_type: AssertType                     # 断言类型
    tolerance: float = 0.02                     # 浮点容差
    # 独立计算预期值的函数，签名: (expected_engine, scan, watcher_snapshot) -> expected_value
    compute_expected: Optional[Callable] = None
    # 从 watcher 提取实际值的函数，签名: (watcher) -> actual_value
    extract_actual: Optional[Callable] = None
    # 额外说明
    notes: str = ""


# ══════════════════════════════════════════════════════════════════════
# 完整检查清单 — 按 _scan() 执行顺序组织
# ══════════════════════════════════════════════════════════════════════

CHECKS: list[CheckItem] = []

def check(**kwargs) -> CheckItem:
    """注册一条检查项."""
    item = CheckItem(**kwargs)
    CHECKS.append(item)
    return item


# ══════════════════════════════════════════════════════════════
# A. 大盘状态 & 指数 (每轮 ~25 个校验)
# ══════════════════════════════════════════════════════════════

# --- A1. 指数价格序列 ---
check(id="A001", category="A", variable="_index_prices 长度",
      description="每轮 _index_prices 长度 = scan+1",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A002", category="A", variable="_index_prices[-1]",
      description="最新指数价格 = 场景预定义值",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.05)

check(id="A003", category="A", variable="_index_high",
      description="日内最高价 = max(_index_prices)",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.1)

check(id="A004", category="A", variable="_index_low",
      description="日内最低价 = min(_index_prices)",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.1)

check(id="A005", category="A", variable="_market_turnovers 长度",
      description="每轮成交额追加一条",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A006", category="A", variable="_last_index_quote",
      description="最近一次指数报价非空",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

# --- A2. 市场模式 (16 种) ---
check(id="A010", category="A", variable="_regime.pattern",
      description="市场模式分类正确（16 种之一）",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A011", category="A", variable="_regime.risk_level",
      description="风险等级: safe/cautious/dangerous/extreme",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A012", category="A", variable="_regime.allow_buy",
      description="是否允许买入",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A013", category="A", variable="_regime.position_mult",
      description="仓位倍数 (0.0-1.0)",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.05)

check(id="A014", category="A", variable="_regime.entry_rule",
      description="入场策略: standard/pullback/confirm/range_boundary/next_day/none",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A015", category="A", variable="_regime.stop_mult",
      description="止损宽度倍数 (0.7-1.5)",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.05)

check(id="A016", category="A", variable="_regime.urgent_action",
      description="紧急动作（panic 时有值）",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

# --- A3. 大盘告警状态 ---
check(id="A020", category="A", variable="_index_alerted_downtrend",
      description="结构性下跌告警状态",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A021", category="A", variable="_volume_alerted_divergence",
      description="量价背离告警状态",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A022", category="A", variable="_max_drawdown_alerted",
      description="最大回撤告警已触发标记",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A023", category="A", variable="_index_last_fluctuation_price",
      description="上次波动分析时的指数价格（≥0.5% 波动时更新）",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.1,
      notes="非波动轮为 0.0")

# --- A4. 指数技术指标 ---
check(id="A030", category="A", variable="_index_tech_state.macd_cross",
      description="分钟 MACD 交叉信号: golden/death/None",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A031", category="A", variable="_index_tech_state.rsi6_zone",
      description="RSI6 区域: overbought/oversold/normal",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A032", category="A", variable="_index_tech_state.rsi12_zone",
      description="RSI12 区域: overbought/oversold/normal",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A033", category="A", variable="_index_tech_state.kdj_cross",
      description="KDJ 交叉信号: golden/death/None",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A034", category="A", variable="_index_tech_state.kdj_j_zone",
      description="KDJ J 值区域: overbought/oversold/normal",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="A035", category="A", variable="_index_tech_state.divergence",
      description="背离信号: divergence_up/divergence_down/None",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

# --- A5. 回撤熔断 ---
check(id="A040", category="A", variable="portfolio.drawdown",
      description="日内最大回撤值",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.005)

check(id="A041", category="A", variable="drawdown_halt 生效",
      description="回撤 >= MAX_DRAWDOWN_PCT 时阻断买入",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="drawdown>=阈值时 regime_ok 应为 False")


# ══════════════════════════════════════════════════════════════
# B. 情景引擎 (每轮 ~12 个校验)
# ══════════════════════════════════════════════════════════════

check(id="B001", category="B", variable="_scenario_probs 存在",
      description="8 个情景概率字典存在",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

check(id="B002", category="B", variable="_scenario_probs 键完整",
      description="包含全部 8 个情景键",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="normal_stable/developing_uptrend/developing_downtrend/accelerating_down/accelerating_up/potential_reversal_up/potential_reversal_down/dead_bounce")

check(id="B003", category="B", variable="_scenario_probs 和为 1",
      description="8 个概率之和 ≈ 1.0",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.02)

check(id="B004", category="B", variable="_scenario_probs 主情景",
      description="概率最高的情景名称",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="B005", category="B", variable="_scenario_prev_outlook",
      description="上一轮情景展望对象存在 (scan>=5)",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE,
      notes="前 5 轮可为 None")

check(id="B006", category="B", variable="_scenario_prev_outlook.primary.name",
      description="主情景名称",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="B007", category="B", variable="_scenario_prev_outlook.primary.probability",
      description="主情景概率",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.05)

check(id="B008", category="B", variable="_scenario_prev_outlook.urgency",
      description="紧急程度: critical/act/watch/none",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="B009", category="B", variable="_scenario_prev_outlook.primary.direction",
      description="方向: bullish/bearish/neutral",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="B010", category="B", variable="_scenario_scan_count",
      description="情景引擎扫描计数",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)


# ══════════════════════════════════════════════════════════════
# C. 持仓风控 (每只持仓每轮 ~35 个校验)
# ══════════════════════════════════════════════════════════════

# --- C1. 持仓基础信息 ---
check(id="C001", category="C", variable="position.volume",
      description="持仓股数 > 0（非零持仓）",
      when=When.EVERY_SCAN, assert_type=AssertType.RANGE,
      notes="检查每只持仓 volume >= 100")

check(id="C002", category="C", variable="position.avg_cost",
      description="持仓均价（含佣金）",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.01)

check(id="C003", category="C", variable="position.current_price",
      description="当前价格 = 场景预定义价格",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.05)

check(id="C004", category="C", variable="position.pnl",
      description="浮动盈亏 = (price - avg_cost) × volume",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.5)

check(id="C005", category="C", variable="position.pnl_pct",
      description="浮动盈亏百分比",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.002)

check(id="C006", category="C", variable="position.entry_date",
      description="入场日期正确",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="C007", category="C", variable="position.sector_code",
      description="板块代码非空",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_EMPTY)

# --- C2. 止损/止盈价格 ---
check(id="C010", category="C", variable="position.stop_loss",
      description="原始止损价 > 0",
      when=When.EVERY_SCAN, assert_type=AssertType.RANGE)

check(id="C011", category="C", variable="position.take_profit",
      description="原始止盈价 > 止损价",
      when=When.EVERY_SCAN, assert_type=AssertType.RANGE)

check(id="C012", category="C", variable="position.trailing_stop",
      description="移动止盈价（可为 0 表示未激活）",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.05)

check(id="C013", category="C", variable="position.highest_price",
      description="持仓期间最高价",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.05)

# --- C3. T+1 锁定 ---
check(id="C020", category="C", variable="position.is_tradable",
      description="T+1 锁定判断: 今日买入→不可卖, 隔日→可卖",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

# --- C4. 动态止损止盈 (effective values) ---
check(id="C030", category="C", variable="effective_sl 计算",
      description="effective_sl = cost - loss_width × sl_tighten",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.02)

check(id="C031", category="C", variable="effective_sl_floor",
      description="effective_sl 不低于原止损 85%",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.02)

check(id="C032", category="C", variable="effective_tp 计算",
      description="effective_tp = cost + profit_width × tp_lower",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.02)

check(id="C033", category="C", variable="sl_tighten 受 risk_level 影响",
      description="safe=1.0, cautious=0.92, dangerous=0.85, extreme=0.70",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.02)

check(id="C034", category="C", variable="sl_tighten 受板块走弱影响",
      description="板块走弱时 sl_tighten × 0.95，加速走弱 × 0.90",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.02)

# --- C5. 止损/止盈触发判断 ---
check(id="C040", category="C", variable="止损是否触发",
      description="price <= effective_sl → 触发",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="C041", category="C", variable="止盈是否触发",
      description="price >= effective_tp → 触发",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="C042", category="C", variable="移动止盈是否触发",
      description="price <= highest_price × (1 - trail_tighten) → 触发",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="C043", category="C", variable="T+1 跳过止损止盈",
      description="今日买入的票不触发止损/止盈",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="即使 price 满足触发条件，is_today_buy=True 时也不触发")

# --- C6. 利润回撤止盈 ---
check(id="C050", category="C", variable="_bought_watch.max_profit_pct",
      description="持仓期间最高浮盈百分比",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.002)

check(id="C051", category="C", variable="回撤止盈 keep_ratio",
      description="T1≥15%→0.60, T2≥10%→0.55, T3≥5%→0.50 (+bonus)",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.02)

check(id="C052", category="C", variable="回撤止盈触发",
      description="current_profit < max_profit × keep_ratio → 触发",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="C053", category="C", variable="risk_level bonus",
      description="extreme +0.10, dangerous +0.05 加成到 keep_ratio",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.02)

# --- C7. 六类持仓状态 ---
check(id="C060", category="C", variable="holding_status",
      description="持仓状态: healthy/watching/at_risk/trapped/deep_trapped/add_opportunity",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="healthy: 盈利>2%, watching: 小亏/微利, at_risk: 亏≥2%且消耗≥85%止损, trapped: 亏5-10%, deep: 亏≥10%, add: 亏但超卖")

# --- C8. 被套离场分析 & 反弹目标 ---
check(id="C070", category="C", variable="exit_context 存在",
      description="trapped/deep_trapped 时有离场分析文本",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_EMPTY,
      notes="仅 trapped/deep_trapped 状态检查")

check(id="C071", category="C", variable="exit_target",
      description="反弹减仓目标价 > current_price",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.05,
      notes="仅 trapped/deep_trapped 状态检查")

check(id="C072", category="C", variable="exit_target_label",
      description="目标标签: 布林中轨/MA60/BBI/成本价",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_EMPTY,
      notes="仅 trapped/deep_trapped 状态检查")

# --- C9. 预测性预警 ---
check(id="C080", category="C", variable="predictive_sl_warning",
      description="距止损<3%+市场偏空+urgency≥act → 提前预警",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="C081", category="C", variable="predictive_tp_warning",
      description="距止盈<3%+市场偏空 → 建议提前锁定",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

# --- C10. 动态目标修正 ---
check(id="C090", category="C", variable="ceiling_found",
      description="阻力天花板存在（布林上轨/中轨/MA20/MA60/BBI 最近者）",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

check(id="C091", category="C", variable="floor_found",
      description="支撑地板存在",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

# --- C11. 日内熔断 ---
check(id="C100", category="C", variable="日内熔断触发",
      description="日亏损>3% → 清仓所有亏损持仓",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="daily_loss_ratio > 0.03 时触发全清")

# --- C12. 时间止损 ---
check(id="C110", category="C", variable="时间止损触发",
      description="持有>5天且仍在亏损 → 触发",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

# --- C13. 止损提醒队列 ---
check(id="C120", category="C", variable="_sl_reminders 去重",
      description="同一 code+type 不重复加入队列",
      when=When.ON_STOP_TRIGGER, assert_type=AssertType.EXACT)

check(id="C121", category="C", variable="_sl_reminders 状态",
      description="新触发 → pending, 用户回复再等 → waiting",
      when=When.ON_STOP_TRIGGER, assert_type=AssertType.EXACT)

check(id="C122", category="C", variable="_sl_reminders 循环提醒",
      description="pending 超过 300s → 重新推送",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

# --- C14. 跌停处理 ---
check(id="C130", category="C", variable="跌停不推送卖出",
      description="价格触及跌停板时，不推送卖出信号",
      when=When.ON_STOP_TRIGGER, assert_type=AssertType.EXACT)


# ══════════════════════════════════════════════════════════════
# D. 买入决策 (每只候选信号 ~35 个校验)
# ══════════════════════════════════════════════════════════════

# --- D1. 信号基础状态 ---
check(id="D001", category="D", variable="signal.status",
      description="信号状态: pending/bought/expired",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="D002", category="D", variable="signal.buy_zone_min/max",
      description="买入区间定义存在且合理 (min < max)",
      when=When.EVERY_SCAN, assert_type=AssertType.RANGE)

# --- D2. 价格 vs 买入区 ---
check(id="D010", category="D", variable="in_zone 判断",
      description="buy_min <= price <= buy_max → in_zone",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="D011", category="D", variable="below_zone 判断",
      description="price < buy_min → below_zone",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="D012", category="D", variable="above_zone 判断",
      description="price > buy_max → above_zone",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="D013", category="D", variable="zone_pos",
      description="买入区内位置: (price - buy_min) / (buy_max - buy_min), 0-1",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.RANGE)

# --- D3. entry_rule 过滤 ---
check(id="D020", category="D", variable="entry_rule 值",
      description="从 regime 获取: standard/pullback/confirm/range_boundary/next_day/none",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="D021", category="D", variable="entry_rule 过滤结果",
      description="standard: in_zone 即通过, pullback: zone_pos<0.5, confirm: 需确认信号, none: 禁止",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

# --- D4. 风控检查 ---
check(id="D030", category="D", variable="risk_engine.can_open",
      description="风控审批: 黑名单/市场环境/集中度",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="D031", category="D", variable="黑名单过滤",
      description="黑名单/风险标签含炸板→拒绝",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="D032", category="D", variable="集中度检查",
      description="单票≤20%, 板块≤50%",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="D033", category="D", variable="市场环境仓位上限",
      description="swing:50%, bull:80%, bear:20%",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

# --- D5. 智能仓位计算 ---
check(id="D040", category="D", variable="position_size base",
      description="panic/one_sided/dead_cat→0, v_reversal→8000, normal→16000",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.FLOAT, tolerance=100)

check(id="D041", category="D", variable="position_size sector_adj",
      description="板块走强+20%, 板块走弱-40%",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.FLOAT, tolerance=100)

check(id="D042", category="D", variable="position_size zone_adj",
      description="买入区下沿+10%, 上沿-30%",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.FLOAT, tolerance=100)

check(id="D043", category="D", variable="position_size final",
      description="最终仓位 = base × (1+sector_adj) × (1+zone_adj)",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.FLOAT, tolerance=200)

# --- D6. 涨跌停 ---
check(id="D050", category="D", variable="_is_limit_up",
      description="涨停判断: 688/300→20%, 其余→10%, price>=limit_up×0.995",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="D051", category="D", variable="_is_limit_down",
      description="跌停判断: price<=limit_down×1.005",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="D052", category="D", variable="涨停跳过买入",
      description="涨停票不触发买入信号",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

# --- D7. 买入上下文分析 ---
check(id="D060", category="D", variable="布林带位置",
      description="pct_b: 价格在布林带中位置 0-100",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.RANGE)

check(id="D061", category="D", variable="均线偏离",
      description="price vs MA5/MA10/MA20 偏离百分比",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.FLOAT, tolerance=0.5)

check(id="D062", category="D", variable="回踩支撑检测",
      description="价格接近 MA10/MA20/布林下轨时标记支撑",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.NOT_NONE,
      notes="仅接近支撑时检查")

# --- D8. stop_mult 调整 ---
check(id="D070", category="D", variable="stop_mult",
      description="根据大 market_regime 调整止损宽度: 0.7~1.5×",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.FLOAT, tolerance=0.05)

check(id="D071", category="D", variable="effective_sl_buy",
      description="effective_sl = price - (price - sl) × stop_mult",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.FLOAT, tolerance=0.02)

# --- D9. 模拟盘买入执行 ---
check(id="D080", category="D", variable="PaperTrader.try_buy 调用",
      description="买入条件满足时 try_buy 被调用",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="D081", category="D", variable="买入后 position 增加",
      description="portfolio 新增持仓或 volume 增加",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.EXACT)

check(id="D082", category="D", variable="买入后 cash 减少",
      description="cash = prev_cash - price × volume - commission",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.FLOAT, tolerance=1.0)

check(id="D083", category="D", variable="买入后 signal status",
      description="trade_signals.status → 'bought'",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.EXACT)

check(id="D084", category="D", variable="买入后 _bought_watch 记录",
      description="code 加入 _bought_watch, max_profit_pct=0",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.EXACT)

# --- D10. 换仓 ---
check(id="D090", category="D", variable="持仓满时换仓触发",
      description="MAX_POSITIONS 满 + 候选在买入区 → _try_swap",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT,
      notes="仅 position_count >= MAX_POSITIONS 时检查")

check(id="D091", category="D", variable="主动换仓每 15 轮",
      description="_evaluate_swaps 调用后可能触发换仓",
      when=When.EVERY_15TH, assert_type=AssertType.EXACT)

# --- D11. 买入区预测性接近 ---
check(id="D100", category="D", variable="above_zone 接近预告",
      description="距买入区<3%+偏空+urgency≥act → 预告接近",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

# --- D12. 动态买入区修正 ---
check(id="D110", category="D", variable="buy_zone 修正",
      description="三层联动因子下移买入区，shift 由 market_adjustment 决定",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.FLOAT, tolerance=0.02)

# --- D13. below_zone 回调评估 ---
check(id="D120", category="D", variable="回调评估 action",
      description="浅跌(<2%):打折买入, 深跌(>5%):放弃, 中间:观望",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT,
      notes="仅 below_zone 信号检查")


# ══════════════════════════════════════════════════════════════
# E. 板块趋势 (每 3 轮 ~10 个板块 × ~12 校验)
# ══════════════════════════════════════════════════════════════

check(id="E001", category="E", variable="_sector_trend_history 更新",
      description="每 3 轮各板块追加一条平均涨跌幅",
      when=When.EVERY_3RD, assert_type=AssertType.EXACT)

check(id="E002", category="E", variable="_sector_trend_history 长度不爆炸",
      description="历史记录不无限增长（有截断）",
      when=When.EVERY_3RD, assert_type=AssertType.RANGE)

check(id="E003", category="E", variable="_sector_stats 存在",
      description="板块统计字典非空",
      when=When.EVERY_3RD, assert_type=AssertType.NOT_EMPTY)

check(id="E004", category="E", variable="sector.change_pct",
      description="板块平均涨跌幅",
      when=When.EVERY_3RD, assert_type=AssertType.FLOAT, tolerance=0.5)

check(id="E005", category="E", variable="sector.relative",
      description="相对大盘强弱",
      when=When.EVERY_3RD, assert_type=AssertType.FLOAT, tolerance=0.5)

check(id="E006", category="E", variable="sector.up / down",
      description="板块涨跌家数",
      when=When.EVERY_3RD, assert_type=AssertType.EXACT)

check(id="E007", category="E", variable="sector.breadth",
      description="涨跌比 (up-down)/(up+down)",
      when=When.EVERY_3RD, assert_type=AssertType.FLOAT, tolerance=0.1)

check(id="E008", category="E", variable="sector.continuity",
      description="连续同方向轮数",
      when=When.EVERY_3RD, assert_type=AssertType.EXACT)

check(id="E009", category="E", variable="sector.vol_ratio",
      description="量比",
      when=When.EVERY_3RD, assert_type=AssertType.FLOAT, tolerance=0.2)

# --- E2. 板块趋势方向 ---
check(id="E020", category="E", variable="_get_sector_trend 方向",
      description="持续走强/走弱/走强/走弱/横盘",
      when=When.EVERY_3RD, assert_type=AssertType.NOT_EMPTY)

check(id="E021", category="E", variable="_get_sector_trend 加速",
      description="加速/趋缓/无",
      when=When.EVERY_3RD, assert_type=AssertType.NOT_EMPTY)

check(id="E022", category="E", variable="_get_sector_trend 相对大盘",
      description="强于大盘/弱于大盘/无",
      when=When.EVERY_3RD, assert_type=AssertType.NOT_EMPTY)

# --- E3. 概念趋势 ---
check(id="E030", category="E", variable="_concept_stats",
      description="概念统计字典",
      when=When.EVERY_3RD, assert_type=AssertType.NOT_NONE)

check(id="E031", category="E", variable="concept.change_pct",
      description="概念平均涨跌幅",
      when=When.EVERY_3RD, assert_type=AssertType.FLOAT, tolerance=0.5)

# --- E4. 三层联动 ---
check(id="E040", category="E", variable="_get_market_adjustment",
      description="三层联动因子: tp_ceil_factor/sl_tighten/buy_zone_shift",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE,
      notes="需要 code 有对应板块时才检查")

check(id="E041", category="E", variable="sector_amplify",
      description="大盘偏空+板块走弱→共振放大 1.2~1.4, 背离→减弱 0.4~0.6",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=0.1)


# ══════════════════════════════════════════════════════════════
# F. Portfolio 总览 (每轮 ~10 个校验)
# ══════════════════════════════════════════════════════════════

check(id="F001", category="F", variable="portfolio.cash",
      description="现金 > 0（除非满仓）",
      when=When.EVERY_SCAN, assert_type=AssertType.RANGE)

check(id="F002", category="F", variable="portfolio.total_value",
      description="总资产 = cash + market_value",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=1.0)

check(id="F003", category="F", variable="portfolio.total_value 守恒",
      description="total_value(t) = cash(t) + Σ(position.volume × current_price)(t)",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=2.0)

check(id="F004", category="F", variable="portfolio.position_ratio",
      description="仓位比例 = market_value / total_value, 0-1",
      when=When.EVERY_SCAN, assert_type=AssertType.RANGE)

check(id="F005", category="F", variable="portfolio.daily_pnl",
      description="当日盈亏 = total_value - 今开 total_value",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=1.0)

check(id="F006", category="F", variable="portfolio.drawdown",
      description="日内最大回撤 ≥ 0",
      when=When.EVERY_SCAN, assert_type=AssertType.RANGE)

check(id="F007", category="F", variable="portfolio._peak_value",
      description="日内峰值 ≥ total_value",
      when=When.EVERY_SCAN, assert_type=AssertType.FLOAT, tolerance=1.0)

check(id="F008", category="F", variable="portfolio.position_count",
      description="持仓数量 <= MAX_POSITIONS",
      when=When.EVERY_SCAN, assert_type=AssertType.RANGE)

check(id="F009", category="F", variable="portfolio 费率",
      description="佣金万0.85 最低5元, 印花税万分之五卖出单边",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.FLOAT, tolerance=0.1)


# ══════════════════════════════════════════════════════════════
# G. 消息推送 (事件驱动)
# ══════════════════════════════════════════════════════════════

check(id="G001", category="G", variable="开盘决策推送",
      description="scan=1 时推送 📋 开盘决策 (持仓+买入区+待观察+集中度)",
      when=When.SCAN_1, assert_type=AssertType.CONTAINS,
      notes="验证消息包含关键部分")

check(id="G002", category="G", variable="买入信号推送",
      description="买入触发时推送 🔴 买入信号 + code + 价格 + 仓位理由",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.CONTAINS)

check(id="G003", category="G", variable="止损卖出推送",
      description="止损触发时推送 ⚠️ 止损信号 + code + 价格 + 确认指令",
      when=When.ON_STOP_TRIGGER, assert_type=AssertType.CONTAINS)

check(id="G004", category="G", variable="止盈卖出推送",
      description="止盈触发时推送 ✅ 止盈信号",
      when=When.ON_STOP_TRIGGER, assert_type=AssertType.CONTAINS)

check(id="G005", category="G", variable="大盘告警推送",
      description="市场 panic/extreme 时推送告警",
      when=When.EVERY_SCAN, assert_type=AssertType.CONTAINS,
      notes="仅 regime.urgent_action 有值时检查")

check(id="G006", category="G", variable="技术拐点推送",
      description="MACD/RSI/KDJ 极值时推送技术提醒",
      when=When.EVERY_SCAN, assert_type=AssertType.CONTAINS,
      notes="仅指标触发时检查")

check(id="G007", category="G", variable="板块热度推送",
      description="每 50 轮推送板块排名",
      when=When.EVERY_50TH, assert_type=AssertType.CONTAINS)

check(id="G008", category="G", variable="异动检测推送",
      description="急速拉升/放量异动时推送",
      when=When.EVERY_3RD, assert_type=AssertType.CONTAINS,
      notes="仅检测到异动时检查")

check(id="G009", category="G", variable="收盘持仓报告",
      description="15:00 收盘推送持仓汇总 (模拟盘→群, 实盘→私聊)",
      when=When.AT_CLOSE, assert_type=AssertType.CONTAINS)

check(id="G010", category="G", variable="消息去重",
      description="同一信号不重复推送（_signal_alert_state 去重）",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="G011", category="G", variable="实盘私聊推送",
      description="实盘信息只发 _private_telegram 不发群聊",
      when=When.AT_CLOSE, assert_type=AssertType.EXACT)


# ══════════════════════════════════════════════════════════════
# H. 跨日状态 (Day1 收盘 → Day2 启动 ~35 个校验)
# ══════════════════════════════════════════════════════════════

# --- H1. 持仓恢复 ---
check(id="H001", category="H", variable="positions 恢复",
      description="Day2 启动后 portfolio.positions 数量 = Day1 收盘持仓数",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H002", category="H", variable="avg_cost 保持",
      description="Day2 恢复的 avg_cost = Day1 收盘的 avg_cost",
      when=When.CROSS_DAY, assert_type=AssertType.FLOAT, tolerance=0.01)

check(id="H003", category="H", variable="volume 保持",
      description="Day2 恢复的 volume = Day1 收盘 volume",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H004", category="H", variable="stop_loss 保持",
      description="Day2 恢复的 stop_loss = Day1 收盘 stop_loss",
      when=When.CROSS_DAY, assert_type=AssertType.FLOAT, tolerance=0.01)

check(id="H005", category="H", variable="take_profit 保持",
      description="Day2 恢复的 take_profit = Day1 收盘 take_profit",
      when=When.CROSS_DAY, assert_type=AssertType.FLOAT, tolerance=0.01)

check(id="H006", category="H", variable="entry_date 保持",
      description="Day2 恢复的 entry_date 不变",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

# --- H2. _bought_watch 恢复 ---
check(id="H010", category="H", variable="_bought_watch 恢复",
      description="Day2 _bought_watch keys = Day1 持仓 codes",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H011", category="H", variable="max_profit_pct 保持",
      description="Day2 恢复的 max_profit_pct = Day1 收盘值",
      when=When.CROSS_DAY, assert_type=AssertType.FLOAT, tolerance=0.002)

# --- H3. 日级变量清空 ---
check(id="H020", category="H", variable="_signal_alert_state 清空",
      description="Day2 启动后 _signal_alert_state = {}",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H021", category="H", variable="_review_alert_state 清空",
      description="Day2 启动后 _review_alert_state = {}",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H022", category="H", variable="_sl_reminders 清空",
      description="Day2 启动后 _sl_reminders = {}",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H023", category="H", variable="_alerted_sl_tp 清空",
      description="Day2 启动后 _alerted_sl_tp = set()",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H024", category="H", variable="_index_alerted_downtrend 重置",
      description="Day2 启动后 _index_alerted_downtrend = False",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H025", category="H", variable="_max_drawdown_alerted 重置",
      description="Day2 启动后 _max_drawdown_alerted = False",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H026", category="H", variable="_closing_decision_done 重置",
      description="Day2 启动后 _closing_decision_done = False",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H027", category="H", variable="_index_prices 清空",
      description="Day2 启动后 _index_prices = []",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H028", category="H", variable="_index_high/_index_low 重置",
      description="Day2 启动后 _index_high=_index_low=0",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H029", category="H", variable="_market_turnovers 清空",
      description="Day2 启动后 _market_turnovers = []",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H030", category="H", variable="_market_snapshot 清空",
      description="Day2 启动后 _market_snapshot = {}",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H031", category="H", variable="_scenario_probs 重置",
      description="Day2 启动后情景引擎概率重置为初始值",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H032", category="H", variable="_scenario_scan_count 重置",
      description="Day2 启动后 _scenario_scan_count = 0",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H033", category="H", variable="_watch_codes_stale 重置",
      description="Day2 启动后 _watch_codes_stale = True",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

# --- H4. Portfolio 连续性 ---
check(id="H040", category="H", variable="portfolio._peak_value 恢复",
      description="Day2 启动后 _peak_value 从 Day1 快照恢复",
      when=When.CROSS_DAY, assert_type=AssertType.FLOAT, tolerance=1.0)

check(id="H041", category="H", variable="portfolio cash 连续",
      description="Day2 cash = Day1 收盘 cash",
      when=When.CROSS_DAY, assert_type=AssertType.FLOAT, tolerance=1.0)

# --- H5. 收盘快照保存 ---
check(id="H050", category="H", variable="trade_portfolio_snapshots 保存",
      description="Day1 收盘后 snapshot 表有当日记录",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="H051", category="H", variable="trade_signals 过期",
      description="Day1 收盘后 pending signal → expired",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)


# ══════════════════════════════════════════════════════════════
# I. 异常韧性 (每轮)
# ══════════════════════════════════════════════════════════════

check(id="I001", category="I", variable="_scan 异常不崩溃",
      description="任一步骤抛异常不导致 _scan 崩溃",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="_scan 顶层有 try/except")

check(id="I002", category="I", variable="空 watch_codes 正常",
      description="无监控代码时 _scan 安全返回",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="仅 watch_codes 为空时检查")

check(id="I003", category="I", variable="空 prices 正常",
      description="QMT 无数据时跳过本轮",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="仅 prices 为空时检查")

check(id="I004", category="I", variable="空持仓正常",
      description="无持仓时 _check_positions 不崩溃",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="I005", category="I", variable="空信号正常",
      description="无 pending 信号时 _check_signals 不崩溃",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="I006", category="I", variable="无板块数据正常",
      description="_industry_cache 为空时不崩溃",
      when=When.EVERY_3RD, assert_type=AssertType.EXACT)

check(id="I007", category="I", variable="午休时间跳过",
      description="11:30-13:00 期间不执行扫描",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="仅午休时间检查")

check(id="I008", category="I", variable="repo 异常不崩溃",
      description="TradeRepository 异常时 catch 后继续",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)


# ══════════════════════════════════════════════════════════════
# J. 边界条件 (关键浮点精度 & 极端值)
# ══════════════════════════════════════════════════════════════

check(id="J001", category="J", variable="止损触发边界",
      description="price 恰好等于 effective_sl 也应触发 (<= 比较)",
      when=When.ON_STOP_TRIGGER, assert_type=AssertType.EXACT,
      notes="浮点精度: 37.4500000001 vs 37.45 不应影响判断")

check(id="J002", category="J", variable="止盈触发边界",
      description="price 恰好等于 effective_tp 也应触发 (>= 比较)",
      when=When.ON_STOP_TRIGGER, assert_type=AssertType.EXACT)

check(id="J003", category="J", variable="买入区上沿边界",
      description="price == buy_max 属于 in_zone",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="J004", category="J", variable="买入区下沿边界",
      description="price == buy_min 属于 in_zone",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="J005", category="J", variable="熔断边界 3%",
      description="daily_loss 恰好 3% 时熔断行为明确",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="J006", category="J", variable="None vs 0 区分",
      description="price=None 时不应参与计算 (用 is None 而非 not price)",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="J007", category="J", variable="空字符串 code 过滤",
      description="code='' 的信号不应被处理",
      when=When.ON_SIGNAL_TRIGGER, assert_type=AssertType.EXACT)

check(id="J008", category="J", variable="负价格保护",
      description="price < 0 时不应参与计算",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)


# ══════════════════════════════════════════════════════════════
# K. 监控列表 & 缓存 (每轮)
# ══════════════════════════════════════════════════════════════

check(id="K001", category="K", variable="_get_watch_codes 组成",
      description="watch_codes = positions + pending_signals + review_picks (去重)",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="K002", category="K", variable="_get_watch_codes 缓存",
      description="_watch_codes_stale=False 时复用 _cached_db_watch_codes",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="K003", category="K", variable="_watch_codes_stale",
      description="买入后或信号变更后 _watch_codes_stale = True",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.EXACT)

check(id="K004", category="K", variable="_cached_db_watch_codes",
      description="缓存 = signals + picks - positions (仅 DB 来源)",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="仅 _watch_codes_stale=False 时检查")

check(id="K005", category="K", variable="_invalidate_watch_codes_cache",
      description="模拟盘成交后触发缓存刷新",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.EXACT)

# --- K2. IPC Collector ---
check(id="K010", category="K", variable="_recv_collector_data 处理",
      description="Collector 推送的 index/market 消息更新 _last_index_quote/_market_snapshot",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="K011", category="K", variable="_last_db_ts 去重",
      description="重启后 ts <= _last_db_ts 的消息被跳过",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

# --- K3. 价格缓存 ---
check(id="K020", category="K", variable="_limit_cache",
      description="涨跌停价缓存: {code: (limit_up, limit_down, pre_close)}",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

check(id="K021", category="K", variable="_instrument_cache",
      description="合约信息缓存",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

check(id="K022", category="K", variable="_intraday_cache 刷新",
      description="每轮刷新日内指标缓存",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

# --- K4. _triggered_ids ---
check(id="K030", category="K", variable="_triggered_ids 去重",
      description="同一 signal_id 不重复执行买入",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.EXACT)

# --- K5. _prev_snapshot ---
check(id="K040", category="K", variable="_prev_snapshot",
      description="异动检测用上一轮快照",
      when=When.EVERY_3RD, assert_type=AssertType.NOT_NONE,
      notes="仅异动检测触发时检查")


# ══════════════════════════════════════════════════════════════
# L. 复盘精选跟踪 (每轮)
# ══════════════════════════════════════════════════════════════

check(id="L001", category="L", variable="_check_review_picks 被调用",
      description="每轮扫 review picks，检查是否进入买入区",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="L002", category="L", variable="review_pick 入区通知",
      description="复盘票进入买入区时推送通知",
      when=When.EVERY_SCAN, assert_type=AssertType.CONTAINS,
      notes="仅入区时检查")

check(id="L003", category="L", variable="review_pick 去重",
      description="已在 trade_signals 中的 REVIEW 信号不重复推送",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="_check_review_picks 和 _check_signals 不重复推送同 code")

check(id="L004", category="L", variable="_load_review_signal_zones",
      description="优先从 trade_signals 取结构化买入区间，其次 fallback MA 动态计算",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

check(id="L005", category="L", variable="_review_alert_state 去重",
      description="复盘票入区通知不重复推送",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="L006", category="L", variable="复盘票买入执行",
      description="复盘票入区后 try_buy 执行（与 AI 信号同路径）",
      when=When.ON_BUY_EXECUTE, assert_type=AssertType.EXACT)


# ══════════════════════════════════════════════════════════════
# M. 风控引擎参数 (每轮)
# ══════════════════════════════════════════════════════════════

check(id="M001", category="M", variable="risk_engine.update_market_env 调用",
      description="每轮 update_market_env(ma20, price, ma60, vol_trend, breadth_ratio, amplitude, active_sectors)",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT)

check(id="M002", category="M", variable="_get_index_baseline 返回",
      description="MA5/MA10/MA20 从 stock_basic 的 000001 获取",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

check(id="M003", category="M", variable="_get_index_ma60 返回",
      description="MA60 非空",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

check(id="M004", category="M", variable="_calc_volume_trend 返回",
      description="量能趋势计算",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)

check(id="M005", category="M", variable="_compute_breadth 返回",
      description="市场宽度: up/down/total",
      when=When.EVERY_SCAN, assert_type=AssertType.NOT_NONE)


# ══════════════════════════════════════════════════════════════
# N. 持仓恢复 (_restore_positions)
# ══════════════════════════════════════════════════════════════

check(id="N001", category="N", variable="_restore_positions 来源",
      description="从 trade_orders 按 stock_code 汇总 buy/sell 净量恢复持仓",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="N002", category="N", variable="_restore_positions net_vol",
      description="net_vol = SUM(buy filled_volume) - SUM(sell filled_volume)",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="N003", category="N", variable="_restore_positions avg_cost",
      description="avg_cost = SUM(buy filled_price × volume) / SUM(buy volume)",
      when=When.CROSS_DAY, assert_type=AssertType.FLOAT, tolerance=0.01)

check(id="N004", category="N", variable="_restore_positions 信号恢复",
      description="从 trade_signals 恢复 stop_loss/take_profit/sector_code",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="N005", category="N", variable="_restore_positions _bought_watch",
      description="从 trade_signals 恢复 max_profit_pct/status",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)

check(id="N006", category="N", variable="_restore_positions 清理已平仓",
      description="net_vol <= 0 的持仓不恢复（已清仓）",
      when=When.CROSS_DAY, assert_type=AssertType.EXACT)


# ══════════════════════════════════════════════════════════════
# O. 数据安全 (始终检查)
# ══════════════════════════════════════════════════════════════

check(id="O001", category="O", variable="DB 路径验证",
      description="测试 DB 路径包含 'tests/e2e/test_db'，拒绝连生产库",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="启动时断言，不满足直接退出")

check(id="O002", category="O", variable="E2E_TEST_MODE 环境变量",
      description="TradeRepository 在 E2E 模式下拒绝无参构造",
      when=When.EVERY_SCAN, assert_type=AssertType.EXACT,
      notes="环境变量设了就绝不可能连生产库")


# ══════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════

def summary():
    """打印检查清单摘要."""
    cats = {}
    for c in CHECKS:
        cats.setdefault(c.category, []).append(c)

    lines = [f"E2E 测试清单: {len(CHECKS)} 条检查项\n"]
    for cat in sorted(cats):
        items = cats[cat]
        cat_names = {
            "A": "大盘状态 & 指数", "B": "情景引擎", "C": "持仓风控",
            "D": "买入决策", "E": "板块趋势", "F": "Portfolio 总览",
            "G": "消息推送", "H": "跨日状态", "I": "异常韧性",
            "J": "边界条件", "K": "监控列表 & 缓存",
            "L": "复盘精选跟踪", "M": "风控引擎参数",
            "N": "持仓恢复", "O": "数据安全",
        }
        lines.append(f"  {cat}. {cat_names.get(cat, cat)}: {len(items)} 条")
        for item in items:
            lines.append(f"    [{item.id}] {item.variable} — {item.description}")

    lines.append(f"\n预估总断言数: ~{_estimate_assertions()} 条 (240 轮 × 2 天)")
    return "\n".join(lines)


def _estimate_assertions() -> int:
    """估算两天的总断言数."""
    per_scan = sum(1 for c in CHECKS if c.when in (
        When.EVERY_SCAN, When.EVERY_3RD, When.EVERY_15TH, When.EVERY_50TH
    ))
    n_positions = 5  # 平均持仓数
    n_candidates = 15  # 平均候选信号数
    n_sectors = 8  # 平均板块数

    total = per_scan * 240 * 2  # 基础 × 240 轮 × 2 天
    total += len([c for c in CHECKS if c.category == "C"]) * n_positions * 240 * 2
    total += len([c for c in CHECKS if c.category == "D"]) * n_candidates * 5  # 平均触发 5 次
    total += len([c for c in CHECKS if c.category == "E"]) * n_sectors * 80 * 2  # 每 3 轮
    total += len([c for c in CHECKS if c.category == "H"])  # 跨日 1 次
    return total


if __name__ == "__main__":
    print(summary())
