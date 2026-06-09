"""
股票查询工具集

用于 Function Calling，提供股票市值、信息查询等工具
"""

import contextlib
from typing import Dict, List

from data._base import connect
from system.config.settings import DATABASE_PATH, STORAGE_PATH
from system.utils.logger import get_system_logger

logger = get_system_logger("ai")


class StockTools:
    """股票查询工具"""

    def __init__(self, db_path: str = None):
        """
        初始化工具

        Args:
            db_path: 数据库路径，默认使用项目数据库
        """
        self.db_path = db_path or DATABASE_PATH

    def get_market_cap(self, stock_code: str) -> Dict:
        """
        查询股票市值

        Args:
            stock_code: 股票代码（如 "688702"）

        Returns:
            {
                "code": "688702",
                "name": "盛科通信",
                "market_cap": 500.5,  # 单位：亿
                "update_time": "2026-04-24",
                "error": None
            }
        """
        try:
            conn = connect(self.db_path)
            cursor = conn.cursor()

            # 查询最新交易日的数据
            cursor.execute(
                """
                SELECT stock_name, total_market_cap, trade_date
                FROM stock_basic
                WHERE stock_code = ?
                ORDER BY trade_date DESC
                LIMIT 1
            """,
                (stock_code,),
            )

            row = cursor.fetchone()
            conn.close()

            if row:
                stock_name, total_market_cap, trade_date = row
                # total_market_cap 单位是元，转为亿
                market_cap_yi = total_market_cap / 100000000 if total_market_cap else None

                logger.info(f"✅ 查询市值成功：{stock_code} - {stock_name} - {market_cap_yi:.1f}亿")

                return {
                    "code": stock_code,
                    "name": stock_name,
                    "market_cap": round(market_cap_yi, 1) if market_cap_yi else None,
                    "update_time": trade_date,
                    "error": None,
                }
            else:
                logger.warning(f"⚠️ 未找到股票数据：{stock_code}")
                return {
                    "code": stock_code,
                    "name": None,
                    "market_cap": None,
                    "update_time": None,
                    "error": f"未找到股票 {stock_code} 的数据",
                }

        except Exception as e:
            logger.error(f"❌ 查询市值失败：{stock_code} - {e}")
            return {
                "code": stock_code,
                "name": None,
                "market_cap": None,
                "update_time": None,
                "error": str(e),
            }

    def get_stock_info(self, stock_code: str) -> Dict:
        """
        查询股票完整信息

        Args:
            stock_code: 股票代码

        Returns:
            {
                "code": "688702",
                "name": "盛科通信",
                "industry": "半导体",
                "market_cap": 500.5,
                "price": 150.2,
                "change_pct": 5.2,
                "error": None
            }
        """
        try:
            conn = connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT stock_name, industry, concepts, total_market_cap, price, change_pct, trade_date
                FROM stock_basic
                WHERE stock_code = ?
                ORDER BY trade_date DESC
                LIMIT 1
            """,
                (stock_code,),
            )

            row = cursor.fetchone()
            conn.close()

            if row:
                (
                    stock_name,
                    industry,
                    concepts,
                    total_market_cap,
                    price,
                    change_pct,
                    trade_date,
                ) = row

                return {
                    "code": stock_code,
                    "name": stock_name,
                    "industry": industry or "",
                    "concept": concepts or "",
                    "market_cap": round(total_market_cap / 100000000, 1) if total_market_cap else None,
                    "price": price,
                    "change_pct": change_pct,
                    "update_time": trade_date,
                    "error": None,
                }
            else:
                return {
                    "code": stock_code,
                    "name": None,
                    "industry": None,
                    "concept": None,
                    "market_cap": None,
                    "price": None,
                    "change_pct": None,
                    "update_time": None,
                    "error": f"未找到股票 {stock_code} 的数据",
                }

        except Exception as e:
            logger.error(f"❌ 查询股票信息失败：{stock_code} - {e}")
            return {
                "code": stock_code,
                "name": None,
                "industry": None,
                "concept": None,
                "market_cap": None,
                "price": None,
                "change_pct": None,
                "update_time": None,
                "error": str(e),
            }

    def get_sector_stocks(self, sector_name: str, limit: int = 10) -> List[Dict]:
        """
        查询板块成分股（按涨幅排序）

        Args:
            sector_name: 板块名称（行业或概念）
            limit: 返回数量限制

        Returns:
            股票列表
        """
        try:
            conn = connect(self.db_path)
            cursor = conn.cursor()

            # 查询最新交易日
            cursor.execute("""
                SELECT MAX(trade_date) FROM stock_basic
            """)
            latest_date = cursor.fetchone()[0]

            if not latest_date:
                conn.close()
                return []

            # 查询板块成分股（按涨幅排序）
            cursor.execute(
                """
                SELECT stock_code, stock_name, industry, concepts, total_market_cap, price, change_pct
                FROM stock_basic
                WHERE trade_date = ? AND (industry = ? OR concepts LIKE ?)
                ORDER BY change_pct DESC
                LIMIT ?
            """,
                (latest_date, sector_name, f"%{sector_name}%", limit),
            )

            rows = cursor.fetchall()
            conn.close()

            stocks = []
            for row in rows:
                stocks.append(
                    {
                        "code": row[0],
                        "name": row[1],
                        "industry": row[2] or "",
                        "concept": row[3] or "",
                        "market_cap": round(row[4] / 100000000, 1) if row[4] else None,
                        "price": row[5],
                        "change_pct": row[6],
                    }
                )

            logger.info(f"✅ 查询板块成分股成功：{sector_name} - {len(stocks)}只")
            return stocks

        except Exception as e:
            logger.error(f"❌ 查询板块成分股失败：{sector_name} - {e}")
            return []

    def get_lhb_seats(self, stock_code: str, trade_date: str = None) -> Dict:
        """
        查询某只股票在指定日期的龙虎榜席位明细

        Args:
            stock_code: 股票代码（如 "001259"）
            trade_date: 交易日期（YYYY-MM-DD），默认最新

        Returns:
            {
                "code": "001259",
                "trade_date": "2026-05-20",
                "buy_seats": [{"name": "...", "buy": 1234.5, "sell": 0, "net": 1234.5, "is_inst": 1}, ...],
                "sell_seats": [...],
                "error": None
            }
        """
        try:
            conn = connect(self.db_path)
            cursor = conn.cursor()

            if not trade_date:
                cursor.execute("SELECT MAX(trade_date) FROM lhb_seats")
                trade_date = cursor.fetchone()[0]
                if not trade_date:
                    conn.close()
                    return {
                        "code": stock_code,
                        "trade_date": None,
                        "buy_seats": [],
                        "sell_seats": [],
                        "error": "无龙虎榜数据",
                    }

            # 买席 TOP5（去重合并，按买入额降序）
            cursor.execute(
                """
                SELECT seat_name, MAX(buy_amount), MAX(sell_amount),
                       MAX(is_institution), MAX(is_hot_money)
                FROM lhb_seats
                WHERE stock_code = ? AND trade_date = ? AND seat_type = 'buy'
                GROUP BY seat_name
                ORDER BY MAX(buy_amount) DESC
                LIMIT 5
            """,
                (stock_code, trade_date),
            )
            buy_rows = cursor.fetchall()

            # 卖席 TOP5
            cursor.execute(
                """
                SELECT seat_name, MAX(buy_amount), MAX(sell_amount),
                       MAX(is_institution), MAX(is_hot_money)
                FROM lhb_seats
                WHERE stock_code = ? AND trade_date = ? AND seat_type = 'sell'
                GROUP BY seat_name
                ORDER BY MAX(sell_amount) DESC
                LIMIT 5
            """,
                (stock_code, trade_date),
            )
            sell_rows = cursor.fetchall()
            conn.close()

            if not buy_rows and not sell_rows:
                return {
                    "code": stock_code,
                    "trade_date": trade_date,
                    "buy_seats": [],
                    "sell_seats": [],
                    "error": f"{stock_code} 在 {trade_date} 未上龙虎榜",
                }

            buy_seats = []
            for row in buy_rows:
                buy_seats.append(
                    {
                        "name": row[0],
                        "buy": round(row[1] / 10000, 1) if row[1] else 0,
                        "sell": round(row[2] / 10000, 1) if row[2] else 0,
                        "net": round((row[1] or 0) / 10000 - (row[2] or 0) / 10000, 1),
                        "is_inst": bool(row[3]),
                        "is_hm": bool(row[4]),
                    }
                )

            sell_seats = []
            for row in sell_rows:
                sell_seats.append(
                    {
                        "name": row[0],
                        "buy": round(row[1] / 10000, 1) if row[1] else 0,
                        "sell": round(row[2] / 10000, 1) if row[2] else 0,
                        "net": round((row[1] or 0) / 10000 - (row[2] or 0) / 10000, 1),
                        "is_inst": bool(row[3]),
                        "is_hm": bool(row[4]),
                    }
                )

            logger.info(f"✅ 查询龙虎榜席位成功：{stock_code} {trade_date} - 买{len(buy_seats)}卖{len(sell_seats)}")
            return {
                "code": stock_code,
                "trade_date": trade_date,
                "buy_seats": buy_seats,
                "sell_seats": sell_seats,
                "error": None,
            }

        except Exception as e:
            logger.error(f"❌ 查询龙虎榜席位失败：{stock_code} - {e}")
            return {
                "code": stock_code,
                "trade_date": trade_date,
                "buy_seats": [],
                "sell_seats": [],
                "error": str(e),
            }

    def get_regulatory_risks(self, stock_code: str, trade_date: str = None) -> Dict:
        """
        查询某只股票的监管函/问询函/处罚等风险记录

        Args:
            stock_code: 股票代码（如 "000608"）
            trade_date: 交易日期（YYYY-MM-DD），默认查最近30天

        Returns:
            {
                "code": "000608",
                "risks": [{"title": "...", "risk_type": "财务造假", "risk_level": 3,
                           "issuer": "深交所", "summary": "..."}, ...],
                "error": None
            }
        """
        try:
            conn = connect(self.db_path)
            cursor = conn.cursor()

            if trade_date:
                cursor.execute(
                    """
                    SELECT title, risk_type, risk_level, issuer_short,
                           COALESCE(pdf_summary, risk_summary, '') as summary,
                           trade_date
                    FROM regulatory_letter
                    WHERE stock_code = ? AND trade_date = ?
                    ORDER BY risk_level DESC, trade_date DESC
                """,
                    (stock_code, trade_date),
                )
            else:
                cursor.execute(
                    """
                    SELECT title, risk_type, risk_level, issuer_short,
                           COALESCE(pdf_summary, risk_summary, '') as summary,
                           trade_date
                    FROM regulatory_letter
                    WHERE stock_code = ?
                    ORDER BY risk_level DESC, trade_date DESC
                    LIMIT 10
                """,
                    (stock_code,),
                )

            rows = cursor.fetchall()
            conn.close()

            risks = []
            for row in rows:
                title, risk_type, risk_level, issuer, summary, rdate = row
                risks.append(
                    {
                        "title": (title or "")[:100],
                        "risk_type": risk_type or "未分类",
                        "risk_level": risk_level or 1,
                        "issuer": issuer or "",
                        "summary": (summary or "")[:200],
                        "date": rdate or "",
                    }
                )

            logger.info(f"✅ 查询监管风险成功：{stock_code} - {len(risks)}条")
            return {"code": stock_code, "risks": risks, "error": None}

        except Exception as e:
            logger.error(f"❌ 查询监管风险失败：{stock_code} - {e}")
            return {"code": stock_code, "risks": [], "error": str(e)}

    def get_yesterday_limit_ups(self, trade_date: str = None, limit: int = 50) -> Dict:
        """
        查询昨日涨停股今日表现全量明细（默认当日）

        Returns:
            {"trade_date": "2026-05-20", "total": 90, "avg_change": -0.23,
             "stocks": [{"code": "001259", "name": "利仁科技", "change": 10.0, ...}]}
        """
        try:
            conn = connect(self.db_path)
            cursor = conn.cursor()

            if not trade_date:
                cursor.execute("SELECT MAX(trade_date) FROM yesterday_zt_performance")
                trade_date = cursor.fetchone()[0]
                if not trade_date:
                    conn.close()
                    return {
                        "trade_date": None,
                        "total": 0,
                        "stocks": [],
                        "error": "无数据",
                    }

            cursor.execute(
                """
                SELECT yp.stock_code, yp.stock_name, yp.change_percent,
                       yp.yesterday_board_count, yp.industry,
                       sb.change_pct, sb.turnover_rate, sb.volume_ratio,
                       sb.amplitude, sb.total_market_cap/100000000 as mcap,
                       sb.circ_market_cap/100000000 as circ_mcap,
                       sb.main_force_net/10000 as mf_wan,
                       sb.main_force_ratio, sb.price
                FROM yesterday_zt_performance yp
                LEFT JOIN stock_basic sb ON yp.stock_code = sb.stock_code
                    AND sb.trade_date = ?
                WHERE yp.trade_date = ?
                ORDER BY yp.change_percent DESC
                LIMIT ?
            """,
                (trade_date, trade_date, limit),
            )

            rows = cursor.fetchall()
            conn.close()

            stocks = []
            for row in rows:
                stocks.append(
                    {
                        "code": row[0],
                        "name": row[1],
                        "change": round(row[2], 2) if row[2] else 0,
                        "boards": row[3] or 0,
                        "industry": row[4] or "",
                        "today_change": round(row[5], 2) if row[5] else 0,
                        "turnover": round(row[6], 1) if row[6] else 0,
                        "vol_ratio": round(row[7], 1) if row[7] else 0,
                        "amplitude": round(row[8], 1) if row[8] else 0,
                        "mcap": round(row[9], 1) if row[9] else 0,
                        "circ_mcap": round(row[10], 1) if row[10] else 0,
                        "mf_wan": round(row[11], 1) if row[11] else 0,
                        "mf_ratio": round(row[12], 2) if row[12] else 0,
                        "price": round(row[13], 2) if row[13] else 0,
                    }
                )

            logger.info(f"✅ 查询昨日涨停表现成功：{trade_date} - {len(stocks)}只")
            return {
                "trade_date": trade_date,
                "total": len(stocks),
                "stocks": stocks,
                "error": None,
            }

        except Exception as e:
            logger.error(f"❌ 查询昨日涨停表现失败：{e}")
            return {"trade_date": trade_date, "total": 0, "stocks": [], "error": str(e)}

    def get_unusual_stocks(self, trade_date: str = None, limit: int = 30) -> Dict:
        """
        查询某日异动股全量明细（涨>5%或主力净流入>5000万，不含ST/688）

        Returns:
            {"trade_date": "2026-05-20", "total": 40, "stocks": [...]}
        """
        try:
            conn = connect(self.db_path)
            cursor = conn.cursor()

            if not trade_date:
                cursor.execute("SELECT MAX(trade_date) FROM stock_basic")
                trade_date = cursor.fetchone()[0]

            cursor.execute(
                """
                SELECT s.stock_code, s.stock_name, s.change_pct,
                       s.total_market_cap/100000000 as mcap,
                       s.circ_market_cap/100000000 as circ_mcap,
                       s.main_force_net/10000 as mf_wan,
                       s.main_force_ratio, s.turnover_rate, s.volume_ratio,
                       s.amplitude, s.industry, s.price,
                       COALESCE(l.consecutive_boards, 0) as cons_boards,
                       lhb.net_inflow/100000000 as lhb_net_yi
                FROM stock_basic s
                LEFT JOIN limit_pool l ON s.stock_code = l.stock_code
                    AND s.trade_date = l.trade_date AND l.pool_type = '涨停'
                LEFT JOIN lhb_stocks lhb ON s.stock_code = lhb.stock_code
                    AND s.trade_date = lhb.trade_date
                WHERE s.trade_date = ?
                    AND s.stock_name NOT LIKE '%ST%'
                    AND s.stock_code NOT LIKE '688%'
                    AND (s.change_pct > 5 OR s.main_force_net > 50000000)
                ORDER BY s.change_pct DESC
                LIMIT ?
            """,
                (trade_date, limit),
            )

            rows = cursor.fetchall()
            conn.close()

            stocks = []
            for row in rows:
                stocks.append(
                    {
                        "code": row[0],
                        "name": row[1],
                        "change": round(row[2], 2) if row[2] else 0,
                        "mcap": round(row[3], 1) if row[3] else 0,
                        "circ_mcap": round(row[4], 1) if row[4] else 0,
                        "mf_wan": round(row[5], 1) if row[5] else 0,
                        "mf_ratio": round(row[6], 2) if row[6] else 0,
                        "turnover": round(row[7], 1) if row[7] else 0,
                        "vol_ratio": round(row[8], 1) if row[8] else 0,
                        "amplitude": round(row[9], 1) if row[9] else 0,
                        "industry": row[10] or "",
                        "price": round(row[11], 2) if row[11] else 0,
                        "boards": row[12] or 0,
                        "lhb_net_yi": round(row[13], 2) if row[13] else None,
                    }
                )

            logger.info(f"✅ 查询异动股成功：{trade_date} - {len(stocks)}只")
            return {
                "trade_date": trade_date,
                "total": len(stocks),
                "stocks": stocks,
                "error": None,
            }

        except Exception as e:
            logger.error(f"❌ 查询异动股失败：{e}")
            return {"trade_date": trade_date, "total": 0, "stocks": [], "error": str(e)}

    def get_sector_zhongjun(self, sector_code: str = None, sector_name: str = None, trade_date: str = None) -> Dict:
        """
        查询指定板块的中军候选（4维打分：市值30% + 流动性25% + 趋势25% + 相对强度20%）

        返回每只股票的：code, name, score, mcap(亿), turnover_5d(亿), trend_status,
        rel_strength(%), change_pct(%), ma5, ma10, ma20, boards, industry

        使用场景：
        - 在「核心标的拆解」中需要确定某板块的中军标的时，**必须**调用此工具
        - 从得分最高的前3名中选取中军，不要凭感觉选
        - 传入 sector_code（如 BK1036）精确查询
        单次返回 TOP5，约 200-400 token
        """
        try:
            from data.readers.sector_reader import SectorReader

            conn = connect(self.db_path)

            if not trade_date:
                cursor = conn.cursor()
                cursor.execute("SELECT MAX(trade_date) FROM stock_basic")
                trade_date = cursor.fetchone()[0]

            # 解析 sector_code
            resolved_code = sector_code
            resolved_name = sector_name or ""
            if not resolved_code and sector_name:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT sector_code, sector_name FROM sector_info WHERE sector_name = ?",
                    (sector_name,),
                )
                row = cursor.fetchone()
                if row:
                    resolved_code, resolved_name = (
                        row["sector_code"],
                        row["sector_name"],
                    )
                else:
                    conn.close()
                    return {
                        "sector_code": "",
                        "sector_name": sector_name,
                        "trade_date": trade_date,
                        "stocks": [],
                        "error": f"未找到板块：{sector_name}",
                    }

            if not resolved_code:
                conn.close()
                return {
                    "sector_code": "",
                    "sector_name": "",
                    "trade_date": trade_date,
                    "stocks": [],
                    "error": "请提供 sector_code 或 sector_name",
                }

            # 判断板块类型
            cursor = conn.cursor()
            cursor.execute(
                "SELECT sector_code FROM sector_industry WHERE sector_code = ? AND trade_date = ?",
                (resolved_code, trade_date),
            )
            sector_table = "sector_industry" if cursor.fetchone() else "sector_concept"

            result = SectorReader.get_sector_zhongjun(conn, trade_date, [resolved_code], sector_table, top_n=5)

            conn.close()

            stocks = result.get(resolved_code, [])
            sector_name_out = stocks[0].get("sector_name", resolved_name) if stocks else resolved_name

            logger.info(f"✅ 中军候选查询成功：{sector_name_out}({resolved_code}) - {len(stocks)}只")
            return {
                "sector_code": resolved_code,
                "sector_name": sector_name_out,
                "trade_date": trade_date,
                "total": len(stocks),
                "stocks": [
                    {
                        "code": s["code"],
                        "name": s["name"],
                        "score": round(s["score"], 0),
                        "mcap": round(s["mcap"], 0),
                        "turnover_5d": round(s["avg_turnover_5d"], 1),
                        "trend": s.get("trend_status", ""),
                        "rel_strength": round(s["rel_strength"], 1),
                        "change": round(s["change_pct"], 1),
                        "boards": s.get("boards", 0),
                        "industry": s.get("industry", ""),
                        "ma5": round(s.get("ma5", 0), 2),
                        "ma10": round(s.get("ma10", 0), 2),
                        "ma20": round(s.get("ma20", 0), 2),
                    }
                    for s in stocks
                ],
                "error": None,
            }

        except Exception as e:
            logger.error(f"❌ 查询中军候选失败：{e}")
            return {
                "sector_code": sector_code or "",
                "sector_name": sector_name or "",
                "trade_date": trade_date,
                "total": 0,
                "stocks": [],
                "error": str(e),
            }

    def get_hotspot_stocks(
        self,
        sector_name: str = None,
        sector_code: str = None,
        trade_date: str = None,
        limit: int = 15,
    ) -> Dict:
        """
        查询指定板块在某日的成分股明细（按涨幅排序）

        Args:
            sector_name: 板块名称，如 '半导体'
            sector_code: 板块编码，如 'BK1036'（优先使用）
            trade_date: 交易日期，默认最新

        Returns:
            {"sector_name": "半导体", "sector_code": "BK1036",
             "trade_date": "2026-05-20", "stocks": [...]}
        """
        try:
            conn = connect(self.db_path)
            cursor = conn.cursor()

            if not trade_date:
                cursor.execute("SELECT MAX(trade_date) FROM stock_basic")
                trade_date = cursor.fetchone()[0]

            # 解析 sector_code
            if sector_code:
                resolved_code = sector_code
                resolved_name = sector_name or sector_code
            elif sector_name:
                cursor.execute(
                    "SELECT sector_code, sector_name FROM sector_info WHERE sector_name = ?",
                    (sector_name,),
                )
                row = cursor.fetchone()
                if row:
                    resolved_code, resolved_name = row
                else:
                    conn.close()
                    return {
                        "sector_name": sector_name,
                        "sector_code": "",
                        "trade_date": trade_date,
                        "stocks": [],
                        "error": f"未找到板块：{sector_name}",
                    }
            else:
                conn.close()
                return {
                    "sector_name": "",
                    "sector_code": "",
                    "trade_date": trade_date,
                    "stocks": [],
                    "error": "请提供 sector_name 或 sector_code",
                }

            # 查询板块成分股+行情
            cursor.execute(
                """
                SELECT sb.stock_code, sb.stock_name, sb.change_pct,
                       sb.total_market_cap/100000000 as mcap,
                       sb.circ_market_cap/100000000 as circ_mcap,
                       sb.main_force_net/10000 as mf_wan,
                       sb.main_force_ratio, sb.turnover_rate, sb.volume_ratio,
                       sb.amplitude, sb.price,
                       COALESCE(l.consecutive_boards, 0) as boards,
                       COALESCE(l.first_seal_time, '') as seal_time
                FROM sector_stocks ss
                JOIN stock_basic sb ON ss.stock_code = sb.stock_code
                    AND sb.trade_date = ?
                LEFT JOIN limit_pool l ON sb.stock_code = l.stock_code
                    AND sb.trade_date = l.trade_date AND l.pool_type = '涨停'
                WHERE ss.sector_code = ?
                ORDER BY sb.change_pct DESC
                LIMIT ?
            """,
                (trade_date, resolved_code, limit),
            )

            rows = cursor.fetchall()
            conn.close()

            stocks = []
            for row in rows:
                stocks.append(
                    {
                        "code": row[0],
                        "name": row[1],
                        "change": round(row[2], 2) if row[2] else 0,
                        "mcap": round(row[3], 1) if row[3] else 0,
                        "circ_mcap": round(row[4], 1) if row[4] else 0,
                        "mf_wan": round(row[5], 1) if row[5] else 0,
                        "mf_ratio": round(row[6], 2) if row[6] else 0,
                        "turnover": round(row[7], 1) if row[7] else 0,
                        "vol_ratio": round(row[8], 1) if row[8] else 0,
                        "amplitude": round(row[9], 1) if row[9] else 0,
                        "price": round(row[10], 2) if row[10] else 0,
                        "boards": row[11] or 0,
                        "seal_time": row[12] or "",
                    }
                )

            logger.info(f"✅ 查询热点板块个股成功：{resolved_name} - {len(stocks)}只")
            return {
                "sector_name": resolved_name,
                "sector_code": resolved_code,
                "trade_date": trade_date,
                "stocks": stocks,
                "error": None,
            }

        except Exception as e:
            logger.error(f"❌ 查询热点板块个股失败：{e}")
            return {
                "sector_name": sector_name or "",
                "sector_code": sector_code or "",
                "trade_date": trade_date,
                "stocks": [],
                "error": str(e),
            }

    # ============================================================
    # 新闻读取工具（读落盘文件，不查 DB）
    # ============================================================

    def get_cls_digest_news(self, trade_date: str = None) -> Dict:
        """
        读取财联社复盘新闻（焦点复盘 + 每日收评）

        数据来源：storage/logs/{date}/collectors/cls_digest.json
        由采集阶段落盘，AI 复盘时必须调用此工具获取。
        """
        import json as _json
        from datetime import datetime as _dt

        from system.config.settings import LOGS_DIR

        if trade_date is None:
            trade_date = _dt.now().strftime("%Y-%m-%d")

        file_path = LOGS_DIR / trade_date / "collectors" / "cls_digest.json"
        try:
            if not file_path.exists():
                return {
                    "trade_date": trade_date,
                    "error": f"新闻文件不存在: {file_path}",
                }

            with open(file_path, encoding="utf-8") as f:
                data = _json.load(f)

            # 返回结构化的摘要，避免 token 爆炸
            result = {"trade_date": trade_date, "sections": {}}
            for key in ("focus_review", "daily_review"):
                section = data.get(key, {})
                if section:
                    content = section.get("content", "")
                    result["sections"][key] = {
                        "title": section.get("title", ""),
                        "time": section.get("time", ""),
                        "source": section.get("source", ""),
                        "word_count": len(content),
                        "content": content,
                    }

            if not result["sections"]:
                result["warning"] = "新闻数据为空，可能采集失败"
            else:
                total_chars = sum(s.get("word_count", 0) for s in result["sections"].values())
                result["summary"] = f"共 {len(result['sections'])} 篇，约 {total_chars} 字"

            logger.info(f"✅ 读取 CLS 复盘新闻成功：{result.get('summary', '空')}")
            return result

        except Exception as e:
            logger.error(f"❌ 读取 CLS 复盘新闻失败：{e}")
            return {"trade_date": trade_date, "error": str(e)}

    def search_stock(self, name: str) -> Dict:
        """
        根据股票简称模糊搜索股票代码。

        在 stock_basic 表中 LIKE 匹配，返回候选列表供 AI 精选。
        """
        try:
            conn = connect(self.db_path)

            like = f"%{name}%"
            cursor = conn.execute(
                """
                SELECT DISTINCT stock_code, stock_name FROM stock_basic
                WHERE stock_name LIKE ? LIMIT 20
            """,
                (like,),
            )
            results = [dict(r) for r in cursor.fetchall()]
            conn.close()
            logger.info(f"search_stock('{name}') → {len(results)} 个候选")
            return {"name": name, "candidates": results, "count": len(results)}
        except Exception as e:
            logger.error(f"search_stock('{name}') 失败: {e}")
            return {"name": name, "candidates": [], "count": 0, "error": str(e)}

    def search_sector(self, keyword: str) -> Dict:
        """
        根据板块关键词模糊搜索板块编码。

        同时查询 sector_info（行业板块）和 sector_concept（概念板块），
        返回候选列表（含 sector_name、sector_code、type）供 AI 精选。
        """
        try:
            conn = connect(self.db_path)

            like = f"%{keyword}%"
            cursor = conn.execute(
                """
                SELECT DISTINCT sector_code, sector_name, 'industry' as type FROM sector_info
                WHERE sector_name LIKE ?
                UNION
                SELECT DISTINCT sector_code, sector_name, 'concept' as type FROM sector_concept
                WHERE sector_name LIKE ?
                LIMIT 20
            """,
                (like, like),
            )
            results = [dict(r) for r in cursor.fetchall()]
            conn.close()
            logger.info(f"search_sector('{keyword}') → {len(results)} 个候选")
            return {"keyword": keyword, "candidates": results, "count": len(results)}
        except Exception as e:
            logger.error(f"search_sector('{keyword}') 失败: {e}")
            return {"keyword": keyword, "candidates": [], "count": 0, "error": str(e)}

    def get_telegraph_news(self, stock_code: str, trade_date: str = None) -> Dict:
        """查询某只股票在今日电报中的相关新闻（通过 CLS stock_tags 匹配）"""
        import json as _json
        from datetime import datetime as _dt

        if trade_date is None:
            trade_date = _dt.now().strftime("%Y-%m-%d")

        try:
            conn = connect(self.db_path)

            cursor = conn.execute(
                """
                SELECT * FROM cls_telegraph
                WHERE trade_date = ?
                  AND stock_tags LIKE ?
                ORDER BY score DESC, reading_num DESC
                LIMIT 200
            """,
                (trade_date, f'%"{stock_code}"%'),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()

            # 过滤 + 格式化
            matched = []
            seen_titles = set()
            for r in rows:
                title = r.get("title", "")
                title_key = title[:20]
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                # 解析 stock_tags + plate_tags
                tags = []
                stock_tags_raw = r.get("stock_tags")
                with contextlib.suppress(_json.JSONDecodeError, TypeError):
                    tags = _json.loads(stock_tags_raw) if stock_tags_raw else []

                plate_tags_raw = r.get("plate_tags")
                plates = []
                with contextlib.suppress(_json.JSONDecodeError, TypeError):
                    plates = _json.loads(plate_tags_raw) if plate_tags_raw else []

                matched.append(
                    {
                        "level": r.get("level", "C"),
                        "category": r.get("category", ""),
                        "title": title,
                        "content": (r.get("content") or "")[:200],
                        "reading_num": r.get("reading_num", 0),
                        "score": r.get("score", 0),
                        "plates": plates,
                        "stock_tags": tags,
                    }
                )

            if matched:
                logger.info(f"查询 {stock_code} 电报：{len(matched)} 条匹配")
                return {
                    "stock_code": stock_code,
                    "trade_date": trade_date,
                    "has_news": True,
                    "count": len(matched),
                    "items": matched,
                }
            else:
                logger.info(f"查询 {stock_code} 电报：无相关新闻")
                return {
                    "stock_code": stock_code,
                    "trade_date": trade_date,
                    "has_news": False,
                    "message": "该股今日无电报新闻",
                    "items": [],
                }

        except Exception as e:
            logger.error(f"查询电报失败：{e}")
            return {
                "stock_code": stock_code,
                "trade_date": trade_date,
                "has_news": False,
                "error": str(e),
                "items": [],
            }

    # ============================================================
    # 自我校准工具（读取昨日复盘报告 + 推荐表现 + 历史统计）
    # ============================================================

    def get_yesterday_review(self, trade_date: str = None) -> Dict:
        """读取昨日复盘报告全文"""
        from datetime import datetime as _dt

        if trade_date is None:
            trade_date = _dt.now().strftime("%Y-%m-%d")

        from system.config.trading_calendar import get_previous_trading_day

        yesterday = get_previous_trading_day(trade_date)
        if not yesterday:
            return {
                "trade_date": trade_date,
                "yesterday_date": None,
                "has_report": False,
                "content": "",
                "word_count": 0,
                "error": "无法确定上一个交易日",
            }

        reports_dir = STORAGE_PATH / "reports"
        matches = sorted(reports_dir.glob(f"review_reports_{yesterday}_*.txt"))
        if not matches:
            return {
                "trade_date": trade_date,
                "yesterday_date": yesterday,
                "has_report": False,
                "content": "",
                "word_count": 0,
                "error": f"未找到 {yesterday} 的复盘报告",
            }

        content = matches[-1].read_text(encoding="utf-8")
        logger.info(f"✅ 读取昨日复盘报告：{len(content)}字")
        return {
            "trade_date": trade_date,
            "yesterday_date": yesterday,
            "has_report": True,
            "content": content,
            "word_count": len(content),
            "error": None,
        }

    def get_yesterday_picks_performance(self, trade_date: str = None) -> Dict:
        """查询昨日 AI 推荐的每只标的今日行情表现"""
        from datetime import datetime as _dt

        if trade_date is None:
            trade_date = _dt.now().strftime("%Y-%m-%d")

        from system.config.trading_calendar import get_previous_trading_day

        yesterday = get_previous_trading_day(trade_date)
        if not yesterday:
            return {
                "trade_date": trade_date,
                "yesterday_date": None,
                "total": 0,
                "stocks": [],
                "error": "无法确定上一个交易日",
            }

        try:
            conn = connect(self.db_path)

            cursor = conn.execute(
                """
                SELECT t.stock_code, t.stock_name, t.plate, t.star_rating,
                       s.change_pct as today_change,
                       s.turnover_rate, s.volume_ratio, s.amplitude,
                       s.total_market_cap/100000000 as mcap,
                       s.main_force_net/10000 as mf_wan,
                       s.main_force_ratio,
                       COALESCE(lp.pool_type, '') as limit_type
                FROM stock_tracker t
                LEFT JOIN stock_basic s ON t.stock_code = s.stock_code
                    AND s.trade_date = ?
                LEFT JOIN limit_pool lp ON t.stock_code = lp.stock_code
                    AND lp.trade_date = ? AND lp.pool_type = '涨停'
                WHERE t.push_date = ? AND t.source = '复盘'
                ORDER BY t.star_rating DESC, COALESCE(s.change_pct, -999) DESC
            """,
                (trade_date, trade_date, yesterday),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()

            stocks = []
            for r in rows:
                star = r["star_rating"] or 0
                star_label = {5: "P0", 4: "P1", 3: "P2"}.get(star, "P3")
                chg = r["today_change"]
                stocks.append(
                    {
                        "code": r["stock_code"],
                        "name": r["stock_name"],
                        "plate": r["plate"] or "",
                        "star": star_label,
                        "today_change": round(chg, 2) if chg is not None else None,
                        "turnover": round(r["turnover_rate"] or 0, 1),
                        "vol_ratio": round(r["volume_ratio"] or 0, 1),
                        "amplitude": round(r["amplitude"] or 0, 1),
                        "mcap": round(r["mcap"] or 0, 1),
                        "mf_wan": round(r["mf_wan"] or 0, 1),
                        "mf_ratio": round(r["main_force_ratio"] or 0, 2),
                        "is_limit_up": r["limit_type"] == "涨停",
                    }
                )

            avg_chg = sum(s["today_change"] for s in stocks if s["today_change"] is not None)
            valid = sum(1 for s in stocks if s["today_change"] is not None)
            win_count = sum(1 for s in stocks if (s["today_change"] or 0) > 0)

            logger.info(f"✅ 查询昨日推荐表现：{len(stocks)}只，平均{avg_chg / valid:+.2f}%" if valid > 0 else "无数据")
            return {
                "trade_date": trade_date,
                "yesterday_date": yesterday,
                "total": len(stocks),
                "avg_change": round(avg_chg / valid, 2) if valid else 0,
                "win_count": win_count,
                "stocks": stocks,
                "error": None,
            }

        except Exception as e:
            logger.error(f"❌ 查询昨日推荐表现失败：{e}")
            return {"trade_date": trade_date, "total": 0, "stocks": [], "error": str(e)}

    def get_historical_calibration(self, trade_date: str = None) -> Dict:
        """
        查询最近 5 个交易日的 AI 推荐校准统计（v4.0 诊断仪表盘）

        返回数据包含角色/板块诊断信号，帮助 AI 识别自己在哪些领域持续亏钱。
        """
        from datetime import datetime as _dt

        if trade_date is None:
            trade_date = _dt.now().strftime("%Y-%m-%d")

        from system.config.trading_calendar import get_recent_trading_days

        recent_days = get_recent_trading_days(trade_date, 5)
        if not recent_days:
            return {
                "date_range": "",
                "num_days": 0,
                "total": 0,
                "win_rate": 0,
                "error": "无历史交易日数据",
            }

        try:
            conn = connect(self.db_path)

            ph = ",".join("?" * len(recent_days))
            cursor = conn.execute(
                f"""
                SELECT t.stock_code, t.stock_name, t.plate, t.star_rating,
                       t.final_return, t.push_date
                FROM stock_tracker t
                WHERE t.push_date IN ({ph}) AND t.source = '复盘'
                  AND t.final_return IS NOT NULL
                ORDER BY t.push_date DESC, t.star_rating DESC
            """,
                recent_days,
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()

            if not rows:
                return {
                    "date_range": f"{recent_days[-1]}~{recent_days[0]}",
                    "num_days": len(recent_days),
                    "total": 0,
                    "win_rate": 0,
                    "error": "无历史推荐数据",
                }

            total = len(rows)
            wins = sum(1 for r in rows if (r["final_return"] or 0) > 0)
            avg_return = sum(r["final_return"] or 0 for r in rows) / total

            # 盈亏比
            returns = [r["final_return"] or 0 for r in rows]
            gains = [r for r in returns if r > 0]
            losses = [abs(r) for r in returns if r < 0]
            avg_gain = sum(gains) / len(gains) if gains else 0
            avg_loss = sum(losses) / len(losses) if losses else 1
            profit_loss_ratio = round(avg_gain / avg_loss, 2) if avg_loss > 0 else 0
            max_win = round(max(returns), 2) if returns else 0
            max_loss = round(min(returns), 2) if returns else 0

            # 按角色分组 + 诊断信号
            role_labels = {
                "P0": "龙头/破局者",
                "P1": "中军",
                "P2": "补涨/次线龙头",
                "P3": "其他",
            }
            by_priority = {}
            for r in rows:
                star = r["star_rating"] or 0
                label = {5: "P0", 4: "P1", 3: "P2"}.get(star, "P3")
                if label not in by_priority:
                    by_priority[label] = {"count": 0, "wins": 0, "total_return": 0}
                by_priority[label]["count"] += 1
                ret = r["final_return"] or 0
                by_priority[label]["total_return"] += ret
                if ret > 0:
                    by_priority[label]["wins"] += 1

            for label, p in by_priority.items():
                p["avg_return"] = round(p["total_return"] / p["count"], 2)
                p["win_rate"] = round(p["wins"] / p["count"] * 100, 1)
                # 诊断信号
                wr = p["win_rate"]
                if wr < 30:
                    p["signal"] = "🔴 这个角色持续亏钱，今天不要用同样的思路选"
                elif wr < 45:
                    p["signal"] = "🟡 胜率偏低，选的时候多想想为什么"
                elif wr >= 60:
                    p["signal"] = "🟢 这是你的强项，可以适度加配"
                else:
                    p["signal"] = "⚪ 胜率一般，按正常标准选即可"
                p["role_name"] = role_labels.get(label, label)

            # 按板块分组 + 诊断信号
            by_sector = {}
            for r in rows:
                plate = r["plate"] or "未分类"
                if plate not in by_sector:
                    by_sector[plate] = {"count": 0, "wins": 0, "total_return": 0}
                by_sector[plate]["count"] += 1
                ret = r["final_return"] or 0
                by_sector[plate]["total_return"] += ret
                if ret > 0:
                    by_sector[plate]["wins"] += 1

            sector_stats = []
            for plate, s in by_sector.items():
                avg_r = round(s["total_return"] / s["count"], 2)
                wr = round(s["wins"] / s["count"] * 100, 1)
                if avg_r > 1.0 and s["count"] >= 2:
                    signal = "🟢 持续赚钱的板块，有 edge"
                elif avg_r < -1.0 and s["count"] >= 2:
                    signal = "🔴 持续亏钱的板块——今天不要推这个板块，你只是在追涨"
                else:
                    signal = "⚪ 信号不明确"
                sector_stats.append(
                    {
                        "plate": plate,
                        "count": s["count"],
                        "avg_return": avg_r,
                        "win_rate": wr,
                        "signal": signal,
                    }
                )
            sector_stats.sort(key=lambda x: x["avg_return"], reverse=True)

            # 整体诊断
            if avg_return < 0:
                overall_signal = (
                    "🔴 平均收益为负——赚的没有亏的多（盈亏比<1）。今天给每只标的更紧的止损，放弃赌一把的票。"  # noqa: E501
                )
            elif profit_loss_ratio < 50:
                overall_signal = "⚠️ 胜率低于50%，说明你的选股偏向追涨而非预判。今天少选涨停板、少选已连板的票、多从趋势股中找蓄力期品种。"  # noqa: E501
            elif profit_loss_ratio < 50:
                overall_signal = "⚠️ 胜率不足50%，勉强打平或微亏。控制推荐数量，只在最有把握的方向出票。"
            else:
                overall_signal = "整体胜率尚可，按正常标准执行。"

            logger.info(f"✅ 历史校准：{total}只，胜率{round(wins / total * 100, 1)}%，平均{avg_return:+.2f}%")
            return {
                "date_range": f"{recent_days[-1]}~{recent_days[0]}",
                "num_days": len(recent_days),
                "total": total,
                "wins": wins,
                "win_rate": round(wins / total * 100, 1),
                "avg_return": round(avg_return, 2),
                "profit_loss_ratio": profit_loss_ratio,
                "max_win": max_win,
                "max_loss": max_loss,
                "overall_signal": overall_signal,
                "by_priority": by_priority,
                "by_sector": sector_stats,
                "error": None,
            }

        except Exception as e:
            logger.error(f"❌ 历史校准查询失败：{e}")
            return {
                "date_range": "",
                "num_days": 0,
                "total": 0,
                "win_rate": 0,
                "error": str(e),
            }

    def get_prediction_accuracy(self, trade_date: str = None) -> Dict:
        """
        查询复盘预测准确率——对比历史预测 vs 次日实际表现。

        返回：指数预测准确率、板块预测准确率、情景符合率。
        """
        from datetime import datetime as _dt

        if trade_date is None:
            trade_date = _dt.now().strftime("%Y-%m-%d")

        try:
            conn = connect(self.db_path)

            # 已核对的预测
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT pred_type, target_name, pred_direction, prob, is_correct FROM review_predictions WHERE push_date < ? AND is_correct IS NOT NULL ORDER BY push_date DESC LIMIT 100",  # noqa: E501
                    (trade_date,),
                ).fetchall()
            ]
            conn.close()

            if not rows:
                return {"total_checked": 0, "by_type": {}, "error": None}

            by_type = {}
            for r in rows:
                pt = r["pred_type"]
                if pt not in by_type:
                    by_type[pt] = {"total": 0, "correct": 0, "correct_pct": 0}
                by_type[pt]["total"] += 1
                if r["is_correct"]:
                    by_type[pt]["correct"] += 1

            for _pt, s in by_type.items():
                s["correct_pct"] = round(s["correct"] / s["total"] * 100, 1)

            total = len(rows)
            correct = sum(1 for r in rows if r["is_correct"])
            overall = round(correct / total * 100, 1) if total > 0 else 0

            # 生成校准提示
            signals = []
            for _pt, s in by_type.items():
                label = {"index": "指数", "sector": "板块", "scenario": "情景"}.get(pt, pt)
                if s["correct_pct"] < 40:
                    signals.append(f"🔴 {label}预测准确率{s['correct_pct']}%——偏差大，需重新审视判断方法")
                elif s["correct_pct"] < 60:
                    signals.append(f"🟡 {label}预测准确率{s['correct_pct']}%——勉强可用，还有提升空间")
                else:
                    signals.append(f"🟢 {label}预测准确率{s['correct_pct']}%——判断靠谱")

            logger.info(f"✅ 预测准确率：总{overall}%")
            return {
                "total_checked": total,
                "overall_pct": overall,
                "signals": signals,
                "by_type": by_type,
                "error": None,
            }
        except Exception as e:
            logger.error(f"❌ 预测准确率查询失败：{e}")
            return {"total_checked": 0, "by_type": {}, "error": str(e)}

    def get_learning_lessons(self) -> Dict:
        """
        获取历史经验教训库（按类型分组，同类问题合并强化）

        返回所有活跃教训，AI 据此规避重复犯错。
        """
        try:
            conn = connect(self.db_path)

            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT lesson_type, lesson_key, lesson_content, occurrence_count, first_date, last_date FROM review_lessons WHERE is_active=1 ORDER BY lesson_type, occurrence_count DESC"  # noqa: E501
                ).fetchall()
            ]
            conn.close()

            if not rows:
                return {"total": 0, "by_type": {}, "error": None}

            by_type = {}
            for r in rows:
                lt = r["lesson_type"]
                if lt not in by_type:
                    by_type[lt] = []
                by_type[lt].append(
                    {
                        "key": r["lesson_key"],
                        "lesson": r["lesson_content"],
                        "occurrences": r["occurrence_count"],
                        "first": r["first_date"],
                        "last": r["last_date"],
                        # 高频重复=严重警告
                        "severity": "🔴 屡犯不改"
                        if r["occurrence_count"] >= 3
                        else ("🟡 重复出现" if r["occurrence_count"] >= 2 else "🆕 新发现"),
                    }
                )

            logger.info(f"✅ 经验教训库：{len(rows)}条，{len(by_type)}个类别")
            return {"total": len(rows), "by_type": by_type, "error": None}

        except Exception as e:
            logger.error(f"❌ 经验教训查询失败：{e}")
            return {"total": 0, "by_type": {}, "error": str(e)}

    def get_pending_signals(self, trade_date: str = None) -> Dict:
        """
        获取昨日所有 pending 状态的买入信号清单（复盘精选 + 趋势筛选）。

        返回每只标的的：code, name, buy_zone, stop_loss, take_profit, score, source, reason。
        早盘校准用——AI 据此判断哪些标的需修正/移除。
        """
        try:
            if trade_date is None:
                from datetime import datetime as _dt

                from system.config.trading_calendar import get_previous_trading_day

                trade_date = get_previous_trading_day(_dt.now().strftime("%Y-%m-%d"))

            conn = connect(self.db_path)

            rows = conn.execute(
                """
                SELECT stock_code, stock_name, signal_source, buy_zone_min, buy_zone_max,
                       stop_loss, take_profit, signal_score, reason, strategy_name
                FROM trade_signals
                WHERE trade_date = ? AND status = 'pending'
                ORDER BY signal_source, signal_score DESC
            """,
                (trade_date,),
            ).fetchall()
            conn.close()

            signals = []
            for r in rows:
                signals.append(
                    {
                        "stock_code": r["stock_code"],
                        "stock_name": r["stock_name"],
                        "source": r["signal_source"],
                        "buy_zone": f"{r['buy_zone_min']}-{r['buy_zone_max']}" if r["buy_zone_min"] else "",
                        "stop_loss": r["stop_loss"],
                        "take_profit": r["take_profit"],
                        "score": r["signal_score"],
                        "reason": r["reason"][:80] if r["reason"] else "",
                    }
                )

            logger.info(f"✅ pending 信号查询：{len(signals)} 只 ({trade_date})")
            return {
                "trade_date": trade_date,
                "total": len(signals),
                "signals": signals,
                "error": None,
            }

        except Exception as e:
            logger.error(f"❌ pending 信号查询失败：{e}")
            return {
                "trade_date": trade_date,
                "total": 0,
                "signals": [],
                "error": str(e),
            }


# 工具定义（用于注册到 AI）
# 符合 OpenAI Function Calling 格式
# 注意：复盘 5 必修 + 批量预计算工具（get_cls_digest_news 等）已从定义中移除，
# 其数据在 AI 调用前本地预计算并嵌入 prompt，无需 FC。
TOOLS_DEFINITION = [
    {
        "type": "function",
        "function": {
            "name": "search_stock",
            "description": "根据股票简称模糊搜索6位股票代码。传入名称（如'宝鼎科技'），返回候选列表（含code和name）。用于电报AI结构化时精确匹配个股代码。",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "股票简称，如'宝鼎科技'、'中芯国际'",
                    }
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_sector",
            "description": "根据板块关键词模糊搜索板块编码。传入板块名（如'存储芯片'、'PCB'），在sector_info和sector_concept中匹配，返回候选列表（含sector_name、sector_code、type）。用于电报AI结构化时精确匹配板块编码。",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "板块关键词，如'存储芯片'、'光模块'、'PCB'",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_cap",
            "description": "查询股票实时市值（单位：亿）。返回字段：code, name, market_cap(亿), update_time。\n\n使用场景：\n- 在「核心标的拆解」中，需要确认某只股票的市值规模（中军 vs 小盘弹性票）时使用\n- 当你需要区分市值梯队、判断标的是否为容量中军时使用\n\n单次返回 1 只股票数据，约 80 token",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "stock_code": {
                        "type": "string",
                        "description": "6 位股票代码，如 '688702'、'002207'",
                    }
                },
                "required": ["stock_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_info",
            "description": "查询股票完整信息。返回字段：code, name, industry, concept, market_cap(亿), price, change_pct, update_time。\n\n使用场景：\n- 在「核心标的拆解」中，需要查看某只股票的行业归属和概念标签时使用\n- 当正文数据中某只股票的信息不完整时使用\n\n单次返回 1 只股票数据，约 100 token",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "stock_code": {
                        "type": "string",
                        "description": "6 位股票代码，如 '688702'、'002207'",
                    }
                },
                "required": ["stock_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sector_stocks",
            "description": "查询板块成分股（按涨幅排序）。返回每只股票的：code, name, industry, concept, market_cap(亿), price, change_pct。\n\n使用场景：\n- 当正文热点数据只展示了 TOP5 板块的个股明细，你想查看其他板块的个股时使用\n- 当你对某个板块的个股分布有疑问，需要查看完整名单时使用\n- 参数 limit 控制返回数量，默认 10 只\n\n每次调用返回 limit 条，约 50 token/股",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "sector_name": {
                        "type": "string",
                        "description": "板块名称，如 '半导体'、'证券'、'AI 芯片'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量限制，默认 10",
                        "default": 10,
                    },
                },
                "required": ["sector_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_yesterday_limit_ups",
            "description": "查询昨日涨停股今日表现的全量明细。返回每只股票的：code, name, change(%), boards, industry, today_change, turnover, vol_ratio, amplitude, mcap(亿), circ_mcap(亿), mf_wan(万), mf_ratio, price。\n\n使用场景：\n- 正文只展示了连板成功+亏损TOP10，如需查看全部昨日涨停股表现，调用此工具获取全量\n- 需要分析昨日涨停股的板块分布统计时，调用此工具获取全量数据自行统计\n- 在「风险提示」章节，如需更详细分析亏钱效应分布\n\n默认返回 50 只，约 500-800 token，用 limit 控制",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {
                        "type": "string",
                        "description": "交易日期 YYYY-MM-DD，默认当日",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量限制，默认 50",
                        "default": 50,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_hotspot_stocks",
            "description": "查询指定板块的完整成分股行情明细（按涨幅排序）。返回每只股票的：code, name, change, mcap(亿), circ_mcap(亿), mf_wan(万), mf_ratio, turnover, vol_ratio, amplitude, price, boards, seal_time。\n\n使用场景：\n- 正文只展示了 TOP5 板块的个股明细（每板块约 10 只），如需查看排名第 6-10 名的板块个股，调用此工具\n- 需要分析某个非 TOP 板块是否有潜伏价值时\n- 传入 sector_code（如 BK1036）精确查询，或 sector_name 模糊匹配\n\n默认返回 15 只，约 300-600 token",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "sector_name": {
                        "type": "string",
                        "description": "板块名称，如 '半导体'、'液冷概念'。与 sector_code 二选一",
                    },
                    "sector_code": {
                        "type": "string",
                        "description": "板块编码，如 'BK1036'。优先使用",
                    },
                    "trade_date": {
                        "type": "string",
                        "description": "交易日期 YYYY-MM-DD，默认当日",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_unusual_stocks",
            "description": "查询某日异动股全量明细（涨>5%或主力净流入>5000万）。返回每只股票的：code, name, change, mcap(亿), circ_mcap(亿), mf_wan(万), mf_ratio, turnover, vol_ratio, amplitude, industry, price, boards, lhb_net_yi(亿)。\n\n使用场景：\n- 正文只展示了主板 20 只+创业板 20 只（最多 40 只），实际异动可能有 60-80 只，调用此工具获取全量\n- 在「核心标的拆解」中，想从更多个股中筛选被遗漏的潜在标的时使用\n- 需要按板块统计异动分布时使用\n\n默认返回 30 只，约 450-750 token，用 limit 调整",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {
                        "type": "string",
                        "description": "交易日期 YYYY-MM-DD，默认当日",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回数量限制，默认 30",
                        "default": 30,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_prediction_accuracy",
            "description": "查询复盘预测准确率——历史指数预测、板块预测、情景推演的准确率统计。AI 据此了解自己的预测偏差，调整推演方法。",  # noqa: E501
            "parameters": {
                "type": "object",
                "properties": {
                    "trade_date": {
                        "type": "string",
                        "description": "交易日期 YYYY-MM-DD，默认当日",
                    }
                },
                "required": [],
            },
        },
    },
]


# 工具函数映射（懒加载，避免导入时实例化）
_tools_instance = None


def _get_tools():
    global _tools_instance
    if _tools_instance is None:
        _tools_instance = StockTools()
    return _tools_instance


TOOL_FUNCTIONS = {
    "get_cls_digest_news": lambda **kw: _get_tools().get_cls_digest_news(**kw),
    "get_telegraph_news": lambda **kw: _get_tools().get_telegraph_news(**kw),
    "search_stock": lambda **kw: _get_tools().search_stock(**kw),
    "search_sector": lambda **kw: _get_tools().search_sector(**kw),
    "get_market_cap": lambda **kw: _get_tools().get_market_cap(**kw),
    "get_stock_info": lambda **kw: _get_tools().get_stock_info(**kw),
    "get_sector_stocks": lambda **kw: _get_tools().get_sector_stocks(**kw),
    "get_lhb_seats": lambda **kw: _get_tools().get_lhb_seats(**kw),
    "get_regulatory_risks": lambda **kw: _get_tools().get_regulatory_risks(**kw),
    "get_yesterday_limit_ups": lambda **kw: _get_tools().get_yesterday_limit_ups(**kw),
    "get_unusual_stocks": lambda **kw: _get_tools().get_unusual_stocks(**kw),
    "get_hotspot_stocks": lambda **kw: _get_tools().get_hotspot_stocks(**kw),
    "get_yesterday_review": lambda **kw: _get_tools().get_yesterday_review(**kw),
    "get_yesterday_picks_performance": lambda **kw: _get_tools().get_yesterday_picks_performance(**kw),
    "get_historical_calibration": lambda **kw: _get_tools().get_historical_calibration(**kw),
    "get_sector_zhongjun": lambda **kw: _get_tools().get_sector_zhongjun(**kw),
    "get_prediction_accuracy": lambda **kw: _get_tools().get_prediction_accuracy(**kw),
    "get_learning_lessons": lambda **kw: _get_tools().get_learning_lessons(**kw),
    "get_pending_signals": lambda **kw: _get_tools().get_pending_signals(**kw),
}
