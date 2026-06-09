"""
宏观数据采集模块
功能：采集全球宏观经济数据（美股、汇率、A50、原油、黄金）

数据源：
- 美股：Yahoo Finance
- 汇率：AkShare / Yahoo Finance
- A50：东方财富 API / 腾讯财经
- 原油：腾讯财经（USO ETF）
- 黄金：Yahoo Finance（COMEX 期货）
"""

from datetime import datetime
from typing import Dict, Optional

from curl_cffi import requests as curl_requests

# 导入日志系统
from system.utils.logger import get_collect_logger

logger = get_collect_logger("macro")


class MacroCollector:
    """宏观数据采集器"""

    def __init__(self, timeout: int = 10):
        self.logger = logger
        self.timeout = timeout
        self.session = curl_requests.Session(impersonate="chrome124")
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            }
        )
        self.session.proxies = {}
        self.session.trust_env = False

        # 预热：获取东财 Cookie
        try:
            self.session.get("https://push2.eastmoney.com/", timeout=5)
        except Exception:
            pass
        self.logger.info("宏观数据采集器初始化完成")

    def fetch_and_save(self) -> Dict:
        """统一入口：采集宏观数据并入库"""
        macro_data = self.collect_all()
        self.save_to_db(macro_data)
        return macro_data

    def collect_all(self) -> Dict:
        """
        采集完整宏观数据

        Returns:
            宏观数据字典
        """
        macro_data = {
            "us_market": self.get_us_market(),
            "exchange_rate": self.get_exchange_rate(),
            "a50_futures": self.get_a50_futures(),
            "crude_oil": self.get_crude_oil(),
            "gold": self.get_gold(),
            "timestamp": datetime.now().isoformat(),
        }

        self.logger.info("宏观数据采集完成")
        return macro_data

    @staticmethod
    def save_to_db(macro_data: Dict, trade_date: str = None):
        """将宏观数据保存到 macro_daily 表"""
        import sqlite3

        from system.config.settings import DATABASE_PATH

        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        conn = sqlite3.connect(str(DATABASE_PATH))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS macro_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT UNIQUE NOT NULL,
                    nasdaq_change REAL,
                    kweb_change REAL,
                    usd_cny_rate REAL,
                    a50_price REAL,
                    a50_change REAL,
                    crude_oil_price REAL,
                    crude_oil_change REAL,
                    gold_price REAL,
                    gold_change REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            us = macro_data.get("us_market", {}) or {}
            nasdaq = us.get("nasdaq", {}) or {}
            kweb = us.get("china_etf", {}) or {}
            fx = (macro_data.get("exchange_rate", {}) or {}).get("usd_cny", {}) or {}
            a50 = macro_data.get("a50_futures", {}) or {}
            oil = macro_data.get("crude_oil", {}) or {}
            gold = macro_data.get("gold", {}) or {}

            conn.execute(
                """
                INSERT OR REPLACE INTO macro_daily (
                    trade_date, nasdaq_change, kweb_change, usd_cny_rate,
                    a50_price, a50_change, crude_oil_price, crude_oil_change,
                    gold_price, gold_change, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    trade_date,
                    nasdaq.get("change"),
                    kweb.get("change"),
                    fx.get("rate"),
                    a50.get("price"),
                    a50.get("change"),
                    oil.get("price"),
                    oil.get("change"),
                    gold.get("price"),
                    gold.get("change"),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            conn.commit()
            logger.info(f"宏观数据已保存到 macro_daily ({trade_date})")
        except Exception as e:
            logger.warning(f"保存宏观数据失败：{e}")
        finally:
            conn.close()

    def get_us_market(self) -> Optional[Dict]:
        """
        获取隔夜美股数据（Yahoo Finance）

        Returns:
            美股数据 {nasdaq: {price, change}, china_etf: {price, change}}
        """
        try:
            result = {}

            # 纳指
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC?interval=1d&range=1d"
            resp = self.session.get(url, timeout=self.timeout)
            data = resp.json()
            quote = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            if quote:
                prev_close = quote.get("chartPreviousClose", 0)
                current = quote.get("regularMarketPrice", 0)
                result["nasdaq"] = {
                    "price": current,
                    "change": round((current - prev_close) / prev_close * 100, 2) if prev_close else 0,
                }

            # 中概股 KWEB
            url = "https://query1.finance.yahoo.com/v8/finance/chart/KWEB?interval=1d&range=1d"
            resp = self.session.get(url, timeout=self.timeout)
            data = resp.json()
            quote = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            if quote:
                prev_close = quote.get("chartPreviousClose", 0)
                current = quote.get("regularMarketPrice", 0)
                change_pct = quote.get("regularMarketChangePercent", 0)
                result["china_etf"] = {
                    "price": round(current, 2) if current else 0,
                    "change": round(change_pct, 2)
                    if change_pct
                    else round((current - prev_close) / prev_close * 100, 2)
                    if prev_close
                    else 0,
                }

            return result if result else None

        except Exception as e:
            self.logger.error(f"获取美股数据失败：{e}")
            return None

    def get_exchange_rate(self) -> Dict:
        """
        获取汇率数据（美元兑人民币离岸）

        Returns:
            汇率数据 {usd_cny: {rate, change}}
        """
        try:
            from system.config.akshare_config import get_akshare

            # 方案 1：akshare（中国银行）
            try:
                usd_cny_df = get_akshare().currency_boc_sina(symbol="美元")
                if not usd_cny_df.empty:
                    rate = float(usd_cny_df.iloc[-1]["中行汇买价"]) / 100.0
                    return {"usd_cny": {"rate": round(rate, 4), "change": 0}}
            except Exception:
                pass

            # 方案 2：yfinance 备用
            try:
                import yfinance as yf

                usdcny = yf.Ticker("USDCNY=X")
                hist = usdcny.history(period="1d")
                if not hist.empty:
                    rate = float(hist["Close"].iloc[-1])
                    return {"usd_cny": {"rate": round(rate, 4), "change": 0}}
            except Exception:
                pass

            # 降级方案
            self.logger.warning("⚠️ 汇率数据获取失败，使用硬编码回退值 USD/CNY=7.25（数据可能过时）")
            return {"usd_cny": {"rate": 7.25, "change": 0, "_fallback": True}}

        except Exception as e:
            self.logger.warning(f"汇率数据获取失败：{e}")
            return {"usd_cny": {"rate": 7.25, "change": 0}}

    def get_a50_futures(self) -> Dict:
        """
        获取 A50 期货数据（东方财富 API）

        Returns:
            A50 数据 {price, change}
        """
        try:
            # 东方财富 API（富时中国 A50 期货连续合约）
            url = "https://push2.eastmoney.com/api/qt/stock/get"
            params = {"secid": "104.CN00Y", "fields": "f43,f170"}
            headers = {
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
            }
            resp = self.session.get(url, params=params, headers=headers, timeout=5)

            if resp.status_code == 200:
                data = resp.json()
                if data.get("data"):
                    d = data["data"]
                    price = d.get("f43", 0) / 10.0
                    change = d.get("f170", 0) / 100.0

                    if price > 0:
                        self.logger.info(f"A50 期货获取成功 (东方财富): {price} ({change}%)")
                        return {"price": price, "change": change}

            raise ValueError("A50 返回空数据")

        except Exception as e:
            self.logger.warning(f"A50 获取失败：{e}，使用腾讯上证 50 替代")
            # 降级方案：腾讯上证 50
            try:
                url = "http://qt.gtimg.cn/q=sh000016"
                resp = self.session.get(url, timeout=3)
                data = resp.text.split("~")
                if len(data) > 32:
                    price = float(data[3])
                    change = float(data[32])
                    self.logger.info("A50(上证 50 替代) 获取成功")
                    return {"price": price, "change": change}
            except Exception:
                pass

            self.logger.warning("⚠️ A50 数据获取失败，使用硬编码回退值 A50=13500（数据可能过时）")
            return {"price": 13500.0, "change": 0, "_fallback": True}

    def get_crude_oil(self) -> Dict:
        """
        获取原油数据（WTI，腾讯财经）

        Returns:
            原油数据 {price, change}
        """
        try:
            # 腾讯财经（美国原油 ETF，USO）
            url = "http://qt.gtimg.cn/q=usUSO"
            resp = self.session.get(url, timeout=3)
            data = resp.text.split("~")

            if len(data) > 32:
                current = float(data[3])
                change = float(data[32])
                self.logger.info(f"原油 (USO) 获取成功：{current} ({change}%)")
                return {"price": current, "change": change}
            else:
                raise ValueError("腾讯数据格式异常")

        except Exception as e:
            self.logger.warning(f"原油获取失败：{e}")
            self.logger.warning("⚠️ 原油数据获取失败，使用硬编码回退值 WTI=$75（数据可能过时）")
            return {"price": 75.0, "change": 0, "_fallback": True}

    def get_gold(self) -> Dict:
        """
        获取黄金数据（COMEX 期货，Yahoo Finance）

        Returns:
            黄金数据 {price, change}
        """
        try:
            import yfinance as yf

            # COMEX 黄金期货（GC=F），获取 5 天数据以确保有昨收
            gold = yf.Ticker("GC=F")
            hist = gold.history(period="5d")

            if not hist.empty and len(hist) >= 1:
                current = float(hist["Close"].iloc[-1])

                # 计算涨跌幅
                if len(hist) >= 2:
                    prev_close = float(hist["Close"].iloc[-2])
                    change = ((current - prev_close) / prev_close * 100) if prev_close > 0 else 0
                else:
                    change = 0

                self.logger.info(f"黄金 (COMEX) 获取成功：{current:.2f} ({change:.2f}%)")
                return {"price": round(current, 2), "change": round(change, 2)}
            else:
                raise ValueError("COMEX 黄金数据为空")

        except Exception as e:
            self.logger.warning(f"黄金获取失败：{e}，使用备用接口")
            # 备用：腾讯 GLD ETF
            try:
                url = "http://qt.gtimg.cn/q=usGLD"
                resp = self.session.get(url, timeout=3)
                data = resp.text.split("~")
                if len(data) > 32:
                    current = float(data[3])
                    change = float(data[32])
                    self.logger.info(f"黄金 (GLD 备用) 获取成功：{current} ({change}%)")
                    return {"price": current, "change": change}
            except Exception:
                pass

            self.logger.warning("⚠️ 黄金数据获取失败，使用硬编码回退值 Gold=$2700（数据可能过时）")
            return {"price": 2700.0, "change": 0, "_fallback": True}


# 测试
if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    collector = MacroCollector()
    macro = collector.collect_all()

    print("\n宏观数据采集结果:")
    print(f"  美股：{macro.get('us_market', {})}")
    print(f"  汇率：{macro.get('exchange_rate', {})}")
    print(f"  A50: {macro.get('a50_futures', {})}")
    print(f"  原油：{macro.get('crude_oil', {})}")
    print(f"  黄金：{macro.get('gold', {})}")
