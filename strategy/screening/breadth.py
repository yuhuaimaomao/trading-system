"""市场宽度计算 + 大盘状态判定"""

from typing import Optional

from data._base import connect
from system.config import settings

_BULL_UP = getattr(settings, "MARKET_BREADTH_BULL", 3000)
_DIVIDE_UP = getattr(settings, "MARKET_BREADTH_DIVIDE", 1500)
_BEAR_UP = getattr(settings, "MARKET_BREADTH_BEAR", 800)
_BOUNCE_UP = getattr(settings, "MARKET_BREADTH_BOUNCE", 2000)


def classify_market_state(
    up: int,
    down: int,
    limit_up: int,
    limit_down: int,
    index_chg: float,
    prev_state: str = "",
    consecutive_down_days: int = 0,
    limit_down_peak: int = 0,
) -> str:
    """大盘状态分类，返回: 普涨/分化/普跌/恐慌/连跌修复/超跌末端"""
    total = up + down
    if total == 0:
        return "分化"

    # 连跌修复: 恐慌次日上涨家数恢复
    if prev_state == "恐慌" and up > _BOUNCE_UP:
        return "连跌修复"

    # 超跌末端: 连跌 3+ 天且跌停家数从峰值回落 30%+
    if consecutive_down_days >= 3 and limit_down_peak > 0 and limit_down < limit_down_peak * 0.7:
        return "超跌末端"

    if up > _BULL_UP:
        return "普涨"
    elif up >= _DIVIDE_UP:
        return "分化"
    elif up >= _BEAR_UP:
        return "普跌"
    else:
        return "恐慌"


class MarketBreadth:
    """市场宽度计算器 — 盘前一次性计算涨跌家数+大盘状态"""

    def __init__(self, db_path: str = ""):
        self.db_path = db_path or settings.DATABASE_PATH

    def compute(self, trade_date: str) -> dict:
        """计算当日市场宽度"""
        conn = connect(self.db_path)
        try:
            # 涨跌家数
            row = conn.execute(
                """SELECT
                    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS up_count,
                    SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) AS down_count,
                    SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) AS flat_count
                FROM stock_basic WHERE trade_date = ?""",
                (trade_date,),
            ).fetchone()
            up, down, flat = row[0] or 0, row[1] or 0, row[2] or 0

            # 涨跌停家数
            row2 = conn.execute(
                """SELECT
                    SUM(CASE WHEN pool_type='涨停' THEN 1 ELSE 0 END) AS limit_up,
                    SUM(CASE WHEN pool_type='跌停' THEN 1 ELSE 0 END) AS limit_down
                FROM limit_pool WHERE trade_date = ?""",
                (trade_date,),
            ).fetchone()
            limit_up, limit_down = row2[0] or 0, row2[1] or 0

            # 上证指数涨跌幅
            from data.strategy.screening import ScreeningReader

            index_chg = ScreeningReader.get_index_change(conn, trade_date)

        finally:
            conn.close()

        prev_state, consec_downs = self._get_prev_context(trade_date, index_chg)
        limit_down_peak = self._get_limit_down_peak(trade_date)

        state = classify_market_state(
            up,
            down,
            limit_up,
            limit_down,
            index_chg,
            prev_state=prev_state,
            consecutive_down_days=consec_downs,
            limit_down_peak=limit_down_peak,
        )

        return {
            "up_count": up,
            "down_count": down,
            "flat_count": flat,
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "index_change_pct": round(index_chg, 4) if index_chg else 0,
            "market_state": state,
        }

    def save(self, trade_date: str) -> dict:
        """计算并写入 market_breadth 表"""
        result = self.compute(trade_date)
        conn = connect(self.db_path)
        try:
            from data.strategy.screening import ScreeningReader

            data = {"trade_date": trade_date, **result}
            ScreeningReader.insert_breadth(conn, data)
            conn.commit()
        finally:
            conn.close()
        return result

    def get(self, trade_date: str) -> Optional[dict]:
        """读取已保存的市场宽度"""
        conn = connect(self.db_path)
        from data.strategy.screening import ScreeningReader

        row = ScreeningReader.get_breadth_record(conn, trade_date)
        conn.close()
        return row

    def _get_prev_context(self, trade_date: str, index_chg: float) -> tuple[str, int]:
        """获取前一日 state 和连跌天数"""
        conn = connect(self.db_path)

        # 前一日状态
        row = conn.execute(
            """SELECT market_state FROM market_breadth
            WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1""",
            (trade_date,),
        ).fetchone()
        prev_state = row[0] if row else ""

        # 连跌天数
        rows = conn.execute(
            """SELECT index_change_pct FROM market_breadth
            WHERE trade_date < ? ORDER BY trade_date DESC""",
            (trade_date,),
        ).fetchall()
        conn.close()

        consec = 0
        for r in rows:
            if r[0] and r[0] < 0:
                consec += 1
            else:
                break
        if index_chg < 0:
            consec += 1
        return prev_state, consec

    def _get_limit_down_peak(self, trade_date: str) -> int:
        """近 5 日跌停家数峰值（从已保存的 market_breadth 或 limit_pool 直接算）"""
        conn = connect(self.db_path)
        # 优先从 market_breadth 取
        row = conn.execute(
            """SELECT MAX(limit_down_count) FROM market_breadth
            WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 5""",
            (trade_date,),
        ).fetchone()
        if row[0] and row[0] > 0:
            conn.close()
            return row[0]

        # fallback: 从 limit_pool 算近 5 天
        rows = conn.execute(
            """SELECT trade_date FROM stock_basic
            WHERE trade_date < ? GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5""",
            (trade_date,),
        ).fetchall()
        peak = 0
        for r in rows:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM limit_pool WHERE trade_date=? AND pool_type='跌停'",
                (r[0],),
            ).fetchone()[0]
            if cnt > peak:
                peak = cnt
        conn.close()
        return peak
