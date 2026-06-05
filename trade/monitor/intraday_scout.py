"""盘中机会发现引擎（引擎2）— 全市场扫描 + 四层筛选管线。

Mixin 方式混入 Watcher。
与引擎1（复盘趋势跟踪）互补：引擎1等回踩，引擎2追突破。
"""

import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class IntradayScoutMixin:
    """盘中机会发现 — 全市场扫描，不等均线回踩。"""

    # ── 运行参数 ──
    SCOUT_INTERVAL = 3          # 每 N 轮触发一次
    SCOUT_AI_TIMEOUT = 45       # AI 超时（秒）

    # ── 风控限制 ──
    MAX_SCOUT_POSITIONS = 4     # 引擎2 最大持仓数
    MIN_POSITION_AMOUNT = 3000  # 单只最小买入金额
    MAX_POSITION_AMOUNT = 5000  # 单只最大买入金额
    MAX_SAME_SECTOR = 2         # 同板块最多持仓数

    # ── 筛选阈值 ──
    CHANGE_MIN = 2.0            # 最低涨幅 %
    CHANGE_MAX = 7.0            # 最高涨幅 %
    SECTOR_TOP_N = 15           # 板块排名门槛
    SECTOR_MIN_PCT = 0.5        # 板块最小涨幅 %

    # ── 内部状态 ──
    _prev_snapshot_amounts: dict[str, float] = {}   # 上轮 amount
    _scout_ai_pending: dict[str, dict] = {}          # AI 待处理
    _scout_recent_sectors: dict[str, int] = {}       # 板块→最近推送 scan_count
    _scout_positions: set[str] = set()               # 引擎2当前持仓 code

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

        # 风控暂停
        regime = getattr(self, "_regime", None)
        risk = getattr(regime, "risk_level", "safe") if regime else "safe"
        if risk in ("extreme", "dangerous"):
            return

        # 大盘回撤暂停（日亏损 > 2% → 暂停引擎2）
        pa = self.paper_account
        if pa.daily_pnl < 0 and pa.total_value > 0:
            if abs(pa.daily_pnl) / pa.total_value > 0.02:
                return

        # 引擎2 仓位已满
        scout_count = self._scout_position_count()
        if scout_count >= self.MAX_SCOUT_POSITIONS:
            return

        # ── 第一层：硬筛 ──
        candidates = self._scout_layer1_filter(snapshot)
        if not candidates:
            return

        # ── 第二层：打分排序 ──
        ranked = self._scout_layer2_rank(candidates)

        # ── 第三层：提交 AI ──
        remaining = self.MAX_SCOUT_POSITIONS - scout_count
        submitted = 0
        for item in ranked:
            if submitted >= remaining * 2:  # 最多提交剩余槽位 x2 的 AI
                break
            # 跳过已有 pending AI 的同票
            akey = f"scout:{item['code']}"
            if akey in self._scout_ai_pending:
                continue
            if self._ai_queue.has_pending(akey):
                continue
            if self._scout_submit_ai(item):
                submitted += 1

        # ── 第四层：处理 AI 结果 ──
        self._scout_process_ai()

        # 保存本轮 amount 供下轮对比
        self._prev_snapshot_amounts = {
            code: float(item.get("amount", 0) or 0)
            for code, item in snapshot.items()
        }

    # ═══════════════════════════════════════════════════════════════
    # 第一层：硬条件过滤
    # ═══════════════════════════════════════════════════════════════

    def _scout_layer1_filter(self, snapshot: dict) -> list[dict]:
        """硬条件过滤：涨跌幅 + 板块 + 量能 + 非垃圾股。"""
        sector_stats = getattr(self, "_sector_stats", {}) or {}

        # ── 板块 TOP N ──
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
        industry_cache = getattr(self, "_industry_cache", {}) or {}

        for code, item in snapshot.items():
            try:
                change_pct = float(item.get("changePct", 0))
                price = float(item.get("price", 0))
                amount = float(item.get("amount", 0) or 0)
            except (ValueError, TypeError):
                continue

            # 涨跌幅范围
            if change_pct < self.CHANGE_MIN or change_pct > self.CHANGE_MAX:
                continue

            # 有效价格
            if price <= 0:
                continue

            # 板块过滤
            industry = industry_cache.get(code, "")
            if industry not in top_sectors:
                continue

            # 量能确认：amount 环比增长
            prev_amount = prev_amounts.get(code, 0)
            amount_up = prev_amount > 0 and amount > prev_amount

            # 价格动量：近5分钟趋势
            price_hist = (
                getattr(self, "_snapshot_price_history", {}) or {}
            ).get(code, [])
            price_trending_up = False
            if len(price_hist) >= 3:
                recent_prices = [p for _, p in price_hist[-5:]]
                if len(recent_prices) >= 3:
                    price_trending_up = recent_prices[-1] > recent_prices[0]

            # 至少满足量增或价涨之一
            if not amount_up and not price_trending_up:
                continue

            # 排除引擎1已推送过的（同票30轮内不重复）
            alert_fps = getattr(self, "_alert_fingerprints", {}) or {}
            already_alerted = any(
                code in k for k in alert_fps
            )
            if already_alerted:
                continue

            # 排除已有持仓
            positions = getattr(self.paper_account, "positions", {}) or {}
            if code in positions:
                continue

            # 排除近期卖出（冷却期内）
            recently_sold = getattr(self, "_recently_sold", {}) or {}
            if code in recently_sold:
                scan_diff = self._scan_count - recently_sold[code]
                if scan_diff < 60:  # 卖出后 ~15 分钟内不回补
                    continue

            candidates.append({
                "code": code,
                "price": price,
                "change_pct": change_pct,
                "amount": amount,
                "amount_up": amount_up,
                "price_trending_up": price_trending_up,
                "industry": industry,
            })

        return candidates

    # ═══════════════════════════════════════════════════════════════
    # 第二层：多维打分排序
    # ═══════════════════════════════════════════════════════════════

    def _scout_layer2_rank(self, candidates: list[dict]) -> list[dict]:
        """四维打分：板块强度 + 价格动量 + 量能 + 大盘配合。"""
        sector_stats = getattr(self, "_sector_stats", {}) or {}
        all_sectors = list(sector_stats.keys())
        total_sectors = len(all_sectors) if all_sectors else 86

        # ── 板块排名映射 ──
        ranked_sectors = sorted(
            sector_stats.items(),
            key=lambda x: x[1].get("change_pct", 0),
            reverse=True,
        )
        sector_rank_map = {
            name: i + 1 for i, (name, _) in enumerate(ranked_sectors)
        }

        regime = getattr(self, "_regime", None)
        market_env = getattr(regime, "pattern", "normal") if regime else "normal"
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"

        for item in candidates:
            industry = item["industry"]
            sector_info = sector_stats.get(industry, {})
            sector_pct = sector_info.get("change_pct", 0)
            sector_rank = sector_rank_map.get(industry, total_sectors)

            # 1. 板块强度分（排名越靠前越高，满分 35）
            rank_score = max(0, (1 - sector_rank / total_sectors)) * 35

            # 2. 价格动量分（涨幅在 2-7% 区间线性映射，满分 25）
            momentum_score = (item["change_pct"] - self.CHANGE_MIN) / (
                self.CHANGE_MAX - self.CHANGE_MIN
            ) * 25

            # 3. 量能分（放量 + 价涨=高分，满分 20）
            volume_score = 0
            if item["amount_up"] and item["price_trending_up"]:
                volume_score = 20  # 量价配合
            elif item["amount_up"]:
                volume_score = 12  # 仅放量
            elif item["price_trending_up"]:
                volume_score = 8   # 仅价涨

            # 4. 大盘配合分（满分 20）
            market_score = 0
            if market_env in ("uptrend", "normal"):
                market_score = 20
            elif market_env in ("v_reversal", "w_bottom", "late_rally", "wide_choppy"):
                market_score = 12
            elif market_env in ("cautious",):
                market_score = 6
            if risk_level in ("cautious",):
                market_score *= 0.7

            item["score"] = rank_score + momentum_score + volume_score + market_score
            item["sector_pct"] = sector_pct
            item["sector_rank"] = sector_rank
            item["sector_total"] = total_sectors
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
        if item["amount_up"] and item["price_trending_up"]:
            amount_desc = "量价齐升"
        elif item["amount_up"]:
            amount_desc = "放量中"
        else:
            amount_desc = "价涨量平"

        # 价格走势描述
        price_hist = (
            getattr(self, "_snapshot_price_history", {}) or {}
        ).get(code, [])
        if len(price_hist) >= 5:
            recent = [p for _, p in price_hist[-5:]]
            if all(recent[i] <= recent[i + 1] for i in range(len(recent) - 1)):
                price_trend = "持续走高 未回落"
            elif recent[-1] > recent[0]:
                price_trend = "震荡上行"
            else:
                price_trend = "高位震荡"
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

            # ── 解析 AI 返回 ──
            decision = self._scout_parse_decision(result)
            logger.info(
                f"Scout AI [{code} {name}] 决策={decision} "
                f"评分={ctx.get('score', 0):.0f} 原话={result[:50]}"
            )

            if decision == "buy":
                self._scout_execute_buy(ctx)

            # 推送 Telegram（买入或观望都推，放弃不推）
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
            held_industry = (
                getattr(self, "_industry_cache", {}) or {}
            ).get(held_code, "")
            if held_industry == industry:
                same_sector_count += 1
        if same_sector_count >= self.MAX_SAME_SECTOR:
            logger.info(f"Scout [{code}] 板块{industry}已达上限{self.MAX_SAME_SECTOR}只，跳过")
            return

        # 计算买入量
        amount = self.MAX_POSITION_AMOUNT
        volume = int(amount / price / 100) * 100
        if volume < 100:
            return

        result = self.paper_account.buy(
            code, name, price, volume,
            source=f"盘中机会(评分{ctx.get('score', 0):.0f})",
        )
        if result.success:
            self._scout_positions.add(code)
            # 记录板块推送时间
            self._scout_recent_sectors[industry] = self._scan_count
            logger.info(
                f"Scout 买入 [{code} {name}] {volume}股 @{price:.2f} "
                f"金额{amount} 评分{ctx.get('score', 0):.0f}"
            )

    def _scout_push_alert(self, ctx: dict, ai_text: str, decision: str):
        """推送盘中机会到 Telegram。"""
        code = ctx["code"]
        name = ctx["name"]
        emoji = "🔥" if decision == "buy" else "👀"
        label = "盘中机会·已买入" if decision == "buy" else "盘中机会·关注"

        msg = (
            f"{emoji} {label} — {code} {name}\n"
            f"   现价: {ctx['price']:.2f}  +{ctx['change_pct']:.1f}%  "
            f"评分: {ctx.get('score', 0):.0f}\n"
            f"   板块: {ctx['industry']} +{ctx['sector_pct']:.1f}%  "
            f"#{ctx['sector_rank']}/{ctx['sector_total']}\n"
            f"   🤖 {ai_text}"
        )
        self._alert(msg)

    # ═══════════════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════════════

    def _scout_position_count(self) -> int:
        """统计引擎2当前持仓数（从持仓中识别 scout 来源）。"""
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
        # 清理板块冷却（30轮 = ~7.5分钟）
        stale_sectors = [
            s for s, scan in self._scout_recent_sectors.items()
            if now_scan - scan > 30
        ]
        for s in stale_sectors:
            del self._scout_recent_sectors[s]
        # 清理已卖出的持仓记录
        self._scout_position_count()
