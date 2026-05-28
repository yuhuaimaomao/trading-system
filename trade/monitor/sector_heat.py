# -*- coding: utf-8 -*-
"""板块热度监控 — 每 5 轮用全市场快照算板块涨跌排名"""

import logging
import sqlite3
from collections import defaultdict

logger = logging.getLogger(__name__)


class SectorHeatMonitor:
    """用全市场快照按行业分组算平均涨跌幅，检查持仓/信号票所在板块排名。"""

    def __init__(self, db_path: str, telegram_bot=None):
        self.db_path = db_path
        self.telegram = telegram_bot
        self._sector_history: dict[str, list[float]] = defaultdict(list)

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def check(self, snapshot: dict[str, dict]) -> list[str]:
        """用全市场快照计算板块热度。

        snapshot: {code: {price, changePct}} 来自 watcher 的 _market_snapshot
        """
        if not snapshot:
            return []

        industry_map = self._load_industry_map()

        # 按行业分组算涨跌幅（用 QMT 的 changePct，不行就用快照价格自算）
        sector_change: dict[str, list[float]] = defaultdict(list)
        for code, item in snapshot.items():
            info = industry_map.get(code)
            if info is None:
                continue
            industry = info["industry"]
            # QMT changePct 已经是百分比值
            chg_pct = item.get("changePct", 0)
            try:
                chg_pct = float(chg_pct)
            except (ValueError, TypeError):
                chg_pct = 0
            sector_change[industry].append(chg_pct)

        if not sector_change:
            return []

        # 行业平均涨跌幅（至少 3 只票）
        sector_avg: dict[str, float] = {}
        for industry, changes in sector_change.items():
            if len(changes) >= 3:
                sector_avg[industry] = round(sum(changes) / len(changes), 2)

        ranked = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)
        top5 = ranked[:5]
        bottom3 = ranked[-3:] if len(ranked) >= 3 else []

        messages: list[str] = []

        # 持仓板块
        my_sectors = self._get_my_sectors()
        # 观察板块（pending + 复盘，去重已持仓的）
        watch_sectors = self._get_watch_sectors() - my_sectors

        # 更新板块历史（保留最近 3 次）
        for ind, avg in sector_avg.items():
            self._sector_history[ind].append(avg)
            if len(self._sector_history[ind]) > 3:
                self._sector_history[ind] = self._sector_history[ind][-3:]

        def _check_sectors(sectors):
            """比较最近3次板块均涨跌幅，检测持续走强/走弱。"""
            warnings, good = [], []
            for ind in sectors:
                history = self._sector_history.get(ind, [])
                if len(history) < 3:
                    continue
                delta = history[-1] - history[0]
                rank = next((i + 1 for i, (n, _) in enumerate(ranked) if n == ind), None)
                avg = history[-1]
                if delta < -1.0:
                    warnings.append(f"{ind} {avg:+.1f}%（↓{delta:+.1f}% 排名{rank}）")
                elif delta > 1.0:
                    good.append(f"{ind} {avg:+.1f}%（↑{delta:+.1f}% 排名{rank}）")
            return warnings, good

        my_warnings, my_good = _check_sectors(my_sectors)
        watch_warnings, watch_good = _check_sectors(watch_sectors)

        if top5:
            lines = ["📊 板块热度 TOP5:"]
            for i, (ind, avg) in enumerate(top5, 1):
                tags = []
                if ind in my_sectors:
                    tags.append("持仓")
                if ind in watch_sectors:
                    tags.append("观察")
                tag = f" ← {','.join(tags)}" if tags else ""
                # 计算相对上次快照的变化
                history = self._sector_history.get(ind, [])
                if len(history) >= 2:
                    delta = history[-1] - history[-2]
                    delta_str = f" ↑{delta:+.1f}%" if delta > 0 else f" ↓{abs(delta):.1f}%"
                else:
                    delta_str = ""
                lines.append(f"  {i}. {ind} {avg:+.1f}%{delta_str}{tag}")
            messages.append("\n".join(lines))

        if my_warnings:
            messages.append(f"⚠️ 持仓板块走弱: {', '.join(my_warnings)}")
        if my_good:
            messages.append(f"✅ 持仓板块走强: {', '.join(my_good)}")
        if watch_warnings:
            messages.append(f"👀 观察板块走弱: {', '.join(watch_warnings)}")
        if watch_good:
            messages.append(f"👀 观察板块走强: {', '.join(watch_good)}")

        return messages

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def _load_industry_map(self) -> dict[str, dict]:
        """从 stock_basic 加载行业映射。"""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT stock_code, industry FROM stock_basic
                   WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
            conn.close()
            return {row[0]: {"industry": row[1] or "其他"} for row in rows}
        except Exception as e:
            logger.warning(f"加载行业映射失败: {e}")
            return {}

    def _get_my_sectors(self) -> set[str]:
        """获取实际持仓所在的行业。"""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT DISTINCT sb.industry
                   FROM stock_basic sb
                   JOIN trade_signals ts ON ts.stock_code = sb.stock_code
                   WHERE ts.status = 'bought'
                     AND sb.trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
            conn.close()
            return {r[0] for r in rows if r[0]}
        except Exception:
            return set()

    def _get_watch_sectors(self) -> set[str]:
        """获取观察板块（pending 信号 + 复盘推荐）。"""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT DISTINCT sb.industry
                   FROM stock_basic sb
                   JOIN trade_signals ts ON ts.stock_code = sb.stock_code
                   WHERE ts.status = 'pending'
                     AND sb.trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
            rows2 = conn.execute(
                """SELECT DISTINCT sb.industry
                   FROM stock_basic sb
                   JOIN stock_tracker st ON st.stock_code = sb.stock_code
                   WHERE st.push_date = (SELECT MAX(push_date) FROM stock_tracker WHERE source='复盘')
                     AND sb.trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
            conn.close()
            sectors = {r[0] for r in rows if r[0]}
            sectors.update(r[0] for r in rows2 if r[0])
            return sectors
        except Exception:
            return set()
