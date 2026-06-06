"""
板块数据读取器

职责：查询行业/概念板块排行、资金流、板块个股明细、概念补充。
不做格式化、不做判断，纯数据查询。
"""

import logging

logger = logging.getLogger(__name__)


class SectorReader:
    """板块数据读取器（所有方法均为静态，传入 conn + trade_date）"""

    @staticmethod
    def get_industry_sectors(conn, trade_date: str) -> tuple:
        """
        查询行业板块排行 + 资金流

        Returns:
            (sectors, fund_flow_map)
            sectors: [{'name', 'change', 'up_count', 'main_force_net', 'super_large_net'}, ...]
            fund_flow_map: {name: {'main_force_net', 'super_large_net'}, ...}
        """
        cursor = conn.execute(
            """
            SELECT s.sector_name as name, s.change_percent as change,
                   s.up_count, s.main_force_net, s.super_large_net
            FROM sector_industry s
            INNER JOIN sector_info si ON s.sector_code = si.sector_code
            WHERE s.trade_date = ? AND si.need_collect = 1
            ORDER BY s.change_percent DESC
        """,
            (trade_date,),
        )
        sectors = [dict(row) for row in cursor.fetchall()]

        fund_flow_map = {}
        for s in sectors:
            fund_flow_map[s["name"]] = {
                "main_force_net": s.get("main_force_net", 0) or 0,
                "super_large_net": s.get("super_large_net", 0) or 0,
            }
        return sectors, fund_flow_map

    @staticmethod
    def get_concept_sectors(conn, trade_date: str) -> tuple:
        """
        查询概念板块排行 + 资金流

        Returns:
            (concept_sectors, concept_fund_map)
        """
        cursor = conn.execute(
            """
            SELECT s.sector_name as name, s.change_percent as change,
                   s.up_count, s.main_force_net, s.super_large_net
            FROM sector_concept s
            INNER JOIN sector_info si ON s.sector_code = si.sector_code
            WHERE s.trade_date = ? AND si.need_collect = 1
            ORDER BY s.change_percent DESC
        """,
            (trade_date,),
        )
        concept_sectors = [dict(row) for row in cursor.fetchall()]

        concept_fund_map = {}
        for s in concept_sectors:
            concept_fund_map[s["name"]] = {
                "main_force_net": s.get("main_force_net", 0) or 0,
                "super_large_net": s.get("super_large_net", 0) or 0,
            }
        return concept_sectors, concept_fund_map

    @staticmethod
    def _calc_hot_days(conn, trade_date: str, table_name: str) -> dict:
        """统计每个板块近10个交易日内进入综合打分前10的天数"""
        from system.config.trading_calendar import get_previous_trading_day

        sector_type = "industry" if table_name == "sector_industry" else "concept"

        start_date = trade_date
        for _ in range(10):
            start_date = get_previous_trading_day(start_date)

        cursor = conn.execute(
            """
            SELECT sector_code, COUNT(*) as appear_count
            FROM sector_hot_history
            WHERE sector_type = ? AND trade_date BETWEEN ? AND ?
            GROUP BY sector_code
        """,
            (sector_type, start_date, trade_date),
        )

        return {row["sector_code"]: row["appear_count"] for row in cursor.fetchall()}

    @staticmethod
    def _calc_hot_trend(
        sector_codes: list, conn, trade_date: str, sector_type: str
    ) -> dict:
        """查询 sector_hot_history，返回每板块的连续上榜天数 + 热度变化方向"""
        if not sector_codes:
            return {}
        placeholders = ",".join("?" * len(sector_codes))
        cursor = conn.execute(
            f"""
            SELECT sector_code, trade_date, hot_score
            FROM sector_hot_history
            WHERE sector_code IN ({placeholders}) AND sector_type = ?
            ORDER BY sector_code, trade_date DESC
        """,
            sector_codes + [sector_type],
        )
        from collections import defaultdict

        records = defaultdict(list)
        for row in cursor.fetchall():
            records[row["sector_code"]].append((row["trade_date"], row["hot_score"]))

        from system.config.trading_calendar import get_previous_trading_day

        yesterday = get_previous_trading_day(trade_date)

        result = {}
        for code in sector_codes:
            entries = records.get(code, [])
            # 连续上榜天数：从昨天往前数，连续出现的次数
            consecutive = 0
            expected_date = yesterday
            for d, _ in entries:
                if d == expected_date:
                    consecutive += 1
                    expected_date = get_previous_trading_day(expected_date)
                elif d < expected_date:
                    break
            # 热度趋势：今天 vs 昨天
            today_score = None
            yesterday_score = None
            for d, score in entries:
                if d == trade_date:
                    today_score = score
                elif d == yesterday:
                    yesterday_score = score
            trend = 0  # 0=首次上榜, 1=上升, -1=下滑, 2=持平
            if today_score is not None and yesterday_score is not None:
                diff = today_score - yesterday_score
                if diff > 3:
                    trend = 1
                elif diff < -3:
                    trend = -1
                else:
                    trend = 2
            result[code] = {"consecutive": consecutive, "trend": trend}
        return result

    @staticmethod
    def get_hot_sectors(
        conn,
        trade_date: str,
        sector_table: str,
        top_n: int = 10,
        prev_date: str = None,
        prev_prev_date: str = None,
    ) -> list:
        """
        综合打分获取热点板块（涨停25%+成交占比20%+资金20%+涨幅10%+持续性20%+晋级5%），含板块内个股得分

        过滤规则：need_collect=0 剔除、总流通市值 <= 0 剔除
        板块打分：
          涨停家数(25%) = 绝对值 sigmoid（1→22分, 3→39分, 5→50分, 10→71分, 20→100分）
          成交占比(15%) = 板块成交/全市场成交，排名百分位
          资金态度(20%) = 主力净额>0 计排名百分位，负流×0.3
          涨跌幅(15%) = 排名百分位，涨幅<1% ×0.5
          持续性(20%) = 近10天进前10天数，排名百分位
          晋级率(5%)   = 板块内2板+涨停股数，排名百分位
        个股打分（板块内）：
          涨幅分×0.30 + 连板分×0.30 + 超大单分×0.20 + 量比分×0.10 + 市值分×0.10
          取前10 + 兜底涨停股，去重

        Returns:
            [{sector_code, name, change, total_count, limit_count, main_force_net(亿),
              hot_days, top_stock, top_stock_change, change_seq, stocks: [...]}, ...]
        """
        # ===== 第一步：板块评分 =====
        cursor = conn.execute(
            f"""
            SELECT si.sector_code,
                   si.sector_name,
                   s.change_percent,
                   s.main_force_net,
                   s.top_stock,
                   s.top_stock_change,
                   s.up_count,
                   s.down_count,
                   COUNT(ss.stock_code) as total_count,
                   COUNT(lp.stock_code) as limit_count,
                   SUM(sb.circ_market_cap) as total_circ_cap,
                   SUM(sb.turnover) as sector_turnover
            FROM {sector_table} s
            JOIN sector_info si ON s.sector_code = si.sector_code
            JOIN sector_stocks ss ON s.sector_code = ss.sector_code
            JOIN stock_basic sb ON ss.stock_code = sb.stock_code
                AND sb.trade_date = ? AND sb.volume > 0
            LEFT JOIN limit_pool lp ON ss.stock_code = lp.stock_code
                AND lp.trade_date = ? AND lp.pool_type = '涨停'
            WHERE s.trade_date = ? AND si.need_collect = 1
            GROUP BY si.sector_code, si.sector_name, s.change_percent, s.main_force_net
        """,
            (trade_date, trade_date, trade_date),
        )

        cols = [desc[0] for desc in cursor.description]
        sectors = []
        for row in cursor.fetchall():
            d = dict(zip(cols, row))
            total = d.get("total_count") or 0
            circ_cap = d.get("total_circ_cap") or 0
            if circ_cap <= 0:
                continue
            sectors.append(d)

        if not sectors:
            return []

        hot_days_map = SectorReader._calc_hot_days(conn, trade_date, sector_table)

        # 全市场成交额（用于计算板块成交占比）
        turnover_row = conn.execute(
            "SELECT SUM(turnover) FROM stock_basic WHERE trade_date = ?", (trade_date,)
        ).fetchone()
        total_market_turnover = turnover_row[0] or 1

        # 各板块 2板+ 涨停股数（涨停晋级率维度）
        chain_count_map = {}
        all_sc = [s["sector_code"] for s in sectors]
        if all_sc:
            ph_sc = ",".join("?" * len(all_sc))
            chain_rows = conn.execute(
                f"""
                SELECT ss.sector_code, COUNT(DISTINCT lp.stock_code) as chain_count
                FROM sector_stocks ss
                JOIN limit_pool lp ON ss.stock_code = lp.stock_code
                WHERE lp.trade_date = ? AND lp.pool_type = '涨停'
                  AND lp.consecutive_boards >= 2
                  AND ss.sector_code IN ({ph_sc})
                GROUP BY ss.sector_code
            """,
                [trade_date] + all_sc,
            ).fetchall()
            chain_count_map = {
                row["sector_code"]: row["chain_count"] for row in chain_rows
            }

        for s in sectors:
            mf_net = s.get("main_force_net") or 0
            s["limit_count"] = s.get("limit_count") or 0
            s["money_ratio"] = mf_net / s["total_circ_cap"]
            s["hot_days"] = hot_days_map.get(s["sector_code"], 0)
            s["up_count"] = s.get("up_count") or 0
            s["down_count"] = s.get("down_count") or 0
            s["turnover_share"] = (
                s.get("sector_turnover") or 0
            ) / total_market_turnover
            s["chain_count"] = chain_count_map.get(s["sector_code"], 0)

        n = len(sectors)

        def rank_pct(key):
            sorted_s = sorted(sectors, key=lambda x: x[key])
            rank_map = {s["sector_code"]: i for i, s in enumerate(sorted_s)}
            for s in sectors:
                s[key + "_pct"] = (
                    rank_map[s["sector_code"]] / (n - 1) * 100 if n > 1 else 0
                )

        rank_pct("change_percent")
        rank_pct("hot_days")
        rank_pct("turnover_share")
        rank_pct("chain_count")
        rank_pct("money_ratio")

        for s in sectors:
            # 涨停家数：绝对值 sigmoid（1→22, 3→39, 5→50, 10→71, 20→100）
            limit_n = s["limit_count"]
            if limit_n >= 20:
                limit_v = 100
            elif limit_n <= 0:
                limit_v = 0
            else:
                limit_v = round(100 * (limit_n / 20) ** 0.5)

            # 资金态度：正流全分，负流×0.3 留余地
            mf_net = s.get("main_force_net") or 0
            money_v = s["money_ratio_pct"] if mf_net > 0 else s["money_ratio_pct"] * 0.3

            # 涨跌幅：<1% 打折
            change_v = s["change_percent_pct"]
            if (s["change_percent"] or 0) < 1:
                change_v *= 0.5

            # 小板块惩罚
            total = s["total_count"]
            penalty = 1.0
            if total <= 3:
                penalty = 0.55
            elif total <= 5:
                penalty = 0.72

            s["hot_score"] = (
                round(
                    limit_v * 0.25
                    + s["turnover_share_pct"] * 0.15
                    + money_v * 0.20
                    + change_v * 0.15
                    + s["hot_days_pct"] * 0.20
                    + s["chain_count_pct"] * 0.05,
                    2,
                )
                * penalty
            )

        sectors.sort(key=lambda x: x["hot_score"], reverse=True)
        top_sectors = sectors[:top_n]

        # 查询连续上榜天数 + 热度趋势
        top_codes = [s["sector_code"] for s in top_sectors]
        hot_trend_map = SectorReader._calc_hot_trend(
            top_codes,
            conn,
            trade_date,
            "industry" if sector_table == "sector_industry" else "concept",
        )

        result = []
        for s in top_sectors:
            code = s["sector_code"]
            trend_data = hot_trend_map.get(code, {"consecutive": 0, "trend": 0})
            up = s.get("up_count") or 0
            down = s.get("down_count") or 0
            result.append(
                {
                    "sector_code": code,
                    "name": s["sector_name"],
                    "change": round(s["change_percent"] or 0, 2),
                    "total_count": s["total_count"],
                    "limit_count": s["limit_count"],
                    "up_count": up,
                    "down_count": down,
                    "up_down_ratio": round(up / down, 1)
                    if down > 0
                    else (up if up > 0 else 0),
                    "main_force_net": round((s["main_force_net"] or 0) / 100000000, 2),
                    "hot_days": s["hot_days"],
                    "consecutive_hot_days": trend_data["consecutive"],
                    "hot_trend": trend_data["trend"],
                    "hot_score": round(s["hot_score"], 2),
                    "turnover_share": round(s.get("turnover_share") or 0, 4),
                    "chain_count": s.get("chain_count") or 0,
                    "top_stock": s.get("top_stock") or "",
                    "top_stock_change": s.get("top_stock_change") or 0,
                    "change_seq": [],
                    "stocks": [],
                }
            )

        # ===== 第二步：3 日涨跌幅序列 =====
        if prev_date and prev_prev_date:
            codes = [s["sector_code"] for s in result]
            placeholders = ",".join("?" * len(codes))
            seq_rows = conn.execute(
                f"""
                SELECT sector_code, trade_date, change_percent
                FROM {sector_table}
                WHERE sector_code IN ({placeholders}) AND trade_date IN (?, ?, ?)
            """,
                codes + [prev_prev_date, prev_date, trade_date],
            ).fetchall()

            seq_map = {}
            for row in seq_rows:
                seq_map.setdefault(row["sector_code"], {})[row["trade_date"]] = round(
                    row["change_percent"] or 0, 2
                )

            for s in result:
                m = seq_map.get(s["sector_code"], {})
                s["change_seq"] = [
                    m.get(prev_prev_date, None),
                    m.get(prev_date, None),
                    m.get(trade_date, None),
                ]

        # ===== 2.5. 板块MA + 排名变化 + 资金流3日趋势 =====
        top_codes = [s["sector_code"] for s in result]
        if top_codes:
            # 板块 MA5/MA20
            sector_ma_map = SectorReader._calc_sector_ma(
                conn, trade_date, top_codes, sector_table
            )

            # 排名变化（昨日排名 vs 今日排名）
            rank_change_map = {}
            if prev_date:
                # 用同样逻辑获取昨日排名
                prev_cursor = conn.execute(
                    f"""
                    SELECT si.sector_code, si.sector_name, s.change_percent,
                           s.main_force_net, COUNT(ss.stock_code) as total_count,
                           COUNT(lp.stock_code) as limit_count
                    FROM {sector_table} s
                    JOIN sector_info si ON s.sector_code = si.sector_code
                    JOIN sector_stocks ss ON s.sector_code = ss.sector_code
                    JOIN stock_basic sb ON ss.stock_code = sb.stock_code
                        AND sb.trade_date = ? AND sb.volume > 0
                    LEFT JOIN limit_pool lp ON ss.stock_code = lp.stock_code
                        AND lp.trade_date = ? AND lp.pool_type = '涨停'
                    WHERE s.trade_date = ? AND si.need_collect = 1
                    GROUP BY si.sector_code
                """,
                    (prev_date, prev_date, prev_date),
                )
                # 简单排名：按 change_percent DESC
                prev_sorted = sorted(
                    [
                        dict(zip([d[0] for d in prev_cursor.description], row))
                        for row in prev_cursor.fetchall()
                    ],
                    key=lambda x: x.get("change_percent") or 0,
                    reverse=True,
                )
                prev_rank_map = {
                    s["sector_code"]: i + 1 for i, s in enumerate(prev_sorted)
                }
                for i, s in enumerate(result):
                    prev_rank = prev_rank_map.get(s["sector_code"])
                    curr_rank = i + 1  # 当前排名（在 result 中的位置）
                    if prev_rank is not None:
                        rank_change_map[s["sector_code"]] = prev_rank - curr_rank
                    else:
                        rank_change_map[s["sector_code"]] = None

            # 资金流 3 日趋势
            fund_flow_3d_map = {}
            if prev_date and prev_prev_date:
                ph_codes = ",".join("?" * len(top_codes))
                flow_rows = conn.execute(
                    f"""
                    SELECT sector_code, trade_date, main_force_net
                    FROM {sector_table}
                    WHERE sector_code IN ({ph_codes}) AND trade_date IN (?, ?, ?)
                """,
                    top_codes + [prev_prev_date, prev_date, trade_date],
                ).fetchall()
                for row in flow_rows:
                    fund_flow_3d_map.setdefault(row["sector_code"], {})[
                        row["trade_date"]
                    ] = round((row["main_force_net"] or 0) / 100000000, 2)

            for i, s in enumerate(result):
                code = s["sector_code"]
                s["rank"] = i + 1
                ma = sector_ma_map.get(code, {})
                s["sector_ma5"] = ma.get("ma5")
                s["sector_ma20"] = ma.get("ma20")
                s["rank_change"] = rank_change_map.get(code)
                ff = fund_flow_3d_map.get(code, {})
                s["fund_flow_3d"] = (
                    [
                        ff.get(prev_prev_date),
                        ff.get(prev_date),
                        ff.get(trade_date),
                    ]
                    if prev_prev_date
                    else []
                )

        # ===== 第三步：板块成分股查询 + 打分 =====
        all_codes = [s["sector_code"] for s in result]
        if all_codes:
            s_placeholders = ",".join("?" * len(all_codes))
            stock_rows = conn.execute(
                f"""
                SELECT ss.sector_code, sb.stock_code, sb.stock_name,
                       sb.change_pct, sb.circ_market_cap,
                       sb.super_large_net, sb.volume_ratio, sb.turnover_rate,
                       sb.amplitude, sb.main_force_net, sb.main_force_ratio,
                       sb.ma5, sb.ma10, sb.ma20, sb.ma5_angle,
                       COALESCE(lp.consecutive_boards, 0) as boards,
                       lp.first_seal_time, lp.open_count
                FROM sector_stocks ss
                JOIN stock_basic sb ON ss.stock_code = sb.stock_code
                    AND sb.trade_date = ? AND sb.volume > 0
                LEFT JOIN limit_pool lp ON sb.stock_code = lp.stock_code
                    AND sb.trade_date = lp.trade_date AND lp.pool_type = '涨停'
                WHERE ss.sector_code IN ({s_placeholders})
                    AND sb.stock_name NOT LIKE '%ST%'
                    AND sb.stock_code NOT LIKE '688%'
            """,
                [trade_date] + all_codes,
            ).fetchall()

            # 按板块分组
            from collections import defaultdict

            sector_stocks = defaultdict(list)
            for row in stock_rows:
                sc = row["sector_code"]
                raw_cap = row["circ_market_cap"] or 0
                raw_sl = row["super_large_net"] or 0
                raw_mf = row["main_force_net"] or 0
                sector_stocks[sc].append(
                    {
                        "code": row["stock_code"],
                        "name": row["stock_name"],
                        "change": row["change_pct"] or 0,
                        "circ_mcap": raw_cap / 100000000,
                        "sl_wan": raw_sl / 10000,
                        "sl_ratio": raw_sl / raw_cap if raw_cap > 0 else 0,
                        "vol_ratio": row["volume_ratio"] or 0,
                        "turnover": row["turnover_rate"] or 0,
                        "amplitude": row["amplitude"] or 0,
                        "mf_wan": raw_mf / 10000,
                        "mf_ratio": row["main_force_ratio"] or 0,
                        "ma5": row["ma5"] or 0,
                        "ma10": row["ma10"] or 0,
                        "ma20": row["ma20"] or 0,
                        "ma5_angle": row["ma5_angle"] or 0,
                        "boards": row["boards"] or 0,
                        "first_seal": row["first_seal_time"] or "",
                        "open_count": row["open_count"] or 0,
                    }
                )

            # 逐板块打分选股
            for s in result:
                stocks = sector_stocks.get(s["sector_code"], [])
                if not stocks:
                    continue

                n_stocks = len(stocks)

                def stock_rank_pct(key):
                    sorted_st = sorted(stocks, key=lambda x: x.get(key) or 0)
                    rank_map = {st["code"]: i for i, st in enumerate(sorted_st)}
                    for st in stocks:
                        st[key + "_pct"] = (
                            rank_map[st["code"]] / (n_stocks - 1) * 100
                            if n_stocks > 1
                            else 0
                        )

                stock_rank_pct("change")
                stock_rank_pct("boards")
                stock_rank_pct("sl_ratio")
                stock_rank_pct("vol_ratio")
                stock_rank_pct("circ_mcap")

                for st in stocks:
                    st["stock_score"] = round(
                        st["change_pct"] * 0.30
                        + st["boards_pct"] * 0.30
                        + st["sl_ratio_pct"] * 0.20
                        + st["vol_ratio_pct"] * 0.10
                        + st["circ_mcap_pct"] * 0.10,
                        2,
                    )

                # 市值排名（用于兜底）
                stocks_by_cap = sorted(
                    stocks, key=lambda x: x["circ_mcap"] or 0, reverse=True
                )
                for i, st in enumerate(stocks_by_cap, 1):
                    st["cap_rank"] = i

                # 按得分排序取前10
                stocks.sort(key=lambda x: x["stock_score"], reverse=True)
                selected = {st["code"]: st for st in stocks[:10]}

                # 兜底：涨停股
                for st in stocks:
                    if st["boards"] >= 1:
                        if st["code"] not in selected:
                            selected[st["code"]] = st

                s["stocks"] = sorted(
                    selected.values(), key=lambda x: x["stock_score"], reverse=True
                )

        return result

    @staticmethod
    def get_sector_stocks(
        conn,
        trade_date: str,
        day_before: str,
        yesterday: str,
        sector_codes: list,
        sector_table: str,
        label: str,
    ) -> list:
        """
        查询板块个股明细（含排名、3日趋势、封板时间，仅 top10 + 涨停）

        Args:
            sector_codes: 板块代码列表（已按热度排序）
            sector_table: 'sector_industry' 或 'sector_concept'
            label: '行业' 或 '概念'

        Returns:
            [{'sector': name, 'stocks': [...], 'label': label}, ...]
        """
        if not sector_codes:
            return []

        s_placeholders = ",".join("?" * len(sector_codes))
        cursor = conn.execute(
            f"""
            SELECT si.sector_code, si.sector_name, sb.stock_code, sb.stock_name,
                   sb.change_pct, sb.total_market_cap/100000000 as mcap,
                   sb.circ_market_cap/100000000 as circ_mcap,
                   sb.main_force_net/10000 as mf_wan,
                   sb.super_large_net/10000 as sl_wan,
                   sb.large_net/10000 as lg_wan,
                   sb.medium_net/10000 as md_wan,
                   sb.small_net/10000 as sm_wan,
                   sb.main_force_ratio, sb.turnover_rate, sb.volume_ratio, sb.amplitude,
                   sb.ma5, sb.ma20, sb.ma5_angle,
                   COALESCE(lp.consecutive_boards, 0) as boards,
                   lp.first_seal_time, lp.open_count
            FROM {sector_table} si
            JOIN sector_stocks ss ON si.sector_code = ss.sector_code
            JOIN stock_basic sb ON ss.stock_code = sb.stock_code
                AND sb.trade_date = ? AND sb.volume > 0
            LEFT JOIN limit_pool lp ON sb.stock_code = lp.stock_code
                AND sb.trade_date = lp.trade_date AND lp.pool_type = '涨停'
            WHERE si.trade_date = ?
                AND si.sector_code IN ({s_placeholders})
                AND sb.stock_name NOT LIKE '%ST%'
                AND sb.stock_code NOT LIKE '688%'
        """,
            [trade_date, trade_date] + sector_codes,
        )

        # 用 code 分组（保持热度排序），name 用于展示
        groups_by_code = {}
        for row in cursor.fetchall():
            sc = row["sector_code"]
            sn = row["sector_name"]
            if sc not in groups_by_code:
                groups_by_code[sc] = {"name": sn, "stocks": []}
            groups_by_code[sc]["stocks"].append(
                {
                    "code": row["stock_code"],
                    "name": row["stock_name"],
                    "change": row["change_pct"] or 0,
                    "mcap": row["mcap"] or 0,
                    "circ_mcap": row["circ_mcap"] or 0,
                    "mf_wan": row["mf_wan"] or 0,
                    "sl_wan": row["sl_wan"] or 0,
                    "lg_wan": row["lg_wan"] or 0,
                    "md_wan": row["md_wan"] or 0,
                    "sm_wan": row["sm_wan"] or 0,
                    "mf_ratio": row["main_force_ratio"] or 0,
                    "turnover": row["turnover_rate"] or 0,
                    "vol_ratio": row["volume_ratio"] or 0,
                    "amplitude": row["amplitude"] or 0,
                    "boards": row["boards"] or 0,
                    "first_seal": row["first_seal_time"] or "",
                    "open_count": row["open_count"] or 0,
                    "ma5": row["ma5"] or 0,
                    "ma20": row["ma20"] or 0,
                    "ma5_angle": row["ma5_angle"] or 0,
                }
            )

        if not groups_by_code:
            return []

        # 收集所有股票代码，查询 3 日历史涨跌幅
        all_codes = [
            s["code"] for info in groups_by_code.values() for s in info["stocks"]
        ]
        hist_data = {}
        if all_codes:
            hist_placeholders = ",".join("?" * len(all_codes))
            cursor = conn.execute(
                f"""
                SELECT stock_code, change_pct, trade_date
                FROM stock_basic
                WHERE stock_code IN ({hist_placeholders})
                  AND trade_date IN (?, ?, ?)
                ORDER BY stock_code, trade_date
            """,
                all_codes + [day_before, yesterday, trade_date],
            )
            for row in cursor.fetchall():
                code = row["stock_code"]
                hist_data.setdefault(code, {})[row["trade_date"]] = (
                    row["change_pct"] or 0
                )

        # 逐板块计算综合得分，保持传入热度排序输出
        result = []
        for sc in sector_codes:
            if sc not in groups_by_code:
                continue

            sn = groups_by_code[sc]["name"]
            stocks = groups_by_code[sc]["stocks"]
            total = len(stocks)

            # 板块内涨幅排名
            stocks_by_change = sorted(stocks, key=lambda x: x["change"], reverse=True)
            change_rank = {s["code"]: i for i, s in enumerate(stocks_by_change, 1)}

            # 市值排名
            stocks_by_cap = sorted(stocks, key=lambda x: x["mcap"], reverse=True)
            cap_rank = {s["code"]: i for i, s in enumerate(stocks_by_cap, 1)}

            # 连涨天数（D-2 → D-1 → 今天，连续涨几天）
            consec_up = {}
            for s in stocks:
                hist = hist_data.get(s["code"], {})
                d2 = hist.get(day_before)
                d1 = hist.get(yesterday)
                td = hist.get(trade_date)
                days = 0
                if td is not None and td > 0:
                    days = 1
                    if d1 is not None and d1 > 0:
                        days = 2
                        if d2 is not None and d2 > 0:
                            days = 3
                consec_up[s["code"]] = days

            # 综合得分：涨幅30% + 连板30% + 超大单20% + 量比10% + 市值10%
            max_change = max(abs(s["change"]) for s in stocks) or 1
            max_sl = max(abs(s["sl_wan"]) for s in stocks) or 1
            max_vol = max(s["vol_ratio"] for s in stocks) or 1
            max_cap = max(s["mcap"] for s in stocks) or 1

            def board_score(boards):
                if boards >= 4:
                    return 100
                elif boards >= 3:
                    return 90
                elif boards >= 2:
                    return 75
                elif boards >= 1:
                    return 50
                return 0

            for s in stocks:
                change_s = abs(s["change"]) / max_change * 100
                board_s = board_score(s["boards"])
                sl_s = abs(s["sl_wan"]) / max_sl * 100
                vol_s = s["vol_ratio"] / max_vol * 100
                cap_s = s["mcap"] / max_cap * 100
                s["composite_score"] = (
                    change_s * 0.30
                    + board_s * 0.30
                    + sl_s * 0.20
                    + vol_s * 0.10
                    + cap_s * 0.10
                )

            # 按综合得分排序，取TOP10
            by_score = sorted(stocks, key=lambda x: x["composite_score"], reverse=True)
            top_codes = {s["code"] for s in by_score[:10]}

            # 涨停股强制塞入
            limit_codes = {s["code"] for s in stocks if s["boards"] >= 1}

            # 市值最大票强制塞入
            largest_cap_code = stocks_by_cap[0]["code"] if stocks_by_cap else None
            if largest_cap_code:
                limit_codes.add(largest_cap_code)

            selected_codes = top_codes | limit_codes

            # 构建输出：按综合得分排序
            enriched = [s for s in by_score if s["code"] in selected_codes]

            for s in enriched:
                s["cap_rank"] = f"{cap_rank[s['code']]}/{total}"
                s["change_rank"] = f"{change_rank[s['code']]}/{total}"
                s["consec_up"] = consec_up.get(s["code"], 0)

            result.append({"sector": sn, "stocks": enriched, "label": label})

        return result

    @staticmethod
    def get_sector_stats(conn, trade_date: str) -> dict | None:
        """查询行业板块统计数据。"""
        rows = conn.execute(
            """SELECT sector_name as name, change_percent as change,
                      up_count, main_force_net, super_large_net
               FROM sector_industry WHERE trade_date = ?""",
            (trade_date,),
        ).fetchall()
        if not rows:
            return None
        return {row["name"]: dict(row) for row in rows}

    @staticmethod
    def get_sector_change(conn, sector_code: str, trade_date: str) -> float | None:
        """查询板块涨跌幅。"""
        row = conn.execute(
            "SELECT change_percent FROM sector_industry WHERE sector_code=? AND trade_date=?",
            (sector_code, trade_date),
        ).fetchone()
        return row["change_percent"] if row else None

    @staticmethod
    def get_concept_stats(conn, trade_date: str) -> dict | None:
        """查询概念板块统计数据。"""
        rows = conn.execute(
            """SELECT sector_name as name, change_percent as change,
                      up_count, main_force_net, super_large_net
               FROM sector_concept WHERE trade_date = ?""",
            (trade_date,),
        ).fetchall()
        if not rows:
            return None
        return {row["name"]: dict(row) for row in rows}

    @staticmethod
    def _calc_sector_ma(
        conn, trade_date: str, sector_codes: list, sector_table: str
    ) -> dict:
        """计算板块 MA5/MA20（基于 latest_price 的近 25 个交易日数据）"""
        if not sector_codes:
            return {}

        from system.config.trading_calendar import get_recent_trading_days

        recent_days = get_recent_trading_days(trade_date, 25)
        if not recent_days:
            return {}
        recent_days.sort()

        ph_sc = ",".join("?" * len(sector_codes))
        ph_days = ",".join("?" * len(recent_days))
        cursor = conn.execute(
            f"""
            SELECT sector_code, trade_date, latest_price
            FROM {sector_table}
            WHERE sector_code IN ({ph_sc})
              AND trade_date IN ({ph_days})
            ORDER BY sector_code, trade_date
        """,
            sector_codes + recent_days,
        )

        from collections import defaultdict

        price_series = defaultdict(list)
        for row in cursor.fetchall():
            price = row["latest_price"]
            if price and price > 0:
                price_series[row["sector_code"]].append(price)

        result = {}
        for code in sector_codes:
            prices = price_series.get(code, [])
            ma5 = round(sum(prices[-5:]) / 5, 2) if len(prices) >= 5 else None
            ma20 = round(sum(prices[-20:]) / 20, 2) if len(prices) >= 20 else None
            result[code] = {"ma5": ma5, "ma20": ma20}
        return result

    @staticmethod
    def get_sector_zhongjun(
        conn, trade_date: str, sector_codes: list, sector_table: str, top_n: int = 5
    ) -> dict:
        """
        为每个板块筛选中军候选（4维打分）

        维度：
        1. 市值（30%）：板块内总市值排名百分位，>150亿满分
        2. 流动性（25%）：近5日均成交额，>20亿满分
        3. 趋势（25%）：MA5>MA10>MA20 完整多头=满分，MA5>MA20=半分
        4. 相对强度（20%）：近5日个股累计涨幅 vs 板块累计涨幅

        Returns:
            {sector_code: [{'code', 'name', 'score', 'mcap', 'avg_turnover_5d',
                            'trend_status', 'rel_strength', 'ma5', 'ma10', 'ma20',
                            'change_pct', 'boards', 'industry'}, ...]}
        """
        if not sector_codes:
            return {}

        from system.config.trading_calendar import get_recent_trading_days

        recent_5 = get_recent_trading_days(trade_date, 5)
        if len(recent_5) < 3:
            return {}

        all_dates = recent_5 + [trade_date]

        # 板块名称映射
        sc_ph = ",".join("?" * len(sector_codes))
        name_rows = conn.execute(
            f"""
            SELECT sector_code, sector_name FROM {sector_table}
            WHERE sector_code IN ({sc_ph}) AND trade_date = ?
        """,
            sector_codes + [trade_date],
        )
        sector_names = {row["sector_code"]: row["sector_name"] for row in name_rows}

        # ===== Step 1: 今日成分股数据 =====
        stock_rows = conn.execute(
            f"""
            SELECT ss.sector_code, sb.stock_code, sb.stock_name,
                   sb.total_market_cap/100000000 as mcap,
                   sb.turnover/100000000 as turnover_yi,
                   sb.change_pct, sb.ma5, sb.ma10, sb.ma20, sb.price,
                   sb.industry, sb.turnover_rate,
                   COALESCE(lp.consecutive_boards, 0) as boards
            FROM sector_stocks ss
            JOIN stock_basic sb ON ss.stock_code = sb.stock_code
                AND sb.trade_date = ?
            LEFT JOIN limit_pool lp ON sb.stock_code = lp.stock_code
                AND sb.trade_date = lp.trade_date AND lp.pool_type = '涨停'
            WHERE ss.sector_code IN ({sc_ph})
              AND sb.stock_name NOT LIKE '%ST%'
              AND sb.stock_code NOT LIKE '688%'
              AND sb.volume > 0
        """,
            [trade_date] + sector_codes,
        )

        today_data = {}  # {stock_code: {...}}
        sector_stocks_map = {}  # {sector_code: [stock_code, ...]}
        for row in stock_rows:
            sc = row["sector_code"]
            code = row["stock_code"]
            if sc not in sector_stocks_map:
                sector_stocks_map[sc] = []
            sector_stocks_map[sc].append(code)
            today_data[code] = {
                "sector_code": sc,
                "code": code,
                "name": row["stock_name"],
                "mcap": row["mcap"] or 0,
                "turnover_yi": row["turnover_yi"] or 0,
                "change_pct": row["change_pct"] or 0,
                "ma5": row["ma5"] or 0,
                "ma10": row["ma10"] or 0,
                "ma20": row["ma20"] or 0,
                "price": row["price"] or 0,
                "industry": row["industry"] or "",
                "turnover_rate": row["turnover_rate"] or 0,
                "boards": row["boards"] or 0,
                "avg_turnover_5d": row["turnover_yi"] or 0,  # 兜底用今日
                "stock_5d_return": 0,
                "rel_strength": 0,
            }

        if not today_data:
            return {}

        all_stock_codes = list(today_data.keys())

        # ===== Step 2: 5日历史（成交额 + 涨跌幅）=====
        st_ph = ",".join("?" * len(all_stock_codes))
        dt_ph = ",".join("?" * len(all_dates))
        hist_rows = conn.execute(
            f"""
            SELECT stock_code, trade_date, turnover/100000000 as turnover_yi, change_pct
            FROM stock_basic
            WHERE stock_code IN ({st_ph}) AND trade_date IN ({dt_ph})
            ORDER BY stock_code, trade_date
        """,
            all_stock_codes + all_dates,
        )

        from collections import defaultdict

        stock_hist = defaultdict(lambda: {"turnovers": [], "returns": []})
        for row in hist_rows:
            code = row["stock_code"]
            stock_hist[code]["turnovers"].append(row["turnover_yi"] or 0)
            stock_hist[code]["returns"].append(row["change_pct"] or 0)

        for code, hist in stock_hist.items():
            if code not in today_data:
                continue
            turnovers = hist["turnovers"]
            if turnovers:
                today_data[code]["avg_turnover_5d"] = round(
                    sum(turnovers) / len(turnovers), 2
                )
            returns = hist["returns"]
            if returns:
                today_data[code]["stock_5d_return"] = round(sum(returns), 2)

        # ===== Step 3: 板块 5 日涨跌幅（相对强度基准）=====
        sector_returns = {}
        sector_hist_rows = conn.execute(
            f"""
            SELECT sector_code, trade_date, change_percent
            FROM {sector_table}
            WHERE sector_code IN ({sc_ph}) AND trade_date IN ({dt_ph})
            ORDER BY sector_code, trade_date
        """,
            sector_codes + all_dates,
        )
        sector_hist = defaultdict(list)
        for row in sector_hist_rows:
            sector_hist[row["sector_code"]].append(row["change_percent"] or 0)
        for sc, changes in sector_hist.items():
            sector_returns[sc] = round(sum(changes), 2) if changes else 0

        # ===== Step 4: 四维打分 =====
        for code, d in today_data.items():
            sc = d["sector_code"]

            # 维度1: 市值（板块内排名百分位）
            pass  # 延迟到按板块分组后计算

        # 按板块分组打分
        result = {}
        for sc in sector_codes:
            if sc not in sector_stocks_map:
                continue
            codes = sector_stocks_map[sc]
            stocks = [today_data[c] for c in codes if c in today_data]
            if not stocks:
                continue

            n = len(stocks)

            # --- 市值排名 ---
            by_mcap = sorted(stocks, key=lambda x: x["mcap"], reverse=True)
            mcap_rank = {s["code"]: i for i, s in enumerate(by_mcap)}
            for s in stocks:
                rank = mcap_rank[s["code"]]
                pct = (1 - rank / max(n - 1, 1)) * 100
                # 相对加分：板块内市值前10% +10，前25% +5
                rank_pct = rank / max(n - 1, 1) if n > 1 else 0
                bonus = 10 if rank_pct <= 0.1 else (5 if rank_pct <= 0.25 else 0)
                s["mcap_score"] = min(100, pct * 0.9 + bonus)

            # --- 流动性排名 ---
            by_turnover = sorted(
                stocks, key=lambda x: x["avg_turnover_5d"], reverse=True
            )
            to_rank = {s["code"]: i for i, s in enumerate(by_turnover)}
            for s in stocks:
                rank = to_rank[s["code"]]
                pct = (1 - rank / max(n - 1, 1)) * 100
                rank_pct = rank / max(n - 1, 1) if n > 1 else 0
                bonus = 10 if rank_pct <= 0.1 else (5 if rank_pct <= 0.25 else 0)
                s["turnover_score"] = min(100, pct * 0.9 + bonus)

            # --- 趋势得分 ---
            for s in stocks:
                ma5, ma10, ma20 = s["ma5"], s["ma10"], s["ma20"]
                price = s["price"]
                if ma5 > ma10 > ma20 and all(x > 0 for x in [ma5, ma10, ma20]):
                    s["trend_score"] = 100
                    s["trend_status"] = "full"
                elif ma5 > ma20 and ma5 > 0 and ma20 > 0:
                    s["trend_score"] = 60
                    s["trend_status"] = "half"
                else:
                    s["trend_score"] = 20
                    s["trend_status"] = "none"
                if price > ma5:
                    s["trend_score"] = min(100, s["trend_score"] + 5)

            # --- 相对强度 ---
            sec_ret = sector_returns.get(sc, 0)
            for s in stocks:
                s["rel_strength"] = round(s["stock_5d_return"] - sec_ret, 2)
            by_rel = sorted(stocks, key=lambda x: x["rel_strength"], reverse=True)
            rel_rank = {s["code"]: i for i, s in enumerate(by_rel)}
            for s in stocks:
                rank = rel_rank[s["code"]]
                pct = (1 - rank / max(n - 1, 1)) * 100
                rank_pct = rank / max(n - 1, 1) if n > 1 else 0
                extra = 15 if rank_pct <= 0.1 else (8 if rank_pct <= 0.25 else 0)
                s["rel_score"] = min(100, pct * 0.85 + extra)

            # --- 综合得分 ---
            for s in stocks:
                s["score"] = round(
                    s["mcap_score"] * 0.30
                    + s["turnover_score"] * 0.25
                    + s["trend_score"] * 0.25
                    + s["rel_score"] * 0.20,
                    1,
                )

            # --- 取 top_n ---
            stocks.sort(key=lambda x: x["score"], reverse=True)
            top = stocks[:top_n]
            # 确保至少包含市值最大的 1 只
            top_codes_set = {s["code"] for s in top}
            if by_mcap[0]["code"] not in top_codes_set:
                top[-1] = by_mcap[0]
                top.sort(key=lambda x: x["score"], reverse=True)

            # 构建输出（不修改原 dict，避免跨板块共享污染）
            keep_keys = [
                "code",
                "name",
                "mcap",
                "change_pct",
                "ma5",
                "ma10",
                "ma20",
                "boards",
                "industry",
                "avg_turnover_5d",
                "rel_strength",
                "trend_status",
                "score",
            ]
            clean_top = []
            for s in top:
                clean = {
                    k: s.get(k, 0)
                    if k != "name" and k != "industry" and k != "trend_status"
                    else s.get(k, "")
                    for k in keep_keys
                }
                clean["sector_name"] = sector_names.get(sc, sc)
                clean_top.append(clean)
            result[sc] = clean_top

        return result

    @staticmethod
    def filter_by_sector_trend(
        conn,
        trade_date: str,
        stock_codes: list,
        top_n: int = 5,
        lookback_hot_days: int = 3,
        lookback_resonance_days: int = 5,
        max_fail_days: int = 3,
    ) -> set:
        """
        板块趋势过滤：查每只股票所属概念板块，过滤掉冷门/弱于大盘的板块

        1. 热度过滤：最近 N 天出现在 hot_history 概念 TOP5 至少 1 次
        2. 共振过滤：近 5 日 vs 上证指数 — 大盘涨板块涨更多，大盘跌板块跌更少或收红

        兜底：过滤后 < 5 只，放宽到热度 OR 共振任一通过即可

        Returns:
            通过过滤的 stock_codes set
        """
        if not stock_codes:
            return set()

        from system.config.trading_calendar import get_recent_trading_days

        # 每只股票取涨幅最强的 3 个概念板块
        ph_codes = ",".join("?" * len(stock_codes))
        cursor = conn.execute(
            f"""
            SELECT ss.stock_code, sc.sector_code, sc.sector_name
            FROM sector_stocks ss
            JOIN sector_concept sc ON ss.sector_code = sc.sector_code
            JOIN sector_info si ON sc.sector_code = si.sector_code
            WHERE ss.stock_code IN ({ph_codes}) AND sc.trade_date = ?
              AND si.need_collect = 1
            ORDER BY ss.stock_code, sc.change_percent DESC
        """,
            stock_codes + [trade_date],
        )

        stock_sectors = {}  # {code: [(sector_code, sector_name), ...]}
        for row in cursor.fetchall():
            code = row["stock_code"]
            if code not in stock_sectors:
                stock_sectors[code] = []
            if len(stock_sectors[code]) < 3:
                stock_sectors[code].append((row["sector_code"], row["sector_name"]))

        if not stock_sectors:
            return set()

        all_sectors = {sc for sectors in stock_sectors.values() for sc, _ in sectors}

        # ===== 热度过滤：最近 N 天 TOP5 =====
        hot_sectors = set()
        hot_days = get_recent_trading_days(trade_date, lookback_hot_days)
        if all_sectors and hot_days:
            ph_sc = ",".join("?" * len(all_sectors))
            ph_days = ",".join("?" * len(hot_days))
            rows = conn.execute(
                f"""
                SELECT sector_code FROM sector_hot_history
                WHERE sector_type = 'concept'
                  AND sector_code IN ({ph_sc})
                  AND trade_date IN ({ph_days})
                  AND rank > 0 AND rank <= ?
            """,
                list(all_sectors) + hot_days + [top_n],
            )
            hot_sectors = {row["sector_code"] for row in rows}

        # ===== 共振过滤：近 5 日 vs 上证指数 =====
        resonance_days = (
            get_recent_trading_days(trade_date, lookback_resonance_days) or []
        )

        # 指数涨跌幅
        index_changes = {}
        if resonance_days:
            ph_days = ",".join("?" * len(resonance_days))
            rows = conn.execute(
                f"""
                SELECT trade_date, change_percent FROM index_realtime_data
                WHERE index_code = 'sh000001' AND trade_date IN ({ph_days})
            """,
                resonance_days,
            )
            index_changes = {
                row["trade_date"]: row["change_percent"] or 0 for row in rows
            }

        # 板块涨跌幅
        sector_changes = {}  # {sc: {date: change, ...}}
        if all_sectors and resonance_days:
            ph_sc = ",".join("?" * len(all_sectors))
            ph_days = ",".join("?" * len(resonance_days))
            rows = conn.execute(
                f"""
                SELECT sector_code, trade_date, change_percent
                FROM sector_concept
                WHERE sector_code IN ({ph_sc}) AND trade_date IN ({ph_days})
            """,
                list(all_sectors) + resonance_days,
            )
            for row in rows:
                sc = row["sector_code"]
                if sc not in sector_changes:
                    sector_changes[sc] = {}
                sector_changes[sc][row["trade_date"]] = row["change_percent"] or 0

        resonance_pass = set()
        for sc in all_sectors:
            changes = sector_changes.get(sc, {})
            if not changes:
                continue
            fail_count = 0
            total = 0
            for date in resonance_days:
                idx_chg = index_changes.get(date)
                sec_chg = changes.get(date)
                if idx_chg is None or sec_chg is None:
                    continue
                total += 1
                if idx_chg > 0:
                    if sec_chg <= idx_chg:
                        fail_count += 1
                elif idx_chg < 0:
                    if sec_chg < 0 and sec_chg <= idx_chg:
                        fail_count += 1
                else:
                    if sec_chg <= 0:
                        fail_count += 1
            if total == 0:
                continue
            if fail_count < max_fail_days:
                resonance_pass.add(sc)

        # 指数数据缺失时，默认所有板块共振通过
        if not index_changes:
            resonance_pass = set(all_sectors)

        # ===== 综合判断 =====
        passed = set()
        for code in stock_codes:
            sectors = stock_sectors.get(code, [])
            for sc, _ in sectors:
                if sc in hot_sectors and sc in resonance_pass:
                    passed.add(code)
                    break

        # 兜底：过滤后太少，放宽
        if len(passed) < 5:
            for code in stock_codes:
                if code in passed:
                    continue
                for sc, _ in stock_sectors.get(code, []):
                    if sc in hot_sectors or sc in resonance_pass:
                        passed.add(code)
                        break

        return passed

    @staticmethod
    def enrich_concepts(conn, trade_date: str, stock_codes: list) -> dict:
        """给股票列表补充概念板块（每只取涨幅最高的 2 个概念，仅 need_collect=1）"""
        if not stock_codes:
            return {}
        placeholders = ",".join("?" * len(stock_codes))
        cursor = conn.execute(
            f"""
            SELECT ss.stock_code, sc.sector_name, sc.change_percent
            FROM sector_stocks ss
            JOIN sector_concept sc ON ss.sector_code = sc.sector_code
            JOIN sector_info si ON sc.sector_code = si.sector_code
            WHERE ss.stock_code IN ({placeholders}) AND sc.trade_date = ?
              AND si.need_collect = 1
            ORDER BY ss.stock_code, sc.change_percent DESC
        """,
            stock_codes + [trade_date],
        )
        result = {}
        for row in cursor.fetchall():
            code = row["stock_code"]
            if code not in result:
                result[code] = []
            if len(result[code]) < 2:
                result[code].append(row["sector_name"])
        return result
