"""交易用趋势票筛选器

双模式筛选:
  - 5日线强趋势 (strong): 沿MA5陡峭爬升，主升浪追涨型
  - 20日线稳健趋势 (normal): 沿MA20稳健上行，回调低吸型

与 quant-system 复盘筛选同源但独立实现——复盘筛选用来看盘，
交易筛选用来做盘，规则更严格，输出可直接供 ai_advisor 分析。
"""

import logging
import sqlite3
from typing import Optional

from system.config import settings
from analysis.signals import StockScore

logger = logging.getLogger(__name__)

# 基础 SQL：筛选条件与参考保持一致
_BASE_SQL = """
    SELECT stock_code, stock_name, change_pct,
           total_market_cap / 100000000.0 AS mcap,
           circ_market_cap / 100000000.0 AS circ_mcap,
           turnover_rate, volume_ratio,
           ma5, ma10, ma20, ma5_angle,
           industry, price,
           main_force_net / 10000.0 AS mf_wan,
           main_force_ratio AS mf_ratio,
           avg_vol_5d, avg_vol_20d
    FROM stock_basic
    WHERE trade_date = ?
      AND stock_name NOT LIKE '%ST%'
      AND stock_code NOT LIKE '688%'
      AND ABS(change_pct) < 9.5
      AND total_market_cap BETWEEN 5000000000 AND 50000000000
      AND turnover_rate BETWEEN 3 AND 15
      AND avg_vol_5d >= avg_vol_20d * 0.9
      AND price > 0 AND ma5 > 0 AND ma10 > 0 AND ma20 > 0
    ORDER BY ma5_angle DESC
"""


class TrendScreener:
    """交易趋势筛选器

    Args:
        db_path: SQLite 数据库路径。默认为 config.settings.DATABASE_PATH。
        min_score: 最低分数过滤线，低于此分的候选不返回。默认 50。
        top_n: 每种模式取前 N 只。默认 10。
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        min_score: float = 50,
        top_n: int = 10,
    ):
        self.db_path = db_path or settings.DATABASE_PATH
        self.min_score = min_score
        self.top_n = top_n

    # ---- 公开接口 ----

    def screen(self, trade_date: str) -> list[StockScore]:
        """执行趋势筛选，返回 StockScore 列表，按 score 降序。

        流程:
          1. 连接数据库，执行基础 SQL。
          2. 逐行判断 strong / normal 趋势。
          3. 各取 TOP N，合并排序后返回。
        """
        conn = sqlite3.connect(self.db_path)
        try:
            rows = self._fetch_base(conn, trade_date)
            strong, normal = self._classify(rows)
            return self._merge_and_sort(strong, normal)
        finally:
            conn.close()

    # ---- 内部方法 ----

    def _fetch_base(self, conn: sqlite3.Connection, trade_date: str) -> list[dict]:
        """执行基础 SQL，返回原始字典列表。"""
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(_BASE_SQL, (trade_date,))
        return [dict(row) for row in cursor.fetchall()]

    def _classify(
        self, rows: list[dict]
    ) -> tuple[list[StockScore], list[StockScore]]:
        """逐行判断趋势类型，返回 (strong列表, normal列表)。"""
        strong: list[StockScore] = []
        normal: list[StockScore] = []
        strong_codes: set[str] = set()

        for row in rows:
            code = row["stock_code"]
            price = row["price"] or 0
            ma5 = row["ma5"] or 0
            ma10 = row["ma10"] or 0
            ma20 = row["ma20"] or 0
            ma5_angle = row["ma5_angle"] or 0

            # --- 5日线强趋势判断 ---
            bias_ma5 = (price - ma5) / ma5 if ma5 > 0 else 999
            spread_5_20 = (ma5 - ma20) / ma20 if ma20 > 0 else 0
            is_strong = (
                price > ma5
                and ma5 > ma10 > ma20
                and bias_ma5 < 0.05
                and spread_5_20 > 0.03
            )

            if is_strong:
                slope_score = min(40 + spread_5_20 * 100, 100)
                if slope_score >= self.min_score:
                    strong.append(self._build_score(row, "strong", slope_score, bias_ma5=bias_ma5 * 100))
                    strong_codes.add(code)
                    if self._should_stop(strong, normal):
                        break
                continue

            # --- 20日线稳健趋势判断 ---
            bias_ma20 = (price - ma20) / ma20 if ma20 > 0 else 999
            is_normal = (
                price > ma20
                and bias_ma20 < 0.10
                and ma5_angle > 0
            )

            if is_normal:
                dev_pct = bias_ma20 * 100
                normal_score = 60 + (20 - dev_pct) * 0.5
                normal_score = min(max(normal_score, 50), 90)
                if normal_score >= self.min_score:
                    normal.append(self._build_score(row, "normal", normal_score, bias_ma20=dev_pct))

            if self._should_stop(strong, normal):
                break

        return strong, normal

    @staticmethod
    def _build_score(
        row: dict,
        mode: str,
        score: float,
        bias_ma5: float = 0.0,
        bias_ma20: float = 0.0,
    ) -> StockScore:
        return StockScore(
            stock_code=row["stock_code"],
            stock_name=row["stock_name"],
            trend_mode=mode,
            score=round(score, 1),
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
            bias_ma5=round(bias_ma5, 2),
            bias_ma20=round(bias_ma20, 2),
        )

    def _should_stop(self, strong: list, normal: list) -> bool:
        """判断是否已收集够候选，可以提前停止遍历。"""
        return len(strong) >= self.top_n and len(normal) >= self.top_n

    def _merge_and_sort(
        self, strong: list[StockScore], normal: list[StockScore]
    ) -> list[StockScore]:
        """各取 TOP N，合并后按 score 降序排列。"""
        strong = sorted(strong, key=lambda x: x.score, reverse=True)[: self.top_n]
        normal = sorted(normal, key=lambda x: x.score, reverse=True)[: self.top_n]
        combined = strong + normal
        combined.sort(key=lambda x: x.score, reverse=True)
        return combined
