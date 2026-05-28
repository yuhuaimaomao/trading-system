# -*- coding: utf-8 -*-
"""异动检测 — 行业 + 概念板块，按成分股计算涨跌幅，3轮累计异动时推送"""

import logging
import sqlite3
from collections import defaultdict

from system.config import settings

logger = logging.getLogger(__name__)

SECTOR_SURGE_PCT = getattr(settings, "ABNORMAL_SECTOR_SURGE_PCT", 1.5)
MIN_STOCKS = 3


class AbnormalDetector:
    """按板块成分股计算平均涨跌幅，覆盖行业和概念，3轮累计超阈值推送。"""

    def __init__(self, db_path: str, telegram_bot=None):
        self.db_path = db_path
        self.telegram = telegram_bot
        self._sector_history: dict[tuple, list[float]] = defaultdict(list)
        self._concept_map: dict[str, list[str]] | None = None

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _load_stock_info(self) -> dict[str, dict]:
        # 盘中不变化，懒加载缓存
        if hasattr(self, "_stock_info_cache"):
            return self._stock_info_cache
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT stock_code, stock_name, industry, total_market_cap
                   FROM stock_basic
                   WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
            conn.close()
            info: dict[str, dict] = {}
            for r in rows:
                code = r[0]
                name = r[1] or ""
                mcap = r[3] or 0
                if code.startswith("688"):
                    continue
                if "ST" in name:
                    continue
                if mcap < 5e9 or mcap > 500e9:
                    continue
                info[code] = {"name": name, "industry": r[2] or "其他"}
            self._stock_info_cache = info
            return info
        except Exception as e:
            logger.warning(f"加载 stock_basic 失败: {e}")
            return {}

    def _load_concept_map(self) -> dict[str, list[str]]:
        """加载概念板块映射 {stock_code: [concept_name, ...]}，懒加载一次。"""
        if self._concept_map is not None:
            return self._concept_map
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT ss.stock_code, si.sector_name
                   FROM sector_stocks ss
                   JOIN sector_info si ON si.sector_code = ss.sector_code
                   WHERE si.sector_type = 'concept'"""
            ).fetchall()
            conn.close()
            cmap: dict[str, list[str]] = defaultdict(list)
            for code, name in rows:
                cmap[code].append(name)
            self._concept_map = dict(cmap)
            return self._concept_map
        except Exception as e:
            logger.warning(f"加载概念映射失败: {e}")
            self._concept_map = {}
            return {}

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def detect_sector(self, current: dict, previous: dict) -> list[str]:
        """按成分股计算板块均涨跌幅（行业+概念），3轮累计超阈值推送。"""
        if not previous:
            return []

        stock_info = self._load_stock_info()
        concept_map = self._load_concept_map()

        # key: (type, name) → list of changes
        sector_changes: dict[tuple, list[float]] = defaultdict(list)
        sector_stocks: dict[tuple, list[tuple]] = defaultdict(list)

        for code, cur_data in current.items():
            info = stock_info.get(code)
            if info is None:
                continue
            prev_data = previous.get(code)
            if prev_data is None:
                continue
            cur_price = cur_data.get("price")
            prev_price = prev_data.get("price")
            if cur_price is None or prev_price is None or prev_price <= 0:
                continue

            change_pct = (cur_price - prev_price) / prev_price * 100
            stock_entry = (code, info["name"], change_pct, cur_price)

            # 行业
            ind_key = ("industry", info["industry"])
            sector_changes[ind_key].append(change_pct)
            sector_stocks[ind_key].append(stock_entry)

            # 概念（一只票属于多个概念）
            for cname in concept_map.get(code, []):
                con_key = ("concept", cname)
                sector_changes[con_key].append(change_pct)
                sector_stocks[con_key].append(stock_entry)

        # 计算均涨跌幅
        sector_avg: dict[tuple, float] = {}
        for key, changes in sector_changes.items():
            if len(changes) >= MIN_STOCKS:
                sector_avg[key] = sum(changes) / len(changes)

        # 更新 3 轮历史
        for key, avg in sector_avg.items():
            self._sector_history[key].append(avg)
            if len(self._sector_history[key]) > 3:
                self._sector_history[key] = self._sector_history[key][-3:]

        # 检测异动：3轮累计超阈值
        messages: list[str] = []
        for key, history in self._sector_history.items():
            if len(history) < 3:
                continue
            cumulative = sum(history)
            if abs(cumulative) < SECTOR_SURGE_PCT:
                continue

            stype, sname = key
            label = "🏭" if stype == "industry" else "💡"
            direction = "涨" if cumulative > 0 else "跌"

            stocks = sector_stocks.get(key, [])
            stocks.sort(key=lambda x: abs(x[2]), reverse=True)
            shown = stocks[:5]

            stock_lines = []
            for s in shown:
                arrow = "↑" if s[2] > 0 else "↓"
                stock_lines.append(f"  {s[0]} {s[1]} {arrow}{abs(s[2]):.1f}% 现价{s[3]:.2f}")

            messages.append(
                f"{label} {sname} 板块异动 | 3轮累计{direction}{abs(cumulative):.1f}%\n"
                + "\n".join(stock_lines)
            )

            self._sector_history[key] = []

        return messages
