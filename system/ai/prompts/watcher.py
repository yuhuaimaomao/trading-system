"""盯盘 AI 模版 — 不同情景用不同模型。

模型配置（.env）:
  AI_MODEL_WATCHER_CHASE → 追高二判（默认 deepseek-v4-pro，需快速）
  AI_MODEL_WATCHER_SWAP → 换仓评估（默认 deepseek-v4-pro）
  AI_MODEL_WATCHER_INDEX → 指数波动（默认 deepseek-v4-pro）
  AI_MODEL_WATCHER_BREAKOUT → 突破追涨（默认 deepseek-v4-pro）
  AI_MODEL_WATCHER_TRAPPED → 被套离场（默认 deepseek-v4-pro）
"""

# ═══════════════════════════════════════
# 追高二判（价格超出买入区时 AI 判断）
# ═══════════════════════════════════════

CHASE_OPINION_SYSTEM = (
    "你是A股短线交易员。根据实时价格、买入区间、板块趋势，快速判断追高/拒绝/等待。"
)

CHASE_OPINION_TEMPLATE = (
    "股票：{code} {name}，现价{price:.2f}，买入区间{buy_min:.2f}~{buy_max:.2f}，"
    "当前{above_desc}。"
    "止损{sl:.2f}，止盈{tp:.2f}。"
    "板块趋势：{trend}。{reject_info}"
    "请根据当前状态判断：同意拒绝 / 可以买入 / 再等等。"
    "用一句话给出结论和关键理由（不超过50字）。"
)

# ═══════════════════════════════════════
# 换仓评估（比较持仓和候选，决定是否换）
# ═══════════════════════════════════════

SWAP_EVAL_SYSTEM = "你是A股短线交易员。基于实时盘面判断换仓，只输出JSON。"

SWAP_EVAL_TEMPLATE = """当前模拟盘持仓（{pos_count}只，上限5只）：

{pos_text}

买点区候选信号：
{cand_text}{ctx_line}{sec_ctx}

请评估是否应该换仓。考虑：
1. 持仓盈亏、止损止盈距离、走势强弱
2. 候选信号的评分、今日涨跌、买点区间
3. 候选所处行业/概念是否比持仓更强
4. 如果候选显著优于某只持仓，建议换仓

只回复JSON：{{"sell": "要卖的代码", "buy": "要买的代码"}} 或 {{"sell": null, "buy": null}}。"""

# ═══════════════════════════════════════
# 指数波动分析
# ═══════════════════════════════════════

INDEX_FLUCTUATION_SYSTEM = (
    "你是A股大盘技术分析专家，基于MACD/RSI/KDJ和均线系统做短线预判。"
    "简洁、准确、可操作。"
)

INDEX_FLUCTUATION_TEMPLATE = """分析上证指数当前走势，预判方向和企稳点位。

## 当前状态
指数现价: {current:.2f}
近{bar_count}轮(约{bar_count}分钟)总变动: {change_from_first:+.2f}%
日线均线: {ma_parts}

## 分钟级技术指标
MACD: DIF={macd_dif:.2f} DEA={macd_dea:.2f} BAR={macd_bar:.2f}
RSI(6/12/24): {rsi6:.1f}/{rsi12:.1f}/{rsi24:.1f}
KDJ: K={kdj_k:.1f} D={kdj_d:.1f} J={kdj_j:.1f}
交叉: {cross_info}
背离: {div_info}

## 近{bar_count}分钟走势
{recent_bars}

请分析:
1. 这波急跌/急拉会继续还是会反转?
2. 如果继续，到什么点位可能企稳?
3. 当前应该追/等/减/守?

用中文简洁回复，不超过150字。格式:
方向: [继续下跌/继续上涨/即将反弹/即将回调]
企稳点位: [具体点位或区间]
建议: [追/等/减/守]
理由: [一句话]"""

from dataclasses import dataclass


@dataclass
class PromptTemplate:
    scenario: str
    system_prompt: str  # AI 角色定义 + 核心判断原则
    user_template: str  # {field} 占位模板
    required_fields: list[str]  # 模板必填字段（格式化前校验）
    max_tokens: int = 100  # AI 返回长度
    dedupe: bool = True  # 同名 key 是否替换旧任务


