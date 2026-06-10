"""
个股数据读取器

市场基础数据 — stock_basic / stock_indicators 查询。
跨域共享，所有业务线均可使用。
纯数据查询，不做格式化。
"""

from system.utils.logger import get_system_logger

logger = get_system_logger("data")


class StockReader:
    """个股数据读取器（所有方法均为静态，传入 conn）"""

    @staticmethod
    def get_candidates(conn, trade_date: str) -> list:
        """查询今日异动股（主板 20 + 创业板 20，合并后按涨幅排序）"""

        def _fetch_pool(extra_where: str, limit: int) -> list:
            cursor = conn.execute(
                f"""
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
            """,
                (trade_date,),
            )
            return [dict(row) for row in cursor.fetchall()]

        main_board = _fetch_pool("(s.stock_code LIKE '60%' OR s.stock_code LIKE '00%')", 20)
        gem_board = _fetch_pool("s.stock_code LIKE '30%'", 20)
        candidate_rows = main_board + gem_board
        candidate_rows.sort(key=lambda x: x["change_pct"] or 0, reverse=True)

        candidates = []
        for row in candidate_rows:
            candidates.append(
                {
                    "code": row["stock_code"],
                    "name": row["stock_name"],
                    "change": row["change_pct"] or 0,
                    "mf_net": row["main_force_net"] or 0,
                    "mcap": row["mcap"] or 0,
                    "circ_mcap": row["circ_mcap"] or 0,
                    "sl_wan": row["sl_wan"] or 0,
                    "lg_wan": row["lg_wan"] or 0,
                    "md_wan": row["md_wan"] or 0,
                    "sm_wan": row["sm_wan"] or 0,
                    "mf_ratio": row["main_force_ratio"] or 0,
                    "turnover": row["turnover_rate"] or 0,
                    "vol_ratio": row["volume_ratio"] or 0,
                    "amplitude": row["amplitude"] or 0,
                    "industry": row["industry"] or "",
                    "is_zt": row["limit_type"] == "涨停",
                    "cons_boards": row["cons_boards"] or 0,
                    "lhb_net_yi": row["lhb_net_yi"],
                    "price": row["price"] or 0,
                    "ma5": row["ma5"] or 0,
                    "ma20": row["ma20"] or 0,
                    "ma5_angle": row["ma5_angle"] or 0,
                }
            )
        return candidates

    @staticmethod
    def get_strong_stocks(conn, trade_date: str, sectors: list) -> list:
        """
        查询近期强势股（60日新高+多次涨停，凑够 30 只）
        """
        cursor = conn.execute(
            """
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
        """,
            (trade_date, trade_date),
        )
        strong_stocks = [dict(row) for row in cursor.fetchall()]

        # 不够 30 只，从涨幅靠前板块中补 60 日新高
        if len(strong_stocks) < 30:
            existing_codes = {s["stock_code"] for s in strong_stocks}
            need = 30 - len(strong_stocks)
            top_sector_names = [s["name"] for s in sectors[:10]] if sectors else []
            if top_sector_names:
                s_placeholders = ",".join("?" * len(top_sector_names))
                exc_placeholders = ",".join("?" * len(existing_codes))
                cursor = conn.execute(
                    f"""
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
                           sb.price, sb.ma5, sb.ma10, sb.ma20, sb.ma5_angle
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
                """,
                    [trade_date, trade_date] + top_sector_names + list(existing_codes) + [need],
                )
                supplement = [dict(row) for row in cursor.fetchall()]
                strong_stocks.extend(supplement)

        return strong_stocks

    # ── 盯盘专用查询 ────────────────────

    @staticmethod
    def get_daily_indicators(conn, code: str) -> dict | None:
        """查询个股最新日线技术指标（stock_indicators JOIN stock_basic）。"""
        row = conn.execute(
            """SELECT sb.ma5, sb.ma10, sb.ma20,
                      si.ma60, si.ma120,
                      si.bb_upper, si.bb_mid, si.bb_lower, si.bb_pct_b, si.bb_width,
                      si.macd_dif, si.macd_dea, si.macd_bar,
                      si.kdj_k, si.kdj_d, si.kdj_j,
                      si.rsi6, si.rsi12, si.rsi24,
                      si.bbi_daily, si.bbi_weekly
               FROM stock_indicators si
               JOIN stock_basic sb ON si.stock_code=sb.stock_code AND si.trade_date=sb.trade_date
               WHERE si.stock_code=?
               ORDER BY si.trade_date DESC LIMIT 1""",
            (code,),
        ).fetchone()
        if not row:
            return None
        return {
            "ma5": row[0] or 0,
            "ma10": row[1] or 0,
            "ma20": row[2] or 0,
            "ma60": row[3] or 0,
            "ma120": row[4] or 0,
            "bb_upper": row[5] or 0,
            "bb_mid": row[6] or 0,
            "bb_lower": row[7] or 0,
            "bb_pct_b": row[8],
            "bb_width": row[9] or 0,
            "macd_dif": row[10] or 0,
            "macd_dea": row[11] or 0,
            "macd_bar": row[12] or 0,
            "kdj_k": row[13] or 50,
            "kdj_d": row[14] or 50,
            "kdj_j": row[15] or 50,
            "rsi6": row[16] or 50,
            "rsi12": row[17] or 50,
            "rsi24": row[18] or 50,
            "bbi_daily": row[19] or 0,
            "bbi_weekly": row[20] or 0,
        }

    @staticmethod
    def get_money_flow(conn, code: str) -> dict | None:
        """查询个股最新主力资金流向（stock_basic 表）。"""
        row = conn.execute(
            """SELECT main_force_net, main_force_ratio,
                      super_large_net, large_net,
                      ma5_angle, pe_dynamic, circ_market_cap
               FROM stock_basic WHERE stock_code=?
               ORDER BY trade_date DESC LIMIT 1""",
            (code,),
        ).fetchone()
        if not row:
            return None
        return {
            "main_force_net": row[0] or 0,
            "main_force_ratio": row[1] or 0,
            "super_large_net": row[2] or 0,
            "large_net": row[3] or 0,
            "ma5_angle": row[4] or 0,
            "pe_dynamic": row[5] or 0,
            "circ_market_cap": row[6] or 0,
        }

    @staticmethod
    def get_money_flow_trend(conn, code: str, days: int = 5) -> dict:
        """查询个股最近 N 个交易日的主力资金流趋势。
        返回 {consecutive_buy: 连续净买天数, amounts: [每日净买额],
              trend_score: 趋势评分(-10~10), trend_strength: 强弱描述}。
        """
        rows = conn.execute(
            """SELECT main_force_net, main_force_ratio, trade_date
               FROM stock_basic WHERE stock_code=?
               ORDER BY trade_date DESC LIMIT ?""",
            (code, days),
        ).fetchall()
        if not rows:
            return {"consecutive_buy": 0, "amounts": [], "trend_score": 0, "trend_strength": "无数据"}

        amounts = [r[0] or 0 for r in rows]  # 最近的在前面
        ratios = [r[1] or 0 for r in rows]

        # 连续净买天数（从最近往前数）
        consecutive = 0
        for amt in amounts:
            if amt > 0:
                consecutive += 1
            else:
                break

        # 趋势评分：连续净买 + 金额递增 + 占比为正
        score = 0
        if consecutive >= 3:
            score += 5
        elif consecutive >= 2:
            score += 2
        elif consecutive == 1:
            score += 1

        # 金额递增检查
        if len(amounts) >= 3 and amounts[0] > amounts[1] > amounts[2] > 0:
            score += 4  # 连续3天递增且均为净买
        elif len(amounts) >= 2 and amounts[0] > amounts[1] > 0:
            score += 2  # 连续2天递增

        # 最近一天净买占比大
        if ratios and ratios[0] > 0.05:  # 主力净买 > 5%
            score += 1

        # 累计净买
        total_net = sum(amounts)
        if total_net > 0:
            score += min(2, int(total_net / 100_000_000))  # 每1亿加1分，最多2分

        # 描述
        if score >= 8:
            strength = "强势吸筹"
        elif score >= 5:
            strength = "温和流入"
        elif score >= 2:
            strength = "小幅流入"
        elif score >= 0:
            strength = "资金平衡"
        elif score >= -3:
            strength = "小幅流出"
        else:
            strength = "持续流出"

        return {
            "consecutive_buy": consecutive,
            "amounts": amounts,
            "total_net": total_net,
            "trend_score": score,
            "trend_strength": strength,
        }

    @staticmethod
    def get_volatility_breakout(conn, code: str, lookback: int = 20) -> dict:
        """波动率异动检测：当前振幅 vs 历史均值。
        返回 {is_breakout, vol_ratio, current_amp, avg_amp, signal}。
        vol_ratio > 2.0 → 强烈异动，1.5-2.0 → 温和异动。
        """
        rows = conn.execute(
            """SELECT amplitude FROM stock_basic WHERE stock_code=?
               ORDER BY trade_date DESC LIMIT ?""",
            (code, lookback),
        ).fetchall()
        if len(rows) < 5:
            return {"is_breakout": False, "vol_ratio": 1.0, "current_amp": 0, "avg_amp": 0, "signal": "数据不足"}

        amps = [r[0] or 0 for r in rows]
        current = amps[0]
        avg = sum(amps[1:]) / len(amps[1:]) if len(amps) > 1 else current
        ratio = current / avg if avg > 0 else 1.0

        if ratio > 3.0:
            signal = "极端异动"
        elif ratio > 2.0:
            signal = "强烈异动"
        elif ratio > 1.5:
            signal = "温和异动"
        elif ratio > 1.2:
            signal = "小幅放大"
        else:
            signal = "正常"

        return {
            "is_breakout": ratio >= 1.5,
            "vol_ratio": round(ratio, 2),
            "current_amp": round(current, 2),
            "avg_amp": round(avg, 2),
            "signal": signal,
        }

    @staticmethod
    def get_stock_basic(conn, code: str) -> dict | None:
        """查询个股基础信息。"""
        row = conn.execute(
            """SELECT trade_date, stock_code, stock_name, price, open, high, low,
                      prev_close, change_pct, total_market_cap, circ_market_cap,
                      turnover_rate, volume_ratio, amplitude, volume,
                      ma5, ma10, ma20, ma5_angle, industry, concepts,
                      main_force_net, main_force_ratio,
                      super_large_net, large_net, medium_net, small_net,
                      avg_vol_5d, avg_vol_20d,
                      pe_ttm, pb_ratio, revenue_growth, profit_growth
               FROM stock_basic WHERE stock_code=?
               ORDER BY trade_date DESC LIMIT 1""",
            (code,),
        ).fetchone()
        if not row:
            return None
        return dict(row)

    @staticmethod
    def get_recent_prices(conn, code: str, limit: int = 5) -> list:
        """查询近期收盘价列表。"""
        rows = conn.execute(
            "SELECT price FROM stock_basic WHERE stock_code=? ORDER BY trade_date DESC LIMIT ?",
            (code, limit),
        ).fetchall()
        return [row["price"] for row in rows]

    @staticmethod
    def get_stock_name(conn, code: str) -> str | None:
        """查询股票名称。"""
        row = conn.execute(
            "SELECT stock_name FROM stock_basic WHERE stock_code=? LIMIT 1",
            (code,),
        ).fetchone()
        return row["stock_name"] if row else None

    @staticmethod
    def get_stock_basic_batch(conn, trade_date: str, codes: list[str]) -> dict[str, dict]:
        """批量查询 stock_basic，返回 {code: row_dict}。"""
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"""SELECT stock_code, stock_name, price, change_pct, total_market_cap,
                       circ_market_cap, turnover_rate, volume_ratio,
                       ma5, ma10, ma20, ma5_angle, industry,
                       main_force_net, main_force_ratio
                FROM stock_basic
                WHERE trade_date=? AND stock_code IN ({placeholders})""",
            [trade_date] + list(codes),
        ).fetchall()
        return {
            r[0]: dict(
                zip(
                    [
                        "code",
                        "name",
                        "price",
                        "change_pct",
                        "mcap",
                        "circ_mcap",
                        "turnover_rate",
                        "volume_ratio",
                        "ma5",
                        "ma10",
                        "ma20",
                        "ma5_angle",
                        "industry",
                        "mf_net",
                        "mf_ratio",
                    ],
                    r,
                )
            )
            for r in rows
        }

    @staticmethod
    def get_latest_stock_basic_batch(conn, codes: list[str]) -> dict[str, dict]:
        """批量查询最新 stock_basic（MAX trade_date），返回 {code: row_dict}。"""
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        rows = conn.execute(
            f"""SELECT stock_code, stock_name, price, change_pct, total_market_cap,
                       circ_market_cap, turnover_rate, volume_ratio,
                       ma5, ma10, ma20, ma5_angle, industry,
                       main_force_net, main_force_ratio
                FROM stock_basic
                WHERE trade_date=(SELECT MAX(trade_date) FROM stock_basic)
                  AND stock_code IN ({placeholders})""",
            list(codes),
        ).fetchall()
        return {
            r[0]: dict(
                zip(
                    [
                        "code",
                        "name",
                        "price",
                        "change_pct",
                        "mcap",
                        "circ_mcap",
                        "turnover_rate",
                        "volume_ratio",
                        "ma5",
                        "ma10",
                        "ma20",
                        "ma5_angle",
                        "industry",
                        "mf_net",
                        "mf_ratio",
                    ],
                    r,
                )
            )
            for r in rows
        }

    @staticmethod
    def get_support_resistance(conn, code: str, price: float) -> dict:
        """查询个股最近支撑/阻力位。

        Returns:
            {"supports": [(price, label), ...], "resistances": [(price, label), ...]}
        """
        row = conn.execute(
            """SELECT bb_upper, bb_mid, bb_lower, ma20, ma60, bbi_daily
               FROM stock_indicators WHERE stock_code=?
               ORDER BY trade_date DESC LIMIT 1""",
            (code,),
        ).fetchone()

        supports = []
        resistances = []
        if row:
            bb_upper, bb_mid, bb_lower, ma20, ma60, bbi = row
            for label, val in [
                ("布林上轨", bb_upper),
                ("布林中轨", bb_mid),
                ("MA20", ma20),
                ("MA60", ma60),
                ("BBI", bbi),
            ]:
                if val and val > price * 1.005:
                    resistances.append((val, label))
            for label, val in [
                ("布林下轨", bb_lower),
                ("布林中轨", bb_mid),
                ("MA20", ma20),
                ("MA60", ma60),
                ("BBI", bbi),
            ]:
                if val and val < price * 0.995:
                    supports.append((val, label))

        supports.sort(key=lambda x: x[0], reverse=True)
        resistances.sort(key=lambda x: x[0])
        return {"supports": supports, "resistances": resistances}

    @staticmethod
    def get_trend_stocks(conn, trade_date: str) -> dict:
        """
        双模式趋势票筛选：

        5日线强趋势 (strong): 沿MA5陡峭爬升
        20日线稳健趋势 (normal): 沿MA20稳健上行

        Returns:
            {'strong': [...], 'normal': [...]} 各 TOP10
        """
        cursor = conn.execute(
            """
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
        """,
            (trade_date,),
        )

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
            industry = row[11] or ""
            price = row[12] or 0
            mf_wan = row[13] or 0
            mf_ratio = row[14] or 0

            record = {
                "stock_code": code,
                "stock_name": name,
                "change_pct": change_pct,
                "mcap": mcap,
                "circ_mcap": circ_mcap,
                "turnover_rate": turnover_rate,
                "volume_ratio": volume_ratio,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20,
                "ma5_angle": ma5_angle,
                "industry": industry,
                "price": price,
                "mf_wan": mf_wan,
                "mf_ratio": mf_ratio,
            }

            bias_ma5 = (price - ma5) / ma5 if ma5 > 0 else 999
            spread_5_20 = (ma5 - ma20) / ma20 if ma20 > 0 else 0
            is_strong = price > ma5 and ma5 > ma10 > ma20 and bias_ma5 < 0.05 and spread_5_20 > 0.03

            if is_strong:
                slope_score = min(40 + spread_5_20 * 100, 100)
                record["mode"] = "strong"
                record["score"] = round(slope_score, 1)
                record["bias_ma5"] = round(bias_ma5 * 100, 2)
                strong.append(record)
                strong_codes.add(code)
                continue

            bias_ma20 = (price - ma20) / ma20 if ma20 > 0 else 999
            is_normal = price > ma20 and bias_ma20 < 0.10 and ma5_angle > 0

            if is_normal:
                dev_pct = bias_ma20 * 100
                normal_score = 60 + (20 - dev_pct) * 0.5
                normal_score = min(max(normal_score, 50), 90)
                record["mode"] = "normal"
                record["score"] = round(normal_score, 1)
                record["bias_ma20"] = round(dev_pct, 2)
                normal.append(record)

            if len(strong) >= 10 and len(normal) >= 10:
                break

        strong.sort(key=lambda x: x["score"], reverse=True)
        normal.sort(key=lambda x: x["score"], reverse=True)

        # 板块趋势过滤
        all_codes = [r["stock_code"] for r in strong[:10]] + [r["stock_code"] for r in normal[:10]]
        if all_codes:
            from data.market.sector_data import SectorReader

            passed = SectorReader.filter_by_sector_trend(conn, trade_date, all_codes)
            strong = [r for r in strong if r["stock_code"] in passed][:10]
            normal = [r for r in normal if r["stock_code"] in passed][:10]

        return {
            "strong": strong,
            "normal": normal,
        }
