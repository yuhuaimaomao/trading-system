"""
复盘 AI 分析器 v3.0（刺客风格）

职责：查询原始数据 → 格式化 Prompt → 调用 AI → 返回复盘报告
不做数据采集、不做消息推送

核心改变：程序采集 + 清洗数据，AI 做判断 + 推演。
不给 AI 喂加工过的「得分」，只喂原始数据 + 环比对比，让 AI 自己定性。
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime

import requests

from data._base import connect
from data.readers.limit_pool_reader import LimitPoolReader  # noqa: E402
from data.readers.sector_reader import SectorReader  # noqa: E402
from data.readers.stock_reader import StockReader  # noqa: E402
from data.review.predictions import PredictionRepo  # noqa: E402
from review.review_formatter import (  # noqa: E402
    calc_position_cap,
    fmt_change,
    format_announcements,
    format_broken_boards,
    format_candidates,
    format_capital_concentration,
    format_chain_ladder,
    format_first_boards,
    format_fund_flow,
    format_hotspot,
    format_index_data,
    format_lhb_full,
    format_limit_quality,
    format_macro_overview,
    format_risk_flags,
    format_strong_stocks,
    format_three_day_trend,
    format_yzt_performance,
)
from system.ai import ai  # noqa: E402
from system.ai.function_calling import FunctionCallingEngine  # noqa: E402
from system.ai.prompts.review import REVIEW_REPORT_PROMPT  # noqa: E402
from system.config import settings
from system.config.settings import DATABASE_PATH, LOGS_DIR, STORAGE_PATH
from system.utils.logger import get_review_logger  # noqa: E402


class ReviewAnalyzer:
    """复盘 AI 分析器（刺客风格）"""

    def __init__(self):
        self.logger = get_review_logger("analyzer")
        # 加载板块名称→编码映射（用于 formatter 输出 sector_code）
        self.sector_code_map = {}
        try:
            conn = connect(DATABASE_PATH)
            try:
                cursor = conn.execute("SELECT sector_code, sector_name FROM sector_info")
                for row in cursor.fetchall():
                    self.sector_code_map[row[1]] = row[0]  # name -> code
            finally:
                conn.close()
            self.logger.info(f"已加载 {len(self.sector_code_map)} 个板块编码映射")
        except Exception as e:
            self.logger.warning(f"加载板块编码映射失败: {e}")

    def generate(self, trade_date: str = None, model: str = None) -> str:
        """
        生成复盘报告——所有数据从 DB 读取，新闻实时抓取。

        Args:
            trade_date: 交易日期，默认今天
            model: 覆盖默认模型

        Returns:
            AI 生成的复盘报告文本
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        from system.config.trading_calendar import get_previous_trading_day

        yesterday = get_previous_trading_day(trade_date)
        day_before = get_previous_trading_day(yesterday)
        day_before_before = get_previous_trading_day(day_before)

        self.logger.info(
            f"开始复盘 AI 分析 v3.0（刺客风格）{trade_date}（D-3:{day_before_before}, D-2:{day_before}, D-1:{yesterday}）..."  # noqa: E501
        )
        start_time = time.time()

        # CLS 复盘新闻已在采集阶段落盘（collect() 模块 13），分析阶段直接通过 FC 工具读取
        # 电报由 FC 工具 get_telegraph_news 直接从 DB 查询

        conn = connect(DATABASE_PATH)
        # conn.row_factory = sqlite3.Row  # connect() 已设置

        try:
            # 读取昨日复盘报告（AI 自我校准）
            # 文件名格式 review_reports_{date}_{model}.txt，需 glob 匹配模型后缀
            yesterday_report = ""
            reports_dir = STORAGE_PATH / "reports"
            yesterday_matches = sorted(reports_dir.glob(f"review_reports_{yesterday}_*.txt"))
            if yesterday_matches:
                yesterday_report = yesterday_matches[-1].read_text(encoding="utf-8")
                self.logger.info(f"✅ 已加载昨日复盘报告（{len(yesterday_report)}字）from {yesterday_matches[-1].name}")
            else:
                self.logger.info(f"昨日（{yesterday}）无复盘报告记录")

            # ===== 1. 市场全貌 =====
            self.logger.info("查询市场全貌...")
            cursor = conn.execute(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) as up_count,
                    SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) as down_count,
                    SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) as flat_count,
                    SUM(turnover)/100000000 as turnover,
                    SUM(CASE WHEN change_pct > 5 THEN 1 ELSE 0 END) as up_5pct,
                    SUM(CASE WHEN change_pct < -5 THEN 1 ELSE 0 END) as down_5pct
                FROM stock_basic WHERE trade_date = ?
            """,
                (trade_date,),
            )
            market_row = cursor.fetchone()
            # stock_basic 数据可能为空（采集失败），用默认值兜底防止 AI 分析崩溃
            market = {
                "total": (market_row["total"] or 0) if market_row else 0,
                "up_count": (market_row["up_count"] or 0) if market_row else 0,
                "down_count": (market_row["down_count"] or 0) if market_row else 0,
                "flat_count": (market_row["flat_count"] or 0) if market_row else 0,
                "turnover": (market_row["turnover"] or 0) if market_row else 0,
                "up_5pct": (market_row["up_5pct"] or 0) if market_row else 0,
                "down_5pct": (market_row["down_5pct"] or 0) if market_row else 0,
            }
            up_ratio = market["up_count"] / market["total"] if market["total"] > 0 else 0
            up_down_ratio = f"{market['up_count'] / max(market['down_count'], 1):.2f}"

            # 涨跌幅分布（区间统计）
            cursor = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN change_pct >= 9.9 THEN 1 ELSE 0 END) as limit_up_range,
                    SUM(CASE WHEN change_pct >= 7 AND change_pct < 9.9 THEN 1 ELSE 0 END) as range_7_10,
                    SUM(CASE WHEN change_pct >= 5 AND change_pct < 7 THEN 1 ELSE 0 END) as range_5_7,
                    SUM(CASE WHEN change_pct >= 2 AND change_pct < 5 THEN 1 ELSE 0 END) as range_2_5,
                    SUM(CASE WHEN change_pct >= 0 AND change_pct < 2 THEN 1 ELSE 0 END) as range_0_2,
                    SUM(CASE WHEN change_pct >= -2 AND change_pct < 0 THEN 1 ELSE 0 END) as range_minus2_0,
                    SUM(CASE WHEN change_pct >= -5 AND change_pct < -2 THEN 1 ELSE 0 END) as range_minus5_minus2,
                    SUM(CASE WHEN change_pct >= -10 AND change_pct < -5 THEN 1 ELSE 0 END) as range_minus10_minus5,
                    SUM(CASE WHEN change_pct <= -9.9 THEN 1 ELSE 0 END) as limit_down_range
                FROM stock_basic WHERE trade_date = ?
            """,
                (trade_date,),
            )
            gain_dist = dict(cursor.fetchone())

            def _format_gain_distribution(dist: dict) -> str:
                parts = []
                if dist.get("limit_up_range", 0):
                    parts.append(f"涨停{dist['limit_up_range']}只")
                if dist.get("range_7_10", 0):
                    parts.append(f"7-10%:{dist['range_7_10']}只")
                if dist.get("range_5_7", 0):
                    parts.append(f"5-7%:{dist['range_5_7']}只")
                if dist.get("range_2_5", 0):
                    parts.append(f"2-5%:{dist['range_2_5']}只")
                if dist.get("range_0_2", 0):
                    parts.append(f"0-2%:{dist['range_0_2']}只")
                if dist.get("range_minus2_0", 0):
                    parts.append(f"-2~0%:{dist['range_minus2_0']}只")
                if dist.get("range_minus5_minus2", 0):
                    parts.append(f"-5~-2%:{dist['range_minus5_minus2']}只")
                if dist.get("range_minus10_minus5", 0):
                    parts.append(f"-10~-5%:{dist['range_minus10_minus5']}只")
                if dist.get("limit_down_range", 0):
                    parts.append(f"跌停{dist['limit_down_range']}只")
                return " | ".join(parts) if parts else "无数据"

            gain_distribution_text = _format_gain_distribution(gain_dist)

            # 昨日成交额（环比）
            cursor = conn.execute(
                """
                SELECT SUM(turnover)/100000000 as prev_turnover
                FROM stock_basic WHERE trade_date = ?
            """,
                (yesterday,),
            )
            prev_row = cursor.fetchone()
            prev_turnover = prev_row["prev_turnover"] if prev_row else 0
            if prev_turnover and prev_turnover > 0 and market["turnover"]:
                turnover_change = (market["turnover"] - prev_turnover) / prev_turnover * 100
            else:
                turnover_change = 0

            # 主要指数表现（含 3 日趋势 + MA5/MA10/MA20）
            index_codes = [
                "sh000001",
                "sz399001",
                "sz399006",
                "sh000016",
                "sh000300",
                "sh000905",
                "sh000852",
                "sz399637",
            ]
            placeholders = ",".join("?" * len(index_codes))

            # 取近 25 个交易日收盘价用于计算 MA
            from system.config.trading_calendar import get_recent_trading_days

            recent_25 = get_recent_trading_days(trade_date, 25)
            dt_ph = ",".join("?" * len(recent_25))
            cursor = conn.execute(
                f"""
                SELECT index_code, trade_date, close_price
                FROM index_realtime_data
                WHERE trade_date IN ({dt_ph}) AND index_code IN ({placeholders})
                ORDER BY index_code, trade_date
            """,
                recent_25 + index_codes,
            )
            close_history = {}
            for row in cursor.fetchall():
                code = row["index_code"]
                if code not in close_history:
                    close_history[code] = []
                close_history[code].append(row["close_price"] or 0)

            def _calc_ma(closes: list, n: int):
                if len(closes) >= n:
                    return round(sum(closes[-n:]) / n, 2)
                return 0

            # 取 3 日完整数据
            cursor = conn.execute(
                f"""
                SELECT index_code, index_name, close_price, open_price, high_price, low_price,
                       change_percent, change_amount, turnover_amount/10000 as turnover_yi,
                       volume, prev_close, trade_date
                FROM index_realtime_data
                WHERE trade_date IN (?, ?, ?) AND index_code IN ({placeholders})
                ORDER BY index_code, trade_date
            """,
                [trade_date, yesterday, day_before] + index_codes,
            )
            index_rows = [dict(row) for row in cursor.fetchall()]

            # 按指数分组，构建 3 日趋势
            index_data = []
            index_groups = {}
            for row in index_rows:
                code = row["index_code"]
                if code not in index_groups:
                    index_groups[code] = {
                        "code": code,
                        "name": row["index_name"],
                        "data": {},
                    }
                index_groups[code]["data"][row["trade_date"]] = {
                    "close": row["close_price"] or 0,
                    "open": row["open_price"] or 0,
                    "high": row["high_price"] or 0,
                    "low": row["low_price"] or 0,
                    "change": row["change_percent"] or 0,
                    "change_amount": row["change_amount"] or 0,
                    "turnover": row["turnover_yi"] or 0,
                    "volume": row["volume"] or 0,
                    "prev_close": row["prev_close"] or 0,
                }
            for code, g in index_groups.items():
                d0 = g["data"].get(trade_date, {})
                d1 = g["data"].get(yesterday, {})
                d2 = g["data"].get(day_before, {})
                closes = close_history.get(code, [])
                if d0.get("close", 0):
                    closes = closes + [d0["close"]]
                index_data.append(
                    {
                        "index_name": g["name"],
                        # 今日
                        "close": d0.get("close", 0),
                        "open": d0.get("open", 0),
                        "high": d0.get("high", 0),
                        "low": d0.get("low", 0),
                        "change_percent": d0.get("change", 0),
                        "change_amount": d0.get("change_amount", 0),
                        "turnover": d0.get("turnover", 0),
                        "volume": d0.get("volume", 0),
                        "prev_close": d0.get("prev_close", 0),
                        # MA
                        "ma5": _calc_ma(closes, 5),
                        "ma10": _calc_ma(closes, 10),
                        "ma20": _calc_ma(closes, 20),
                        # 昨日
                        "d1_close": d1.get("close", 0),
                        "d1_open": d1.get("open", 0),
                        "d1_high": d1.get("high", 0),
                        "d1_low": d1.get("low", 0),
                        "d1_change": d1.get("change", 0),
                        "d1_change_amount": d1.get("change_amount", 0),
                        "d1_turnover": d1.get("turnover", 0),
                        "d1_volume": d1.get("volume", 0),
                        # 前天
                        "d2_close": d2.get("close", 0),
                        "d2_open": d2.get("open", 0),
                        "d2_high": d2.get("high", 0),
                        "d2_low": d2.get("low", 0),
                        "d2_change": d2.get("change", 0),
                        "d2_change_amount": d2.get("change_amount", 0),
                        "d2_turnover": d2.get("turnover", 0),
                        "d2_volume": d2.get("volume", 0),
                    }
                )

            # 隔夜宏观
            cursor = conn.execute("""
                SELECT * FROM macro_daily ORDER BY trade_date DESC LIMIT 1
            """)
            macro_row = cursor.fetchone()
            macro_data = dict(macro_row) if macro_row else {}

            # ===== 2. 涨跌停 & 环比 =====
            self.logger.info("查询涨跌停数据...")
            cursor = conn.execute(
                """
                SELECT pool_type, COUNT(*) as cnt FROM limit_pool
                WHERE trade_date = ? GROUP BY pool_type
            """,
                (trade_date,),
            )
            limit_today = {row["pool_type"]: row["cnt"] for row in cursor.fetchall()}

            cursor = conn.execute(
                """
                SELECT pool_type, COUNT(*) as cnt FROM limit_pool
                WHERE trade_date = ? GROUP BY pool_type
            """,
                (yesterday,),
            )
            limit_yest = {row["pool_type"]: row["cnt"] for row in cursor.fetchall()}

            limit_up = limit_today.get("涨停", 0)
            limit_down = limit_today.get("跌停", 0)
            broken = limit_today.get("炸板", 0)
            touched = limit_up + broken
            seal_rate = (limit_up / touched * 100) if touched > 0 else 0

            prev_limit_up = limit_yest.get("涨停", 0)
            prev_limit_down = limit_yest.get("跌停", 0)
            prev_broken = limit_yest.get("炸板", 0)
            prev_touched = prev_limit_up + prev_broken
            prev_seal_rate = (prev_limit_up / prev_touched * 100) if prev_touched > 0 else 0

            limit_up_change = limit_up - prev_limit_up
            limit_down_change = limit_down - prev_limit_down
            seal_rate_change = seal_rate - prev_seal_rate

            # 涨停代码集合（供电报交叉比对）
            cursor = conn.execute(
                """
                SELECT stock_code FROM limit_pool
                WHERE trade_date = ? AND pool_type = '涨停'
            """,
                (trade_date,),
            )
            zt_codes = {row["stock_code"] for row in cursor.fetchall()}

            # 涨停质量细分（一字板/换手板/回封板）
            cursor = conn.execute(
                """
                SELECT first_seal_time, last_seal_time, open_count
                FROM limit_pool WHERE trade_date = ? AND pool_type = '涨停'
            """,
                (trade_date,),
            )
            limit_quality = {"一字板": 0, "换手板": 0, "回封板": 0}
            for row in cursor.fetchall():
                open_cnt = row["open_count"] or 0
                first_seal = row["first_seal_time"] or ""
                if open_cnt >= 1:
                    limit_quality["回封板"] += 1
                elif first_seal and first_seal <= "09:35":
                    limit_quality["一字板"] += 1
                else:
                    limit_quality["换手板"] += 1

            # ===== 3. 连板梯队 =====
            self.logger.info("查询连板梯队...")
            chain, chain_count, highest_board = LimitPoolReader.get_chain_ladder(conn, trade_date)

            # 补充概念板块
            chain_codes = [s["code"] for stocks in chain.values() for s in stocks]
            chain_concepts = SectorReader.enrich_concepts(conn, trade_date, chain_codes)
            for stocks in chain.values():
                for s in stocks:
                    concepts = chain_concepts.get(s["code"], [])
                    if concepts:
                        s["concepts"] = concepts

            # 昨日连板（环比）
            prev_chain_count, prev_highest_board = LimitPoolReader.get_prev_chain_stats(conn, yesterday)
            chain_count_change = chain_count - prev_chain_count

            # ===== 3.05. 连板晋级率（近 3 日）=====
            self.logger.info("计算近 3 日连板晋级率...")
            prev_chain, _, _ = LimitPoolReader.get_chain_ladder(conn, yesterday)
            d2_chain, _, d2_highest = LimitPoolReader.get_chain_ladder(conn, day_before)
            d3_chain, _, d3_highest = LimitPoolReader.get_chain_ladder(conn, day_before_before)

            def _calc_promotion(from_chain, from_highest, to_chain, to_highest):
                """计算相邻两日的连板晋级率"""
                rates = {}
                for board in range(2, from_highest + 1):
                    from_codes = {s["code"] for s in from_chain.get(board, [])}
                    to_codes = {s["code"] for s in to_chain.get(board + 1, [])}
                    promoted = from_codes & to_codes
                    from_count = len(from_codes)
                    if from_count > 0:
                        rates[board] = {
                            "from": board,
                            "to": board + 1,
                            "prev_count": from_count,
                            "promoted": len(promoted),
                            "rate": round(len(promoted) / from_count * 100, 1),
                        }
                return rates

            promotion_rates = [
                {
                    "label": f"{day_before_before}→{day_before}",
                    "rates": _calc_promotion(d3_chain, d3_highest, d2_chain, d2_highest),
                },
                {
                    "label": f"{day_before}→{yesterday}",
                    "rates": _calc_promotion(d2_chain, d2_highest, prev_chain, prev_highest_board),
                },
                {
                    "label": f"{yesterday}→今日",
                    "rates": _calc_promotion(prev_chain, prev_highest_board, chain, highest_board),
                },
            ]

            # ===== 3.3. 炸板明细 =====
            self.logger.info("查询炸板明细...")
            broken_records = LimitPoolReader.get_broken_boards(conn, trade_date)

            # 补充概念板块
            broken_codes = [r["code"] for r in broken_records]
            broken_concepts = SectorReader.enrich_concepts(conn, trade_date, broken_codes)
            for r in broken_records:
                concepts = broken_concepts.get(r["code"], [])
                if concepts:
                    r["concepts"] = concepts

            # ===== 3.35. 首板苗子 =====
            self.logger.info("查询首板苗子...")
            first_board_records = LimitPoolReader.get_first_boards(conn, trade_date)

            # 补充概念板块
            first_codes = [r["code"] for r in first_board_records]
            first_concepts = SectorReader.enrich_concepts(conn, trade_date, first_codes)
            for r in first_board_records:
                concepts = first_concepts.get(r["code"], [])
                if concepts:
                    r["concepts"] = concepts

            # ===== 3.4. D-2 数据（构建 3 日趋势）=====
            self.logger.info(f"查询 D-2 ({day_before}) 数据...")

            # D-2 市场概览
            cursor = conn.execute(
                """
                SELECT
                    SUM(turnover)/100000000 as turnover,
                    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) as up_count,
                    COUNT(*) as total,
                    SUM(CASE WHEN change_pct > 5 THEN 1 ELSE 0 END) as up_5pct
                FROM stock_basic WHERE trade_date = ?
            """,
                (day_before,),
            )
            d2_market = cursor.fetchone()
            d2_turnover = d2_market["turnover"] if d2_market else 0
            d2_up_ratio = d2_market["up_count"] / d2_market["total"] if d2_market and d2_market["total"] > 0 else 0
            d2_up_5pct = d2_market["up_5pct"] if d2_market else 0

            # D-2 涨跌停
            cursor = conn.execute(
                """
                SELECT pool_type, COUNT(*) as cnt FROM limit_pool
                WHERE trade_date = ? GROUP BY pool_type
            """,
                (day_before,),
            )
            d2_limit = {row["pool_type"]: row["cnt"] for row in cursor.fetchall()}
            d2_limit_up = d2_limit.get("涨停", 0)
            d2_broken = d2_limit.get("炸板", 0)
            d2_touched = d2_limit_up + d2_broken
            d2_seal_rate = (d2_limit_up / d2_touched * 100) if d2_touched > 0 else 0
            d2_limit_down = d2_limit.get("跌停", 0)

            # D-2 连板
            cursor = conn.execute(
                """
                SELECT COUNT(DISTINCT stock_code) as cnt,
                       MAX(consecutive_boards) as max_board
                FROM limit_pool WHERE trade_date = ? AND pool_type = '涨停'
                AND consecutive_boards >= 2
            """,
                (day_before,),
            )
            d2_chain = cursor.fetchone()
            d2_chain_count = d2_chain["cnt"] if d2_chain else 0
            d2_highest_board = d2_chain["max_board"] if d2_chain else 0

            # D-1 涨跌比 & 涨幅>5%（提前查询，构建三日趋势时直接用）
            cursor = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) as up_count,
                    COUNT(*) as total,
                    SUM(CASE WHEN change_pct > 5 THEN 1 ELSE 0 END) as up_5pct
                FROM stock_basic WHERE trade_date = ?
            """,
                (yesterday,),
            )
            d1_row = cursor.fetchone()
            d1_up_ratio = d1_row["up_count"] / d1_row["total"] if d1_row and d1_row["total"] > 0 else 0
            d1_up_5pct = d1_row["up_5pct"] if d1_row else 0

            # 构建 3 日趋势
            three_day_trend = {
                "d2_date": day_before,
                "d1_date": yesterday,
                "d_date": trade_date,
                "turnover": [d2_turnover, prev_turnover, market["turnover"]],
                "up_ratio": [d2_up_ratio, d1_up_ratio, up_ratio],
                "limit_up": [d2_limit_up, prev_limit_up, limit_up],
                "limit_down": [d2_limit_down, prev_limit_down, limit_down],
                "seal_rate": [d2_seal_rate, prev_seal_rate, seal_rate],
                "chain_count": [d2_chain_count, prev_chain_count, chain_count],
                "highest_board": [d2_highest_board, prev_highest_board, highest_board],
                "up_5pct": [d2_up_5pct, d1_up_5pct, market["up_5pct"] or 0],
                "broken": [d2_broken, prev_broken, broken],
            }

            # ===== 4. 行业板块排行 + 资金流 =====
            self.logger.info("查询板块排行与资金流...")
            sectors, fund_flow_map = SectorReader.get_industry_sectors(conn, trade_date)
            concept_sectors, concept_fund_map = SectorReader.get_concept_sectors(conn, trade_date)

            # ===== 5. 龙虎榜（全量 + 席位明细）=====
            self.logger.info("查询龙虎榜...")
            # 过滤ST + 科创板，按净流入占比（净买入/总成交额）降序
            cursor = conn.execute(
                """
                SELECT stock_code, stock_name, close_price, change_percent,
                       net_inflow/10000 as net_wan, turnover_amount,
                       buy_amount/10000 as buy_wan, sell_amount/10000 as sell_wan,
                       turnover_rate, reason,
                       CASE WHEN turnover_amount > 0 THEN net_inflow / turnover_amount ELSE 0 END as net_ratio
                FROM lhb_stocks WHERE trade_date = ?
                  AND stock_name NOT LIKE '%ST%'
                  AND stock_code NOT LIKE '688%'
                ORDER BY net_ratio DESC
            """,
                (trade_date,),
            )
            lhb_rows = [dict(row) for row in cursor.fetchall()]

            # 查询席位明细
            cursor = conn.execute(
                """
                SELECT ls.stock_code, ls.seat_name, ls.buy_amount, ls.sell_amount,
                       ls.net_amount, ls.is_institution, ls.is_hot_money,
                       ls.seat_type
                FROM lhb_seats ls
                JOIN lhb_stocks s ON ls.stock_code = s.stock_code AND ls.trade_date = s.trade_date
                WHERE ls.trade_date = ?
                ORDER BY ls.stock_code, ls.net_amount DESC
            """,
                (trade_date,),
            )
            lhb_seats_by_stock = {}
            for row in cursor.fetchall():
                code = row["stock_code"]
                if code not in lhb_seats_by_stock:
                    lhb_seats_by_stock[code] = {"buy": [], "sell": []}
                seat = {
                    "name": row["seat_name"],
                    "amount": row["net_amount"] or 0,
                    "buy": row["buy_amount"] or 0,
                    "sell": row["sell_amount"] or 0,
                    "is_inst": row["is_institution"],
                    "is_hm": row["is_hot_money"],
                    "type": row["seat_type"] or "",
                }
                if (row["buy_amount"] or 0) > (row["sell_amount"] or 0):
                    lhb_seats_by_stock[code]["buy"].append(seat)
                else:
                    lhb_seats_by_stock[code]["sell"].append(seat)

            # 查连板数
            lhb_codes = [r["stock_code"] for r in lhb_rows]
            boards_map = {}
            if lhb_codes:
                ph = ",".join("?" * len(lhb_codes))
                cursor = conn.execute(
                    f"""
                    SELECT stock_code, consecutive_boards FROM limit_pool
                    WHERE trade_date = ? AND pool_type = '涨停'
                      AND stock_code IN ({ph})
                """,
                    [trade_date] + lhb_codes,
                )
                for r in cursor.fetchall():
                    boards_map[r["stock_code"]] = r["consecutive_boards"] or 0

            # 查近5天上榜频次
            freq_map = {}
            if lhb_codes:
                ph = ",".join("?" * len(lhb_codes))
                cursor = conn.execute(
                    f"""
                    SELECT stock_code, COUNT(*) as freq
                    FROM lhb_stocks
                    WHERE stock_code IN ({ph})
                      AND trade_date >= date(?, '-5 days')
                    GROUP BY stock_code
                """,
                    lhb_codes + [trade_date],
                )
                for r in cursor.fetchall():
                    freq_map[r["stock_code"]] = r["freq"]

            # 组装：每只股票附带席位 + 连板 + 频次
            lhb_data = []
            for row in lhb_rows:
                code = row["stock_code"]
                seats = lhb_seats_by_stock.get(code, {"buy": [], "sell": []})
                sell_wan = row["sell_wan"] or 0
                lhb_data.append(
                    {
                        "code": code,
                        "name": row["stock_name"],
                        "change": row["change_percent"] or 0,
                        "net_wan": row["net_wan"] or 0,
                        "net_ratio": row["net_ratio"] or 0,
                        "turnover": row["turnover_amount"] or 0,
                        "turnover_rate": row["turnover_rate"] or 0,
                        "buy_wan": row["buy_wan"] or 0,
                        "sell_wan": -abs(sell_wan) if sell_wan else 0,
                        "reason": row["reason"] or "",
                        "buy_seats": seats["buy"][:5],
                        "sell_seats": seats["sell"][:5],
                        "boards": boards_map.get(code, 0),
                        "lhb_freq": freq_map.get(code, 1),
                    }
                )

            # ===== 6. 今日异动股（分层抽样：主板20 + 创业板20）=====
            self.logger.info("查询今日异动股...")
            candidates = StockReader.get_candidates(conn, trade_date)

            # 补充概念板块
            candidate_codes = [c["code"] for c in candidates]
            candidate_concepts = SectorReader.enrich_concepts(conn, trade_date, candidate_codes)
            for c in candidates:
                concepts = candidate_concepts.get(c["code"], [])
                if concepts:
                    c["concepts"] = concepts

            # ===== 6.5. 近期强势股（60日新高+多次涨停，凑够30只）=====
            self.logger.info("查询近期强势股...")
            active_stocks = StockReader.get_strong_stocks(conn, trade_date, sectors)

            # 补充概念板块
            active_codes = [s["stock_code"] for s in active_stocks]
            active_concepts = SectorReader.enrich_concepts(conn, trade_date, active_codes)
            for s in active_stocks:
                concepts = active_concepts.get(s["stock_code"], [])
                if concepts:
                    s["concepts"] = concepts

            # ===== 7. 热点板块（综合打分取 top10）=====
            self.logger.info("查询热点板块（综合打分）...")
            top_industries = SectorReader.get_hot_sectors(
                conn,
                trade_date,
                "sector_industry",
                top_n=10,
                prev_date=yesterday,
                prev_prev_date=day_before,
            )
            top_concepts = SectorReader.get_hot_sectors(
                conn,
                trade_date,
                "sector_concept",
                top_n=10,
                prev_date=yesterday,
                prev_prev_date=day_before,
            )
            if top_industries:
                self.logger.info(f"行业热点TOP10：{', '.join(s['name'] for s in top_industries[:5])}...")
            else:
                self.logger.info("无行业热点数据")
            if top_concepts:
                self.logger.info(f"概念热点TOP10：{', '.join(s['name'] for s in top_concepts[:5])}...")
            else:
                self.logger.info("无概念热点数据")

            # ===== 7.5. 保存热点历史 ====
            self.logger.info("保存热点历史...")

            # 保存今日 TOP10 到历史表
            for s in top_industries:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sector_hot_history
                    (trade_date, sector_type, rank, sector_code, sector_name, hot_score)
                    VALUES (?, 'industry', ?, ?, ?, ?)
                """,
                    (
                        trade_date,
                        s.get("rank", 0),
                        s["sector_code"],
                        s["name"],
                        s["hot_score"],
                    ),
                )
            for s in top_concepts:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sector_hot_history
                    (trade_date, sector_type, rank, sector_code, sector_name, hot_score)
                    VALUES (?, 'concept', ?, ?, ?, ?)
                """,
                    (
                        trade_date,
                        s.get("rank", 0),
                        s["sector_code"],
                        s["name"],
                        s["hot_score"],
                    ),
                )
            conn.commit()

            # 今日 TOP10 本身也算一次上榜（_calc_hot_days 查的是保存前的历史，不含今日）
            for s in top_industries:
                s["hot_days"] = s.get("hot_days", 0) + 1
            for s in top_concepts:
                s["hot_days"] = s.get("hot_days", 0) + 1

            # ===== 8. 昨日涨停今日表现 =====
            self.logger.info("查询昨日涨停今日表现...")
            yzt_records = LimitPoolReader.get_yzt_performance(conn, trade_date)

            # 补充概念板块
            yzt_codes = [r["code"] for r in yzt_records]
            yzt_concepts = SectorReader.enrich_concepts(conn, trade_date, yzt_codes)
            for r in yzt_records:
                concepts = yzt_concepts.get(r["code"], [])
                if concepts:
                    r["concepts"] = concepts

            # ===== 10. 资金集中度 =====
            self.logger.info("计算资金集中度...")
            # 合并行业+概念板块主力净额，计算 TOP3 占比
            all_sector_flows = []
            for s in sectors:
                ff = fund_flow_map.get(s["name"], {})
                mf = ff.get("main_force_net", 0) or 0
                if mf > 0:
                    all_sector_flows.append(mf)
            for s in concept_sectors:
                ff = concept_fund_map.get(s["name"], {})
                mf = ff.get("main_force_net", 0) or 0
                if mf > 0:
                    all_sector_flows.append(mf)
            all_sector_flows.sort(reverse=True)
            total_inflow = sum(all_sector_flows) / 100000000 if all_sector_flows else 0
            top3_inflow = sum(all_sector_flows[:3]) / 100000000 if all_sector_flows else 0
            top3_pct = (top3_inflow / total_inflow * 100) if total_inflow > 0 else 0
            capital_concentration = {
                "top3_pct": top3_pct,
                "total_inflow": total_inflow,
            }

            # 昨日涨停平均溢价率
            yzt_avg_change = sum(r.get("change", 0) for r in yzt_records) / len(yzt_records) if yzt_records else 0

            # ===== 11. 股东增减持 =====
            self.logger.info("查询股东增减持...")
            cursor = conn.execute(
                """
                SELECT stock_code, stock_name, holder_name, change_type,
                       change_direction, change_rate, change_num_symbol
                FROM share_holder_change WHERE trade_date = ?
                ORDER BY ABS(change_rate) DESC
            """,
                (trade_date,),
            )
            share_holder_changes = [dict(row) for row in cursor.fetchall()]

            # ===== 12. 重点监控 =====
            self.logger.info("查询重点监控...")
            cursor = conn.execute(
                """
                SELECT stock_code, stock_name, monitor_type, trigger_rule, status
                FROM stock_monitor WHERE trade_date = ?
            """,
                (trade_date,),
            )
            stock_monitors = [dict(row) for row in cursor.fetchall()]

            # ===== 13. 重要公告过滤 =====
            self.logger.info("查询重要公告...")
            announcement_whitelist = [
                "业绩预告",
                "一季度报告全文",
                "年度报告全文",
                "年度报告摘要",
                "半年度报告",
                "三季度报告",
                "分配预案",
                "重组进展公告",
                "收购出售资产/股权",
                "重大合同",
                "实施退市风险警示",
                "其它风险提示公告",
                "停复牌公告",
                "股权激励进展公告",
                "诉讼仲裁",
                "月度经营情况",
            ]
            placeholders = ",".join("?" * len(announcement_whitelist))
            cursor = conn.execute(
                f"""
                SELECT stock_code, stock_name, announcement_title, announcement_type,
                       importance_score, announcement_url
                FROM future_announcements
                WHERE announcement_type IN ({placeholders})
                  AND importance_score >= 8
                  AND trade_date >= ?
                ORDER BY importance_score DESC, trade_date DESC
            """,
                announcement_whitelist + [trade_date],
            )
            important_announcements = [dict(row) for row in cursor.fetchall()]

            # ===== 14. 仓位硬顶 =====
            # 计算指数均线健康度：多少个指数站上 MA5 且 MA20
            healthy_indices = 0
            total_indices = len(index_data)
            for idx in index_data:
                close = idx.get("close", 0)
                ma5 = idx.get("ma5", 0)
                ma20 = idx.get("ma20", 0)
                if close > 0 and ma5 > 0 and ma20 > 0 and close > ma5 and close > ma20:
                    healthy_indices += 1
            index_ma_health = (healthy_indices / total_indices * 100) if total_indices > 0 else 50

            position_cap = calc_position_cap(
                limit_up=limit_up,
                broken=broken,
                highest_board=highest_board,
                seal_rate=seal_rate,
                up_ratio=up_ratio,
                yzt_avg_change=yzt_avg_change,
                turnover_change=turnover_change,
                up_5pct=market["up_5pct"] or 0,
                down_5pct=market["down_5pct"] or 0,
                limit_down=limit_down,
                index_ma_health=index_ma_health,
            )
            self.logger.info(
                f"仓位硬顶：{position_cap}%（"
                f"涨停{limit_up}, 封板率{seal_rate:.1f}%, 最高{highest_board}板, "
                f"涨跌比{up_ratio:.2f}, 溢价率{yzt_avg_change:+.2f}%, "
                f"量能环比{turnover_change:+.1f}%, "
                f"涨>5%:{market['up_5pct'] or 0}/跌>5%:{market['down_5pct'] or 0}, "
                f"跌停{limit_down}, 指数健康{healthy_indices}/{total_indices}"
                f")"
            )

            # ===== 14.5. 预计算 FC 工具数据（消除多轮 FC 对话）=====
            precomputed = self._precompute_all_data(
                trade_date=trade_date,
                zt_codes=zt_codes,
                chain=chain,
                lhb_codes=[r.get("code", r.get("stock_code", "")) for r in lhb_rows] if lhb_rows else [],
                candidate_codes=[c["code"] for c in candidates],
                top_industries=top_industries,
                top_concepts=top_concepts,
                first_board_codes=[r["code"] for r in first_board_records],
                active_codes=[s["stock_code"] for s in active_stocks],
            )

            # ===== 15. 格式化所有数据（按 Prompt 模板新顺序）=====
            self.logger.info("格式化 Prompt 数据...")
            prompt_data = {
                # 一、市场全貌
                "trade_date": trade_date,
                "turnover": round(market["turnover"] or 0, 0),
                "prev_turnover": round(prev_turnover or 0, 0),
                "turnover_change": f"{turnover_change:+.1f}%",
                "up_count": market["up_count"] or 0,
                "down_count": market["down_count"] or 0,
                "flat_count": market["flat_count"] or 0,
                "up_down_ratio": up_down_ratio,
                "gain_distribution_text": gain_distribution_text,
                "limit_up_count": limit_up,
                "limit_down_count": limit_down,
                "broken_count": broken,
                "seal_rate": round(seal_rate, 1),
                "limit_up_change": fmt_change(limit_up_change),
                "limit_down_change": fmt_change(limit_down_change),
                "seal_rate_change": f"{seal_rate_change:+.1f}%",
                "limit_quality_text": format_limit_quality(limit_quality),
                "up_5pct_count": market["up_5pct"] or 0,
                "down_5pct_count": market["down_5pct"] or 0,
                "chain_count": chain_count,
                "chain_count_change": fmt_change(chain_count_change),
                "highest_board": highest_board,
                "prev_highest_board": prev_highest_board,
                "d2_date": three_day_trend["d2_date"],
                "d1_date": three_day_trend["d1_date"],
                "three_day_trend": format_three_day_trend(three_day_trend),
                "index_data_text": format_index_data(index_data),
                # 二、隔夜外围
                "macro_text": format_macro_overview(macro_data),
                # 三、财联社复盘新闻（预计算）
                "news_data_text": precomputed["news_data_text"],
                # 四、财联社盘中电报（预计算，详见预查数据章节）
                "telegraph_text": "盘中电报已采集。个股电报详见「五、预查数据」。",
                # 五、连板梯队（含首板苗子+炸板明细）
                "chain_ladder_text": format_chain_ladder(chain, promotion_rates, sector_code_map=self.sector_code_map),
                "first_boards_text": format_first_boards(first_board_records, sector_code_map=self.sector_code_map),
                "broken_boards_text": format_broken_boards(
                    broken_records,
                    broken_trend={"d2": d2_broken, "d1": prev_broken, "d": broken},
                    sector_code_map=self.sector_code_map,
                ),
                # 六、热点板块数据
                "hotspot_text": format_hotspot(top_industries, top_concepts, sector_code_map=self.sector_code_map),
                # 七、板块资金暗流
                "fund_flow_text": (
                    "【行业】\n"
                    + format_fund_flow(sectors, fund_flow_map)
                    + "\n【概念】\n"
                    + format_fund_flow(concept_sectors, concept_fund_map)
                ),
                "capital_concentration_text": format_capital_concentration(capital_concentration),
                # 八、今日异动股
                "candidate_count": len(candidates),
                "candidate_text": format_candidates(candidates, sector_code_map=self.sector_code_map),
                # 九、龙虎榜全量
                "lhb_text": format_lhb_full(lhb_data),
                # 十、近期强势股
                "strong_count": len(active_stocks),
                "strong_stocks_text": format_strong_stocks(active_stocks, sector_code_map=self.sector_code_map),
                # 十一、昨日涨停股今日表现
                "yzt_count": len(yzt_records),
                "yzt_text": format_yzt_performance(yzt_records, sector_code_map=self.sector_code_map),
                # 十三、风险地雷
                "share_holder_text": format_risk_flags(share_holder_changes, candidates),
                "monitor_text": format_risk_flags(stock_monitors, candidates, label="监控"),
                # 十四、今日重要公告
                "announcements_text": format_announcements(important_announcements),
                # 十五、昨日对账（判断+表现+校准+预测+教训）
                "calibration_section": precomputed["calibration_section"],
                # 六-B、板块中军（预计算）
                "zhongjun_section": precomputed["zhongjun_section"],
                # 十一-B、连板股席位（预计算）
                "lhb_seats_section": precomputed["lhb_seats_section"],
                # 预查数据（电报+监管）
                "precompute_section": precomputed["precompute_section"],
                # 仓位
                "position_cap": position_cap,
            }

            prompt = REVIEW_REPORT_PROMPT.format(**prompt_data)

            # ===== 12. 调用 AI（单次调用，数据已预计算嵌入）=====
            system_prompt = (
                '你是一个顶级游资复盘手，代号"刺客"，风格犀利、直接、有观点。'
                "你看到的是今日收盘后的全量市场数据。你的核心任务是："
                "1. 先判市场阶段（进攻/防御切换/抱团瓦解/混沌轮动/退潮冰点），再选主线，再选标的"
                "2. 你的推荐是给用户明天（T+1）开盘买入、后天（T+2）卖出用的——推荐的依据必须是明天会涨，不是今天涨了"
                "3. 识别一日游风险：首次上榜的板块、防御标签无催化的板块、领涨无中军大票的板块，都是高危一日游"
                "4. 给出多路径情景推演（主线延续/分歧回流/新方向/一日游退潮），每条标注触发条件和概率，不能只给一种判断"
                '5. 风险提示要具体——写清楚什么情况下会崩，不要写"注意风险"这种废话'
                "6. 拆解标的时，要说出它在市场中的地位和信号意义"
                "7. 用中文输出，所有数值用阿拉伯数字"
                "8. 报告末尾必须包含 <<<STOCKS>>> 股票池 JSON（包含第六节所有游资标的），这是强制性要求"
                "9. 选股前务必查阅「五」的监管风险和电报新闻，有风险直接剔除。板块中军见「六-B」，龙虎榜席位见「十-B」。"  # noqa: E501
                "10. **绝对禁止**在报告中出现：工具调用过程、打分排名（如'得分XX分'）、prompt指令、数据筛选条件——报告只呈现判断结论和逻辑推演"  # noqa: E501
            )

            review_model = settings.AI_MODEL_REVIEW or settings.AI_MODEL
            models_to_run = [
                (review_model, review_model),
            ]

            reports = {}  # {model_name: report_text}

            for model_name, model_label in models_to_run:
                pass  # model override no longer needed (use system.ai)
                try:
                    if settings.BATCH_ENABLED:
                        self.logger.info(f"调用 {model_label} AI 生成复盘报告（Batch模式，Prompt {len(prompt)}字）...")
                        report_text = self._run_batch_review(prompt, trade_date, system_prompt)
                        if report_text is None:
                            # Batch 已提交，子进程负责收尾
                            self.logger.info(f"✅ {model_label} Batch 已提交，报告稍后通过子进程推送")
                            reports[model_name] = None
                            continue
                    else:
                        self.logger.info(f"调用 {model_label} AI 生成复盘报告（预计算模式，Prompt {len(prompt)}字）...")
                        report_text = self._run_fc_review(prompt, trade_date, system_prompt, precomputed=True)
                except Exception as e:
                    self.logger.error(f"❌ {model_label} AI 调用异常: {e}")
                    reports[model_name] = None
                    continue
                reports[model_name] = report_text
                self.logger.info(f"✅ {model_label} 报告生成完成（{len(report_text)}字）")

            # 保存报告到文件（供下次复盘自我校准）
            try:
                reports_dir = STORAGE_PATH / "reports"
                reports_dir.mkdir(parents=True, exist_ok=True)

                for model_name, report_text in reports.items():
                    if report_text is None:
                        continue
                    suffix = f"_{model_name}"
                    report_path = reports_dir / f"review_reports_{trade_date}{suffix}.txt"
                    report_path.write_text(report_text, encoding="utf-8")
                    self.logger.info(f"✅ {model_name} 复盘报告已保存到 {report_path}")
            except Exception as e:
                self.logger.warning(f"保存复盘报告失败（不影响主流程）：{e}")

            report = reports.get(settings.AI_MODEL)
            if not report:
                report = next((v for v in reports.values() if v), None)
            if not report:
                # 可能是 Batch 模式，子进程稍后推送
                elapsed = time.time() - start_time
                self.logger.info(f"📤 Batch 已提交，主进程退出（{elapsed:.0f}s），子进程稍后推送报告")
                return "报告正在通过批处理生成，稍后推送。", []

            elapsed = time.time() - start_time
            self.logger.info(f"✅ 复盘 AI 分析完成，耗时 {elapsed:.1f}秒")

            return self._finalize_report(report, trade_date)

        except Exception as e:
            self.logger.error(f"❌ 复盘 AI 分析失败：{e}", exc_info=True)
            return f"AI 分析失败：{e}", []
        finally:
            conn.close()

    def _extract_predictions(self, report_text: str, trade_date: str):
        """从报告中解析 <<<PREDICTIONS>>> 结构化预测，存入 review_predictions 表"""
        import json as _json

        match = re.search(r"<<<PREDICTIONS>>>(.*?)<<<END>>>", report_text, re.DOTALL)
        if not match:
            self.logger.warning("⚠️ 未找到 <<<PREDICTIONS>>> 标记")
            return

        raw = match.group(1).strip()
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError as e:
            self.logger.warning(f"PREDICTIONS JSON 解析失败: {e}")
            return

        conn = connect(DATABASE_PATH)
        count = 0
        # 指数预测
        for idx in data.get("index", []):
            PredictionRepo.insert_index_prediction(
                conn,
                trade_date,
                idx.get("name", ""),
                idx.get("direction", ""),
                f"支撑{idx.get('support', '?')}/压力{idx.get('resistance', '?')}",
                None,
            )
            count += 1
        # 板块预测
        for sec in data.get("sectors", []):
            PredictionRepo.insert_sector_prediction(
                conn,
                trade_date,
                sec.get("name", ""),
                sec.get("prediction", ""),
                sec.get("prob"),
            )
            count += 1
        # 主导情景
        scenario = data.get("dominant_scenario", "")
        if scenario:
            PredictionRepo.insert_scenario_prediction(
                conn,
                trade_date,
                scenario,
            )
            count += 1
        conn.commit()
        conn.close()
        self.logger.info(f"✅ 预测入库: {count} 条")

    def _extract_lessons(self, report_text: str, trade_date: str):
        """
        从复盘报告中提取通用经验教训，和已有教训对比合并后存入 review_lessons 表。

        不是简单匹配关键词，而是让 AI 从偏差校准和自我诊断中提炼模式级教训。
        同类教训合并（增加 occurrence_count），新教训追加。
        """
        import json as _json

        # 只取报告中的偏差校准 + 自我诊断部分，减少 prompt 长度
        cali_match = re.search(
            r"(?:昨日预测偏差校准|自我诊断|[📊⚠️].*?校准).*?(?:\n\n|\n(?=🎯|📈|⚔️|📊))",
            report_text,
            re.DOTALL,
        )
        excerpt = cali_match.group(0)[:1200] if cali_match else report_text[-3000:]

        # 拉取已有教训（用于合并对比）
        conn = connect(DATABASE_PATH)
        existing = PredictionRepo.get_active_lessons(conn)
        conn.close()

        existing_text = (
            "\n".join(
                f"[{r['lesson_type']}] {r['lesson_key']}: {r['lesson_content'][:80]} (出现{r['occurrence_count']}次)"
                for r in existing
            )
            if existing
            else "（尚无历史教训）"
        )

        # AI 提取 prompt
        extract_prompt = f"""你是复盘分析师。从以下复盘报告中提取**通用模式级经验教训**。

