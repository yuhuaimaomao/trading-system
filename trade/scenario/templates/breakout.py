"""盘中突破场景模板 — 引擎2（Intraday Scout）专用。

适用场景：全市场扫描发现的盘中强势股，不等均线回踩。
AI 角色：动量交易分析师，判断突破有效性和追入风险。
"""

from trade.scenario.templates.prompt_model import PromptTemplate

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
