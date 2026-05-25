# -*- coding: utf-8 -*-
"""
复盘 AI 分析器 v3.0（刺客风格）

职责：查询原始数据 → 格式化 Prompt → 调用 AI → 返回复盘报告
不做数据采集、不做消息推送

核心改变：程序采集 + 清洗数据，AI 做判断 + 推演。
不给 AI 喂加工过的「得分」，只喂原始数据 + 环比对比，让 AI 自己定性。
"""

import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import os
import re
from typing import Dict, Any, Optional, List

import requests

from system.config.settings import DATABASE_PATH, LOGS_DIR
from system.config.prompts.review import REVIEW_REPORT_PROMPT
from analysis.review.formatter import (
    format_chain_ladder, format_lhb_full,
    format_fund_flow, format_candidates, format_strong_stocks,
    format_yzt_performance, format_yesterday_watchlist,
    format_hotspot, format_limit_quality, format_capital_concentration,
    format_three_day_trend, format_index_data,
    format_macro_overview, format_risk_flags, format_announcements,
    format_historical_calibration, format_broken_boards,
    format_first_boards, format_trend_stocks, format_zhongjun,
    calc_position_cap, fmt_change, safe_float,
)
from system.utils.logger import get_core_logger, get_system_logger
from system.utils.function_calling import FunctionCallingEngine
from data.readers.sector_reader import SectorReader
from data.readers.limit_pool_reader import LimitPoolReader
from data.readers.stock_reader import StockReader


