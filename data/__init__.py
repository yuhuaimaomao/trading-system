"""
数据层 — 所有 DB 访问的统一入口。

按业务线组织：
- data.market  — 行情基础数据（跨域共享）
- data.trade   — 交易线（信号/订单/持仓）
- data.strategy — 策略线（漏斗/决策/改进）
- data.review  — 复盘线（预测/追踪/分析）
- data.audit   — 审计线（日志/发现/教训）
"""

# 向后兼容：保留旧路径的 re-export
from data.repo import TradeRepository  # noqa: F401
