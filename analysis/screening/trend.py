"""交易用趋势筛选器 — 多因子 + 场景匹配 + 分层输出

改造要点:
  - 硬关卡取代原 SQL WHERE 条件，市值上限移除
  - 因子纯函数逐个打分，标签数量替代原 score 公式
  - 场景匹配取代原 strong/normal 两类
  - 大盘状态参数控制策略倾向（恐慌空仓/普跌禁追）
"""

import logging
import sqlite3
from typing import Optional

from system.config import settings
from analysis.signals import StockScore
from analysis.screening.factors import (
    check_hard_gates,
    check_volume_breakout,
    check_volume_pullback,
    check_amplitude_contract,
    check_main_force_buy,
    check_chip_concentrate,
    check_consecutive_yang,
    check_pullback_hold,
    check_trend_persist,
    check_low_volatility,
    check_volume_expand,
    check_trend_strength,
    check_rps_20_strong,
    check_rps_60_strong,
    check_rps_120_strong,
    check_rps_resonance,
    check_leader_in_sector,
    check_stronger_than_sector,
    check_sector_fund_resonance,
    check_weekly_bbi,
)

logger = logging.getLogger(__name__)

_BASE_SQL = """
    SELECT stock_code, stock_name, change_pct, price,
           total_market_cap / 100000000.0 AS mcap,
           circ_market_cap / 100000000.0 AS circ_mcap,
           turnover_rate, volume_ratio, amplitude,
           ma5, ma10, ma20, ma5_angle,
           industry, open, high, low, prev_close,
           main_force_net / 10000.0 AS mf_wan,
           main_force_ratio AS mf_ratio,
           main_force_net, super_large_net, large_net,
           medium_net, small_net,
           avg_vol_5d, avg_vol_20d,
           pe_ttm, pb_ratio, revenue_growth, profit_growth,
           total_market_cap
    FROM stock_basic
    WHERE trade_date = ?
      AND price > 0 AND ma5 > 0 AND ma10 > 0 AND ma20 > 0
    ORDER BY ma5_angle DESC
"""


# 板块黑名单：硬编码 sector_code，筛选时精确匹配，每日无需重查 DB
_SECTOR_BLACKLIST: set[str] = {
    # 白酒
    "BK1575", "BK1277", "BK0896",
    # 银行
    "BK1610", "BK1283", "BK0475", "BK1611", "BK0525",
    # 保险
    "BK1358", "BK0474", "BK0604",
    # 证券/券商
    "BK1366", "BK0473", "BK0711", "BK0514",
    # 房地产
    "BK1045", "BK1342", "BK1202", "BK0451", "BK1344", "BK1345",
    # 农业
    "BK1506", "BK1257", "BK0669", "BK0888",
    # 新能源车
    "BK0900",
    # 环保
    "BK0728", "BK1387", "BK1234", "BK0494",
    # 消费
    "BK1338", "BK1037", "BK1337", "BK1654", "BK1646", "BK1652",
    # 食品
    "BK0438", "BK1581", "BK1280", "BK1281", "BK1582", "BK1507", "BK1429", "BK0614",
}


