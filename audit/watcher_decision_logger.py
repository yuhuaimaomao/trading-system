# -*- coding: utf-8 -*-
"""决策日志记录 — Mixin 方式混入 Watcher.

每笔关键决策写入 watcher_decision_log，供收盘后 RuleAuditor 回溯验证。
"""

from datetime import datetime

from system.utils.logger import get_audit_logger

logger = get_audit_logger("watcher")


class DecisionLoggerMixin:
    """向 watcher_decision_log 写入关键决策。日志失败不影响主流程。"""

    def _log_decision(
        self, decision_type: str, stock_code: str | None = None, **kwargs
    ):
        try:
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            self.repo.insert_decision_log(
                trade_date=self._trade_date,
                ts=ts,
                decision_type=decision_type,
                stock_code=stock_code,
                decision_data=kwargs,
            )
        except Exception:
            logger.warning(f"决策日志写入失败: {decision_type}", exc_info=True)

    # ---- 便捷方法 ----

    def _log_regime_change(
        self,
        pattern: str,
        confidence: str,
        prev_pattern: str,
        index_price: float,
        index_change: float,
        up_count: int,
        down_count: int,
        top_sectors: list,
        worst_sectors: list,
        **extra,
    ):
        self._log_decision(
            "regime_change",
            pattern=pattern,
            confidence=confidence,
            prev_pattern=prev_pattern,
            index_price=index_price,
            index_change=index_change,
            up_count=up_count,
            down_count=down_count,
            top_sectors=top_sectors,
            worst_sectors=worst_sectors,
            **extra,
        )

    def _log_buy_trigger(
        self,
        signal_id: int,
        stock_code: str,
        price: float,
        buy_min: float,
        buy_max: float,
        position_size: int,
        entry_rule: str,
        sector_trend: str,
        market_regime: str,
        **extra,
    ):
        self._log_decision(
            "buy_trigger",
            stock_code=stock_code,
            signal_id=signal_id,
            price=price,
            buy_zone_min=buy_min,
            buy_zone_max=buy_max,
            position_size=position_size,
            entry_rule=entry_rule,
            sector_trend=sector_trend,
            market_regime=market_regime,
            **extra,
        )

    def _log_buy_filter(
        self,
        signal_id: int,
        stock_code: str,
        entry_rule: str,
        reason_filtered: str,
        price: float,
        buy_min: float,
        buy_max: float,
        **extra,
    ):
        self._log_decision(
            "buy_filter",
            stock_code=stock_code,
            signal_id=signal_id,
            entry_rule=entry_rule,
            reason_filtered=reason_filtered,
            price=price,
            buy_zone_min=buy_min,
            buy_zone_max=buy_max,
            **extra,
        )

    def _log_stop_trigger(
        self,
        stock_code: str,
        stype: str,
        trigger_price: float,
        avg_cost: float,
        pnl_pct: float,
        risk_level: str,
        **extra,
    ):
        self._log_decision(
            "stop_trigger",
            stock_code=stock_code,
            type=stype,
            trigger_price=trigger_price,
            avg_cost=avg_cost,
            pnl_pct=pnl_pct,
            risk_level=risk_level,
            **extra,
        )

    def _log_tp_trigger(
        self,
        stock_code: str,
        stype: str,
        trigger_price: float,
        avg_cost: float,
        pnl_pct: float,
        **extra,
    ):
        self._log_decision(
            "tp_trigger",
            stock_code=stock_code,
            type=stype,
            trigger_price=trigger_price,
            avg_cost=avg_cost,
            pnl_pct=pnl_pct,
            **extra,
        )

    def _log_position_size(
        self,
        stock_code: str,
        amount: int,
        base_amount: int,
        reason: str,
        sector_mult: float,
        zone_mult: float,
        **extra,
    ):
        self._log_decision(
            "position_size",
            stock_code=stock_code,
            amount=amount,
            base_amount=base_amount,
            reason=reason,
            sector_mult=sector_mult,
            zone_mult=zone_mult,
            **extra,
        )

    def _log_exit_analysis(
        self,
        stock_code: str,
        holding_status: str,
        market_env: str,
        sector_trend: str,
        **extra,
    ):
        self._log_decision(
            "exit_analysis",
            stock_code=stock_code,
            holding_status=holding_status,
            market_env=market_env,
            sector_trend=sector_trend,
            **extra,
        )

    def _log_swap_eval(
        self, swap_decision: dict, candidate_codes: list, holding_codes: list, **extra
    ):
        self._log_decision(
            "swap_eval",
            swap_decision=swap_decision,
            candidate_codes=candidate_codes,
            current_holdings=holding_codes,
            **extra,
        )

    def _log_sector_alert(
        self,
        top_sectors: list,
        bottom_sectors: list,
        warnings: list,
        good: list,
        **extra,
    ):
        self._log_decision(
            "sector_alert",
            top_sectors=top_sectors,
            bottom_sectors=bottom_sectors,
            warnings=warnings,
            good=good,
            **extra,
        )

    def _log_resonance_alert(
        self,
        index_direction: str,
        index_change: float,
        resonance_down: list,
        counter_up: list,
        **extra,
    ):
        self._log_decision(
            "resonance_alert",
            index_direction=index_direction,
            index_change=index_change,
            resonance_down=resonance_down,
            counter_up=counter_up,
            **extra,
        )
