# -*- coding: utf-8 -*-
"""早盘简报：隔夜宏观 + 候选池确认 + 推送"""

import sqlite3
from datetime import datetime
from typing import Optional

from system.config.settings import DATABASE_PATH
from data.repo import TradeRepository
from system.utils.logger import get_task_logger


class MorningBrief:
    """早盘简报：隔夜宏观数据更新 + 候选池确认 + Telegram推送"""

    def __init__(self, telegram_bot=None):
        self.telegram = telegram_bot
        self.repo = TradeRepository()
        self.logger = get_task_logger('morning')

    def generate_and_send(self, trade_date: str = None):
        """完整流程：更新宏观数据 -> 加载候选信号 -> 构建简报 -> 推送"""
        if trade_date is None:
            trade_date = datetime.now().strftime('%Y-%m-%d')

        # 1. 更新宏观数据（静默失败，使用缓存）
        self._update_macro(trade_date)

        # 2. 从 DB 读取最新宏观数据
        macro = self._get_latest_macro()

        # 3. 加载待处理信号
        signals = self._get_pending_signals(trade_date)

        # 4. 构建简报文本
        brief = self._build_brief(trade_date, macro, signals)

        # 5. 推送
        try:
            self._send(brief)
        except Exception as e:
            self.logger.warning(f"简报推送失败: {e}")

        self.logger.info(f"早盘简报已生成 ({len(signals)} 个候选信号)")

    # ---- 宏观数据 ----

    def _update_macro(self, trade_date: str):
        """更新隔夜宏观数据。失败不抛异常，日志记录即可。"""
        try:
            from data.collectors.macro.macro_collector import MacroCollector
            collector = MacroCollector(timeout=15)
            collector.fetch_and_save()
            self.logger.info("隔夜宏观数据已更新")
        except Exception as e:
            self.logger.warning(f"宏观数据更新失败（将使用最新缓存）: {e}")

    def _get_latest_macro(self) -> dict:
        """从 macro_daily 表读取最新一条宏观数据"""
        conn = sqlite3.connect(DATABASE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM macro_daily ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else {}
        except Exception:
            self.logger.warning("读取宏观数据失败（表可能不存在或为空）")
            return {}
        finally:
            conn.close()

    # ---- 候选信号 ----

    def _get_pending_signals(self, trade_date: str) -> list[dict]:
        """获取当日待处理买入信号"""
        try:
            return self.repo.get_pending_signals(trade_date)
        except Exception as e:
            self.logger.warning(f"读取待处理信号失败: {e}")
            return []

    # ---- 简报构建 ----

    @staticmethod
    def _fmt_change(value, suffix="%") -> str:
        """格式化涨跌幅：+1.23% / -0.45% / --"""
        if value is None:
            return "--"
        return f"{value:+.2f}{suffix}"

    @staticmethod
    def _fmt_price(value, decimal=2) -> str:
        """格式化价格：1234.56 / --"""
        if value is None:
            return "--"
        return f"{value:.{decimal}f}"

    def _build_brief(self, trade_date: str, macro: dict, signals: list[dict]) -> str:
        """构建完整早盘简报文本"""
        lines = []
        lines.append(f"\U0001f4ca 早盘简报 {trade_date}")
        lines.append("")

        # === Section 1: 隔夜宏观 ===
        lines.append("【隔夜宏观】")

        macro_sections = []

        # US markets
        nasdaq_chg = macro.get("nasdaq_change")
        if nasdaq_chg is not None:
            macro_sections.append(
                f"\U0001f1fa\U0001f1f8 纳斯达克: {self._fmt_change(nasdaq_chg)}"
            )

        kweb_chg = macro.get("kweb_change")
        if kweb_chg is not None:
            macro_sections.append(
                f"\U0001f1fa\U0001f1f8 中概股(KWEB): {self._fmt_change(kweb_chg)}"
            )

        # A50
        a50_price = macro.get("a50_price")
        a50_chg = macro.get("a50_change")
        if a50_price is not None or a50_chg is not None:
            a50_parts = ["\U0001f1e8\U0001f1f3 A50期货:"]
            a50_parts.append(f" {self._fmt_price(a50_price, 2)}")
            if a50_chg is not None:
                a50_parts.append(f" ({self._fmt_change(a50_chg)})")
            macro_sections.append("".join(a50_parts))

        # 原油
        oil_price = macro.get("crude_oil_price")
        oil_chg = macro.get("crude_oil_change")
        if oil_price is not None or oil_chg is not None:
            oil_parts = ["\U0001f6e2️ WTI原油:"]
            oil_parts.append(f" {self._fmt_price(oil_price, 2)}")
            if oil_chg is not None:
                oil_parts.append(f" ({self._fmt_change(oil_chg)})")
            macro_sections.append("".join(oil_parts))

        # 黄金
        gold_price = macro.get("gold_price")
        gold_chg = macro.get("gold_change")
        if gold_price is not None or gold_chg is not None:
            gold_parts = ["\U0001f4bf 黄金:"]
            gold_parts.append(f" {self._fmt_price(gold_price, 2)}")
            if gold_chg is not None:
                gold_parts.append(f" ({self._fmt_change(gold_chg)})")
            macro_sections.append("".join(gold_parts))

        # 汇率
        usd_cny = macro.get("usd_cny_rate")
        if usd_cny is not None:
            macro_sections.append(
                f"\U0001f4b1 美元/人民币: {self._fmt_price(usd_cny, 4)}"
            )

        if macro_sections:
            lines.extend(macro_sections)
        else:
            lines.append("  暂无宏观数据")
        lines.append("")

        # === Section 2: 候选池确认 ===
        lines.append(f"【候选池确认】({len(signals)}只)")

        if not signals:
            lines.append("  无待处理买入信号")
        else:
            for i, sig in enumerate(signals, 1):
                code = sig.get("stock_code", "--")
                name = sig.get("stock_name", "--")
                zone_min = sig.get("buy_zone_min")
                zone_max = sig.get("buy_zone_max")
                sl = self._fmt_price(sig.get("stop_loss"))
                tp = self._fmt_price(sig.get("take_profit"))
                score = sig.get("signal_score", "")

                zone_str = (
                    f"{self._fmt_price(zone_min)}-{self._fmt_price(zone_max)}"
                    if zone_min is not None or zone_max is not None
                    else "--"
                )

                line_parts = [f"  {i}. {code} {name}"]
                line_parts.append(f"| 买入区间 {zone_str}")
                line_parts.append(f"| 止损{sl} 止盈{tp}")
                if score:
                    line_parts.append(f"| 评分{score}")
                lines.append(" ".join(line_parts))
        lines.append("")

        # === Section 3: 今日关注 ===
        lines.append("【今日关注】")

        if signals:
            lines.append(f"  - 重点监控上述 {len(signals)} 只标的的买入区间触发情况")
        else:
            lines.append("  - 暂无候选交易信号，维持现有持仓监控")

        # 隔夜变化影响
        if a50_chg is not None:
            direction = "上涨" if a50_chg > 0 else "下跌"
            lines.append(f"  - 关注隔夜A50{direction}({a50_chg:+.2f}%)对开盘的影响")
        if oil_chg is not None:
            direction = "上涨" if oil_chg > 0 else "下跌"
            lines.append(f"  - WTI原油隔夜{direction}({oil_chg:+.2f}%)")

        lines.append("")

        return "\n".join(lines)

    # ---- 推送 ----

    def _send(self, brief: str):
        """推送简报。优先 Telegram，降级到 print。"""
        if self.telegram:
            try:
                self.telegram.send(brief)
                self.logger.info("早盘简报已推送至 Telegram")
            except Exception as e:
                self.logger.warning(f"Telegram 推送失败: {e}")
                self._fallback_print(brief)
        else:
            self._fallback_print(brief)

    @staticmethod
    def _fallback_print(brief: str):
        """控制台输出降级"""
        print("\n" + "=" * 60)
        print("【早盘简报】")
        print("=" * 60)
        print(brief)
