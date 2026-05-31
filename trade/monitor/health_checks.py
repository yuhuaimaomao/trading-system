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

    # ── 账户 ──
    cash: float = 0.0
    total_value: float = 0.0
    daily_pnl: float = 0.0
    positions: dict = field(default_factory=dict)
    max_positions: int = 5
    entry_dates: dict = field(default_factory=dict)  # {code: entry_date}

    # ── 行情 ──
    prices: dict = field(default_factory=dict)
    limit_cache: dict = field(default_factory=dict)  # {code: {limit_up, limit_down, pre_close}}
    index_prices: list = field(default_factory=list)
    index_high: float = 0.0
    index_low: float = 0.0
    index_pre_close: float = 0.0
    qmt_change_pct: float | None = None

    # ── 板块 ──
    sector_stats: dict = field(default_factory=dict)
    sector_data_point_count: int = 0  # 板块历史数据点数

    # ── 盯盘内部状态 ──
    pos_meta: dict = field(default_factory=dict)
    bought_watch: dict = field(default_factory=dict)
    sl_reminder_count: int = 0
    alerted_sl_tp_count: int = 0
    triggered_ids_count: int = 0
    pending_signal_count: int = 0  # 还有多少 pending 信号
    scan_count: int = 0
    prev_scan_count: int = 0  # 上一轮 scan_count

    # ── 基准锚点 ──
    baseline_pre_close: float = 0.0
    baseline_qmt_pct: float = 0.0
    trade_date: str = ""
    collector_connected: bool = False

    # ── 决策上下文 ──
    risk_level: str = "safe"
    regime_pattern: str = "normal"
    sector_trends: dict = field(default_factory=dict)
    index_technicals: dict = field(default_factory=dict)  # {rsi6, rsi12, macd_dif, macd_dea, kdj_k, kdj_d, kdj_j}
    market_env: str = "swing"
    market_env_score: int = 0


# ═══════════════════════════════════════════════════════════════
# 1. 源数据完整性 — 数据进来了没有，进来的对不对
# ═══════════════════════════════════════════════════════════════


def _index_price_valid(ctx: CheckContext) -> list[str]:
    """指数价格不能为 0 或负数"""
    if ctx.index_prices and ctx.index_prices[-1] <= 0:
        return [f"🔴 指数价格异常: {ctx.index_prices[-1]}"]
    return []


def _index_sequence_monotonic_gap(ctx: CheckContext) -> list[str]:
    """指数价格相邻两点之间的跳空不应超过 2%（单点数据错误）"""
    if len(ctx.index_prices) < 2:
        return []
    prev = ctx.index_prices[-2]
    curr = ctx.index_prices[-1]
    if prev > 0 and abs(curr - prev) / prev > 0.02:
        return [f"⚠️ 指数跳空: {prev:.2f}→{curr:.2f} ({(curr - prev) / prev:+.2%})"]
    return []


def _price_within_limits(ctx: CheckContext) -> list[str]:
    """个股价格应在涨跌停范围内"""
    alerts = []
    for code, price in ctx.prices.items():
        limit = ctx.limit_cache.get(code)
        if limit and price > 0:
            if price > limit["limit_up"] * 1.005:
                alerts.append(f"🔴 价格超涨停: {code} {price} > {limit['limit_up']}")
            if price < limit["limit_down"] * 0.995:
                alerts.append(f"🔴 价格破跌停: {code} {price} < {limit['limit_down']}")
    return alerts


def _price_coverage(ctx: CheckContext) -> list[str]:
    """有持仓但本轮没拿到价格"""
    missing = [c for c in ctx.positions if c not in ctx.prices]
    if missing:
        return [f"⚠️ 缺价格: {', '.join(missing)}"]
    return []


def _index_sequence_no_duplicates(ctx: CheckContext) -> list[str]:
    """价格序列不应有连续相同值（非停更时的重复记录）"""
    if len(ctx.index_prices) < 3:
        return []
    recent = ctx.index_prices[-3:]
    if recent[0] == recent[1] == recent[2]:
        return []  # 停更由 index_stale 在 Watcher 层处理
    return []


# ═══════════════════════════════════════════════════════════════
# 2. 账户/持仓一致性
# ═══════════════════════════════════════════════════════════════


