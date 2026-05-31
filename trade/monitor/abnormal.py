"""异动检测 + 换仓评估 + 板块热度。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import logging
import time
from datetime import datetime

from system.config import settings

logger = logging.getLogger(__name__)


class AbnormalMonitorMixin:
    """异动检测 + 换仓评估 + 板块热度。"""

    def _check_sector_heat(self, snapshot: dict[str, dict]):
        monitor = self._get_sector_monitor()
        if monitor is None:
            return
        try:
            messages = monitor.check(snapshot)
            for msg in messages:
                self._alert(msg)
        except Exception as e:
            logger.warning(f"板块热度检查异常: {e}")

    def _get_sector_monitor(self):
        if self._sector_monitor is None:
            try:
                from trade.monitor.sector_heat import SectorHeatMonitor

                self._sector_monitor = SectorHeatMonitor(
                    db_path=self.db_path,
                    telegram_bot=self.telegram,
                )
            except Exception as e:
                logger.warning(f"板块热度监控器初始化失败: {e}")
        return self._sector_monitor

    # ======================== 第三层：异动检测 ========================

    def _check_abnormal(self, prices: dict[str, float]):
        detector = self._get_abnormal_detector()
        if detector is None:
            return
        try:
            if self._market_snapshot:
                current_snapshot = self._market_snapshot
            else:
                current_snapshot = self._build_market_snapshot(prices)
            messages = detector.detect_sector(current_snapshot, self._prev_snapshot)
            self._prev_snapshot = current_snapshot
            if messages:
                self._alert("\n".join(messages))
        except Exception as e:
            logger.warning(f"异动检测异常: {e}")

    def _evaluate_swaps(self, prices: dict[str, float]):
        """每15分钟主动评估换仓：AI 实时判断是否卖出某持仓换入候选。"""
        pt = self._get_paper_trader()
        if not pt or len(pt.portfolio.positions) < 3:
            return

        try:
            signals = self.repo.get_pending_signals(account="paper")
        except Exception as e:
            logger.warning(f"换仓评估获取信号失败: {e}")
            return

        candidates = []
        for s in signals:
            code = s["stock_code"]
            price = prices.get(code)
            if price is None:
                continue
            buy_min = s.get("buy_zone_min") or 0
            buy_max = s.get("buy_zone_max") or 0
            if buy_min <= 0:
                continue
            in_or_near = buy_min * 0.95 <= price <= buy_max
            if in_or_near:
                snap = (
                    self._market_snapshot.get(code, {}) if self._market_snapshot else {}
                )
                industry = (
                    self._industry_cache.get(code, "")
                    if hasattr(self, "_industry_cache")
                    else ""
                )
                sec_trend = ""
                if industry and hasattr(self, "_sector_trend_history"):
                    history = self._sector_trend_history.get(industry, [])
                    if history:
                        sec_trend = f"{history[-1]:+.1f}%"
                candidates.append(
                    {
                        "code": code,
                        "name": s.get("stock_name", ""),
                        "price": price,
                        "change_pct": snap.get("changePct", 0),
                        "score": s.get("signal_score", 0) or 0,
                        "sl": s.get("stop_loss", 0) or 0,
                        "tp": s.get("take_profit", 0) or 0,
                        "buy_min": buy_min,
                        "buy_max": buy_max,
                        "sector": industry,
                        "sector_trend": sec_trend,
                    }
                )
                concepts = self._concept_cache.get(code, [])
                if concepts and self._concept_stats:
                    top = sorted(
                        concepts,
                        key=lambda c: abs(
                            self._concept_stats.get(c, {}).get("change_pct", 0)
                        ),
                        reverse=True,
                    )[:3]
                    candidates[-1]["concepts"] = top

        if not candidates:
            return

        ctx = ""
        if self._index_prices and len(self._index_prices) >= 2:
            idx_chg = (
                (self._index_prices[-1] - self._index_prices[-2])
                / self._index_prices[-2]
                * 100
            )
            ctx = f"上证指数 日内变动{idx_chg:+.2f}%"

        price_info = {}
        if self._market_snapshot:
            for code, pos in pt.portfolio.positions.items():
                snap = self._market_snapshot.get(code, {})
                info = {"change_pct": snap.get("changePct", 0)}
                industry = pos.sector_code
                if industry and hasattr(self, "_sector_trend_history"):
                    history = self._sector_trend_history.get(industry, [])
                    if history:
                        info["sector_trend"] = f"{history[-1]:+.1f}%"
                concepts = self._concept_cache.get(code, [])
                if concepts and self._concept_stats:
                    top = sorted(
                        concepts,
                        key=lambda c: abs(
                            self._concept_stats.get(c, {}).get("change_pct", 0)
                        ),
                        reverse=True,
                    )[:3]
                    info["concepts"] = top
                price_info[code] = info

        all_codes = set(c["code"] for c in candidates) | set(
            pt.portfolio.positions.keys()
        )
        sector_context = self._build_sector_context(all_codes)

        logger.info(
            f"主动换仓评估: {len(candidates)} 个候选, {len(pt.portfolio.positions)} 个持仓"
        )
        try:
            swapped = pt.evaluate_swaps(
                candidates,
                market_context=ctx,
                price_info=price_info,
                sector_context=sector_context,
            )
            if swapped:
                self._invalidate_watch_codes_cache()
        except Exception as e:
            logger.warning(f"换仓评估异常: {e}")

    def _get_abnormal_detector(self):
        if self._abnormal_detector is None:
            self._abnormal_detector = AbnormalDetector()
        return self._abnormal_detector

    @staticmethod
    def _build_market_snapshot(prices: dict[str, float]) -> dict:
        """将当前价格字典转为 snapshot 格式供异动检测器使用。"""
        return {
            code: {"price": p, "timestamp": time.time()} for code, p in prices.items()
        }

    # ======================== 收盘收尾 ========================


class AbnormalDetector:
    """盘中异动检测器 — 急速拉升 / 量比暴增 / 逼近涨停。"""

    def detect_sector(self, current: dict, previous: dict) -> list[str]:
        """对比两轮快照，返回异动告警消息列表。"""
        alerts = []
        if not current or not previous:
            return alerts

        rapid_rise = getattr(settings, "ABNORMAL_RAPID_RISE_PCT", 1.0)
        vol_surge = getattr(settings, "ABNORMAL_VOLUME_SURGE_RATIO", 3.0)
        near_limit = getattr(settings, "ABNORMAL_NEAR_LIMIT_PCT", 7.0)

        rapid_list = []
        vol_list = []
        limit_list = []

        for code, info in current.items():
            prev = previous.get(code, {})
            cur_chg = float(info.get("changePct", 0))
            prev_chg = float(prev.get("changePct", 0))

            if cur_chg - prev_chg > rapid_rise:
                rapid_list.append(f"{code} {cur_chg - prev_chg:+.1f}%")

            cur_vol = float(info.get("amount", 0))
            prev_vol = float(prev.get("amount", 0))
            if prev_vol > 0 and cur_vol > prev_vol * vol_surge:
                vol_list.append(f"{code} {cur_vol / prev_vol:.0f}×")

            if cur_chg >= near_limit and prev_chg < near_limit:
                limit_list.append(f"{code} {cur_chg:+.1f}%")

        now = datetime.now().strftime("%H:%M")
        if rapid_list:
            alerts.append(f"🏭 急速拉升 {now}\n   " + " ".join(rapid_list))
        if vol_list:
            alerts.append(f"📊 量比暴增 {now}\n   " + " ".join(vol_list))
        if limit_list:
            alerts.append(f"🔥 逼近涨停 {now}\n   " + " ".join(limit_list))

        return alerts
