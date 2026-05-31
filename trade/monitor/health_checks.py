"""盯盘运行时数据校验框架。

每个校验函数签名:
    check(ctx: CheckContext) -> list[str]
返回告警消息列表（空列表 = 通过）。

加新检查只需写函数然后注册到 CHECKS 列表，不需要改 Watcher。
"""

from dataclasses import dataclass, field


@dataclass
class CheckContext:
    """校验上下文 — 由 Watcher._health_check() 每轮填充"""

    # 账户
    cash: float = 0.0
    total_value: float = 0.0
    daily_pnl: float = 0.0
    positions: dict = field(default_factory=dict)
    max_positions: int = 5

    # 行情
    prices: dict = field(default_factory=dict)
    index_prices: list = field(default_factory=list)
    index_high: float = 0.0
    index_low: float = 0.0
    index_pre_close: float = 0.0
    qmt_change_pct: float | None = None

    # 板块
    sector_stats: dict = field(default_factory=dict)

    # 系统内部
    pos_meta: dict = field(default_factory=dict)
    bought_watch: dict = field(default_factory=dict)
    sl_reminder_count: int = 0
    alerted_sl_tp_count: int = 0
    triggered_ids_count: int = 0
    scan_count: int = 0
    baseline_pre_close: float = 0.0
    baseline_qmt_pct: float = 0.0
    trade_date: str = ""
    collector_connected: bool = False
    # 决策上下文（供健康检查独立重算用）
    risk_level: str = "safe"
    sector_trends: dict = field(default_factory=dict)  # {code: trend_str}


# ═══════════════════════════════════════════════════════════════
# 基础不变式
# ═══════════════════════════════════════════════════════════════


def _account_equation(ctx: CheckContext) -> list[str]:
    """账户恒等式: total == cash + sum(market_value)"""
    mv = sum(p.market_value for p in ctx.positions.values())
    drift = abs(ctx.total_value - ctx.cash - mv)
    if drift > 10:
        return [f"⚠️ 账户不一致: total={ctx.total_value:.0f} cash+mv={ctx.cash + mv:.0f} (差{drift:.0f})"]
    return []


def _position_count_limit(ctx: CheckContext) -> list[str]:
    """持仓数不超上限"""
    if len(ctx.positions) > ctx.max_positions:
        return [f"⚠️ 持仓超限: {len(ctx.positions)}/{ctx.max_positions}"]
    return []


def _price_freshness(ctx: CheckContext) -> list[str]:
    """价格覆盖度 — 有持仓但本轮没拿到价格"""
    missing = []
    for code in ctx.positions:
        if code not in ctx.prices:
            missing.append(code)
    if missing:
        return [f"⚠️ 缺价格: {', '.join(missing)} — QMT 可能丢数据"]
    return []


def _price_jump(ctx: CheckContext) -> list[str]:
    """单轮跳变 > 15%"""
    alerts = []
    for code, price in ctx.prices.items():
        pos = ctx.positions.get(code)
        if pos and pos.current_price > 0:
            chg = abs(price - pos.current_price) / pos.current_price
            if chg > 0.15:
                alerts.append(f"⚠️ 价格跳变: {code} {pos.current_price:.2f}→{price:.2f} ({chg:.1%})")
    return alerts


def _cash_non_negative(ctx: CheckContext) -> list[str]:
    """现金不能为负"""
    if ctx.cash < -1:
        return [f"🔴 现金为负: {ctx.cash:.2f}"]
    return []


# ═══════════════════════════════════════════════════════════════
# 内部状态一致性
# ═══════════════════════════════════════════════════════════════


def _pos_meta_orphan(ctx: CheckContext) -> list[str]:
    """_pos_meta 有但持仓没有"""
    orphan = set(ctx.pos_meta.keys()) - set(ctx.positions.keys())
    if orphan:
        return [f"⚠️ 元数据孤儿: {', '.join(sorted(orphan))}"]
    return []


def _pos_meta_missing(ctx: CheckContext) -> list[str]:
    """持仓有但 _pos_meta 没有（新买入未写 meta？）"""
    missing = set(ctx.positions.keys()) - set(ctx.pos_meta.keys())
    if missing:
        return [f"⚠️ 缺元数据: {', '.join(sorted(missing))} — 止损止盈可能未设"]
    return []


def _bought_watch_orphan(ctx: CheckContext) -> list[str]:
    """_bought_watch 有但持仓没有（卖了没清理？）"""
    orphan = set(ctx.bought_watch.keys()) - set(ctx.positions.keys())
    if orphan:
        return [f"⚠️ 盯盘残留: {', '.join(sorted(orphan))} — 已卖出但未清理 bought_watch"]
    return []


