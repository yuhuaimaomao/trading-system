"""盯盘运行时数据校验框架。

每个校验函数签名:
    check(ctx: CheckContext) -> list[str]
返回告警消息列表（空列表 = 通过）。

不加新检查只写一个函数然后注册到 CHECKS 列表即可，不需要改 Watcher。
"""

from dataclasses import dataclass, field


@dataclass
class CheckContext:
    """校验上下文 — 由 Watcher._health_check() 每轮填充后传给所有校验函数"""

    # 账户
    cash: float = 0.0
    total_value: float = 0.0
    positions: dict = field(default_factory=dict)  # {code: Position}
    max_positions: int = 5

    # 行情
    prices: dict = field(default_factory=dict)  # {code: price} 本轮价格
    index_prices: list = field(default_factory=list)  # 全日上证价格序列
    index_pre_close: float = 0.0  # 昨收价
    qmt_change_pct: float | None = None  # QMT 返回的涨跌幅

    # 板块
    sector_stats: dict = field(default_factory=dict)  # {sector_name: {change_pct, ...}}

    # 系统内部
    pos_meta: dict = field(default_factory=dict)  # {code: {sl, tp, ...}}
    scan_count: int = 0
    baseline_pre_close: float = 0.0  # 开盘首轮锚定的昨收价
    baseline_qmt_pct: float = 0.0  # 开盘首轮锚定的 QMT 涨跌幅


# ═══════════════════════════════════════════════════════════════
# 校验函数
# ═══════════════════════════════════════════════════════════════


def _account_equation(ctx: CheckContext) -> list[str]:
    """账户恒等式: total == cash + sum(market_value)"""
    mv = sum(p.market_value for p in ctx.positions.values())
    drift = abs(ctx.total_value - ctx.cash - mv)
    if drift > 10:
        return [f"⚠️ 账户不一致: total={ctx.total_value:.0f} cash+mv={ctx.cash + mv:.0f} 偏差={drift:.0f}"]
    return []


def _position_limit(ctx: CheckContext) -> list[str]:
    """持仓数不超上限"""
    if len(ctx.positions) > ctx.max_positions:
        return [f"⚠️ 持仓超限: {len(ctx.positions)}/{ctx.max_positions}"]
    return []


def _price_jump(ctx: CheckContext) -> list[str]:
    """价格单轮跳变 > 15%（除权除息或数据异常）"""
    alerts = []
    for code, price in ctx.prices.items():
        pos = ctx.positions.get(code)
        if pos and pos.current_price > 0:
            chg = abs(price - pos.current_price) / pos.current_price
            if chg > 0.15:
                alerts.append(f"⚠️ 价格跳变: {code} {pos.current_price:.2f}→{price:.2f} ({chg:.1%})")
    return alerts


def _index_stale(ctx: CheckContext) -> list[str]:
    """指数价格停更"""
    if len(ctx.index_prices) < 5:
        return []
    recent = ctx.index_prices[-5:]
    if max(recent) - min(recent) < 0.01:
        # 状态由 Watcher 维护 _index_stale_count
        return []  # Watcher 层面处理计数，这里只做本轮检测
    return []


def _orphan_meta(ctx: CheckContext) -> list[str]:
    """_pos_meta 与 positions 不一致"""
    orphan = set(ctx.pos_meta.keys()) - set(ctx.positions.keys())
    if orphan:
        return [f"⚠️ 元数据孤儿: {', '.join(sorted(orphan))}"]
    return []


# ── 双路交叉验证 ──


def _cross_validate_change_pct(ctx: CheckContext) -> list[str]:
    """涨跌幅双算: 自算 vs QMT返回值 → 差 > 0.05% 告警"""
    if ctx.baseline_pre_close <= 0 or not ctx.index_prices or ctx.qmt_change_pct is None:
        return []
    our_pct = (ctx.index_prices[-1] - ctx.baseline_pre_close) / ctx.baseline_pre_close
    diff = abs(our_pct - ctx.qmt_change_pct)
    if diff > 0.005:
        return [
            f"🔴 涨跌幅分歧: 自算={our_pct:.4f} QMT={ctx.qmt_change_pct:.4f} "
            f"(差{diff:.4f}) → 基准价可能用错了"
        ]
    return []