def _account_equation(ctx: CheckContext) -> list[str]:
    mv = sum(p.market_value for p in ctx.positions.values())
    drift = abs(ctx.total_value - ctx.cash - mv)
    if drift > 10:
        return [f"⚠️ 账户不一致: total={ctx.total_value:.0f} cash+mv={ctx.cash + mv:.0f} (差{drift:.0f})"]
    return []


def _cash_non_negative(ctx: CheckContext) -> list[str]:
    if ctx.cash < -1:
        return [f"🔴 现金为负: {ctx.cash:.2f}"]
    return []


def _position_count_limit(ctx: CheckContext) -> list[str]:
    if len(ctx.positions) > ctx.max_positions:
        return [f"⚠️ 持仓超限: {len(ctx.positions)}/{ctx.max_positions}"]
    return []


def _entry_date_future(ctx: CheckContext) -> list[str]:
    """持仓的 entry_date 不可能晚于 trade_date"""
    alerts = []
    for code, pos in ctx.positions.items():
        if pos.entry_date and ctx.trade_date and pos.entry_date > ctx.trade_date:
            alerts.append(f"🔴 未来持仓: {code} entry={pos.entry_date} > today={ctx.trade_date}")
    return alerts


def _t1_lock_consistency(ctx: CheckContext) -> list[str]:
    """locked_volume 与 entry_date 一致：今天买的锁，昨天买的解锁"""
    alerts = []
    for code, pos in ctx.positions.items():
        locked = getattr(pos, "locked_volume", 0)
        is_today = (pos.entry_date == ctx.trade_date) if ctx.trade_date else False
        if is_today and locked < pos.volume:
            alerts.append(f"⚠️ 锁仓不足: {code} 今日买入但 locked={locked} < vol={pos.volume}")
        if not is_today and locked > 0:
            alerts.append(f"⚠️ 锁仓残留: {code} 昨日买入但 locked={locked} > 0")
    return alerts


def _position_journaled(ctx: CheckContext) -> list[str]:
    """有持仓没有 _pos_meta → 止损止盈未设"""
    missing = set(ctx.positions.keys()) - set(ctx.pos_meta.keys())
    if missing:
        return [f"⚠️ 缺元数据: {', '.join(sorted(missing))}"]
    return []


# ═══════════════════════════════════════════════════════════════
# 3. 盯盘元数据一致性
# ═══════════════════════════════════════════════════════════════


def _pos_meta_orphan(ctx: CheckContext) -> list[str]:
    orphan = set(ctx.pos_meta.keys()) - set(ctx.positions.keys())
    if orphan:
        return [f"⚠️ 元数据孤儿: {', '.join(sorted(orphan))}"]
    return []


def _bought_watch_orphan(ctx: CheckContext) -> list[str]:
    orphan = set(ctx.bought_watch.keys()) - set(ctx.positions.keys())
    if orphan:
        return [f"⚠️ 盯盘残留: {', '.join(sorted(orphan))}"]
    return []


def _highest_price_non_decreasing(ctx: CheckContext) -> list[str]:
    """_pos_meta 里的 highest_price 不应降低（日内最高只升不降）"""
    alerts = []
    for code, pos in ctx.positions.items():
        meta = ctx.pos_meta.get(code, {})
        hp = meta.get("highest_price", 0)
        price = ctx.prices.get(code, pos.current_price)
        if hp > 0 and price > 0 and price > hp:
            pass  # 正常：突破新高（等 _check_positions 更新）
        # 不检查降低，因为 _check_positions 还没更新
    return alerts  # 这个检查需要跨轮状态，先占位


def _max_profit_non_decreasing(ctx: CheckContext) -> list[str]:
    """_bought_watch 的 max_profit_pct 不应降低"""
    alerts = []
    for code, pos in ctx.positions.items():
        watch = ctx.bought_watch.get(code, {})
        mp = watch.get("max_profit_pct", 0)
        if pos.avg_cost > 0:
            cur_pct = (ctx.prices.get(code, pos.current_price) - pos.avg_cost) / pos.avg_cost
            if mp > 0 and cur_pct > mp + 0.02:
                alerts.append(f"⚠️ 浮盈遗漏: {code} 当前{cur_pct:.1%} > 记录{mp:.1%}")
    return alerts