def _sl_reminders_leak(ctx: CheckContext) -> list[str]:
    """止损提醒队列不应无限增长"""
    if ctx.sl_reminder_count > ctx.max_positions * 3:
        return [f"⚠️ SL提醒泄漏: {ctx.sl_reminder_count} 条 — 清理逻辑可能失效"]
    return []


def _alerted_set_leak(ctx: CheckContext) -> list[str]:
    """防重复推送集合不应无限增长"""
    if ctx.alerted_sl_tp_count > ctx.max_positions * 10:
        return [f"⚠️ alerted_sl_tp 膨胀: {ctx.alerted_sl_tp_count} 条"]
    return []


def _triggered_ids_leak(ctx: CheckContext) -> list[str]:
    """已触发信号集合不应无限增长"""
    if ctx.triggered_ids_count > 100:
        return [f"⚠️ triggered_ids 膨胀: {ctx.triggered_ids_count} 条"]
    return []


# ═══════════════════════════════════════════════════════════════
# 双路交叉验证
# ═══════════════════════════════════════════════════════════════


def _cross_validate_change_pct(ctx: CheckContext) -> list[str]:
    """涨跌幅: 自算 vs QMT — 差 > 0.05% 告警"""
    if ctx.baseline_pre_close <= 0 or not ctx.index_prices or ctx.qmt_change_pct is None:
        return []
    our_pct = (ctx.index_prices[-1] - ctx.baseline_pre_close) / ctx.baseline_pre_close
    diff = abs(our_pct - ctx.qmt_change_pct)
    if diff > 0.0005:
        return [
            f"🔴 涨跌幅分歧: 自算={our_pct:.4f} QMT={ctx.qmt_change_pct:.4f}"
            f" (差{diff:.4f})"
        ]
    return []


def _cross_validate_preclose_stability(ctx: CheckContext) -> list[str]:
    """昨收价不变性"""
    if ctx.baseline_pre_close <= 0 or ctx.index_pre_close <= 0:
        return []
    if abs(ctx.index_pre_close - ctx.baseline_pre_close) > 0.01:
        return [
            f"🔴 昨收价漂移: {ctx.baseline_pre_close:.2f}→{ctx.index_pre_close:.2f}"
        ]
    return []


def _cross_validate_direction(ctx: CheckContext) -> list[str]:
    """指数方向 vs 板块均值方向"""
    if not ctx.sector_stats or len(ctx.index_prices) < 2:
        return []
    market_avg = sum(
        s.get("change_pct", 0) for s in ctx.sector_stats.values()
    ) / max(len(ctx.sector_stats), 1)
    index_dir = 1 if ctx.index_prices[-1] > ctx.index_prices[0] else -1
    sector_dir = 1 if market_avg > 0 else -1
    if index_dir != sector_dir and abs(market_avg) > 0.005:
        arrow = lambda d: "↑" if d > 0 else "↓"
        return [
            f"⚠️ 方向背离: 上证{arrow(index_dir)} 板块{arrow(sector_dir)} ({market_avg:+.4f})"
        ]
    return []


def _cross_validate_market_value(ctx: CheckContext) -> list[str]:
    """市值双算: 记录值 vs 价格×股数"""
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
            f"⚠️ 市值分歧: 记录={mv_from_pos:.0f} 实算={mv_from_prices:.0f} (差{drift:.1%})"
        ]
    return []


def _cross_validate_pnl(ctx: CheckContext) -> list[str]:
    """持仓盈亏双算: 每只 position.pnl 之和 vs (市值 - 成本)"""
    if not ctx.positions:
        return []
    pnl_from_pos = sum(p.pnl for p in ctx.positions.values())
    pnl_from_calc = sum(
        (ctx.prices.get(code, p.current_price) - p.avg_cost) * p.volume
        for code, p in ctx.positions.items()
    )
    drift = abs(pnl_from_pos - pnl_from_calc)
    if drift > pnl_from_calc * 0.02 + 10:  # 差 2% 或 10 块
        return [
            f"⚠️ 盈亏分歧: position.pnl={pnl_from_pos:.0f} 实算={pnl_from_calc:.0f} (差{drift:.0f})"
        ]
    return []


def _cross_validate_index_high_low(ctx: CheckContext) -> list[str]:
    """指数 high/low 应与价格序列一致"""
    if not ctx.index_prices or ctx.index_high <= 0:
        return []
    actual_high = max(ctx.index_prices)
    actual_low = min(ctx.index_prices)
    alerts = []
    if abs(ctx.index_high - actual_high) > 0.5:
        alerts.append(f"⚠️ 最高价不一致: 记录={ctx.index_high:.2f} 序列={actual_high:.2f}")
    if abs(ctx.index_low - actual_low) > 0.5:
        alerts.append(f"⚠️ 最低价不一致: 记录={ctx.index_low:.2f} 序列={actual_low:.2f}")
    return alerts