class ReviewAnalyzer:
    """复盘 AI 分析器（刺客风格）"""

    def __init__(self):
        self.logger = get_core_logger('review_analyzer')
        self.ai = AIAnalyzer()
        # 加载板块名称→编码映射（用于 formatter 输出 sector_code）
        self.sector_code_map = {}
        try:
            conn = sqlite3.connect(str(DATABASE_PATH))
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
            model: 覆盖默认模型（如 'deepseek-v3'、'qwen3.6-plus'）

        Returns:
            AI 生成的复盘报告文本
        """
        if model:
            self.ai.model = model
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        from system.config.trading_calendar import get_previous_trading_day
        yesterday = get_previous_trading_day(trade_date)
        day_before = get_previous_trading_day(yesterday)
        day_before_before = get_previous_trading_day(day_before)

        self.logger.info(f"开始复盘 AI 分析 v3.0（刺客风格）{trade_date}（D-3:{day_before_before}, D-2:{day_before}, D-1:{yesterday}）...")
        start_time = time.time()

        # CLS 复盘新闻已在采集阶段落盘（collect() 模块 13），分析阶段直接通过 FC 工具读取
        # 电报由 FC 工具 get_telegraph_news 直接从 DB 查询

        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.row_factory = sqlite3.Row

        try:
            # 读取昨日复盘报告（AI 自我校准）
            # 文件名格式 review_reports_{date}_{model}.txt，需 glob 匹配模型后缀
            yesterday_report = ""
            reports_dir = Path(__file__).parent.parent.parent / 'storage' / 'reports'
            yesterday_matches = sorted(reports_dir.glob(f'review_reports_{yesterday}_*.txt'))
            if yesterday_matches:
                yesterday_report = yesterday_matches[-1].read_text(encoding='utf-8')
                self.logger.info(f"✅ 已加载昨日复盘报告（{len(yesterday_report)}字）from {yesterday_matches[-1].name}")
            else:
                self.logger.info(f"昨日（{yesterday}）无复盘报告记录")

            # ===== 1. 市场全貌 =====
            self.logger.info("查询市场全貌...")
            cursor = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) as up_count,
                    SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) as down_count,
                    SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) as flat_count,
                    SUM(turnover)/100000000 as turnover,
                    SUM(CASE WHEN change_pct > 5 THEN 1 ELSE 0 END) as up_5pct,
                    SUM(CASE WHEN change_pct < -5 THEN 1 ELSE 0 END) as down_5pct
                FROM stock_basic WHERE trade_date = ?
            """, (trade_date,))
            market_row = cursor.fetchone()
            # stock_basic 数据可能为空（采集失败），用默认值兜底防止 AI 分析崩溃
            market = {
                'total': (market_row['total'] or 0) if market_row else 0,
                'up_count': (market_row['up_count'] or 0) if market_row else 0,
                'down_count': (market_row['down_count'] or 0) if market_row else 0,
                'flat_count': (market_row['flat_count'] or 0) if market_row else 0,
                'turnover': (market_row['turnover'] or 0) if market_row else 0,
                'up_5pct': (market_row['up_5pct'] or 0) if market_row else 0,
                'down_5pct': (market_row['down_5pct'] or 0) if market_row else 0,
            }
            up_ratio = market['up_count'] / market['total'] if market['total'] > 0 else 0

            # 昨日成交额（环比）
            cursor = conn.execute("""
                SELECT SUM(turnover)/100000000 as prev_turnover
                FROM stock_basic WHERE trade_date = ?
            """, (yesterday,))
            prev_row = cursor.fetchone()
            prev_turnover = prev_row['prev_turnover'] if prev_row else 0
            if prev_turnover and prev_turnover > 0 and market['turnover']:
                turnover_change = (market['turnover'] - prev_turnover) / prev_turnover * 100
            else:
                turnover_change = 0

            # 主要指数表现（含 3 日趋势 + MA5/MA10/MA20）
            index_codes = ['sh000001','sz399001','sz399006','sh000016','sh000300','sh000905','sh000852','sz399637']
            placeholders = ','.join('?' * len(index_codes))

            # 取近 25 个交易日收盘价用于计算 MA
            from system.config.trading_calendar import get_recent_trading_days
            recent_25 = get_recent_trading_days(trade_date, 25)
            dt_ph = ','.join('?' * len(recent_25))
            cursor = conn.execute(f"""
                SELECT index_code, trade_date, close_price
                FROM index_realtime_data
                WHERE trade_date IN ({dt_ph}) AND index_code IN ({placeholders})
                ORDER BY index_code, trade_date
            """, recent_25 + index_codes)
            close_history = {}
            for row in cursor.fetchall():
                code = row['index_code']
                if code not in close_history:
                    close_history[code] = []
                close_history[code].append(row['close_price'] or 0)

            def _calc_ma(closes: list, n: int):
                if len(closes) >= n:
                    return round(sum(closes[-n:]) / n, 2)
                return 0

            # 取 3 日完整数据
            cursor = conn.execute(f"""
                SELECT index_code, index_name, close_price, open_price, high_price, low_price,
                       change_percent, change_amount, turnover_amount/10000 as turnover_yi,
                       volume, prev_close, trade_date
                FROM index_realtime_data
                WHERE trade_date IN (?, ?, ?) AND index_code IN ({placeholders})
                ORDER BY index_code, trade_date
            """, [trade_date, yesterday, day_before] + index_codes)
            index_rows = [dict(row) for row in cursor.fetchall()]

            # 按指数分组，构建 3 日趋势
            index_data = []
            index_groups = {}
            for row in index_rows:
                code = row['index_code']
                if code not in index_groups:
                    index_groups[code] = {'code': code, 'name': row['index_name'], 'data': {}}
                index_groups[code]['data'][row['trade_date']] = {
                    'close': row['close_price'] or 0,
                    'open': row['open_price'] or 0,
                    'high': row['high_price'] or 0,
                    'low': row['low_price'] or 0,
                    'change': row['change_percent'] or 0,
                    'change_amount': row['change_amount'] or 0,
                    'turnover': row['turnover_yi'] or 0,
                    'volume': row['volume'] or 0,
                    'prev_close': row['prev_close'] or 0,
                }
            for code, g in index_groups.items():
                d0 = g['data'].get(trade_date, {})
                d1 = g['data'].get(yesterday, {})
                d2 = g['data'].get(day_before, {})
                closes = close_history.get(code, [])
                index_data.append({
                    'index_name': g['name'],
                    # 今日
                    'close': d0.get('close', 0),
                    'open': d0.get('open', 0),
                    'high': d0.get('high', 0),
                    'low': d0.get('low', 0),
                    'change_percent': d0.get('change', 0),
                    'change_amount': d0.get('change_amount', 0),
                    'turnover': d0.get('turnover', 0),
                    'volume': d0.get('volume', 0),
                    'prev_close': d0.get('prev_close', 0),
                    # MA
                    'ma5': _calc_ma(closes, 5),
                    'ma10': _calc_ma(closes, 10),
                    'ma20': _calc_ma(closes, 20),
                    # 昨日
                    'd1_close': d1.get('close', 0),
                    'd1_open': d1.get('open', 0),
                    'd1_high': d1.get('high', 0),
                    'd1_low': d1.get('low', 0),
                    'd1_change': d1.get('change', 0),
                    'd1_change_amount': d1.get('change_amount', 0),
                    'd1_turnover': d1.get('turnover', 0),
                    'd1_volume': d1.get('volume', 0),
                    # 前天
                    'd2_close': d2.get('close', 0),
                    'd2_open': d2.get('open', 0),
                    'd2_high': d2.get('high', 0),
                    'd2_low': d2.get('low', 0),
                    'd2_change': d2.get('change', 0),
                    'd2_change_amount': d2.get('change_amount', 0),
                    'd2_turnover': d2.get('turnover', 0),
                    'd2_volume': d2.get('volume', 0),
                })

            # 隔夜宏观
            cursor = conn.execute("""
                SELECT * FROM macro_daily ORDER BY trade_date DESC LIMIT 1
            """)
            macro_row = cursor.fetchone()
            macro_data = dict(macro_row) if macro_row else {}

            # ===== 2. 涨跌停 & 环比 =====
            self.logger.info("查询涨跌停数据...")
            cursor = conn.execute("""
                SELECT pool_type, COUNT(*) as cnt FROM limit_pool
                WHERE trade_date = ? GROUP BY pool_type
            """, (trade_date,))
            limit_today = {row['pool_type']: row['cnt'] for row in cursor.fetchall()}

            cursor = conn.execute("""
                SELECT pool_type, COUNT(*) as cnt FROM limit_pool
                WHERE trade_date = ? GROUP BY pool_type
            """, (yesterday,))
            limit_yest = {row['pool_type']: row['cnt'] for row in cursor.fetchall()}

            limit_up = limit_today.get('涨停', 0)
            limit_down = limit_today.get('跌停', 0)
            broken = limit_today.get('炸板', 0)
            touched = limit_up + broken
            seal_rate = (limit_up / touched * 100) if touched > 0 else 0

            prev_limit_up = limit_yest.get('涨停', 0)
            prev_limit_down = limit_yest.get('跌停', 0)
            prev_broken = limit_yest.get('炸板', 0)
            prev_touched = prev_limit_up + prev_broken
            prev_seal_rate = (prev_limit_up / prev_touched * 100) if prev_touched > 0 else 0

            limit_up_change = limit_up - prev_limit_up
            limit_down_change = limit_down - prev_limit_down
            seal_rate_change = seal_rate - prev_seal_rate

            # 涨停代码集合（供电报交叉比对）
            cursor = conn.execute("""
                SELECT stock_code FROM limit_pool
                WHERE trade_date = ? AND pool_type = '涨停'
            """, (trade_date,))
            zt_codes = {row['stock_code'] for row in cursor.fetchall()}

            # 涨停质量细分（一字板/换手板/回封板）
            cursor = conn.execute("""
                SELECT first_seal_time, last_seal_time, open_count
                FROM limit_pool WHERE trade_date = ? AND pool_type = '涨停'
            """, (trade_date,))
            limit_quality = {'一字板': 0, '换手板': 0, '回封板': 0}
            for row in cursor.fetchall():
                open_cnt = row['open_count'] or 0
                first_seal = row['first_seal_time'] or ''
                if open_cnt >= 1:
                    limit_quality['回封板'] += 1
                elif first_seal and first_seal <= '09:35':
                    limit_quality['一字板'] += 1
                else:
                    limit_quality['换手板'] += 1

            # ===== 3. 连板梯队 =====
            self.logger.info("查询连板梯队...")
            chain, chain_count, highest_board = LimitPoolReader.get_chain_ladder(conn, trade_date)

            # 补充概念板块
            chain_codes = [s['code'] for stocks in chain.values() for s in stocks]
            chain_concepts = SectorReader.enrich_concepts(conn, trade_date, chain_codes)
            for stocks in chain.values():
                for s in stocks:
                    concepts = chain_concepts.get(s['code'], [])
                    if concepts:
                        s['concepts'] = concepts

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
                    from_codes = {s['code'] for s in from_chain.get(board, [])}
                    to_codes = {s['code'] for s in to_chain.get(board + 1, [])}
                    promoted = from_codes & to_codes
                    from_count = len(from_codes)
                    if from_count > 0:
                        rates[board] = {
                            'from': board,
                            'to': board + 1,
                            'prev_count': from_count,
                            'promoted': len(promoted),
                            'rate': round(len(promoted) / from_count * 100, 1)
                        }
                return rates

            promotion_rates = [
                {'label': f'{day_before_before}→{day_before}',
                 'rates': _calc_promotion(d3_chain, d3_highest, d2_chain, d2_highest)},
                {'label': f'{day_before}→{yesterday}',
                 'rates': _calc_promotion(d2_chain, d2_highest, prev_chain, prev_highest_board)},
                {'label': f'{yesterday}→今日',
                 'rates': _calc_promotion(prev_chain, prev_highest_board, chain, highest_board)},
            ]

            # ===== 3.3. 炸板明细 =====
            self.logger.info("查询炸板明细...")
            broken_records = LimitPoolReader.get_broken_boards(conn, trade_date)

            # 补充概念板块
            broken_codes = [r['code'] for r in broken_records]
            broken_concepts = SectorReader.enrich_concepts(conn, trade_date, broken_codes)
            for r in broken_records:
                concepts = broken_concepts.get(r['code'], [])
                if concepts:
                    r['concepts'] = concepts

            # ===== 3.35. 首板苗子 =====
            self.logger.info("查询首板苗子...")
            first_board_records = LimitPoolReader.get_first_boards(conn, trade_date)

            # 补充概念板块
            first_codes = [r['code'] for r in first_board_records]
            first_concepts = SectorReader.enrich_concepts(conn, trade_date, first_codes)
            for r in first_board_records:
                concepts = first_concepts.get(r['code'], [])
                if concepts:
                    r['concepts'] = concepts

            # ===== 3.4. D-2 数据（构建 3 日趋势）=====
            self.logger.info(f"查询 D-2 ({day_before}) 数据...")

            # D-2 市场概览
            cursor = conn.execute("""
                SELECT
                    SUM(turnover)/100000000 as turnover,
                    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) as up_count,
                    COUNT(*) as total,
                    SUM(CASE WHEN change_pct > 5 THEN 1 ELSE 0 END) as up_5pct
                FROM stock_basic WHERE trade_date = ?
            """, (day_before,))
            d2_market = cursor.fetchone()
            d2_turnover = d2_market['turnover'] if d2_market else 0
            d2_up_ratio = d2_market['up_count'] / d2_market['total'] if d2_market and d2_market['total'] > 0 else 0
            d2_up_5pct = d2_market['up_5pct'] if d2_market else 0

            # D-2 涨跌停
            cursor = conn.execute("""
                SELECT pool_type, COUNT(*) as cnt FROM limit_pool
                WHERE trade_date = ? GROUP BY pool_type
            """, (day_before,))
            d2_limit = {row['pool_type']: row['cnt'] for row in cursor.fetchall()}
            d2_limit_up = d2_limit.get('涨停', 0)
            d2_broken = d2_limit.get('炸板', 0)
            d2_touched = d2_limit_up + d2_broken
            d2_seal_rate = (d2_limit_up / d2_touched * 100) if d2_touched > 0 else 0
            d2_limit_down = d2_limit.get('跌停', 0)

            # D-2 连板
            cursor = conn.execute("""
                SELECT COUNT(DISTINCT stock_code) as cnt,
                       MAX(consecutive_boards) as max_board
                FROM limit_pool WHERE trade_date = ? AND pool_type = '涨停'
                AND consecutive_boards >= 2
            """, (day_before,))
            d2_chain = cursor.fetchone()
            d2_chain_count = d2_chain['cnt'] if d2_chain else 0
            d2_highest_board = d2_chain['max_board'] if d2_chain else 0

            # D-1 涨跌比 & 涨幅>5%（提前查询，构建三日趋势时直接用）
            cursor = conn.execute("""
                SELECT
                    SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) as up_count,
                    COUNT(*) as total,
                    SUM(CASE WHEN change_pct > 5 THEN 1 ELSE 0 END) as up_5pct
                FROM stock_basic WHERE trade_date = ?
            """, (yesterday,))
            d1_row = cursor.fetchone()
            d1_up_ratio = d1_row['up_count'] / d1_row['total'] if d1_row and d1_row['total'] > 0 else 0
            d1_up_5pct = d1_row['up_5pct'] if d1_row else 0

            # 构建 3 日趋势
            three_day_trend = {
                'd2_date': day_before, 'd1_date': yesterday, 'd_date': trade_date,
                'turnover': [d2_turnover, prev_turnover, market['turnover']],
                'up_ratio': [d2_up_ratio, d1_up_ratio, up_ratio],
                'limit_up': [d2_limit_up, prev_limit_up, limit_up],
                'limit_down': [d2_limit_down, prev_limit_down, limit_down],
                'seal_rate': [d2_seal_rate, prev_seal_rate, seal_rate],
                'chain_count': [d2_chain_count, prev_chain_count, chain_count],
                'highest_board': [d2_highest_board, prev_highest_board, highest_board],
                'up_5pct': [d2_up_5pct, d1_up_5pct, market['up_5pct'] or 0],
                'broken': [d2_broken, prev_broken, broken],
            }

            # ===== 4. 行业板块排行 + 资金流 =====
            self.logger.info("查询板块排行与资金流...")
            sectors, fund_flow_map = SectorReader.get_industry_sectors(conn, trade_date)
            concept_sectors, concept_fund_map = SectorReader.get_concept_sectors(conn, trade_date)

            # ===== 5. 龙虎榜（全量 + 席位明细）=====
            self.logger.info("查询龙虎榜...")
            # 过滤ST + 科创板，按净流入占比（净买入/总成交额）降序
            cursor = conn.execute("""
                SELECT stock_code, stock_name, close_price, change_percent,
                       net_inflow/10000 as net_wan, turnover_amount,
                       buy_amount/10000 as buy_wan, sell_amount/10000 as sell_wan,
                       turnover_rate, reason,
                       CASE WHEN turnover_amount > 0 THEN net_inflow / turnover_amount ELSE 0 END as net_ratio
                FROM lhb_stocks WHERE trade_date = ?
                  AND stock_name NOT LIKE '%ST%'
                  AND stock_code NOT LIKE '688%'
                ORDER BY net_ratio DESC
            """, (trade_date,))
            lhb_rows = [dict(row) for row in cursor.fetchall()]

            # 查询席位明细
            cursor = conn.execute("""
                SELECT ls.stock_code, ls.seat_name, ls.buy_amount, ls.sell_amount,
                       ls.net_amount, ls.is_institution, ls.is_hot_money,
                       ls.seat_type
                FROM lhb_seats ls
                JOIN lhb_stocks s ON ls.stock_code = s.stock_code AND ls.trade_date = s.trade_date
                WHERE ls.trade_date = ?
                ORDER BY ls.stock_code, ls.net_amount DESC
            """, (trade_date,))
            lhb_seats_by_stock = {}
            for row in cursor.fetchall():
                code = row['stock_code']
                if code not in lhb_seats_by_stock:
                    lhb_seats_by_stock[code] = {'buy': [], 'sell': []}
                seat = {
                    'name': row['seat_name'], 'amount': row['net_amount'] or 0,
                    'buy': row['buy_amount'] or 0, 'sell': row['sell_amount'] or 0,
                    'is_inst': row['is_institution'], 'is_hm': row['is_hot_money'],
                    'type': row['seat_type'] or '',
                }
                if (row['buy_amount'] or 0) > (row['sell_amount'] or 0):
                    lhb_seats_by_stock[code]['buy'].append(seat)
                else:
                    lhb_seats_by_stock[code]['sell'].append(seat)

            # 查连板数
            lhb_codes = [r['stock_code'] for r in lhb_rows]
            boards_map = {}
            if lhb_codes:
                ph = ','.join('?' * len(lhb_codes))
                cursor = conn.execute(f"""
                    SELECT stock_code, consecutive_boards FROM limit_pool
                    WHERE trade_date = ? AND pool_type = '涨停'
                      AND stock_code IN ({ph})
                """, [trade_date] + lhb_codes)
                for r in cursor.fetchall():
                    boards_map[r['stock_code']] = r['consecutive_boards'] or 0

            # 查近5天上榜频次
            freq_map = {}
            if lhb_codes:
                ph = ','.join('?' * len(lhb_codes))
                cursor = conn.execute(f"""
                    SELECT stock_code, COUNT(*) as freq
                    FROM lhb_stocks
                    WHERE stock_code IN ({ph})
                      AND trade_date >= date(?, '-5 days')
                    GROUP BY stock_code
                """, lhb_codes + [trade_date])
                for r in cursor.fetchall():
                    freq_map[r['stock_code']] = r['freq']

            # 组装：每只股票附带席位 + 连板 + 频次
            lhb_data = []
            for row in lhb_rows:
                code = row['stock_code']
                seats = lhb_seats_by_stock.get(code, {'buy': [], 'sell': []})
                sell_wan = row['sell_wan'] or 0
                lhb_data.append({
                    'code': code, 'name': row['stock_name'],
                    'change': row['change_percent'] or 0,
                    'net_wan': row['net_wan'] or 0,
                    'net_ratio': row['net_ratio'] or 0,
                    'turnover': row['turnover_amount'] or 0,
                    'turnover_rate': row['turnover_rate'] or 0,
                    'buy_wan': row['buy_wan'] or 0,
                    'sell_wan': -abs(sell_wan) if sell_wan else 0,
                    'reason': row['reason'] or '',
                    'buy_seats': seats['buy'][:5],
                    'sell_seats': seats['sell'][:5],
                    'boards': boards_map.get(code, 0),
                    'lhb_freq': freq_map.get(code, 1),
                })

            # ===== 6. 今日异动股（分层抽样：主板20 + 创业板20）=====
            self.logger.info("查询今日异动股...")
            candidates = StockReader.get_candidates(conn, trade_date)

            # 补充概念板块
            candidate_codes = [c['code'] for c in candidates]
            candidate_concepts = SectorReader.enrich_concepts(conn, trade_date, candidate_codes)
            for c in candidates:
                concepts = candidate_concepts.get(c['code'], [])
                if concepts:
                    c['concepts'] = concepts

            # ===== 6.5. 近期强势股（60日新高+多次涨停，凑够30只）=====
            self.logger.info("查询近期强势股...")
            strong_stocks = StockReader.get_strong_stocks(conn, trade_date, sectors)

            # 补充概念板块
            strong_codes = [s['stock_code'] for s in strong_stocks]
            strong_concepts = SectorReader.enrich_concepts(conn, trade_date, strong_codes)
            for s in strong_stocks:
                concepts = strong_concepts.get(s['stock_code'], [])
                if concepts:
                    s['concepts'] = concepts

            # ===== 6.7. 趋势股（双模式：5日线强趋势 + 20日线稳健趋势）=====
            self.logger.info("查询趋势股（双模式）...")
            trend_data = StockReader.get_trend_stocks(conn, trade_date)
            strong_stocks = trend_data.get('strong', [])
            normal_stocks = trend_data.get('normal', [])
            self.logger.info(f"趋势股：强趋势{len(strong_stocks)}只 + 稳健趋势{len(normal_stocks)}只")

            # 补充概念板块（两类合并查询）
            all_trend_codes = [s['stock_code'] for s in strong_stocks + normal_stocks]
            trend_concepts = SectorReader.enrich_concepts(conn, trade_date, all_trend_codes)
            for s in strong_stocks + normal_stocks:
                concepts = trend_concepts.get(s['stock_code'], [])
                if concepts:
                    s['concepts'] = concepts

            # ===== 7. 热点板块（综合打分取 top10）=====
            self.logger.info("查询热点板块（综合打分）...")
            top_industries = SectorReader.get_hot_sectors(conn, trade_date, 'sector_industry', top_n=10,
                                                            prev_date=yesterday, prev_prev_date=day_before)
            top_concepts = SectorReader.get_hot_sectors(conn, trade_date, 'sector_concept', top_n=10,
                                                         prev_date=yesterday, prev_prev_date=day_before)
            if top_industries:
                self.logger.info(f"行业热点TOP10：{', '.join(s['name'] for s in top_industries[:5])}...")
            else:
                self.logger.info("无行业热点数据")
            if top_concepts:
                self.logger.info(f"概念热点TOP10：{', '.join(s['name'] for s in top_concepts[:5])}...")
            else:
                self.logger.info("无概念热点数据")

            # ===== 7.1. 板块中军筛选（4维打分）=====
            self.logger.info("筛选中军候选...")
            industry_codes = [s['sector_code'] for s in top_industries] if top_industries else []
            concept_codes = [s['sector_code'] for s in top_concepts] if top_concepts else []
            zhongjun_industry = SectorReader.get_sector_zhongjun(
                conn, trade_date, industry_codes, 'sector_industry', top_n=5
            ) if industry_codes else {}
            zhongjun_concept = SectorReader.get_sector_zhongjun(
                conn, trade_date, concept_codes, 'sector_concept', top_n=5
            ) if concept_codes else {}
            all_zhongjun = {**zhongjun_industry, **zhongjun_concept}
            self.logger.info(f"中军候选筛选完成：{sum(len(v) for v in all_zhongjun.values())}只")

            # ===== 7.5. 保存热点历史 + 计算近N日上榜次数 ====
            HOT_SCORE_THRESHOLD = 70  # 热度分阈值，≥70 分算"上榜"
            LOOKBACK_DAYS = 5  # 回溯最近5个交易日
            self.logger.info("保存热点历史并计算近N日上榜次数...")

            # 保存今日 TOP10 到历史表
            for s in top_industries:
                conn.execute("""
                    INSERT OR REPLACE INTO sector_hot_history
                    (trade_date, sector_type, rank, sector_code, sector_name, hot_score)
                    VALUES (?, 'industry', ?, ?, ?, ?)
                """, (trade_date, s.get('rank', 0), s['sector_code'], s['name'], s['hot_score']))
            for s in top_concepts:
                conn.execute("""
                    INSERT OR REPLACE INTO sector_hot_history
                    (trade_date, sector_type, rank, sector_code, sector_name, hot_score)
                    VALUES (?, 'concept', ?, ?, ?, ?)
                """, (trade_date, s.get('rank', 0), s['sector_code'], s['name'], s['hot_score']))
            conn.commit()

            # 获取最近 LOOKBACK_DAYS 个交易日
            from system.config.trading_calendar import get_recent_trading_days
            recent_days = get_recent_trading_days(trade_date, LOOKBACK_DAYS)

            # 一次 SQL 查出所有板块在近期交易日的上榜次数
            all_codes = [s['sector_code'] for s in top_industries] + [s['sector_code'] for s in top_concepts]
            if all_codes and recent_days:
                ph_codes = ','.join('?' * len(all_codes))
                ph_days = ','.join('?' * len(recent_days))
                history_rows = conn.execute(f"""
                    SELECT sector_code, COUNT(*) as appear_count
                    FROM sector_hot_history
                    WHERE sector_code IN ({ph_codes})
                      AND trade_date IN ({ph_days})
                      AND hot_score >= ?
                    GROUP BY sector_code
                """, all_codes + recent_days + [HOT_SCORE_THRESHOLD]).fetchall()

                count_map = {row['sector_code']: row['appear_count'] for row in history_rows}

                for s in top_industries:
                    s['recent_appear'] = count_map.get(s['sector_code'], 0)
                for s in top_concepts:
                    s['recent_appear'] = count_map.get(s['sector_code'], 0)
            else:
                for s in top_industries:
                    s['recent_appear'] = 0
                for s in top_concepts:
                    s['recent_appear'] = 0

            # ===== 8. 昨日涨停今日表现 =====
            self.logger.info("查询昨日涨停今日表现...")
            yzt_records = LimitPoolReader.get_yzt_performance(conn, trade_date)

            # 补充概念板块
            yzt_codes = [r['code'] for r in yzt_records]
            yzt_concepts = SectorReader.enrich_concepts(conn, trade_date, yzt_codes)
            for r in yzt_records:
                concepts = yzt_concepts.get(r['code'], [])
                if concepts:
                    r['concepts'] = concepts

            # ===== 9. 昨日 AI 推荐标的今日验证 =====
            self.logger.info("查询昨日 AI 推荐标的今日表现...")
            cursor = conn.execute("""
                SELECT t.stock_code, t.stock_name, t.plate, t.star_rating,
                       s.change_pct, s.turnover_rate, s.volume_ratio,
                       s.amplitude, s.total_market_cap/100000000 as mcap,
                       s.circ_market_cap/100000000 as circ_mcap,
                       s.main_force_net/10000 as mf_wan,
                       s.super_large_net/10000 as sl_wan,
                       s.large_net/10000 as lg_wan,
                       s.medium_net/10000 as md_wan,
                       s.small_net/10000 as sm_wan,
                       s.main_force_ratio,
                       COALESCE(lp.pool_type, '') as limit_type
                FROM stock_tracker t
                LEFT JOIN stock_basic s ON t.stock_code = s.stock_code
                    AND s.trade_date = ?
                LEFT JOIN limit_pool lp ON t.stock_code = lp.stock_code
                    AND lp.trade_date = ? AND lp.pool_type = '涨停'
                WHERE t.push_date = ? AND t.source = '复盘'
                ORDER BY t.star_rating DESC, COALESCE(s.change_pct, -999) DESC
            """, (trade_date, trade_date, yesterday))
            yesterday_watch = []
            for row in cursor.fetchall():
                chg = row['change_pct'] if row['change_pct'] is not None else None
                star = row['star_rating'] or 0
                star_label = {5: 'P0', 4: 'P1', 3: 'P2'}.get(star, 'P3')
                yesterday_watch.append({
                    'code': row['stock_code'],
                    'name': row['stock_name'],
                    'change': chg,
                    'turnover': row['turnover_rate'] or 0,
                    'vol_ratio': row['volume_ratio'] or 0,
                    'amplitude': row['amplitude'] or 0,
                    'mcap': row['mcap'] or 0,
                    'circ_mcap': row['circ_mcap'] or 0,
                    'mf_wan': row['mf_wan'] or 0,
                    'sl_wan': row['sl_wan'] or 0,
                    'lg_wan': row['lg_wan'] or 0,
                    'md_wan': row['md_wan'] or 0,
                    'sm_wan': row['sm_wan'] or 0,
                    'mf_ratio': row['main_force_ratio'] or 0,
                    'plate': row['plate'] or '',
                    'star_label': star_label,
                    'is_limit_up': row['limit_type'] == '涨停',
                })

            # ===== 10. 资金集中度 =====
            self.logger.info("计算资金集中度...")
            # 合并行业+概念板块主力净额，计算 TOP3 占比
            all_sector_flows = []
            for s in sectors:
                ff = fund_flow_map.get(s['name'], {})
                mf = ff.get('main_force_net', 0) or 0
                if mf > 0:
                    all_sector_flows.append(mf)
            for s in concept_sectors:
                ff = concept_fund_map.get(s['name'], {})
                mf = ff.get('main_force_net', 0) or 0
                if mf > 0:
                    all_sector_flows.append(mf)
            all_sector_flows.sort(reverse=True)
            total_inflow = sum(all_sector_flows) / 100000000 if all_sector_flows else 0
            top3_inflow = sum(all_sector_flows[:3]) / 100000000 if all_sector_flows else 0
            top3_pct = (top3_inflow / total_inflow * 100) if total_inflow > 0 else 0
            capital_concentration = {
                'top3_pct': top3_pct,
                'total_inflow': total_inflow,
            }

            # 昨日涨停平均溢价率
            yzt_avg_change = (
                sum(r.get('change', 0) for r in yzt_records) / len(yzt_records)
                if yzt_records else 0
            )

            # ===== 11. 股东增减持 =====
            self.logger.info("查询股东增减持...")
            cursor = conn.execute("""
                SELECT stock_code, stock_name, holder_name, change_type,
                       change_direction, change_rate, change_num_symbol
                FROM share_holder_change WHERE trade_date = ?
                ORDER BY ABS(change_rate) DESC
            """, (trade_date,))
            share_holder_changes = [dict(row) for row in cursor.fetchall()]

            # ===== 12. 重点监控 =====
            self.logger.info("查询重点监控...")
            cursor = conn.execute("""
                SELECT stock_code, stock_name, monitor_type, trigger_rule, status
                FROM stock_monitor WHERE trade_date = ?
            """, (trade_date,))
            stock_monitors = [dict(row) for row in cursor.fetchall()]

            # ===== 13. 重要公告过滤 =====
            self.logger.info("查询重要公告...")
            ANNOUNCEMENT_WHITELIST = [
                '业绩预告', '一季度报告全文', '年度报告全文', '年度报告摘要',
                '半年度报告', '三季度报告', '分配预案',
                '重组进展公告', '收购出售资产/股权', '重大合同',
                '实施退市风险警示', '其它风险提示公告', '停复牌公告',
                '股权激励进展公告', '诉讼仲裁', '月度经营情况',
            ]
            placeholders = ','.join('?' * len(ANNOUNCEMENT_WHITELIST))
            cursor = conn.execute(f"""
                SELECT stock_code, stock_name, announcement_title, announcement_type,
                       importance_score, announcement_url
                FROM future_announcements
                WHERE announcement_type IN ({placeholders})
                  AND importance_score >= 8
                  AND trade_date >= ?
                ORDER BY importance_score DESC, trade_date DESC
            """, ANNOUNCEMENT_WHITELIST + [trade_date])
            important_announcements = [dict(row) for row in cursor.fetchall()]

            # ===== 14. 仓位硬顶 =====
            position_cap = calc_position_cap(
                limit_up=limit_up, broken=broken,
                highest_board=highest_board, seal_rate=seal_rate,
                up_ratio=up_ratio, yzt_avg_change=yzt_avg_change,
            )
            self.logger.info(f"仓位硬顶：{position_cap}%（评分依据：涨停{limit_up}, 封板率{seal_rate:.1f}%, 最高{highest_board}板, 涨跌比{up_ratio:.2f}, 溢价率{yzt_avg_change:+.2f}%）")

            # ===== 14.5. AI 推荐历史校准统计 =====
            calibration_stats = self._get_calibration_stats(conn, yesterday)
            calibration_stats_text = format_historical_calibration(calibration_stats)

            # ===== 15. 格式化所有数据（按 Prompt 模板新顺序）=====
            self.logger.info("格式化 Prompt 数据...")
            prompt_data = {
                # 一、市场全貌
                'trade_date': trade_date,
                'turnover': round(market['turnover'] or 0, 0),
                'prev_turnover': round(prev_turnover or 0, 0),
                'turnover_change': f"{turnover_change:+.1f}%",
                'up_count': market['up_count'] or 0,
                'down_count': market['down_count'] or 0,
                'flat_count': market['flat_count'] or 0,
                'limit_up_count': limit_up,
                'limit_down_count': limit_down,
                'broken_count': broken,
                'seal_rate': round(seal_rate, 1),
                'limit_up_change': fmt_change(limit_up_change),
                'limit_down_change': fmt_change(limit_down_change),
                'seal_rate_change': f"{seal_rate_change:+.1f}%",
                'limit_quality_text': format_limit_quality(limit_quality),
                'up_5pct_count': market['up_5pct'] or 0,
                'down_5pct_count': market['down_5pct'] or 0,
                'chain_count': chain_count,
                'chain_count_change': fmt_change(chain_count_change),
                'highest_board': highest_board,
                'prev_highest_board': prev_highest_board,
                'd2_date': three_day_trend['d2_date'],
                'd1_date': three_day_trend['d1_date'],
                'three_day_trend': format_three_day_trend(three_day_trend),
                'index_data_text': format_index_data(index_data),
                # 二、隔夜外围
                'macro_text': format_macro_overview(macro_data),
                # 三、财联社复盘新闻（必修 FC 工具）
                'news_data_text': '**你必须立即调用 get_cls_digest_news 工具获取财联社复盘新闻。这是必修工具，包含 AI 编辑撰写的高质量盘后总结，不可跳过。**',
                # 四、财联社盘中电报（FC 按股查询，直接从 DB 读）
                'telegraph_text': '盘中电报已采集。如需查询某只股票今天是否有相关电报新闻，调用 get_telegraph_news(stock_code) 工具，直接从数据库查询该股的盘中快讯。',
                # 五、连板梯队（含首板苗子+炸板明细）
                'chain_ladder_text': format_chain_ladder(chain, promotion_rates, sector_code_map=self.sector_code_map),
                'first_boards_text': format_first_boards(first_board_records, sector_code_map=self.sector_code_map),
                'broken_boards_text': format_broken_boards(
                    broken_records,
                    broken_trend={'d2': d2_broken, 'd1': prev_broken, 'd': broken},
                    sector_code_map=self.sector_code_map
                ),
                # 六、热点板块数据
                'hotspot_text': format_hotspot(top_industries, top_concepts, sector_code_map=self.sector_code_map),
                # 六-B、板块中军候选
                'zhongjun_text': format_zhongjun(all_zhongjun, sector_code_map=self.sector_code_map),
                # 七、板块资金暗流
                'fund_flow_text': (
                    "【行业】\n" + format_fund_flow(sectors, fund_flow_map) +
                    "\n【概念】\n" + format_fund_flow(concept_sectors, concept_fund_map)
                ),
                'capital_concentration_text': format_capital_concentration(capital_concentration),
                # 八、今日异动股
                'candidate_count': len(candidates),
                'candidate_text': format_candidates(candidates, sector_code_map=self.sector_code_map),
                # 九、龙虎榜全量
                'lhb_text': format_lhb_full(lhb_data),
                # 九-B、趋势股（双模式）
                'trend_count': len(strong_stocks) + len(normal_stocks),
                'trend_stocks_text': format_trend_stocks(trend_data, sector_code_map=self.sector_code_map),
                # 十、近期强势股
                'strong_count': len(strong_stocks),
                'strong_stocks_text': format_strong_stocks(strong_stocks, sector_code_map=self.sector_code_map),
                # 十一、昨日涨停股今日表现
                'yzt_count': len(yzt_records),
                'yzt_text': format_yzt_performance(yzt_records, sector_code_map=self.sector_code_map),
                # 十二、昨日 AI 推荐标的今日验证
                'yesterday_watchlist_text': '（调用 get_yesterday_picks_performance 工具查看昨日推荐标的今日表现，对比昨日预判和今日实际）',
                # 十三、风险地雷
                'share_holder_text': format_risk_flags(share_holder_changes, candidates),
                'monitor_text': format_risk_flags(stock_monitors, candidates, label='监控'),
                # 十四、今日重要公告
                'announcements_text': format_announcements(important_announcements),
                # 十五、AI 推荐历史校准
                'calibration_stats_text': '（调用 get_historical_calibration 工具查看近5日推荐胜率和板块表现统计，用于自我校准）',
                # 十六、昨日复盘回顾
                'yesterday_review_text': '（调用 get_yesterday_review 工具查看昨日复盘报告全文，对比昨日预判和今日实际盘面）',
                # 仓位
                'position_cap': position_cap,
            }

            prompt = REVIEW_REPORT_PROMPT.format(**prompt_data)

            # 保存 Prompt 到日志目录
            try:
                prompt_dir = LOGS_DIR / trade_date / 'prompts'
                prompt_dir.mkdir(parents=True, exist_ok=True)
                prompt_path = prompt_dir / 'review_prompt.txt'
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                with open(prompt_path, 'w', encoding='utf-8') as f:
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
                "你看到的是今日收盘后的全量市场数据。你的任务是："
                "1. 做情绪周期判断（启动/发酵/高潮/分歧/退潮/冰点），必须引用具体数据变化作为依据"
                '2. 给出具体的交易策略（什么条件下买/不买），不要笼统的"关注"'
                '3. 风险提示要具体——写清楚什么情况下会崩，不要写"注意风险"这种废话'
                "4. 拆解标的时，要说出它在市场中的地位和信号意义"
                "5. 用中文输出，所有数值用阿拉伯数字"
                "6. 报告末尾必须包含 <<<STOCKS>>> 股票池 JSON（包含第六节所有游资标的 + 第七节所有 5 只趋势票），这是强制性要求，忘记输出 STOCKS 等于报告不合格"
                "7. 数据章节做精选：龙虎榜只展示摘要，其余明细可调用 get_lhb_seats/get_hotspot_stocks/get_yesterday_limit_ups/get_unusual_stocks/get_stock_info 等工具按需获取"
                "8. 工具调用策略：调用 get_telegraph_news / get_regulatory_risks / get_lhb_seats 核查标的后，有监管风险的直接剔除不展示，有利好消息的才在个股下方标注"
                "9. 报告中不要出现任何工具名称（如 get_cls_digest_news、get_telegraph_news 等），不要写「数据来源：XX工具返回」，直接用分析结论"
                "10. 第七节「趋势交易者精选」角色切换为趋势交易者，从趋势股和板块中军候选数据中选 5 只趋势票。优先选蓄力期/主升初期的票（均线刚多头排列、还没大幅拉升），按趋势思维分析买点和止损。波段交易思维，持仓周期以交易日计"
            )

            models_to_run = [
                ('qwen3.6-plus', '千问'),
            ]

            reports = {}  # {model_name: report_text}

            for model_name, model_label in models_to_run:
                self.ai.model = model_name
                self.logger.info(f"调用 {model_label} AI 生成复盘报告（FC 多轮对话，Prompt {len(prompt)}字）...")
                try:
                    report_text = self._run_fc_review(prompt, trade_date, system_prompt)
                except Exception as e:
                    self.logger.error(f"❌ {model_label} AI 调用异常: {e}")
                    reports[model_name] = None
                    continue
                reports[model_name] = report_text
                self.logger.info(f"✅ {model_label} 报告生成完成（{len(report_text)}字）")

            # 保存报告到文件（供下次复盘自我校准）
            try:
                reports_dir = Path(__file__).parent.parent.parent / 'storage' / 'reports'
                reports_dir.mkdir(parents=True, exist_ok=True)

                for model_name, report_text in reports.items():
                    if report_text is None:
                        continue
                    suffix = f"_{model_name}"
                    report_path = reports_dir / f'review_reports_{trade_date}{suffix}.txt'
                    report_path.write_text(report_text, encoding='utf-8')
                    self.logger.info(f"✅ {model_name} 复盘报告已保存到 {report_path}")
            except Exception as e:
                self.logger.warning(f"保存复盘报告失败（不影响主流程）：{e}")

            report = reports.get('qwen3.6-plus')
            if not report:
                report = next((v for v in reports.values() if v), None)
            if not report:
                raise RuntimeError("所有模型均调用失败，无法生成复盘报告")

            elapsed = time.time() - start_time
            self.logger.info(f"✅ 复盘 AI 分析完成，耗时 {elapsed:.1f}秒")

            # 解析股票池
            stock_pool = self._extract_stock_pool(report)
            cleaned_report = self._remove_stock_pool(report)

            return cleaned_report, stock_pool

        except Exception as e:
            self.logger.error(f"❌ 复盘 AI 分析失败：{e}", exc_info=True)
            return f"AI 分析失败：{e}", []
        finally:
            conn.close()

    def _extract_stock_pool(self, report_text: str) -> list:
        """从复盘报告中提取股票池 JSON"""
        import re
        import json as _json
        match = re.search(r'<<<STOCKS>>>(.*?)<<<END>>>', report_text, re.DOTALL)
        if not match:
            self.logger.warning("⚠️ 未找到股票池标记 <<<STOCKS>>>")
            return []

        raw = match.group(1).strip()
        # 清理可能的 markdown 代码块包裹
        raw = re.sub(r'^```(?:json)?\s*\n?', '', raw)
        raw = re.sub(r'\n?```\s*$', '', raw)

        try:
            data = _json.loads(raw)
            stock_list = data.get('stocks', [])
        except _json.JSONDecodeError as e:
            self.logger.error(f"❌ 股票池 JSON 解析失败: {e}")
            self.logger.error(f"原始内容(前300字): {raw[:300]}")
            return []

        stocks = []
        for s in stock_list:
            name = (s.get('name') or '').strip()
            code = (s.get('code') or '').strip()
            role = (s.get('role') or '').strip()

            # 角色 → 优先级映射
            if '龙头' in role or '破局' in role:
                priority = 'P0'
            elif '中军' in role:
                priority = 'P1'
            else:
                priority = 'P2'

            stocks.append({
                '股票名称': name,
                '股票代码': code if code else '',
                '所属板块': (s.get('sector_name') or '').strip(),
                'sector_code': (s.get('sector_code') or '').strip(),
                '推荐理由': f"{role} | {(s.get('buy_condition') or '').strip()}" if role else (s.get('buy_condition') or '').strip(),
                '优先级': priority,
                '买入条件': (s.get('buy_condition') or '').strip(),
                '放弃条件': (s.get('abandon_condition') or '').strip(),
                '止损位': s.get('stop_loss', ''),
                '目标位': s.get('target', ''),
                '市值': '',
            })

        self.logger.info(f"✅ 从复盘股票池解析到 {len(stocks)} 只股票")
        return stocks

    def _remove_stock_pool(self, report_text: str) -> str:
        """从复盘报告中删除股票池部分（推送前调用）"""
        import re
        cleaned = re.sub(r'<<<STOCKS>>>.*?<<<END>>>', '', report_text, flags=re.DOTALL)
        cleaned = re.sub(r'\n\s*\n\s*\n', '\n\n', cleaned)
        self.logger.info("✅ 已删除复盘股票池标记")
        return cleaned
    
    def _fetch_news(self, trade_date: str) -> dict:
        """分析时实时抓取 CLS 复盘新闻"""
        try:
            from data.collectors.events.cls_digest_collector import CLSDigestCollector
            collector = CLSDigestCollector()
            return collector.collect_review()
        except Exception as e:
            self.logger.warning(f"CLS 复盘新闻抓取失败: {e}")
            return {}

    def _run_fc_review(self, prompt: str, trade_date: str, system_prompt: str) -> str:
        """
        FC 多轮对话：强制 AI 先调用新闻工具，再生成复盘报告。

        第一轮：仅暴露 get_cls_digest_news 一个工具 + Prompt 强制指令 → 变相必修
        后续轮：暴露全部工具，AI 自主选择是否调用
        """
        from system.utils.stock_tools import TOOLS_DEFINITION

        fc_engine = FunctionCallingEngine()
        news_tool_names = {'get_cls_digest_news'}
        news_tools = [t for t in TOOLS_DEFINITION if t['function']['name'] in news_tool_names]
        all_tool_names = [t['function']['name'] for t in TOOLS_DEFINITION]

        # FC 日志文件
        fc_log_path = LOGS_DIR / trade_date / 'prompts' / 'review_fc_log.txt'
        fc_log_path.parent.mkdir(parents=True, exist_ok=True)
        fc_lines = []
        def _fc_log(msg: str):
            ts = datetime.now().strftime('%H:%M:%S')
            line = f"[{ts}] {msg}"
            fc_lines.append(line)
            self.logger.info(f"  {msg}")

        _fc_log("=" * 60)
        _fc_log(f"复盘 FC 多轮对话日志 - {trade_date}")
        _fc_log(f"模型: {self.ai.model}")
        _fc_log(f"可用工具 ({len(all_tool_names)}): {', '.join(all_tool_names)}")
        _fc_log(f"必修工具: get_cls_digest_news")
        _fc_log("=" * 60)

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': prompt},
        ]

        round_num = 0
        while True:
            round_num += 1
            if round_num == 1:
                tools = news_tools
                # 仅暴露 1 个工具 + Prompt 强制指令 = 变相必修，不用 'required'（百炼 API 不兼容）
                tool_choice = 'auto'
                _fc_log(f"\n第 {round_num+1} 轮（必修轮，仅 1 个工具 + Prompt 强制）")
                _fc_log(f"  暴露工具: get_cls_digest_news")
                _fc_log(f"  tool_choice: auto")
            else:
                tools = None
                tool_choice = 'auto'
                _fc_log(f"\n第 {round_num+1} 轮（自主轮）")
                _fc_log(f"  暴露工具: 全部 {len(all_tool_names)} 个")
                _fc_log(f"  tool_choice: auto")

            response = self.ai._call_ai_with_tools(
                messages, max_tokens=8000,
                tools=tools, tool_choice=tool_choice
            )

            content = response.get('content', '')
            tool_calls = response.get('tool_calls', [])

            if content:
                preview = content[:200].replace('\n', '\\n')
                _fc_log(f"  AI 文本回复: {len(content)}字 → {preview}...")

            if tool_calls:
                _fc_log(f"  工具调用: {len(tool_calls)} 个")
                for tc in tool_calls:
                    fn = tc.get('function', {}) if isinstance(tc, dict) else tc.function
                    fn_name = fn.get('name', '?')
                    fn_args = fn.get('arguments', '{}')
                    _fc_log(f"    → {fn_name}({fn_args})")

                assistant_msg = {'role': 'assistant', 'content': content or ''}
                assistant_msg['tool_calls'] = tool_calls
                messages.append(assistant_msg)

                tool_messages = fc_engine.process_tool_calls(tool_calls)
                for tm in tool_messages:
                    # 记录返回数据（截断长内容）
                    result_str = str(tm.get('content', ''))
                    if len(result_str) > 500:
                        result_str = result_str[:500] + f"...(共{len(result_str)}字)"
                    _fc_log(f"    ← 返回: {result_str}")

                messages.extend(tool_messages)
                continue

            # 无工具调用 = 最终回复
            if content:
                _fc_log(f"\n报告完成: {len(content)}字")
                _fc_log("=" * 60)

                # 写日志文件
                try:
                    with open(fc_log_path, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(fc_lines))
                    self.logger.info(f"✅ FC 日志已保存: {fc_log_path}")
                except Exception as e:
                    self.logger.warning(f"保存 FC 日志失败: {e}")

                return content

            _fc_log(f"  ⚠️ AI 未返回内容也未调用工具")
            continue

    # 电报类别黑名单：与 A 股选股无关的类别
    TELEGRAPH_SKIP_CATEGORIES = {'期货市场情报', '原油市场动态', '环球市场情报'}
    # 标题过滤词：宏观/政治/大盘描述/重复新闻汇编
    TELEGRAPH_SKIP_KEYWORDS = [
        '习近平同', '普京', '伊朗', '投资日历', '隔夜全球要闻',
        'LPR报价', '财政收入', '发改委', '国务院令', '矿产资源',
        '李强签署', '商务部美大司', '外交部：',
        '三大指数', '沪深两市成交额突破',
        '早间新闻精选', '午间新闻精选',
    ]

    def _query_telegraph(self, trade_date: str) -> list:
        """从 DB 查询当日高评分电报（过滤宏观/政治/大盘噪音）"""
        import json as _json
        try:
            conn = sqlite3.connect(str(DATABASE_PATH))
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT * FROM cls_telegraph
                WHERE trade_date = ? AND score >= 2
                ORDER BY score DESC, reading_num DESC
                LIMIT 120
            """, (trade_date,))
            rows = [dict(r) for r in cursor.fetchall()]
            conn.close()

            # 解析 JSON 字段 + 过滤
            filtered = []
            seen_titles = set()
            for r in rows:
                for field in ('stock_tags', 'subject_tags', 'plate_tags'):
                    try:
                        r[field] = _json.loads(r[field]) if r[field] else []
                    except (_json.JSONDecodeError, TypeError):
                        r[field] = []

                # 类别黑名单
                cat = r.get('category', '')
                if cat in self.TELEGRAPH_SKIP_CATEGORIES:
                    continue

                # 标题关键词过滤
                title = r.get('title', '')
                if any(kw in title for kw in self.TELEGRAPH_SKIP_KEYWORDS):
                    continue

                # 去重：相似标题（取前 20 字做 key）
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
        focus_review = news_data.get('focus_review', {})
        if focus_review:
            text += f"\n【财联社焦点复盘】\n"
            text += f"标题：{focus_review.get('title', '无')}\n"
            text += f"时间：{focus_review.get('time', '无')}\n"
            text += f"来源：{focus_review.get('source', '无')}\n"
            text += f"字数：{focus_review.get('word_count', 0)}字\n\n"
            text += f"{focus_review.get('content', '无内容')}\n"
        
        # 每日收评
        daily_review = news_data.get('daily_review', {})
        if daily_review:
            text += f"\n{'='*80}\n"
            text += f"【财联社每日收评】\n"
            text += f"标题：{daily_review.get('title', '无')}\n"
            text += f"时间：{daily_review.get('time', '无')}\n"
            text += f"来源：{daily_review.get('source', '无')}\n"
            text += f"字数：{daily_review.get('word_count', 0)}字\n\n"
            text += f"{daily_review.get('content', '无内容')}\n"
        
        if not text:
            return "（无复盘新闻数据）"

        return text

    def _format_telegraph(self, trade_date: str, zt_codes: set = None) -> str:
        """从 DB 读取电报并格式化为 Prompt 文本（按类别分组，涨停股交叉标记，内容截断 80 字）"""
        import json as _json
        telegraph_list = self._query_telegraph(trade_date)
        if not telegraph_list:
            return "（今日无高评分电报）"

        zt_codes = zt_codes or set()

        # 按 category 分组
        groups: dict[str, list] = {}
        for t in telegraph_list:
            cat = t.get('category', '其他')
            groups.setdefault(cat, []).append(t)

        text = f"共 {len(telegraph_list)} 条高评分电报，按类别分组如下：\n"

        for cat, items in groups.items():
            text += f"\n【{cat}】（{len(items)}条）\n"
            for item in items:
                level = item.get('level', 'C')
                title = item.get('title', '无标题')
                content = item.get('content', '')
                reading = item.get('reading_num', 0)
                stock_tags = item.get('stock_tags', [])

                # 截取 content 前 80 字
                snippet = (content[:80] + '...') if len(content) > 80 else content

                # 涨停交叉比对
                zt_matched = []
                for tag in (stock_tags or []):
                    code = tag.get('code', '') if isinstance(tag, dict) else ''
                    if code and code in zt_codes:
                        zt_matched.append(tag.get('name', code))

                zt_flag = ' 🔥涨停关联' if zt_matched else ''
                line = f"- [{level}]{zt_flag} {title}"
                if snippet and snippet != title:
                    line += f" | {snippet}"

                # 关联股票（涨停股前置，最多 3 只）
                if stock_tags:
                    tags_parts = []
                    for tag in (stock_tags or [])[:3]:
                        if isinstance(tag, dict):
                            name = tag.get('name', '')
                            code = tag.get('code', '')
                            prefix = '🔥' if code in zt_codes else ''
                            tags_parts.append(f'{prefix}{name}({code})')
                    if tags_parts:
                        line += f"\n  关联：{'、'.join(tags_parts)}"
                elif zt_matched:
                    line += f"\n  涨停关联：{'、'.join(zt_matched)}"

                text += line + '\n'

        return text

    def _get_calibration_stats(self, conn, end_date: str, num_days: int = 5) -> dict:
        """
        从 stock_tracker 获取历史 AI 推荐校准统计

        Args:
            conn: 数据库连接
            end_date: 截止日期（上一个交易日）
            num_days: 回溯交易日数

        Returns:
            统计数据 dict，数据不足时返回 None
        """
        from system.config.trading_calendar import is_trading_day

        # 回溯 N 个交易日
        trading_days = []
        check_date = datetime.strptime(end_date, '%Y-%m-%d')
        for _ in range(num_days * 3):
            ds = check_date.strftime('%Y-%m-%d')
            if is_trading_day(ds):
                trading_days.append(ds)
                if len(trading_days) >= num_days:
                    break
            check_date = check_date - timedelta(days=1)

        if len(trading_days) < 1:
            self.logger.info("无历史交易日数据，跳过校准统计")
            return None

        placeholders = ','.join('?' * len(trading_days))
        cursor = conn.execute(f"""
            SELECT t.stock_code, t.stock_name, t.plate, t.star_rating,
                   t.final_return, t.push_date
            FROM stock_tracker t
            WHERE t.push_date IN ({placeholders}) AND t.source = '复盘'
              AND t.final_return IS NOT NULL
            ORDER BY t.push_date DESC, t.star_rating DESC
        """, trading_days)

        rows = [dict(row) for row in cursor.fetchall()]
        if not rows:
            self.logger.info("stock_tracker 中无历史复盘数据，跳过校准统计")
            return None

        total = len(rows)
        wins = sum(1 for r in rows if (r['final_return'] or 0) > 0)
        avg_return = sum(r['final_return'] or 0 for r in rows) / total

        # 按优先级分组
        by_priority = {}
        for r in rows:
            star = r['star_rating'] or 0
            label = {5: 'P0', 4: 'P1', 3: 'P2'}.get(star, 'P3')
            if label not in by_priority:
                by_priority[label] = {'count': 0, 'wins': 0, 'total_return': 0}
            by_priority[label]['count'] += 1
            ret = r['final_return'] or 0
            by_priority[label]['total_return'] += ret
            if ret > 0:
                by_priority[label]['wins'] += 1

        # 按板块分组
        by_sector = {}
        for r in rows:
            plate = r['plate'] or '未分类'
            if plate not in by_sector:
                by_sector[plate] = {'count': 0, 'wins': 0, 'total_return': 0}
            by_sector[plate]['count'] += 1
            ret = r['final_return'] or 0
            by_sector[plate]['total_return'] += ret
            if ret > 0:
                by_sector[plate]['wins'] += 1

        sector_stats = []
        for plate, s in by_sector.items():
            sector_stats.append({
                'plate': plate,
                'count': s['count'],
                'avg_return': s['total_return'] / s['count'],
                'win_rate': s['wins'] / s['count'] * 100,
            })
        sector_stats.sort(key=lambda x: x['avg_return'], reverse=True)

        return {
            'date_range': f"{trading_days[-1]} ~ {trading_days[0]}",
            'num_days': len(trading_days),
            'total': total,
            'wins': wins,
            'win_rate': wins / total * 100,
            'avg_return': avg_return,
            'by_priority': by_priority,
            'by_sector': sector_stats,
        }


# ============================================================
# AIAnalyzer — Low-level AI engine (from original analyzer.py)
# ============================================================

logger = get_system_logger('analyzer')

from system.utils.stock_tools import TOOLS_DEFINITION


class AIAnalyzer:
    """AI 分析引擎（支持 Function Calling）"""

    def __init__(self):
        self.api_key = os.getenv('DASHSCOPE_API_KEY', '')
        self.endpoint = os.getenv('DASHSCOPE_ENDPOINT', 'https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation')
        self.model = os.getenv('DASHSCOPE_ANALYSIS_MODEL', 'qwen3.5-plus')

        # 验证 API Key 配置
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY 未配置，请在.env 文件中设置")

        # 验证 API Key 格式（百炼 API Key 以 sk-sp-开头）
        if not (self.api_key.startswith('sk-') or self.api_key.startswith('sk-sp-')):
            logger.warning("API Key 格式可能不正确（应以 sk-或 sk-sp-开头）")

        logger.info("AI 分析引擎初始化完成（模型：{}，支持 Function Calling）".format(self.model))

    def _call_ai(self, prompt: str, system_prompt: str = "你是一个专业的 A 股量化分析师。请务必使用阿拉伯数字格式输出所有数值，不要转换为中文数字。例如：85%而不是百分之八十五，2.5万亿而不是二点五万亿，2026-04-29而不是二零二六年四月二十九日。", enable_search: bool = False, max_tokens: int = 2000) -> Optional[str]:
        """调用 AI 模型

        Args:
            prompt: 用户 prompt
            system_prompt: 系统 prompt
            enable_search: 是否启用联网搜索（百炼专属功能）
        """
        # 根据模型名选择 provider
        if self.model.startswith('deepseek'):
            api_key = os.getenv('DEEPSEEK_API_KEY', '')
            endpoint = os.getenv('DEEPSEEK_ENDPOINT', 'https://api.deepseek.com/v1/chat/completions')
        else:
            api_key = self.api_key
            endpoint = self.endpoint

        if not api_key:
            return "AI API Key 未配置，无法生成分析"

        try:
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }

            payload = {
                'model': self.model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': prompt}
                ],
                'max_tokens': max_tokens
            }

            # 启用百炼联网搜索功能（仅百炼支持）
            if enable_search and not self.model.startswith('deepseek'):
                payload['enable_search'] = True

            logger.info("开始调用 AI API（模型：{}，端点：{}，超时：600 秒）...".format(self.model, endpoint))
            response = requests.post(endpoint, json=payload, headers=headers, timeout=600)
            response.raise_for_status()

            logger.info("AI API 响应成功（状态码：{}）".format(response.status_code))
            result = response.json()

            # 百炼 API 响应格式：{choices: [{message: {content: "..."}}]}
            # 或：{output: {text: "..."}}
            content = ""
            if 'choices' in result and result['choices']:
                content = result['choices'][0].get('message', {}).get('content', '')
            elif 'output' in result:
                content = result['output'].get('text', '')

            # 过滤千问模型的 CoT 思考过程
            content = re.sub(r'<think>.*?</think>\s*', '', content, flags=re.DOTALL)

            if not content:
                logger.warning("AI 返回空内容，完整响应：{}".format(result))
            else:
                logger.info("AI 分析完成（返回 {} 字）".format(len(content)))

            return content

        except Exception as e:
            logger.error("AI 调用失败：{}".format(e))
            raise RuntimeError("AI API 调用失败: {}".format(e)) from e

    def _call_ai_with_tools(self, messages: List[Dict], max_tokens: int = 2000,
                            tools: List[Dict] = None, tool_choice: str = 'auto') -> Dict:
        """
        调用 AI（支持工具调用）

        Args:
            messages: 对话历史
            max_tokens: 最大输出 token 数（默认 2000）
            tools: 自定义工具列表，默认使用 TOOLS_DEFINITION
            tool_choice: 工具选择策略，'auto'/'required'/'none' 或指定工具

        Returns:
            {
                'content': str,  # AI 回复内容
                'tool_calls': list  # 工具调用列表（如果有）
            }
        """
        try:
            # 根据模型名选择 provider
            if self.model.startswith('deepseek'):
                api_key = os.getenv('DEEPSEEK_API_KEY', '')
                endpoint = os.getenv('DEEPSEEK_ENDPOINT', 'https://api.deepseek.com/v1/chat/completions')
            else:
                api_key = self.api_key
                endpoint = self.endpoint

            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json'
            }

            _tools = tools if tools is not None else TOOLS_DEFINITION

            payload = {
                'model': self.model,
                'messages': messages,
                'tools': _tools,
                'tool_choice': tool_choice,
                'parallel_tool_calls': True,
                'max_tokens': max_tokens
            }

            logger.info("调用 AI（支持工具，消息数：{}，tool_choice={}）...".format(len(messages), tool_choice))
            response = requests.post(endpoint, json=payload, headers=headers, timeout=600)
            response.raise_for_status()

            result = response.json()

            # 解析响应
            message = result['choices'][0]['message']
            content = message.get('content', '')
            tool_calls = message.get('tool_calls', [])

            if content:
                logger.info("AI 返回内容（{}字）".format(len(content)))

            return {
                'content': content,
                'tool_calls': tool_calls
            }

        except Exception as e:
            logger.error("AI 调用失败：{}".format(e))
            raise RuntimeError("AI API 调用失败: {}".format(e)) from e