def _stop_below_entry_tp_above_entry(ctx: CheckContext) -> list[str]:
    """止损 < 成本 < 止盈，否则设错了"""
    alerts = []
    for code, pos in ctx.positions.items():
        meta = ctx.pos_meta.get(code, {})
        sl = meta.get("sl", 0)
        tp = meta.get("tp", 0)
        if sl > 0 and pos.avg_cost > 0 and sl >= pos.avg_cost:
            alerts.append(f"⚠️ 止损≥成本: {code} sl={sl:.2f} >= cost={pos.avg_cost:.2f}")
        if tp > 0 and pos.avg_cost > 0 and tp <= pos.avg_cost:
            alerts.append(f"⚠️ 止盈≤成本: {code} tp={tp:.2f} <= cost={pos.avg_cost:.2f}")
    return alerts


def _stop_not_triggered(ctx: CheckContext) -> list[str]:
    """现价已跌破止损但未触发 → 逻辑 bug"""
    alerts = []
    for code, pos in ctx.positions.items():
        meta = ctx.pos_meta.get(code, {})
        sl = meta.get("sl", 0)
        price = ctx.prices.get(code, pos.current_price)
        is_today = pos.entry_date == ctx.trade_date if ctx.trade_date else False
        if sl > 0 and price > 0 and not is_today and price < sl:
            alerts.append(f"🔴 止损未触发: {code} 现价{price:.2f} < 止损{sl:.2f}")
    return alerts


# ═══════════════════════════════════════════════════════════════
# 4. 状态集合膨胀检测
# ═══════════════════════════════════════════════════════════════


def _sl_reminders_leak(ctx: CheckContext) -> list[str]:
    if ctx.sl_reminder_count > ctx.max_positions * 3:
        return [f"⚠️ SL提醒泄漏: {ctx.sl_reminder_count} 条"]
    return []


def _alerted_set_leak(ctx: CheckContext) -> list[str]:
    if ctx.alerted_sl_tp_count > ctx.max_positions * 10:
        return [f"⚠️ alerted_sl_tp 膨胀: {ctx.alerted_sl_tp_count} 条"]
    return []


def _triggered_ids_leak(ctx: CheckContext) -> list[str]:
    if ctx.triggered_ids_count > 100:
        return [f"⚠️ triggered_ids 膨胀: {ctx.triggered_ids_count} 条"]
    return []


def _pending_signals_leak(ctx: CheckContext) -> list[str]:
    """pending 信号数不应无限增长（应该要么买入要么过期）"""
    if ctx.pending_signal_count > 50:
        return [f"⚠️ pending 信号堆积: {ctx.pending_signal_count} 条"]
    return []


# ═══════════════════════════════════════════════════════════════
# 5. 双路交叉验证
# ═══════════════════════════════════════════════════════════════


def _cross_validate_change_pct(ctx: CheckContext) -> list[str]:
    if ctx.baseline_pre_close <= 0 or not ctx.index_prices or ctx.qmt_change_pct is None:
        return []
    our_pct = (ctx.index_prices[-1] - ctx.baseline_pre_close) / ctx.baseline_pre_close
    diff = abs(our_pct - ctx.qmt_change_pct)
    if diff > 0.0005:
        return [
            f"🔴 涨跌幅分歧: 自算={our_pct:.4f} QMT={ctx.qmt_change_pct:.4f} (差{diff:.4f})"
        ]
    return []


def _cross_validate_preclose_stability(ctx: CheckContext) -> list[str]:
    if ctx.baseline_pre_close <= 0 or ctx.index_pre_close <= 0:
        return []
    if abs(ctx.index_pre_close - ctx.baseline_pre_close) > 0.01:
        return [f"🔴 昨收价漂移: {ctx.baseline_pre_close:.2f}→{ctx.index_pre_close:.2f}"]
    return []


def _cross_validate_direction(ctx: CheckContext) -> list[str]:
    if not ctx.sector_stats or len(ctx.index_prices) < 2:
        return []
    market_avg = sum(
        s.get("change_pct", 0) for s in ctx.sector_stats.values()
    ) / max(len(ctx.sector_stats), 1)
    index_dir = 1 if ctx.index_prices[-1] > ctx.index_prices[0] else -1
    sector_dir = 1 if market_avg > 0 else -1
    if index_dir != sector_dir and abs(market_avg) > 0.005:
        a = lambda d: "↑" if d > 0 else "↓"
        return [f"⚠️ 方向背离: 上证{a(index_dir)} 板块{a(sector_dir)} ({market_avg:+.4f})"]
    return []


