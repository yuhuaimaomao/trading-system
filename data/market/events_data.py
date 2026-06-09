"""
涨跌停池数据读取器

市场基础数据 — 连板梯队、炸板、首板、涨停质量、昨日涨停表现。
跨域共享，全部业务线均可使用。
纯数据查询，不做格式化。
"""

from system.utils.logger import get_system_logger

logger = get_system_logger("data")


class LimitPoolReader:
    """涨跌停池读取器（所有方法均为静态，传入 conn）"""

    @staticmethod
    def get_chain_ladder(conn, trade_date: str) -> tuple:
        """
        查询连板梯队（≥2 板）

        Returns:
            (chain, chain_count, highest_board)
        """
        cursor = conn.execute(
            """
            SELECT lp.consecutive_boards, lp.stock_code, lp.stock_name,
                   lp.first_seal_time, lp.seal_amount, lp.zt_stat,
                   sb.change_pct, sb.turnover_rate, sb.volume_ratio,
                   sb.amplitude, sb.total_market_cap/100000000 as mcap,
                   sb.circ_market_cap/100000000 as circ_mcap,
                   sb.main_force_net/10000 as mf_wan,
                   sb.super_large_net/10000 as sl_wan,
                   sb.large_net/10000 as lg_wan,
                   sb.medium_net/10000 as md_wan,
                   sb.small_net/10000 as sm_wan,
                   sb.main_force_ratio,
                   sb.turnover/10000 as turnover_wan,
                   lp.industry,
                   sb.price, sb.ma5, sb.ma10, sb.ma20, sb.ma5_angle
            FROM limit_pool lp
            JOIN stock_basic sb ON lp.stock_code = sb.stock_code AND sb.trade_date = ?
            WHERE lp.trade_date = ? AND lp.pool_type = '涨停'
            AND lp.consecutive_boards >= 2
            ORDER BY lp.consecutive_boards DESC
        """,
            (trade_date, trade_date),
        )
        chain = {}
        for row in cursor.fetchall():
            board = row["consecutive_boards"]
            if board not in chain:
                chain[board] = []
            chain[board].append(
                {
                    "code": row["stock_code"],
                    "name": row["stock_name"],
                    "change": row["change_pct"] or 0,
                    "turnover": row["turnover_rate"] or 0,
                    "vol_ratio": row["volume_ratio"] or 0,
                    "amplitude": row["amplitude"] or 0,
                    "mcap": row["mcap"] or 0,
                    "circ_mcap": row["circ_mcap"] or 0,
                    "mf_wan": row["mf_wan"] or 0,
                    "sl_wan": row["sl_wan"] or 0,
                    "lg_wan": row["lg_wan"] or 0,
                    "md_wan": row["md_wan"] or 0,
                    "sm_wan": row["sm_wan"] or 0,
                    "mf_ratio": row["main_force_ratio"] or 0,
                    "first_seal": row["first_seal_time"] or "",
                    "seal_amount": row["seal_amount"] or 0,
                    "turnover_wan": row["turnover_wan"] or 0,
                    "zt_stat": row["zt_stat"] or "0/0",
                    "industry": row["industry"] or "",
                    "price": row["price"] or 0,
                    "ma5": row["ma5"] or 0,
                    "ma10": row["ma10"] or 0,
                    "ma20": row["ma20"] or 0,
                    "ma5_angle": row["ma5_angle"] or 0,
                }
            )
        chain_count = sum(len(v) for v in chain.values())
        highest_board = max(chain.keys()) if chain else 0
        return chain, chain_count, highest_board

    @staticmethod
    def get_prev_chain_stats(conn, yesterday: str) -> tuple:
        """查询昨日连板统计（用于环比）"""
        cursor = conn.execute(
            """
            SELECT COUNT(DISTINCT stock_code) as cnt,
                   MAX(consecutive_boards) as max_board
            FROM limit_pool WHERE trade_date = ? AND pool_type = '涨停'
            AND consecutive_boards >= 2
        """,
            (yesterday,),
        )
        row = cursor.fetchone()
        prev_chain_count = row["cnt"] if row else 0
        prev_highest_board = row["max_board"] if row else 0
        return prev_chain_count, prev_highest_board

    @staticmethod
    def get_broken_boards(conn, trade_date: str) -> list:
        """查询炸板明细"""
        cursor = conn.execute(
            """
            SELECT lp.stock_code, lp.stock_name,
                   lp.first_seal_time, lp.open_count, lp.industry,
                   sb.change_pct, sb.turnover_rate, sb.amplitude,
                   sb.main_force_net/10000 as mf_wan,
                   sb.total_market_cap/100000000 as mcap,
                   sb.price, sb.ma5, sb.ma10, sb.ma20, sb.ma5_angle
            FROM limit_pool lp
            JOIN stock_basic sb ON lp.stock_code = sb.stock_code AND sb.trade_date = ?
            WHERE lp.trade_date = ? AND lp.pool_type = '炸板'
            ORDER BY sb.change_pct DESC
        """,
            (trade_date, trade_date),
        )
        records = []
        for row in cursor.fetchall():
            records.append(
                {
                    "code": row["stock_code"],
                    "name": row["stock_name"],
                    "change": row["change_pct"] or 0,
                    "turnover": row["turnover_rate"] or 0,
                    "amplitude": row["amplitude"] or 0,
                    "mf_wan": row["mf_wan"] or 0,
                    "mcap": row["mcap"] or 0,
                    "first_seal": row["first_seal_time"] or "",
                    "open_count": row["open_count"] or 0,
                    "industry": row["industry"] or "",
                    "price": row["price"] or 0,
                    "ma5": row["ma5"] or 0,
                    "ma10": row["ma10"] or 0,
                    "ma20": row["ma20"] or 0,
                    "ma5_angle": row["ma5_angle"] or 0,
                }
            )
        return records

    @staticmethod
    def get_first_boards(conn, trade_date: str) -> list:
        """查询首板苗子（封板 ≤ 09:40 或龙虎榜上榜）"""
        cursor = conn.execute(
            """
            SELECT lp.stock_code, lp.stock_name, lp.first_seal_time,
                   lp.seal_amount, lp.zt_stat, lp.industry,
                   sb.change_pct, sb.turnover_rate, sb.volume_ratio,
                   sb.amplitude, sb.total_market_cap/100000000 as mcap,
                   sb.circ_market_cap/100000000 as circ_mcap,
                   sb.main_force_net/10000 as mf_wan,
                   sb.super_large_net/10000 as sl_wan,
                   sb.large_net/10000 as lg_wan,
                   sb.medium_net/10000 as md_wan,
                   sb.small_net/10000 as sm_wan,
                   sb.main_force_ratio,
                   sb.turnover/10000 as turnover_wan,
                   COALESCE(lhb.net_inflow, 0) as lhb_net,
                   sb.price, sb.ma5, sb.ma10, sb.ma20, sb.ma5_angle
            FROM limit_pool lp
            JOIN stock_basic sb ON lp.stock_code = sb.stock_code
                AND sb.trade_date = ?
            LEFT JOIN lhb_stocks lhb ON lp.stock_code = lhb.stock_code
                AND lhb.trade_date = ?
            WHERE lp.trade_date = ? AND lp.pool_type = '涨停'
              AND lp.consecutive_boards = 1
              AND (
                lp.first_seal_time <= '09:40'
                OR lhb.stock_code IS NOT NULL
              )
            ORDER BY lp.first_seal_time ASC
            LIMIT 15
        """,
            (trade_date, trade_date, trade_date),
        )
        records = []
        for row in cursor.fetchall():
            records.append(
                {
                    "code": row["stock_code"],
                    "name": row["stock_name"],
                    "change": row["change_pct"] or 0,
                    "turnover": row["turnover_rate"] or 0,
                    "vol_ratio": row["volume_ratio"] or 0,
                    "amplitude": row["amplitude"] or 0,
                    "mcap": row["mcap"] or 0,
                    "circ_mcap": row["circ_mcap"] or 0,
                    "mf_wan": row["mf_wan"] or 0,
                    "sl_wan": row["sl_wan"] or 0,
                    "lg_wan": row["lg_wan"] or 0,
                    "md_wan": row["md_wan"] or 0,
                    "sm_wan": row["sm_wan"] or 0,
                    "mf_ratio": row["main_force_ratio"] or 0,
                    "first_seal": row["first_seal_time"] or "",
                    "seal_amount": row["seal_amount"] or 0,
                    "turnover_wan": row["turnover_wan"] or 0,
                    "zt_stat": row["zt_stat"] or "0/0",
                    "industry": row["industry"] or "",
                    "lhb_net": row["lhb_net"] or 0,
                    "price": row["price"] or 0,
                    "ma5": row["ma5"] or 0,
                    "ma10": row["ma10"] or 0,
                    "ma20": row["ma20"] or 0,
                    "ma5_angle": row["ma5_angle"] or 0,
                }
            )
        return records

    @staticmethod
    def get_limit_quality(conn, trade_date: str) -> dict:
        """查询涨停质量细分（一字板/换手板/回封板）"""
        cursor = conn.execute(
            """
            SELECT open_count,
                   CASE
                       WHEN first_seal_time = '09:25' THEN '一字板'
                       WHEN open_count > 0 THEN '回封板'
                       ELSE '换手板'
                   END as quality_type
            FROM limit_pool
            WHERE trade_date = ? AND pool_type = '涨停'
        """,
            (trade_date,),
        )
        quality = {}
        for row in cursor.fetchall():
            qt = row["quality_type"]
            quality[qt] = quality.get(qt, 0) + 1
        return quality

    @staticmethod
    def get_yzt_performance(conn, trade_date: str) -> list:
        """查询昨日涨停今日表现"""
        cursor = conn.execute(
            """
            SELECT yp.stock_code, yp.stock_name,
                   yp.change_percent as yzt_change,
                   yp.yesterday_board_count,
                   yp.yesterday_seal_time, yp.industry,
                   sb.change_pct, sb.turnover_rate, sb.volume_ratio,
                   sb.amplitude, sb.total_market_cap/100000000 as mcap,
                   sb.circ_market_cap/100000000 as circ_mcap,
                   sb.main_force_net/10000 as mf_wan,
                   sb.super_large_net/10000 as sl_wan,
                   sb.large_net/10000 as lg_wan,
                   sb.medium_net/10000 as md_wan,
                   sb.small_net/10000 as sm_wan,
                   sb.main_force_ratio,
                   sb.price, sb.ma5, sb.ma10, sb.ma20, sb.ma5_angle
            FROM yesterday_zt_performance yp
            LEFT JOIN stock_basic sb ON yp.stock_code = sb.stock_code AND sb.trade_date = ?
            WHERE yp.trade_date = ?
            ORDER BY yp.change_percent DESC
        """,
            (trade_date, trade_date),
        )
        records = []
        for row in cursor.fetchall():
            records.append(
                {
                    "code": row["stock_code"],
                    "name": row["stock_name"],
                    "change": row["yzt_change"] or 0,
                    "boards": row["yesterday_board_count"] or 0,
                    "seal_time": row["yesterday_seal_time"] or "",
                    "industry": row["industry"] or "",
                    "today_change": row["change_pct"] or 0,
                    "turnover": row["turnover_rate"] or 0,
                    "vol_ratio": row["volume_ratio"] or 0,
                    "amplitude": row["amplitude"] or 0,
                    "mcap": row["mcap"] or 0,
                    "circ_mcap": row["circ_mcap"] or 0,
                    "mf_wan": row["mf_wan"] or 0,
                    "sl_wan": row["sl_wan"] or 0,
                    "lg_wan": row["lg_wan"] or 0,
                    "md_wan": row["md_wan"] or 0,
                    "sm_wan": row["sm_wan"] or 0,
                    "mf_ratio": row["main_force_ratio"] or 0,
                    "price": row["price"] or 0,
                    "ma5": row["ma5"] or 0,
                    "ma10": row["ma10"] or 0,
                    "ma20": row["ma20"] or 0,
                    "ma5_angle": row["ma5_angle"] or 0,
                }
            )
        return records

    @staticmethod
    def get_limit_pool(conn, trade_date: str) -> list:
        """查询涨跌停池列表。"""
        cursor = conn.execute(
            """SELECT stock_code, stock_name, pool_type, consecutive_boards,
                      first_seal_time, seal_amount, zt_stat, industry, open_count
               FROM limit_pool WHERE trade_date = ?""",
            (trade_date,),
        )
        return [dict(row) for row in cursor.fetchall()]