"""盘中突破场景模板 — 引擎2（Intraday Scout）专用。

适用场景：全市场扫描发现的盘中强势股，不等均线回踩。
AI 角色：动量交易分析师，判断突破有效性和追入风险。
"""


BREAKOUT_TEMPLATE = PromptTemplate(
    scenario="breakout",
    system_prompt=(
        "你是动量交易分析师。核心原则：\n"
        "1. 盘中强势股不需要等均线回踩——真正强势的票不会盘中回踩\n"
        "2. 开盘回踩或尾盘回踩均线后拉起 = 可以追；盘中回踩 = 大概率是坑\n"
        "3. 判断标准：量能持续放大 + 板块共振 TOP5 + 价格持续创新高\n"
        "4. 开盘30分钟内急拉需警惕诱多（集合竞价假突破）\n"
        "5. 尾盘14:30后突破可追但仓位减半\n"
        "6. 板块龙头松动（板块内涨幅最大股开板/回落）= 立即放弃该板块所有候选\n"
        "7. 放量突破平台/前高 = 强信号；缩量上涨 = 弱信号\n"
        "用一句话给出结论：买入 / 观望 / 放弃，加关键理由（不超过50字）。"
    ),
    user_template=(
        "盘中突破候选：{code} {name}\n"
        "现价 {price:.2f} 涨幅 {change_pct:+.1f}%\n"
        "板块：{sector_name} 涨幅 {sector_pct:+.1f}% 排名 {sector_rank}/{sector_total}\n"
        "量能：{amount_desc}\n"
        "价格走势（近5分钟）：{price_trend}\n"
        "大盘：{market_env}（{risk_level}）日内高 {index_high:.0f} 低 {index_low:.0f}\n"
        "请判断：买入 / 观望 / 放弃？一句话理由（不超过50字）。"
    ),
    required_fields=[
        "code",
        "name",
        "price",
        "change_pct",
        "sector_name",
        "sector_pct",
        "sector_rank",
        "sector_total",
        "amount_desc",
        "price_trend",
        "market_env",
        "risk_level",
        "index_high",
        "index_low",
    ],
    max_tokens=80,
)
"""被套离场场景模板 — 持仓浮亏 > 5% 时辅助判断离场时机。

适用场景：深跌被套，等待反弹，需要判断是继续等还是立即止损。
AI 角色：持仓风控分析师，综合考虑个股技术+板块+大盘+消息面。
"""


TRAPPED_EXIT_TEMPLATE = PromptTemplate(
    scenario="trapped_exit",
    system_prompt=(
        "你是持仓风控分析师。核心原则：\n"
        "1. 被套后不要等解套——找最优离场点，不是找成本价\n"
        "2. 反弹到最近阻力位（BBI/MA60/布林中轨/成本价）= 减仓窗口，不要贪\n"
        "3. 板块加速走弱时，任何反弹都应减仓，不要等更高\n"
        "4. 大盘恐慌/极端风险时，反弹不可靠，优先保本\n"
        "5. 从最低点反弹超过3%后回落超过2% = 反弹失败，立即离场\n"
        "6. 板块走强 + 大盘企稳 = 可稍耐心等待更好卖点\n"
        "用一句话给出结论：立即止损 / 等反弹减仓 / 继续持有，加关键理由（不超过50字）。"
    ),
    user_template=(
        "持仓被套：{code} {name}\n"
        "成本 {cost:.2f} 现价 {price:.2f} 浮亏 {loss_pct:.1f}%\n"
        "止损线 {sl:.2f} 止盈线 {tp:.2f}\n"
        "日内：最低 {lowest:.2f} 反弹高点 {rebound_high:.2f} 当前距低点 +{rebound_pct:.1f}%\n"
        "最近阻力：{resistance_label} {resistance_price:.2f}\n"
        "板块趋势：{sector_trend}\n"
        "大盘：{market_env}（{risk_level}）\n"
        "请判断：立即止损 / 等反弹减仓 / 继续持有？一句话理由（不超过50字）。"
    ),
    required_fields=[
        "code",
        "name",
        "price",
        "cost",
        "loss_pct",
        "sl",
        "tp",
        "lowest",
        "rebound_high",
        "rebound_pct",
        "resistance_label",
        "resistance_price",
        "sector_trend",
        "market_env",
        "risk_level",
    ],
    max_tokens=80,
)
# PromptTemplate 已定义在文件头部