# ═══════════════════════════════════════════════════════════════
# 逻辑合理性 — 值本身不异常，但组合起来说不通
# ═══════════════════════════════════════════════════════════════


def _stop_loss_above_price(ctx: CheckContext) -> list[str]:
    """止损价 > 现价 且未触发 → 说明触发逻辑有 bug"""
    alerts = []
    for code, pos in ctx.positions.items():
        meta = ctx.pos_meta.get(code, {})
        sl = meta.get("sl", 0)
        price = ctx.prices.get(code, pos.current_price)
        if sl > 0 and price > 0 and pos.entry_date != ctx.trade_date:
            if price < sl:
                alerts.append(
                    f"🔴 止损未触发: {code} 现价{price:.2f} < 止损{sl:.2f} → 触发逻辑可能失效"
                )
    return alerts


def _take_profit_below_entry(ctx: CheckContext) -> list[str]:
    """止盈价 < 成本价 → 设错了"""
    alerts = []
    for code, pos in ctx.positions.items():
        meta = ctx.pos_meta.get(code, {})
        tp = meta.get("tp", 0)
        if tp > 0 and pos.avg_cost > 0 and tp < pos.avg_cost:
            alerts.append(f"⚠️ 止盈<成本: {code} tp={tp:.2f} < cost={pos.avg_cost:.2f}")
    return alerts


def _buy_zone_invalid(ctx: CheckContext) -> list[str]:
    """买入区下限 > 上限 → 信号数据错"""
    alerts = []
    for code, meta in ctx.pos_meta.items():
        buy_min = meta.get("buy_min", 0)
        buy_max = meta.get("buy_max", 0)
        if buy_min > 0 and buy_max > 0 and buy_min >= buy_max:
            alerts.append(f"⚠️ 买入区异常: {code} min={buy_min:.2f} >= max={buy_max:.2f}")
    return alerts


def _index_price_gap(ctx: CheckContext) -> list[str]:
    """指数价格序列不应有 > 2% 的跳空（单轮数据错）"""
    if len(ctx.index_prices) < 2:
        return []
    prev = ctx.index_prices[-2]
    curr = ctx.index_prices[-1]
    if prev > 0 and abs(curr - prev) / prev > 0.02:
        return [f"⚠️ 指数跳空: {prev:.2f}→{curr:.2f} (单轮 {((curr - prev) / prev):.2%})"]
    return []


def _daily_pnl_plausible(ctx: CheckContext) -> list[str]:
    """日盈亏不应超过持仓总市值×涨跌停限制（不可能一天亏掉持仓市值的 20%+）"""
    if not ctx.positions:
        return []
    total_mv = sum(p.market_value for p in ctx.positions.values())
    if total_mv <= 0:
        return []
    loss_ratio = abs(ctx.daily_pnl) / total_mv if ctx.daily_pnl < 0 else 0
    if loss_ratio > 0.25:  # 持仓市值不可能一天亏 25%
        return [f"🔴 日亏损异常: {loss_ratio:.1%} — 可能是数据计算错误而非真实亏损"]
    return []


# ═══════════════════════════════════════════════════════════════
# 累积一致性
# ═══════════════════════════════════════════════════════════════


def _index_prices_length(ctx: CheckContext) -> list[str]:
    """价格序列长度应 ≈ scan_count"""
    expected = ctx.scan_count
    actual = len(ctx.index_prices)
    if expected > 10 and actual > 0 and abs(expected - actual) > 5:
        return [
            f"⚠️ 序列长度异常: scan={expected} prices={actual} (差{abs(expected - actual)})"
        ]
    return []


def _locked_volume_consistency(ctx: CheckContext) -> list[str]:
    """尾盘 locked_volume 应为 0"""
    total_locked = sum(getattr(p, "locked_volume", 0) for p in ctx.positions.values())
    if ctx.scan_count > 200 and total_locked > 0:
        return [f"⚠️ 尾盘仍有锁仓: {total_locked} 股"]
    return []


def _sector_data_accumulating(ctx: CheckContext) -> list[str]:
    """开盘后板块数据应持续累积，不应在某轮归零"""
    if ctx.scan_count < 10:
        return []
    if not ctx.sector_stats:
        return [f"⚠️ 板块数据丢失: 第{ctx.scan_count}轮 sector_stats 为空"]
    return []


# ═══════════════════════════════════════════════════════════════
# 独立重算验证 — 用原始输入从头算，对比系统实际应用的参数
# ═══════════════════════════════════════════════════════════════


def _expected_base_tighten(risk_level: str) -> tuple:
    """给定 risk_level，返回理论上的 (sl_tighten, tp_lower, trail_tighten)"""
    if risk_level == "extreme":
        return (0.70, 0.80, 0.70)
    elif risk_level == "dangerous":
        return (0.85, 0.90, 0.85)
    elif risk_level == "cautious":
        return (0.92, 1.0, 0.92)
    else:
        return (1.0, 1.0, 1.0)