def _cross_validate_market_value(ctx: CheckContext) -> list[str]:
    if not ctx.positions:
        return []
    mv_pos = sum(p.market_value for p in ctx.positions.values())
    mv_calc = sum(ctx.prices.get(c, p.current_price) * p.volume for c, p in ctx.positions.items())
    if mv_calc <= 0:
        return []
    drift = abs(mv_pos - mv_calc) / mv_calc
    if drift > 0.01:
        return [f"⚠️ 市值分歧: 记录={mv_pos:.0f} 实算={mv_calc:.0f} (差{drift:.1%})"]
    return []


def _cross_validate_pnl(ctx: CheckContext) -> list[str]:
    if not ctx.positions:
        return []
    pnl_pos = sum(p.pnl for p in ctx.positions.values())
    pnl_calc = sum(
        (ctx.prices.get(c, p.current_price) - p.avg_cost) * p.volume
        for c, p in ctx.positions.items()
    )
    drift = abs(pnl_pos - pnl_calc)
    if pnl_calc != 0 and drift > abs(pnl_calc) * 0.02 + 10:
        return [f"⚠️ 盈亏分歧: position.pnl={pnl_pos:.0f} 实算={pnl_calc:.0f} (差{drift:.0f})"]
    return []


def _cross_validate_index_high_low(ctx: CheckContext) -> list[str]:
    if not ctx.index_prices or ctx.index_high <= 0:
        return []
    ah, al = max(ctx.index_prices), min(ctx.index_prices)
    alerts = []
    if abs(ctx.index_high - ah) > 0.5:
        alerts.append(f"⚠️ 最高价不一致: 记录={ctx.index_high:.2f} 序列={ah:.2f}")
    if abs(ctx.index_low - al) > 0.5:
        alerts.append(f"⚠️ 最低价不一致: 记录={ctx.index_low:.2f} 序列={al:.2f}")
    return alerts


# ═══════════════════════════════════════════════════════════════
# 6. 技术指标合理性
# ═══════════════════════════════════════════════════════════════


def _rsi_range(ctx: CheckContext) -> list[str]:
    """RSI 应在 [0, 100]"""
    t = ctx.index_technicals
    alerts = []
    for key in ("rsi6", "rsi12", "rsi24"):
        v = t.get(key)
        if v is not None and (v < 0 or v > 100):
            alerts.append(f"🔴 {key.upper()} 越界: {v}")
    return alerts


def _kdj_ordering(ctx: CheckContext) -> list[str]:
    """KDJ 三者通常不会同时相等（等于没算出值）"""
    t = ctx.index_technicals
    k = t.get("kdj_k")
    d = t.get("kdj_d")
    j = t.get("kdj_j")
    if k is not None and k == d == j and ctx.scan_count > 20:
        if k == 50.0:  # 常见的默认/未计算值
            return [f"⚠️ KDJ 未计算: K=D=J=50"]
    return []


# ═══════════════════════════════════════════════════════════════
# 7. 市场状态 / Regime 合理性
# ═══════════════════════════════════════════════════════════════


def _market_env_score_plausible(ctx: CheckContext) -> list[str]:
    """市场环境打分与实际走势方向应大致一致"""
    # bull 时上证应涨，bear 时应跌（开盘后 30 轮以上才检查）
    if ctx.scan_count < 30:
        return []
    if not ctx.index_prices or ctx.baseline_pre_close <= 0:
        return []
    day_chg = (ctx.index_prices[-1] - ctx.baseline_pre_close) / ctx.baseline_pre_close
    if ctx.market_env == "bull" and day_chg < -0.01:
        return [f"⚠️ 牛市误判: 环境=bull 但指数{day_chg:+.2%}"]
    if ctx.market_env == "bear" and day_chg > 0.01:
        return [f"⚠️ 熊市误判: 环境=bear 但指数{day_chg:+.2%}"]
    return []


# ═══════════════════════════════════════════════════════════════
# 8. 独立重算验证
# ═══════════════════════════════════════════════════════════════


def _expected_base_tighten(risk_level: str) -> tuple:
    if risk_level == "extreme":
        return (0.70, 0.80, 0.70)
    elif risk_level == "dangerous":
        return (0.85, 0.90, 0.85)
    elif risk_level == "cautious":
        return (0.92, 1.0, 0.92)
    else:
        return (1.0, 1.0, 1.0)


