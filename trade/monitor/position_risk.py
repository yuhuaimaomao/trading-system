"""持仓风控：止损止盈、移动止损、回撤止损、被套/补仓分类.

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性.
"""

import logging
import sqlite3
import time
from datetime import datetime
from datetime import time as dt_time

from system.config import settings
from trade.risk.rules.stop_loss import should_stop_loss
from trade.risk.rules.take_profit import should_take_profit, should_trailing_stop

logger = logging.getLogger(__name__)


class PositionRiskMixin:
    """持仓风控：止损止盈、移动止损、回撤止损、被套/补仓分类."""

    # 开盘缓冲：开盘后 N 秒内不触发止损（防止开盘恐慌扫止损）
    OPENING_BUFFER_SECONDS = 300

    def _minutes_since_open(self) -> float:
        from datetime import date

        morning = datetime.combine(date.today(), dt_time(9, 30))
        return (datetime.now() - morning).total_seconds() / 60

    def _check_positions(self, prices: dict[str, float]):
        # 大盘 + 板块环境，用于动态调整止损止盈触发条件
        regime = getattr(self, "_regime", None)
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"
        pattern = getattr(regime, "pattern", "normal") if regime else "normal"

        # 开盘缓冲期内跳过止损止盈执行（价格序列不稳定，容易恐慌扫止损）
        seconds_since_open = self._minutes_since_open() * 60
        in_opening_buffer = seconds_since_open < self.OPENING_BUFFER_SECONDS

        # ── 日内熔断：日亏损 > 3%，所有浮亏仓位立即平仓 ──
        pa = self.paper_account
        if pa.daily_pnl < 0 and pa.total_value > 0:
            loss_ratio = abs(pa.daily_pnl) / pa.total_value
            if loss_ratio > settings.MAX_DAILY_LOSS:
                logger.warning(
                    f"日内熔断触发: 日亏损 {loss_ratio:.1%} > {settings.MAX_DAILY_LOSS:.0%}"
                )
                closed = []
                blocked_t1 = []
                for code, pos in list(pa.positions.items()):
                    if pos.pnl_pct is not None and pos.pnl_pct < 0:
                        if pos.available_volume <= 0:
                            blocked_t1.append(f"{code} {pos.stock_name}")
                            continue
                        price = prices.get(code) or pos.current_price
                        result = pa.sell(
                            code, price, f"日内熔断 (日亏损 {loss_ratio:.1%})"
                        )
                        if result.success:
                            closed.append(f"{code} {pos.stock_name}")
                            self._pos_meta.pop(code, None)
                msg = f"🚨 日内熔断: 日亏损 {loss_ratio:.1%}，已平仓: {', '.join(closed) if closed else '无'}"
                if blocked_t1:
                    msg += f"\n🔒 T+1 锁定无法卖出: {', '.join(blocked_t1)}"
                self._alert(msg)
                return  # 熔断后本轮不再逐只检查

        # 基础调整因子（每只票从基础值开始，不在循环中累积）
        if risk_level == "extreme":
            base_sl_tighten = 0.70  # 止损线上移 30%
            base_tp_lower = 0.80  # 止盈线下移 20%
            base_trail_tighten = 0.70  # 移动止盈回撤容忍缩 30%
        elif risk_level == "dangerous":
            base_sl_tighten = 0.85
            base_tp_lower = 0.90
            base_trail_tighten = 0.85
        elif risk_level == "cautious":
            base_sl_tighten = 0.92
            base_tp_lower = 1.0  # 止盈不动
            base_trail_tighten = 0.92
        else:
            base_sl_tighten = 1.0
            base_tp_lower = 1.0
            base_trail_tighten = 1.0

        # 自动补全 _pos_meta（买入持久化失败等边缘情况可能导致缺条目）
        for code, pos in self.paper_account.positions.items():
            if code not in self._pos_meta:
                self._pos_meta[code] = {
                    "sl": 0,
                    "tp": 0,
                    "trailing_stop": 0.05,
                    "highest_price": pos.current_price or 0,
                    "sector": "",
                    "score": 0,
                    "signal_id": None,
                }
                logger.warning(f"自动补全缺失元数据: {code}")

        t1_locked = 0
        for code, pos in list(self.paper_account.positions.items()):
            meta = self._pos_meta.get(code, {})
            sl = meta.get("sl", 0)
            tp = meta.get("tp", 0)
            trailing_stop = meta.get("trailing_stop", 0.05)
            highest_price = meta.get("highest_price", 0)
            price = prices.get(code)
            if price is None:
                # fallback: 用持仓记录的当前价格（可能是上一轮的价格或昨收价）
                price = pos.current_price
            if price is None or price <= 0:
                continue

            is_today_buy = pos.entry_date == self._trade_date
            if is_today_buy:
                t1_locked += 1
            trend = self._get_sector_trend(code)
            limit_down = self._is_limit_down(code, price)
            is_sector_weak = any(w in trend for w in ("持续走弱", "弱于大盘", "普跌"))
            is_sector_accel_down = "持续走弱" in trend and "加速" in trend

            # 每只票从基础值开始，叠加板块修正
            sl_tighten = base_sl_tighten
            tp_lower = base_tp_lower
            trail_tighten = base_trail_tighten

            # 板块走弱 → 额外收紧 5%，加速走弱 → 额外收紧 10%
            if is_sector_accel_down:
                sl_tighten *= 0.90
                tp_lower *= 0.90
                trail_tighten *= 0.90
            elif is_sector_weak:
                sl_tighten *= 0.95
                tp_lower *= 0.95
                trail_tighten *= 0.95

            # 记录调整因子供健康检查独立重算验证
            if code not in self._pos_meta:
                self._pos_meta[code] = {}
            self._pos_meta[code]["_sl_tighten"] = sl_tighten
            self._pos_meta[code]["_tp_lower"] = tp_lower
            self._pos_meta[code]["_trail_tighten"] = trail_tighten

            pnl_pct = (
                (price - pos.avg_cost) / pos.avg_cost * 100 if pos.avg_cost > 0 else 0
            )

            # T+1 前不触发止损止盈
            if is_today_buy:
                # 每30轮汇总输出一次，避免刷屏
                if self._scan_count % 30 == 0:
                    logger.info(
                        f"持仓风控 [{code} {pos.stock_name}] T+1锁定 跳过止损止盈 "
                        f"价格{price:.2f} 成本{pos.avg_cost:.2f} 盈亏{pnl_pct:+.1f}%"
                    )
            elif in_opening_buffer and pnl_pct > -5:
                # 开盘缓冲期：T+1前持仓跳过止损（防止开盘恐慌扫止损）
                # 仅当浮亏不超 -5% 时跳过 — 真正的暴跌不止损更危险
                if self._scan_count % 5 == 0:
                    logger.info(
                        f"持仓风控 [{code} {pos.stock_name}] 开盘缓冲 跳过止损 "
                        f"价格{price:.2f} 盈亏{pnl_pct:+.1f}% "
                        f"距开盘{seconds_since_open:.0f}s"
                    )
            else:
                # ── 止损：大盘/板块弱时收紧触发线 ──
                triggered, effective_sl = should_stop_loss(
                    price, pos.avg_cost, sl, sl_tighten
                )
                if triggered:
                    key = f"{code}:sl"
                    loss_pct = -pnl_pct  # pnl_pct = (price-cost)/cost，正值=盈利
                    extra = ""  # 止损附言，深跌分支会覆盖

                    # ━━ 深跌判断：亏损超 7%，不立即止损，等反弹机会 ━━
                    # 状态机：深跌 → 等反弹 → 检测反弹失败 → 止损
                    if loss_pct > 7 and not is_today_buy:
                        deep_state = meta.get("_deep_loss", {})
                        hour = datetime.now().hour
                        minute = datetime.now().minute
                        afternoon = hour >= 14 or (hour == 13 and minute >= 30)

                        if not deep_state:
                            deep_state = {
                                "entry_price": price,
                                "lowest": price,
                                "start_scan": self._scan_count,
                                "rebound_high": 0,
                                "rebound_scan": 0,
                                "failed": False,
                                "sector_at_entry": trend,
                                "last_ai_scan": -100,  # 首次触发时立即调AI
                            }
                            meta["_deep_loss"] = deep_state
                            logger.info(
                                f"深跌等待 [{code}] 亏损{loss_pct:.1f}%>7% "
                                f"现价{price:.2f} 等待反弹机会"
                            )
                            self._alert(
                                f"🔄 深跌等待反弹 — {code} {pos.stock_name}\n"
                                f"   现价: {price:.2f}  亏损: {-loss_pct:+.1f}%\n"
                                f"   止损触发但跌幅已深，等待反弹评估\n"
                                f"   板块:{trend}"
                            )
                            # 异步 AI：被套离场分析
                            self._submit_trapped_exit_ai(
                                code,
                                pos.stock_name,
                                price,
                                pos.avg_cost,
                                sl,
                                tp,
                                trend,
                                deep_state,
                            )

                        # 更新最低价
                        if price < deep_state.get("lowest", price):
                            deep_state["lowest"] = price

                        lowest = deep_state.get("lowest", price)
                        rebound = (price - lowest) / lowest * 100 if lowest > 0 else 0
                        rebound_high = deep_state.get("rebound_high", 0)

                        # ── 更新反弹高点（从最低反弹 ≥3%）──
                        if rebound >= 3 and price > rebound_high:
                            is_new_high = rebound_high == 0
                            deep_state["rebound_high"] = price
                            deep_state["rebound_scan"] = self._scan_count
                            if is_new_high:
                                logger.info(
                                    f"深跌反弹 [{code}] 最低{lowest:.2f}→现价{price:.2f} "
                                    f"(+{rebound:.1f}%) 开始监控反弹失败"
                                )
                            # 反弹新高 → 异步 AI 重新评估离场时机
                            if (
                                self._scan_count - deep_state.get("last_ai_scan", -100)
                                >= 20
                            ):
                                deep_state["last_ai_scan"] = self._scan_count
                                self._submit_trapped_exit_ai(
                                    code,
                                    pos.stock_name,
                                    price,
                                    pos.avg_cost,
                                    sl,
                                    tp,
                                    trend,
                                    deep_state,
                                )

                        # ── 反弹失败检测（代码级，不等 AI 不等 14:00）──
                        fail_reason = None
                        if rebound_high > 0:
                            drop_from_high = (rebound_high - price) / rebound_high * 100
                            # 条件1: 从反弹高点回落 ≥2%
                            if drop_from_high >= 2:
                                fail_reason = f"反弹高点{rebound_high:.2f}回落{drop_from_high:.1f}%"
                            # 条件2: 横盘 ≥30 轮 + 技术指标无改善
                            elif (
                                self._scan_count - deep_state.get("rebound_scan", 0)
                                >= 30
                            ):
                                if not self._deep_rebound_improving(code, deep_state):
                                    fail_reason = (
                                        f"反弹后横盘"
                                        f"{self._scan_count - deep_state['rebound_scan']}轮"
                                        f" 指标无改善"
                                    )
                            # 条件3: 板块加速走弱 + 价格低于反弹高点
                            elif is_sector_accel_down and price < rebound_high * 0.99:
                                fail_reason = "板块加速走弱 反弹夭折"

                        if fail_reason:
                            deep_state["failed"] = True
                            extra = f"反弹失败({fail_reason})"
                            logger.info(
                                f"深跌反弹失败 [{code}] {fail_reason} 立即止损 "
                                f"现价{price:.2f} 亏损{loss_pct:.1f}%"
                            )
                            # 跳出深跌分支，走正常止损执行
                        elif not afternoon:
                            # 上午/午休：未检测到失败，继续等
                            continue
                        else:
                            # 14:00 后：根据反弹情况决定
                            if rebound >= 3:
                                if (
                                    self._scan_count
                                    - deep_state.get("last_alert_scan", 0)
                                    >= 30
                                ):
                                    deep_state["last_alert_scan"] = self._scan_count
                                    self._alert(
                                        f"↗️ 深跌反弹中 — {code} {pos.stock_name}\n"
                                        f"   现价: {price:.2f}  亏损: {-loss_pct:+.1f}%\n"
                                        f"   从低点反弹: +{rebound:.1f}%  继续持有等卖点"
                                    )
                                continue
                            elif rebound >= 1:
                                # 小反弹：尾盘14:45+就卖，否则再等
                                if hour < 14 or (hour == 14 and minute < 45):
                                    continue
                                extra = "深跌弱反弹，尾盘止损"
                            else:
                                extra = "深跌无反弹，尾盘止损"

                        # 没有 fail_reason → continue 已跳过了，到这里一定是失败或下午
                        if not fail_reason:
                            continue
                    # ━━ 正常止损 ━━

                    _extra = ""
                    if sl_tighten < 1.0:
                        _extra = f"大盘{risk_level}→止损收紧至{effective_sl:.2f}"
                    if extra:
                        _extra = extra if not _extra else f"{_extra}; {extra}"
                    try:
                        self._log_stop_trigger(
                            stock_code=code,
                            stype="止损",
                            trigger_price=effective_sl,
                            avg_cost=pos.avg_cost,
                            pnl_pct=pnl_pct,
                            risk_level=risk_level,
                            sl_original=sl,
                            sl_effective=effective_sl,
                        )
                    except Exception:
                        pass
                    self._handle_stop_signal(
                        key,
                        code,
                        pos.stock_name,
                        "止损",
                        price,
                        effective_sl,
                        pos.avg_cost,
                        trend,
                        limit_down,
                        extra=_extra,
                    )
                    continue

                # ── 止盈：大盘危险时提前锁定利润 ──
                triggered, effective_tp = should_take_profit(
                    price, pos.avg_cost, tp, tp_lower
                )
                if triggered:
                    key = f"{code}:tp"
                    stype = "止盈(收紧)" if tp_lower < 1.0 else "止盈"
                    extra = ""
                    if tp_lower < 1.0:
                        extra = f"大盘{risk_level}→止盈下调至{effective_tp:.2f}"
                    try:
                        self._log_tp_trigger(
                            stock_code=code,
                            stype=stype,
                            trigger_price=effective_tp,
                            avg_cost=pos.avg_cost,
                            pnl_pct=pnl_pct,
                            tp_original=tp,
                            tp_effective=effective_tp,
                        )
                    except Exception:
                        pass
                    self._handle_stop_signal(
                        key,
                        code,
                        pos.stock_name,
                        stype,
                        price,
                        effective_tp,
                        pos.avg_cost,
                        trend,
                        limit_down,
                        extra=extra,
                    )
                    continue

                # ── 移动止盈：大盘危险时缩小回撤容忍 ──
                triggered, trail_price = should_trailing_stop(
                    price, highest_price, trailing_stop, trail_tighten
                )
                if triggered:
                    key = f"{code}:trail"
                    try:
                        self._log_stop_trigger(
                            stock_code=code,
                            stype="移动止盈",
                            trigger_price=trail_price,
                            avg_cost=highest_price,
                            pnl_pct=(price - pos.avg_cost) / pos.avg_cost
                            if pos.avg_cost
                            else 0,
                            risk_level=risk_level,
                            highest_price=highest_price,
                        )
                    except Exception:
                        pass
                    self._handle_stop_signal(
                        key,
                        code,
                        pos.stock_name,
                        "移动止盈",
                        price,
                        trail_price,
                        highest_price,
                        trend,
                        limit_down,
                        extra=f"最高{highest_price:.2f}",
                    )
                    continue

                # ── 利润回撤止盈：大盘危险时保留更多利润 ──
                retrace_key, retrace_signal = self._check_retracement_stop(
                    code,
                    pos.stock_name,
                    price,
                    pos.avg_cost,
                    trend,
                    limit_down,
                    risk_level=risk_level,
                )
                if retrace_signal:
                    try:
                        pnl_pct = (
                            (price - pos.avg_cost) / pos.avg_cost if pos.avg_cost else 0
                        )
                        self._log_stop_trigger(
                            stock_code=code,
                            stype="利润回撤止盈",
                            trigger_price=price,
                            avg_cost=pos.avg_cost,
                            pnl_pct=pnl_pct,
                            risk_level=risk_level,
                        )
                    except Exception:
                        pass
                    self._handle_stop_signal(**retrace_signal)
                    continue

            # 更新最高浮盈（即使 T+1 锁定也记录）
            if pos.avg_cost > 0:
                cur_pct = (price - pos.avg_cost) / pos.avg_cost
                watch = self._bought_watch.setdefault(code, {"max_profit_pct": 0})
                if cur_pct > watch.get("max_profit_pct", 0):
                    watch["max_profit_pct"] = cur_pct

            pos.update_price(price)
            if price > highest_price:
                if code not in self._pos_meta:
                    self._pos_meta[code] = {}
                self._pos_meta[code]["highest_price"] = price

        # 每10轮输出持仓风控摘要
        if self._scan_count % 10 == 0:
            total_positions = len(self.paper_account.positions)
            logger.info(
                f"持仓风控摘要 扫描#{self._scan_count} 持仓{total_positions}只 "
                f"T+1锁定{t1_locked}只 可操作{total_positions - t1_locked}只 "
                f"风险等级{risk_level}"
            )

    def _check_stale_positions(self, prices: dict[str, float]):
        """主动退出：在硬止损触发前识别该走的仓位。

        6 种场景：僵持、时间止损、板块转弱、开盘压力、利润回吐、机会成本。
        满足 ≥2 个条件时建议/执行卖出。
        """
        if self._scan_count % 10 != 0:  # 每 10 轮检查一次
            return

        now = datetime.now()
        pos_count = len(self.paper_account.positions)
        pa = self.paper_account

        for code, pos in list(pa.positions.items()):
            price = prices.get(code) or pos.current_price
            if price <= 0:
                continue
            pnl = pos.pnl_pct or 0
            watch = self._bought_watch.get(code, {})
            meta = self._pos_meta.get(code, {})
            entry_price = watch.get("entry_price") or pos.avg_cost
            buy_scan = watch.get("buy_scan", 0)
            max_profit = watch.get("max_profit_pct", 0)
            buy_date = watch.get("buy_trade_date", "")

            # 基本过滤：T+1 锁定跳过，持仓 < 10 轮不评估
            if pos.available_volume <= 0:
                continue
            scans_held = self._scan_count - buy_scan if buy_scan > 0 else 999
            if scans_held < 5:  # 至少 5 轮（约 5 分钟）
                continue

            # 行业 + 板块趋势
            industry = self._industry_cache.get(code, "")
            trend = self._get_sector_trend(code)
            buy_trend = meta.get("buy_sector_trend", "")

            triggers = []
            exit_reason = ""

            # ━━ 场景1: 僵持退出 ━━
            # 持仓 ≥ 30 轮（约 30 分钟），近 10 轮价格振幅 < 1%，板块不配合
            if scans_held >= 30:
                recent = (
                    list(self._index_prices)[-30:]
                    if hasattr(self._index_prices, "__getitem__")
                    else []
                )
                if len(recent) >= 10:
                    # 用日内的价格波动代替个股波动（个股数据不够细）
                    amp = (
                        (max(recent[-10:]) - min(recent[-10:])) / recent[-15]
                        if len(recent) >= 15
                        else 0
                    )
                    price_stale = amp < 0.015  # 指数 10 轮振幅 < 1.5%，个股大概率也横盘
                else:
                    price_stale = False
                sector_ok = "持续走强" in trend
                if price_stale and not sector_ok and abs(pnl) < 0.03:
                    triggers.append("僵持横盘")
                    exit_reason = f"僵持{scans_held}轮 振幅{amp:.1%} 板块:{trend}"

            # ━━ 场景2: 时间止损 ━━
            # 跨日持仓（昨天买的），今日无表现，微利/微亏
            is_overnight = buy_date and buy_date < self._trade_date
            if is_overnight:
                # 今日已有充分时间表现（开市 > 30 分钟）
                minutes_since_open = (
                    (
                        datetime.combine(datetime.now().date(), dt_time(9, 30))
                        - datetime.now()
                    ).total_seconds()
                    / 60
                    if False
                    else 999
                )
                morning_passed = (
                    time.time() - getattr(self, "_data_ready_at", 0)
                ) > 1800  # 数据就绪 > 30 分钟
                if morning_passed and abs(pnl) < 0.03:
                    triggers.append("跨日无进展")
                    exit_reason = f"昨日买入至今{pnl:+.1%} 板块:{trend}"

            # ━━ 场景3: 板块转弱 ━━
            # 买入时板块强但现在弱
            if buy_trend and "持续走强" in buy_trend:
                if (
                    "走弱" in trend
                    and "强" not in trend
                    or "横盘" in trend
                    and abs(pnl) < 0.02
                ):
                    triggers.append("板块转弱")
                    exit_reason = f"买入时{buy_trend} → 当前{trend}"

            # ━━ 场景4: 开盘压力 ━━
            # 开盘 30 分钟内，价格下跌且板块弱
            if scans_held <= 180 and scans_held >= 20:  # ~20-180 分钟
                chg_from_open = (
                    (price - entry_price) / entry_price if entry_price > 0 else 0
                )
                if chg_from_open < -0.01 and "走弱" in trend and "强" not in trend:
                    triggers.append("开盘压力")
                    exit_reason = f"开盘跌{chg_from_open:+.1%} 板块弱"

            # ━━ 场景5: 利润回吐 ━━
            # 曾经赚 >3%，现在回吐到 <1%，锁住残存利润
            if max_profit > 0.03 and pnl < 0.01:
                given_back = max_profit - pnl
                if given_back > 0.02:
                    triggers.append("利润回吐")
                    exit_reason = (
                        f"最高+{max_profit:.1%} → 当前{pnl:+.1%} 回吐{given_back:.1%}"
                    )

            # ━━ 场景6: 机会成本 ━━
            # 仓位满，有热门板块候选但当前持仓板块不热
            if pos_count >= settings.MAX_POSITIONS:
                hot = (
                    self._detect_hot_sectors()
                    if hasattr(self, "_detect_hot_sectors")
                    else []
                )
                hot_names = {h["name"] for h in hot}
                if industry and industry not in hot_names:
                    sector_chg = self._get_sector_change(code)
                    sector_normal = sector_chg is not None and sector_chg < 1.0
                    if sector_normal and abs(pnl) < 0.03:
                        triggers.append("机会成本")
                        exit_reason = f"仓位满 板块{industry}不热 腾位"

            # ━━ 汇总判断 ━━
            if len(triggers) >= 2:
                tags = " + ".join(triggers)
                msg = (
                    f"🔔 主动退出建议 — {code} {pos.stock_name}\n"
                    f"   现价: {price:.2f}  成本: {entry_price:.2f}  盈亏: {pnl:+.1%}\n"
                    f"   {exit_reason}\n"
                    f"   📋 触发: {tags}"
                )
                self._alert(msg)

                # 模拟盘自动卖出
                result = pa.sell(code, price, f"主动退出({tags})")
                if result.success:
                    self._pos_meta.pop(code, None)
                    self._bought_watch.pop(code, None)
                    recently_sold = getattr(self, "_recently_sold", {})
                    recently_sold[code] = self._scan_count
                    self._invalidate_watch_codes_cache()
                    logger.info(f"主动退出卖出: {code} {tags} ({exit_reason})")

    def _check_retracement_stop(
        self,
        code: str,
        name: str,
        price: float,
        entry_price: float,
        trend: str,
        limit_down: bool,
        risk_level: str = "safe",
    ):
        """分级利润回撤止盈，大盘风险高时更保守（保留更多利润）.

        分级阈值（从 _bought_watch 读取历史最高浮盈）：
        - 最高浮盈 ≥ 15%: 保留 60% 利润（极端→70%，危险→65%）
        - 最高浮盈 ≥ 10%: 保留 55% 利润（极端→65%，危险→60%）
        - 最高浮盈 ≥ 5%:  保留 50% 利润（极端→60%，危险→55%）
        返回 (key, kwargs) 或 (None, None) 表示未触发.
        """
        if entry_price <= 0:
            return None, None

        watch = self._bought_watch.get(code, {})
        max_profit = watch.get("max_profit_pct", 0)
        if max_profit < 0.05:
            return None, None

        current_profit = (price - entry_price) / entry_price

        # 基础保留比例 + 大盘风险加成
        if risk_level == "extreme":
            bonus = 0.10  # 多保留 10% 利润
        elif risk_level == "dangerous":
            bonus = 0.05
        else:
            bonus = 0.0

        if max_profit >= 0.15:
            keep_ratio = min(0.60 + bonus, 0.75)
        elif max_profit >= 0.10:
            keep_ratio = min(0.55 + bonus, 0.70)
        else:
            keep_ratio = min(0.50 + bonus, 0.65)

        threshold = max_profit * keep_ratio
        if current_profit >= threshold:
            return None, None

        tier_label = (
            "T1" if max_profit >= 0.15 else "T2" if max_profit >= 0.10 else "T3"
        )
        risk_note = (
            f" 大盘{risk_level}" if risk_level in ("extreme", "dangerous") else ""
        )
        key = f"{code}:retrace"
        extra = (
            f"{tier_label}{risk_note} 最高浮盈{max_profit * 100:.1f}% → 当前{current_profit * 100:.1f}%"
            f"（保留{keep_ratio * 100:.0f}%利润触发）"
        )
        trigger_price = entry_price * (1 + threshold)
        return key, {
            "key": key,
            "code": code,
            "name": name,
            "stype": "利润回撤止盈",
            "price": price,
            "trigger": trigger_price,
            "ref_price": entry_price,
            "trend": trend,
            "limit_down": limit_down,
            "extra": extra,
        }

    def _evaluate_sell_context(self, code: str, stype: str, trend: str) -> str:
        """评估卖出时的市场/板块上下文，返回 'normal' / 'hold' / 'urgent'。

        - hold: 大盘V反/低开高走/开盘恐慌，止损可能卖在最低点，暂缓
        - urgent: 板块加速下行，立即卖出不要犹豫
        """
        regime = getattr(self, "_regime", None)
        pattern = getattr(regime, "pattern", "normal") if regime else "normal"

        # 开盘缓冲期 + 大盘跌幅 < 1% → 开盘恐慌，暂缓止损
        seconds_since_open = self._minutes_since_open() * 60
        if stype == "止损" and seconds_since_open < self.OPENING_BUFFER_SECONDS:
            idx = self._get_index_quote()
            if idx:
                chg = idx.get("change_pct", 0) or 0
                if abs(chg) < 0.01:  # 大盘跌幅 < 1%，非系统性风险
                    return "hold"

        # V反/低开高走 → 止损可能卖在地板，暂缓（止盈不受影响）
        if stype in ("止损",) and pattern in ("v_reversal", "gap_down_recover"):
            return "hold"

        # 板块加速下行 → 立即卖
        if "加速" in trend and "走弱" in trend:
            return "urgent"

        # 尾盘急跌 → 立即卖
        if pattern == "late_dump":
            return "urgent"

        return "normal"

    def _handle_stop_signal(
        self,
        key: str,
        code: str,
        name: str,
        stype: str,
        price: float,
        trigger: float,
        ref_price: float,
        trend: str,
        limit_down: bool,
        extra: str = "",
    ):
        """止损/止盈触发时的统一处理：推送提醒 + 模拟盘执行（实盘等用户确认）."""
        now = datetime.now()

        # 已在提醒队列中，跳过
        if key in self._sl_reminders:
            return

        # 卖出上下文评估：大盘/板块极端情况调整策略
        sell_advice = self._evaluate_sell_context(code, stype, trend)
        if sell_advice == "hold":
            logger.info(f"卖出 [{code}] 大盘V反/低开高走中，暂缓卖出观察")
            return  # 暂不卖出，继续观察
        elif sell_advice == "urgent":
            extra = (extra + "；" if extra else "") + "板块加速下行，立即卖出"

        chg = (price - ref_price) / ref_price * 100 if ref_price else 0

        if limit_down:
            # 加入提醒队列防重复推送（开板后可触发卖出）
            if key not in self._sl_reminders:
                self._alert(
                    f"🚫 跌停无法卖出 — {code} {name}\n"
                    f"   现价: {price:.2f}  触发: {trigger:.2f}  亏损: {chg:+.1f}%\n"
                    f"   跌停封单中，下轮继续监控"
                )
                self._sl_reminders[key] = {
                    "code": code,
                    "name": name,
                    "type": stype,
                    "price": price,
                    "trigger": trigger,
                    "ref_price": ref_price,
                    "last_push": now,
                    "status": "limited_down",
                }
            return

        emoji = "⚠️" if stype != "止盈" else "✅"
        pnl_label = "亏损" if chg < 0 else "盈利"
        extra_str = f"  {extra}" if extra else ""

        self._alert(
            f"{emoji} {stype}卖出 — {code} {name}\n"
            f"   现价: {price:.2f}  触发: {trigger:.2f}  {pnl_label}: {chg:+.1f}%{extra_str}\n"
            f"   板块:{trend}\n"
            f"   📋 模拟盘已卖出"
        )

        # 私聊：实盘确认请求（实盘未启用时跳过推送）
        if settings.REAL_TRADE_ENABLED:
            self._alert_private(
                f"{emoji} {stype}触发 — 实盘待确认\n"
                f"   {code} {name}  现价: {price:.2f}  触发: {trigger:.2f}  {pnl_label}: {chg:+.1f}%\n"
                f"   ✏️ 已执行回复「成交 {code}」\n"
                f"   ⏳ 暂时不卖回复「再等 5 {code}」"
            )

        # 加入提醒队列（5分钟后未确认则再推）
        self._sl_reminders[key] = {
            "code": code,
            "name": name,
            "type": stype,
            "price": price,
            "trigger": trigger,
            "ref_price": ref_price,
            "last_push": now,
            "status": "pending",
        }

        # 模拟盘直接执行（实盘等用户确认）
        from trade.paper.executor import execute_paper_sell

        meta = self._pos_meta.get(code, {})
        result = execute_paper_sell(
            code, name, price, stype,
            paper_account=self.paper_account,
            pos_meta=self._pos_meta,
            bought_watch=self._bought_watch,
            signal_id=meta.get("signal_id"),
        )
        if result["success"]:
            # 卖出冷却：防止同一轮或短期内重新买入
            recently_sold = getattr(self, "_recently_sold", {})
            recently_sold[code] = self._scan_count
            self._invalidate_watch_codes_cache()

    def _check_sl_reminders(self):
        """止损提醒循环：5分钟未确认则重新推送；跌停开板自动卖出."""
        now = datetime.now()
        for key, rem in list(self._sl_reminders.items()):
            elapsed = (now - rem["last_push"]).total_seconds()

            if rem["status"] == "limited_down":
                # 跌停开板检测：每轮检查是否已开板
                code = rem["code"]
                price = self.paper_account.positions.get(code)
                if price is None:
                    # 可能已手动卖出
                    self._sl_reminders.pop(key, None)
                    continue
                cur_price = (
                    price.current_price
                    if hasattr(price, "current_price")
                    else rem["price"]
                )
                if not self._is_limit_down(code, cur_price):
                    logger.info(f"跌停开板 [{code}]，自动执行卖出")
                    self._alert(
                        f"🔓 跌停开板 — {code} {rem['name']}\n   现价: {cur_price:.2f}  自动执行卖出"
                    )
                    meta = self._pos_meta.get(code, {})
                    result = self.paper_account.sell(
                        code,
                        cur_price,
                        f"跌停开板({rem['type']})",
                        signal_id=meta.get("signal_id"),
                    )
                    if result.success:
                        self._pos_meta.pop(code, None)
                        self._bought_watch.pop(code, None)
                        recently_sold = getattr(self, "_recently_sold", {})
                        recently_sold[code] = self._scan_count
                        self._invalidate_watch_codes_cache()
                    self._sl_reminders.pop(key, None)
                continue

            if rem["status"] == "waiting":
                if now < rem.get("wake_at", now):
                    continue
                rem["status"] = "pending"

            # 15 分钟提醒一次，最多 3 次
            if rem["status"] == "pending" and elapsed > 900:
                push_count = rem.get("push_count", 0)
                if push_count >= 3:
                    continue  # 3次未响应，不再提醒
                rem["last_push"] = now
                rem["push_count"] = push_count + 1
                code = rem["code"]
                name = rem["name"]
                stype = rem["type"]
                price = rem["price"]
                trigger = rem["trigger"]
                # 实盘未启用时跳过提醒推送
                if settings.REAL_TRADE_ENABLED:
                    self._alert_private(
                        f"⏰ 第{push_count + 1}次提醒 — {code} {name}  {stype}\n"
                        f"   触发价: {trigger:.2f}  已过 {elapsed / 60:.0f} 分钟未确认\n"
                        f"   ✏️ 已执行回复「成交 {code}」\n"
                        f"   ⏳ 延迟回复「再等 5 {code}」"
                    )

    def handle_sl_command(self, text: str) -> str:
        """处理用户对止损提醒的回复.

        返回确认消息或空字符串.
        格式：
          成交 CODE — 已手动执行
          再等 N CODE — 等待N分钟后再提醒
        """
        import re

        text = text.strip()

        # 成交确认
        m_done = re.search(r"成交\s*(\d{6})", text)
        if m_done:
            code = m_done.group(1)
            removed = [k for k, v in self._sl_reminders.items() if v["code"] == code]
            for k in removed:
                del self._sl_reminders[k]
            self._bought_watch.pop(code, None)  # 确认卖出后清理盯盘状态
            if removed:
                return f"✅ 已确认 {code} 成交，停止提醒"
            return ""

        # 延迟提醒（必须行首或以"再等"开头，避免误匹配闲聊）
        m_wait = re.search(r"(?:^|[，。；\s])再等\s*(\d+)\s*(\d{6})?", text)
        if not m_wait:
            m_wait = re.search(r"^再等\s*(\d+)\s*(\d{6})?", text)
        if m_wait:
            minutes = int(m_wait.group(1))
            code = m_wait.group(2)
            if code:
                keys = [k for k, v in self._sl_reminders.items() if v["code"] == code]
            else:
                keys = list(self._sl_reminders.keys())

            from datetime import timedelta

            wake = datetime.now().replace(second=0, microsecond=0) + timedelta(
                minutes=minutes
            )
            for k in keys:
                self._sl_reminders[k]["status"] = "waiting"
                self._sl_reminders[k]["wake_at"] = wake
            return f"⏰ 延迟 {minutes} 分钟后再提醒"

        return ""

    def _resolve_sl_reminders(self, text: str):
        """成交消息中提取代码，清理对应的 SL 提醒."""
        import re

        m = re.search(r"\b(\d{6})\b", text)
        if not m:
            return
        code = m.group(1)
        keys = [k for k, v in self._sl_reminders.items() if v["code"] == code]
        for k in keys:
            del self._sl_reminders[k]
        if keys:
            logger.info(f"已清理 {code} 的 SL 提醒（用户确认成交）")

    # ======================== 智能仓位计算 ========================

    def _check_bought_signals(self, prices: dict[str, float]):
        """监控已买入持仓：盯盘状态 + 补仓信号.止损止盈由 _check_positions 统一处理."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT ts.*, buy_info.entry_price, buy_info.buy_time
                   FROM trade_signals ts
                   JOIN (
                       SELECT signal_id,
                              SUM(filled_price * filled_volume) / SUM(filled_volume) as entry_price,
                              MAX(order_time) as buy_time
                       FROM trade_orders
                       WHERE order_type='buy' AND order_status='filled'
                         AND filled_volume > 0 AND account='paper'
                       GROUP BY signal_id
                   ) buy_info ON buy_info.signal_id = ts.id
                   WHERE ts.status='bought' AND ts.account='paper'""",
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.warning(f"获取已买入信号异常: {e}")
            return

        for row in rows:
            s = dict(row)
            code = s["stock_code"]

            # 已卖出（不在持仓中）则跳过，避免 DB 旧 signal 残留重建 _bought_watch
            if code not in self.paper_account.positions:
                continue

            name = s.get("stock_name", "")
            if not name or name == code:
                name = self._resolve_name(code)
            price = prices.get(code)
            if price is None:
                # fallback: 从 portfolio 取当前价格
                pos = self.paper_account.positions.get(code)
                price = pos.current_price if pos else None
            if price is None or price <= 0:
                continue

            sl = s.get("stop_loss") or 0
            tp = s.get("take_profit") or 0
            entry_price = s.get("entry_price") or 0
            buy_time = s.get("buy_time", "")
            is_today_buy = str(buy_time).startswith(self._trade_date)

            trend = self._get_sector_trend(code)

            # === 买入后盯盘（每20轮~20分钟推送一次状态） ===
            watch = self._bought_watch.setdefault(
                code,
                {
                    "entry_price": entry_price,
                    "last_alert_scan": 0,
                    "buy_scan": self._scan_count,
                    "buy_trade_date": self._trade_date,
                    "status": "watching",
                    "alert_count": 0,
                    "max_profit_pct": 0,
                },
            )
            if entry_price and not watch.get("entry_price"):
                watch["entry_price"] = entry_price

            # 更新最高浮盈
            if entry_price > 0:
                cur_pct = (price - entry_price) / entry_price
                if cur_pct > watch.get("max_profit_pct", 0):
                    watch["max_profit_pct"] = cur_pct

            scans_since = self._scan_count - watch["last_alert_scan"]
            pnl_pct = (
                (price - entry_price) / entry_price * 100 if entry_price > 0 else 0
            )

            new_status = self._classify_holding_status(
                code, price, entry_price, sl, tp, is_today_buy
            )
            status_changed = new_status != watch["status"]
            if status_changed:
                logger.info(
                    f"持仓监控 [{code} {name}] 状态变更 {watch['status']}→{new_status} "
                    f"价格{price:.2f} 成本{entry_price:.2f} 盈亏{pnl_pct:+.1f}%"
                )
            # T+1 锁定仓位不推送止损止盈（当天卖不了），补仓机会除外
            if is_today_buy:
                should_alert = False
            else:
                alert_interval = 20
                should_alert = scans_since >= alert_interval or status_changed

            # === 被套/深套：反弹减仓目标盯盘（T+1 跳过，当天卖不了）===
            if (
                new_status in ("trapped", "deep_trapped")
                and entry_price > 0
                and not is_today_buy
            ):
                target = watch.get("exit_target")
                # 状态刚变成被套 或 目标失效 → 重新计算
                if target is None or status_changed:
                    target_price, target_label = self._calc_exit_target(
                        code, price, entry_price, trend
                    )
                    if target_price:
                        watch["exit_target"] = target_price
                        watch["exit_target_label"] = target_label
                        watch["exit_target_alert_at"] = 0
                else:
                    # 每轮检查是否接近目标
                    dist_pct = (watch["exit_target"] - price) / price * 100
                    last_target_alert = watch.get("exit_target_alert_at", 0)

                    if price >= watch["exit_target"]:
                        # 已达到目标，告警并算下一个
                        self._alert(
                            f"🎯 减仓目标达成 — {code} {name}\n"
                            f"   现价: {price:.2f}  目标: {watch['exit_target']:.2f}  ({watch.get('exit_target_label', '')})\n"
                            f"   盈亏: {pnl_pct:+.1f}%  → 到达阻力位，建议减仓"
                        )
                        next_price, next_label = self._calc_exit_target(
                            code, price, entry_price, trend
                        )
                        if next_price:
                            watch["exit_target"] = next_price
                            watch["exit_target_label"] = next_label
                            watch["exit_target_alert_at"] = 0
                        else:
                            watch.pop("exit_target", None)
                            watch.pop("exit_target_label", None)
                    elif dist_pct <= 2.0 and self._scan_count - last_target_alert >= 10:
                        # 距目标 2% 以内，且距上次提醒 >= 10 轮
                        watch["exit_target_alert_at"] = self._scan_count
                        self._alert(
                            f"🔔 接近减仓目标 — {code} {name}\n"
                            f"   现价: {price:.2f}  目标: {watch['exit_target']:.2f}  ({watch.get('exit_target_label', '')})\n"
                            f"   距目标: {dist_pct:.1f}%  盈亏: {pnl_pct:+.1f}%  → 准备减仓"
                        )
            elif new_status not in ("trapped", "deep_trapped"):
                # 状态恢复正常，清目标
                watch.pop("exit_target", None)
                watch.pop("exit_target_label", None)
                watch.pop("exit_target_alert_at", None)

            # —— 动态目标修正：止盈天花板+止损地板，三层联动 ——
            dyn_fired = self._check_dynamic_targets(
                code, name, price, entry_price, sl, tp, is_today_buy, trend, watch
            )

            # —— 预测性接近告警：修正已发则跳过，避免重复 ——
            if not dyn_fired:
                self._check_predictive_proximity(
                    code, name, price, entry_price, sl, tp, is_today_buy, trend, watch
                )

            # 今日买入仅补仓机会推送，其余静默（T+1 卖不了）
            effective_alert = should_alert or (
                is_today_buy and new_status == "add_opportunity" and status_changed
            )
            if effective_alert and entry_price > 0:
                watch["last_alert_scan"] = self._scan_count
                watch["alert_count"] += 1
                if status_changed:
                    watch["status"] = new_status

                emoji = {
                    "healthy": "✅",
                    "watching": "👀",
                    "at_risk": "🟠",
                    "trapped": "🔴",
                    "deep_trapped": "💀",
                    "add_opportunity": "🟡",
                }
                status_labels = {
                    "healthy": "持仓健康",
                    "watching": "持续观察",
                    "at_risk": "接近止损",
                    "trapped": "被套",
                    "deep_trapped": "深度套牢",
                    "add_opportunity": "补仓机会",
                }

                # 收集到批次列表，循环结束后合并推送
                dist_to_sl = (price - sl) / price * 100 if sl > 0 and price > 0 else 0
                batch = getattr(self, "_holding_batch", None)
                if batch is None:
                    self._holding_batch = []
                    batch = self._holding_batch
                batch.append(
                    f"{emoji.get(new_status, '👀')} {code} {name} {price:.2f} {pnl_pct:+.1f}% "
                    f"距止损{dist_to_sl:.1f}%"
                )

                # 状态变更 → 即时推送详细信息
                if status_changed:
                    day_label = (
                        "今日买入" if is_today_buy else f"成本: {entry_price:.2f}"
                    )
                    detail = (
                        f"{emoji.get(new_status, '👀')} 持仓状态变更 — {code} {name}\n"
                        f"   {status_labels.get(new_status, new_status)}  现价: {price:.2f}  {day_label}"
                        f"  盈亏: {pnl_pct:+.1f}%\n"
                        f"   止损: {sl:.2f}  止盈: {tp:.2f}  板块:{trend}"
                    )
                    if new_status == "at_risk":
                        detail += f"\n   ⚠️ 接近止损线，距触发仅 {dist_to_sl:.1f}%，做好离场准备"
                    elif new_status in ("trapped", "deep_trapped"):
                        exit_ctx = self._analyze_exit_context(
                            code, price, entry_price, trend
                        )
                        try:
                            self._log_exit_analysis(
                                stock_code=code,
                                holding_status=new_status,
                                market_env=pattern,
                                sector_trend=trend,
                            )
                        except Exception:
                            pass
                        label = (
                            "深度套牢超10%"
                            if new_status == "deep_trapped"
                            else "被套5%~10%"
                        )
                        detail += f"\n   {emoji.get(new_status)} {label}\n   {exit_ctx}"
                    elif new_status == "add_opportunity":
                        add_context = self._analyze_add_context(
                            code, price, entry_price
                        )
                        if add_context:
                            detail += f"\n   💡 补仓机会: {add_context}"
                    self._alert(detail)

        # 合并持仓摘要，每 20 轮推送一条（避免每票单独刷屏）
        batch = getattr(self, "_holding_batch", None)
        if batch:
            self._holding_batch = []
            msg = "📊 持仓汇总\n" + "\n".join(f"  {b}" for b in batch)
            self._alert(msg)

    def _check_predictive_proximity(
        self,
        code: str,
        name: str,
        price: float,
        entry_price: float,
        sl: float,
        tp: float,
        is_today_buy: bool,
        trend: str,
        watch: dict,
    ):
        """预测性接近告警：结合情景引擎市场方向预判，在触发前给出预警.

        与止损/止盈触发（事后）互补：在价格接近关键位时结合情景预测提前行动.
        """
        if is_today_buy or entry_price <= 0:
            return

        # 情景引擎预判
        outlook = getattr(self, "_scenario_prev_outlook", None)
        market_bearish = outlook.primary.direction == "bearish" if outlook else False
        market_urgency = outlook.urgency if outlook else "none"
        scenario_label = outlook.primary.label if outlook else ""
        scenario_prob = outlook.primary.probability if outlook else 0

        pnl_pct = (price - entry_price) / entry_price * 100

        # —— 止损接近预警 ——
        if sl > 0 and price > sl:
            dist_to_sl = (price - sl) / price * 100
            sl_last = watch.get("sl_prox_alert_at", 0)

            # 市场偏空+高urgency + 距止损<3% → 提前预警
            if (
                dist_to_sl < 3.0
                and market_bearish
                and market_urgency in ("critical", "act")
            ):
                if self._scan_count - sl_last >= 30:
                    watch["sl_prox_alert_at"] = self._scan_count
                    self._alert(
                        f"⚠️ 止损预警 — {code} {name}\n"
                        f"   现价: {price:.2f}  止损: {sl:.2f}  距触发: {dist_to_sl:.1f}%  盈亏: {pnl_pct:+.1f}%\n"
                        f"   🔮 {scenario_label} ({scenario_prob:.0%})  → 市场偏空，准备离场"
                    )
            elif dist_to_sl < 1.5:
                # 非常接近，即使市场中性也预警
                if self._scan_count - sl_last >= 30:
                    watch["sl_prox_alert_at"] = self._scan_count
                    extra = (
                        f"  🔮 {scenario_label} ({scenario_prob:.0%})"
                        if market_bearish
                        else ""
                    )
                    self._alert(
                        f"⚠️ 接近止损 — {code} {name}\n"
                        f"   现价: {price:.2f}  止损: {sl:.2f}  距触发: {dist_to_sl:.1f}%  盈亏: {pnl_pct:+.1f}%{extra}\n"
                        f"   → 价格逼近止损位，密切关注"
                    )

        # —— 止盈接近预警 ——
        if tp > 0 and price < tp:
            dist_to_tp = (tp - price) / price * 100
            tp_last = watch.get("tp_prox_alert_at", 0)

            # 市场可能反转（偏空）+ 接近止盈 → 建议提前锁定
            if (
                dist_to_tp < 3.0
                and market_bearish
                and market_urgency in ("critical", "act")
            ):
                if self._scan_count - tp_last >= 15:
                    watch["tp_prox_alert_at"] = self._scan_count
                    self._alert(
                        f"🔔 止盈预警 — {code} {name}\n"
                        f"   现价: {price:.2f}  止盈: {tp:.2f}  距目标: {dist_to_tp:.1f}%  盈利: {pnl_pct:+.1f}%\n"
                        f"   🔮 {scenario_label} ({scenario_prob:.0%})  → 市场可能反转，考虑提前锁定"
                    )
            elif dist_to_tp < 1.5 and market_urgency in ("critical", "act"):
                if self._scan_count - tp_last >= 15:
                    watch["tp_prox_alert_at"] = self._scan_count
                    self._alert(
                        f"🔔 接近止盈 — {code} {name}\n"
                        f"   现价: {price:.2f}  止盈: {tp:.2f}  距目标: {dist_to_tp:.1f}%  盈利: {pnl_pct:+.1f}%\n"
                        f"   🔮 {scenario_label} ({scenario_prob:.0%})  → 接近止盈目标，关注盘面"
                    )

    def _check_dynamic_targets(
        self,
        code: str,
        name: str,
        price: float,
        entry_price: float,
        sl: float,
        tp: float,
        is_today_buy: bool,
        trend: str,
        watch: dict,
    ) -> bool:
        """动态目标修正：三层联动（大盘→板块→个股）评估止盈/止损是否需要修正.

        核心思路：
        - 算阻力天花板 → 如果原止盈在天花板之上很多 → 建议下调
        - 算支撑地板 → 如果市场偏空+板块弱 → 建议收紧止损
        - 不是每次扫描都告警，只在修正幅度 > 2% 且距上次 > 20 轮时推送

        SL 收紧使用 cost 基准（与 _check_positions 口径一致）：
        effective_sl = cost - (cost - sl) * width_mult

        返回 True 表示本次发送了告警（调用方可据此去重）.
        """
        if is_today_buy or entry_price <= 0:
            return False

        # 获取三层联动因子
        adj = self._get_market_adjustment(code, trend)
        if adj["tp_ceil_factor"] >= 1.0 and adj["sl_tighten"] <= 1.0:
            return False  # 无需调整

        pnl_pct = (price - entry_price) / entry_price * 100

        # ━━ 止盈天花板：算上方阻力 ━━
        new_tp = None
        tp_reason = ""
        if tp > 0 and price < tp and adj["tp_ceil_factor"] < 1.0:
            ceiling = self._find_resistance_ceiling(code, price)
            if ceiling is None:
                ceiling = tp

            # 天花板打折（市场+板块联动）
            adjusted_ceiling = price + (ceiling - price) * adj["tp_ceil_factor"]
            adjusted_ceiling = max(adjusted_ceiling, price * 1.01)

            if adjusted_ceiling < tp * 0.97:
                new_tp = round(adjusted_ceiling, 2)
                below_pct = (tp - new_tp) / tp * 100
                adj_part = f" → {adj['reason']}" if adj["reason"] else ""
                tp_reason = (
                    f"原止盈 {tp:.2f}，最近阻力 {ceiling:.2f}"
                    f"{adj_part}"
                    f" → 建议下调至 {new_tp:.2f} (-{below_pct:.0f}%)"
                )

        # ━━ 止损地板：cost 基准（与 _check_positions 口径一致）━━
        new_sl = None
        sl_reason = ""
        if sl > 0 and entry_price > sl and adj["sl_tighten"] > 1.0:
            floor = self._find_support_floor(code, price)
            if floor is None:
                floor = sl

            # 使用 cost 基准计算收紧（与 _check_positions 一致）
            loss_width = entry_price - sl
            width_mult = 2.0 - adj["sl_tighten"]  # sl_tighten=1.2 → 0.8 (收 20%)
            tightened_width = loss_width * max(0.5, width_mult)
            adjusted_sl = entry_price - tightened_width

            # 不低于支撑位下方 1%
            if floor > 0:
                adjusted_sl = max(adjusted_sl, floor * 0.99)

            if adjusted_sl > sl * 1.02:
                new_sl = round(adjusted_sl, 2)
                adj_part = f" → {adj['reason']}" if adj["reason"] else ""
                sl_reason = (
                    f"原止损 {sl:.2f}，最近支撑 {floor:.2f}"
                    f"{adj_part}"
                    f" → 建议收紧至 {new_sl:.2f}"
                )

        if not new_tp and not new_sl:
            return False

        # 去重：距上次告警 >= 20 轮，或目标变化 > 1%
        last_adj_scan = watch.get("dyn_target_alert_at", 0)
        prev_new_tp = watch.get("dyn_tp")
        prev_new_sl = watch.get("dyn_sl")

        tp_changed = new_tp and (
            prev_new_tp is None or abs(new_tp - prev_new_tp) / prev_new_tp > 0.01
        )
        sl_changed = new_sl and (
            prev_new_sl is None or abs(new_sl - prev_new_sl) / prev_new_sl > 0.01
        )

        if self._scan_count - last_adj_scan < 20 and not (tp_changed or sl_changed):
            return False

        watch["dyn_target_alert_at"] = self._scan_count

        # 构建消息（reason 本身已含 🔮，不再重复添加）
        lines = [f"🎯 动态目标修正 — {code} {name}"]
        lines.append(f"   现价: {price:.2f}  盈亏: {pnl_pct:+.1f}%")
        if new_tp:
            watch["dyn_tp"] = new_tp
            lines.append(f"   📈 {tp_reason}")
        if new_sl:
            watch["dyn_sl"] = new_sl
            lines.append(f"   📉 {sl_reason}")

        self._alert("\n".join(lines))
        return True

    def _find_resistance_ceiling(self, code: str, price: float) -> float | None:
        """委托至 StockReader.get_support_resistance。"""
        import sqlite3
        from data.readers.stock_reader import StockReader

        try:
            conn = sqlite3.connect(self.db_path)
            sr = StockReader.get_support_resistance(conn, code, price)
            conn.close()
            resistances = sr.get("resistances", [])
            return resistances[0][0] if resistances else None
        except Exception:
            return None

    def _find_support_floor(self, code: str, price: float) -> float | None:
        """委托至 StockReader.get_support_resistance。"""
        import sqlite3
        from data.readers.stock_reader import StockReader

        try:
            conn = sqlite3.connect(self.db_path)
            sr = StockReader.get_support_resistance(conn, code, price)
            conn.close()
            supports = sr.get("supports", [])
            return supports[0][0] if supports else None
        except Exception:
            return None

    def _classify_holding_status(
        self,
        code: str,
        price: float,
        entry_price: float,
        sl: float,
        tp: float,
        is_today_buy: bool,
    ) -> str:
        """分类持仓状态：healthy / watching / at_risk / trapped / deep_trapped.

        - healthy: 盈利 > 2%
        - watching: 小亏/微利，正常波动
        - at_risk: 亏损 ≥ 2% 且接近止损线
        - trapped: 亏损 5%~10%
        - deep_trapped: 亏损 ≥ 10%
        - add_opportunity: 亏损但出现反弹迹象
        """
        if entry_price <= 0:
            return "watching"

        pnl_pct = (price - entry_price) / entry_price * 100

        # 深度套牢：亏损 >= 10%
        if pnl_pct <= -10:
            return "deep_trapped"
        # 被套：亏损 5%~10%
        if pnl_pct <= -5:
            return "trapped"

        if pnl_pct <= -2 and sl > 0 and entry_price > sl:
            loss_used = (entry_price - price) / (entry_price - sl)
            # 消耗止损空间 >= 85%，非常接近止损 → 接近止损警示
            if loss_used >= 0.85:
                return "at_risk"
            # 补仓机会：亏损但出现反弹迹象，且止损空间还够
            if loss_used < 0.5:
                return self._check_add_opportunity(code)

        # 健康
        if pnl_pct > 2:
            return "healthy"

        return "watching"

    def _deep_rebound_improving(self, code: str, deep_state: dict) -> bool:
        """深跌反弹后盘中走势是否有改善 — 纯盘中数据，不依赖日线指标。

        检查维度：
        1. 价格是否还在创新高（rebound_high 在最近 10 轮内更新过）
        2. 板块趋势是否在好转
        3. 大盘是否企稳（未持续新低）

        任一改善即返回 True（继续等），全部恶化返回 False（触发止损）。
        """
        # 维度1: 反弹高点是否在最近 10 轮内更新过 — 价格还在走强
        rebound_scan = deep_state.get("rebound_scan", 0)
        if self._scan_count - rebound_scan < 10:
            return True

        # 维度2: 板块趋势好转 — 从弱转强或横盘
        trend = self._get_sector_trend(code)
        improving_keywords = ("持续走强", "强于大盘", "普涨", "反弹")
        if any(kw in trend for kw in improving_keywords):
            return True
        # 板块从「加速走弱」变成「走弱」也算改善
        if "走弱" in trend and "加速" not in trend:
            deep_sector = deep_state.get("sector_at_entry", "")
            if "加速" in deep_sector and "走弱" in deep_sector:
                return True

        # 维度3: 大盘未持续新低 — 最近 5 轮最低价没破位
        if hasattr(self, "_index_prices") and len(self._index_prices) >= 5:
            recent = self._index_prices[-5:]
            if (
                min(recent) >= min(self._index_prices[-15:-5])
                if len(self._index_prices) >= 15
                else min(recent)
            ):
                return True  # 5轮低点 ≥ 前10轮低点 → 大盘企稳

        return False

    def _check_add_opportunity(self, code: str) -> str:
        """委托至 StockReader。"""
        import sqlite3
        from data.readers.stock_reader import StockReader
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_pct_b, rsi12 FROM stock_indicators
                   WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            if row:
                pct_b, rsi12 = row[0], row[1]
                if pct_b is not None and 5 <= pct_b <= 30 and rsi12 is not None and rsi12 < 40:
                    return "add_opportunity"
        except Exception:
            pass
        return "watching"

    def _analyze_add_context(self, code: str, price: float, entry_price: float) -> str:
        """委托至 StockReader。"""
        import sqlite3
        pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        parts = [f"当前亏损{pnl_pct:+.1f}%，成本{entry_price:.2f}"]
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_lower, bb_mid, ma20, rsi12 FROM stock_indicators
                   WHERE stock_code=? AND bb_lower > 0 ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            if row:
                bb_lower, bb_mid, ma20, rsi12 = row
                if bb_lower and price <= bb_lower * 1.05:
                    parts.append("📍 价格已触及布林下轨，技术性超卖")
                if ma20 and price < ma20:
                    parts.append(f"📉 低于MA20={ma20:.2f}约{(ma20-price)/ma20*100:.1f}%，均线压制中")
                if rsi12 and rsi12 < 35:
                    parts.append(f"📊 RSI(12)={rsi12:.1f}，接近超卖区域")
        except Exception:
            pass
        parts.append("→ 补仓需确认盘面企稳，建议等反弹确认后再操作")
        return "\n".join(parts)

    def _analyze_exit_context(
        self, code: str, price: float, entry_price: float, trend: str = ""
    ) -> str:
        """委托至 trade.decision.sell.analyze_exit_signals。"""
        import sqlite3
        from trade.decision.sell import analyze_exit_signals

        pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0
        regime = getattr(self, "_regime", None)
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"
        pattern = getattr(regime, "pattern", "normal") if regime else "normal"

        bb_lower = bb_mid = ma60 = bbi_daily = None
        rsi12 = rsi6 = macd_bar = macd_dif = kdj_j = None
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_lower, bb_mid, rsi12, rsi6, macd_bar, macd_dif, kdj_j, ma60, bbi_daily
                   FROM stock_indicators WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            if row:
                bb_lower, bb_mid, rsi12, rsi6, macd_bar, macd_dif, kdj_j, ma60, bbi_daily = row
        except Exception:
            pass

        exit_signals, wait_signals, env_parts = analyze_exit_signals(
            price=price, entry_price=entry_price, trend=trend,
            risk_level=risk_level, pattern=pattern,
            bb_mid=bb_mid, ma60=ma60, macd_bar=macd_bar, macd_dif=macd_dif,
            bbi_daily=bbi_daily, rsi12=rsi12, rsi6=rsi6,
            bb_lower=bb_lower, kdj_j=kdj_j,
        )

        parts = []
        if env_parts:
            parts.extend(env_parts)
        if exit_signals:
            parts.append("📍 减仓时机: " + "；".join(exit_signals))
        if wait_signals:
            parts.append("⏳ 等待反弹: " + "；".join(wait_signals))
        if parts:
            return "\n   ".join(parts)
        return f"亏损{pnl_pct:+.1f}%，继续观察盘面"

    def _submit_trapped_exit_ai(
        self,
        code: str,
        name: str,
        price: float,
        cost: float,
        sl: float,
        tp: float,
        trend: str,
        deep_state: dict,
    ):
        """异步 AI 被套离场分析 — 结合个股+板块+大盘给出离场建议。"""
        loss_pct = (cost - price) / cost * 100 if cost > 0 else 0
        lowest = deep_state.get("lowest", price)
        rebound_high = deep_state.get("rebound_high", 0) or price
        rebound_pct = (price - lowest) / lowest * 100 if lowest > 0 else 0

        # 找最近阻力位
        target_price, target_label = self._calc_exit_target(code, price, cost, trend)
        if target_price is None:
            target_price = cost
            target_label = "成本价"

        # 大盘/板块环境
        regime = getattr(self, "_regime", None)
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"
        pattern = getattr(regime, "pattern", "normal") if regime else "normal"
        market_env = pattern if pattern != "normal" else risk_level

        try:
            self._submit_scenario_ai(
                key=f"trapped:{code}",
                scenario="trapped_exit",
                code=code,
                name=name,
                price=price,
                cost=cost,
                loss_pct=loss_pct,
                sl=sl,
                tp=tp,
                lowest=lowest,
                rebound_high=rebound_high,
                rebound_pct=rebound_pct,
                resistance_label=target_label,
                resistance_price=target_price,
                sector_trend=trend,
                market_env=market_env,
                risk_level=risk_level,
            )
        except Exception as e:
            logger.warning(f"提交被套AI分析失败 [{code}]: {e}")

    def _process_trapped_ai_results(self):
        """处理被套离场 AI 异步结果（每 10 轮调用一次）。"""
        for code in list(self.paper_account.positions.keys()):
            akey = f"trapped:{code}"
            result = getattr(self, "_ai_queue", None)
            if result is None:
                continue
            text = result.pop_result(akey)
            if text:
                pos = self.paper_account.positions.get(code)
                if pos:
                    pnl = pos.pnl_pct or 0
                    self._alert(
                        f"🤖 被套AI分析 — {code} {pos.stock_name}\n"
                        f"   现价: {pos.current_price:.2f}  盈亏: {pnl:+.1f}%\n"
                        f"   {text}"
                    )

    def _calc_exit_target(
        self, code: str, price: float, entry_price: float, trend: str = ""
    ) -> tuple:
        """计算被套持仓的反弹减仓目标价.

        找当前价上方最近的阻力位：布林中轨 > MA60 > BBI > 成本价.
        板块持续走弱时，目标趋向保守（优先取更近的阻力位）.
        返回 (target_price, label) 或 (None, None).
        """
        candidates = []
        is_sector_accelerating_down = "持续走弱" in trend and "加速" in trend

        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_mid, ma60, bbi_daily
                   FROM stock_indicators WHERE stock_code=?
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()

            if row:
                bb_mid, ma60, bbi_daily = row
                if bb_mid is not None and bb_mid > price:
                    candidates.append((bb_mid, f"布林中轨{bb_mid:.2f}"))
                if ma60 is not None and ma60 > price:
                    candidates.append((ma60, f"MA60={ma60:.2f}"))
                if bbi_daily is not None and bbi_daily > price:
                    candidates.append((bbi_daily, f"BBI{bbi_daily:.2f}"))
        except Exception:
            pass

        # 成本价作为保底目标
        if entry_price > price:
            candidates.append((entry_price, f"成本价{entry_price:.2f}"))

        if not candidates:
            return None, None

        # 取最近的（最低的）阻力位
        candidates.sort(key=lambda x: x[0])
        target_price, target_label = candidates[0]

        # 板块加速走弱 → 目标向下修正：阻力位下浮 2%
        if is_sector_accelerating_down and len(candidates) >= 1:
            adjusted = target_price * 0.98
            if adjusted > price:
                target_price = adjusted
                target_label += "(下调)"

        return target_price, target_label

    # ======================== 第一层：复盘推荐跟踪 ========================
