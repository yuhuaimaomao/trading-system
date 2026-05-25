# -*- coding: utf-8 -*-
"""QMT HTTP 客户端 — 封装对 Windows QMT Server 的请求"""
import logging
import time

import requests

from system.config.settings import QMT_BASE_URL

QMT_SERVER = QMT_BASE_URL
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 120

logger = logging.getLogger(__name__)


def strip_suffix(code):
    """去掉 QMT 代码后缀 .SH/.SZ/.BJ，与东财格式保持一致"""
    for s in (".SH", ".SZ", ".BJ"):
        if code.endswith(s):
            return code[:-len(s)]
    return code


class QMTClient:
    def __init__(self, server=QMT_SERVER):
        self.server = server

    def _get(self, path, params=None, timeout=None):
        if timeout is None:
            timeout = (CONNECT_TIMEOUT, READ_TIMEOUT)
        url = f"{self.server}{path}"
        t0 = time.time()
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            return {"success": False, "error": f"请求超时({time.time()-t0:.0f}s)", "elapsed": round(time.time()-t0, 2)}
        except requests.exceptions.ConnectionError:
            return {"success": False, "error": "无法连接QMT服务器", "elapsed": round(time.time()-t0, 2)}
        except Exception as e:
            return {"success": False, "error": str(e), "elapsed": round(time.time()-t0, 2)}

    def status(self):
        return self._get("/status", timeout=(5, 10))

    def all_quotes(self):
        return self._get("/all_quotes")

    def quote(self, code):
        return self._get(f"/quote/{code}")

    def quotes(self, codes):
        return self._get("/quotes", {"codes": ",".join(codes)})

    def instrument(self, code):
        return self._get(f"/instrument/{code}")

    def history(self, code, period="1d", start=None, end=None, count=None):
        params = {"period": period}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        if count:
            params["count"] = count
        return self._get(f"/history/{code}", params)

    def minute_kline(self, code, period="1m", start=None, end=None):
        return self.history(code, period=period, start=start, end=end)

    def tick(self, code, start=None, end=None):
        params = {}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        return self._get(f"/tick/{code}", params)

    def financial(self, code, tables=None):
        params = {}
        if tables:
            params["tables"] = ",".join(tables)
        return self._get(f"/financial/{code}", params)

    def dividend(self, code):
        return self._get(f"/dividend/{code}")

    def st_history(self, code):
        return self._get(f"/st_history/{code}")

    def calendar(self, market="sh"):
        return self._get(f"/calendar/{market}")

    def sectors(self):
        return self._get("/sectors")

    def sector_stocks(self, sector_name):
        return self._get(f"/sector/{sector_name}")