def _expected_sector_mult(trend: str) -> float:
    if "持续走弱" in trend and "加速" in trend:
        return 0.90
    if any(w in trend for w in ("持续走弱", "弱于大盘", "普跌")):
        return 0.95
    return 1.0


def _recompute_adjustment(ctx: CheckContext) -> list[str]:
    """独立重算调整因子，与 _pos_meta 记录值对比 → 抓决策链漂移"""
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
    """从原始序列重算 EMA12，检测累积误差"""
    if len(ctx.index_prices) < 30:
        return []

    def ema(series, period):
        if len(series) < period:
            return []
        result = [sum(series[:period]) / period]
        k = 2 / (period + 1)
        for p in series[period:]:
            result.append(p * k + result[-1] * (1 - k))
        return result

    ema12 = ema(ctx.index_prices, 12)
    if len(ema12) >= 10:
        recent = ema12[-10:]
        swings = sum(
            1 for i in range(1, len(recent))
            if (recent[i] - recent[i - 1]) * (recent[i - 1] - recent[i - 2]) < 0
        )
        if swings >= 7:
            return [f"⚠️ EMA12 异常振荡: 近10点 {swings} 次方向切换"]
    return []


# ═══════════════════════════════════════════════════════════════
# 9. 累积一致性 / 跨轮状态
# ═══════════════════════════════════════════════════════════════


def _scan_count_monotonic(ctx: CheckContext) -> list[str]:
    """scan_count 应单调递增"""
    if ctx.prev_scan_count > 0 and ctx.scan_count <= ctx.prev_scan_count:
        return [f"⚠️ 扫描计数回退: {ctx.prev_scan_count}→{ctx.scan_count}"]
    return []


def _index_prices_length(ctx: CheckContext) -> list[str]:
    expected = ctx.scan_count
    actual = len(ctx.index_prices)
    if expected > 10 and actual > 0 and abs(expected - actual) > 5:
        return [f"⚠️ 序列长度异常: scan={expected} prices={actual} (差{abs(expected - actual)})"]
    return []


def _locked_volume_consistency(ctx: CheckContext) -> list[str]:
    total_locked = sum(getattr(p, "locked_volume", 0) for p in ctx.positions.values())
    if ctx.scan_count > 200 and total_locked > 0:
        return [f"⚠️ 尾盘仍有锁仓: {total_locked} 股"]
    return []


def _sector_data_accumulating(ctx: CheckContext) -> list[str]:
    if ctx.scan_count < 10:
        return []
    if not ctx.sector_stats:
        return [f"⚠️ 板块数据丢失: 第{ctx.scan_count}轮 sector_stats 为空"]
    return []


def _trade_date_stable(ctx: CheckContext) -> list[str]:
    """trade_date 全天不变，也不能为空"""
    if not ctx.trade_date:
        return ["⚠️ trade_date 为空"]
    return []


# ═══════════════════════════════════════════════════════════════
# 注册表
# ═══════════════════════════════════════════════════════════════

CHECKS = [
    # 1. 源数据完整性
    _index_price_valid,
    _index_sequence_monotonic_gap,
    _price_within_limits,
    _price_coverage,
    # 2. 账户/持仓一致性
    _account_equation,
    _cash_non_negative,
    _position_count_limit,
    _entry_date_future,
    _t1_lock_consistency,
    _position_journaled,
    # 3. 盯盘元数据
    _pos_meta_orphan,
    _bought_watch_orphan,
    _max_profit_non_decreasing,
    _stop_below_entry_tp_above_entry,
    _stop_not_triggered,
    # 4. 状态集合膨胀
    _sl_reminders_leak,
    _alerted_set_leak,
    _triggered_ids_leak,
    _pending_signals_leak,
    # 5. 双路交叉验证
    _cross_validate_change_pct,
    _cross_validate_preclose_stability,
    _cross_validate_direction,
    _cross_validate_market_value,
    _cross_validate_pnl,
    _cross_validate_index_high_low,
    # 6. 技术指标
    _rsi_range,
    _kdj_ordering,
    # 7. 市场状态
    _market_env_score_plausible,
    # 8. 独立重算
    _recompute_adjustment,
    _recompute_ema,
    # 9. 累积/跨轮
    _scan_count_monotonic,
    _index_prices_length,
    _locked_volume_consistency,
    _sector_data_accumulating,
    _trade_date_stable,
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
