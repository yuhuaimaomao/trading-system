"""复盘推荐盯盘 — 与趋势信号同一逻辑：盘中持续盯买入区间，进入/离开/再进入可重复提醒。

数据来源: trade_signals (signal_source='REVIEW')
买入区间: 直接使用 REVIEW 信号中的 buy_zone_min / buy_zone_max，不复算 MA 均线。
"""

from datetime import datetime
from typing import Optional

from data._base import connect
from system.utils.logger import get_trade_logger

logger = get_trade_logger("core")


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
        self._picks: dict[str, dict] = {}  # code → {name, buy_min, buy_max, stop_loss, ...}
        self._loaded = False

    def _get_conn(self):
        return connect(self.db_path)

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def load_picks(self):
        """开盘一次性加载复盘推荐（从 trade_signals 的 REVIEW 信号）。"""
        rows = self._load_from_trade_signals()
        if not rows:
            logger.info("无复盘推荐标的")
            self._loaded = True
            return

        for r in rows:
            code = r["stock_code"]
            self._picks[code] = {
                "name": r.get("stock_name", code),
                "buy_min": r.get("buy_zone_min", 0) or 0,
                "buy_max": r.get("buy_zone_max", 0) or 0,
                "stop_loss": r.get("stop_loss", 0) or 0,
                "target_price": r.get("take_profit", 0) or 0,
            }

        self._loaded = True
        logger.info(f"复盘盯盘加载: {len(self._picks)} 只标的（来源: trade_signals REVIEW）")

    def _load_from_trade_signals(self) -> list[dict]:
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT stock_code, stock_name, buy_zone_min, buy_zone_max,
                          stop_loss, take_profit
                   FROM trade_signals
                   WHERE trade_date = ?
                     AND signal_source = 'REVIEW'
                     AND status = 'pending'""",
                (today,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning(f"加载复盘信号失败: {e}")
            return []

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
        """返回买入区间 (buy_min, buy_max)，直接取 REVIEW 信号中已计算好的区间。"""
        pick = self._picks.get(code)
        if not pick:
            return (0, 0)
        return (pick.get("buy_min", 0) or 0, pick.get("buy_max", 0) or 0)

    # ------------------------------------------------------------------
    # 开盘参考
    # ------------------------------------------------------------------

    def build_opening_reference(
        self,
        prices: dict[str, float],
        market_state: dict = None,
        sector_data: dict[str, float] = None,
    ) -> Optional[str]:
        """生成开盘买入参考（只在第一轮调用）。"""
        if not self._picks:
            return None

        lines: list[str] = []
        for code, pick in self._picks.items():
            price = prices.get(code)
            if price is None:
                continue

            buy_min = pick.get("buy_min", 0) or 0
            buy_max = pick.get("buy_max", 0) or 0
            if buy_min <= 0:
                continue

            # 判断当前状态
            if price >= buy_min:
                status = "在买入区" if price <= buy_max else "高于买入区"
            else:
                status = "低于买入区"

            sl = pick.get("stop_loss", 0) or 0
            tp = pick.get("target_price", 0) or 0

            line = f"{code} {pick['name']} 现价{price:.2f} | {status}"
            line += f" | 买入{buy_min:.2f}-{buy_max:.2f}"
            if sl > 0:
                line += f" | 止损{sl:.2f}"
            if tp > 0:
                line += f" | 目标{tp:.2f}"
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