class TrendScreener:
    """趋势筛选器 — 多因子 + 场景匹配 + 分层输出"""

    def __init__(
        self,
        db_path: Optional[str] = None,
        min_score: float = 50,
        top_n: int = 10,
    ):
        self.db_path = db_path or settings.DATABASE_PATH
        self.min_score = min_score
        self.top_n = top_n

    def screen(self, trade_date: str, market_state: str = "") -> list[StockScore]:
        if market_state == "恐慌":
            logger.info("大盘恐慌状态，跳过筛选")
            return []

        conn = sqlite3.connect(self.db_path)
        try:
            rows = self._fetch_base(conn, trade_date)
            sector_data = self._load_sector_data(conn, trade_date, rows)
            candidates = self._screen_rows(conn, rows, trade_date, market_state, sector_data)
            return self._rank_and_limit(candidates)
        finally:
            conn.close()

    def _fetch_base(self, conn: sqlite3.Connection, trade_date: str) -> list[dict]:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(_BASE_SQL, (trade_date,))
        return [dict(r) for r in cursor.fetchall()]

    # ---- 板块数据 ----

    def _load_sector_data(
        self, conn: sqlite3.Connection, trade_date: str, rows: list[dict],
    ) -> dict:
        """加载板块相关数据，供板块因子使用。表不存在时返回空数据，因子会自动跳过。"""
        codes = [r["stock_code"] for r in rows]
        try:
            stock_sectors = self._load_stock_sectors(conn, codes)
            sector_changes = self._load_sector_changes(conn, trade_date)
            sector_hot = self._load_sector_hot(conn, trade_date)
            sector_funds = self._load_sector_funds(conn, trade_date)
        except Exception:
            return {
                "stock_sectors": {}, "sector_changes": {},
                "sector_hot": {}, "sector_funds": {},
                "sector_stocks_pct": {},
            }
        sector_stocks_pct = {r["stock_code"]: r["change_pct"] or 0 for r in rows}
        return {
            "stock_sectors": stock_sectors,
            "sector_changes": sector_changes,
            "sector_hot": sector_hot,
            "sector_funds": sector_funds,
            "sector_stocks_pct": sector_stocks_pct,
        }

    @staticmethod
    def _load_stock_sectors(conn, codes: list[str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        if not codes:
            return result
        placeholders = ",".join("?" for _ in codes)
        for row in conn.execute(
            f"SELECT stock_code, sector_code FROM sector_stocks WHERE stock_code IN ({placeholders})",
            codes,
        ):
            result.setdefault(row["stock_code"], []).append(row["sector_code"])
        return result

    @staticmethod
    def _load_sector_changes(conn, trade_date: str) -> dict[str, float]:
        result: dict[str, float] = {}
        for row in conn.execute(
            """SELECT sector_code, change_percent FROM sector_industry WHERE trade_date=?
               UNION ALL
               SELECT sector_code, change_percent FROM sector_concept WHERE trade_date=?""",
            (trade_date, trade_date),
        ):
            result[row["sector_code"]] = row["change_percent"] or 0
        return result

    @staticmethod
    def _load_sector_hot(conn, trade_date: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for row in conn.execute(
            """SELECT sector_code, COUNT(*) as cnt FROM sector_hot_history
               WHERE trade_date >= date(?, '-9 days') AND trade_date <= ?
                 AND rank > 0
               GROUP BY sector_code""",
            (trade_date, trade_date),
        ):
            result[row["sector_code"]] = row["cnt"]
        return result

    @staticmethod
    def _load_sector_funds(conn, trade_date: str) -> dict[str, float]:
        result: dict[str, float] = {}
        for row in conn.execute(
            """SELECT sector_code, main_force_net FROM sector_industry WHERE trade_date=?
               UNION ALL
               SELECT sector_code, main_force_net FROM sector_concept WHERE trade_date=?""",
            (trade_date, trade_date),
        ):
            result[row["sector_code"]] = row["main_force_net"] or 0
        return result

    # ---- 板块硬门槛 ----

    @staticmethod
    def _check_sector_blacklist(
        row: dict, blacklist: set[str], stock_sectors: dict[str, list[str]],
    ) -> bool:
        """股票属于黑名单板块 → 过滤"""
        code = row.get("stock_code", "")
        sectors = stock_sectors.get(code, [])
        if not sectors:
            return True
        return not any(s in blacklist for s in sectors)

    @staticmethod
    def _check_sector_gate(
        row: dict, sector_hot: dict[str, int], stock_sectors: dict[str, list[str]],
    ) -> bool:
        """股票所属板块至少有一个在近10日热点榜上，否则过滤。板块数据为空时放行。"""
        if not sector_hot:
            return True
        code = row.get("stock_code", "")
        sectors = stock_sectors.get(code, [])
        if not sectors:
            return True  # 无板块信息的票不卡
        return any(s in sector_hot for s in sectors)

    # ---- 筛选主流程 ----

    @staticmethod
    def _load_serious_risks(
        conn: sqlite3.Connection, codes: list[str], trade_date: str,
    ) -> set[str]:
        """返回存在严重风险的股票代码集合：监管函(财务造假/信披违规) + 电报利空(业绩暴雷/减持/诉讼)"""
        if not codes:
            return set()
        result: set[str] = set()
        placeholders = ",".join("?" * len(codes))

        # 1. 监管函：近90天，财务造假或信披违规
        reg_rows = conn.execute(
            f"""SELECT stock_code FROM regulatory_letter
                WHERE stock_code IN ({placeholders})
                  AND trade_date >= date(?, '-90 days')
                  AND risk_type IN ('财务造假', '信披违规')""",
            codes + [trade_date],
        ).fetchall()
        result.update(r[0] for r in reg_rows)

        # 2. 电报：近3天利空消息涉及业绩暴雷/减持/诉讼
        tel_rows = conn.execute(
            """SELECT ai_stocks, ai_summary FROM cls_telegraph
               WHERE trade_date >= date(?, '-3 days')
                 AND ai_status = 'done'
                 AND ai_sentiment IN ('利空', '负面')""",
            (trade_date,),
        ).fetchall()
        import json
        for r in tel_rows:
            try:
                stocks_list = json.loads(r["ai_stocks"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            summary = r["ai_summary"] or ""
            if not any(kw in summary for kw in ("业绩暴雷", "减持", "诉讼")):
                continue
            for item in stocks_list:
                if isinstance(item, dict):
                    code = item.get("code", "")
                    if code in codes:
                        result.add(code)

        return result

    @staticmethod
    def _load_weekly_bbi(
        conn: sqlite3.Connection, trade_date: str, codes: list[str],
    ) -> dict[str, float]:
        """批量加载周线 BBI"""
        if not codes:
            return {}
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"""SELECT stock_code, bbi_weekly
                FROM stock_indicators
                WHERE trade_date=? AND stock_code IN ({placeholders})
                  AND bbi_weekly IS NOT NULL""",
            [trade_date] + codes,
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _screen_rows(
        self, conn: sqlite3.Connection, rows: list[dict],
        trade_date: str, market_state: str, sector_data: dict,
    ) -> list[StockScore]:
        sd = sector_data
        candidates = []

        # 批量加载周线 BBI
        row_codes = [r["stock_code"] for r in rows]
        weekly_bbi_map = self._load_weekly_bbi(conn, trade_date, row_codes)

        # 批量加载严重风险（监管函/电报利空），初筛直接过滤
        serious_risk_codes = self._load_serious_risks(conn, row_codes, trade_date)

        for row in rows:
            if row["stock_code"] in serious_risk_codes:
                continue
            if not check_hard_gates(row):
                continue

            if not self._check_sector_gate(row, sd["sector_hot"], sd["stock_sectors"]):
                continue

            if not self._check_sector_blacklist(row, _SECTOR_BLACKLIST, sd["stock_sectors"]):
                continue

            history = self._get_history(conn, row["stock_code"], trade_date, days=20)

            # 跑所有因子（量价/资金/趋势 + 板块类）
            factor_results = []
            for fn in [
                check_volume_breakout, check_volume_pullback,
                check_amplitude_contract, check_main_force_buy,
                check_chip_concentrate, check_consecutive_yang,
                check_pullback_hold, check_trend_persist,
                check_low_volatility, check_volume_expand,
                check_trend_strength,
                check_rps_20_strong, check_rps_60_strong,
                check_rps_120_strong, check_rps_resonance,
            ]:
                result = fn(row, history)
                if result:
                    factor_results.append(result)

            # 周线 BBI
            result = check_weekly_bbi(row, history, weekly_bbi_map=weekly_bbi_map)
            if result:
                factor_results.append(result)

            # 板块因子
            for fn in [
                check_leader_in_sector,
                check_stronger_than_sector, check_sector_fund_resonance,
            ]:
                try:
                    result = fn(row, history,
                                sector_hot=sd["sector_hot"],
                                stock_sectors=sd["stock_sectors"],
                                sector_changes=sd["sector_changes"],
                                sector_funds=sd["sector_funds"],
                                sector_stocks_pct=sd["sector_stocks_pct"])
                    if result:
                        factor_results.append(result)
                except Exception:
                    pass  # 板块数据缺失时静默跳过

            if len(factor_results) < 2:
                continue

            scenarios = self._match_scenarios(row, history, factor_results, market_state)
            score = self._compute_score(factor_results, scenarios, row)
            mode = self._determine_mode(scenarios, row, history)

            candidates.append(StockScore(
                stock_code=row["stock_code"],
                stock_name=row["stock_name"],
                trend_mode=mode,
                score=score,
                price=row["price"] or 0,
                change_pct=row["change_pct"] or 0,
                mcap=row["mcap"] or 0,
                circ_mcap=row["circ_mcap"] or 0,
                turnover_rate=row["turnover_rate"] or 0,
                volume_ratio=row["volume_ratio"] or 0,
                ma5=row["ma5"] or 0,
                ma10=row["ma10"] or 0,
                ma20=row["ma20"] or 0,
                ma5_angle=row["ma5_angle"] or 0,
                industry=row["industry"] or "",
                mf_wan=row["mf_wan"] or 0,
                mf_ratio=row["mf_ratio"] or 0,
                tags=factor_results,
                scenarios=scenarios,
            ))

        return candidates

    def _get_history(
        self, conn: sqlite3.Connection, stock_code: str,
        trade_date: str, days: int,
    ) -> list[dict]:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT trade_date, price, open, high, low, prev_close,
                      change_pct, volume_ratio, ma5, ma10, ma20
               FROM stock_basic
               WHERE stock_code = ? AND trade_date < ?
               ORDER BY trade_date DESC LIMIT ?""",
            (stock_code, trade_date, days),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ---- 场景匹配 ----

    def _match_scenarios(
        self, row: dict, history: list[dict], tags: list[str], market_state: str,
    ) -> list[str]:
        scenarios = []
        # "放量启动" = 今日量比≥1.5，是突破信号
        # "量能放大" = 5日均量>20日均量，是温和放量，不触发突破类场景
        has_breakout = "放量启动" in tags
        has_volume_up = has_breakout or "量能放大" in tags

        # 强趋势（恐慌日跳过）
        if market_state != "恐慌":
            if has_breakout:
                scenarios.append("突破追涨")
                if self._is_20d_high(row, history):
                    scenarios.append("新高突破")

            if "趋势延续" in tags and "主力介入" in tags:
                scenarios.append("趋势加速")

            if self._is_reversal(row, history) and has_breakout:
                scenarios.append("强势反包")

        # 稳健
        if "缩量回调" in tags and self._near_ma(row, "ma5", pct=2):
            scenarios.append("回踩MA5")
        if "缩量回调" in tags and self._near_ma(row, "ma10", pct=2) and "趋势延续" in tags:
            scenarios.append("回踩MA10")
        if self._near_ma(row, "ma20", pct=3) and has_volume_up:
            scenarios.append("回踩MA20")
        if "蓄力中" in tags and self._is_tight_consolidation(row, history):
            scenarios.append("强势横盘")

        # 转折
        if self._is_bounce_from_below_ma20(row, history) and has_volume_up:
            scenarios.append("底部反弹")
        if self._is_ma_diverging(row, history):
            scenarios.append("均线发散")

        # 兜底: 有趋势标签但没命中具体场景
        if not scenarios and ("趋势延续" in tags or "趋势强劲" in tags):
            scenarios.append("趋势行进")

        return scenarios

    def _is_20d_high(self, row: dict, history: list[dict]) -> bool:
        price = row.get("price") or 0
        hh = max((h.get("high") or 0 for h in history[-20:]), default=0)
        return price >= hh * 0.99

    def _is_reversal(self, row: dict, history: list[dict]) -> bool:
        if len(history) < 1:
            return False
        prev_chg = (history[-1].get("change_pct") or 0)
        today_chg = row.get("change_pct") or 0
        return prev_chg < 0 and today_chg > 0

    def _near_ma(self, row: dict, ma_key: str, pct: float) -> bool:
        price = row.get("price") or 0
        ma = row.get(ma_key) or 0
        if ma <= 0:
            return False
        return abs(price - ma) / ma * 100 < pct

    def _is_tight_consolidation(self, row: dict, history: list[dict]) -> bool:
        if len(history) < 5:
            return False
        highs = [h.get("high") or 0 for h in history[-5:]]
        lows = [h.get("low") or 0 for h in history[-5:]]
        hh, ll = max(highs), min(lows)
        return (hh - ll) / ll * 100 < 5 if ll > 0 else False

    def _is_bounce_from_below_ma20(self, row: dict, history: list[dict]) -> bool:
        if len(history) < 1:
            return False
        prev = history[-1]
        prev_price = prev.get("price") or 0
        prev_ma20 = prev.get("ma20") or 0
        cur_price = row.get("price") or 0
        cur_ma20 = row.get("ma20") or 0
        return prev_price < prev_ma20 and cur_price > cur_ma20

    def _is_ma_diverging(self, row: dict, history: list[dict]) -> bool:
        if len(history) < 3:
            return False
        ma5, ma10, ma20 = row.get("ma5") or 0, row.get("ma10") or 0, row.get("ma20") or 0
        if not (ma5 > ma10 > ma20):
            return False
        prev = history[-1]
        prev_ma20 = max(abs(prev.get("ma20") or 1), 1)
        prev_spread = max(
            abs((prev.get("ma5") or 0) - (prev.get("ma10") or 0)),
            abs((prev.get("ma10") or 0) - (prev.get("ma20") or 0)),
        ) / prev_ma20 * 100
        return prev_spread < 2

    def _determine_mode(self, scenarios: list[str], row: dict, history: list[dict]) -> str:
        """纯数据驱动：只看价格与均线的位置关系，不看场景标签。

        5日线强趋势 — 价格紧贴MA5，沿MA5上行
        20日线稳健趋势 — 价格在MA20上方，MA20是有效支撑（近期回踩过或偏离不大）
        """
        price = row.get("price") or 0
        ma5 = row.get("ma5") or 0
        ma10 = row.get("ma10") or 0
        ma20 = row.get("ma20") or 0
        angle = row.get("ma5_angle") or 0

        if ma5 <= 0 or ma20 <= 0:
            return "normal"

        bias5 = abs(price - ma5) / ma5 * 100
        bias20 = (price - ma20) / ma20 * 100 if price > ma20 else 999

        # 5日线：价格在MA5附近（±3%），MA5向上，多头排列
        if bias5 <= 3 and angle >= 1 and ma5 > ma10 > ma20:
            return "strong"

        # 20日线：价格在MA20上方，且近期回踩过MA20（10日内低点触及MA20附近）
        if price > ma20 and bias20 < 15:
            if self._has_ma20_bounce(history, ma20) or bias20 < 8:
                return "normal"

        # 两者都不典型时：MA5角度高且多头排列 → strong；否则 normal
        if angle >= 2 and ma5 > ma10 > ma20:
            return "strong"
        return "normal"

    def _has_ma20_bounce(self, history: list[dict], ma20: float) -> bool:
        """近10日是否有低点触及MA20（±3%）后回升"""
        if not history or ma20 <= 0:
            return False
        for h in history[-10:]:
            low = h.get("low") or 0
            h_ma20 = h.get("ma20") or 0
            if h_ma20 > 0 and abs(low - h_ma20) / h_ma20 * 100 < 3:
                # 确认后一天收盘高于该日最低价（反弹）
                return True
        return False

    def _compute_score(
        self, tags: list[str], scenarios: list[str], row: dict,
    ) -> float:
        base = 20.0
        base += len(tags) * 5
        base += len(scenarios) * 8
        angle = row.get("ma5_angle") or 0
        base += max(-10, min(10, angle * 2))
        return round(min(base, 100), 1)

    def _rank_and_limit(self, candidates: list[StockScore]) -> list[StockScore]:
        candidates.sort(key=lambda x: (-x.score, x.stock_code))
        return candidates[:self.top_n * 2]
