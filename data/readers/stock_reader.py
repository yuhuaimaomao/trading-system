# -*- coding: utf-8 -*-
"""
个股数据读取器

职责：初筛观察池、强势股。
纯数据查询，不做格式化。
"""

import logging

logger = logging.getLogger(__name__)


class StockReader:
    """个股数据读取器（所有方法均为静态）"""

    @staticmethod
    def get_candidates(conn, trade_date: str) -> list:
        """查询今日异动股（主板 20 + 创业板 20，合并后按涨幅排序）"""

        def _fetch_pool(extra_where: str, limit: int) -> list:
            cursor = conn.execute(f"""
                SELECT s.stock_code, s.stock_name, s.change_pct,
                       s.main_force_net, s.total_market_cap/100000000 as mcap,
                       s.circ_market_cap/100000000 as circ_mcap,
                       s.super_large_net/10000 as sl_wan,
                       s.large_net/10000 as lg_wan,
                       s.medium_net/10000 as md_wan,
                       s.small_net/10000 as sm_wan,
                       s.main_force_ratio, s.turnover_rate, s.volume_ratio, s.amplitude,
                       s.industry,
                       s.price, s.ma5, s.ma20, s.ma5_angle,
                       COALESCE(l.pool_type, '') as limit_type,
                       COALESCE(l.consecutive_boards, 0) as cons_boards,
                       lhb.net_inflow/100000000 as lhb_net_yi
                FROM stock_basic s
                LEFT JOIN limit_pool l ON s.stock_code = l.stock_code
                    AND s.trade_date = l.trade_date
                    AND l.pool_type IN ('涨停', '炸板')
                LEFT JOIN lhb_stocks lhb ON s.stock_code = lhb.stock_code
                    AND s.trade_date = lhb.trade_date
                WHERE s.trade_date = ?
                    AND s.stock_name NOT LIKE '%ST%'
                    AND s.stock_code NOT LIKE '688%'
                    AND (s.change_pct > 5
                         OR s.main_force_net > 50000000
                         OR l.pool_type = '涨停')
                    AND {extra_where}
                ORDER BY s.change_pct DESC
                LIMIT {limit}
            """, (trade_date,))
            return [dict(row) for row in cursor.fetchall()]

        main_board = _fetch_pool(
            "(s.stock_code LIKE '60%' OR s.stock_code LIKE '00%')", 20)
        gem_board = _fetch_pool("s.stock_code LIKE '30%'", 20)
        candidate_rows = main_board + gem_board
        candidate_rows.sort(key=lambda x: x['change_pct'] or 0, reverse=True)

        candidates = []
        for row in candidate_rows:
            candidates.append({
                'code': row['stock_code'], 'name': row['stock_name'],
                'change': row['change_pct'] or 0,
                'mf_net': row['main_force_net'] or 0,
                'mcap': row['mcap'] or 0,
                'circ_mcap': row['circ_mcap'] or 0,
                'sl_wan': row['sl_wan'] or 0,
                'lg_wan': row['lg_wan'] or 0,
                'md_wan': row['md_wan'] or 0,
                'sm_wan': row['sm_wan'] or 0,
                'mf_ratio': row['main_force_ratio'] or 0,
                'turnover': row['turnover_rate'] or 0,
                'vol_ratio': row['volume_ratio'] or 0,
                'amplitude': row['amplitude'] or 0,
                'industry': row['industry'] or '',
                'is_zt': row['limit_type'] == '涨停',
                'cons_boards': row['cons_boards'] or 0,
                'lhb_net_yi': row['lhb_net_yi'],
                'price': row['price'] or 0,
                'ma5': row['ma5'] or 0,
                'ma20': row['ma20'] or 0,
                'ma5_angle': row['ma5_angle'] or 0,
            })
        return candidates

    @staticmethod
    def get_strong_stocks(conn, trade_date: str, sectors: list) -> list:
        """
        查询近期强势股（60日新高+多次涨停，凑够 30 只）

        Args:
            sectors: 行业板块列表（用于补充 60 日新高股）
        """
        # 第一优先级：60日新高且近期多次涨停
        cursor = conn.execute("""
            SELECT ss.stock_code, ss.stock_name, ss.limit_up_days, ss.limit_up_count,
                   ss.is_limit_up, ss.reason,
                   sb.change_pct, sb.total_market_cap/100000000 as mcap,
                   sb.circ_market_cap/100000000 as circ_mcap,
                   sb.main_force_net/10000 as mf_wan,
                   sb.super_large_net/10000 as sl_wan,
                   sb.large_net/10000 as lg_wan,
                   sb.medium_net/10000 as md_wan,
                   sb.small_net/10000 as sm_wan,
                   sb.main_force_ratio, sb.turnover_rate, sb.volume_ratio, sb.amplitude,
                   sb.price, sb.ma5, sb.ma20, sb.ma5_angle
            FROM strong_stock ss
            JOIN stock_basic sb ON ss.stock_code = sb.stock_code AND sb.trade_date = ?
            WHERE ss.trade_date = ?
                AND ss.reason IN ('60日新高且近期多次涨停', '近期多次涨停')
            ORDER BY ss.limit_up_days DESC, ss.limit_up_count DESC
        """, (trade_date, trade_date))
        strong_stocks = [dict(row) for row in cursor.fetchall()]

        # 不够 30 只，从涨幅靠前板块中补 60 日新高
        if len(strong_stocks) < 30:
            existing_codes = {s['stock_code'] for s in strong_stocks}
            need = 30 - len(strong_stocks)
            top_sector_names = [s['name'] for s in sectors[:10]] if sectors else []
            if top_sector_names:
                s_placeholders = ','.join('?' * len(top_sector_names))
                exc_placeholders = ','.join('?' * len(existing_codes))
                cursor = conn.execute(f"""
                    SELECT ss.stock_code, ss.stock_name, ss.limit_up_days, ss.limit_up_count,
                           ss.is_limit_up, ss.reason,
                           sb.change_pct, sb.total_market_cap/100000000 as mcap,
                           sb.circ_market_cap/100000000 as circ_mcap,
                           sb.main_force_net/10000 as mf_wan,
                           sb.super_large_net/10000 as sl_wan,
                           sb.large_net/10000 as lg_wan,
                           sb.medium_net/10000 as md_wan,
                           sb.small_net/10000 as sm_wan,
                           sb.main_force_ratio, sb.turnover_rate, sb.volume_ratio, sb.amplitude,
                           sb.price, sb.ma5, sb.ma20, sb.ma5_angle
                    FROM strong_stock ss
                    JOIN stock_basic sb ON ss.stock_code = sb.stock_code AND sb.trade_date = ?
                    JOIN sector_stocks sk ON ss.stock_code = sk.stock_code
                    JOIN sector_info si ON sk.sector_code = si.sector_code
                    WHERE ss.trade_date = ?
                        AND si.sector_name IN ({s_placeholders})
                        AND si.need_collect = 1
                        AND ss.reason = '60日新高'
                        AND ss.stock_code NOT IN ({exc_placeholders})
                    ORDER BY ss.limit_up_days DESC
                    LIMIT ?
                """, [trade_date, trade_date] + top_sector_names + list(existing_codes) + [need])
                supplement = [dict(row) for row in cursor.fetchall()]
                strong_stocks.extend(supplement)

        return strong_stocks

    @staticmethod
    def get_trend_stocks(conn, trade_date: str) -> dict:
        """
        双模式趋势票筛选：

        5日线强趋势 (strong): 沿MA5陡峭爬升，主升浪追涨型
          - 站上MA5, MA5>MA10>MA20 多头排列
          - 偏离MA5<5%, MA5-MA20乖离>3%
          - 量能: avg_vol_5d >= avg_vol_20d * 0.9

        20日线稳健趋势 (normal): 沿MA20稳健上行，回调低吸型
          - 站上MA20, 偏离MA20<10%
          - 量能: avg_vol_5d >= avg_vol_20d * 0.9
          - 排除已归入强趋势的股票

        Returns:
            {'strong': [...], 'normal': [...]} 各 TOP10
        """
        cursor = conn.execute("""
            SELECT stock_code, stock_name, change_pct,
                   total_market_cap/100000000 as mcap,
                   circ_market_cap/100000000 as circ_mcap,
                   turnover_rate, volume_ratio,
                   ma5, ma10, ma20, ma5_angle,
                   industry, price,
                   main_force_net/10000 as mf_wan,
                   main_force_ratio,
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
        """, (trade_date,))

        strong = []
        normal = []
        strong_codes = set()

        for row in cursor.fetchall():
            code = row[0]
            name = row[1]
            change_pct = row[2] or 0
            mcap = row[3] or 0
            circ_mcap = row[4] or 0
            turnover_rate = row[5] or 0
            volume_ratio = row[6] or 0
            ma5 = row[7] or 0
            ma10 = row[8] or 0
            ma20 = row[9] or 0
            ma5_angle = row[10] or 0
            industry = row[11] or ''
            price = row[12] or 0
            mf_wan = row[13] or 0
            mf_ratio = row[14] or 0

            record = {
                'stock_code': code, 'stock_name': name,
                'change_pct': change_pct, 'mcap': mcap, 'circ_mcap': circ_mcap,
                'turnover_rate': turnover_rate, 'volume_ratio': volume_ratio,
                'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'ma5_angle': ma5_angle,
                'industry': industry, 'price': price,
                'mf_wan': mf_wan, 'mf_ratio': mf_ratio,
            }

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
                record['mode'] = 'strong'
                record['score'] = round(slope_score, 1)
                record['bias_ma5'] = round(bias_ma5 * 100, 2)
                strong.append(record)
                strong_codes.add(code)
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
                record['mode'] = 'normal'
                record['score'] = round(normal_score, 1)
                record['bias_ma20'] = round(dev_pct, 2)
                normal.append(record)

            if len(strong) >= 10 and len(normal) >= 10:
                break

        strong.sort(key=lambda x: x['score'], reverse=True)
        normal.sort(key=lambda x: x['score'], reverse=True)

        # 板块趋势过滤
        all_codes = [r['stock_code'] for r in strong[:10]] + [r['stock_code'] for r in normal[:10]]
        if all_codes:
            from data.readers.sector_reader import SectorReader
            passed = SectorReader.filter_by_sector_trend(conn, trade_date, all_codes)
            strong = [r for r in strong if r['stock_code'] in passed][:10]
            normal = [r for r in normal if r['stock_code'] in passed][:10]

        return {
            'strong': strong,
            'normal': normal,
        }