规则：
1. 不是"XX股票判断错了"这种个股层面，而要提炼成模式。
例如"防御切换期追高日内强势股，次日低开被套"、
"分歧期龙头炸板后回流失败，追板中位股被埋"
2. lesson_type 从以下选：选股角色、板块判断、仓位管理、情绪判断、趋势选股
3. lesson_key 用简短词组（≤15字），同类问题用同一个 key 才能合并，如"防御切换追高"、"补涨标的已连板"、"退潮期仍推荐标的"
4. 如果和已有教训同类，只返回已有教训的 lesson_key 即可
5. 如果这份报告没有新教训，返回空列表

【已有教训】
{existing_text}

【报告摘录】
{excerpt}

返回 JSON（只返回 JSON，不要其他文字）：
{{
  "lessons": [
    {{
      "lesson_type": "板块判断",
      "lesson_key": "防御切换追高",
      "lesson_content": "防御切换期追涨日内强势的防御板块标的，次日高开低走被套。"
防御板块需验证持续性再推，不能因为它今天涨了就推荐"
    }}
  ]
}}
"""
        try:
            result_text = ai.chat(extract_prompt, model="review")
            # 清理 JSON
            result_text = re.sub(r"```(?:json)?\s*", "", result_text)
            result_text = result_text.strip()
            data = _json.loads(result_text)
            lessons = data.get("lessons", [])
        except Exception as e:
            self.logger.warning(f"AI 教训提取失败: {e}")
            return

        if not lessons:
            self.logger.info("本次报告无新增经验教训")
            return

        # 合并入库
        conn = connect(DATABASE_PATH)
        new_count, merge_count = 0, 0
        for lesson in lessons:
            lt = lesson.get("lesson_type", "")
            lk = lesson.get("lesson_key", "")
            lc = lesson.get("lesson_content", "")
            if not lt or not lk or not lc:
                continue
            # 判断是否已存在（用于日志区分）
            cur = conn.execute(
                "SELECT COUNT(*) FROM review_lessons WHERE lesson_type=? AND lesson_key=?",
                (lt, lk),
            )
            is_new = cur.fetchone()[0] == 0
            PredictionRepo.upsert_lesson(conn, lt, lk, lc, trade_date)
            if is_new:
                new_count += 1
                self.logger.info(f"  新增教训: [{lt}] {lk}")
            else:
                merge_count += 1
                self.logger.info(f"  合并教训: [{lt}] {lk}")
        conn.commit()
        conn.close()
        self.logger.info(f"✅ 教训提取完成：新增{new_count}条，合并{merge_count}条")

    def _extract_stock_pool(self, report_text: str) -> list:
        """从复盘报告中提取股票池 JSON"""
        import json as _json
        import re

        match = re.search(r"<<<STOCKS>>>(.*?)<<<END>>>", report_text, re.DOTALL)
        if not match:
            self.logger.warning("⚠️ 未找到股票池标记 <<<STOCKS>>>")
            return []

        raw = match.group(1).strip()
        # 清理可能的 markdown 代码块包裹
        raw = re.sub(r"^```(?:json)?\s*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)

        try:
            data = _json.loads(raw)
            stock_list = data.get("stocks", [])
        except _json.JSONDecodeError as e:
            self.logger.error(f"❌ 股票池 JSON 解析失败: {e}")
            self.logger.error(f"原始内容(前300字): {raw[:300]}")
            return []

        stocks = []
        for s in stock_list:
            name = (s.get("name") or "").strip()
            code = (s.get("code") or "").strip()
            role = (s.get("role") or "").strip()

            # 角色 → 优先级映射
            if "龙头" in role or "破局" in role:
                priority = "P0"
            elif "中军" in role:
                priority = "P1"
            else:
                priority = "P2"

            stocks.append(
                {
                    "股票名称": name,
                    "股票代码": code if code else "",
                    "所属板块": (s.get("sector_name") or "").strip(),
                    "sector_code": (s.get("sector_code") or "").strip(),
                    "推荐理由": f"{role} | {(s.get('buy_condition') or '').strip()}"
                    if role
                    else (s.get("buy_condition") or "").strip(),
                    "优先级": priority,
                    "买入条件": (s.get("buy_condition") or "").strip(),
                    "放弃条件": (s.get("abandon_condition") or "").strip(),
                    "止损位": s.get("stop_loss", ""),
                    "目标位": s.get("target", ""),
                    "市值": "",
                }
            )

        self.logger.info(f"✅ 从复盘股票池解析到 {len(stocks)} 只股票")
        return stocks

    def _remove_stock_pool(self, report_text: str) -> str:
        """从复盘报告中删除股票池部分（推送前调用）"""
        import re

        cleaned = re.sub(r"<<<STOCKS>>>.*?<<<END>>>", "", report_text, flags=re.DOTALL)
        cleaned = re.sub(r"\n\s*\n\s*\n", "\n\n", cleaned)
        self.logger.info("✅ 已删除复盘股票池标记")
        return cleaned

    def _remove_predictions(self, report_text: str) -> str:
        """从复盘报告中删除预测结构化数据（推送前调用）"""
        import re

        cleaned = re.sub(r"<<<PREDICTIONS>>>.*?<<<END>>>", "", report_text, flags=re.DOTALL)
        cleaned = re.sub(r"\n\s*\n\s*\n", "\n\n", cleaned)
        self.logger.info("✅ 已删除复盘预测标记")
        return cleaned

    def _fetch_news(self, trade_date: str) -> dict:
        """分析时实时抓取 CLS 复盘新闻"""
        try:
            from data.collect.events.cls_digest_collector import CLSDigestCollector

            collector = CLSDigestCollector()
            return collector.collect_review()
        except Exception as e:
            self.logger.warning(f"CLS 复盘新闻抓取失败: {e}")
            return {}

    def _run_batch_review(self, prompt: str, trade_date: str, system_prompt: str) -> str:
        """
        通过 Batch API 提交复盘请求（50% 成本），轮询等待结果。

        仅支持 dashscope provider（qwen 系列模型）。
        Batch 失败时自动回退到实时 FC 模式。
        """
        import json as _json

        batch_start = time.time()

        model_name = settings.AI_MODEL_REVIEW or settings.AI_MODEL
        if not model_name:
            self.logger.warning("Batch: 未配置 review 模型，回退实时模式")
            return self._run_fc_review(prompt, trade_date, system_prompt, precomputed=True)

        from system.ai.ai_service import _resolve_provider

        provider, api_key, _ = _resolve_provider(model_name)

        # Batch API 需要 OpenAI 兼容端点，dashscope 支持，deepseek 待确认
        if provider != "dashscope":
            self.logger.info(f"Batch: provider={provider} 暂不支持 Batch API，回退实时模式")
            return self._run_fc_review(prompt, trade_date, system_prompt, precomputed=True)

        batch_base = settings.DASHSCOPE_COMPAT_ENDPOINT
        headers = {"Authorization": f"Bearer {api_key}"}
        deadline = time.time() + settings.BATCH_TIMEOUT_MINUTES * 60

        self.logger.info(
            f"Batch: 模型={model_name}，超时={settings.BATCH_TIMEOUT_MINUTES}分钟，"
            f"轮询间隔={settings.BATCH_POLL_INTERVAL}s"
        )

        try:
            # ── Step 1: 构建 JSONL ──
            request_body = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 4096,
                "temperature": 0.6,
            }

            custom_id = f"review-{trade_date}"
            jsonl = (
                _json.dumps(
                    {
                        "custom_id": custom_id,
                        "method": "POST",
                        "url": "/v1/chat/completions",
                        "body": request_body,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

            # 保存 JSONL 到日志目录
            jsonl_path = LOGS_DIR / trade_date / "prompts" / "batch_input.jsonl"
            jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            jsonl_path.write_text(jsonl, encoding="utf-8")
            self.logger.info(f"Batch: JSONL 已保存 {jsonl_path}")

            # ── Step 2: 上传文件 ──
            self.logger.info("Batch: 上传文件...")
            resp = requests.post(
                f"{batch_base}/files",
                headers=headers,
                files={
                    "file": (
                        "batch_input.jsonl",
                        jsonl.encode("utf-8"),
                        "application/jsonl",
                    )
                },
                data={"purpose": "batch"},
                timeout=30,
            )
            resp.raise_for_status()
            file_id = resp.json()["id"]
            self.logger.info(f"Batch: 文件已上传 → {file_id}")

            # ── Step 3: 创建批处理任务 ──
            self.logger.info("Batch: 创建任务...")
            resp = requests.post(
                f"{batch_base}/batches",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "input_file_id": file_id,
                    "endpoint": "/v1/chat/completions",
                    "completion_window": "24h",
                },
                timeout=30,
            )
            resp.raise_for_status()
            batch_info = resp.json()
            batch_id = batch_info["id"]
            self.logger.info(f"Batch: 任务已创建 → {batch_id}")

            # ── Step 4: 启动子进程异步等待 ──
            self.logger.info(f"Batch: 任务已提交 → {batch_id}，启动子进程异步轮询")
            elapsed = time.time() - batch_start
            self.logger.info(f"Batch: 主进程退出（{elapsed:.0f}s），子进程接管后续轮询+收尾")

            # spawn 子进程，主进程不等待
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    f"from review.review_analyzer import ReviewAnalyzer; "
                    f"ReviewAnalyzer._batch_poll_and_finalize('{trade_date}', '{batch_id}')",
                ],
                env={**os.environ},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return None  # 告诉上层：已提交，报告稍后推送

        except Exception as e:
            self.logger.error(f"Batch API 失败: {e}，回退实时模式")
            return self._run_fc_review(prompt, trade_date, system_prompt, precomputed=True)

    def _run_fc_review(
        self,
        prompt: str,
        trade_date: str,
        system_prompt: str,
        precomputed: bool = False,
    ) -> str:
        """
        FC 多轮对话：一轮暴露全部工具，AI 自由选择调用顺序。

        事后校验：AI 返回报告内容时，检查必修工具是否都已调用。
        未调完的，追加消息要求继续调用，最多 10 轮防止死循环。

        precomputed=True 时跳过必修工具校验（数据已预计算嵌入 prompt）。
        """
        from system.ai.stock_tools import TOOLS_DEFINITION

        fc_engine = FunctionCallingEngine()
        all_tool_names = [t["function"]["name"] for t in TOOLS_DEFINITION]

        # 报告生成前必须调用的工具（get_sector_zhongjun 在选股环节调用，不在此列）
        mandatory_tools = [
            "get_cls_digest_news",
            "get_yesterday_review",
            "get_historical_calibration",
            "get_yesterday_picks_performance",
            "get_learning_lessons",
        ]
        # FC 日志文件
        fc_log_path = LOGS_DIR / trade_date / "prompts" / "review_fc_log.txt"
        fc_log_path.parent.mkdir(parents=True, exist_ok=True)
        fc_lines = []

        def _fc_log(msg: str):
            ts = datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {msg}"
            fc_lines.append(line)
            self.logger.info(f"  {msg}")

        _fc_log("=" * 60)
        _fc_log(f"复盘 FC 多轮对话日志 - {trade_date}")
        _fc_log(f"模型: review (system.ai) | 模式: {'预计算' if precomputed else '标准FC'}")
        _fc_log(f"可用工具 ({len(all_tool_names)}): {', '.join(all_tool_names)}")
        if not precomputed:
            _fc_log(f"必修工具 ({len(mandatory_tools)}): {', '.join(mandatory_tools)}")
        _fc_log("=" * 60)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        called_tools = set()
        round_num = 0
        max_fc_rounds = 10  # 防止死循环
        content = ""  # 确保 finally 块可访问

        try:
            while True:
                round_num += 1
                if round_num > max_fc_rounds:
                    _fc_log(f"  ⚠️ 已达最大轮次 {max_fc_rounds}，强制终止")
                    break
                tools = TOOLS_DEFINITION
                tool_choice = "auto"
                # 预计算模式无必修约束，始终 600s；标准模式首轮也 600s 防止大盘普跌时模型思考超时
                _fc_log(f"\n第 {round_num} 轮（{len(tools)} 个工具，tool_choice=auto，超时600s）")

                response = ai.chat_with_tools_raw(
                    messages,
                    model="review",
                    tools=tools,
                    tool_choice=tool_choice,
                )

                content = response.get("content", "")
                tool_calls = response.get("tool_calls", [])

                if content:
                    preview = content[:200].replace("\n", "\\n")
                    _fc_log(f"  AI 文本回复: {len(content)}字 → {preview}...")

                if tool_calls:
                    _fc_log(f"  工具调用: {len(tool_calls)} 个")
                    for tc in tool_calls:
                        fn = tc.get("function", {}) if isinstance(tc, dict) else tc.function
                        fn_name = fn.get("name", "?")
                        fn_args = fn.get("arguments", "{}")
                        called_tools.add(fn_name)
                        _fc_log(f"    → {fn_name}({fn_args})")

                    assistant_msg = {"role": "assistant", "content": content or ""}
                    assistant_msg["tool_calls"] = tool_calls
                    messages.append(assistant_msg)

                    tool_messages = fc_engine.process_tool_calls(tool_calls)
                    for tm in tool_messages:
                        result_str = str(tm.get("content", ""))
                        if len(result_str) > 500:
                            result_str = result_str[:500] + f"...(共{len(result_str)}字)"
                        _fc_log(f"    ← 返回: {result_str}")

                    messages.extend(tool_messages)
                    continue

                # --- 无工具调用：判断是否为最终回复 ---
                if not content:
                    _fc_log("  ⚠️ AI 未返回内容也未调用工具，重试")
                    continue

                # 检查报告标记
                is_report = "【复盘分析" in content or "<<<STOCKS>>>" in content

                if is_report:
                    missing = [t for t in mandatory_tools if t not in called_tools] if not precomputed else []
                    if missing:
                        _fc_log(f"  ⚠️ 检测到报告内容，但必修工具未调用: {missing}")
                        messages.append(
                            {
                                "role": "user",
                                "content": f"请先调用以下必修工具获取数据，再生成报告：{', '.join(missing)}。不要在工具调用前输出报告正文。",  # noqa: E501
                            }
                        )
                        continue
                    # 接受报告
                    if precomputed:
                        _fc_log(f"  ✅ 报告完成（预计算模式，已调用 {len(called_tools)} 个可选工具）")
                    else:
                        _fc_log(f"  ✅ 所有必修工具已调用 ({len(called_tools)} 个)，报告完成")
                    break

                # 不是报告、也没调工具，可能是中间回复，推回继续
                _fc_log("  AI 返回中间回复，继续等待工具调用")
                messages.append({"role": "assistant", "content": content})
                continue

            _fc_log(f"\n共 {round_num} 轮，已调用工具: {', '.join(sorted(called_tools))}")
            _fc_log(f"报告: {len(content)}字" if content else "⚠️ 未生成报告")
            _fc_log("=" * 60)

            return content

        finally:
            # 无论成功还是异常，FC 日志都要落盘
            try:
                with open(fc_log_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(fc_lines))
                self.logger.info(f"✅ FC 日志已保存: {fc_log_path}")
            except Exception as e:
                self.logger.warning(f"保存 FC 日志失败: {e}")

    def _finalize_report(self, report: str, trade_date: str) -> tuple:
        """收尾：解析股票池 → 清理报告 → 提取预测/教训 → 返回 (cleaned_report, stock_pool)"""
        stock_pool = self._extract_stock_pool(report)
        cleaned_report = self._remove_stock_pool(report)
        cleaned_report = self._remove_predictions(cleaned_report)
        cleaned_report = cleaned_report.lstrip("-").lstrip()

        try:
            self._extract_predictions(report, trade_date)
        except Exception as e:
            self.logger.warning(f"预测提取失败（不影响主流程）: {e}")

        try:
            self._extract_lessons(cleaned_report, trade_date)
        except Exception as e:
            self.logger.warning(f"教训提取失败（不影响主流程）: {e}")

        return cleaned_report, stock_pool

    @staticmethod
    def _batch_poll_and_finalize(trade_date: str, batch_id: str) -> None:
        """子进程入口：轮询 Batch → 下载 → 收尾 → 推送 → 记录股票池"""
        import json as _json
        import os as _os
        import time as _time

        from system.config import settings as _settings
        from system.config.settings import STORAGE_PATH as _SP

        batch_base = _settings.DASHSCOPE_COMPAT_ENDPOINT
        api_key = _os.environ.get("DASHSCOPE_API_KEY", "")
        headers = {"Authorization": f"Bearer {api_key}"}
        deadline = _time.time() + _settings.BATCH_TIMEOUT_MINUTES * 60
        log_prefix = f"[Batch子进程 {batch_id[:8]}...]"

        # 用文件日志（子进程 stdout 不可见）
        log_path = _SP / "logs" / trade_date / "tasks" / "batch_finalize.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(str(log_path), "a", encoding="utf-8")

        def _log(msg):
            line = f"{_time.strftime('%Y-%m-%d %H:%M:%S')} {log_prefix} {msg}"
            log_fh.write(line + "\n")
            log_fh.flush()

        try:
            _log(f"启动，轮询间隔 5 分钟，超时 {_settings.BATCH_TIMEOUT_MINUTES} 分钟")

            # ── 轮询 ──
            status = "unknown"
            while _time.time() < deadline:
                _time.sleep(300)  # 5 分钟
                resp = requests.get(f"{batch_base}/batches/{batch_id}", headers=headers, timeout=30)
                info = resp.json()
                status = info.get("status", status)
                counts = info.get("request_counts", {})
                _log(
                    f"{status} | total={counts.get('total', '?')} completed={counts.get('completed', '?')} failed={counts.get('failed', '?')}"
                )
                if status in ("completed", "failed", "expired", "cancelled"):
                    break

            if status != "completed":
                _log(f"Batch 非正常结束: {status}")
                return

            # ── 下载结果 ──
            output_file_id = info["output_file_id"]
            resp = requests.get(f"{batch_base}/files/{output_file_id}/content", headers=headers, timeout=30)
            results = [_json.loads(line) for line in resp.text.strip().split("\n") if line.strip()]
            content = results[0]["response"]["body"]["choices"][0]["message"]["content"]
            _log(f"下载完成，报告 {len(content)} 字")

            # ── 收尾 ──
            analyzer = ReviewAnalyzer()
            cleaned_report, stock_pool = analyzer._finalize_report(content, trade_date)

            # 保存报告
            reports_dir = _SP / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            report_path = reports_dir / f"review_reports_{trade_date}.txt"
            report_path.write_text(content, encoding="utf-8")
            _log(f"报告已保存: {report_path}")

            # ── 推送 ──
            from system.config.settings import (
                TELEGRAM_PRIVATE_CHAT_ID,
                TELEGRAM_REPORT_BOT_TOKEN,
                TELEGRAM_REPORT_CHAT_ID,
            )
            from system.message import MessageSender

            targets = []
            if TELEGRAM_REPORT_CHAT_ID:
                targets.append((TELEGRAM_REPORT_CHAT_ID, "群"))
            if TELEGRAM_PRIVATE_CHAT_ID:
                targets.append((TELEGRAM_PRIVATE_CHAT_ID, "私聊"))

            for chat_id, label in targets:
                try:
                    sender = MessageSender(chat_id=chat_id, bot_token=TELEGRAM_REPORT_BOT_TOKEN)
                    sender.send(cleaned_report)
                    _log(f"推送成功 ({label})")
                except Exception as e:
                    _log(f"推送失败 ({label}): {e}")

            # ── 记录股票池 ──
            if stock_pool:
                from review.stock_tracker import StockTracker

                tracker = StockTracker()
                stocks = tracker.enrich_stock_pool(stock_pool)
                if stocks:
                    tracker.record_stocks(stocks, trade_date, cleaned_report, source="复盘")
                    _log(f"已记录 {len(stocks)} 只股票到追踪表")

            _log("✅ 收尾完成")

        except Exception as e:
            _log(f"❌ 失败: {e}")
            import traceback

            log_fh.write(traceback.format_exc() + "\n")
        finally:
            log_fh.close()

    # ═══════════════════════════════════════════════════════════
    # 预计算 FC 工具数据（消除多轮 FC 对话）
    # ═══════════════════════════════════════════════════════════

    def _precompute_all_data(
        self,
        trade_date: str,
        zt_codes: set = None,
        chain: dict = None,
        lhb_codes: list = None,
        candidate_codes: list = None,
        top_industries: list = None,
        top_concepts: list = None,
        first_board_codes: list = None,
        active_codes: list = None,
    ) -> dict:
        """
        预计算所有 FC 工具数据，消除多轮 FC 对话。

        在 AI 调用前本地执行所有工具查询，将结果嵌入 prompt，
        使 AI 可以直接基于全量数据生成报告，无需多轮 FC。

        Returns:
            dict with keys:
            - news_data_text:       CLS 复盘新闻
            - calibration_section:  昨日对账（判断+表现+校准+预测+教训）
            - zhongjun_section:     板块中军候选（嵌入六-B）
            - lhb_seats_section:    连板股席位明细（嵌入十一-B）
            - precompute_section:   个股电报+监管风险
        """
        from system.ai.stock_tools import StockTools

        tools = StockTools()
        result = {}
        start_time = time.time()

        # ── 1. 5 个必修工具 ──
        self.logger.info("预计算：5 个必修工具...")

        # CLS 复盘新闻
        try:
            cls_data = tools.get_cls_digest_news(trade_date)
            result["news_data_text"] = self._fmt_cls_news(cls_data)
            self.logger.info(f"  ✅ CLS 新闻：{cls_data.get('summary', '空')}")
        except Exception as e:
            self.logger.warning(f"  ⚠️ CLS 新闻预计算失败: {e}")
            result["news_data_text"] = "（CLS 新闻数据不可用）"

        # 昨日复盘报告（原始数据，后续 _fmt_calibration_section 统一处理）
        yr_data = {"has_report": False, "content": "", "error": None}
        try:
            yr_data = tools.get_yesterday_review(trade_date)
            self.logger.info(f"  ✅ 昨日复盘：{'有报告' if yr_data.get('has_report') else '无报告'}")
        except Exception as e:
            self.logger.warning(f"  ⚠️ 昨日复盘预计算失败: {e}")
            yr_data["error"] = str(e)

        # 历史校准（原始数据，由 _fmt_calibration_section 统一格式化）
        cal_data = {"total": 0, "error": "未查询"}
        try:
            cal_data = tools.get_historical_calibration(trade_date)
            self.logger.info(f"  ✅ 校准统计：{cal_data.get('total', 0)}只，胜率{cal_data.get('win_rate', 0)}%")
        except Exception as e:
            self.logger.warning(f"  ⚠️ 校准统计预计算失败: {e}")
            cal_data["error"] = str(e)

        # 昨日推荐表现
        yp_data = {"total": 0, "stocks": [], "error": "未查询"}
        try:
            yp_data = tools.get_yesterday_picks_performance(trade_date)
            self.logger.info(f"  ✅ 推荐表现：{yp_data.get('total', 0)}只，平均{yp_data.get('avg_change', 0):+.2f}%")
        except Exception as e:
            self.logger.warning(f"  ⚠️ 推荐表现预计算失败: {e}")
            yp_data["error"] = str(e)

        # 经验教训
        ll_data = {"total": 0, "by_type": {}, "error": "未查询"}
        try:
            ll_data = tools.get_learning_lessons()
            self.logger.info(f"  ✅ 经验教训：{ll_data.get('total', 0)}条")
        except Exception as e:
            self.logger.warning(f"  ⚠️ 经验教训预计算失败: {e}")
            ll_data["error"] = str(e)

        # ── 整合十五~十七：昨日对账 ──
        result["calibration_section"] = self._fmt_calibration_section(yr_data, yp_data, cal_data, ll_data)

        # ── 2. 提取候选股票 ──
        all_codes = self._extract_candidate_codes(
            zt_codes,
            chain,
            lhb_codes,
            candidate_codes,
            top_industries,
            top_concepts,
            first_board_codes,
            active_codes,
        )
        self.logger.info(f"预计算：候选股票 {len(all_codes)} 只，开始批量查询...")

        # ── 3. 批量电报（一次查询全部，不再逐只查）──
        all_codes_list = list(all_codes)
        telegraph_raw = tools.get_telegraph_news(stock_codes=all_codes_list, trade_date=trade_date)
        telegraph_results = {}
        news_by_code = telegraph_raw.get("news_by_code", {})
        for code in all_codes_list:
            items = news_by_code.get(code, [])
            telegraph_results[code] = {
                "stock_code": code,
                "trade_date": trade_date,
                "has_news": bool(items),
                "count": len(items),
                "items": items,
            }

        news_count = sum(1 for r in telegraph_results.values() if r.get("has_news"))
        self.logger.info(f"  ✅ 电报批量：{news_count}/{len(all_codes_list)} 只有新闻")

        # ── 4. 批量监管风险（一次查询全部）──
        regulatory_raw = tools.get_regulatory_risks(stock_codes=all_codes_list, trade_date=trade_date)
        regulatory_results = {}
        risks_by_code = regulatory_raw.get("risks_by_code", {})
        for code in all_codes_list:
            risks = risks_by_code.get(code, [])
            regulatory_results[code] = {"code": code, "risks": risks, "error": None}

        risk_count = sum(1 for r in regulatory_results.values() if r.get("risks"))
        self.logger.info(f"  ✅ 监管风险批量：{risk_count} 只有风险记录")

        # ── 5. 连板股龙虎榜席位（一次查询全部）──
        chain_codes: set[str] = set()
        if chain:
            for stocks in chain.values():
                chain_codes.update(s["code"] for s in stocks)

        chain_codes_list = list(chain_codes)
        lhb_results = {}
        if chain_codes_list:
            lhb_raw = tools.get_lhb_seats(stock_codes=chain_codes_list, trade_date=trade_date)
            seats = lhb_raw.get("seats", {})
            for code in chain_codes_list:
                s = seats.get(code, {"buy_seats": [], "sell_seats": []})
                lhb_results[code] = {"code": code, "trade_date": trade_date, **s, "error": None}
        else:
            lhb_results = {}

        self.logger.info(f"  ✅ 龙虎榜席位：{len(chain_codes)} 只连板股")

        # ── 6. 热点板块中军 ──
        hot_sectors = []
        for slist in [top_industries, top_concepts]:
            if slist:
                hot_sectors.extend(slist[:5])

        zhongjun_results = {}
        for sector in hot_sectors:
            try:
                sc = sector.get("code", "")
                sn = sector.get("name", "")
                if sc:
                    zhongjun_results[sc] = tools.get_sector_zhongjun(sector_code=sc, trade_date=trade_date)
                elif sn:
                    zhongjun_results[sn] = tools.get_sector_zhongjun(sector_name=sn, trade_date=trade_date)
            except Exception:
                pass

        valid_zj = sum(1 for r in zhongjun_results.values() if r.get("stocks") and not r.get("error"))
        self.logger.info(f"  ✅ 板块中军：{valid_zj}/{len(hot_sectors)} 个板块有数据")

        # ── 7. 组装预计算章节 ──
        zj_text = self._fmt_zhongjun_batch(zhongjun_results, hot_sectors)
        lhb_text = self._fmt_lhb_batch(lhb_results)

        result["zhongjun_section"] = zj_text or "（今日无热点板块中军数据）"
        result["lhb_seats_section"] = lhb_text or "（今日无连板股龙虎榜席位数据）"

        # 预查数据：电报 + 监管风险（中军和席位已分别嵌入对应章节）
        parts = [
            self._fmt_telegraph_batch(telegraph_results, all_codes),
            self._fmt_regulatory_batch(regulatory_results, all_codes),
        ]

        result["precompute_section"] = "\n\n".join(p for p in parts if p) or "（今日无预查数据）"

        elapsed = time.time() - start_time
        self.logger.info(
            f"✅ 预计算完成（{elapsed:.1f}s）："
            f"{len(all_codes)}只候选，{news_count}只有电报，"
            f"{risk_count}只有风险，{len(chain_codes)}只查席位，"
            f"{valid_zj}个板块中军"
        )

        return result

    # ── 候选股票提取 ──

    @staticmethod
    def _extract_candidate_codes(
        zt_codes,
        chain,
        lhb_codes,
        candidate_codes,
        top_industries,
        top_concepts,
        first_board_codes,
        active_codes,
    ) -> set[str]:
        """从所有数据源提取 AI 可能关注的股票代码"""
        codes: set[str] = set()

        if zt_codes:
            codes.update(zt_codes)
        if chain:
            for stocks in chain.values():
                codes.update(s["code"] for s in stocks)
        if lhb_codes:
            codes.update(lhb_codes)
        if candidate_codes:
            codes.update(candidate_codes)
        if first_board_codes:
            codes.update(first_board_codes)
        if active_codes:
            codes.update(active_codes)

        # 热点板块成分股
        for sector_list in [top_industries, top_concepts]:
            if not sector_list:
                continue
            for sector in sector_list[:5]:
                stocks = sector.get("stocks", [])
                if stocks:
                    codes.update(s["code"] for s in stocks)

        return codes

    # ── 格式化辅助方法 ──

    @staticmethod
    def _fmt_cls_news(cls_data: dict) -> str:
        """CLS 新闻 → prompt 文本"""
        if cls_data.get("error"):
            return f"（CLS 新闻获取失败：{cls_data['error']}）"

        sections = cls_data.get("sections", {})
        parts = []
        for key in ("focus_review", "daily_review"):
            sec = sections.get(key, {})
            if sec and sec.get("content"):
                parts.append(f"【{sec.get('title', key)}】\n{sec['content']}")

        return "\n\n".join(parts) if parts else "（今日 CLS 新闻数据为空）"

    @staticmethod
    def _fmt_calibration_section(yr_data: dict, yp_data: dict, cal_data: dict, ll_data: dict) -> str:
        """整合十五~十七：昨日对账（判断+表现+校准+预测+教训）"""
        import re

        sections = []

        # ── 昨日判断 ──
        if yr_data.get("has_report") and not yr_data.get("error"):
            content = yr_data.get("content", "")

            def _f(p, c, d="?"):
                i = c.find(p)
                if i < 0:
                    return d
                s = i + len(p)
                e = c.find("\n", s)
                return c[s:e].strip().strip("*").strip() if e > 0 else d

            stage = _f("• 当前节点判定：", content)
            main_line = _f("• 绝对主线：", content)
            sub_line = _f("• 次线/轮动暗流：", content)
            decline = _f("• 退潮方向：", content)
            safety = _f("• 大盘安全级别：", content)
            core = _f("• 核心判断：", content)

            sections.append(
                f"昨日判断：{stage} | 主线 {main_line} | 次线 {sub_line}"
                + (f" | 退潮 {decline}" if decline != "?" else "")
                + f"\n{safety}"
                + (f"\n{core}" if core != "?" else "")
            )
        else:
            sections.append("昨日判断：（无昨日复盘报告）")

        # ── 今日验证 ──
        if not yp_data.get("error") and yp_data.get("total", 0) > 0:
            stocks = yp_data.get("stocks", [])
            win = yp_data.get("win_count", 0)
            total = yp_data.get("total", 0)
            avg = yp_data.get("avg_change", 0)

            stock_strs = []
            for s in stocks:
                chg = s.get("today_change")
                chg_s = f"{chg:+.1f}%" if chg is not None else "?"
                e = "✅" if (chg or 0) > 0 else "❌"
                stock_strs.append(f"{e}{s['name']}({s.get('star', '')}){chg_s}")

            sections.append(f"今日验证：{total}只 {win}涨{total - win}跌 平均{avg:+.2f}%\n" + "  ".join(stock_strs))
        else:
            sections.append("今日验证：（无昨日推荐数据）")

        # ── 自我诊断 ──
        if not cal_data.get("error") and cal_data.get("total", 0) > 0:
            parts = [f"近{cal_data['num_days']}日胜率{cal_data['win_rate']}% 平均{cal_data['avg_return']:+.2f}%"]

            by_p = cal_data.get("by_priority", {})
            strengths = []
            weaknesses = []
            for label in ("P0", "P1", "P2"):
                p = by_p.get(label)
                if not p:
                    continue
                tag = p.get("role_name", label)
                wr = p["win_rate"]
                ar = p["avg_return"]
                item = f"{label}{tag} 胜{wr}% 均{ar:+.2f}%"
                if wr >= 50:
                    strengths.append(item)
                else:
                    weaknesses.append(item)

            if strengths:
                parts.append("强项：" + " | ".join(strengths))
            if weaknesses:
                parts.append("弱项：" + " | ".join(weaknesses))

            sections.append("自我诊断：" + "\n".join(parts))
        else:
            sections.append("自我诊断：（无历史校准数据）")

        # ── 昨日预测 ──
        if yr_data.get("has_report") and not yr_data.get("error"):
            content = yr_data.get("content", "")
            m = re.search(r"<<<PREDICTIONS>>>(.*?)<<<END>>>", content, re.DOTALL)
            if m:
                try:
                    pred = __import__("json").loads(m.group(1).strip())
                    pred_lines = []
                    # 指数预测
                    for ix in pred.get("index", []):
                        pred_lines.append(
                            f"{ix['name']}：{ix['direction']}"
                            f"（支{ix.get('support', '?')}/压{ix.get('resistance', '?')}）"
                        )
                    # 板块预测
                    for sc in pred.get("sectors", []):
                        pred_lines.append(f"{sc['name']}：{sc['prediction']}({sc['prob']}%)")
                    # 主情景
                    ds = pred.get("dominant_scenario", "")
                    if ds:
                        pred_lines.insert(0, f"主情景：{ds}")

                    sections.append("昨日预测：\n" + "\n".join(pred_lines))
                except Exception:
                    sections.append("昨日预测：（解析失败）")
            else:
                sections.append("昨日预测：（无）")
        else:
            sections.append("昨日预测：（无昨日报告）")

        # ── 关键教训 ──
        if not ll_data.get("error") and ll_data.get("total", 0) > 0:
            by_type = ll_data.get("by_type", {})
            lesson_lines = []
            # 🔴 屡犯不改 排最前面，其他按出现次数降序
            all_lessons = []
            for lt, lessons_list in by_type.items():
                for ll in lessons_list:
                    all_lessons.append((lt, ll))
            all_lessons.sort(
                key=lambda x: (
                    0 if "🔴" in x[1].get("severity", "") else 1,
                    -(x[1].get("occurrences", 0)),
                )
            )

            for lt, ll in all_lessons:
                sev = ll.get("severity", "")
                cnt = ll.get("occurrences", 0)
                lesson_line = f"{sev} [{lt}]（{cnt}次，最近{ll.get('last', '?')}）\n  {ll['lesson']}"
                lesson_lines.append(lesson_line)

            if lesson_lines:
                sections.append("关键教训：\n" + "\n".join(lesson_lines))
        else:
            sections.append("关键教训：（无）")

        return "\n\n".join(sections)

    @staticmethod
    def _fmt_calibration(cal_data: dict) -> str:
        """校准统计 → prompt 文本"""
        if cal_data.get("error"):
            return f"（校准统计数据获取失败：{cal_data['error']}）"
        if cal_data.get("total", 0) == 0:
            return "（无历史推荐数据，跳过校准）"

        lines = [
            f"近{cal_data['num_days']}日统计（{cal_data['date_range']}）："
            f"共推荐 {cal_data['total']} 只，胜率 {cal_data['win_rate']}%，"
            f"平均收益 {cal_data['avg_return']:+.2f}%，"
            f"盈亏比 {cal_data['profit_loss_ratio']}",
            f"整体诊断：{cal_data.get('overall_signal', '')}",
        ]

        by_priority = cal_data.get("by_priority", {})
        if by_priority:
            lines.append("\n按角色：")
            for label in ("P0", "P1", "P2", "P3"):
                p = by_priority.get(label)
                if not p:
                    continue
                lines.append(
                    f"  {label}（{p.get('role_name', label)}）：{p['count']}只，"
                    f"胜率{p['win_rate']}%，平均{p['avg_return']:+.2f}%"
                )

        by_sector = cal_data.get("by_sector", [])
        if by_sector:
            lines.append("\n按板块（前8）：")
            for s in by_sector[:8]:
                lines.append(f"  {s['plate']}：{s['count']}只，胜率{s['win_rate']}%，平均{s['avg_return']:+.2f}%")

        return "\n".join(lines)

    @staticmethod
    def _fmt_picks_performance(yp_data: dict) -> str:
        """昨日推荐今日表现 → prompt 文本"""
        if yp_data.get("error"):
            return f"（昨日推荐表现获取失败：{yp_data['error']}）"
        if yp_data.get("total", 0) == 0:
            return "（昨日无推荐标的）"

        lines = [
            f"昨日推荐 {yp_data['total']} 只标的今日表现："
            f"平均涨幅 {yp_data.get('avg_change', 0):+.2f}%，"
            f"上涨 {yp_data.get('win_count', 0)} 只\n"
        ]

        for s in yp_data.get("stocks", []):
            emoji = "✅" if (s.get("today_change") or 0) > 0 else "❌"
            chg = s.get("today_change")
            chg_str = f"{chg:+.2f}%" if chg is not None else "无数据"
            flags = []
            if s.get("is_limit_up"):
                flags.append("涨停")
            lines.append(
                f"  {emoji} {s['code']} {s['name']}"
                f"（{s.get('plate', '')}，{s.get('star', '')}）："
                f"{chg_str}，换手{s.get('turnover', 0)}%" + ("，" + "、".join(flags) if flags else "")
            )

        return "\n".join(lines)

    @staticmethod
    def _fmt_lessons(ll_data: dict) -> str:
        """经验教训 → prompt 文本"""
        if ll_data.get("error"):
            return f"（经验教训获取失败：{ll_data['error']}）"
        if ll_data.get("total", 0) == 0:
            return "（暂无历史经验教训）"

        lines = [f"共 {ll_data['total']} 条经验教训："]
        by_type = ll_data.get("by_type", {})
        for lesson_type, lessons in by_type.items():
            lines.append(f"\n【{lesson_type}】（{len(lessons)}条）")
            for ll in lessons:
                lines.append(
                    f"  {ll.get('severity', '')} {ll.get('lesson', '')}"
                    f"（出现{ll.get('occurrences', 0)}次，"
                    f"最近{ll.get('last', '?')}）"
                )

        return "\n".join(lines)

    @staticmethod
    def _fmt_telegraph_batch(results: dict, all_codes: set) -> str:
        """批量电报 → prompt 摘要"""
        with_news = []
        without_news = []

        for code in sorted(all_codes):
            r = results.get(code, {})
            if r.get("has_news"):
                items = r.get("items", [])
                summaries = []
                for item in items[:2]:  # 每只最多2条
                    title = (item.get("title", "") or "")[:60]
                    sentiment = item.get("sentiment", "")
                    tag = f" [{sentiment}]" if sentiment else ""
                    summaries.append(f"{title}{tag}")
                with_news.append(f"  {code}：{'；'.join(summaries)}（共{len(items)}条）")
            elif not r.get("error"):
                without_news.append(code)

        lines = ["### 个股电报速查"]
        if with_news:
            lines.append(f"有电报（{len(with_news)}只）：")
            lines.extend(with_news)
        if without_news:
            preview = ", ".join(sorted(without_news)[:15])
            suffix = f"...等共{len(without_news)}只" if len(without_news) > 15 else ""
            lines.append(f"无电报（{len(without_news)}只）：{preview}{suffix}")

        return "\n".join(lines)

    @staticmethod
    def _fmt_regulatory_batch(results: dict, all_codes: set) -> str:
        """批量监管风险 → prompt 摘要"""
        with_risks = []
        clean = 0

        for code in sorted(all_codes):
            r = results.get(code, {})
            risks = r.get("risks", [])
            if risks:
                for risk in risks[:2]:
                    with_risks.append(
                        f"  ⚠️ {code}：{risk.get('risk_type', '未分类')}"
                        f"（Lv{risk.get('risk_level', 1)}，"
                        f"{risk.get('issuer', '')}）"
                        f" — {risk.get('title', '')[:80]}"
                    )
            elif not r.get("error"):
                clean += 1

        lines = ["### 监管风险速查"]
        if with_risks:
            lines.append(f"有风险（{len(with_risks)}条）：")
            lines.extend(with_risks)
        if clean > 0:
            prefix = "其余" if with_risks else "全部"
            lines.append(f"{prefix} {clean} 只候选标的无监管风险记录")

        return "\n".join(lines)

    @staticmethod
    def _fmt_zhongjun_batch(results: dict, hot_sectors: list) -> str:
        """批量板块中军 → prompt 摘要"""
        lines = []
        has_data = False

        for sector in hot_sectors:
            code = sector.get("code", "")
            name = sector.get("name", "")
            key = code or name
            r = results.get(key, {})

            if r.get("error") or not r.get("stocks"):
                continue

            has_data = True
            stocks = r.get("stocks", [])[:3]
            trend_map = {
                "full": "多头排列",
                "ma5_above": "MA5>MA20",
                "none": "弱势",
            }
            stock_strs = []
            for s in stocks:
                trend_label = trend_map.get(s.get("trend", ""), s.get("trend", ""))
                chg = s.get("change")
                chg_str = f"/涨{chg:.1f}%" if chg else ""
                stock_strs.append(f"{s['code']} {s['name']}（{s.get('mcap', 0)}亿/{trend_label}{chg_str}）")

            resolved_name = r.get("sector_name", name)
            resolved_code = r.get("sector_code", code)
            lines.append(f"  {resolved_name}（{resolved_code}）：" + "、".join(stock_strs))

        return "\n".join(lines) if has_data else ""

    @staticmethod
    def _fmt_lhb_batch(results: dict) -> str:
        """批量龙虎榜席位 → prompt 摘要"""
        lines = ["### 龙虎榜席位速查（连板股≥2板）"]
        has_data = False

        for code, r in sorted(results.items()):
            if r.get("error"):
                continue

            buy_seats = r.get("buy_seats", [])
            if not buy_seats:
                continue

            has_data = True
            top_buyers = []
            for s in buy_seats[:3]:
                name = (s.get("name", "?") or "?")[:12]
                buy = s.get("buy", 0) or 0
                top_buyers.append(f"{name}（买{buy:.0f}万）")

            lines.append(f"  {code}：{'、'.join(top_buyers)}")

        return "\n".join(lines) if has_data else ""

    # 电报类别黑名单：与 A 股选股无关的类别
    TELEGRAPH_SKIP_CATEGORIES = {"期货市场情报", "原油市场动态", "环球市场情报"}
    # 标题过滤词：宏观/政治/大盘描述/重复新闻汇编
    TELEGRAPH_SKIP_KEYWORDS = [
        "习近平同",
        "普京",
        "伊朗",
        "投资日历",
        "隔夜全球要闻",
        "LPR报价",
        "财政收入",
        "发改委",
        "国务院令",
        "矿产资源",
        "李强签署",
        "商务部美大司",
        "外交部：",
        "三大指数",
        "沪深两市成交额突破",
        "早间新闻精选",
        "午间新闻精选",
    ]

    def _query_telegraph(self, trade_date: str) -> list:
        """从 DB 查询当日高评分电报，标准化标签字段"""
        import json as _json

        try:
            conn = connect(DATABASE_PATH)
            # conn.row_factory = sqlite3.Row  # connect() 已设置
            cursor = conn.execute(
                """
                SELECT * FROM cls_telegraph
                WHERE trade_date = ?
                  AND score >= 2
                ORDER BY score DESC, reading_num DESC
                LIMIT 120
            """,
                (trade_date,),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()

            # 解析 JSON 字段 + 过滤
            filtered = []
            seen_titles = set()
            for r in rows:
                # 解析 CLS JSON 字段
                for field in ("stock_tags", "subject_tags", "plate_tags"):
                    try:
                        r[field] = _json.loads(r[field]) if r[field] else []
                    except (_json.JSONDecodeError, TypeError):
                        r[field] = []

                # 手工过滤：跳过无关类别/关键词
                cat = r.get("category", "")
                if cat in self.TELEGRAPH_SKIP_CATEGORIES:
                    continue
                title = r.get("title", "")
                if any(kw in title for kw in self.TELEGRAPH_SKIP_KEYWORDS):
                    continue

                # 去重
                title_key = title[:20]
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                filtered.append(r)
                if len(filtered) >= 35:
                    break

            return filtered
        except Exception as e:
            self.logger.warning(f"电报 DB 查询失败: {e}")
            return []

    def _format_news_data(self, news_data: dict = None) -> str:
        """格式化复盘新闻数据为 Prompt 文本"""
        if not news_data:
            return "（无复盘新闻数据）"

        text = ""

        # 焦点复盘
        focus_review = news_data.get("focus_review", {})
        if focus_review:
            text += "\n【财联社焦点复盘】\n"
            text += f"标题：{focus_review.get('title', '无')}\n"
            text += f"时间：{focus_review.get('time', '无')}\n"
            text += f"来源：{focus_review.get('source', '无')}\n"
            text += f"字数：{focus_review.get('word_count', 0)}字\n\n"
            text += f"{focus_review.get('content', '无内容')}\n"

        # 每日收评
        daily_review = news_data.get("daily_review", {})
        if daily_review:
            text += f"\n{'=' * 80}\n"
            text += "【财联社每日收评】\n"
            text += f"标题：{daily_review.get('title', '无')}\n"
            text += f"时间：{daily_review.get('time', '无')}\n"
            text += f"来源：{daily_review.get('source', '无')}\n"
            text += f"字数：{daily_review.get('word_count', 0)}字\n\n"
            text += f"{daily_review.get('content', '无内容')}\n"

        if not text:
            return "（无复盘新闻数据）"

        return text

    def _format_telegraph(self, trade_date: str, zt_codes: set = None) -> str:
        """从 DB 读取电报并格式化为 Prompt 文本"""
        telegraph_list = self._query_telegraph(trade_date)
        if not telegraph_list:
            return "（今日无高评分电报）"

        zt_codes = zt_codes or set()

        # 按 category 分组
        groups: dict[str, list] = {}
        for t in telegraph_list:
            cat = t.get("category", "其他")
            groups.setdefault(cat, []).append(t)

        text = f"共 {len(telegraph_list)} 条高评分电报，按类别分组如下：\n"

        for cat, items in groups.items():
            text += f"\n【{cat}】（{len(items)}条）\n"
            for item in items:
                level = item.get("level", "C")
                title = item.get("title", "无标题")

                # 摘要：content 前 80 字
                content = item.get("content", "")
                snippet = (content[:80] + "...") if len(content or "") > 80 else (content or "")

                # 关联股票
                stock_tags = item.get("stock_tags", [])

                # 涨停交叉比对
                zt_matched = []
                for s in stock_tags or []:
                    code = s.get("code", "") if isinstance(s, dict) else ""
                    if code and code in zt_codes:
                        zt_matched.append(s.get("name", code) if isinstance(s, dict) else code)

                zt_flag = " 🔥涨停关联" if zt_matched else ""
                line = f"- [{level}]{zt_flag} {title}"
                if snippet and snippet != title:
                    line += f" | {snippet}"

                # 关联股票展示（涨停股前置，最多 3 只）
                if stock_tags:
                    tags_parts = []
                    for s in stock_tags[:3]:
                        if isinstance(s, dict):
                            name = s.get("name", "")
                            code = s.get("code", "")
                            prefix = "🔥" if code in zt_codes else ""
                            tag_text = f"{prefix}{name}({code})"
                            tags_parts.append(tag_text)
                    if tags_parts:
                        line += f"\n  关联：{'、'.join(tags_parts)}"
                elif zt_matched:
                    line += f"\n  涨停关联：{'、'.join(zt_matched)}"

                text += line + "\n"

        return text
