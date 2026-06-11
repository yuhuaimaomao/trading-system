"""尾盘选股引擎（引擎3）— 14:30~14:57 多轮筛选 + 次日卖出。

策略核心：赚隔夜信息差 + 次日惯性冲高。
四维筛选：量价结构 > 趋势过滤 > 资金面 > 板块确认。
每轮拉腾讯 qt 刷新换手率/量比，首轮拉资金流后续复用。
全量融合快照写入 intraday_fusion 表，审计可回溯。

Mixin 方式混入 Watcher。与引擎1（趋势信号）和引擎2（底部发力）互补。
"""

import time
from collections import defaultdict
from datetime import datetime
from datetime import time as dt_time

from data.collect.events.limit_pool_collector import LimitPoolCollector
from data.collect.market.stock_basic_fusion import StockBasicFusionCollector
from data.trade.intraday_fusion import IntradayFusionRepo
from system.config import settings
from system.utils.logger import get_trade_logger

logger = get_trade_logger("detect")

# 引擎3 时间窗口
TAIL_START = dt_time(14, 30)
TAIL_CUTOFF = dt_time(14, 57)  # 最后3分钟集合竞价不可撤单

# 筛选阈值
CHANGE_MIN = 2.0
CHANGE_MAX = 7.0
AMPLITUDE_MIN = 3.0
AMPLITUDE_MAX = 8.0
TURNOVER_RATE_MIN = 3.0
TURNOVER_RATE_MAX = 15.0
CIRC_MCAP_MIN = 30e8  # 30亿
CIRC_MCAP_MAX = 500e8  # 500亿
TAIL_VOL_RATIO_MIN = 0.3  # 尾盘量占比最低（14:30后量/全天均量比例）
PRICE_NEAR_HIGH_RATIO = 0.98  # 收盘价在最高价 2% 以内
SECTOR_RANK_MAX = 20  # 板块排名前20
SECTOR_LIMIT_UP_MIN = 2  # 板块涨停家数下限


