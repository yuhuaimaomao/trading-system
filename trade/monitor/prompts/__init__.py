"""AI 场景化模板系统。

模板注册 → 场景识别 → 格式化 prompt。
新增场景只需：(1) 写模板文件 (2) 在 _TEMPLATES 注册 (3) detect_scenario 加规则。
"""

from trade.monitor.prompts.breakout import BREAKOUT_TEMPLATE
from trade.monitor.prompts.schemas import PromptTemplate
from trade.monitor.prompts.trapped_exit import TRAPPED_EXIT_TEMPLATE

# ── 模板注册表 ──
_TEMPLATES: dict[str, PromptTemplate] = {
    "breakout": BREAKOUT_TEMPLATE,
    "trapped_exit": TRAPPED_EXIT_TEMPLATE,
}


def get_template(scenario: str) -> PromptTemplate | None:
    """获取指定场景的模板。"""
    return _TEMPLATES.get(scenario)


def list_scenarios() -> list[str]:
    """列出所有已注册场景。"""
    return list(_TEMPLATES.keys())


def detect_scenario(ctx: dict) -> str:
    """根据上下文自动识别场景。

    ctx 常见字段：
        - loss_pct: 亏损百分比（正数表示亏损）
        - above_pct: 超出买入区百分比
        - zone_type: "pullback" / "breakout"
        - is_trapped: 标记为被套持仓
    """
    if ctx.get("is_trapped"):
        return "trapped_exit"
    if ctx.get("loss_pct", 0) > 5:
        return "trapped_exit"
    if ctx.get("zone_type") == "breakout":
        return "breakout"
    if ctx.get("scenario"):
        return ctx["scenario"]
    return "breakout"  # 默认（新场景用 breakout）


def build_prompt(scenario: str, **fields) -> tuple[str, str, int]:
    """构建 AI prompt。

    Returns:
        (system_prompt, user_prompt, max_tokens)
    Raises:
        KeyError: 场景未注册
        ValueError: 缺少必填字段
    """
    tmpl = _TEMPLATES.get(scenario)
    if tmpl is None:
        raise KeyError(f"未注册的场景: {scenario}")

    missing = [f for f in tmpl.required_fields if f not in fields]
    if missing:
        raise ValueError(f"场景 {scenario} 缺少必填字段: {missing}")

    try:
        user_prompt = tmpl.user_template.format(**fields)
    except KeyError as e:
        raise ValueError(f"场景 {scenario} 模板字段 {e} 未提供") from e

    return tmpl.system_prompt, user_prompt, tmpl.max_tokens