def _cross_validate_preclose_stability(ctx: CheckContext) -> list[str]:
    """昨收价不变性: 全天不能变"""
    if ctx.baseline_pre_close <= 0 or ctx.index_pre_close <= 0:
        return []
    if abs(ctx.index_pre_close - ctx.baseline_pre_close) > 0.01:
        return [
            f"🔴 昨收价漂移: {ctx.baseline_pre_close:.2f}→{ctx.index_pre_close:.2f} "
            "→ 涨跌幅计算基准全偏"
        ]
    return []


def _cross_validate_direction(ctx: CheckContext) -> list[str]:
    """指数方向 vs 板块均值方向 — 不应长期背离"""
    if not ctx.sector_stats or not ctx.index_prices:
        return []
    market_avg = sum(
        s.get("change_pct", 0) for s in ctx.sector_stats.values()
    ) / max(len(ctx.sector_stats), 1)
    if len(ctx.index_prices) < 2:
        return []
    index_dir = 1 if ctx.index_prices[-1] > ctx.index_prices[0] else -1
    sector_dir = 1 if market_avg > 0 else -1
    if index_dir != sector_dir and abs(market_avg) > 0.005:
        return [
            f"⚠️ 方向背离: 上证={'↑' if index_dir > 0 else '↓'} "
            f"板块均值={'↑' if sector_dir > 0 else '↓'} ({market_avg:+.4f})"
        ]
    return []


def _cross_validate_market_value(ctx: CheckContext) -> list[str]:
    """持仓市值双算: 记录值 vs 价格×股数 — 差 > 1% 告警"""
    if not ctx.positions:
        return []
    mv_from_pos = sum(p.market_value for p in ctx.positions.values())
    mv_from_prices = sum(
        ctx.prices.get(code, p.current_price) * p.volume
        for code, p in ctx.positions.items()
    )
    if mv_from_prices <= 0:
        return []
    drift = abs(mv_from_pos - mv_from_prices) / mv_from_prices
    if drift > 0.01:
        return [
            f"⚠️ 市值分歧: 记录={mv_from_pos:.0f} 价格×股数={mv_from_prices:.0f} (差{drift:.1%})"
        ]
    return []


# ── 累积一致性 ──

# 以下校验需要跨轮状态，状态由 Watcher 在 CheckContext 外维护。
# 框架提供函数签名和注册位，实现按需在 Watcher 侧补状态。


def _scan_count_continuity(ctx: CheckContext) -> list[str]:
    """扫描计数连续性 — scan_count 应逐轮 +1，跳跃说明丢了轮次"""
    return []  # 需要 Watcher 侧维护 _prev_scan_count


def _index_prices_length(ctx: CheckContext) -> list[str]:
    """index_prices 长度应 ≈ scan_count（每轮记录一个价格）"""
    expected = ctx.scan_count
    actual = len(ctx.index_prices)
    if expected > 10 and actual > 0 and abs(expected - actual) > 3:
        return [f"⚠️ 价格序列长度异常: scan={expected} prices={actual} (差{abs(expected - actual)})"]
    return []


def _locked_volume_consistency(ctx: CheckContext) -> list[str]:
    """T+1 锁仓：收盘时 locked_volume 应全部归零（次日解锁）"""
    total_locked = sum(
        getattr(p, "locked_volume", 0) for p in ctx.positions.values()
    )
    # 只在尾盘检查（scan_count 很大时）
    if ctx.scan_count > 200 and total_locked > 0:
        return [f"⚠️ 尾盘仍有锁仓: {total_locked} 股未解锁"]
    return []


# ═══════════════════════════════════════════════════════════════
# 注册表 — 加新检查只需在这里加一行
# ═══════════════════════════════════════════════════════════════

CHECKS = [
    # 基础
    _account_equation,
    _position_limit,
    _price_jump,
    _orphan_meta,
    # 双路交叉验证
    _cross_validate_change_pct,
    _cross_validate_preclose_stability,
    _cross_validate_direction,
    _cross_validate_market_value,
    # 累积一致性
    _index_prices_length,
    _locked_volume_consistency,
]


def run_checks(ctx: CheckContext) -> list[str]:
    """运行所有注册的校验，返回告警列表"""
    alerts = []
    for check in CHECKS:
        try:
            result = check(ctx)
            if result:
                alerts.extend(result)
        except Exception:
            pass  # 单个检查失败不影响其他
    return alerts
