# -*- coding: utf-8 -*-
"""复盘推荐盯盘 — 与趋势信号同一逻辑：盘中持续盯买入区间，进入/离开/再进入可重复提醒。

数据来源: stock_tracker (source='复盘')
买入区间: 基于均线支撑位动态计算
  - MA10 > MA20 → MA10 支撑买入
  - 否则 → MA20 回踩买入
"""

import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)


class ReviewPickMonitor:
    """复盘推荐监控器。

    用法:
        monitor = ReviewPickMonitor(db_path)
        monitor.load_picks()          # 开盘一次性加载
        for each scan:
            msgs = monitor.check(prices, market_state, sector_data)
    """

    def __init__(self, db_path: str, telegram_bot=None):
        self.db_path = db_path
        self._picks: dict[str, dict] = {}  # code → {name, ma5, ma10, ma20, ...}
        self._loaded = False

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load_picks(self):
        """开盘一次性加载复盘推荐 + 均线参考位。"""
        rows = self._load_from_tracker()
        if not rows:
            logger.info("无复盘推荐标的")
            self._loaded = True
            return

        self._fill_ma_levels(rows)

        for r in rows:
            code = r["stock_code"]
            self._picks[code] = {
                "name": r.get("stock_name", code),
                "ma5": r.get("ma5", 0) or 0,
                "ma10": r.get("ma10", 0) or 0,
                "ma20": r.get("ma20", 0) or 0,
                "stop_loss": r.get("stop_loss", 0) or 0,
                "target_price": r.get("target_price", 0) or 0,
                "industry": r.get("industry", ""),
            }

        self._loaded = True
        logger.info(f"复盘盯盘加载: {len(self._picks)} 只标的")

    def _load_from_tracker(self) -> list[dict]:
        try:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM stock_tracker
                   WHERE push_date = (
                       SELECT MAX(push_date) FROM stock_tracker WHERE source='复盘'
                   )"""
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"加载复盘推荐失败: {e}")
            return []

    def _fill_ma_levels(self, picks: list[dict]):
        """从 stock_basic 补充 MA 值和行业。"""
        codes = [p["stock_code"] for p in picks]
        if not codes:
            return
        try:
            conn = self._get_conn()
            placeholders = ",".join("?" for _ in codes)
            rows = conn.execute(
                f"""SELECT stock_code, ma5, ma10, ma20, price as prev_close, industry
                    FROM stock_basic
                    WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)
                    AND stock_code IN ({placeholders})""",
                codes,
            ).fetchall()
            conn.close()
            ref_map = {row[0]: row for row in rows}
            for p in picks:
                ref = ref_map.get(p["stock_code"])
                if ref:
                    p["ma5"] = ref[1]
                    p["ma10"] = ref[2]
                    p["ma20"] = ref[3]
                    p["prev_close"] = ref[4]
                    p["industry"] = ref[5] or ""
        except Exception as e:
            logger.warning(f"加载均线参考位失败: {e}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def is_loaded(self) -> bool:
        return self._loaded

    def get_codes(self) -> list[str]:
        return list(self._picks.keys())

    def get_pick(self, code: str) -> Optional[dict]:
        return self._picks.get(code)

    def get_buy_zone(self, code: str) -> tuple[float, float]:
        """返回买入区间 (buy_min, buy_max)，无有效区间返回 (0, 0)。

        规则: MA10 在 MA20 上方 → MA10 支撑，否则 → MA20 回踩。
        """
        pick = self._picks.get(code)
        if not pick:
            return (0, 0)
        ma10, ma20 = pick.get("ma10", 0) or 0, pick.get("ma20", 0) or 0

        if ma10 > 0 and ma10 > ma20:
            return (round(ma10 * 0.99, 2), round(ma10 * 1.01, 2))
        if ma20 > 0:
            return (round(ma20 * 0.99, 2), round(ma20 * 1.02, 2))
        return (0, 0)

    # ------------------------------------------------------------------
    # 开盘参考
    # ------------------------------------------------------------------

    def build_opening_reference(self, prices: dict[str, float],
                                market_state: dict = None,
                                sector_data: dict[str, float] = None) -> Optional[str]:
        """生成开盘买入参考（只在第一轮调用）。"""
        if not self._picks:
            return None

        lines: list[str] = []
        for code, pick in self._picks.items():
            price = prices.get(code)
            if price is None:
                continue

            buy_min, buy_max = self.get_buy_zone(code)
            if buy_min <= 0:
                continue

            # 判断当前状态
            if price >= buy_min:
                status = "在买入区" if price <= buy_max else "高于买入区"
            else:
                status = "低于买入区"

            sl = pick.get("stop_loss", 0) or 0
            tp = pick.get("target_price", 0) or 0
            industry = pick.get("industry", "")
            sector_chg = sector_data.get(industry) if sector_data else None
            sector_str = f"板块{sector_chg:+.1f}%" if sector_chg is not None else ""

            line = f"{code} {pick['name']} 现价{price:.2f} | {status}"
            line += f" | 买入{buy_min:.2f}-{buy_max:.2f}"
            if sl > 0:
                line += f" | 止损{sl:.2f}"
            if tp > 0:
                line += f" | 目标{tp:.2f}"
            if sector_str:
                line += f" | {sector_str}"
            lines.append(line)

        if not lines:
            return None

        header = "📋 复盘开盘参考"
        if market_state:
            note = self._build_market_note(market_state)
            if note:
                header += "\n  " + note
        return header + "\n  " + "\n  ".join(lines)

    @staticmethod
    def _build_market_note(market_state: dict) -> str:
        if not market_state:
            return ""
        ok = market_state.get("market_ok", True)
        idx_price = market_state.get("index_price")
        chg_pct = market_state.get("change_pct")
        ma20 = market_state.get("ma20")
        parts = []
        if idx_price and chg_pct is not None:
            parts.append(f"上证{idx_price:.2f} {chg_pct:+.2%}")
        if ma20 and idx_price:
            vs_ma20 = "上方" if idx_price >= ma20 else "下方"
            parts.append(f"MA20{vs_ma20}")
        parts.append("大盘危险，暂停买入" if not ok else "大盘正常，可参考买入区间")
        return " | ".join(parts)