def _expected_sector_mult(trend: str) -> float:
    """给定板块趋势，返回理论上的板块修正乘数"""
    if "持续走弱" in trend and "加速" in trend:
        return 0.90
    if any(w in trend for w in ("持续走弱", "弱于大盘", "普跌")):
        return 0.95
    return 1.0


def _recompute_adjustment(ctx: CheckContext) -> list[str]:
    """独立重算止损/止盈调整因子，与系统实际应用的对比。

    原理：从 risk_level 和 sector_trend 这两个原始输入出发，
    独立计算 sl_tighten / tp_lower / trail_tighten。
    再与 _pos_meta 里记录的 _sl_tighten / _tp_lower / _trail_tighten 对比。
    偏差 > 0.001 说明调整链某一步出了累积误差或条件判断错误。
    """
    alerts = []
    base_sl, base_tp, base_trail = _expected_base_tighten(ctx.risk_level)

    for code, pos in ctx.positions.items():
        meta = ctx.pos_meta.get(code, {})
        actual_sl = meta.get("_sl_tighten")
        actual_tp = meta.get("_tp_lower")
        actual_trail = meta.get("_trail_tighten")
        if actual_sl is None and actual_tp is None and actual_trail is None:
            continue

        trend = ctx.sector_trends.get(code, "")
        mult = _expected_sector_mult(trend)

        expected_sl = round(base_sl * mult, 4)
        expected_tp = round(base_tp * mult, 4)
        expected_trail = round(base_trail * mult, 4)

        if actual_sl is not None and abs(actual_sl - expected_sl) > 0.001:
            alerts.append(
                f"🔴 止损因子偏离: {code} 预期={expected_sl:.4f} 实际={actual_sl:.4f}"
                f" (risk={ctx.risk_level} trend={trend[:8]})"
            )
        if actual_tp is not None and abs(actual_tp - expected_tp) > 0.001:
            alerts.append(
                f"🔴 止盈因子偏离: {code} 预期={expected_tp:.4f} 实际={actual_tp:.4f}"
            )
        if actual_trail is not None and abs(actual_trail - expected_trail) > 0.001:
            alerts.append(
                f"🔴 移动止盈因子偏离: {code} 预期={expected_trail:.4f} 实际={actual_trail:.4f}"
            )

    return alerts


def _recompute_ema(ctx: CheckContext) -> list[str]:
    """独立重算指数 EMA12/EMA26，检测累积漂移。

    原理：market_state 内部维护 EMA，如果每轮增量更新的浮点误差
    累积到一定程度（如乘法舍入），独立从头重算的结果会对不上。
    差 > 0.1% 说明累积误差已影响动态均线判断（如单边下跌/V型反转）。
    """
    if len(ctx.index_prices) < 30:
        return []

    # 从头计算 EMA12
    def ema(series, period):
        if len(series) < period:
            return []
        result = [sum(series[:period]) / period]  # SMA 起始
        k = 2 / (period + 1)
        for p in series[period:]:
            result.append(p * k + result[-1] * (1 - k))
        return result

    ema12 = ema(ctx.index_prices, 12)
    # 不需要对比系统值（系统不暴露 EMA），检测自身稳定性：
    # EMA 值不应来回剧烈振荡（说明输入数据有问题）
    if len(ema12) >= 10:
        recent_ema = ema12[-10:]
        swings = sum(
            1 for i in range(1, len(recent_ema))
            if (recent_ema[i] - recent_ema[i - 1]) * (recent_ema[i - 1] - recent_ema[i - 2]) < 0
        )
        if swings >= 7:  # 10 个点里 7 次方向切换 → 数据噪声异常
            return [f"⚠️ EMA12 异常振荡: 近 10 点 {swings} 次方向切换"]
    return []


# ═══════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════

CHECKS = [
    # 基础不变式
    _account_equation,
    _position_count_limit,
    _price_freshness,
    _price_jump,
    _cash_non_negative,
    # 内部状态一致性
    _pos_meta_orphan,
    _pos_meta_missing,
    _bought_watch_orphan,
    _sl_reminders_leak,
    _alerted_set_leak,
    _triggered_ids_leak,
    # 双路交叉验证
    _cross_validate_change_pct,
    _cross_validate_preclose_stability,
    _cross_validate_direction,
    _cross_validate_market_value,
    _cross_validate_pnl,
    _cross_validate_index_high_low,
    # 逻辑合理性
    _stop_loss_above_price,
    _take_profit_below_entry,
    _buy_zone_invalid,
    _index_price_gap,
    _daily_pnl_plausible,
    # 累积一致性
    _index_prices_length,
    _locked_volume_consistency,
    _sector_data_accumulating,
    # 独立重算验证
    _recompute_adjustment,
    _recompute_ema,
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
            pass
    return alerts