class TailCloseMixin:
    """引擎3 — 尾盘选股 + 次日卖出。"""

    # ── 运行参数 ──（名额从 settings 读取，不再硬编码）

    # ── 内部状态 ──
    _tail_active: bool = False
    _tail_round: int = 0
    _tail_round_ts: float = 0  # 当前轮 epoch timestamp
    _tail_candidates: dict = {}  # {code: {first_seen, rounds, scores, missed, latest}}
    _tail_positions: set[str] = set()  # 引擎3 本日持仓 code 集合（从 trade_orders 恢复）
    _tail_sector_limit_ups: dict[str, int] = {}  # {sector_name: 涨停家数}
    _tail_fund_data: dict = {}  # 首轮资金流缓存
    _tail_executed: bool = False
    _tail_fusion_repo: IntradayFusionRepo | None = None
    _tail_collector: StockBasicFusionCollector | None = None
    _tail_vol_baseline: dict[str, float] = {}  # 14:30 各股累计成交量基线

    # ═══════════════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════════════

    def _scan_tail_close(self):
        """引擎3 主入口 — 每 tick 由 _scan() 调用。"""
        # 时间门控
        now = datetime.now().time()
        if now < TAIL_START or now > TAIL_CUTOFF:
            return
        if self._tail_executed:
            return

        # 周五 / 长假前跳过
        if self._tail_check_skip_day():
            return

        # 大盘熔断
        regime = getattr(self, "_regime", None)
        risk = getattr(regime, "risk_level", "safe") if regime else "safe"
        if risk in ("extreme", "dangerous"):
            return

        # 数据未就绪
        if not getattr(self, "_data_ready", False):
            return
        snapshot = getattr(self, "_market_snapshot", None)
        if not snapshot:
            return

        # 激活
        if not self._tail_active:
            self._tail_activate()

        self._tail_round += 1
        self._tail_round_ts = time.time()
        round_type = "full" if self._tail_round == 1 else "qt_only"
        logger.info(f"引擎3 第{self._tail_round}轮开始 ts={self._tail_round_ts}")

        # ── 拉数据 ──
        qt = self._tail_fetch_qt()
        fund = self._tail_fetch_fund()

        # ── 融合 + 存库（审计）──
        self._tail_save_fusion(qt, fund, round_type)

        # ── 四维筛选 ──
        candidates = list(self._tail_screen(qt, fund))

        # ── 候选池跟踪 ──
        self._tail_track_candidates(candidates)

        logger.info(
            f"引擎3 第{self._tail_round}轮: qt={len(qt)}只, "
            f"候选={len(candidates)}只, 池中={len(self._tail_candidates)}只"
        )

        # ── 执行窗口 (14:50~14:57) ──
        if now >= dt_time(14, 50):
            self._tail_execute()

    # ═══════════════════════════════════════════════════════════════
    # 激活 / 重置
    # ═══════════════════════════════════════════════════════════════

    def _tail_activate(self):
        """引擎3 首次激活，初始化状态。"""
        self._tail_active = True
        self._tail_round = 0
        self._tail_candidates = {}
        self._tail_fund_data = {}
        self._tail_executed = False
        self._tail_fusion_repo = IntradayFusionRepo()
        self._tail_collector = StockBasicFusionCollector(self._trade_date)

        # 板块涨停家数（调涨停采集器，不落库）
        self._tail_sector_limit_ups = self._tail_fetch_limit_up_map()

        # 恢复持仓（盘中重启场景：从 trade_orders 找回今日引擎3 已买股票）
        self._tail_restore_positions()

        # 记录 14:30 各股累计量作为基线（用于后续计算尾盘量占比）
        snapshot = getattr(self, "_market_snapshot", {})
        self._tail_vol_baseline = {code: float(item.get("volume", 0)) for code, item in snapshot.items()}
        logger.info(
            f"引擎3 激活 — 尾盘选股开始 "
            f"(涨停板块={len(self._tail_sector_limit_ups)}个, 已持仓={len(self._tail_positions)}只)"
        )
        self._alert_private("🔍 引擎3 激活\n   尾盘选股开始 (14:30~14:57)")

    def _tail_reset(self):
        """每日重置 — 由 watcher run() 调用。"""
        self._tail_active = False
        self._tail_round = 0
        self._tail_candidates.clear()
        self._tail_positions.clear()
        self._tail_sector_limit_ups.clear()
        self._tail_fund_data.clear()
        self._tail_executed = False
        self._tail_vol_baseline.clear()
        self._tail_collector = None
        self._tail_fusion_repo = None

    # ═══════════════════════════════════════════════════════════════
    # 跳过判断（周五 + 长假前）
    # ═══════════════════════════════════════════════════════════════

    def _tail_check_skip_day(self) -> bool:
        """周五或节假日前一交易日返回 True，跳过尾盘选股。"""
        today = datetime.now()
        # 周五 (weekday=4)
        if today.weekday() == 4:
            logger.info("引擎3 跳过: 周五不持仓过周末")
            return True
        # 检查是否节假日前一交易日（从 trading_calendar 表查）
        try:
            conn = self.repo._signal._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT is_trading_day FROM trading_calendar WHERE trade_date = date(?, '+1 day')",
                (today.strftime("%Y-%m-%d"),),
            )
            row = cur.fetchone()
            conn.close()
            if row and row[0] == 0:
                logger.info("引擎3 跳过: 节假日前一交易日")
                return True
        except Exception:
            pass  # 表不存在或查询失败就跳过这个检查
        return False

    # ═══════════════════════════════════════════════════════════════
    # 板块涨停家数（调采集器，不落库）
    # ═══════════════════════════════════════════════════════════════

    def _tail_fetch_limit_up_map(self) -> dict[str, int]:
        """拉涨停池 → 按行业归属统计 → 返回 {sector_name: count}。"""
        try:
            collector = LimitPoolCollector()
            limit_ups = collector.fetch_limit_up(self._trade_date)
            if not limit_ups:
                logger.warning("引擎3 涨停池为空")
                return {}

            # 用 watcher 已有的行业缓存做 code→sector 映射
            industry_cache = getattr(self, "_industry_cache", {})
            sector_counts: dict[str, int] = defaultdict(int)
            for stock in limit_ups:
                code = stock.get("stock_code", "")
                # 优先用涨停池自带行业字段，缺失时用缓存
                sector = stock.get("industry", "") or industry_cache.get(code, "")
                if sector:
                    sector_counts[sector] += 1

            logger.info(f"引擎3 涨停池: {len(limit_ups)}只, {len(sector_counts)}个板块")
            return dict(sector_counts)
        except Exception as e:
            logger.warning(f"引擎3 涨停池拉取失败: {e}")
            return {}

    # ═══════════════════════════════════════════════════════════════
    # 持仓恢复（盘中重启 + 次日启动）
    # ═══════════════════════════════════════════════════════════════

    def _tail_restore_positions(self):
        """从 trade_orders 恢复引擎3 的当日持仓列表。"""
        try:
            conn = self.repo._signal._conn()
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT stock_code FROM trade_orders "
                "WHERE trade_date=? AND source='tail_close' AND side='buy'",
                (self._trade_date,),
            )
            self._tail_positions = {r[0] for r in cur.fetchall()}
            conn.close()
            if self._tail_positions:
                logger.info(f"引擎3 从DB恢复持仓: {self._tail_positions}")
        except Exception as e:
            logger.warning(f"引擎3 持仓恢复失败: {e}")
            self._tail_positions = set()

    # ═══════════════════════════════════════════════════════════════
    # 数据获取
    # ═══════════════════════════════════════════════════════════════

    def _tail_fetch_qt(self) -> dict:
        """拉腾讯 qt 全量数据。"""
        try:
            if self._tail_collector is None:
                self._tail_collector = StockBasicFusionCollector(self._trade_date)
            return self._tail_collector._fetch_tencent_qt_all()
        except Exception as e:
            logger.warning(f"引擎3 腾讯 qt 拉取失败: {e}")
            return {}

    def _tail_fetch_fund(self) -> dict:
        """拉资金流数据 — 首轮拉全量，后续复用缓存。"""
        if self._tail_fund_data:
            return self._tail_fund_data

        try:
            if self._tail_collector is None:
                self._tail_collector = StockBasicFusionCollector(self._trade_date)
            self._tail_fund_data = self._tail_collector._fetch_fund_flow()
            logger.info(f"引擎3 资金流: {len(self._tail_fund_data)} 只")
        except Exception as e:
            logger.warning(f"引擎3 资金流拉取失败: {e}")
            self._tail_fund_data = {}
        return self._tail_fund_data

    # ═══════════════════════════════════════════════════════════════
    # 审计落库
    # ═══════════════════════════════════════════════════════════════

    def _tail_save_fusion(self, qt: dict, fund: dict, round_type: str):
        """融合 qt + fund + QMT 快照 → 写 intraday_fusion。"""
        if self._tail_fusion_repo is None:
            return

        snapshot = getattr(self, "_market_snapshot", {})
        rows = []
        for code, q in qt.items():
            snap = snapshot.get(code, {})
            f = fund.get(code, {})
            rows.append(
                {
                    "trade_date": self._trade_date,
                    "round_ts": self._tail_round_ts,
                    "stock_code": code,
                    "stock_name": q.get("stock_name", ""),
                    "price": snap.get("price", q.get("price", 0)),
                    "open": snap.get("open", q.get("open", 0)),
                    "high": snap.get("high", q.get("high", 0)),
                    "low": snap.get("low", q.get("low", 0)),
                    "prev_close": snap.get("preClose", q.get("prev_close", 0)),
                    "change_pct": snap.get("changePct", q.get("change_pct", 0)),
                    "volume": snap.get("volume", q.get("volume", 0)),
                    "turnover": snap.get("amount", q.get("turnover", 0)),
                    "turnover_rate": q.get("turnover_rate", 0),
                    "amplitude": q.get("amplitude", 0),
                    "circ_market_cap": q.get("circ_market_cap", 0),
                    "volume_ratio": q.get("volume_ratio", 0),
                    "pe_ttm": q.get("pe_ttm", 0),
                    "main_force_net": f.get("main_force_net", 0),
                    "main_force_ratio": f.get("main_force_ratio", 0),
                    "round_type": round_type,
                    "is_candidate": 0,  # 下面 _tail_screen 后会更新
                    "candidate_score": 0,
                }
            )

        try:
            self._tail_fusion_repo.save_batch(self._trade_date, self._tail_round_ts, rows)
        except Exception as e:
            logger.warning(f"引擎3 融合落库失败: {e}")

    # ═══════════════════════════════════════════════════════════════
    # 四维筛选
    # ═══════════════════════════════════════════════════════════════

    def _tail_screen(self, qt: dict, fund: dict):
        """四维筛选 — 量价结构 + 趋势 + 资金面 + 板块确认。"""
        snapshot = getattr(self, "_market_snapshot", {})
        sector_stats = getattr(self, "_sector_stats", {})

        # 板块排名（按 change_pct 降序）
        ranked_sectors = sorted(
            sector_stats.items(),
            key=lambda x: x[1].get("change_pct", 0),
            reverse=True,
        )
        sector_rank_map = {name: i + 1 for i, (name, _) in enumerate(ranked_sectors)}

        for code, q in qt.items():
            snap = snapshot.get(code, {})
            if not snap or not snap.get("price"):
                continue
            f = fund.get(code, {})

            # ── 维度1: 量价结构 ──
            chg = float(snap.get("changePct", 0))
            if not (CHANGE_MIN <= chg <= CHANGE_MAX):
                continue

            amplitude = q.get("amplitude", 0)
            if not (AMPLITUDE_MIN <= amplitude <= AMPLITUDE_MAX):
                continue

            # 尾盘量占比 — 当前累计量 vs 14:30 基线
            vol_now = float(snap.get("volume", 0))
            vol_base = self._tail_vol_baseline.get(code, 0)
            if vol_base > 0:
                tail_vol = vol_now - vol_base
                tail_ratio = tail_vol / (vol_now / max(self._tail_round, 1)) if vol_now > 0 else 0
                if tail_ratio < TAIL_VOL_RATIO_MIN:
                    continue

            # 价格 > VWAP (amount/volume)
            amt = float(snap.get("amount", 0))
            vol = float(snap.get("volume", 0))
            if vol > 0 and amt > 0:
                vwap = amt / vol
                if snap.get("price", 0) <= vwap:
                    continue

            # 收盘在最高价附近
            high = float(snap.get("high", 0))
            price = float(snap.get("price", 0))
            if high > 0 and price < high * PRICE_NEAR_HIGH_RATIO:
                continue

            # ── 维度2: 趋势过滤 ──
            if not self._tail_check_trend(code, q):
                continue

            # ── 维度3: 资金面 ──
            turnover_rate = q.get("turnover_rate", 0)
            if not (TURNOVER_RATE_MIN <= turnover_rate <= TURNOVER_RATE_MAX):
                continue

            circ_mcap = q.get("circ_market_cap", 0)
            if not (CIRC_MCAP_MIN <= circ_mcap <= CIRC_MCAP_MAX):
                continue

            if f.get("main_force_net", 0) <= 0:
                continue

            # ── 维度4: 板块确认 ──
            sector_name = self._tail_get_sector_name(code)
            sector_rank = sector_rank_map.get(sector_name, 999)
            if sector_rank > SECTOR_RANK_MAX:
                continue
            # 板块涨停家数 ≥ SECTOR_LIMIT_UP_MIN
            sector_limits = self._tail_sector_limit_ups.get(sector_name, 0)
            if sector_limits < SECTOR_LIMIT_UP_MIN:
                continue

            # ── 评分 ──
            score = self._tail_score(q, snap, f, sector_rank)
            yield (code, score, q, snap, f)

    # ═══════════════════════════════════════════════════════════════
    # 趋势过滤
    # ═══════════════════════════════════════════════════════════════

    def _tail_check_trend(self, code: str, q: dict) -> bool:
        """趋势过滤：从 DB 查 MA 排列 + 近3日无量价恶化。

        用 stock_basic_fusion 或 stock_basic 表的最新日线数据。
        """
        try:
            conn = self.repo._signal._conn()
            cur = conn.cursor()
            # 查最近3日数据
            cur.execute(
                "SELECT price, change_pct, volume FROM stock_basic_fusion "
                "WHERE stock_code=? AND trade_date <= ? "
                "ORDER BY trade_date DESC LIMIT 5",
                (code, self._trade_date),
            )
            rows = cur.fetchall()
            conn.close()

            if len(rows) < 3:
                return False

            prices = [r[0] for r in rows if r[0]]
            volumes = [r[2] for r in rows if r[2]]
            changes = [r[1] for r in rows if r[1] is not None]

            # MA 多头: 5日 > 10日 > 20日 (用价格近似)
            if len(prices) >= 5:
                ma5 = sum(prices[:5]) / 5
                ma10 = sum(prices[:10]) / min(10, len(prices))
                ma20 = sum(prices[:20]) / min(20, len(prices))
                if not (ma5 > ma10 > ma20):
                    return False
                # 当前价 > MA5
                if q.get("price", 0) <= ma5:
                    return False

            # 近3日无放量大阴线 (-3%以上且当日量 > 5日均量1.5倍)
            if len(changes) >= 3 and len(volumes) >= 5:
                avg_vol = sum(volumes[:5]) / 5 if len(volumes) >= 5 else volumes[0]
                for i in range(min(3, len(changes))):
                    if changes[i] < -3 and volumes[i] > avg_vol * 1.5:
                        return False

            return True
        except Exception as e:
            logger.warning(f"引擎3 趋势检查失败 {code}: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # 板块工具
    # ═══════════════════════════════════════════════════════════════

    def _tail_get_sector_name(self, code: str) -> str:
        """获取个股所属行业板块名。"""
        cache = getattr(self, "_industry_cache", {})
        return cache.get(code, "")

    # ═══════════════════════════════════════════════════════════════
    # 评分
    # ═══════════════════════════════════════════════════════════════

    def _tail_score(self, q: dict, snap: dict, f: dict, sector_rank: int) -> float:
        """多维度加权评分，满分 ~100。"""
        score = 0.0

        # 量价 (40分)
        chg = float(snap.get("changePct", 0))
        score += min(chg / 7.0 * 20, 20)  # 涨幅分 (20)
        score += min(q.get("volume_ratio", 0) / 3.0 * 10, 10)  # 量比分 (10)
        score += min(q.get("turnover_rate", 0) / 15.0 * 10, 10)  # 换手分 (10)

        # 资金 (25分)
        mf_ratio = f.get("main_force_ratio", 0)
        score += min(mf_ratio / 20.0 * 15, 15)  # 主力占比分 (15)
        mf_net = f.get("main_force_net", 0)
        score += min(mf_net / 100_000_000 * 10, 10)  # 主力净额分 (10)

        # 板块 (20分)
        score += max(20 - sector_rank, 0)  # 板块排名分 (20)

        # 趋势 (15分)
        score += 15 if q.get("price", 0) > q.get("prev_close", 0) else 5

        return round(score, 1)

    # ═══════════════════════════════════════════════════════════════
    # 候选池跟踪
    # ═══════════════════════════════════════════════════════════════

    def _tail_track_candidates(self, candidates):
        """跨轮跟踪候选池 — 连续出现加分，消失2轮剔除。"""
        current_codes = {c[0] for c in candidates}

        # 新候选入库
        for code, score, q, snap, f in candidates:
            if code not in self._tail_candidates:
                self._tail_candidates[code] = {
                    "first_seen": self._tail_round,
                    "rounds_present": 0,
                    "scores": [],
                    "missed": 0,
                    "latest_score": 0,
                    "stock_name": q.get("stock_name", ""),
                }
            entry = self._tail_candidates[code]
            entry["rounds_present"] += 1
            entry["scores"].append(score)
            entry["latest_score"] = score
            entry["missed"] = 0

        # 标记缺失
        for code in self._tail_candidates:
            if code not in current_codes:
                self._tail_candidates[code]["missed"] += 1

        # 连续2轮缺失 → 剔除
        stale = [c for c, e in self._tail_candidates.items() if e["missed"] >= 2]
        for c in stale:
            del self._tail_candidates[c]

    # ═══════════════════════════════════════════════════════════════
    # 执行
    # ═══════════════════════════════════════════════════════════════

    def _tail_execute(self):
        """14:50~14:57 终筛 → 仓位检查 → 下单。"""
        if self._tail_executed:
            return

        # 按综合评分排序（出现轮数 + 最新评分）
        ranked = sorted(
            self._tail_candidates.items(),
            key=lambda x: (x[1]["rounds_present"], x[1]["latest_score"]),
            reverse=True,
        )

        # 已持仓代码
        pa = self.paper_account
        held_codes = set(pa.positions.keys()) if pa else set()

        # 全局仓位已满
        total_held = len(held_codes)
        if total_held >= settings.MAX_POSITIONS:
            logger.info(f"引擎3 全局仓位已满 ({total_held}/{settings.MAX_POSITIONS})，跳过")
            self._tail_executed = True
            return

        # 引擎3 可开仓数
        tail_held = len(self._tail_positions & held_codes)
        remaining = settings.MAX_TAIL_POSITIONS - tail_held

        if remaining <= 0:
            logger.info("引擎3 仓位已满，跳过执行")
            self._tail_executed = True
            self._alert_private("🔍 引擎3 仓位已满\n   本轮跳过执行")
            return

        executed = []
        for code, entry in ranked:
            if len(executed) >= remaining:
                break
            if code in held_codes:
                continue

            snap = getattr(self, "_market_snapshot", {}).get(code, {})
            price = snap.get("price", 0)
            if price <= 0:
                continue

            name = entry.get("stock_name", code)
            score = entry["latest_score"]

            # 计算买入量：均分可用仓位
            try:
                cash_per_slot = pa.portfolio.cash / remaining if remaining > 0 else 0
                target_amount = min(cash_per_slot * 0.9, pa.portfolio.cash * 0.2)
                volume = int(target_amount / price / 100) * 100
                if volume < 100:
                    continue

                result = pa.buy(
                    code=code,
                    name=name,
                    price=price,
                    volume=volume,
                    source="tail_close",
                )
                if result and result.success:
                    self._tail_positions.add(code)
                    executed.append(f"{code} {name} {volume}股 评分:{score:.0f}")
                    logger.info(f"引擎3 买入: {code} {name} {volume}股 @{price}")
            except Exception as e:
                logger.warning(f"引擎3 买入失败 {code}: {e}")

        self._tail_executed = True
        if executed:
            self._alert_private("🔍 引擎3 买入\n   " + "\n   ".join(executed))
        else:
            self._alert_private("🔍 引擎3 无满足条件的标的\n   本轮未执行买入")
            logger.info("引擎3 无满足条件的标的")

    # ═══════════════════════════════════════════════════════════════
    # 次日卖出（集成到 watcher 早盘 tick）
    # ═══════════════════════════════════════════════════════════════

    def _check_tail_close_positions(self, prices: dict[str, float]):
        """次日早盘检查引擎3 持仓，按高开/平开/低开策略卖出。

        由 _scan() 在 9:30~14:00 时段调用。
        """
        now = datetime.now().time()
        if now < dt_time(9, 30) or now > dt_time(14, 0):
            return

        # 懒加载：次日早上 _tail_activate() 未调用，从 DB 恢复持仓
        if not self._tail_positions:
            self._tail_restore_positions()
        if not self._tail_positions:
            return

        pa = self.paper_account
        if not pa or not pa.positions:
            return

        for code in list(self._tail_positions):
            pos = pa.positions.get(code)
            if not pos or pos.available_volume <= 0:
                continue

            price = prices.get(code)
            if not price or price <= 0:
                continue

            entry_price = pos.avg_cost
            chg_pct = (price - entry_price) / entry_price * 100
            prev_close = pos.avg_cost  # fallback
            snap = getattr(self, "_market_snapshot", {}).get(code, {})
            if snap and snap.get("preClose"):
                prev_close = snap["preClose"]
            open_chg = (price - prev_close) / prev_close * 100 if prev_close else 0

            # 止损：亏损超 2%
            if chg_pct <= -2.0:
                self._tail_sell(code, "止损 -2%", price)
                continue

            # 高开 (> +1%) — 全部卖出锁利
            if open_chg > 1.0:
                self._tail_sell(code, "高开锁利", price)
                continue

            # 平开 (±1%)
            if -1.0 <= open_chg <= 1.0:
                if now >= dt_time(9, 45):
                    if chg_pct >= 2.0:
                        self._tail_sell(code, "平开达标 +2%", price)
                    elif chg_pct <= -1.5:
                        self._tail_sell(code, "平开止损 -1.5%", price)
                continue

            # 低开 (< -1%)
            if open_chg < -1.0:
                if now >= dt_time(9, 35):
                    if price < prev_close:
                        self._tail_sell(code, "低开不修复", price)
                continue

        # 14:00 强制清仓
        if now >= dt_time(14, 0):
            for code in list(self._tail_positions):
                pos = pa.positions.get(code)
                if pos and pos.available_volume > 0:
                    self._tail_sell(code, "14:00强制清仓", prices.get(code, pos.avg_cost))

    def _tail_sell(self, code: str, reason: str, price: float):
        """执行卖出 + 推送通知 + 从跟踪集移除。"""
        pa = self.paper_account
        if not pa:
            return
        pos = pa.positions.get(code)
        if not pos or pos.available_volume <= 0:
            self._tail_positions.discard(code)
            return
        try:
            result = pa.sell(code, price, reason)
            if result and result.success:
                self._tail_positions.discard(code)
                logger.info(f"引擎3 卖出: {code} @ {price} ({reason})")
                self._alert_private(f"🔍 引擎3 卖出\n   {code}\n   @ {price:.2f}  {reason}")
        except Exception as e:
            logger.warning(f"引擎3 卖出失败 {code}: {e}")
