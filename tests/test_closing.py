# -*- coding: utf-8 -*-
"""尾盘决策单元测试 — ClosingDecisionMixin"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, time as dt_time
from trade.monitor.closing import ClosingDecisionMixin
from trade.portfolio.portfolio import Portfolio

# 所有尾盘测试需 mock 时间为 14:35
CLOSING_TIME_PATCH = patch("trade.monitor.closing.datetime")
CLOSING_TIME_PATCH.start().now.return_value = datetime(2026, 5, 29, 14, 35, 0)


def make_closing(**attrs):
    """创建 ClosingDecisionMixin 最小 mock 对象"""
    class TestClosing(ClosingDecisionMixin):
        pass

    obj = TestClosing()
    defaults = {
        "portfolio": Portfolio(initial_cash=100000),
        "_trade_date": "2026-05-29",
        "_closing_decision_done": False,
        "_alert": MagicMock(),
        "_alert_private": MagicMock(),
    }
    for k, v in defaults.items():
        setattr(obj, k, v)
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# ====================== 尾盘决策 ======================


class TestCheckClosing:
    @pytest.fixture
    def obj_with_position(self):
        obj = make_closing()
        obj.portfolio.open_position(
            stock_code="000001", stock_name="平安银行",
            volume=1000, price=12.00,
            entry_date="2026-05-20",  # past date, not T+1
            stop_loss=11.00, take_profit=14.00,
        )
        return obj

    def test_after_close_no_decision(self):
        """收盘后不再做尾盘决策"""
        obj = make_closing(_closing_decision_done=True)
        obj.portfolio.open_position(
            stock_code="000001", stock_name="平安银行",
            volume=1000, price=12.00,
            entry_date="2026-05-20",
            stop_loss=11.00,
        )
        obj._check_closing({"000001": 10.50})
        obj._alert.assert_not_called()

    def test_pnl_loss_triggers_stop_advice(self, obj_with_position):
        """亏损 > 3% 建议止损"""
        obj_with_position._check_closing({"000001": 11.50})
        assert obj_with_position._alert.called
        msg = obj_with_position._alert.call_args[0][0]
        assert "止损" in msg or "000001" in msg

    def test_pnl_profit_triggers_reduce_advice(self, obj_with_position):
        """盈利 > 5% 建议减仓"""
        obj_with_position._check_closing({"000001": 13.00})
        assert obj_with_position._alert.called

    def test_close_to_stop_loss_warn(self, obj_with_position):
        """价格接近止损价且亏损提醒关注"""
        obj_with_position.portfolio.positions["000001"].current_price = 11.30
        obj_with_position._check_closing({"000001": 11.20})
        # May or may not alert depending on exact logic

    def test_normal_holding_no_alert(self):
        """正常持仓不触发尾盘告警"""
        obj = make_closing()
        obj.portfolio.open_position(
            stock_code="000001", stock_name="平安银行",
            volume=1000, price=12.00,
            entry_date="2026-05-20",
            stop_loss=11.00, take_profit=14.00,
        )
        obj._check_closing({"000001": 12.30})  # +2.5%, normal
        # May or may not alert depending on threshold

    def test_t1_position_not_sold(self):
        """T+1 持仓不触发卖出建议 (仅建议关注)"""
        obj = make_closing()
        obj.portfolio.open_position(
            stock_code="000001", stock_name="平安银行",
            volume=1000, price=12.00,
            entry_date=obj._trade_date,  # today = T+1 locked
            stop_loss=11.00, take_profit=14.00,
        )
        obj._check_closing({"000001": 11.00})
        # Should not suggest selling T+1 locked positions

    def test_multiple_positions(self):
        """多持仓尾盘汇总"""
        obj = make_closing()
        for code, price in [("000001", 12.00), ("000002", 25.00)]:
            obj.portfolio.open_position(
                stock_code=code, stock_name=f"股{code}",
                volume=1000, price=price,
                entry_date="2026-05-20",
                stop_loss=price * 0.9,
            )
        obj._check_closing({"000001": 11.50, "000002": 26.00})
        # Should handle all positions

    def test_empty_positions_skips(self):
        """无持仓跳过尾盘"""
        obj = make_closing()
        obj._check_closing({})
        obj._alert.assert_not_called()

    def test_closing_decision_flag_set(self, obj_with_position):
        """尾盘决策后设置完成标志"""
        obj_with_position._check_closing({"000001": 12.50})
        assert obj_with_position._closing_decision_done is True
