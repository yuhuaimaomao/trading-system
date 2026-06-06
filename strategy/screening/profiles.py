"""ProfileBuilder — 将 StockScore 富化为 StockProfile 画像"""

import json
import logging
import sqlite3
from typing import Optional

from stock.signals import StockProfile, StockScore
from system.config import settings

logger = logging.getLogger(__name__)


class ProfileBuilder:
    """富化候选票：多日序列 + 板块 + RPS + 共振 + 电报"""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path or settings.DATABASE_PATH

    def build(
        self,
        stocks: list[StockScore],
        trade_date: str,
        market_state: str = "",
        breadth: Optional[dict] = None,
    ) -> list[StockProfile]:
        if not stocks:
            return []

        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row

            codes = [s.stock_code for s in stocks]
            stock_sectors = self._load_stock_sectors(conn, codes)
            sector_changes = self._load_sector_changes(conn, trade_date)
            sector_hot_map = self._load_sector_hot(conn, trade_date)
            sector_funds = self._load_sector_funds(conn, trade_date)

            profiles = []
            # 批量加载风险数据
            risk_map = self._load_risks(conn, codes, trade_date)

            for s in stocks:
                history = self._load_history(conn, s.stock_code, trade_date, 60)
                snapshot = self._build_snapshot(s, conn, trade_date)
                history_data = self._build_history(s, history)
                rps = self._compute_rps(conn, s.stock_code, trade_date)
                sectors = self._build_sector_ref(
                    s, stock_sectors, sector_changes, sector_hot_map
                )
                resonance = self._build_resonance(sectors, sector_hot_map)
                valuation = self._build_valuation(s, conn, trade_date)
                telegraphs = self._load_telegraphs(conn, s, trade_date)
                indicators = self._calc_indicators(
                    conn, s.stock_code, trade_date, history, snapshot
                )
                risks = risk_map.get(s.stock_code, [])

                profile = StockProfile(
                    code=s.stock_code,
                    name=s.stock_name,
                    trade_date=trade_date,
                    score=s.score,
                    trend_mode=s.trend_mode,
                    scenarios=s.scenarios,
                    tags=s.tags,
                    snapshot=snapshot,
                    history=history_data,
                    rps=rps,
                    sectors=sectors,
                    sector_resonance=resonance,
                    valuation=valuation,
                    market_state=market_state,
                    telegraphs=telegraphs,
                    indicators=indicators,
                    risks=risks,
                )
                profiles.append(profile)

            return profiles
        finally:
            conn.close()

    # ---- 快照 ----------------------------------------------------------------

    def _build_snapshot(
        self,
        s: StockScore,
        conn: sqlite3.Connection,
        trade_date: str,
    ) -> dict:
        row = conn.execute(
            """SELECT price, open, high, low, change_pct, volume_ratio, amplitude,
                      main_force_net, main_force_ratio, small_net,
                      industry, turnover_rate
               FROM stock_basic
               WHERE stock_code=? AND trade_date=?""",
            (s.stock_code, trade_date),
        ).fetchone()
        if not row:
            return {}
        d = dict(row)
        return {
            "price": d.get("price", 0),
            "open": d.get("open", 0),
            "high": d.get("high", 0),
            "low": d.get("low", 0),
            "change_pct": d.get("change_pct", 0),
            "volume_ratio": d.get("volume_ratio", 0),
            "amplitude": d.get("amplitude", 0),
            "main_force_net": d.get("main_force_net", 0),
            "main_force_ratio": d.get("main_force_ratio", 0),
            "small_net": d.get("small_net", 0),
            "industry": d.get("industry", ""),
            "turnover_rate": d.get("turnover_rate", 0),
        }

    # ---- 多日序列 ------------------------------------------------------------

    def _load_history(
        self,
        conn: sqlite3.Connection,
        stock_code: str,
        trade_date: str,
        days: int,
    ) -> list[dict]:
        rows = conn.execute(
            """SELECT trade_date, price, open, high, low, prev_close,
                      change_pct, volume_ratio, ma5, ma10, ma20, volume,
                      main_force_net
               FROM stock_basic
               WHERE stock_code=? AND trade_date < ?
               ORDER BY trade_date DESC LIMIT ?""",
            (stock_code, trade_date, days),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def _build_history(self, s: StockScore, history: list[dict]) -> dict:
        if not history:
            return {
                "ma5": s.ma5,
                "ma10": s.ma10,
                "ma20": s.ma20,
                "consecutive_yang": 0,
                "ma_bull_days": 0,
                "mf_5d_cum": 0,
                "mf_consec_inflow": 0,
                "daily": [],
            }

        returns = [h.get("change_pct") or 0 for h in history[-5:]]
        vol_ratios = [h.get("volume_ratio") or 0 for h in history[-5:]]
        highs = [h.get("high") or 0 for h in history[-20:]]
        lows = [h.get("low") or 0 for h in history[-20:]]

        yang_days = 0
        for h in reversed(history):
            if (h.get("price") or 0) > (h.get("open") or 0):
                yang_days += 1
            else:
                break

        bull_days = 0
        for h in reversed(history):
            if (h.get("ma5") or 0) > (h.get("ma10") or 0) > (h.get("ma20") or 0):
                bull_days += 1
            else:
                break

        mf_cum = sum(h.get("main_force_net") or 0 for h in history[-5:])
        mf_inflow = 0
        for h in reversed(history):
            if (h.get("main_force_net") or 0) > 0:
                mf_inflow += 1
            else:
                break

        daily = []
        for h in history[-10:]:
            daily.append(
                {
                    "date": (h.get("trade_date") or "")[-5:],
                    "open": h.get("open") or 0,
                    "high": h.get("high") or 0,
                    "low": h.get("low") or 0,
                    "close": h.get("price") or 0,
                    "chg": h.get("change_pct") or 0,
                    "vol_ratio": h.get("volume_ratio") or 0,
                    "mf_net": (h.get("main_force_net") or 0) / 10000,
                }
            )

        return {
            "returns_5d": returns,
            "vol_ratios_5d": vol_ratios,
            "high_20d": max(highs) if highs else 0,
            "low_20d": min(lows) if lows else 0,
            "consecutive_yang": yang_days,
            "ma5": s.ma5,
            "ma10": s.ma10,
            "ma20": s.ma20,
            "ma_bull_days": bull_days,
            "mf_5d_cum": mf_cum,
            "mf_consec_inflow": mf_inflow,
            "daily": daily,
        }

    # ---- RPS -----------------------------------------------------------------

    def _compute_rps(
        self,
        conn: sqlite3.Connection,
        stock_code: str,
        trade_date: str,
    ) -> dict:
        """计算 RPS_20 百分位。RPS_60/120 需要更长历史，当前仅 RPS_20 可用。"""
        result = {"rps_20": 0, "rps_60": 0, "rps_120": 0}

        # RPS_20: 从现有 21 天数据中计算
        hist_rows = conn.execute(
            """SELECT trade_date, price FROM stock_basic
            WHERE stock_code=? AND trade_date <= ?
            ORDER BY trade_date DESC LIMIT 21""",
            (stock_code, trade_date),
        ).fetchall()
        if len(hist_rows) < 20:
            return result

        # 该股 20 日涨幅
        new_p, old_p = hist_rows[0][1] or 0, hist_rows[-1][1] or 0
        if old_p <= 0:
            return result
        stock_return = (new_p - old_p) / old_p

        # 全市场股票数
        total = conn.execute(
            "SELECT COUNT(DISTINCT stock_code) FROM stock_basic WHERE trade_date=?",
            (trade_date,),
        ).fetchone()[0]
        if total == 0:
            return result

        # 该股当日涨幅在全市场的排名（近似 RPS_20）
        rank = conn.execute(
            """SELECT COUNT(*) + 1 FROM stock_basic
            WHERE trade_date=? AND change_pct > (
                SELECT change_pct FROM stock_basic
                WHERE stock_code=? AND trade_date=?
            )""",
            (trade_date, stock_code, trade_date),
        ).fetchone()[0]

        rps20 = 1 - (rank - 1) / total
        result["rps_20"] = round(rps20, 4)
        return result

    # ---- 板块 -----------------------------------------------------------------

    def _load_stock_sectors(
        self,
        conn: sqlite3.Connection,
        codes: list[str],
    ) -> dict:
        if not codes:
            return {}
        placeholders = ",".join("?" * len(codes))
        rows = conn.execute(
            f"SELECT stock_code, sector_code FROM sector_stocks "
            f"WHERE stock_code IN ({placeholders})",
            codes,
        ).fetchall()
        result: dict = {}
        for r in rows:
            result.setdefault(r[0], []).append(r[1])
        return result

    def _load_sector_changes(
        self,
        conn: sqlite3.Connection,
        trade_date: str,
    ) -> dict:
        rows = conn.execute(
            """SELECT sector_code, change_percent, sector_name
            FROM sector_industry WHERE trade_date=?
            UNION ALL
            SELECT sector_code, change_percent, sector_name
            FROM sector_concept WHERE trade_date=?""",
            (trade_date, trade_date),
        ).fetchall()
        return {r[0]: {"change_pct": r[1], "name": r[2]} for r in rows}

    def _load_sector_hot(
        self,
        conn: sqlite3.Connection,
        trade_date: str,
    ) -> dict:
        """板块近 5 日上榜 top5 次数"""
        rows = conn.execute(
            """SELECT sector_code, COUNT(*) as cnt
            FROM sector_hot_history
            WHERE trade_date >= date(?, '-5 days') AND trade_date <= ?
              AND rank > 0 AND rank <= 5
            GROUP BY sector_code""",
            (trade_date, trade_date),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _load_sector_funds(
        self,
        conn: sqlite3.Connection,
        trade_date: str,
    ) -> dict:
        rows = conn.execute(
            """SELECT sector_code, main_force_net
            FROM sector_industry WHERE trade_date=?
            UNION ALL
            SELECT sector_code, main_force_net
            FROM sector_concept WHERE trade_date=?""",
            (trade_date, trade_date),
        ).fetchall()
        return {r[0]: r[1] or 0 for r in rows}

    def _build_sector_ref(
        self,
        s: StockScore,
        stock_sectors: dict,
        sector_changes: dict,
        sector_hot_map: dict,
    ) -> list[dict]:
        sectors = stock_sectors.get(s.stock_code, [])
        industry = s.industry or ""

        def _priority(sc: str) -> tuple:
            info = sector_changes.get(sc, {})
            hot_days = sector_hot_map.get(sc, 0)
            chg = abs(info.get("change_pct", 0))
            name = info.get("name", "")
            # 板块名包含行业名 → 行业板块优先级更高
            industry_match = 1 if (industry and industry in name) else 0
            return (-hot_days, -chg, -industry_match)

        result = []
        for sc in sorted(sectors, key=_priority)[:3]:
            info = sector_changes.get(sc, {})
            result.append(
                {
                    "code": sc,
                    "name": info.get("name", ""),
                    "change_pct": info.get("change_pct", 0),
                }
            )
        return result

    def _build_resonance(
        self,
        sector_refs: list[dict],
        sector_hot: dict,
    ) -> dict:
        resonance = {}
        has_any = False
        for ref in sector_refs:
            code = ref["code"]
            days = sector_hot.get(code, 0)
            ok = days >= 1
            resonance[code] = {"hot": ok, "days": days}
            if ok:
                has_any = True
        resonance["overall"] = has_any
        return resonance

    # ---- 估值 -----------------------------------------------------------------

    def _build_valuation(
        self,
        s: StockScore,
        conn: sqlite3.Connection,
        trade_date: str,
    ) -> dict:
        row = conn.execute(
            """SELECT pe_ttm, pb_ratio, total_market_cap,
                      revenue_growth, profit_growth
               FROM stock_basic
               WHERE stock_code=? AND trade_date=?""",
            (s.stock_code, trade_date),
        ).fetchone()
        if not row:
            return {}
        mcap = (row["total_market_cap"] or 0) / 1_0000_0000
        return {
            "pe_ttm": row["pe_ttm"] or 0,
            "pb": row["pb_ratio"] or 0,
            "mcap_yi": round(mcap, 0),
            "revenue_growth": row["revenue_growth"] or 0,
            "profit_growth": row["profit_growth"] or 0,
        }

    # ---- 电报 -----------------------------------------------------------------

    def _load_telegraphs(
        self,
        conn: sqlite3.Connection,
        s: StockScore,
        trade_date: str,
    ) -> list[dict]:
        rows = conn.execute(
            """SELECT ctime, ai_summary, ai_sentiment, ai_stocks
            FROM cls_telegraph
            WHERE trade_date=? AND ai_status='done'
            ORDER BY ctime""",
            (trade_date,),
        ).fetchall()
        result = []
        for r in rows:
            try:
                stocks_list = json.loads(r["ai_stocks"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            matched = any(
                item.get("code", "") == s.stock_code
                for item in stocks_list
                if isinstance(item, dict)
            )
            if matched:
                result.append(
                    {
                        "time": r["ctime"],
                        "summary": r["ai_summary"] or "",
                        "sentiment": r["ai_sentiment"] or "",
                    }
                )
        return result[:5]

    # ---- 技术指标 -------------------------------------------------------------

    def _calc_indicators(
        self,
        conn: sqlite3.Connection,
        stock_code: str,
        trade_date: str,
        history: list[dict],
        snapshot: dict,
    ) -> dict:
        """从 stock_indicators 表读取当前 + 5日前指标值；形态检测实时计算"""
        from stock.indicators import (
            calc_macd_series,
            detect_divergence,
            detect_macd_cross,
        )

        # 1. 从 stock_indicators 读取当前值和 5 日前值
        today_row = conn.execute(
            """SELECT macd_dif, macd_dea, macd_bar,
                      rsi6, rsi12, rsi24, kdj_k, kdj_d, kdj_j,
                      bb_upper, bb_mid, bb_lower, bb_width, bb_pct_b
               FROM stock_indicators
               WHERE stock_code=? AND trade_date=?""",
            (stock_code, trade_date),
        ).fetchone()

        # 5 日前交易日
        prev_date = history[-5]["trade_date"] if len(history) >= 5 else None
        prev_row = None
        if prev_date:
            prev_row = conn.execute(
                """SELECT macd_dif, macd_dea, macd_bar,
                          rsi6, rsi12, rsi24, kdj_k
                   FROM stock_indicators
                   WHERE stock_code=? AND trade_date=?""",
                (stock_code, prev_date),
            ).fetchone()

        # 2. 形态检测：从历史+快照构建完整序列
        closes = [h.get("close", h.get("price", 0)) or 0 for h in history]
        if snapshot:
            closes.append(snapshot.get("price", 0))
        series = calc_macd_series(closes)
        crosses = detect_macd_cross(series["dif"], series["dea"])
        divergences = detect_divergence(closes, series["dif"])
        patterns = crosses + divergences

        # 3. 组装结果
        if today_row:
            result = {
                "macd": {
                    "dif": today_row["macd_dif"],
                    "dea": today_row["macd_dea"],
                    "bar": today_row["macd_bar"],
                },
                "rsi6": today_row["rsi6"],
                "rsi12": today_row["rsi12"],
                "rsi24": today_row["rsi24"],
                "kdj": {
                    "k": today_row["kdj_k"],
                    "d": today_row["kdj_d"],
                    "j": today_row["kdj_j"],
                },
                "boll": {
                    "upper": today_row["bb_upper"] or 0,
                    "mid": today_row["bb_mid"] or 0,
                    "lower": today_row["bb_lower"] or 0,
                    "width": today_row["bb_width"] or 0,
                    "pct_b": today_row["bb_pct_b"] or 0,
                },
                "patterns": patterns,
                "trend_5d": {},
            }
        else:
            result = {
                "macd": {},
                "rsi6": 0,
                "rsi12": 0,
                "rsi24": 0,
                "kdj": {},
                "boll": {},
                "patterns": patterns,
                "trend_5d": {},
            }

        if today_row and prev_row:
            result["trend_5d"] = {
                "macd_dif": f"{prev_row['macd_dif']:.2f}→{today_row['macd_dif']:.2f}",
                "rsi6": f"{prev_row['rsi6']:.1f}→{today_row['rsi6']:.1f}",
                "rsi12": f"{prev_row['rsi12']:.1f}→{today_row['rsi12']:.1f}",
                "kdj_k": f"{prev_row['kdj_k']:.1f}→{today_row['kdj_k']:.1f}",
                "macd_bar": f"{prev_row['macd_bar']:.2f}→{today_row['macd_bar']:.2f}",
            }

        return result

    # ---- 风险扫描 -------------------------------------------------------------

    def _load_risks(
        self,
        conn: sqlite3.Connection,
        codes: list[str],
        trade_date: str,
    ) -> dict[str, list[dict]]:
        """批量加载候选股票的风险信息"""
        if not codes:
            return {}
        result: dict[str, list[dict]] = {c: [] for c in codes}

        # 1. 监管函：近 90 天有风险等级的
        placeholders = ",".join("?" * len(codes))
        reg_rows = conn.execute(
            f"""SELECT stock_code, risk_level, risk_type, title, trade_date
            FROM regulatory_letter
            WHERE stock_code IN ({placeholders})
              AND trade_date >= date(?, '-90 days')
              AND risk_level >= 2
            ORDER BY trade_date DESC""",
            codes + [trade_date],
        ).fetchall()
        for r in reg_rows:
            result.setdefault(r["stock_code"], []).append(
                {
                    "type": "监管函",
                    "level": r["risk_level"],
                    "risk_type": r["risk_type"] or "",
                    "title": (r["title"] or "")[:80],
                    "date": r["trade_date"],
                }
            )

        # 2. 电报：近 3 天利空消息
        tel_rows = conn.execute(
            """SELECT ctime, ai_summary, ai_sentiment, ai_stocks
            FROM cls_telegraph
            WHERE trade_date >= date(?, '-3 days')
              AND ai_status = 'done'
              AND ai_sentiment IN ('利空', '负面')
            ORDER BY ctime""",
            (trade_date,),
        ).fetchall()
        for r in tel_rows:
            try:
                stocks_list = json.loads(r["ai_stocks"] or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            mentioned_codes = {
                item.get("code", "") for item in stocks_list if isinstance(item, dict)
            }
            for code in mentioned_codes & set(codes):
                result.setdefault(code, []).append(
                    {
                        "type": "电报利空",
                        "level": 2,
                        "risk_type": "利空消息",
                        "title": (r["ai_summary"] or "")[:80],
                        "date": r["ctime"][:10] if r["ctime"] else "",
                    }
                )

        # 3. 炸板池：今日炸板未回封的股票
        zhapa_rows = conn.execute(
            f"""SELECT stock_code FROM limit_pool
            WHERE trade_date=? AND pool_type='炸板'
              AND stock_code IN ({placeholders})""",
            [trade_date] + codes,
        ).fetchall()
        zhapa_codes = {r[0] for r in zhapa_rows}
        for code in zhapa_codes:
            result.setdefault(code, []).append(
                {
                    "type": "炸板未回封",
                    "level": 2,
                    "risk_type": "炸板",
                    "title": "今日触及涨停但未封板，高位抛压重，需区分试盘还是出货",
                    "date": trade_date,
                }
            )

        return result
