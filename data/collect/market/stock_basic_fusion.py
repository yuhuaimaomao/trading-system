"""
个股行情融合采集器

数据源:
  1. 腾讯 qt.gtimg.cn    → 基础 OHLCV + 市值/PE/PB/换手率/量比/振幅 (全市场, ~2s)
  2. 同花顺 资金流向       → 主力净流入                                (全市场, ~15s)
     ↓ 挂了降级
     腾讯 getBoardRankList → 主力净流入                               (仅 4597, 缺科创/北交)

目标表: stock_basic_fusion (临时, 跑几天观察稳定性后再替换 stock_basic)
"""

import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime

# ⚠️ 必须在任何网络库导入前清除代理
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
os.environ["no_proxy"] = "*"

from system.utils.dns_bypass import install as dns_bypass_install

dns_bypass_install()

import requests  # noqa: E402

from system.config.settings import DATABASE_PATH  # noqa: E402
from system.utils.logger import get_collect_logger  # noqa: E402


class StockBasicFusionCollector:
    """个股行情融合采集器 — 腾讯主 + 同花顺资金"""

    TABLE_NAME = "stock_basic_fusion"

    # ===== 腾讯 qt API =====
    QT_URL = "http://qt.gtimg.cn/q="
    QT_BATCH_SIZE = 800  # 每批最多代码数
    QT_TOTAL_ESTIMATE = 5800  # 估算全市场数量, 用于分页终止判断

    # ===== 腾讯 board API (主力资金降级) =====
    BOARD_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"

    # ===== qt字段索引 =====
    # 格式: v_CODE="1~名称~纯代码~最新价~昨收~今开~成交量~..."
    QT_MARKET = 0  # 1=沪 51=深 62=北交
    QT_NAME = 1
    QT_PURE_CODE = 2
    QT_PRICE = 3
    QT_PREV_CLOSE = 4
    QT_OPEN = 5
    QT_VOLUME = 6  # 手
    QT_HIGH = 33
    QT_LOW = 34
    QT_CHANGE_AMOUNT = 31
    QT_CHANGE_PCT = 32
    QT_TURNOVER = 37  # 万元
    QT_TURNOVER_RATE = 38
    QT_PE_TTM = 39
    QT_AMPLITUDE = 43
    QT_CIRC_MCAP = 44  # 亿元
    QT_TOTAL_MCAP = 45  # 亿元
    QT_PB = 46
    QT_VOLUME_RATIO = 49
    QT_STOCK_TYPE = 61
    QT_SPEED = 80
    QT_YTD = 62
    QT_D5 = 63
    QT_D10 = 69
    QT_D20 = 70
    QT_D60 = 71
    QT_W52 = 79

    def __init__(self, trade_date: str = None):
        self.trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        self.logger = get_collect_logger("market")
        self.logger.info(f"融合采集器初始化: trade_date={self.trade_date}")

    # ============================================================
    # 主流程
    # ============================================================

    def fetch_and_save(self) -> dict:
        """主入口: 采集 → 融合 → 保存 → 计算均线"""
        t0 = time.time()

        # Step 1: 腾讯 qt → 全量基础数据
        qt_data = self._fetch_tencent_qt_all()
        if not qt_data:
            self.logger.error("❌ 腾讯 qt 数据为空, 中止")
            return {"success": False, "error": "腾讯 qt 返回空"}

        # Step 2: 主力资金 (同花顺 → 降级腾讯board)
        fund_data = self._fetch_fund_flow()

        # Step 3: 行业 (从现有 stock_basic 表取最新)
        industry_map = self._load_industry()

        # Step 4: 融合
        merged = self._merge(qt_data, fund_data, industry_map)

        # Step 5: 保存
        self._save_to_db(merged)

        # Step 6: 计算均线
        self._compute_moving_averages()

        elapsed = time.time() - t0
        self.logger.info(f"✅ 融合采集完成: {len(merged)} 只, 耗时 {elapsed:.1f}s")
        return {"success": True, "count": len(merged), "elapsed": elapsed}

    # ============================================================
    # Step 1: 腾讯 qt.gtimg.cn 全量数据
    # ============================================================

    def _fetch_tencent_qt_all(self) -> dict:
        """全量拉腾讯 qt (需先知道所有代码 → 从 Sina 拿代码列表, 再批量查 qt)"""
        self.logger.info("📡 Step 1/3: 腾讯 qt.gtimg.cn 全量...")
        t0 = time.time()

        # 先通过 Sina 拿全量代码列表 (快, 24s 但只拉代码)
        codes = self._fetch_code_list()
        if not codes:
            self.logger.error("❌ 无法获取代码列表")
            return {}

        # 批量查 qt
        result = {}
        for i in range(0, len(codes), self.QT_BATCH_SIZE):
            batch = codes[i : i + self.QT_BATCH_SIZE]
            qt_codes = [self._to_qt_code(c) for c in batch]
            try:
                r = requests.get(self.QT_URL + ",".join(qt_codes), timeout=15)
                for line in r.text.strip().split("\n"):
                    m = re.match(r'v_\w+="(.+)"', line)
                    if not m:
                        continue
                    f = m.group(1).split("~")
                    if len(f) < 50:
                        continue
                    code = f[self.QT_PURE_CODE]
                    if not code:
                        continue
                    result[code] = {
                        "stock_code": code,
                        "stock_name": f[self.QT_NAME],
                        "price": self._f(f[self.QT_PRICE]),
                        "open": self._f(f[self.QT_OPEN]),
                        "high": self._f(f[self.QT_HIGH]),
                        "low": self._f(f[self.QT_LOW]),
                        "prev_close": self._f(f[self.QT_PREV_CLOSE]),
                        "change_pct": self._f(f[self.QT_CHANGE_PCT]),
                        "change_amount": self._f(f[self.QT_CHANGE_AMOUNT]),
                        "volume": self._f(f[self.QT_VOLUME]),  # 手
                        "turnover": self._f(f[self.QT_TURNOVER]) * 10000,  # 万→元
                        "turnover_rate": self._f(f[self.QT_TURNOVER_RATE]),
                        "pe_ttm": self._f(f[self.QT_PE_TTM]),
                        "pb_ratio": self._f(f[self.QT_PB]),
                        "amplitude": self._f(f[self.QT_AMPLITUDE]),
                        "total_market_cap": self._f(f[self.QT_TOTAL_MCAP]) * 100_000_000,  # 亿→元
                        "circ_market_cap": self._f(f[self.QT_CIRC_MCAP]) * 100_000_000,
                        "volume_ratio": self._f(f[self.QT_VOLUME_RATIO]),
                        "stock_type": f[self.QT_STOCK_TYPE] if len(f) > self.QT_STOCK_TYPE else "",
                    }
            except Exception as e:
                self.logger.warning(f"腾讯 qt 批次 {i} 失败: {e}")
                continue

        self.logger.info(f"  腾讯 qt: {len(result)}/{len(codes)} 只, 耗时 {time.time() - t0:.1f}s")
        return result

    def _fetch_code_list(self) -> list:
        """获取全市场代码列表"""
        self.logger.info("  获取代码列表...")
        t0 = time.time()

        # 方法1: 从 own stock_basic 表取最近一天的代码
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT DISTINCT stock_code FROM stock_basic "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)"
            )
            codes = [r[0] for r in cur.fetchall()]
            conn.close()
            if len(codes) > 4000:
                self.logger.info(f"    从现有 stock_basic 获取: {len(codes)} 只, 耗时 {time.time() - t0:.1f}s")
                return codes
        except Exception:
            pass

        # 方法2: 从 stock_basic_fusion 历史取
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            cur = conn.cursor()
            cur.execute(
                f"SELECT DISTINCT stock_code FROM {self.TABLE_NAME} "
                f"WHERE trade_date = (SELECT MAX(trade_date) FROM {self.TABLE_NAME})"
            )
            codes = [r[0] for r in cur.fetchall()]
            conn.close()
            if len(codes) > 4000:
                self.logger.info(f"    从 {self.TABLE_NAME} 获取: {len(codes)} 只, 耗时 {time.time() - t0:.1f}s")
                return codes
        except Exception:
            pass

        # 方法3: Sina (最后手段)
        return self._fetch_code_list_sina()

    def _fetch_code_list_sina(self) -> list:
        """从 Sina 拉代码列表 (仅代码, 不拉数据)"""
        self.logger.info("    从 Sina 获取代码列表...")
        try:
            import akshare as ak

            df = ak.stock_zh_a_spot()
            codes = []
            for _, row in df.iterrows():
                c = str(row.get("代码", ""))
                pure = c[2:] if len(c) > 2 and c[:2] in ("sh", "sz", "bj") else c
                if pure:
                    codes.append(pure)
            self.logger.info(f"    Sina 代码列表: {len(codes)} 只")
            return codes
        except Exception as e:
            self.logger.error(f"    Sina 代码列表失败: {e}")
            # 硬编码常见区间
            codes = []
            for i in range(600000, 610000):
                codes.append(str(i))
            for i in range(0, 310000):
                codes.append(f"{i:06d}")
            for i in range(920000, 930000):
                codes.append(str(i))
            self.logger.warning(f"    使用硬编码代码范围: {len(codes)} 个")
            return codes

    # ============================================================
    # Step 2: 主力资金 (同花顺 → 降级腾讯board)
    # ============================================================

    def _fetch_fund_flow(self) -> dict:
        """主力资金, 返回 {code: {main_force_net, main_force_ratio}}"""
        self.logger.info("📡 Step 2/3: 主力资金...")
        t0 = time.time()

        # 先试同花顺
        result = self._fetch_fund_flow_ths()
        if result and len(result) > 1000:
            self.logger.info(f"  同花顺: {len(result)} 只, 耗时 {time.time() - t0:.1f}s")
            return result

        # 降级腾讯board
        self.logger.warning("  同花顺失败, 降级到腾讯 getBoardRankList (缺科创/北交)")
        result = self._fetch_fund_flow_tencent_board()
        self.logger.info(f"  腾讯board: {len(result)} 只, 耗时 {time.time() - t0:.1f}s")
        return result

    def _fetch_fund_flow_ths(self) -> dict:
        """同花顺个股资金流"""
        try:
            import akshare as ak

            df = ak.stock_fund_flow_individual(symbol="即时")
            result = {}
            for _, row in df.iterrows():
                code = str(row.get("股票代码", "")).strip()
                if not code:
                    continue
                net_val = self._parse_cn_money(str(row.get("净额", "0")))
                if net_val is None:
                    continue
                turnover_val = self._parse_cn_money(str(row.get("成交额", "0")))
                ratio = round(net_val / turnover_val * 100, 2) if turnover_val and turnover_val > 0 else 0
                result[code] = {
                    "main_force_net": net_val,
                    "main_force_ratio": ratio,
                }
            return result
        except Exception as e:
            self.logger.warning(f"同花顺资金流失败: {e}")
            return {}

    def _fetch_fund_flow_tencent_board(self) -> dict:
        """腾讯 getBoardRankList 主力资金 (仅 4597 只)"""
        result = {}
        try:
            params = {
                "board_code": "aStock",
                "sort_type": "price",
                "direct": "down",
                "count": "200",
            }
            for offset in range(0, 6000, 200):
                params["offset"] = str(offset)
                r = requests.get(self.BOARD_URL, params=params, timeout=15)
                data = r.json()
                if "data" not in data:
                    break
                items = data["data"]["rank_list"]
                if not items:
                    break
                for item in items:
                    code = item.get("code", "")
                    pure_code = code[2:] if len(code) > 2 else code
                    zljlr = float(item.get("zljlr", 0) or 0)  # 万元
                    turnover = float(item.get("turnover", 0) or 0)  # 万元
                    ratio = round(zljlr / turnover * 100, 2) if turnover > 0 else 0
                    result[pure_code] = {
                        "main_force_net": zljlr * 10000,  # 万→元
                        "main_force_ratio": ratio,
                    }
        except Exception as e:
            self.logger.warning(f"腾讯board失败: {e}")
        return result

    # ============================================================
    # Step 3: 行业 (从现有表补充)
    # ============================================================

    def _load_industry(self) -> dict:
        """从 stock_basic 最新日期取行业, 返回 {code: industry}"""
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            cur = conn.cursor()
            cur.execute(
                "SELECT stock_code, industry FROM stock_basic "
                "WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic) "
                "AND industry IS NOT NULL AND industry != ''"
            )
            result = {row[0]: row[1] for row in cur.fetchall()}
            conn.close()
            self.logger.info(f"  行业映射: {len(result)} 只")
            return result
        except Exception as e:
            self.logger.warning(f"加载行业失败: {e}")
            return {}

    # ============================================================
    # Step 4: 数据融合
    # ============================================================

    def _merge(self, qt: dict, fund: dict, industry: dict) -> list:
        """融合二源数据"""
        self.logger.info("🔗 Step 3/3: 数据融合...")
        result = []

        for code, q in qt.items():
            row = dict(q)

            # 主力资金
            fund_row = fund.get(code, {})
            row["main_force_net"] = fund_row.get("main_force_net", 0)
            row["main_force_ratio"] = fund_row.get("main_force_ratio", 0)

            # 四档资金拆分无法获取, 置0
            row["super_large_net"] = 0
            row["large_net"] = 0
            row["medium_net"] = 0
            row["small_net"] = 0
            row["super_large_ratio"] = 0
            row["large_ratio"] = 0
            row["medium_ratio"] = 0
            row["small_ratio"] = 0

            # 行业
            row["industry"] = industry.get(code, "")

            # 均价 = 成交额 / (成交量 * 100)
            if row["volume"] > 0 and row["turnover"] > 0:
                row["avg_price"] = round(row["turnover"] / (row["volume"] * 100), 2)
            else:
                row["avg_price"] = 0

            # 东财特有字段, 新源无法获取
            row["pe_dynamic"] = 0
            row["total_shares"] = 0
            row["circ_shares"] = 0
            row["revenue_growth"] = 0
            row["profit_growth"] = 0
            row["undistributed_profit"] = 0
            row["asset_liability_ratio"] = 0
            row["region"] = ""
            row["concepts"] = ""
            row["bps"] = 0
            row["listing_date"] = ""

            row["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row["trade_date"] = self.trade_date

            result.append(row)

        self.logger.info(f"  融合完成: {len(result)} 只")
        return result

    # ============================================================
    # Step 5: 保存到数据库
    # ============================================================

    def _save_to_db(self, data: list):
        """批量保存到 stock_basic_fusion"""
        if not data:
            self.logger.warning("⚠️ 数据为空,跳过保存")
            return

        self.logger.info(f"💾 保存 {len(data)} 只到 {self.TABLE_NAME}...")

        conn = sqlite3.connect(DATABASE_PATH)
        try:
            cur = conn.cursor()
            self._ensure_table(cur)

            # 列顺序严格对齐
            cols = [
                "trade_date",
                "stock_code",
                "stock_name",
                "price",
                "change_pct",
                "change_amount",
                "volume",
                "turnover",
                "amplitude",
                "turnover_rate",
                "pe_dynamic",
                "volume_ratio",
                "high",
                "low",
                "open",
                "prev_close",
                "total_market_cap",
                "circ_market_cap",
                "pb_ratio",
                "total_shares",
                "circ_shares",
                "revenue_growth",
                "profit_growth",
                "undistributed_profit",
                "asset_liability_ratio",
                "main_force_net",
                "super_large_net",
                "large_net",
                "medium_net",
                "small_net",
                "main_force_ratio",
                "super_large_ratio",
                "large_ratio",
                "medium_ratio",
                "small_ratio",
                "pe_ttm",
                "industry",
                "region",
                "concepts",
                "bps",
                "listing_date",
                "avg_price",
                "updated_at",
            ]

            insert_data = []
            seen = set()
            for row in data:
                code = row.get("stock_code", "")
                if code in seen:
                    continue
                seen.add(code)
                values = []
                for col in cols:
                    if col == "trade_date":
                        values.append(self.trade_date)
                    elif col == "stock_code":
                        values.append(code)
                    elif col == "updated_at":
                        values.append(row.get("updated_at", ""))
                    else:
                        val = row.get(col, 0)
                        values.append(val if val is not None else 0)
                insert_data.append(tuple(values))

            placeholders = ",".join(["?"] * len(cols))
            col_str = ",".join(cols)
            cur.executemany(
                f"INSERT OR REPLACE INTO {self.TABLE_NAME} ({col_str}) VALUES ({placeholders})",
                insert_data,
            )

            conn.commit()
            actual = cur.execute(
                f"SELECT COUNT(*) FROM {self.TABLE_NAME} WHERE trade_date = ?",
                (self.trade_date,),
            ).fetchone()[0]
            self.logger.info(f"✅ 保存成功: {actual} 只")
        except Exception as e:
            conn.rollback()
            self.logger.error(f"❌ 保存失败: {e}")
            raise
        finally:
            conn.close()

    def _ensure_table(self, cur):
        """创建临时表 (与 stock_basic 同结构)"""
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT,
                price REAL,
                change_pct REAL,
                change_amount REAL,
                volume REAL,
                turnover REAL,
                amplitude REAL,
                turnover_rate REAL,
                pe_dynamic REAL,
                volume_ratio REAL,
                high REAL,
                low REAL,
                open REAL,
                prev_close REAL,
                total_market_cap REAL,
                circ_market_cap REAL,
                pb_ratio REAL,
                total_shares REAL,
                circ_shares REAL,
                revenue_growth REAL,
                profit_growth REAL,
                undistributed_profit REAL,
                asset_liability_ratio REAL,
                main_force_net REAL,
                super_large_net REAL,
                large_net REAL,
                medium_net REAL,
                small_net REAL,
                main_force_ratio REAL,
                super_large_ratio REAL,
                large_ratio REAL,
                medium_ratio REAL,
                small_ratio REAL,
                pe_ttm REAL,
                industry TEXT,
                region TEXT,
                concepts TEXT,
                bps REAL,
                listing_date TEXT,
                updated_at TEXT,
                avg_price REAL,
                ma5 REAL,
                ma20 REAL,
                ma5_angle REAL,
                ma10 REAL,
                avg_vol_5d REAL,
                avg_vol_20d REAL
            )
        """)
        cur.execute(f"""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_{self.TABLE_NAME}_date_code
            ON {self.TABLE_NAME}(trade_date, stock_code)
        """)

    # ============================================================
    # Step 6: 均线计算
    # ============================================================

    def _compute_moving_averages(self):
        """计算 MA5/MA10/MA20/MA5_angle + avg_vol_5d/avg_vol_20d"""
        self.logger.info("📐 计算均线 + 量能均值...")
        conn = sqlite3.connect(DATABASE_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT stock_code, trade_date, price, volume FROM {self.TABLE_NAME} "
                "WHERE trade_date <= ? ORDER BY stock_code, trade_date DESC",
                (self.trade_date,),
            )

            prices_by_code = defaultdict(list)
            volumes_by_code = defaultdict(list)
            for row in cur.fetchall():
                prices_by_code[row[0]].append(row[2] or 0)
                volumes_by_code[row[0]].append(row[3] or 0)

            updates = []
            for code, prices in prices_by_code.items():
                if not prices or prices[0] == 0:
                    continue
                vols = volumes_by_code.get(code, [])

                ma5 = round(sum(prices[:5]) / min(5, len(prices)), 2)
                ma10 = round(sum(prices[:10]) / min(10, len(prices)), 2)
                ma20 = round(sum(prices[:20]) / min(20, len(prices)), 2)
                avg_vol_5d = round(sum(vols[:5]) / min(5, len(vols)), 2)
                avg_vol_20d = round(sum(vols[:20]) / min(20, len(vols)), 2)

                prev_prices = prices[1:6]
                prev_ma5 = round(sum(prev_prices) / min(5, len(prev_prices)), 2) if prev_prices else 0
                ma5_angle = round((ma5 / prev_ma5 - 1) * 100, 2) if prev_ma5 > 0 else 0

                updates.append((ma5, ma10, ma20, ma5_angle, avg_vol_5d, avg_vol_20d, self.trade_date, code))

            if updates:
                cur.executemany(
                    f"UPDATE {self.TABLE_NAME} SET ma5=?, ma10=?, ma20=?, ma5_angle=?, "
                    "avg_vol_5d=?, avg_vol_20d=? WHERE trade_date=? AND stock_code=?",
                    updates,
                )
                conn.commit()
                self.logger.info(f"✅ 均线计算完成: {len(updates)} 只")
        finally:
            conn.close()

    # ============================================================
    # 工具方法
    # ============================================================

    @staticmethod
    def _f(val, default=0.0) -> float:
        """安全转 float"""
        try:
            return float(val) if val and val != "" else default
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _to_qt_code(code: str) -> str:
        """纯代码 → qt 格式: 600519 → sh600519"""
        if code.startswith("6"):
            return f"sh{code}"
        elif code.startswith(("0", "3", "2")):
            return f"sz{code}"
        elif code.startswith(("8", "4", "9")):
            return f"bj{code}"
        return code

    @staticmethod
    def _parse_cn_money(raw: str) -> float | None:
        """解析中文金额: '3.66亿' → 366000000, '4349.01万' → 43490100"""
        if not raw:
            return None
        raw = raw.strip().replace(",", "").replace(" ", "")
        try:
            if "亿" in raw:
                return float(raw.replace("亿", "")) * 100_000_000
            elif "万" in raw:
                return float(raw.replace("万", "")) * 10_000
            else:
                return float(raw)
        except ValueError:
            return None
