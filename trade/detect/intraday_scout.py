"""盘中机会发现引擎（引擎2）— 底部发力检测 + 四层筛选管线。

策略核心：找「刚要涨」的票，不是「已经涨了」的票。
四个条件：低涨幅 + 量能放大 + 涨幅加速 + 板块联动。

Mixin 方式混入 Watcher。与引擎1（复盘趋势跟踪）互补。
"""

import time
from datetime import datetime
from datetime import time as dt_time

from data._base import connect
from system.config import settings
from system.utils.logger import get_trade_logger

logger = get_trade_logger("detect")

# 交易时段
MORNING_START = dt_time(9, 30)
SCOUT_END = dt_time(14, 30)  # 引擎2 覆盖全天（上午+下午）


class IntradayScoutMixin:
    """盘中机会发现 — 底部发力策略。"""

    # ── 运行参数 ──
    SCOUT_INTERVAL = 3  # 每 N 轮触发一次
    SCOUT_AI_TIMEOUT = 45  # AI 超时（秒）

    # ── 风控限制 ──
    MAX_SCOUT_POSITIONS = 4  # 引擎2 最大持仓数
    MIN_POSITION_AMOUNT = 5000  # 单只最小买入金额
    MAX_SAME_SECTOR = 2  # 同板块最多持仓数

    # ── 趋势确认追入筛选阈值 ──
    CHANGE_MIN = 1.0  # 最低涨幅 %（已启动）
    CHANGE_MAX = 5.0  # 最高涨幅 %（还在合理位置）
    MIN_PRICE = 5.0  # 最低价格（排除垃圾股）
    VOL_EXPAND_RATIO = 1.2  # 量能放大倍数（加分项，非硬门槛）
    CHG_ACCEL_MIN = 0.2  # 涨幅加速最小幅度 %（加分项）
    SECTOR_TOP_N = 20  # 板块排名门槛（放宽）
    SECTOR_MIN_PCT = 0.0  # 板块最小涨幅 %

    # ── 内部状态 ──
    _prev_snapshot_amounts: dict[str, float] = {}  # 上轮 amount
    _prev_snapshot_changes: dict[str, float] = {}  # 上轮 change_pct
    _scout_ai_pending: dict[str, dict] = {}  # AI 待处理
    _scout_recent_sectors: dict[str, int] = {}  # 板块→最近推送 scan_count
    _scout_positions: set[str] = set()  # 引擎2当前持仓 code
    _scout_daily_bars: dict[str, list[dict]] = {}  # 前2-3天日线数据缓存
    _scout_pretrend_loaded: bool = False  # 预趋势数据是否已加载

    # ═══════════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════════

    def _scout_intraday(self):
        """引擎2 主入口 — 每 SCOUT_INTERVAL 轮由 _scan() 调用。"""
        if not getattr(self, "_data_ready", False):
            return
        snapshot = getattr(self, "_market_snapshot", None)
        if not snapshot:
            return

        # ── 加载前2-3天日线数据（整个会话只加载一次）──
        self._scout_ensure_pretrend_loaded()

        # ── 时段门控：9:30-14:30 全交易时段 ──
        now = datetime.now().time()
        if now < dt_time(9, 30) or now > SCOUT_END:
            return

        # ── 大盘熔断/极端行情暂停 ──
        regime = getattr(self, "_regime", None)
        risk = getattr(regime, "risk_level", "safe") if regime else "safe"
        pattern = getattr(regime, "pattern", "normal") if regime else "normal"
        if risk in ("extreme", "dangerous"):
            return
        if pattern in ("panic", "one_sided", "late_dump", "fishing_line"):
            return

        # ── 大盘回撤暂停（日亏损 > 2% → 暂停引擎2）──
        pa = self.paper_account
        if pa.daily_pnl < 0 and pa.total_value > 0 and abs(pa.daily_pnl) / pa.total_value > 0.02:
            return

        # ── 引擎2 仓位已满 ──
        scout_count = self._scout_position_count()
        if scout_count >= self.MAX_SCOUT_POSITIONS:
            return

        # ── 第一层：底部发力硬筛 ──
        candidates = self._scout_layer1_filter(snapshot)
        if not candidates:
            return

        # ── 第二层：多维打分排序 ──
        ranked = self._scout_layer2_rank(candidates)

        # ── 第三层：高分直买 / 中分 AI 辅助 / 低分仅提醒 ──
        remaining = self.MAX_SCOUT_POSITIONS - scout_count
        buys_attempted = 0
        ai_submitted = 0

        for item in ranked:
            if buys_attempted >= remaining:
                break
            score = item.get("score", 0)

            if score >= 70:
                # 高分：直接买入，AI 后置评估
                if self._scout_execute_buy(item):
                    buys_attempted += 1
                # AI 异步提交（不阻塞）
                akey = f"scout:{item['code']}"
                if akey not in self._scout_ai_pending and not self._ai_queue.has_pending(akey):
                    self._scout_submit_ai(item)
            elif score >= 50:
                # 中分：AI 辅助决策
                akey = f"scout:{item['code']}"
                if akey in self._scout_ai_pending or self._ai_queue.has_pending(akey):
                    continue
                if ai_submitted >= remaining * 2:
                    continue
                if self._scout_submit_ai(item):
                    ai_submitted += 1
            else:
                # 低分：只推消息
                name = self._resolve_name(item["code"])
                self._scout_push_alert(item, f"评分{score:.0f}偏低，关注后续走势", "观望")

        # ── 第四层：处理 AI 结果 → AI 说买就买 ──
        self._scout_process_ai()

        # 保存本轮数据供下轮对比
        self._prev_snapshot_amounts = {code: float(item.get("amount", 0) or 0) for code, item in snapshot.items()}
        self._prev_snapshot_changes = {code: float(item.get("changePct", 0)) for code, item in snapshot.items()}

    # ═══════════════════════════════════════════════════════════════
    # 第一层：底部发力硬条件过滤
    # ═══════════════════════════════════════════════════════════════

    def _scout_layer1_filter(self, snapshot: dict) -> list[dict]:
        """趋势确认追入检测：已启动 + 板块走强 → 放量/加速为加分项。

        核心条件（必须满足）:
        1. 涨幅 1.0-5.0%（已经启动，但还在合理位置）
        2. 所属板块当日涨 > 0（有板块支撑）
        加分项（影响评分，不硬过滤）:
        - 量能环比放大
        - 涨幅在加速
        - 板块排名靠前
        - 前2-3天趋势形态好
        """
        sector_stats = getattr(self, "_sector_stats", {}) or {}

        # ── 板块排名（用于后续评分加分）──
        ranked_sectors = sorted(
            sector_stats.items(),
            key=lambda x: x[1].get("change_pct", 0),
            reverse=True,
        )
        # 板块排名映射：TOP5=25分, TOP10=18分, TOP20=10分
        sector_rank_map: dict[str, int] = {}
        for i, (name, stats) in enumerate(ranked_sectors):
            rank = i + 1
            if rank <= 5:
                sector_rank_map[name] = 25
            elif rank <= 10:
                sector_rank_map[name] = 18
            elif rank <= 20:
                sector_rank_map[name] = 10
            else:
                sector_rank_map[name] = 3

        # ── 板块涨幅分 ──
        def _sector_bonus(name: str) -> float:
            s = sector_stats.get(name, {})
            chg = s.get("change_pct", 0)
            if chg > 2.0:
                return 12
            elif chg > 1.0:
                return 8
            elif chg > 0:
                return 5
            return 0

        # ── 逐股筛选 ──
        candidates = []
        prev_amounts = getattr(self, "_prev_snapshot_amounts", {}) or {}
        prev_changes = getattr(self, "_prev_snapshot_changes", {}) or {}
        industry_cache = getattr(self, "_industry_cache", {}) or {}

        for code, item in snapshot.items():
            try:
                change_pct = float(item.get("changePct", 0))
                price = float(item.get("price", 0))
                amount = float(item.get("amount", 0) or 0)
            except (ValueError, TypeError):
                continue

            # 核心条件1：涨幅范围
            if change_pct < self.CHANGE_MIN or change_pct > self.CHANGE_MAX:
                continue

            # 价格门槛
            if price < self.MIN_PRICE:
                continue

            # 核心条件2：板块当天涨 > 0（不要求 TOP10）
            industry = industry_cache.get(code, "")
            sector_info = sector_stats.get(industry, {})
            sector_pct = sector_info.get("change_pct", -999)
            if sector_pct is None or sector_pct < 0:
                continue

            # ── 加分项：量能放大 ──
            prev_amount = prev_amounts.get(code, 0)
            vol_expanding = prev_amount > 0 and amount > prev_amount * self.VOL_EXPAND_RATIO
            vol_ratio = amount / prev_amount if prev_amount > 0 else 1.0

            # ── 加分项：涨幅加速 ──
            prev_chg = prev_changes.get(code, 0)
            chg_accel = change_pct - prev_chg if prev_chg != 0 else 0
            chg_accel_bonus = chg_accel >= self.CHG_ACCEL_MIN

            # 排除已有推送
            alert_fps = getattr(self, "_alert_fingerprints", {}) or {}
            if any(code in k for k in alert_fps):
                continue

            # 排除已有持仓
            positions = getattr(self.paper_account, "positions", {}) or {}
            if code in positions:
                continue

            # 排除近期卖出
            recently_sold = getattr(self, "_recently_sold", {}) or {}
            if code in recently_sold:
                scan_diff = self._scan_count - recently_sold[code]
                if scan_diff < 60:
                    continue

            # 前趋势检测（只排除极端恶化）
            pretrend = self._scout_check_pretrend(code)
            if pretrend["form"] == "distributing":
                continue
            # 高位+加速 → 可能是赶顶，排除
            if pretrend["position"] == "high" and chg_accel > 1.5:
                continue

            candidates.append(
                {
                    "code": code,
                    "price": price,
                    "change_pct": change_pct,
                    "chg_accel": chg_accel,
                    "amount": amount,
                    "prev_amount": prev_amount,
                    "vol_ratio": vol_ratio,
                    "industry": industry,
                    "pretrend_form": pretrend["form"],
                    "pretrend_position": pretrend["position"],
                    "pretrend_cum_chg": pretrend["cum_chg"],
                    # 加分标记
                    "vol_expanding": vol_expanding,
                    "chg_accel_bonus": chg_accel_bonus,
                    "sector_rank_bonus": sector_rank_map.get(industry, 3),
                    "sector_pct_bonus": _sector_bonus(industry),
                }
            )

        return candidates

    # ═══════════════════════════════════════════════════════════════
    # 第二层：多维打分排序
    # ═══════════════════════════════════════════════════════════════

    def _scout_layer2_rank(self, candidates: list[dict]) -> list[dict]:
        """多维打分：板块排名 + 板块涨幅 + 量能 + 加速 + 位置 + 大盘配合。"""
        sector_stats = getattr(self, "_sector_stats", {}) or {}

        regime = getattr(self, "_regime", None)
        market_env = getattr(regime, "pattern", "normal") if regime else "normal"
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"

        for item in candidates:
            industry = item["industry"]
            sector_info = sector_stats.get(industry, {})
            sector_pct = sector_info.get("change_pct", 0)

            # 1. 板块排名分（满分 25）— 使用 layer1 预计算的排名分数
            sector_rank_score = item.get("sector_rank_bonus", 3)

            # 2. 板块涨幅分（满分 12）— 使用 layer1 预计算的涨幅分数
            sector_pct_score = item.get("sector_pct_bonus", 0)

            # 2b. 板块连续性分（满分 10）— 连续走强比突然冒出来可靠
            continuity = getattr(self, "_sector_trend_continuity", {}).get(industry, 0)
            if continuity >= 5:
                continuity_score = 10
            elif continuity >= 3:
                continuity_score = 7
            elif continuity >= 2:
                continuity_score = 4
            else:
                continuity_score = continuity  # 1轮=1分

            # 3. 量能爆发分（满分 15）— 放量为加分项
            vol_ratio = item["vol_ratio"]
            if item.get("vol_expanding"):
                vol_score = min(15, (vol_ratio - 1.0) * 6)
            else:
                vol_score = max(0, (vol_ratio - 0.8) * 3) if vol_ratio > 0.8 else 0

            # 4. 加速强度分（满分 15）— 加速为加分项
            if item.get("chg_accel_bonus"):
                accel = item["chg_accel"]
                accel_score = min(15, accel * 5)
            else:
                accel_score = max(0, item["chg_accel"] * 2)

            # 5. 位置安全分（满分 18）
            pretrend_pos = item.get("pretrend_position", "unknown")
            if pretrend_pos == "low":
                position_score = 18
            elif pretrend_pos == "mid":
                position_score = 12
            elif pretrend_pos == "high":
                position_score = 3
            else:
                position_score = max(0, 12 - item["change_pct"] * 3)

            # 6. 前日趋势质量分（满分 15）
            pretrend_form = item.get("pretrend_form", "unknown")
            if pretrend_form == "climbing":
                pretrend_score = 15
            elif pretrend_form == "oscillating_up":
                pretrend_score = 10
            elif pretrend_form == "unknown":
                pretrend_score = 5
            else:
                pretrend_score = 2

            # 7. 大盘配合分（满分 10）
            market_score = 10
            if market_env in ("uptrend", "normal"):
                market_score = 10
            elif market_env in ("v_reversal", "w_bottom", "gap_down_recover"):
                market_score = 7
            elif market_env in ("cautious", "wide_choppy"):
                market_score = 4
            else:
                market_score = 2
            if risk_level in ("cautious",):
                market_score *= 0.7

            total = (
                sector_rank_score
                + sector_pct_score
                + continuity_score
                + vol_score
                + accel_score
                + position_score
                + pretrend_score
                + market_score
            )
            item["score"] = total  # 满分 ~120
            item["sector_pct"] = sector_pct
            item["market_env"] = market_env
            item["risk_level"] = risk_level

        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        return candidates

    # ═══════════════════════════════════════════════════════════════
    # 第三层：AI 异步过滤
    # ═══════════════════════════════════════════════════════════════

    def _scout_submit_ai(self, item: dict) -> bool:
        """提交 breakout 场景 AI 判断。"""
        code = item["code"]
        name = self._resolve_name(code)

        # 量能描述
        vol_ratio = item.get("vol_ratio", 1.0)
        if vol_ratio > 3.0:
            amount_desc = "量能爆发（>3倍）"
        elif vol_ratio > 2.0:
            amount_desc = "显著放量（>2倍）"
        elif vol_ratio > 1.5:
            amount_desc = "温和放量"
        else:
            amount_desc = "量能正常"

        # 价格走势描述
        price_hist = (getattr(self, "_snapshot_price_history", {}) or {}).get(code, [])
        if len(price_hist) >= 5:
            recent = [p for _, p in price_hist[-5:]]
            if all(recent[i] <= recent[i + 1] for i in range(len(recent) - 1)):
                price_trend = "持续走高 未回落"
            elif recent[-1] > recent[0]:
                price_trend = "震荡上行"
            elif recent[-1] < recent[0]:
                price_trend = "冲高回落"
            else:
                price_trend = "横盘"
        else:
            price_trend = "数据不足"

        # 大盘高低点
        index_prices = getattr(self, "_index_prices", []) or []
        index_high = max(index_prices) if index_prices else 0
        index_low = min(index_prices) if index_prices else 0

        try:
            ok = self._submit_scenario_ai(
                key=f"scout:{code}",
                scenario="breakout",
                code=code,
                name=name,
                price=item["price"],
                change_pct=item["change_pct"],
                sector_name=item["industry"],
                sector_pct=item["sector_pct"],
                sector_rank=item["sector_rank"],
                sector_total=item["sector_total"],
                amount_desc=amount_desc,
                price_trend=price_trend,
                market_env=item["market_env"],
                risk_level=item["risk_level"],
                index_high=index_high,
                index_low=index_low,
            )
        except Exception as e:
            logger.warning(f"Scout AI 提交失败 [{code}]: {e}")
            return False

        if ok:
            self._scout_ai_pending[f"scout:{code}"] = {
                **item,
                "name": name,
                "submitted_at": time.time(),
            }
        return ok

    # ═══════════════════════════════════════════════════════════════
    # 第四层：处理 AI 结果 + 执行
    # ═══════════════════════════════════════════════════════════════

    def _scout_process_ai(self):
        """处理已完成的 AI 结果，执行买入。"""
        for akey in list(self._scout_ai_pending.keys()):
            result = self._ai_queue.pop_result(akey)
            if result is None:
                ctx = self._scout_ai_pending[akey]
                if time.time() - ctx.get("submitted_at", 0) > self.SCOUT_AI_TIMEOUT:
                    del self._scout_ai_pending[akey]
                continue

            ctx = self._scout_ai_pending.pop(akey)
            if not result:
                continue

            code = ctx["code"]
            name = ctx["name"]

            decision = self._scout_parse_decision(result)
            logger.info(f"Scout AI [{code} {name}] 决策={decision} 评分={ctx.get('score', 0):.0f} 原话={result[:50]}")

            if decision == "buy":
                self._scout_execute_buy(ctx)

            if decision in ("buy", "观望"):
                self._scout_push_alert(ctx, result, decision)

    def _scout_parse_decision(self, text: str) -> str:
        """解析 AI 返回的决策：买入 / 观望 / 放弃。"""
        t = text.strip()
        if "买入" in t and "不" not in t:
            return "buy"
        if "观望" in t:
            return "观望"
        return "放弃"

    def _scout_execute_buy(self, ctx: dict) -> bool:
        """引擎2 模拟盘买入。使用引擎1统一仓位计算。成功返回 True。"""
        code = ctx["code"]
        name = ctx["name"]
        price = ctx["price"]

        # 同板块检查
        industry = ctx.get("industry", "")
        same_sector_count = 0
        for held_code in self._scout_positions:
            held_industry = (getattr(self, "_industry_cache", {}) or {}).get(held_code, "")
            if held_industry == industry:
                same_sector_count += 1
        if same_sector_count >= self.MAX_SAME_SECTOR:
            logger.info(f"Scout [{code}] 板块{industry}已达上限{self.MAX_SAME_SECTOR}只，跳过")
            return False

        # 使用引擎1统一仓位计算
        pattern = (
            getattr(getattr(self, "_regime", None), "pattern", "normal")
            if hasattr(self, "_regime", "pattern")
            else "normal"
        )
        trend = self._get_sector_trend(code)
        max_amount, _ = self._calculate_position_size(code, price, price * 0.98, price * 1.02, pattern, trend)
        amount = max(max_amount, self.MIN_POSITION_AMOUNT)

        # 现金约束
        max_affordable = int(self.paper_account.cash * 0.9 / price / 100) * 100
        volume = min(int(amount / price / 100) * 100, max_affordable)
        if volume < 100:
            return False

        result = self.paper_account.buy(
            code,
            name,
            price,
            volume,
            source=f"盘中机会(评分{ctx.get('score', 0):.0f})",
        )
        if result.success:
            self._scout_positions.add(code)
            self._scout_recent_sectors[industry] = self._scan_count
            logger.info(
                f"Scout 买入 [{code} {name}] {volume}股 @{price:.2f} 金额{amount} 评分{ctx.get('score', 0):.0f}"
            )
            return True
        return False

    def _scout_push_alert(self, ctx: dict, ai_text: str, decision: str):
        """推送盘中机会到 Telegram。"""
        code = ctx["code"]
        name = ctx["name"]
        emoji = "🔥" if decision == "buy" else "👀"
        label = "底部发力·已买入" if decision == "buy" else "底部发力·关注"

        msg = (
            f"{emoji} {label} — {code} {name}\n"
            f"   现价: {ctx['price']:.2f}  +{ctx['change_pct']:.1f}%  "
            f"加速{ctx.get('chg_accel', 0):+.1f}%  量比{ctx.get('vol_ratio', 0):.1f}x\n"
            f"   板块: {ctx['industry']} +{ctx['sector_pct']:.1f}%  "
            f"评分: {ctx.get('score', 0):.0f}\n"
            f"   🤖 {ai_text}"
        )
        self._alert(msg)

    # ═══════════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════════

    def _scout_position_count(self) -> int:
        """统计引擎2当前持仓数。"""
        count = 0
        for code in list(self._scout_positions):
            if code in self.paper_account.positions:
                count += 1
            else:
                self._scout_positions.discard(code)
        return count

    def _scout_cleanup_stale(self):
        """定期清理过期状态。"""
        now_scan = self._scan_count
        stale_sectors = [s for s, scan in self._scout_recent_sectors.items() if now_scan - scan > 30]
        for s in stale_sectors:
            del self._scout_recent_sectors[s]
        self._scout_position_count()

    # ═══════════════════════════════════════════════════════════════
    # 前2-3天日线趋势 + 位置检测
    # ═══════════════════════════════════════════════════════════════

    def _scout_ensure_pretrend_loaded(self):
        """加载前2-3个交易日的日线数据，整个会话只加载一次。"""
        if self._scout_pretrend_loaded:
            return
        db_path = getattr(settings, "DATABASE_PATH", "")
        if not db_path:
            return
        try:
            conn = connect(db_path)
            # 找最近3个交易日（不含今天）
            recent_dates = conn.execute(
                """SELECT DISTINCT trade_date FROM stock_basic
                   WHERE trade_date < date('now')
                   ORDER BY trade_date DESC LIMIT 3"""
            ).fetchall()
            if not recent_dates:
                conn.close()
                return
            dates = [r[0] for r in recent_dates]
            placeholders = ",".join("?" for _ in dates)
            rows = conn.execute(
                f"""SELECT stock_code, trade_date, price, open, high, low,
                           change_pct, volume_ratio, ma5, ma10, ma20
                    FROM stock_basic
                    WHERE trade_date IN ({placeholders})
                    ORDER BY stock_code, trade_date""",
                dates,
            ).fetchall()
            conn.close()

            for r in rows:
                code = r["stock_code"]
                self._scout_daily_bars.setdefault(code, []).append(dict(r))

            self._scout_pretrend_loaded = True
            logger.info(f"预趋势数据加载完成: {len(self._scout_daily_bars)} 只股票, 日期 {dates}")
        except Exception as e:
            logger.warning(f"预趋势数据加载失败: {e}")
            self._scout_pretrend_loaded = True  # 标记已尝试，不反复重试

    def _scout_check_pretrend(self, code: str) -> dict:
        """根据前2-3天日线判断个股的趋势形态和位置。
        返回 {'form': 'climbing'|'oscillating_up'|'weak'|'distributing'|'unknown',
               'position': 'low'|'mid'|'high'|'unknown',
               'vol_trend': 'expanding'|'contracting'|'stable'|'unknown',
               'cum_chg': float}
        """
        bars = self._scout_daily_bars.get(code, [])
        if len(bars) < 2:
            return {
                "form": "unknown",
                "position": "unknown",
                "vol_trend": "unknown",
                "cum_chg": 0,
            }

        changes = [b.get("change_pct") or 0 for b in bars]
        vol_ratios = [b.get("volume_ratio") or 1.0 for b in bars]
        cum_chg = sum(changes)

        # 趋势形态判断
        up_days = sum(1 for c in changes if c > 0)
        vol_increasing = len(vol_ratios) >= 2 and vol_ratios[-1] > vol_ratios[0] and all(v > 1.0 for v in vol_ratios)

        if up_days == len(changes) and vol_increasing:
            form = "climbing"  # 连续爬升：每天涨 + 量递增
        elif up_days >= len(changes) - 1 and cum_chg > 0:
            # 震荡向上：最多一天跌，整体上涨，中心上移
            highs = [b.get("high") or 0 for b in bars]
            lows = [b.get("low") or 0 for b in bars]
            center_first = (highs[0] + lows[0]) / 2
            center_last = (highs[-1] + lows[-1]) / 2
            form = "oscillating_up" if center_last > center_first else "weak"
        elif cum_chg < -2:
            form = "distributing"  # 放量下跌
        else:
            form = "unknown"

        # 位置判断：用日线高低点算20日近似位置
        try:
            all_highs = [b.get("high") or 0 for b in bars]
            all_lows = [b.get("low") or 0 for b in bars]
            # 也把今天盘中价格纳入计算
            snapshot = getattr(self, "_market_snapshot", {}) or {}
            item = snapshot.get(code, {})
            today_price = float(item.get("price", 0))
            if today_price > 0:
                all_highs.append(today_price)
                all_lows.append(today_price)
            hh, ll = max(all_highs), min(all_lows)
            if hh > ll > 0:
                last_price = bars[-1].get("price") or today_price
                pos_pct = (last_price - ll) / (hh - ll) * 100
                if pos_pct > 75:
                    position = "high"
                elif pos_pct < 35:
                    position = "low"
                else:
                    position = "mid"
            else:
                position = "unknown"
        except Exception:
            position = "unknown"

        # 量能趋势
        if len(vol_ratios) >= 2 and vol_ratios[-1] > vol_ratios[0] * 1.1:
            vol_trend = "expanding"
        elif len(vol_ratios) >= 2 and vol_ratios[-1] < vol_ratios[0] * 0.9:
            vol_trend = "contracting"
        else:
            vol_trend = "stable"

        return {
            "form": form,
            "position": position,
            "vol_trend": vol_trend,
            "cum_chg": round(cum_chg, 2),
        }
