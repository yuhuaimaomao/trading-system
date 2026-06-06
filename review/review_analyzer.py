"""
复盘 AI 分析器 v3.0（刺客风格）

职责：查询原始数据 → 格式化 Prompt → 调用 AI → 返回复盘报告
不做数据采集、不做消息推送

核心改变：程序采集 + 清洗数据，AI 做判断 + 推演。
不给 AI 喂加工过的「得分」，只喂原始数据 + 环比对比，让 AI 自己定性。
"""

import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from system.config import settings
from system.config.settings import DATABASE_PATH, LOGS_DIR
from system.utils.dns_bypass import install as install_dns_bypass

# 绕过 Shadowrocket/Surge 本地代理的 DNS 劫持
install_dns_bypass()
from data.readers.limit_pool_reader import LimitPoolReader
from data.readers.sector_reader import SectorReader
from data.readers.stock_reader import StockReader
from review.review_formatter import (
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
    format_trend_stocks,
    format_yzt_performance,
)
from system.ai import ai
from system.ai.function_calling import FunctionCallingEngine
from system.ai.prompts.review import REVIEW_REPORT_PROMPT
from system.utils.logger import get_core_logger


class ReviewAnalyzer:
    """复盘 AI 分析器（刺客风格）"""

    def __init__(self):
        self.logger = get_core_logger("review_analyzer")
        # 加载板块名称→编码映射（用于 formatter 输出 sector_code）
        self.sector_code_map = {}
        try:
            conn = sqlite3.connect(str(DATABASE_PATH))
            try:
                cursor = conn.execute(
                    "SELECT sector_code, sector_name FROM sector_info"
                )
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
            f"开始复盘 AI 分析 v3.0（刺客风格）{trade_date}（D-3:{day_before_before}, D-2:{day_before}, D-1:{yesterday}）..."
        )
        start_time = time.time()

        # CLS 复盘新闻已在采集阶段落盘（collect() 模块 13），分析阶段直接通过 FC 工具读取
        # 电报由 FC 工具 get_telegraph_news 直接从 DB 查询

        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.row_factory = sqlite3.Row

        try:
            # 读取昨日复盘报告（AI 自我校准）
            # 文件名格式 review_reports_{date}_{model}.txt，需 glob 匹配模型后缀
            yesterday_report = ""
            reports_dir = Path(__file__).parent.parent.parent / "storage" / "reports"
            yesterday_matches = sorted(
                reports_dir.glob(f"review_reports_{yesterday}_*.txt")
            )
            if yesterday_matches:
                yesterday_report = yesterday_matches[-1].read_text(encoding="utf-8")
                self.logger.info(
                    f"✅ 已加载昨日复盘报告（{len(yesterday_report)}字）from {yesterday_matches[-1].name}"
                )
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
            up_ratio = (
                market["up_count"] / market["total"] if market["total"] > 0 else 0
            )
            up_down_ratio = f"{market['up_count'] / max(market['down_count'], 1):.2f}"
            up_down_value = market["up_count"] / max(market["down_count"], 1)

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
                turnover_change = (
                    (market["turnover"] - prev_turnover) / prev_turnover * 100
                )
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
            prev_seal_rate = (
                (prev_limit_up / prev_touched * 100) if prev_touched > 0 else 0
            )

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
            chain, chain_count, highest_board = LimitPoolReader.get_chain_ladder(
                conn, trade_date
            )

            # 补充概念板块
            chain_codes = [s["code"] for stocks in chain.values() for s in stocks]
            chain_concepts = SectorReader.enrich_concepts(conn, trade_date, chain_codes)
            for stocks in chain.values():
                for s in stocks:
                    concepts = chain_concepts.get(s["code"], [])
                    if concepts:
                        s["concepts"] = concepts

            # 昨日连板（环比）
            prev_chain_count, prev_highest_board = LimitPoolReader.get_prev_chain_stats(
                conn, yesterday
            )
            chain_count_change = chain_count - prev_chain_count

            # ===== 3.05. 连板晋级率（近 3 日）=====
            self.logger.info("计算近 3 日连板晋级率...")
            prev_chain, _, _ = LimitPoolReader.get_chain_ladder(conn, yesterday)
            d2_chain, _, d2_highest = LimitPoolReader.get_chain_ladder(conn, day_before)
            d3_chain, _, d3_highest = LimitPoolReader.get_chain_ladder(
                conn, day_before_before
            )

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
                    "rates": _calc_promotion(
                        d3_chain, d3_highest, d2_chain, d2_highest
                    ),
                },
                {
                    "label": f"{day_before}→{yesterday}",
                    "rates": _calc_promotion(
                        d2_chain, d2_highest, prev_chain, prev_highest_board
                    ),
                },
                {
                    "label": f"{yesterday}→今日",
                    "rates": _calc_promotion(
                        prev_chain, prev_highest_board, chain, highest_board
                    ),
                },
            ]

            # ===== 3.3. 炸板明细 =====
            self.logger.info("查询炸板明细...")
            broken_records = LimitPoolReader.get_broken_boards(conn, trade_date)

            # 补充概念板块
            broken_codes = [r["code"] for r in broken_records]
            broken_concepts = SectorReader.enrich_concepts(
                conn, trade_date, broken_codes
            )
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
            d2_up_ratio = (
                d2_market["up_count"] / d2_market["total"]
                if d2_market and d2_market["total"] > 0
                else 0
            )
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
            d1_up_ratio = (
                d1_row["up_count"] / d1_row["total"]
                if d1_row and d1_row["total"] > 0
                else 0
            )
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
            concept_sectors, concept_fund_map = SectorReader.get_concept_sectors(
                conn, trade_date
            )

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
            candidate_concepts = SectorReader.enrich_concepts(
                conn, trade_date, candidate_codes
            )
            for c in candidates:
                concepts = candidate_concepts.get(c["code"], [])
                if concepts:
                    c["concepts"] = concepts

            # ===== 6.5. 近期强势股（60日新高+多次涨停，凑够30只）=====
            self.logger.info("查询近期强势股...")
            active_stocks = StockReader.get_strong_stocks(conn, trade_date, sectors)

            # 补充概念板块
            active_codes = [s["stock_code"] for s in active_stocks]
            active_concepts = SectorReader.enrich_concepts(
                conn, trade_date, active_codes
            )
            for s in active_stocks:
                concepts = active_concepts.get(s["stock_code"], [])
                if concepts:
                    s["concepts"] = concepts

            # ===== 6.7. 趋势股（双模式：5日线强趋势 + 20日线稳健趋势）=====
            self.logger.info("查询趋势股（双模式）...")
            trend_data = StockReader.get_trend_stocks(conn, trade_date)
            strong_trend = trend_data.get("strong", [])
            normal_stocks = trend_data.get("normal", [])
            self.logger.info(
                f"趋势股：强趋势{len(strong_trend)}只 + 稳健趋势{len(normal_stocks)}只"
            )

            # 补充概念板块（两类合并查询）
            all_trend_codes = [s["stock_code"] for s in strong_trend + normal_stocks]
            trend_concepts = SectorReader.enrich_concepts(
                conn, trade_date, all_trend_codes
            )
            for s in strong_trend + normal_stocks:
                concepts = trend_concepts.get(s["stock_code"], [])
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
                self.logger.info(
                    f"行业热点TOP10：{', '.join(s['name'] for s in top_industries[:5])}..."
                )
            else:
                self.logger.info("无行业热点数据")
            if top_concepts:
                self.logger.info(
                    f"概念热点TOP10：{', '.join(s['name'] for s in top_concepts[:5])}..."
                )
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
            top3_inflow = (
                sum(all_sector_flows[:3]) / 100000000 if all_sector_flows else 0
            )
            top3_pct = (top3_inflow / total_inflow * 100) if total_inflow > 0 else 0
            capital_concentration = {
                "top3_pct": top3_pct,
                "total_inflow": total_inflow,
            }

            # 昨日涨停平均溢价率
            yzt_avg_change = (
                sum(r.get("change", 0) for r in yzt_records) / len(yzt_records)
                if yzt_records
                else 0
            )

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
            ANNOUNCEMENT_WHITELIST = [
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
            placeholders = ",".join("?" * len(ANNOUNCEMENT_WHITELIST))
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
                ANNOUNCEMENT_WHITELIST + [trade_date],
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
            index_ma_health = (
                (healthy_indices / total_indices * 100) if total_indices > 0 else 50
            )

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
                # 三、财联社复盘新闻（必修 FC 工具）
                "news_data_text": "**你必须立即调用 get_cls_digest_news 工具获取财联社复盘新闻。这是必修工具，包含 AI 编辑撰写的高质量盘后总结，不可跳过。**",
                # 四、财联社盘中电报（FC 按股查询，直接从 DB 读）
                "telegraph_text": "盘中电报已采集。如需查询某只股票今天是否有相关电报新闻，调用 get_telegraph_news(stock_code) 工具，直接从数据库查询该股的盘中快讯。",
                # 五、连板梯队（含首板苗子+炸板明细）
                "chain_ladder_text": format_chain_ladder(
                    chain, promotion_rates, sector_code_map=self.sector_code_map
                ),
                "first_boards_text": format_first_boards(
                    first_board_records, sector_code_map=self.sector_code_map
                ),
                "broken_boards_text": format_broken_boards(
                    broken_records,
                    broken_trend={"d2": d2_broken, "d1": prev_broken, "d": broken},
                    sector_code_map=self.sector_code_map,
                ),
                # 六、热点板块数据
                "hotspot_text": format_hotspot(
                    top_industries, top_concepts, sector_code_map=self.sector_code_map
                ),
                # 七、板块资金暗流
                "fund_flow_text": (
                    "【行业】\n"
                    + format_fund_flow(sectors, fund_flow_map)
                    + "\n【概念】\n"
                    + format_fund_flow(concept_sectors, concept_fund_map)
                ),
                "capital_concentration_text": format_capital_concentration(
                    capital_concentration
                ),
                # 八、今日异动股
                "candidate_count": len(candidates),
                "candidate_text": format_candidates(
                    candidates, sector_code_map=self.sector_code_map
                ),
                # 九、龙虎榜全量
                "lhb_text": format_lhb_full(lhb_data),
                # 九-B、趋势股（双模式）
                "trend_count": len(strong_trend) + len(normal_stocks),
                "trend_stocks_text": format_trend_stocks(
                    trend_data, sector_code_map=self.sector_code_map
                ),
                # 十、近期强势股
                "strong_count": len(active_stocks),
                "strong_stocks_text": format_strong_stocks(
                    active_stocks, sector_code_map=self.sector_code_map
                ),
                # 十一、昨日涨停股今日表现
                "yzt_count": len(yzt_records),
                "yzt_text": format_yzt_performance(
                    yzt_records, sector_code_map=self.sector_code_map
                ),
                # 十二、昨日 AI 推荐标的今日验证
                "yesterday_watchlist_text": "（调用 get_yesterday_picks_performance 工具查看昨日推荐标的今日表现，对比昨日预判和今日实际）",
                # 十三、风险地雷
                "share_holder_text": format_risk_flags(
                    share_holder_changes, candidates
                ),
                "monitor_text": format_risk_flags(
                    stock_monitors, candidates, label="监控"
                ),
                # 十四、今日重要公告
                "announcements_text": format_announcements(important_announcements),
                # 十五、AI 推荐历史校准
                "calibration_stats_text": "（调用 get_historical_calibration 工具查看近5日推荐胜率和板块表现统计，用于自我校准）",
                # 十六、昨日复盘回顾
                "yesterday_review_text": "（调用 get_yesterday_review 工具查看昨日复盘报告全文，对比昨日预判和今日实际盘面）",
                # 仓位
                "position_cap": position_cap,
            }

            prompt = REVIEW_REPORT_PROMPT.format(**prompt_data)

            # 保存 Prompt 到日志目录
            try:
                prompt_dir = LOGS_DIR / trade_date / "prompts"
                prompt_dir.mkdir(parents=True, exist_ok=True)
                prompt_path = prompt_dir / "review_prompt.txt"
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(prompt_path, "w", encoding="utf-8") as f:
                    f.write("=" * 80 + "\n")
                    f.write(f"复盘 AI Prompt 调试日志 - {current_time}\n")
                    f.write("=" * 80 + "\n\n")
                    f.write("【完整 Prompt 内容】\n\n")
                    f.write(prompt)
                    f.write("\n\n" + "=" * 80 + "\n")
                    f.write(f"Prompt 总字数：{len(prompt)}字\n")
                    f.write("=" * 80 + "\n")
                self.logger.info(f"✅ 复盘 Prompt 已保存到：{prompt_path}")
            except Exception as e:
                self.logger.warning(f"保存 Prompt 日志失败：{e}")

            # ===== 12. 调用 AI（FC 多轮对话）=====
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
                "8. 报告末尾必须包含 <<<STOCKS>>> 股票池 JSON（包含第六节所有游资标的 + 第七节所有趋势票），这是强制性要求"
                "9. 工具调用策略：调用 get_telegraph_news / get_regulatory_risks / get_lhb_seats 核查标的后，有监管风险的直接剔除不展示，有利好消息的才在个股下方标注"
                "10. 报告中不要出现任何工具名称（如 get_cls_digest_news、get_telegraph_news 等），不要写「数据来源：XX工具返回」，直接用分析结论"
                "11. **绝对禁止**在报告中出现：工具调用过程、打分排名（如'得分XX分'）、prompt指令、数据筛选条件——报告只呈现判断结论和逻辑推演"
                "12. 第七节「趋势交易者精选」用趋势交易思维：从趋势股和板块中军候选中选3-5只，优先蓄力期/主升初期的票（均线刚多头排列、还没大幅拉升），排除当日涨停和一日游板块的票"
            )

            models_to_run = [
                (settings.AI_MODEL, settings.AI_MODEL),
            ]

            reports = {}  # {model_name: report_text}

            for model_name, model_label in models_to_run:
                pass  # model override no longer needed (use system.ai)
                self.logger.info(
                    f"调用 {model_label} AI 生成复盘报告（FC 多轮对话，Prompt {len(prompt)}字）..."
                )
                try:
                    report_text = self._run_fc_review(prompt, trade_date, system_prompt)
                except Exception as e:
                    self.logger.error(f"❌ {model_label} AI 调用异常: {e}")
                    reports[model_name] = None
                    continue
                reports[model_name] = report_text
                self.logger.info(
                    f"✅ {model_label} 报告生成完成（{len(report_text)}字）"
                )

            # 保存报告到文件（供下次复盘自我校准）
            try:
                reports_dir = (
                    Path(__file__).parent.parent.parent / "storage" / "reports"
                )
                reports_dir.mkdir(parents=True, exist_ok=True)

                for model_name, report_text in reports.items():
                    if report_text is None:
                        continue
                    suffix = f"_{model_name}"
                    report_path = (
                        reports_dir / f"review_reports_{trade_date}{suffix}.txt"
                    )
                    report_path.write_text(report_text, encoding="utf-8")
                    self.logger.info(f"✅ {model_name} 复盘报告已保存到 {report_path}")
            except Exception as e:
                self.logger.warning(f"保存复盘报告失败（不影响主流程）：{e}")

            report = reports.get(settings.AI_MODEL)
            if not report:
                report = next((v for v in reports.values() if v), None)
            if not report:
                raise RuntimeError("所有模型均调用失败，无法生成复盘报告")

            elapsed = time.time() - start_time
            self.logger.info(f"✅ 复盘 AI 分析完成，耗时 {elapsed:.1f}秒")

            # 解析股票池
            stock_pool = self._extract_stock_pool(report)
            cleaned_report = self._remove_stock_pool(report)
            cleaned_report = self._remove_predictions(cleaned_report)
            # 去掉 AI 输出开头的 markdown 分隔线
            cleaned_report = cleaned_report.lstrip("-").lstrip()

            # 提取预测
            try:
                self._extract_predictions(report, trade_date)
            except Exception as e:
                self.logger.warning(f"预测提取失败（不影响主流程）: {e}")

            # 提取经验教训
            try:
                self._extract_lessons(cleaned_report, trade_date)
            except Exception as e:
                self.logger.warning(f"教训提取失败（不影响主流程）: {e}")

            return cleaned_report, stock_pool

        except Exception as e:
            self.logger.error(f"❌ 复盘 AI 分析失败：{e}", exc_info=True)
            return f"AI 分析失败：{e}", []
        finally:
            conn.close()

    def _extract_predictions(self, report_text: str, trade_date: str):
        """从报告中解析 <<<PREDICTIONS>>> 结构化预测，存入 review_predictions 表"""
        import json as _json
        import sqlite3

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

        conn = sqlite3.connect(str(DATABASE_PATH))
        count = 0
        # 指数预测
        for idx in data.get("index", []):
            conn.execute(
                "INSERT INTO review_predictions (push_date, pred_type, target_name, pred_direction, pred_detail, prob) VALUES (?, 'index', ?, ?, ?, ?)",
                (
                    trade_date,
                    idx.get("name", ""),
                    idx.get("direction", ""),
                    f"支撑{idx.get('support', '?')}/压力{idx.get('resistance', '?')}",
                    None,
                ),
            )
            count += 1
        # 板块预测
        for sec in data.get("sectors", []):
            conn.execute(
                "INSERT INTO review_predictions (push_date, pred_type, target_name, pred_direction, pred_detail, prob) VALUES (?, 'sector', ?, ?, '', ?)",
                (
                    trade_date,
                    sec.get("name", ""),
                    sec.get("prediction", ""),
                    sec.get("prob"),
                ),
            )
            count += 1
        # 主导情景
        scenario = data.get("dominant_scenario", "")
        if scenario:
            conn.execute(
                "INSERT INTO review_predictions (push_date, pred_type, target_name, pred_direction, pred_detail, prob) VALUES (?, 'scenario', '主导情景', ?, '第五节日均推演', NULL)",
                (trade_date, scenario),
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
        import sqlite3

        # 只取报告中的偏差校准 + 自我诊断部分，减少 prompt 长度
        cali_match = re.search(
            r"(?:昨日预测偏差校准|自我诊断|[📊⚠️].*?校准).*?(?:\n\n|\n(?=🎯|📈|⚔️|📊))",
            report_text,
            re.DOTALL,
        )
        excerpt = cali_match.group(0)[:1200] if cali_match else report_text[-3000:]

        # 拉取已有教训（用于合并对比）
        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.row_factory = sqlite3.Row
        existing = [
            dict(r)
            for r in conn.execute(
                "SELECT id, lesson_type, lesson_key, lesson_content, occurrence_count FROM review_lessons WHERE is_active=1"
            ).fetchall()
        ]
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
1. 不是"XX股票判断错了"这种个股层面，而要提炼成模式。例如"防御切换期追高日内强势股，次日低开被套"、"分歧期龙头炸板后回流失败，追板中位股被埋"
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
      "lesson_content": "防御切换期追涨日内强势的防御板块标的，次日高开低走被套。防御板块需验证持续性再推，不能因为它今天涨了就推荐"
    }}
  ]
}}
"""
        try:
            result_text = ai.chat(extract_prompt, model="review", max_tokens=800)
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
        conn = sqlite3.connect(str(DATABASE_PATH))
        new_count, merge_count = 0, 0
        for lesson in lessons:
            lt = lesson.get("lesson_type", "")
            lk = lesson.get("lesson_key", "")
            lc = lesson.get("lesson_content", "")
            if not lt or not lk or not lc:
                continue
            # 尝试更新已有教训
            cur = conn.execute(
                "UPDATE review_lessons SET occurrence_count=occurrence_count+1, last_date=?, lesson_content=?, is_active=1 WHERE lesson_type=? AND lesson_key=?",
                (trade_date, lc, lt, lk),
            )
            if cur.rowcount > 0:
                merge_count += 1
                self.logger.info(f"  合并教训: [{lt}] {lk}")
            else:
                conn.execute(
                    "INSERT INTO review_lessons (lesson_type, lesson_key, lesson_content, occurrence_count, first_date, last_date, is_active) VALUES (?, ?, ?, 1, ?, ?, 1)",
                    (lt, lk, lc, trade_date, trade_date),
                )
                new_count += 1
                self.logger.info(f"  新增教训: [{lt}] {lk}")
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

        cleaned = re.sub(
            r"<<<PREDICTIONS>>>.*?<<<END>>>", "", report_text, flags=re.DOTALL
        )
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

    def _run_fc_review(self, prompt: str, trade_date: str, system_prompt: str) -> str:
        """
        FC 多轮对话：一轮暴露全部工具，AI 自由选择调用顺序。

        事后校验：AI 返回报告内容时，检查必修工具是否都已调用。
        未调完的，追加消息要求继续调用，最多 10 轮防止死循环。
        """
        from system.ai.stock_tools import TOOLS_DEFINITION

        fc_engine = FunctionCallingEngine()
        all_tool_names = [t["function"]["name"] for t in TOOLS_DEFINITION]

        # 报告生成前必须调用的工具（get_sector_zhongjun 在选股环节调用，不在此列）
        MANDATORY_TOOLS = [
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
        _fc_log("模型: review (system.ai)")
        _fc_log(f"可用工具 ({len(all_tool_names)}): {', '.join(all_tool_names)}")
        _fc_log(f"必修工具 ({len(MANDATORY_TOOLS)}): {', '.join(MANDATORY_TOOLS)}")
        _fc_log("=" * 60)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        called_tools = set()
        round_num = 0
        MAX_FC_ROUNDS = 10  # 防止死循环
        content = ""  # 确保 finally 块可访问

        try:
            while True:
                round_num += 1
                if round_num > MAX_FC_ROUNDS:
                    _fc_log(f"  ⚠️ 已达最大轮次 {MAX_FC_ROUNDS}，强制终止")
                    break
                tools = TOOLS_DEFINITION
                tool_choice = "auto"
                mandatory_done = set(MANDATORY_TOOLS).issubset(called_tools)
                fc_timeout = 600 if mandatory_done else 180
                _fc_log(
                    f"\n第 {round_num} 轮（全部 {len(all_tool_names)} 个工具，tool_choice=auto，超时{fc_timeout}s）"
                )

                response = ai.chat_with_tools_raw(
                    messages,
                    model="review",
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=4000,
                )

                content = response.get("content", "")
                tool_calls = response.get("tool_calls", [])

                if content:
                    preview = content[:200].replace("\n", "\\n")
                    _fc_log(f"  AI 文本回复: {len(content)}字 → {preview}...")

                if tool_calls:
                    _fc_log(f"  工具调用: {len(tool_calls)} 个")
                    for tc in tool_calls:
                        fn = (
                            tc.get("function", {})
                            if isinstance(tc, dict)
                            else tc.function
                        )
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
                            result_str = (
                                result_str[:500] + f"...(共{len(result_str)}字)"
                            )
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
                    missing = [t for t in MANDATORY_TOOLS if t not in called_tools]
                    if missing:
                        _fc_log(f"  ⚠️ 检测到报告内容，但必修工具未调用: {missing}")
                        messages.append(
                            {
                                "role": "user",
                                "content": f"请先调用以下必修工具获取数据，再生成报告：{', '.join(missing)}。不要在工具调用前输出报告正文。",
                            }
                        )
                        continue
                    # 全部必修工具已调用，接受报告
                    _fc_log(
                        f"  ✅ 所有必修工具已调用 ({len(called_tools)} 个)，报告完成"
                    )
                    break

                # 不是报告、也没调工具，可能是中间回复，推回继续
                _fc_log("  AI 返回中间回复，继续等待工具调用")
                messages.append({"role": "assistant", "content": content})
                continue

            _fc_log(
                f"\n共 {round_num} 轮，已调用工具: {', '.join(sorted(called_tools))}"
            )
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
        """从 DB 查询当日高评分电报，优先用 AI 结构化字段，旧字段做 fallback"""
        import json as _json

        try:
            conn = sqlite3.connect(str(DATABASE_PATH))
            conn.row_factory = sqlite3.Row
            # AI 已处理的用 ai_importance>=3 且排除 ai_status='skipped'，
            # 未处理的用旧 score>=2 并保留手工过滤逻辑
            cursor = conn.execute(
                """
                SELECT * FROM cls_telegraph
                WHERE trade_date = ?
                  AND (
                    (ai_status = 'done' AND ai_importance >= 3 AND ai_status != 'skipped')
                    OR
                    ((ai_status IS NULL OR ai_status = 'pending' OR ai_status = 'failed') AND score >= 2)
                  )
                ORDER BY COALESCE(ai_importance, score) DESC, reading_num DESC
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
                # 解析 CLS 原始 JSON 字段
                for field in ("stock_tags", "subject_tags", "plate_tags"):
                    try:
                        r[field] = _json.loads(r[field]) if r[field] else []
                    except (_json.JSONDecodeError, TypeError):
                        r[field] = []

                # 解析 AI 结构化 JSON 字段
                for field in ("ai_stocks", "ai_sectors"):
                    try:
                        r[field] = _json.loads(r[field]) if r[field] else []
                    except (_json.JSONDecodeError, TypeError):
                        r[field] = []

                # AI 已处理的不再做手工关键词过滤，AI 的 ai_status='skipped' 已在 SQL 排除
                ai_status = r.get("ai_status", "")
                if ai_status not in ("done",):
                    # 未处理的电报：保留手工过滤
                    cat = r.get("category", "")
                    if cat in self.TELEGRAPH_SKIP_CATEGORIES:
                        continue

                    title = r.get("title", "")
                    if any(kw in title for kw in self.TELEGRAPH_SKIP_KEYWORDS):
                        continue

                # 去重
                title_key = (r.get("title") or "")[:20]
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
        """从 DB 读取电报并格式化为 Prompt 文本，优先用 AI 结构化字段"""
        telegraph_list = self._query_telegraph(trade_date)
        if not telegraph_list:
            return "（今日无高评分电报）"

        zt_codes = zt_codes or set()

        # 按 ai_direction 分组（fallback 到旧 category）
        groups: dict[str, list] = {}
        for t in telegraph_list:
            cat = t.get("ai_direction") or t.get("category", "其他")
            groups.setdefault(cat, []).append(t)

        text = f"共 {len(telegraph_list)} 条高评分电报，按类别分组如下：\n"

        for cat, items in groups.items():
            text += f"\n【{cat}】（{len(items)}条）\n"
            for item in items:
                level = item.get("level", "C")
                title = item.get("title", "无标题")
                reading = item.get("reading_num", 0)

                # 摘要：优先 ai_summary，其次 content 前 80 字
                ai_summary = item.get("ai_summary", "")
                snippet = (
                    ai_summary
                    if ai_summary
                    else (
                        (item.get("content", "")[:80] + "...")
                        if len(item.get("content", "") or "") > 80
                        else (item.get("content", "") or "")
                    )
                )

                # 关联股票：优先 ai_stocks（覆盖率高），fallback 到 stock_tags
                ai_stocks = item.get("ai_stocks", [])
                stock_tags = item.get("stock_tags", [])
                display_stocks = ai_stocks if ai_stocks else stock_tags

                # 涨停交叉比对
                zt_matched = []
                for s in display_stocks or []:
                    code = s.get("code", "") if isinstance(s, dict) else ""
                    if code and code in zt_codes:
                        zt_matched.append(
                            s.get("name", code) if isinstance(s, dict) else code
                        )

                zt_flag = " 🔥涨停关联" if zt_matched else ""
                sentiment = item.get("ai_sentiment", "")
                sentiment_flag = f" [{sentiment}]" if sentiment else ""
                line = f"- [{level}]{sentiment_flag}{zt_flag} {title}"
                if snippet and snippet != title:
                    line += f" | {snippet}"

                # 关联股票展示（涨停股前置，最多 3 只）
                if display_stocks:
                    tags_parts = []
                    for s in (display_stocks or [])[:3]:
                        if isinstance(s, dict):
                            name = s.get("name", "")
                            code = s.get("code", "")
                            relevance = s.get("relevance", "")
                            prefix = "🔥" if code in zt_codes else ""
                            tag_text = f"{prefix}{name}({code})"
                            if relevance:
                                tag_text += f"[{relevance}]"
                            tags_parts.append(tag_text)
                    if tags_parts:
                        line += f"\n  关联：{'、'.join(tags_parts)}"
                elif zt_matched:
                    line += f"\n  涨停关联：{'、'.join(zt_matched)}"

                # AI 对 A 股的具体影响
                ai_impact = item.get("ai_impact", "")
                if ai_impact:
                    line += f"\n  影响：{ai_impact}"

                text += line + "\n"

        return text


