"""intraday_fusion 表数据访问

盘中融合快照 — 尾盘选股引擎3 审计用。
每轮腾讯 qt + 资金流 + QMT 快照落库，可回溯完整决策过程。
"""

from data._base import BaseRepository
from system.utils.logger import get_system_logger

logger = get_system_logger("data")

_INTRODAY_FUSION_COLS = [
    "trade_date",
    "round_ts",
    "stock_code",
    "stock_name",
    "price",
    "open",
    "high",
    "low",
    "prev_close",
    "change_pct",
    "volume",
    "turnover",
    "turnover_rate",
    "amplitude",
    "circ_market_cap",
    "volume_ratio",
    "pe_ttm",
    "main_force_net",
    "main_force_ratio",
    "round_type",
    "is_candidate",
    "candidate_score",
]


class IntradayFusionRepo(BaseRepository):
    """盘中融合快照 CRUD"""

    def __init__(self):
        super().__init__()

    def save_batch(self, trade_date: str, round_ts: float, rows: list[dict]):
        """批量保存一轮融合数据"""
        if not rows:
            return

        conn = self._conn()
        try:
            placeholders = ",".join(["?"] * len(_INTRODAY_FUSION_COLS))
            col_str = ",".join(_INTRODAY_FUSION_COLS)
            insert_data = []
            seen = set()
            for row in rows:
                code = row.get("stock_code", "")
                key = (trade_date, round_ts, code)
                if key in seen:
                    continue
                seen.add(key)
                values = tuple(row.get(c, 0) for c in _INTRODAY_FUSION_COLS)
                insert_data.append(values)

            conn.executemany(
                f"INSERT OR REPLACE INTO intraday_fusion ({col_str}) VALUES ({placeholders})",
                insert_data,
            )
            conn.commit()
            logger.debug(f"intraday_fusion 写入: {len(insert_data)} 条, round_ts={round_ts}")
        except Exception as e:
            conn.rollback()
            logger.warning(f"intraday_fusion 写入失败: {e}")
            raise
        finally:
            conn.close()

    def get_latest_round(self, trade_date: str) -> float | None:
        """获取当日最新一轮的 round_ts"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT MAX(round_ts) FROM intraday_fusion WHERE trade_date=?",
                (trade_date,),
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()

    def get_candidates(self, trade_date: str, round_ts: float) -> list[dict]:
        """获取某一轮的候选标的"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM intraday_fusion "
                "WHERE trade_date=? AND round_ts=? AND is_candidate=1 "
                "ORDER BY candidate_score DESC",
                (trade_date, round_ts),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_rounds(self, trade_date: str) -> list[dict]:
        """获取当日所有轮次摘要（轮次时间 + 候选数）"""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT round_ts, round_type, COUNT(*) as total, "
                "SUM(is_candidate) as candidates "
                "FROM intraday_fusion WHERE trade_date=? "
                "GROUP BY round_ts ORDER BY round_ts",
                (trade_date,),
            ).fetchall()
            return [
                {
                    "round_ts": r[0],
                    "round_type": r[1],
                    "total": r[2],
                    "candidates": r[3],
                }
                for r in rows
            ]
        finally:
            conn.close()
