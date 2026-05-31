"""
个股行情采集器

功能:
- 从东财采集全市场个股行情数据
- 保存到 stock_basic 表
- 只保留当天数据 (每次 DELETE + INSERT)
- 与复盘任务一起执行

数据源: 东方财富 API
执行时机: 每个交易日盘后 (与复盘任务一起)
"""

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

from data.collectors.proxy.proxy_base_collector import USER_AGENTS, ProxyBaseCollector


class StockBasicCollector(ProxyBaseCollector):
    """个股行情采集器"""

    # 数据库配置
    TABLE_NAME = "stock_basic"

    # API 配置
    API_URL = "https://push2.eastmoney.com/api/qt/clist/get"
    PAGE_SIZE = 100  # 每页 100 条
    MAX_PAGES = 100  # 最多 100 页 (10000 只股票,远超实际需求)
    MAX_RETRIES = 3  # 每页最多重试 3 次
    RETRY_DELAYS = [2, 5, 10]  # 重试延时 (秒)
    FIRST_PAGE_MAX_RETRIES = 5  # 第 1 页多试 2 次，拿到 total 才能分页
    REQUEST_TIMEOUT = 10  # 请求超时 (秒)

    # API 参数 (37 个字段)
    API_PARAMS = {
        "pn": "1",
        "pz": str(PAGE_SIZE),
        "po": "1",
        "np": "1",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:262144",
        "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23,f38,f39,f41,f46,f48,f57,f62,f66,f69,f72,f75,f78,f81,f84,f87,f88,f100,f102,f103,f113,f115,f26",
    }

    # Referer
    REFERER_URL = "https://quote.eastmoney.com/center/boardlist.html"

    # User-Agent 池
    USER_AGENTS = USER_AGENTS

    def __init__(self, trade_date: str = None, task_mgr=None):
        """
        初始化采集器

        Args:
            trade_date: 交易日期 (默认今天)
            task_mgr: 任务状态管理器
        """
        super().__init__(
            logger_name="StockBasicCollector", trade_date=trade_date, task_mgr=task_mgr
        )
        self.logger.info("个股行情采集器初始化完成")
        self.logger.info(f"交易日期:{self.trade_date}")

    def _parse_data(self, data: Dict) -> List[Dict]:
        """
        解析个股行情数据

        Args:
            data: API 返回数据

        Returns:
            股票列表
        """
        stocks = []

        if not data.get("data") or not data["data"].get("diff"):
            return stocks

        for item in data["data"]["diff"]:
            # 辅助函数:安全转换为 float
            def safe_float(value, default=0):
                if value is None or value == "" or value == "-":
                    return default
                try:
                    return float(value)
                except (ValueError, TypeError):
                    return default

            # 上市日期格式化 (YYYYMMDD → YYYY-MM-DD)
            listing_date_raw = item.get("f26", "")
            listing_date = ""
            if listing_date_raw:
                listing_date_str = (
                    str(int(listing_date_raw))
                    if isinstance(listing_date_raw, (int, float))
                    else str(listing_date_raw)
                )
                if len(listing_date_str) == 8 and listing_date_str.isdigit():
                    listing_date = f"{listing_date_str[:4]}-{listing_date_str[4:6]}-{listing_date_str[6:]}"

            stock = {
                "trade_date": self.trade_date,
                "stock_code": item.get("f12", ""),
                "stock_name": item.get("f14", ""),
                # 基础信息 (6 个)
                "industry": item.get("f100", ""),
                "region": item.get("f102", ""),
                "concepts": item.get("f103", ""),
                "listing_date": listing_date,
                "total_shares": safe_float(item.get("f38"), 0),
                "circ_shares": safe_float(item.get("f39"), 0),
                # 市值数据 (2 个)
                "total_market_cap": safe_float(item.get("f20"), 0),
                "circ_market_cap": safe_float(item.get("f21"), 0),
                # 每股指标 (2 个)
                "bps": safe_float(item.get("f113"), 0),
                "undistributed_profit": safe_float(item.get("f48"), 0),
                # 财务指标 (3 个)
                "asset_liability_ratio": safe_float(item.get("f57"), 0),
                "profit_growth": safe_float(item.get("f46"), 0),
                "revenue_growth": safe_float(item.get("f41"), 0),
                # 行情数据 (8 个)
                "price": safe_float(item.get("f2"), 0),
                "change_pct": safe_float(item.get("f3"), 0),
                "change_amount": safe_float(item.get("f4"), 0),
                "prev_close": safe_float(item.get("f18"), 0),
                "open": safe_float(item.get("f17"), 0),
                "high": safe_float(item.get("f15"), 0),
                "low": safe_float(item.get("f16"), 0),
                # 成交数据 (5 个)
                "amplitude": safe_float(item.get("f7"), 0),
                "volume": safe_float(item.get("f5"), 0),  # 手
                "turnover": safe_float(item.get("f6"), 0),  # 元
                "turnover_rate": safe_float(item.get("f8"), 0),
                # 平均股价(计算字段)
                # avg_price = 成交额 / (成交量 × 100)  注意:1 手=100 股
                "avg_price": round(
                    safe_float(item.get("f6"), 0)
                    / (safe_float(item.get("f5"), 0) * 100),
                    2,
                )
                if safe_float(item.get("f5"), 0) > 0
                else 0,
                "volume_ratio": safe_float(item.get("f10"), 0),
                # 资金流向 (9 个)
                "main_force_net": safe_float(item.get("f62"), 0),
                "super_large_net": safe_float(item.get("f66"), 0),
                "large_net": safe_float(item.get("f72"), 0),
                "medium_net": safe_float(item.get("f78"), 0),
                "small_net": safe_float(item.get("f84"), 0),
                "main_force_ratio": safe_float(item.get("f88"), 0),
                "super_large_ratio": safe_float(item.get("f69"), 0),
                "large_ratio": safe_float(item.get("f75"), 0),
                "medium_ratio": safe_float(item.get("f81"), 0),
                "small_ratio": safe_float(item.get("f87"), 0),
                # 估值指标 (3 个)
                "pe_dynamic": safe_float(item.get("f9"), 0),
                "pe_ttm": safe_float(item.get("f115"), 0),
                "pb_ratio": safe_float(item.get("f23"), 0),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            stocks.append(stock)

        return stocks

    def _save_to_db(self, data: list):
        """
        批量保存到数据库（INSERT OR REPLACE，不 DELETE，靠唯一索引防重复）
        """
        if not data or len(data) == 0:
            self.logger.warning("⚠️ 数据为空,跳过保存")
            return

        self.logger.info(f"保存 {len(data)} 只股票到数据库表 {self.TABLE_NAME}...")

        try:
            from system.config.settings import DATABASE_PATH

            trade_date = self.trade_date
            updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            conn = sqlite3.connect(DATABASE_PATH)
            cursor = conn.cursor()

            # 批量插入（唯一索引 idx_stock_basic_date_code 保证不重复）
            insert_data = []
            seen = set()  # 同一批次去重
            for row in data:
                code = row.get("stock_code", "")
                if code in seen:
                    continue
                seen.add(code)
                insert_data.append(
                    (
                        trade_date,
                        code,
                        row.get("stock_name", ""),
                        row.get("price", 0),
                        row.get("change_pct", 0),
                        row.get("change_amount", 0),
                        row.get("volume", 0),
                        row.get("turnover", 0),
                        row.get("amplitude", 0),
                        row.get("turnover_rate", 0),
                        row.get("pe_dynamic", 0),
                        row.get("volume_ratio", 0),
                        row.get("high", 0),
                        row.get("low", 0),
                        row.get("open", 0),
                        row.get("prev_close", 0),
                        row.get("total_market_cap", 0),
                        row.get("circ_market_cap", 0),
                        row.get("pb_ratio", 0),
                        row.get("total_shares", 0),
                        row.get("circ_shares", 0),
                        row.get("revenue_growth", 0),
                        row.get("profit_growth", 0),
                        row.get("asset_liability_ratio", 0),
                        row.get("undistributed_profit", 0),
                        row.get("main_force_net", 0),
                        row.get("super_large_net", 0),
                        row.get("large_net", 0),
                        row.get("medium_net", 0),
                        row.get("small_net", 0),
                        row.get("main_force_ratio", 0),
                        row.get("super_large_ratio", 0),
                        row.get("large_ratio", 0),
                        row.get("medium_ratio", 0),
                        row.get("small_ratio", 0),
                        row.get("pe_ttm", 0),
                        row.get("industry", ""),
                        row.get("region", ""),
                        row.get("concepts", ""),
                        row.get("bps", 0),
                        row.get("listing_date", ""),
                        row.get("avg_price", 0),
                        updated_at,
                    )
                )

            cursor.executemany(
                f"""
                INSERT OR REPLACE INTO {self.TABLE_NAME} (
                    trade_date, stock_code, stock_name,
                    price, change_pct, change_amount,
                    volume, turnover, amplitude, turnover_rate, pe_dynamic, volume_ratio,
                    high, low, open, prev_close,
                    total_market_cap, circ_market_cap,
                    pb_ratio, total_shares, circ_shares,
                    revenue_growth, profit_growth, undistributed_profit, asset_liability_ratio,
                    main_force_net, super_large_net, large_net, medium_net, small_net,
                    main_force_ratio, super_large_ratio, large_ratio, medium_ratio, small_ratio,
                    pe_ttm, industry, region, concepts, bps, listing_date, avg_price,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                insert_data,
            )

            conn.commit()

            # 验证: 今天实际保存了多少只
            actual = conn.execute(
                f"SELECT COUNT(DISTINCT stock_code) FROM {self.TABLE_NAME} WHERE trade_date = ?",
                (trade_date,),
            ).fetchone()[0]
            api_total = self.cache_data.get("total", 0)
            if api_total and actual < api_total * 0.99:
                self.logger.warning(
                    f"⚠️ 覆盖率不足: 已保存 {actual} 只, API total {api_total} 只 ({actual / api_total * 100:.1f}%)"
                )
            else:
                self.logger.info(
                    f"✅ 保存到数据库成功: {actual} 只 (API total: {api_total})"
                )

        except Exception as e:
            if "conn" in locals() and conn:
                conn.rollback()
            self.logger.error(f"❌ 保存到数据库失败：{e}")
            raise

        finally:
            if "conn" in locals() and conn:
                conn.close()

    def _compute_moving_averages(self):
        """计算当日所有个股的 MA5/MA10/MA20/MA5_angle + avg_vol_5d/avg_vol_20d 并更新入库"""
        self.logger.info("计算均线 + 量能均值...")
        import sqlite3 as _sql

        from system.config.settings import DATABASE_PATH

        conn = _sql.connect(DATABASE_PATH)
        try:
            cur = conn.cursor()

            cur.execute(
                """
                SELECT stock_code, trade_date, price, volume
                FROM stock_basic
                WHERE trade_date <= ?
                ORDER BY stock_code, trade_date DESC
            """,
                (self.trade_date,),
            )

            # 按 stock_code 分组，每组按日期降序排列
            prices_by_code = defaultdict(list)
            volumes_by_code = defaultdict(list)
            for row in cur.fetchall():
                code = row[0]
                prices_by_code[code].append(row[2])
                volumes_by_code[code].append(row[3] or 0)

            updates = []
            for code, prices in prices_by_code.items():
                if not prices or prices[0] == 0:
                    continue

                volumes = volumes_by_code.get(code, [])

                ma5 = round(sum(prices[:5]) / min(5, len(prices)), 2)
                ma10 = round(sum(prices[:10]) / min(10, len(prices)), 2)
                ma20 = round(sum(prices[:20]) / min(20, len(prices)), 2)

                avg_vol_5d = round(sum(volumes[:5]) / min(5, len(volumes)), 2)
                avg_vol_20d = round(sum(volumes[:20]) / min(20, len(volumes)), 2)

                # MA5 斜率: 今日 MA5 / 昨日 MA5 - 1
                prev_prices = prices[1:6]
                prev_ma5 = (
                    round(sum(prev_prices) / min(5, len(prev_prices)), 2)
                    if prev_prices
                    else 0
                )
                ma5_angle = round((ma5 / prev_ma5 - 1) * 100, 2) if prev_ma5 > 0 else 0

                updates.append(
                    (
                        ma5,
                        ma10,
                        ma20,
                        ma5_angle,
                        avg_vol_5d,
                        avg_vol_20d,
                        self.trade_date,
                        code,
                    )
                )

            if updates:
                cur.executemany(
                    """
                    UPDATE stock_basic SET ma5 = ?, ma10 = ?, ma20 = ?, ma5_angle = ?,
                        avg_vol_5d = ?, avg_vol_20d = ?
                    WHERE trade_date = ? AND stock_code = ?
                """,
                    updates,
                )
                conn.commit()
                self.logger.info(f"✅ 均线+量能计算完成: {len(updates)} 只个股")
            else:
                self.logger.warning("没有需要计算均线的数据")

        finally:
            conn.close()

    def _compute_indicators(self):
        """计算 MA60/MA120/BBI/MACD/RSI/KDJ，写入 stock_indicators"""
        self.logger.info("计算技术指标 (MA60/120/BBI/MACD/RSI/KDJ/BOLL)...")
        import sqlite3 as _sql

        from analysis.screening.indicators import (
            calc_bollinger,
            calc_kdj,
            calc_macd,
            calc_rsi,
        )
        from system.config.settings import DATABASE_PATH

        conn = _sql.connect(DATABASE_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT stock_code, trade_date, price, open, high, low
                FROM stock_basic
                WHERE trade_date <= ?
                ORDER BY stock_code, trade_date ASC
            """,
                (self.trade_date,),
            )

            # 按 stock_code 分组，日期升序
            from collections import defaultdict as _dd

            data_by_code = _dd(list)
            for row in cur.fetchall():
                code = row[0]
                data_by_code[code].append(
                    {
                        "date": row[1],
                        "close": row[2] or 0,
                        "open": row[3] or 0,
                        "high": row[4] or 0,
                        "low": row[5] or 0,
                    }
                )

            inserts = []
            for code, rows in data_by_code.items():
                closes = [r["close"] for r in rows]
                highs = [r["high"] for r in rows]
                lows = [r["low"] for r in rows]
                n = len(closes)

                if n < 3 or closes[-1] == 0:
                    continue

                # ---- 日线均线 ----
                ma60 = round(sum(closes[-60:]) / min(60, n), 2)
                ma120 = round(sum(closes[-120:]) / min(120, n), 2) if n >= 120 else None

                # ---- 日线 BBI ----
                ma3 = sum(closes[-3:]) / min(3, n)
                ma6 = sum(closes[-6:]) / min(6, n)
                ma12 = sum(closes[-12:]) / min(12, n)
                ma24 = sum(closes[-24:]) / min(24, n)
                bbi_daily = round((ma3 + ma6 + ma12 + ma24) / 4, 2)

                # ---- 周线 BBI ----
                bbi_weekly = self._compute_weekly_bbi(rows)

                # ---- MACD / RSI / KDJ ----
                macd = calc_macd(closes)
                rsi6 = calc_rsi(closes, 6)
                rsi12 = calc_rsi(closes, 12)
                rsi24 = calc_rsi(closes, 24)
                kdj = calc_kdj(highs, lows, closes)
                bb = calc_bollinger(closes)

                inserts.append(
                    (
                        code,
                        self.trade_date,
                        ma60,
                        ma120,
                        bbi_daily,
                        bbi_weekly,
                        macd["dif"],
                        macd["dea"],
                        macd["bar"],
                        rsi6,
                        rsi12,
                        rsi24,
                        kdj["k"],
                        kdj["d"],
                        kdj["j"],
                        bb["upper"],
                        bb["mid"],
                        bb["lower"],
                        bb["width"],
                        bb["pct_b"],
                    )
                )

            if inserts:
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO stock_indicators
                    (stock_code, trade_date,
                     ma60, ma120, bbi_daily, bbi_weekly,
                     macd_dif, macd_dea, macd_bar,
                     rsi6, rsi12, rsi24,
                     kdj_k, kdj_d, kdj_j,
                     bb_upper, bb_mid, bb_lower,
                     bb_width, bb_pct_b)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    inserts,
                )
                conn.commit()
                self.logger.info(f"✅ 技术指标计算完成: {len(inserts)} 只个股")
            else:
                self.logger.warning("没有需要计算技术指标的数据")

        finally:
            conn.close()

    @staticmethod
    def _compute_weekly_bbi(daily_rows: list) -> float | None:
        """从日线合成周K，计算周线 BBI = (MA3+MA6+MA12+MA24)/4"""
        if len(daily_rows) < 25:  # 至少需要 ~5 周数据
            return None

        # 按 ISO 周分组
        weeks: dict[tuple, dict] = {}
        for r in daily_rows:
            if r["close"] == 0:
                continue
            iso = datetime.strptime(r["date"], "%Y-%m-%d").isocalendar()
            key = (iso[0], iso[1])
            if key not in weeks:
                weeks[key] = {
                    "open": r["open"] or r["close"],
                    "close": r["close"],
                    "high": r["high"] or r["close"],
                    "low": r["low"] or r["close"],
                }
            else:
                w = weeks[key]
                w["close"] = r["close"]
                w["high"] = max(w["high"], r["high"] or r["close"])
                w["low"] = min(w["low"], r["low"] or r["close"])

        weekly_closes = [w["close"] for w in weeks.values()]

        n = len(weekly_closes)
        if n < 4:
            return None

        ma3w = sum(weekly_closes[-3:]) / min(3, n)
        ma6w = sum(weekly_closes[-6:]) / min(6, n)
        ma12w = sum(weekly_closes[-12:]) / min(12, n)
        ma24w = sum(weekly_closes[-24:]) / min(24, n)

        return round((ma3w + ma6w + ma12w + ma24w) / 4, 2)

    def collect_all(self):
        """采集全市场个股行情(使用基类的 fetch_all)"""
        self.logger.info("=" * 60)
        self.logger.info("🍎 股票量化系统 - 个股行情采集器")
        self.logger.info("=" * 60)

        # 使用基类的 fetch_all 方法采集原始数据
        raw_data = self.fetch_all()

        # 解析原始数据
        parsed_data = self._parse_data({"data": {"diff": raw_data}}) if raw_data else []

        # 返回数据,由复盘服务统一保存
        return {
            "data": parsed_data,
            "failed_pages": self.cache_data.get("failed_pages", []),
        }

    def fetch_and_save(self) -> Dict:
        """【新方法】采集并保存(一次执行)"""
        self.logger.info("=" * 60)
        self.logger.info(f"🍎 {self.__class__.__name__} 开始采集")
        self.logger.info("=" * 60)

        try:
            # 使用基类的 fetch_all 方法采集原始数据
            raw_data = self.fetch_all()

            # 解析原始数据
            data = self._parse_data({"data": {"diff": raw_data}}) if raw_data else []

            if not data or len(data) == 0:
                self.logger.error("❌ 采集失败:数据为空")
                return {"success": False, "count": 0, "total": 0, "data": []}

            # 保存数据
            self._save_to_db(data)

            # 计算均线 (MA5/MA20/MA5_angle)
            self._compute_moving_averages()

            # 计算技术指标 (MA60/120/BBI/MACD/RSI/KDJ)
            self._compute_indicators()

            # 获取应采集总数
            total = self.cache_data.get("total", len(data))

            result = {"success": True, "count": len(data), "total": total, "data": data}

            self.logger.info(
                f"✅ {self.__class__.__name__} 采集完成:{len(data)}条(应采集{total}条)"
            )
            self.logger.info("=" * 60)
            return result

        except Exception as e:
            self.logger.error(f"❌ {self.__class__.__name__} 采集异常:{e}")
            self.logger.info("=" * 60)
            return {"success": False, "count": 0, "total": 0, "data": []}


# ==================== 测试入口 ====================

if __name__ == "__main__":
    print("=" * 60)
    print("个股行情数据采集器 - 测试运行")
    print("=" * 60)

    try:
        collector = StockBasicCollector()
        result = collector.fetch_and_save()

        if result.get("success"):
            print(f"\n✅ 采集成功:{result['count']}条数据")
        else:
            print("\n❌ 采集失败")

    except Exception as e:
        print(f"\n❌ 执行异常:{e}")
        import traceback

        traceback.print_exc()

    print("=" * 60)
