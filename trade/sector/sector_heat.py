"""板块热度监控 — 每 5 轮用全市场快照算板块涨跌排名（行业 + 概念）"""

import sqlite3
from collections import defaultdict

from system.utils.logger import get_trade_logger

logger = get_trade_logger("sector")


class SectorHeatMonitor:
    """用全市场快照按行业+概念分组算平均涨跌幅，检查持仓/信号票所在板块排名。"""

    def __init__(self, db_path: str, telegram_bot=None):
        self.db_path = db_path
        self.telegram = telegram_bot
        self._sector_history: dict[str, list[float]] = defaultdict(list)
        # 概念板块同样记录历史趋势
        self._concept_history: dict[str, list[float]] = defaultdict(list)

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def check(
        self, snapshot: dict[str, dict], resonance_labels: dict[str, str] | None = None
    ) -> list[str]:
        """用全市场快照计算板块热度（行业 + 概念）。

        snapshot: {code: {price, changePct}} 来自 watcher 的 _market_snapshot
        resonance_labels: {sector_name: "📈共振"|"📉共振"|"🔄逆势"|"⚠️逆势"}
        """
        if not snapshot:
            return []

        industry_map = self._load_industry_map()
        concept_map = self._load_concept_map()

        # ── 行业维度（1:1）──
        sector_change: dict[str, list[float]] = defaultdict(list)
        # ── 概念维度（1:N）──
        concept_change: dict[str, list[float]] = defaultdict(list)

        for code, item in snapshot.items():
            # QMT changePct 已经是百分比值
            chg_pct = item.get("changePct", 0)
            try:
                chg_pct = float(chg_pct)
            except (ValueError, TypeError):
                chg_pct = 0

            # 行业
            info = industry_map.get(code)
            if info:
                sector_change[info["industry"]].append(chg_pct)

            # 概念（一只股票可归属多个概念）
            for c in concept_map.get(code, []):
                concept_change[c].append(chg_pct)

        if not sector_change and not concept_change:
            return []

        # ── 行业平均涨跌幅（至少 3 只票）──
        sector_avg: dict[str, float] = {}
        for industry, changes in sector_change.items():
            if len(changes) >= 3:
                sector_avg[industry] = round(sum(changes) / len(changes), 2)

        # ── 概念平均涨跌幅（至少 3 只票）──
        concept_avg: dict[str, float] = {}
        for c, changes in concept_change.items():
            if len(changes) >= 3:
                concept_avg[c] = round(sum(changes) / len(changes), 2)

        ranked = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)
        top5 = ranked[:5]

        concept_ranked = sorted(concept_avg.items(), key=lambda x: x[1], reverse=True)
        concept_top5 = concept_ranked[:5]

        messages: list[str] = []

        # 持仓板块
        my_sectors = self._get_my_sectors()
        # 观察板块（pending + 复盘，去重已持仓的）
        watch_sectors = self._get_watch_sectors() - my_sectors

        # ── 更新行业历史（保留最近 3 次）──
        for ind, avg in sector_avg.items():
            self._sector_history[ind].append(avg)
            if len(self._sector_history[ind]) > 3:
                self._sector_history[ind] = self._sector_history[ind][-3:]

        # ── 更新概念历史（保留最近 3 次）──
        for c, avg in concept_avg.items():
            self._concept_history[c].append(avg)
            if len(self._concept_history[c]) > 3:
                self._concept_history[c] = self._concept_history[c][-3:]

        def _check_sectors(sectors):
            """比较最近3次板块均涨跌幅，检测持续走强/走弱。"""
            warnings, good = [], []
            for ind in sectors:
                history = self._sector_history.get(ind, [])
                if len(history) < 3:
                    continue
                delta = history[-1] - history[0]
                rank = next(
                    (i + 1 for i, (n, _) in enumerate(ranked) if n == ind), None
                )
                total = len(ranked)
                avg = history[-1]
                rank_str = f" 排名: {rank}/{total}" if rank else ""
                if delta < -1.0:
                    warnings.append(
                        f"   ⚠️ {ind}: {avg:+.1f}%  ↓{delta:+.1f}%{rank_str}"
                    )
                elif delta > 1.0:
                    good.append(f"   ✅ {ind}: {avg:+.1f}%  ↑{delta:+.1f}%{rank_str}")
            return warnings, good

        my_warnings, my_good = _check_sectors(my_sectors)
        watch_warnings, watch_good = _check_sectors(watch_sectors)

        # ── 行业 TOP5 ──
        if top5:
            medals = ["🥇", "🥈", "🥉"]
            rl = resonance_labels or {}
            lines = ["📊 行业热度 TOP5"]
            for i, (ind, avg) in enumerate(top5, 1):
                tags = []
                if ind in my_sectors:
                    tags.append("持仓✓")
                if ind in watch_sectors:
                    tags.append("观察")
                res_label = rl.get(ind, "")
                if res_label:
                    tags.append(res_label)
                tag = f"  {' '.join(tags)}" if tags else ""
                history = self._sector_history.get(ind, [])
                if len(history) >= 2:
                    delta = history[-1] - history[-2]
                    delta_str = f"  {'↑' if delta > 0 else '↓'}{abs(delta):.1f}%"
                else:
                    delta_str = ""
                prefix = medals[i - 1] if i <= 3 else f" {i}."
                lines.append(f"   {prefix} {ind}: {avg:+.1f}%{delta_str}{tag}")
            messages.append("\n".join(lines))

        # ── 概念 TOP5 ──
        if concept_top5:
            concept_lines = ["📊 概念热度 TOP5"]
            for i, (c, avg) in enumerate(concept_top5, 1):
                stock_count = len(concept_change.get(c, []))
                chistory = self._concept_history.get(c, [])
                if len(chistory) >= 2:
                    delta = chistory[-1] - chistory[-2]
                    delta_str = f"  {'↑' if delta > 0 else '↓'}{abs(delta):.1f}%"
                else:
                    delta_str = ""
                prefix = medals[i - 1] if i <= 3 else f" {i}."
                concept_lines.append(
                    f"   {prefix} {c}: {avg:+.1f}%{delta_str}  ({stock_count}只)"
                )
            messages.append("")  # 空行分隔
            messages.append("\n".join(concept_lines))

        # ── 持仓/观察板块趋势 ──
        if my_warnings or my_good or watch_warnings or watch_good:
            messages.append("   ─────────────────────────")
        if my_warnings:
            messages.append("\n".join(my_warnings))
        if my_good:
            messages.append("\n".join(my_good))
        if watch_warnings:
            messages.append("\n".join(watch_warnings))
        if watch_good:
            messages.append("\n".join(watch_good))

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
            result = {}
            for row in rows:
                ind = (row[1] or "").strip()
                # 过滤无效值：空串、单独横杠（数据库占位符）
                if ind and ind != "-":
                    result[row[0]] = {"industry": ind}
            return result
        except Exception as e:
            logger.warning(f"加载行业映射失败: {e}")
            return {}

    def _load_concept_map(self) -> dict[str, list[str]]:
        """从 stock_basic 加载代码→概念列表映射（1:N，一只股票可能属于多个概念）。"""
        try:
            conn = self._get_conn()
            rows = conn.execute(
                """SELECT stock_code, concepts FROM stock_basic
                   WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)
                     AND concepts IS NOT NULL AND concepts != ''"""
            ).fetchall()
            conn.close()
            cmap: dict[str, list[str]] = {}
            for row in rows:
                raw = (row[1] or "").strip()
                if raw:
                    cmap[row[0]] = [
                        c.strip()
                        for c in raw.replace("|", ",").split(",")
                        # 过滤无效值：空串、单独横杠
                        if c.strip() and c.strip() != "-"
                    ]
            return cmap
        except Exception as e:
            logger.warning(f"加载概念映射失败: {e}")
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
