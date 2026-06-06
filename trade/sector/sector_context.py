"""板块/上下文：板块趋势追踪、快照持久化、开盘决策汇总、指数恢复。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime

from system.config import settings

logger = logging.getLogger(__name__)


class SectorContextMixin:
    """板块/上下文：板块趋势追踪、快照持久化、开盘决策汇总、指数恢复。"""

    def _send_opening_decision(self, prices: dict[str, float], market_ok: bool):
        """集合竞价后推送一条汇总决策：持仓状态 + 买入区信号 + 待观察。"""
        self._ensure_industry_cache()
        idx = self._get_index_quote()
        idx_price = idx["price"] if idx else 0
        chg_pct = idx["change_pct"] if idx else 0
        _, _, ma20 = self._get_index_baseline()
        vs_ma20 = (
            "MA20: 上方" if ma20 and idx_price >= ma20 else "MA20: 下方" if ma20 else ""
        )
        market_label = "市场: ⚠️危险" if not market_ok else "市场: 正常"

        lines = [f"📋 开盘决策  {self._trade_date}", "   ─────────────────────────"]
        if idx_price:
            lines.append(
                f"   上证: {idx_price:.0f}  {chg_pct:+.2%}  {vs_ma20}  {market_label}"
            )
        lines.append("")

        # ━━━ 当前持仓 ━━━
        if self.paper_account.positions:
            pos_list = list(self.paper_account.positions.items())
            lines.append(f"   📦 持仓 {len(pos_list)} 只")
            for code, pos in pos_list:
                price = prices.get(code)
                if price is None:
                    continue
                pnl_pct = (
                    (price - pos.avg_cost) / pos.avg_cost * 100 if pos.avg_cost else 0
                )
                pnl_emoji = "🟢" if pnl_pct > 2 else "🟡" if pnl_pct > -2 else "🔴"
                is_today = "  🔒T+1" if pos.entry_date == self._trade_date else ""
                meta = self._pos_meta.get(code, {})
                sl = meta.get("sl", 0)
                tp = meta.get("tp", 0)
                triggered = ""
                if sl > 0 and price <= sl:
                    triggered = "  ⚠️触发止损"
                elif tp > 0 and price >= tp:
                    triggered = "  ✅触发止盈"
                lines.append(
                    f"   {pnl_emoji} {code} {pos.stock_name}  成本: {pos.avg_cost:.2f}  "
                    f"现价: {price:.2f}  盈亏: {pnl_pct:+.1f}%{is_today}{triggered}"
                )
                lines.append(f"       止损: {sl:.2f}  止盈: {tp:.2f}")
            lines.append("")

        # ━━━ 信号列表（来自 trade_signals）━━━
        try:
            signals = self.repo.get_pending_signals(account="paper")
        except Exception:
            signals = []

        buy_list = []  # 已在买入区
        watch_list = []  # 未进入买入区

        for s in signals:
            code = s["stock_code"]
            price = prices.get(code)
            if price is None:
                continue
            buy_min = s.get("buy_zone_min") or 0
            buy_max = s.get("buy_zone_max") or 0
            if buy_min <= 0:
                continue

            in_zone = buy_min <= price <= buy_max
            raw_name = s.get("stock_name", "")
            entry_name = (
                raw_name if raw_name and raw_name != code else self._resolve_name(code)
            )
            entry = (
                code,
                entry_name,
                price,
                buy_min,
                buy_max,
                s.get("stop_loss") or 0,
                s.get("take_profit") or 0,
                s.get("signal_source", ""),
                s.get("signal_score", 0),
            )

            if in_zone:
                buy_list.append(entry)
            else:
                watch_list.append(entry)

        if buy_list:
            lines.append(f"   🎯 买入区信号 {len(buy_list)} 只")
            for code, name, price, buy_min, buy_max, sl, tp, source, score in buy_list:
                src_tag = "复盘" if source == "REVIEW" else "AI"
                lines.append(
                    f"   🔴 {code} {name}  现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}"
                )
                lines.append(
                    f"       止损: {sl:.2f}  止盈: {tp:.2f}  评分: {score:.0f}  {src_tag}"
                )
            lines.append("")

        if watch_list:
            lines.append(f"   👀 待观察 {len(watch_list)} 只")
            for (
                code,
                name,
                price,
                buy_min,
                buy_max,
                sl,
                tp,
                source,
                score,
            ) in watch_list:
                status = "高于区间" if price > buy_max else "低于区间"
                lines.append(
                    f"   👀 {code} {name}  现价: {price:.2f}  {status}: {buy_min:.2f}~{buy_max:.2f}"
                )
            lines.append("")

        if not buy_list and not watch_list:
            lines.append("   ━━━ 今日无待处理信号 ━━━")
            lines.append("")

        # ━━━ 板块集中度提示 ━━━
        from collections import Counter

        industries = []
        for code, _, _, _, _, _, _, _, _ in buy_list + watch_list:
            ind = self._industry_cache.get(code, "")
            if ind:
                industries.append(ind)
        for ind, cnt in Counter(industries).items():
            if cnt >= 3:
                lines.append(f"   ⚠️ 板块集中度: {ind} {cnt} 只信号，注意分散风险")

        self._alert("\n".join(lines))

    def _update_sector_trends(self):
        """用全市场快照更新板块趋势 — 含连续性追踪 + 板块广度 + 量能。"""
        if not self._market_snapshot:
            return
        self._ensure_industry_cache()
        self._ensure_concept_cache()

        ind_changes: dict[str, list[float]] = defaultdict(list)
        con_changes: dict[str, list[float]] = defaultdict(list)
        ind_amounts: dict[str, float] = defaultdict(float)
        con_amounts: dict[str, float] = defaultdict(float)

        for code, item in self._market_snapshot.items():
            chg = item.get("changePct", 0)
            amt = item.get("amount", 0)
            try:
                chg = float(chg)
                amt = float(amt)
            except (ValueError, TypeError):
                chg = 0
                amt = 0

            industry = self._industry_cache.get(code)
            if industry:
                ind_changes[industry].append(chg)
                ind_amounts[industry] += amt

            for concept in self._concept_cache.get(code, []):
                con_changes[concept].append(chg)
                con_amounts[concept] += amt

        # 市场均值（用于相对强度）
        all_chgs = [c for changes in ind_changes.values() for c in changes]
        market_avg = sum(all_chgs) / len(all_chgs) if all_chgs else 0

        now_str = datetime.now().strftime("%H:%M")

        # 行业趋势 + 连续性 + 趋势起点时间
        for ind, changes in ind_changes.items():
            if len(changes) < 3:
                continue
            avg = sum(changes) / len(changes)
            history = self._sector_trend_history[ind]
            history.append(avg)

            # 连续性追踪 + 趋势起点
            if len(history) >= 2:
                cur_dir = (
                    "up"
                    if history[-1] > history[-2]
                    else "down"
                    if history[-1] < history[-2]
                    else "flat"
                )
                prev_dir = self._sector_trend_last_dir.get(ind, "")
                if cur_dir == prev_dir and cur_dir != "flat":
                    self._sector_trend_continuity[ind] += 1
                else:
                    self._sector_trend_continuity[ind] = 1 if cur_dir != "flat" else 0
                    if cur_dir != "flat":
                        self._sector_trend_start[ind] = now_str
                self._sector_trend_last_dir[ind] = cur_dir

        # 行业实时统计（含相对强度 + 量能）
        prev_ind_amounts = getattr(self, "_prev_ind_amounts", {})
        self._sector_stats.clear()
        for ind, changes in ind_changes.items():
            if len(changes) < 3:
                continue
            avg = sum(changes) / len(changes)
            up = sum(1 for c in changes if c > 0)
            down = sum(1 for c in changes if c < 0)
            total = len(changes)
            cur_amt = ind_amounts.get(ind, 0)
            prev_amt = prev_ind_amounts.get(ind, 0)
            vol_ratio = cur_amt / prev_amt if prev_amt > 0 else 1.0
            self._sector_stats[ind] = {
                "change_pct": avg,
                "relative": avg - market_avg,
                "up": up,
                "down": down,
                "breadth": (up - down) / total if total > 0 else 0,
                "continuity": self._sector_trend_continuity.get(ind, 0),
                "trend_history": list(self._sector_trend_history.get(ind, [])),
                "amount": cur_amt,
                "vol_ratio": vol_ratio,
            }
        self._prev_ind_amounts = dict(ind_amounts)

        # 概念实时统计（含量能）
        prev_con_amounts = getattr(self, "_prev_con_amounts", {})
        self._concept_stats.clear()
        for con, changes in con_changes.items():
            if len(changes) < 3:
                continue
            avg = sum(changes) / len(changes)
            up = sum(1 for c in changes if c > 0)
            down = sum(1 for c in changes if c < 0)
            cur_amt = con_amounts.get(con, 0)
            prev_amt = prev_con_amounts.get(con, 0)
            vol_ratio = cur_amt / prev_amt if prev_amt > 0 else 1.0
            self._concept_stats[con] = {
                "change_pct": avg,
                "relative": avg - market_avg,
                "up": up,
                "down": down,
                "amount": cur_amt,
                "vol_ratio": vol_ratio,
            }
        self._prev_con_amounts = dict(con_amounts)

        # 概念趋势历史 + 趋势起点时间
        for con, changes in con_changes.items():
            if len(changes) < 3:
                continue
            avg = sum(changes) / len(changes)
            history = self._concept_trend_history[con]
            history.append(avg)

            if len(history) >= 2:
                cur_dir = (
                    "up"
                    if history[-1] > history[-2]
                    else "down"
                    if history[-1] < history[-2]
                    else "flat"
                )
                prev_dir = self._concept_trend_last_dir.get(con, "")
                if cur_dir == prev_dir and cur_dir != "flat":
                    self._concept_trend_continuity[con] += 1
                else:
                    self._concept_trend_continuity[con] = 1 if cur_dir != "flat" else 0
                    if cur_dir != "flat":
                        self._concept_trend_start[con] = now_str
                self._concept_trend_last_dir[con] = cur_dir

        # 落盘板块快照（原始市场快照已由 collector 写入）
        self._save_sector_snapshots(ind_changes, market_avg)

    def _save_sector_snapshots(self, ind_changes: dict, market_avg: float):
        """将本轮板块快照写入 sector_snapshots 表。"""
        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for ind, changes in ind_changes.items():
            if len(changes) < 3:
                continue
            avg = sum(changes) / len(changes)
            up = sum(1 for c in changes if c > 0)
            down = sum(1 for c in changes if c < 0)
            rows.append(
                (
                    self._trade_date,
                    now,
                    ind,
                    round(avg, 4),
                    up,
                    down,
                    round(market_avg, 4),
                )
            )

        if not rows:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            conn.executemany(
                """INSERT OR REPLACE INTO sector_snapshots
                   (trade_date, ts, sector_name, avg_change, up_count, down_count, market_avg)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def _load_sector_history(self):
        """从 sector_snapshots 恢复全天板块趋势历史。若无，从 raw market_snapshots 重建。"""
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT sector_name, ts, avg_change
                   FROM sector_snapshots
                   WHERE trade_date=?
                   ORDER BY ts""",
                (self._trade_date,),
            ).fetchall()
            conn.close()

            if rows:
                self._rebuild_from_sector_snapshots(rows)
                return

            # sector_snapshots 为空 → 从 raw market_snapshots 重建
            logger.info(
                "sector_snapshots 为空，尝试从 raw market_snapshots 重建板块趋势"
            )
            self._rebuild_from_market_snapshots()
        except Exception as e:
            logger.warning(f"恢复板块历史异常: {e}")

    def _rebuild_from_sector_snapshots(self, rows):
        """从已聚合的 sector_snapshots 恢复趋势历史。"""
        temp: dict[str, list[float]] = defaultdict(list)
        for sector_name, ts, avg_change in rows:
            temp[sector_name].append(avg_change)

        for sector, history in temp.items():
            self._sector_trend_history[sector] = history
            if len(history) >= 2:
                cur_dir = (
                    "up"
                    if history[-1] > history[-2]
                    else "down"
                    if history[-1] < history[-2]
                    else "flat"
                )
                self._sector_trend_last_dir[sector] = cur_dir
                cont = 1
                for i in range(len(history) - 2, 0, -1):
                    prev_dir = (
                        "up"
                        if history[i] > history[i - 1]
                        else "down"
                        if history[i] < history[i - 1]
                        else "flat"
                    )
                    if prev_dir == cur_dir and prev_dir != "flat":
                        cont += 1
                    else:
                        break
                self._sector_trend_continuity[sector] = cont

        logger.info(f"恢复 {len(temp)} 个板块趋势历史（{len(rows)} 条快照）")

    def _rebuild_from_market_snapshots(self):
        """从 raw market_snapshots 重建板块趋势历史。用于重启后恢复全天趋势。"""
        self._ensure_industry_cache()
        try:
            conn = sqlite3.connect(self.db_path)
            snapshots = conn.execute(
                """SELECT ts, code, change_pct FROM market_snapshots
                   WHERE trade_date=? ORDER BY ts""",
                (self._trade_date,),
            ).fetchall()
            conn.close()

            if not snapshots:
                logger.info("market_snapshots 也无数据，从零开始追踪")
                return

            # 按时间分组，每轮重建 sector 均值
            ts_groups: dict[str, dict[str, list[float]]] = defaultdict(
                lambda: defaultdict(list)
            )
            for ts, code, chg in snapshots:
                ind = self._industry_cache.get(code, "")
                if ind:
                    ts_groups[ts][ind].append(chg)

            # 按时间排序重建 history
            for ts in sorted(ts_groups.keys()):
                industries = ts_groups[ts]
                all_chgs = [c for changes in industries.values() for c in changes]
                if not all_chgs:
                    continue
                # 这一轮不需要 market_avg，只重建 history
                for ind, changes in industries.items():
                    if len(changes) < 3:
                        continue
                    avg = sum(changes) / len(changes)
                    self._sector_trend_history[ind].append(avg)

            # 重建连续性
            for ind, history in self._sector_trend_history.items():
                if len(history) >= 2:
                    cur_dir = (
                        "up"
                        if history[-1] > history[-2]
                        else "down"
                        if history[-1] < history[-2]
                        else "flat"
                    )
                    self._sector_trend_last_dir[ind] = cur_dir
                    cont = 1
                    for i in range(len(history) - 2, 0, -1):
                        prev_dir = (
                            "up"
                            if history[i] > history[i - 1]
                            else "down"
                            if history[i] < history[i - 1]
                            else "flat"
                        )
                        if prev_dir == cur_dir and prev_dir != "flat":
                            cont += 1
                        else:
                            break
                    self._sector_trend_continuity[ind] = cont

            logger.info(
                f"从 raw 快照恢复 {len(self._sector_trend_history)} 个板块趋势（{len(snapshots)} 条原始数据）"
            )
        except Exception as e:
            logger.warning(f"从 market_snapshots 重建板块趋势异常: {e}")

    def _cleanup_old_snapshots(self):
        """清理 3 天前的市场快照。"""
        try:
            from datetime import timedelta

            cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
            conn = sqlite3.connect(self.db_path)
            deleted_m = conn.execute(
                "DELETE FROM market_snapshots WHERE trade_date < ?", (cutoff,)
            ).rowcount
            deleted_s = conn.execute(
                "DELETE FROM sector_snapshots WHERE trade_date < ?", (cutoff,)
            ).rowcount
            conn.commit()
            conn.close()
            if deleted_m or deleted_s:
                logger.info(
                    f"清理旧快照: market_snapshots={deleted_m}, sector_snapshots={deleted_s}"
                )
        except Exception:
            pass

    def _restore_index_context(self):
        """从 index_snapshots DB 恢复全天指数走势上下文（盘中容灾重启用）。

        不再调 QMT，数据由 collector 写入 index_snapshots 表。
        """
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT ts, price, high, low, amount FROM index_snapshots
                   WHERE trade_date=? AND (index_code='000001.SH' OR index_code IS NULL)
                   ORDER BY ts ASC""",
                (self._trade_date,),
            ).fetchall()
            conn.close()

            if len(rows) < 5:
                logger.info("index_snapshots 数据不足，从当前扫描开始积累")
                return

            closes = []
            highs = []
            lows = []
            amounts = []
            max_ts = 0.0
            for ts_val, price, high, low, amount in rows:
                closes.append(price)
                if high and high > 0:
                    highs.append(high)
                if low and low > 0:
                    lows.append(low)
                if amount and amount > 0:
                    amounts.append(amount)
                if ts_val > max_ts:
                    max_ts = ts_val

            if closes:
                self._index_prices = closes
                self._index_high = max(highs) if highs else max(closes)
                self._index_low = min(lows) if lows else min(closes)
                # 存储收盘价最大/最小值用于健康检查交叉验证
                self._index_close_high = max(closes)
                self._index_close_low = min(closes)
                self._last_db_ts = max(self._last_db_ts, max_ts)
                logger.info(
                    f"从DB恢复指数走势: {len(closes)}条 "
                    f"高{self._index_high:.2f} 低{self._index_low:.2f} "
                    f"当前{closes[-1]:.2f} last_ts={max_ts:.1f}"
                )

            if amounts:
                self._market_turnovers = amounts

        except Exception as e:
            logger.warning(f"从DB恢复指数上下文异常: {e}")

    def _ensure_industry_cache(self):
        """加载代码→行业映射（懒加载一次）。"""
        if self._industry_cache:
            return
        try:
            import sqlite3 as _sql

            conn = _sql.connect(self.db_path)
            rows = conn.execute(
                """SELECT stock_code, industry FROM stock_basic
                   WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
            conn.close()
            self._industry_cache = {r[0]: (r[1] or "") for r in rows}
        except Exception:
            pass

    def _ensure_concept_cache(self):
        """加载代码→概念列表映射（懒加载一次）。"""
        if self._concept_cache:
            return
        try:
            import sqlite3 as _sql

            conn = _sql.connect(self.db_path)
            rows = conn.execute(
                """SELECT stock_code, concepts FROM stock_basic
                   WHERE trade_date = (SELECT MAX(trade_date) FROM stock_basic)"""
            ).fetchall()
            conn.close()
            for r in rows:
                concepts_str = (r[1] or "").strip()
                if concepts_str:
                    self._concept_cache[r[0]] = [
                        c.strip()
                        for c in concepts_str.replace("|", ",").split(",")
                        if c.strip()
                    ]
            logger.info(f"概念缓存加载: {len(self._concept_cache)} 只")
        except Exception as e:
            logger.warning(f"概念缓存加载失败: {e}")

    def _build_sector_context(self, codes: set[str]) -> str:
        """构建板块行情上下文（行业+概念日内实时数据），供 AI 换仓评估使用。"""
        self._ensure_industry_cache()
        self._ensure_concept_cache()

        # 收集涉及的行业和概念
        industries: dict[str, list[str]] = {}
        concepts_dict: dict[str, list[str]] = {}
        for code in codes:
            ind = self._industry_cache.get(code, "")
            if ind and ind in self._sector_stats:
                industries.setdefault(ind, []).append(code)
            for c in self._concept_cache.get(code, []):
                if c in self._concept_stats:
                    concepts_dict.setdefault(c, []).append(code)

        if not industries and not concepts_dict:
            return ""

        lines = ["板块行情（日内实时）："]

        if industries:
            lines.append("行业:")
            for ind_name in sorted(
                industries,
                key=lambda n: abs(self._sector_stats.get(n, {}).get("change_pct", 0)),
                reverse=True,
            ):
                s = self._sector_stats[ind_name]
                lines.append(
                    f"  {ind_name}: {s['change_pct']:+.2f}% 涨{s['up']}跌{s['down']}"
                )
            lines.append("")

        if concepts_dict:
            sorted_c = sorted(
                concepts_dict,
                key=lambda n: abs(self._concept_stats.get(n, {}).get("change_pct", 0)),
                reverse=True,
            )[:10]
            lines.append("概念（前10）:")
            for c_name in sorted_c:
                s = self._concept_stats[c_name]
                lines.append(
                    f"  {c_name}: {s['change_pct']:+.2f}% 涨{s['up']}跌{s['down']}"
                )
            lines.append("")

        return "\n".join(lines)

    def _get_concept_trend_score(self, code: str) -> tuple[int, str]:
        from trade.detect.sector_trend import get_concept_trend_score

        self._ensure_concept_cache()
        return get_concept_trend_score(code, self._concept_cache, self._concept_stats)

    def _get_sector_trend(self, code: str) -> str:
        from trade.detect.sector_trend import get_sector_trend

        self._ensure_industry_cache()
        self._ensure_concept_cache()
        return get_sector_trend(
            code,
            self._industry_cache,
            self._sector_stats,
            self._concept_cache,
            self._concept_stats,
        )

    def _detect_hot_sectors(self) -> list[dict]:
        """盘中检测热门板块，用于动态候选生成。

        综合评分维度：绝对涨幅、相对强度、宽度、连续性、量能、共振标签。
        返回热度前 5 的板块，每个包含 {name, change_pct, score, reason, ...}。
        """
        if not self._sector_stats:
            return []

        res_labels = getattr(self, "_last_resonance_labels", {})
        hot = []

        for name, stats in self._sector_stats.items():
            chg = stats.get("change_pct", 0)
            rel = stats.get("relative", 0)
            breadth = stats.get("breadth", 0)
            cont = stats.get("continuity", 0)
            vol = stats.get("vol_ratio", 1.0)
            history = stats.get("trend_history", [])

            if len(history) < 3:
                continue

            score = 0
            reasons = []

            if chg > 1.5:
                score += 2
                reasons.append(f"+{chg:.1f}%")
            elif chg > 0.8:
                score += 1
                reasons.append(f"+{chg:.1f}%")

            if rel > 1.0:
                score += 2
                reasons.append(f"强于大盘{rel:+.1f}%")
            elif rel > 0.5:
                score += 1

            if breadth > 0.4:
                score += 2
                reasons.append("普涨")
            elif breadth > 0.2:
                score += 1

            if cont >= 3:
                score += 2
                reasons.append(f"连续{cont}轮")
            elif cont >= 2:
                score += 1

            if vol > 1.3:
                score += 1
                reasons.append("放量")

            label = res_labels.get(name, "")
            if label in ("🔄逆势", "🟢共振"):
                score += 3
                reasons.append(label)

            if score >= settings.DYNAMIC_SECTOR_HEAT_THRESHOLD:
                hot.append(
                    {
                        "name": name,
                        "change_pct": chg,
                        "relative": rel,
                        "breadth": breadth,
                        "continuity": cont,
                        "vol_ratio": vol,
                        "score": score,
                        "reason": " ".join(reasons),
                        "resonance_label": label,
                    }
                )

        hot.sort(key=lambda x: -x["score"])
        top = hot[:5]
        if not top and self._sector_stats:
            # 有板块数据但无过阈值板块 → 输出最高分板块供诊断
            best = max(
                (
                    (n, s)
                    for n, s in self._sector_stats.items()
                    if len(s.get("trend_history", [])) >= 3
                ),
                key=lambda x: abs(x[1].get("change_pct", 0)),
                default=None,
            )
            if best:
                logger.info(
                    f"动态板块发现: 无板块过阈值(需≥{settings.DYNAMIC_SECTOR_HEAT_THRESHOLD}) "
                    f"涨幅最大: {best[0]} {best[1].get('change_pct', 0):+.1f}%"
                )
        return top

    def _detect_cooling_sectors(self) -> list[str]:
        """检测从热转冷的板块（用于换仓轮出）。

        返回板块名列表。
        """
        if not self._sector_stats:
            return []

        cooling = []
        for name, stats in self._sector_stats.items():
            history = stats.get("trend_history", [])
            if len(history) < 6:
                continue

            split = len(history) * 2 // 3
            early_avg = sum(history[:split]) / split if split > 0 else 0
            recent = history[split:]
            recent_avg = sum(recent) / len(recent) if recent else 0

            if (
                early_avg > 0.3
                and recent_avg < early_avg * 0.5
                or len(recent) >= 3
                and recent[-1] < recent[0] - 0.5
            ):
                cooling.append(name)

        return cooling
