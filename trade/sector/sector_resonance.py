"""板块共振/逆势分析 — 同一时间窗口内比较板块与大盘变化方向。

核心原则: 大盘和板块用同一个时间窗口内的变化方向来比较，不用绝对值。
- 大盘↓ + 板块↓ = 共振下行
- 大盘↑ + 板块↑ = 共振上行
- 大盘↓ + 板块↑ = 逆势走强
- 大盘↑ + 板块↓ = 逆势走弱
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Callable

from system.config import settings

logger = logging.getLogger(__name__)

# 从配置读取阈值（保留模块级别名供外部引用）
DIRECTION_THRESHOLD = settings.RESONANCE_INDEX_DIRECTION_THRESHOLD
SECTOR_DIRECTION_THRESHOLD = settings.RESONANCE_SECTOR_DIRECTION_THRESHOLD
INDEX_VOLATILITY_THRESHOLD = settings.RESONANCE_VOLATILITY_TRIGGER


class SectorResonanceAnalyzer:
    """分析板块与大盘的共振/逆势关系。

    用法:
        analyzer = SectorResonanceAnalyzer()
        result = analyzer.analyze(
            index_prices=self._index_prices,
            sector_histories=self._sector_trend_history,
            concept_histories=self._concept_trend_history,
            window_entries=settings.RESONANCE_PUSH_WINDOW_ENTRIES,
            ...
        )
        msg = analyzer.format_push_message(result)
    """

    def analyze(
        self,
        index_prices: list[float],
        sector_histories: dict[str, list[float]],
        concept_histories: dict[str, list[float]],
        sector_stats: dict[str, dict],
        concept_stats: dict[str, dict],
        market_snapshot: dict[str, dict],
        industry_cache: dict[str, str],
        concept_cache: dict[str, list[str]],
        trend_starts: dict[str, str],
        resolve_name: Callable[[str], str],
        window_entries: int = 4,
    ) -> dict:
        """分析指定窗口内的共振/逆势关系。

        Args:
            window_entries: 板块趋势历史中取多少条（每条约3分钟）
                独立推送用 RESONANCE_PUSH_WINDOW_ENTRIES，定时TOP5用 RESONANCE_TOP5_WINDOW_ENTRIES

        Returns:
            {
                "index_change": float,
                "index_direction": "up"|"down"|"flat",
                "index_price": float,
                "resonance_up": [(name, change_pct, stats, leaders, trend_start), ...],
                "resonance_down": [...],
                "counter_up": [...],
                "counter_down": [...],
            }
            leaders: [(code, change_pct, name), ...]
        """
        empty = self._empty_result()

        if len(index_prices) < 3:
            return empty

        # 1. 大盘窗口内变化
        # _index_prices 每轮一个点，_sector_trend_history 每3轮一个点
        # 使用 window_entries*3 个 index 点来对齐
        index_n = min(
            window_entries * 3,
            len(index_prices) - 1,
            settings.RESONANCE_INDEX_WINDOW_MAX,
        )
        if index_n < 2:
            return empty

        idx_start = index_prices[-(index_n + 1)]
        idx_end = index_prices[-1]
        if idx_start <= 0:
            return empty
        index_change = (idx_end - idx_start) / idx_start
        index_dir = self._direction(index_change)

        if index_dir == "flat":
            return empty

        # 2. 行业分类
        ind_results = self._classify_group(
            sector_histories,
            sector_stats,
            index_dir,
            window_entries,
            market_snapshot,
            industry_cache,
            trend_starts,
            resolve_name,
            cache_type="industry",
        )

        # 3. 概念分类
        con_results = self._classify_group(
            concept_histories,
            concept_stats,
            index_dir,
            window_entries,
            market_snapshot,
            concept_cache,
            trend_starts,
            resolve_name,
            cache_type="concept",
        )

        # 4. 合并，各取前5
        result = {
            "index_change": index_change,
            "index_direction": index_dir,
            "index_price": idx_end,
        }
        for key in ["resonance_up", "resonance_down", "counter_up", "counter_down"]:
            combined = ind_results.get(key, []) + con_results.get(key, [])
            # 按变化幅度绝对值降序
            combined.sort(key=lambda x: abs(x[1]), reverse=True)
            result[key] = combined[: settings.RESONANCE_TOP_N]

        return result

    # ======================== 内部分类 ========================

    def _direction(self, change: float) -> str:
        """指数变化率方向判定。change 是比率（如 -0.007 = -0.7%）。"""
        if change > DIRECTION_THRESHOLD:
            return "up"
        elif change < -DIRECTION_THRESHOLD:
            return "down"
        return "flat"

    def _direction_pct(self, change: float) -> str:
        """板块百分点变化方向判定。change 是百分点差（如 -1.2 = 跌1.2个百分点）。"""
        if change > SECTOR_DIRECTION_THRESHOLD:
            return "up"
        elif change < -SECTOR_DIRECTION_THRESHOLD:
            return "down"
        return "flat"

    def _classify_group(
        self,
        histories: dict[str, list[float]],
        stats: dict[str, dict],
        index_dir: str,
        window_entries: int,
        snapshot: dict[str, dict],
        code_cache: dict,
        trend_starts: dict[str, str],
        resolve_name: Callable[[str], str],
        cache_type: str,
    ) -> dict[str, list]:
        """对一组板块做共振/逆势分类。"""
        result: dict[str, list] = {
            "resonance_up": [],
            "resonance_down": [],
            "counter_up": [],
            "counter_down": [],
        }

        # 构建反向索引（板块名 → [codes]），用于查领涨股
        reverse_index: dict[str, list[str]] = defaultdict(list)
        if cache_type == "industry":
            for code, ind in code_cache.items():
                if ind:
                    reverse_index[ind].append(code)
        else:
            for code, concepts in code_cache.items():
                for c in concepts:
                    reverse_index[c].append(code)

        for name, history in histories.items():
            if len(history) < window_entries:
                continue

            # 窗口首尾差
            sector_start = history[-(window_entries + 1)]
            sector_end = history[-1]
            sector_change = sector_end - sector_start  # 百分点差
            sector_dir = self._direction_pct(sector_change)

            if sector_dir == "flat":
                continue

            # 四分类
            if index_dir == "up" and sector_dir == "up":
                category = "resonance_up"
            elif index_dir == "down" and sector_dir == "down":
                category = "resonance_down"
            elif index_dir == "down" and sector_dir == "up":
                category = "counter_up"
            elif index_dir == "up" and sector_dir == "down":
                category = "counter_down"
            else:
                continue

            s = stats.get(name, {})
            trend_start = trend_starts.get(name, "")

            # 领涨/领跌股：领跌只取跌的，龙头只取涨的
            is_down = category in ("resonance_down", "counter_down")
            leaders = self._get_leaders(
                name,
                reverse_index,
                snapshot,
                resolve_name,
                top_n=settings.RESONANCE_LEADER_COUNT,
                reverse=not is_down,
                only_negative=is_down,
            )

            result[category].append((name, sector_change, s, leaders, trend_start))

        return result

    def _get_leaders(
        self,
        group_name: str,
        reverse_index: dict[str, list[str]],
        snapshot: dict[str, dict],
        resolve_name: Callable[[str], str],
        top_n: int = 3,
        reverse: bool = True,
        only_negative: bool = False,
    ) -> list[tuple[str, float, str]]:
        """获取板块内的领涨/领跌股。

        reverse=True + only_negative=False → 涨幅最大在前（龙头）
        reverse=False + only_negative=True → 跌幅最大在前（领跌），只取跌的
        """
        codes = reverse_index.get(group_name, [])
        if not codes:
            return []

        stocks = []
        for code in codes:
            snap = snapshot.get(code, {})
            chg = snap.get("changePct", 0)
            try:
                chg = float(chg)
            except (ValueError, TypeError):
                chg = 0
            # 只取同向的：领跌只取跌的，龙头只取涨的
            if only_negative and chg >= 0:
                continue
            if not only_negative and chg <= 0:
                continue
            stocks.append((code, chg))

        stocks.sort(key=lambda x: x[1], reverse=reverse)
        return [(code, chg, resolve_name(code)) for code, chg in stocks[:top_n]]

    def _empty_result(self) -> dict:
        return {
            "index_change": 0,
            "index_direction": "flat",
            "index_price": 0,
            "resonance_up": [],
            "resonance_down": [],
            "counter_up": [],
            "counter_down": [],
        }

    # ======================== 格式化 ========================

    def format_push_message(
        self, result: dict, my_codes: set | None = None
    ) -> str | None:
        """格式化独立推送消息。无有效内容返回 None。"""
        has_any = any(
            result.get(k)
            for k in ["resonance_up", "resonance_down", "counter_up", "counter_down"]
        )
        if not has_any:
            return None

        now = datetime.now().strftime("%H:%M")
        idx_price = result.get("index_price", 0)
        idx_change = result.get("index_change", 0)
        lines = [
            f"🏭 板块异动  {now}",
            f"上证 {idx_price:.2f}  {idx_change:+.2%}",
        ]

        my_codes = my_codes or set()

        sections = [
            ("🟢 共振上行", result.get("resonance_up", []), False),
            ("🔴 共振下行", result.get("resonance_down", []), True),
            ("🔄 逆势走强", result.get("counter_up", []), False),
            ("🔴 逆势走弱", result.get("counter_down", []), True),
        ]

        for title, items, is_down in sections:
            if not items:
                continue
            lines.append("")
            lines.append(title)
            lines.append("─" * 30)
            for name, change, stats, leaders, trend_start in items:
                up = stats.get("up", 0)
                down = stats.get("down", 0)
                trend_tag = f"  {trend_start}起" if trend_start else ""
                vol_tag = ""
                vr = stats.get("vol_ratio", 1.0)
                if vr > settings.RESONANCE_VOL_SURGE_RATIO:
                    vol_tag = "  🔥放量"
                elif vr < settings.RESONANCE_VOL_SHRINK_RATIO:
                    vol_tag = "  量缩"

                # 标记持仓板块
                my_tag = ""
                for _code, _chg, leader_name in leaders:
                    if _code in my_codes:
                        my_tag = f"  ⚠️{leader_name}"
                        break

                lines.append(
                    f"  {name}  {change:+.2f}%  涨{up}跌{down}{trend_tag}{vol_tag}{my_tag}"
                )
                if leaders:
                    leader_str = "  ".join(f"{n} {c:+.1f}%" for _c, c, n in leaders)
                    lines.append(f"  → {leader_str}")

            lines.append(f"  ── 共 {len(items)} 个板块 ──")

        return "\n".join(lines)

    def format_top5_labels(self, result: dict) -> dict[str, str]:
        """为板块热度TOP5生成共振/逆势标签。返回 {sector_name: "📈共振"|...}。"""
        labels: dict[str, str] = {}
        mapping = [
            ("resonance_up", "🟢共振"),
            ("resonance_down", "🔴共振"),
            ("counter_up", "🔄逆势"),
            ("counter_down", "🔴逆势"),
        ]
        for key, emoji_label in mapping:
            for name, *_ in result.get(key, []):
                labels[name] = emoji_label
        return labels
