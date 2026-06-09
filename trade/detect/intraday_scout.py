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
SCOUT_END = dt_time(11, 30)  # 引擎2 只在早盘运行


class IntradayScoutMixin:
    """盘中机会发现 — 底部发力策略。"""

    # ── 运行参数 ──
    SCOUT_INTERVAL = 3  # 每 N 轮触发一次
    SCOUT_AI_TIMEOUT = 45  # AI 超时（秒）

    # ── 风控限制 ──
    MAX_SCOUT_POSITIONS = 4  # 引擎2 最大持仓数
    MIN_POSITION_AMOUNT = 3000  # 单只最小买入金额
    MAX_POSITION_AMOUNT = 5000  # 单只最大买入金额
    MAX_SAME_SECTOR = 2  # 同板块最多持仓数

    # ── 底部发力筛选阈值 ──
    CHANGE_MIN = 0.5  # 最低涨幅 %（刚启动）
    CHANGE_MAX = 3.5  # 最高涨幅 %（还没涨太多）
    MIN_PRICE = 5.0  # 最低价格（排除垃圾股）
    VOL_EXPAND_RATIO = 1.2  # 量能放大倍数
    CHG_ACCEL_MIN = 0.3  # 涨幅加速最小幅度 %
    SECTOR_TOP_N = 10  # 板块排名门槛
    SECTOR_MIN_PCT = 0.0  # 板块最小涨幅 %（必须为正）

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

        # ── 时段门控：只在 10:00-11:30 运行 ──
        now = datetime.now().time()
        if now < dt_time(10, 0) or now > SCOUT_END:
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

        # ── 第三层：提交 AI ──
        remaining = self.MAX_SCOUT_POSITIONS - scout_count
        submitted = 0
        for item in ranked:
            if submitted >= remaining * 2:
                break
            akey = f"scout:{item['code']}"
            if akey in self._scout_ai_pending:
                continue
            if self._ai_queue.has_pending(akey):
                continue
            if self._scout_submit_ai(item):
                submitted += 1

        # ── 第四层：处理 AI 结果 ──
        self._scout_process_ai()

        # 保存本轮数据供下轮对比
        self._prev_snapshot_amounts = {code: float(item.get("amount", 0) or 0) for code, item in snapshot.items()}
        self._prev_snapshot_changes = {code: float(item.get("changePct", 0)) for code, item in snapshot.items()}

    # ═══════════════════════════════════════════════════════════════
    # 第一层：底部发力硬条件过滤
    # ═══════════════════════════════════════════════════════════════

    def _scout_layer1_filter(self, snapshot: dict) -> list[dict]:
        """底部发力检测：低涨幅 + 放量 + 加速 + 板块联动。

        条件:
        1. 涨幅 0.5-3.5%（刚启动，不是已经涨了）
        2. 量能环比放大 > 1.2x（资金在进）
        3. 涨幅加速 > 0.3%（动量在增强）
        4. 所属板块在 TOP10 且板块涨 > 0（有板块支撑）
        5. 价格 > 5 元（排除垃圾股）
        """
        sector_stats = getattr(self, "_sector_stats", {}) or {}

        # ── 板块 TOP N（只保留上涨板块）──
        top_sectors: set[str] = set()
        ranked_sectors = sorted(
            sector_stats.items(),
            key=lambda x: x[1].get("change_pct", 0),
            reverse=True,
        )
        for i, (name, stats) in enumerate(ranked_sectors):
            if i >= self.SECTOR_TOP_N:
                break
            if stats.get("change_pct", 0) >= self.SECTOR_MIN_PCT:
                top_sectors.add(name)

        if not top_sectors:
            return []

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

            # 1. 涨幅范围：刚启动，不是已经涨了
            if change_pct < self.CHANGE_MIN or change_pct > self.CHANGE_MAX:
                continue

            # 2. 价格门槛：排除垃圾股
            if price < self.MIN_PRICE:
                continue

            # 3. 板块过滤：必须在上涨板块中
            industry = industry_cache.get(code, "")
            if industry not in top_sectors:
                continue

            # 4. 量能放大：环比 > 1.2x
            prev_amount = prev_amounts.get(code, 0)
            vol_expanding = prev_amount > 0 and amount > prev_amount * self.VOL_EXPAND_RATIO

            # 5. 涨幅加速：chg > prev_chg + 0.3%
            prev_chg = prev_changes.get(code, 0)
            chg_accel = change_pct - prev_chg if prev_chg != 0 else 0

            # 必须同时满足量能放大 + 涨幅加速
            if not vol_expanding or chg_accel < self.CHG_ACCEL_MIN:
                continue

            # 排除已有推送
            alert_fps = getattr(self, "_alert_fingerprints", {}) or {}
            if any(code in k for k in alert_fps):
                continue

            # 排除已有持仓
            positions = getattr(self.paper_account, "positions", {}) or {}
            if code in positions:
                continue

            # 排除近期卖出（冷却期内）
            recently_sold = getattr(self, "_recently_sold", {}) or {}
            if code in recently_sold:
                scan_diff = self._scan_count - recently_sold[code]
                if scan_diff < 60:
                    continue

            # 前2-3天趋势检测：区分「有准备的启动」和「一日游」
            pretrend = self._scout_check_pretrend(code)
            # 放量下跌形态 → 排除，不是底部发力
            if pretrend["form"] == "distributing":
                continue
            # 高位加速 → 排除，可能是赶顶而非底部发力
            if pretrend["position"] == "high":
                continue

            candidates.append(
                {
                    "code": code,
                    "price": price,
                    "change_pct": change_pct,
                    "chg_accel": chg_accel,
                    "amount": amount,
                    "prev_amount": prev_amount,
                    "vol_ratio": amount / prev_amount if prev_amount > 0 else 0,
                    "industry": industry,
                    "pretrend_form": pretrend["form"],
                    "pretrend_position": pretrend["position"],
                    "pretrend_cum_chg": pretrend["cum_chg"],
                }
            )

        return candidates

    # ═══════════════════════════════════════════════════════════════
    # 第二层：多维打分排序
    # ═══════════════════════════════════════════════════════════════

    def _scout_layer2_rank(self, candidates: list[dict]) -> list[dict]:
        """六维打分：加速强度 + 量能爆发 + 板块强度 + 位置安全 + 前日趋势 + 大盘配合。"""
        sector_stats = getattr(self, "_sector_stats", {}) or {}
        total_sectors = len(sector_stats) if sector_stats else 86

        ranked_sectors = sorted(
            sector_stats.items(),
            key=lambda x: x[1].get("change_pct", 0),
            reverse=True,
        )
        sector_rank_map = {name: i + 1 for i, (name, _) in enumerate(ranked_sectors)}

        regime = getattr(self, "_regime", None)
        market_env = getattr(regime, "pattern", "normal") if regime else "normal"
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"

        for item in candidates:
            industry = item["industry"]
            sector_info = sector_stats.get(industry, {})
            sector_pct = sector_info.get("change_pct", 0)
            sector_rank = sector_rank_map.get(industry, total_sectors)
            sector_vol = sector_info.get("vol_ratio", 1.0)  # 板块量比

            # 1. 加速强度分（满分 30）— 加速越快越好
            accel = item["chg_accel"]
            accel_score = min(30, accel * 7)

            # 2. 量能爆发分（满分 22）— 放量越大越好，但有上限
            vol_ratio = item["vol_ratio"]
            vol_score = min(22, (vol_ratio - 1.0) * 8) if vol_ratio > 1.0 else 0

            # 3. 板块强度分（满分 18）
            sector_score = max(0, (1 - sector_rank / total_sectors)) * 18
            # 板块量价齐升：板块涨 + 板块放量 → 个股有板块级支撑
            sector_vol_price = sector_pct > 0 and sector_vol > 1.0
            if sector_vol_price:
                sector_score += 3  # 板块量价齐升额外加分

            # 4. 位置安全分（满分 18）— 低位 > 中位 > 高位
            pretrend_pos = item.get("pretrend_position", "unknown")
            if pretrend_pos == "low":
                position_score = 18
            elif pretrend_pos == "mid":
                position_score = 10
            elif pretrend_pos == "high":
                position_score = 3  # 高位仍有分，但很低
            else:
                # 无预趋势数据时回退：涨幅越低分越高（安全边际）
                position_score = max(0, 12 - item["change_pct"] * 3)

            # 5. 前日趋势质量分（满分 12）
            pretrend_form = item.get("pretrend_form", "unknown")
            if pretrend_form == "climbing":
                # 连续爬升 + 连续放量 → 有准备的启动
                pretrend_score = 12
            elif pretrend_form == "oscillating_up":
                # 震荡向上 + 中心上移 → 温和蓄力
                pretrend_score = 8
            elif pretrend_form == "unknown":
                pretrend_score = 4  # 数据不足，给基准分
            else:
                pretrend_score = 0

            # 板块量价齐升 + 个股低位爬升 → 最强组合
            if sector_vol_price and pretrend_pos == "low" and pretrend_form == "climbing":
                pretrend_score += 3

            # 6. 大盘配合分（满分 10）
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

            item["score"] = accel_score + vol_score + sector_score + position_score + pretrend_score + market_score
            item["sector_pct"] = sector_pct
            item["sector_rank"] = sector_rank
            item["sector_total"] = total_sectors
            item["market_env"] = market_env
            item["risk_level"] = risk_level
            item["sector_vol_price"] = sector_vol_price

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

    def _scout_execute_buy(self, ctx: dict):
        """引擎2 模拟盘买入。"""
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
            return

        amount = self.MAX_POSITION_AMOUNT
        volume = int(amount / price / 100) * 100
        if volume < 100:
            return

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