class AIAnalyzer:
    """AI 分析引擎 — 委托给 system.ai。"""

    def __init__(self):
        self.model = ""  # 空=用默认模型

    def _call_ai(
        self,
        prompt: str,
        system_prompt: str = "你是一个专业的 A 股量化分析师。请务必使用阿拉伯数字格式输出所有数值，不要转换为中文数字。例如：85%而不是百分之八十五，2.5万亿而不是二点五万亿，2026-04-29而不是二零二六年四月二十九日。",
        enable_search: bool = False,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """调用 AI 模型。根据 provider 自动选择 API 格式。"""
        api_key = self.api_key
        endpoint = self.endpoint

        if not api_key:
            return "AI API Key 未配置，无法生成分析"

        try:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            }
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens

            # DashScope 独有参数
            if self._provider == "dashscope" and enable_search:
                payload["enable_search"] = True

            self.logger.info(
                f"开始调用 AI API（模型：{self.model}，Provider：{self._provider}，超时：600 秒）..."
            )
            response = self._api_post_with_retry(endpoint, payload, headers)

            self.logger.info(f"AI API 响应成功（状态码：{response.status_code}）")
            result = response.json()

            # 统一解析：choices[0].message.content（DeepSeek/DashScope 均兼容）
            content = ""
            if "choices" in result and result["choices"]:
                content = result["choices"][0].get("message", {}).get("content", "")
            elif "output" in result:
                content = result["output"].get("text", "")

            # 过滤 CoT 思考过程
            content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)

            if not content:
                self.logger.warning(f"AI 返回空内容，完整响应：{result}")
            else:
                self.logger.info(f"AI 分析完成（返回 {len(content)} 字）")

            return content

        except Exception as e:
            self.logger.error(f"AI 调用失败：{e}")
            raise RuntimeError(f"AI API 调用失败: {e}") from e

    def _call_ai_with_tools(
        self,
        messages: List[Dict],
        max_tokens: Optional[int] = None,
        tools: List[Dict] = None,
        tool_choice: str = "auto",
        timeout: int = 180,
    ) -> Dict:
        """
        调用 AI（支持工具调用）

        Args:
            messages: 对话历史
            max_tokens: 最大输出 token 数（默认 2000）
            tools: 自定义工具列表，默认使用 TOOLS_DEFINITION
            tool_choice: 工具选择策略，'auto'/'required'/'none' 或指定工具
            timeout: 读超时秒数，工具调用轮默认 180s，最终报告轮应给更长

        Returns:
            {
                'content': str,  # AI 回复内容
                'tool_calls': list  # 工具调用列表（如果有）
            }
        """
        try:
            from system.ai.stock_tools import TOOLS_DEFINITION

            api_key = self.api_key
            endpoint = self.endpoint

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            _tools = tools if tools is not None else TOOLS_DEFINITION

            payload = {
                "model": self.model,
                "messages": messages,
                "tools": _tools,
                "tool_choice": tool_choice,
            }
            # parallel_tool_calls 仅 DeepSeek/OpenAI 兼容格式支持
            if self._provider != "dashscope":
                payload["parallel_tool_calls"] = True
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens

            self.logger.info(
                f"调用 AI（支持工具，消息数：{len(messages)}，tool_choice={tool_choice}，超时：{timeout}s）..."
            )
            response = self._api_post_with_retry(
                endpoint, payload, headers, timeout=timeout, connect_timeout=15
            )

            result = response.json()

            # 解析响应
            message = result["choices"][0]["message"]
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

            if content:
                self.logger.info(f"AI 返回内容（{len(content)}字）")

            return {"content": content, "tool_calls": tool_calls}

        except Exception as e:
            self.logger.error(f"AI 调用失败：{e}")
            raise RuntimeError(f"AI API 调用失败: {e}") from e
