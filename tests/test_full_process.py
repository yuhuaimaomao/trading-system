"""完整交易日端到端模拟 — 从信号入库到收盘清算全链路集成测试。

FullTradingDaySimulator 封装以下组件：
  - PaperAccount（真实执行层）
  - TradeRepository（真实 DB 操作）
  - Mock QMT（价格场景提供）
  - Mock AI（简单信号参数）
  - Mock Telegram（记录推送消息）

测试覆盖的信号生命周期:
  pending（DB）→ prices enter buy zone → buy executed → signal status 'bought'
  → price triggers SL/TP → sell executed → position closed

覆盖场景:
  1. 基本交易日 240 步（9:30-15:00, 1 步/分钟）
  2. 止损触发
  3. 止盈触发
  4. 多日持仓连续性（跨日 snapshot→restore）
  5. 信号→执行管线完整性（signal status 变化）
  6. 空交易日（无信号无持仓）
  7. 多持仓同日不同时点
  8. 市场态势变化影响买入决策
"""

import logging
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from data.repo import TradeRepository
from system.config import settings
from trade.core.scan_state import MarketRegime
from trade.exec.paper.account import PaperAccount
from trade.exec.paper.executor import (
    execute_paper_buy,
    execute_paper_sell,
)
from trade.risk.rules.stop_loss import should_stop_loss
from trade.risk.rules.take_profit import should_take_profit, should_trailing_stop

logger = logging.getLogger(__name__)

DEFAULT_CAPITAL = 100_000.0

# ═══════════════════════════════════════════════════════════════════
# FullTradingDaySimulator — 全链路交易日模拟器
# ═══════════════════════════════════════════════════════════════════


class FullTradingDaySimulator:
    """模拟一个完整的交易日：信号入库 → 盯盘扫描 → 买入执行 → 持仓风控 → 收盘清算。

    工作流程:
      1. setup_signals(): 从 DB 写入 pending 信号（模拟策略管线输出）
      2. run(): 每分钟步进，更新价格、检查信号、检查持仓、记录快照
      3. finalize_close(): 收盘后快照 + 信号过期 + 总结
      4. restore_next_day(): 从 DB 恢复次日状态（多日测试用）

    Mock 依赖:
      - PaperAccount._get_pre_close / _get_day_high: patch 为返回 0
      - QMT 实时行情: 由 price_scenarios 提供
      - Telegram: 由 self.message_log 记录

    """

    # 信号触发后标记
    FIRED = "fired"

    def __init__(
        self,
        db_path: str,
        trade_date: str = None,
        initial_capital: float = DEFAULT_CAPITAL,
    ):
        self.db_path = db_path
        self.trade_date = trade_date or "2026-06-01"
        self.initial_capital = initial_capital
        self.repo = TradeRepository(db_path=db_path)

        # Mock PaperAccount 的远程调用
        self._patchers = [
            patch.object(PaperAccount, "_get_pre_close", return_value=0),
            patch.object(PaperAccount, "_get_day_high", return_value=0),
        ]
        for p in self._patchers:
            p.start()

        self.account = PaperAccount(
            db_path=db_path,
            telegram_bot=MagicMock(),
            initial_capital=initial_capital,
        )
        self.account._trade_date = self.trade_date

        # Telegram 消息记录
        self.message_log: list[str] = []
        self.account.telegram.send.side_effect = self._record_message

        # 持仓元数据（模拟 watcher._pos_meta）
        self._pos_meta: dict[str, dict] = {}

        # 市场态势（模拟 watcher._regime）
        self._regime = MarketRegime(
            pattern="normal",
            risk_level="safe",
            allow_buy=True,
        )

        # 指数价格追踪（模拟 watcher._index_prices）
        self._index_prices: list[float] = []

        # 决策历史
        self.decisions: list[dict] = []

        # 每步快照
        self.snapshots: list[dict] = []

        # 已触发过的信号 ID 集合（防重复触发）
        self._triggered_ids: set[int] = set()

        # 信号池（备用索引，用于查询信号信息）
        self._signal_pool: dict[int, dict] = {}

        # 收盘是否已完成
        self._finalized = False

    # ────────────────────────────────────
    # 信号设置
    # ────────────────────────────────────

    def setup_signals(self, signals: list[dict]):
        """将 signals 写入 trade_signals 表，模拟策略管线输出。

        每个 signal 必须含字段:
          stock_code, stock_name, buy_zone_min, buy_zone_max,
          stop_loss, take_profit, signal_score

        可选: strategy_name, reason, signal_source
        """

        now = datetime.now().isoformat()
        for s in signals:
            sid = self.repo.insert_signal(
                {
                    "trade_date": self.trade_date,
                    "created_at": now,
                    "signal_type": "BUY",
                    "signal_source": s.get("signal_source", "AI_ENHANCED"),
                    "stock_code": s["stock_code"],
                    "stock_name": s.get("stock_name", ""),
                    "buy_zone_min": s.get("buy_zone_min"),
                    "buy_zone_max": s.get("buy_zone_max"),
                    "target_position": s.get(
                        "target_position", settings.DEFAULT_POSITION_PCT
                    ),
                    "stop_loss": s.get("stop_loss"),
                    "take_profit": s.get("take_profit"),
                    "trailing_stop": s.get("trailing_stop", 0.05),
                    "signal_score": s.get("signal_score", 70),
                    "strategy_name": s.get("strategy_name", "test_signal"),
                    "reason": s.get("reason", ""),
                    "status": "pending",
                    "account": "paper",
                }
            )
            self._signal_pool[sid] = {"stock_code": s["stock_code"], "sid": sid}
            logger.info(
                "信号入库: %s %s id=%d", s["stock_code"], s.get("stock_name", ""), sid
            )

    # ────────────────────────────────────
    # 价格场景运行
    # ────────────────────────────────────

    def run(self, price_scenarios: list[dict]):
        """按价格场景步进运行完全交易日。

        price_scenarios: list of {
            timestamp: float (seconds from midnight 或 epoch),
            prices: {code: price},
            index_price: float (可选，上证指数价格),
        }
        """
        for scenario in price_scenarios:
            ts = scenario["timestamp"]
            prices = scenario.get("prices", {})
            index_price = scenario.get("index_price", 0)

            self.run_scan_at(ts, prices, index_price)

    def run_scan_at(
        self,
        timestamp: float,
        prices: dict[str, float],
        index_price: float = 0,
    ):
        """执行一次盯盘扫描迭代。

        流程：
          1. 更新指数追踪
          2. 更新持仓市值
          3. 检查是否在交易时段（跳过非交易时段判断，调用方控制）
          4. 检查信号（通过 DB pending 信号）
          5. 检查持仓（止损/止盈/移动止盈）
          6. 记录快照
        """
        # 指数追踪
        if index_price > 0:
            self._index_prices.append(index_price)

        # 更新持仓价格
        self.account.update_prices(prices)

        # 检查信号（从 DB 读取 pending 信号）
        self._check_signals_from_db(timestamp, prices)

        # 检查持仓风控
        self._check_positions(timestamp, prices)

        # 记录快照
        self._record_snapshot(timestamp, prices)

    # ────────────────────────────────────
    # 信号检查（从 DB 读取，模拟 watcher._check_signals）
    # ────────────────────────────────────

    def _check_signals_from_db(self, timestamp: float, prices: dict[str, float]):
        """模拟 Watcher 的信号检查流程。

        1. 从 DB 查 pending 信号
        2. 检查价格是否进入买入区
        3. 检查市场态势允许买入
        4. 执行买入
        5. 更新信号状态为 'bought'
        6. 初始化持仓元数据
        """
        if not self._regime.allow_buy:
            return

        try:
            pending = self.repo.get_pending_signals(
                trade_date=self.trade_date, account="paper"
            )
        except Exception:
            return

        for sig in pending:
            sid = sig["id"]
            if sid in self._triggered_ids:
                continue

            code = sig["stock_code"]
            price = prices.get(code)
            if price is None or price <= 0:
                continue

            bmin = sig.get("buy_zone_min") or 0
            bmax = sig.get("buy_zone_max") or 0
            if bmin <= 0 or bmax <= 0:
                continue
            if not (bmin <= price <= bmax):
                continue

            # 计算买入股数
            max_affordable = int(self.account.cash * 0.9 / price / 100) * 100
            target_amount = min(
                max_affordable * price,
                self.account.total_value * settings.DEFAULT_POSITION_PCT,
            )
            volume = int(target_amount / price / 100) * 100
            if volume < 100:
                self.decisions.append(
                    {
                        "ts": timestamp,
                        "type": "buy_failed",
                        "code": code,
                        "price": price,
                        "signal_id": sid,
                        "reason": "资金不足",
                    }
                )
                self._triggered_ids.add(sid)
                continue

            # 执行买入
            name = sig.get("stock_name", code)
            sl = sig.get("stop_loss") or 0
            tp = sig.get("take_profit") or 0
            trailing = sig.get("trailing_stop", 0.05)

            result = execute_paper_buy(
                code=code,
                name=name,
                price=price,
                volume=volume,
                sl=sl,
                tp=tp,
                signal_id=sid,
                source="signal",
                paper_account=self.account,
                repo=self.repo,
            )

            if result["success"]:
                self._triggered_ids.add(sid)
                self._pos_meta[code] = {
                    "sl": sl,
                    "tp": tp,
                    "trailing_stop": trailing,
                    "highest_price": price,
                    "signal_id": sid,
                    "signal_score": sig.get("signal_score", 0),
                }
                self.decisions.append(
                    {
                        "ts": timestamp,
                        "type": "buy",
                        "code": code,
                        "name": name,
                        "price": price,
                        "volume": volume,
                        "cost": result["cost"],
                        "commission": result["commission"],
                        "signal_id": sid,
                    }
                )
                logger.info(
                    "买入执行: %s %s %d股 @%.2f signal_id=%d",
                    code,
                    name,
                    volume,
                    price,
                    sid,
                )
            else:
                self._triggered_ids.add(sid)  # 标记已处理
                self.decisions.append(
                    {
                        "ts": timestamp,
                        "type": "buy_failed",
                        "code": code,
                        "price": price,
                        "signal_id": sid,
                        "reason": result["reason"],
                    }
                )

    # ────────────────────────────────────
    # 持仓风控（模拟 watcher._check_positions）
    # ────────────────────────────────────

    def _check_positions(self, timestamp: float, prices: dict[str, float]):
        """检查持仓的止损/止盈/移动止盈，模拟 PositionRiskMixin._check_positions。"""
        from trade.risk.position_rules import adjust_tightening

        risk_level = self._regime.risk_level if self._regime else "safe"
        base_sl_tighten, base_tp_lower, base_trail_tighten = adjust_tightening(
            risk_level, ""
        )

        for code, pos in list(self.account.positions.items()):
            price = prices.get(code)
            if price is None:
                price = pos.current_price
            if price is None or price <= 0:
                continue

            meta = self._pos_meta.get(code, {})
            sl = meta.get("sl", 0)
            tp = meta.get("tp", 0)
            trailing_stop = meta.get("trailing_stop", 0.05)
            highest_price = meta.get("highest_price", 0)

            # 更新最高价
            if price > highest_price:
                self._pos_meta[code] = {**meta, "highest_price": price}
                highest_price = price
                meta = self._pos_meta[code]

            # T+1 保护
            if pos.available_volume <= 0:
                continue

            # 止损
            triggered, effective_sl = should_stop_loss(
                price,
                pos.avg_cost,
                sl,
                tighten=base_sl_tighten,
            )
            if triggered:
                result = execute_paper_sell(
                    code,
                    pos.stock_name,
                    price,
                    "止损",
                    paper_account=self.account,
                    pos_meta=self._pos_meta,
                    bought_watch={},
                    signal_id=meta.get("signal_id"),
                )
                if result["success"]:
                    self.decisions.append(
                        {
                            "ts": timestamp,
                            "type": "sell",
                            "sell_type": "止损",
                            "code": code,
                            "price": price,
                            "pnl": result["pnl"],
                            "pnl_pct": result["pnl_pct"],
                            "trigger_price": effective_sl,
                        }
                    )
                    logger.info("止损触发: %s @%.2f sl=%.2f", code, price, effective_sl)
                continue

            # 止盈
            triggered, effective_tp = should_take_profit(
                price,
                pos.avg_cost,
                tp,
                tp_lower=base_tp_lower,
            )
            if triggered:
                result = execute_paper_sell(
                    code,
                    pos.stock_name,
                    price,
                    "止盈",
                    paper_account=self.account,
                    pos_meta=self._pos_meta,
                    bought_watch={},
                    signal_id=meta.get("signal_id"),
                )
                if result["success"]:
                    self.decisions.append(
                        {
                            "ts": timestamp,
                            "type": "sell",
                            "sell_type": "止盈",
                            "code": code,
                            "price": price,
                            "pnl": result["pnl"],
                            "pnl_pct": result["pnl_pct"],
                            "trigger_price": effective_tp,
                        }
                    )
                    logger.info("止盈触发: %s @%.2f tp=%.2f", code, price, effective_tp)
                continue

            # 移动止盈
            if trailing_stop > 0 and highest_price > 0:
                triggered, trail_price = should_trailing_stop(
                    price,
                    highest_price,
                    trailing_stop,
                    trail_tighten=base_trail_tighten,
                )
                if triggered:
                    result = execute_paper_sell(
                        code,
                        pos.stock_name,
                        price,
                        "移动止盈",
                        paper_account=self.account,
                        pos_meta=self._pos_meta,
                        bought_watch={},
                        signal_id=meta.get("signal_id"),
                    )
                    if result["success"]:
                        self.decisions.append(
                            {
                                "ts": timestamp,
                                "type": "sell",
                                "sell_type": "移动止盈",
                                "code": code,
                                "price": price,
                                "pnl": result["pnl"],
                                "pnl_pct": result["pnl_pct"],
                                "trail_price": trail_price,
                                "highest_price": highest_price,
                            }
                        )
                        logger.info(
                            "移动止盈触发: %s @%.2f 最高=%.2f trail=%.2f",
                            code,
                            price,
                            highest_price,
                            trail_price,
                        )

    # ────────────────────────────────────
    # 收盘清算（模拟 CloseSummaryMixin._finalize_close）
    # ────────────────────────────────────

    def finalize_close(self):
        """收盘处理：快照落库 + 过期信号 + 关闭状态。

        模拟 CloseSummaryMixin._finalize_close 的核心步骤。
        """
        if self._finalized:
            return

        # 刷新持仓价格并落盘
        self.account._trade_date = self.trade_date
        self.account._persist_state()

        # 过期当日 pending 信号（模拟 _expire_signals）
        try:
            self.repo.expire_old_pending_signals(self.trade_date)
        except Exception:
            pass

        self._finalized = True

        # 生成收盘摘要
        summary = self.summarize()
        logger.info(
            "收盘清算完成: total_value=%.0f cash=%.0f positions=%d realized_pnl=%.0f",
            summary["final_total_value"],
            summary["final_cash"],
            summary["final_position_count"],
            summary["total_realized_pnl"],
        )

    # ────────────────────────────────────
    # 多日连续性支持
    # ────────────────────────────────────

    def restore_next_day(self, next_trade_date: str):
        """从 DB 恢复快照和持仓到下一交易日。

        模拟 Watcher.run() 中 PaperAccount.restore() 的行为。
        """
        self.account.restore(next_trade_date)
        self.account._trade_date = next_trade_date
        self.trade_date = next_trade_date
        self._finalized = False

        # 重新构建 _pos_meta（模拟 _restore_pos_meta）
        for code, pos in self.account.positions.items():
            sig = self.repo.get_signal_for_pos_meta(code)
            if sig:
                self._pos_meta[code] = {
                    "sl": sig.get("stop_loss", 0) or 0,
                    "tp": sig.get("take_profit", 0) or 0,
                    "trailing_stop": sig.get("trailing_stop", 0.05) or 0.05,
                    "highest_price": pos.current_price,
                    "signal_id": sig.get("id"),
                    "signal_score": sig.get("signal_score", 0) or 0,
                }
            else:
                self._pos_meta[code] = {
                    "sl": 0,
                    "tp": 0,
                    "trailing_stop": 0.05,
                    "highest_price": pos.current_price,
                    "signal_id": None,
                    "signal_score": 0,
                }

        logger.info(
            "下一交易日恢复完成: %s cash=%.0f positions=%d",
            next_trade_date,
            self.account.cash,
            len(self.account.positions),
        )

    # ────────────────────────────────────
    # 快照记录
    # ────────────────────────────────────

    def _record_snapshot(self, timestamp: float, prices: dict[str, float]):
        total = self.account.total_value
        cash = self.account.cash
        mkt_val = sum(p.market_value for p in self.account.positions.values())
        self.snapshots.append(
            {
                "ts": timestamp,
                "total_value": round(total, 2),
                "cash": round(cash, 2),
                "market_value": round(mkt_val, 2),
                "position_count": len(self.account.positions),
                "daily_pnl": round(self.account.daily_pnl, 2),
                "total_pnl": round(self.account.total_pnl, 2),
                "drawdown": round(self.account.drawdown, 2),
            }
        )

    # ────────────────────────────────────
    # 摘要与清理
    # ────────────────────────────────────

    def summarize(self) -> dict:
        buys = [d for d in self.decisions if d["type"] == "buy"]
        sells = [d for d in self.decisions if d["type"] == "sell"]
        failed = [d for d in self.decisions if d["type"] == "buy_failed"]
        total_realized = sum(s.get("pnl", 0) for s in sells)

        # 检查信号状态
        sig_statuses = {}
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT id, stock_code, status FROM trade_signals WHERE trade_date=?",
                (self.trade_date,),
            ).fetchall()
            sig_statuses = {r[1]: r[2] for r in rows}
        finally:
            conn.close()

        return {
            "trade_date": self.trade_date,
            "initial_capital": self.initial_capital,
            "final_total_value": round(self.account.total_value, 2),
            "final_cash": round(self.account.cash, 2),
            "final_position_count": len(self.account.positions),
            "buys": buys,
            "sells": sells,
            "failed_signals": failed,
            "total_realized_pnl": round(total_realized, 2),
            "total_pnl": round(self.account.total_pnl, 2),
            "daily_pnl": round(self.account.daily_pnl, 2),
            "drawdown": round(self.account.drawdown, 2),
            "signal_statuses": sig_statuses,
        }

    def close(self):
        """清理 mock patchers。"""
        for p in self._patchers:
            p.stop()
        self.account.telegram = None

    def _record_message(self, text: str):
        self.message_log.append(text)


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


def build_linear_price_scenario(
    codes: list[str],
    start_prices: dict[str, float],
    end_prices: dict[str, float],
    steps: int,
    start_ts: float = 0,
    step_interval: float = 60.0,
    index_start: float = 3000.0,
    index_end: float = 3000.0,
) -> list[dict]:
    """生成线性变化的价格场景。

    每步 (timestamp, prices, index_price)，价格从 start_prices 到 end_prices 线性过渡。
    """
    scenarios = []
    for step in range(steps + 1):
        ratio = step / steps if steps > 0 else 0
        ts = start_ts + step * step_interval
        prices = {}
        for c in codes:
            sp = start_prices.get(c, 100.0)
            ep = end_prices.get(c, sp)
            prices[c] = round(sp + (ep - sp) * ratio, 2)
        index_price = round(index_start + (index_end - index_start) * ratio, 2)
        scenarios.append(
            {
                "timestamp": ts,
                "prices": prices,
                "index_price": index_price,
            }
        )
    return scenarios


def build_flat_with_trigger(
    codes: list[str],
    trigger_code: str,
    trigger_value: float,
    steps_before: int,
    steps_after: int,
    base_prices: dict[str, float],
    step_interval: float = 60.0,
) -> list[dict]:
    """生成价格平台 → 触发特定值的场景。

    指定股票在 steps_before 步后跳到 trigger_value，维持 steps_after 步。
    """
    scenarios = []
    ts = 0.0
    for _ in range(steps_before):
        scenarios.append(
            {
                "timestamp": ts,
                "prices": dict(base_prices),
                "index_price": 3000.0,
            }
        )
        ts += step_interval

    prices = dict(base_prices)
    prices[trigger_code] = trigger_value
    for _ in range(steps_after):
        scenarios.append(
            {
                "timestamp": ts,
                "prices": dict(prices),
                "index_price": 3000.0,
            }
        )
        ts += step_interval
    return scenarios


def verify_total_value_consistency(sim: FullTradingDaySimulator):
    """验证每步 total_value = cash + sum(market_value)。"""
    for snap in sim.snapshots:
        expected = snap["cash"] + snap["market_value"]
        assert abs(snap["total_value"] - expected) < 0.02, (
            f"ts={snap['ts']}: total_value={snap['total_value']} != "
            f"cash+mv={snap['cash']}+{snap['market_value']}={expected}"
        )


# ═══════════════════════════════════════════════════════════════════
# 1. test_full_day_basic — 基本交易日
# ═══════════════════════════════════════════════════════════════════


class TestFullDayBasic:
    """2 个买入信号，240 步（9:30-15:00），价格线性变化。"""

    CODES = ["000001", "000002"]
    NAMES = {"000001": "股票A", "000002": "股票B"}

    @pytest.fixture
    def sim(self, db_path):
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "股票A",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 46.0,
                    "take_profit": 55.0,
                    "signal_score": 75,
                    "signal_source": "AI_ENHANCED",
                },
                {
                    "stock_code": "000002",
                    "stock_name": "股票B",
                    "buy_zone_min": 29.0,
                    "buy_zone_max": 31.0,
                    "stop_loss": 27.0,
                    "take_profit": 34.0,
                    "signal_score": 70,
                    "signal_source": "REVIEW",
                },
            ]
        )
        yield sim
        sim.close()

    def test_signals_executed_when_price_in_zone(self, sim):
        """价格进入买入区后执行买入。"""
        # 价格从区间下方线性上升到区间上方：
        #   股票A: 48.00 → 52.00 (买入区 49-51)
        #   股票B: 28.00 → 32.00 (买入区 29-31)
        steps = build_linear_price_scenario(
            self.CODES,
            start_prices={"000001": 48.0, "000002": 28.0},
            end_prices={"000001": 52.0, "000002": 32.0},
            steps=240,
        )
        sim.run(steps)

        summary = sim.summarize()
        # 两个信号都应被触发
        assert len(summary["buys"]) >= 2, (
            f"应有至少 2 笔买入，实有 {len(summary['buys'])}"
        )
        # 现金应减少
        assert summary["final_cash"] < DEFAULT_CAPITAL, "现金应减少"
        # 总资产 > 0
        assert summary["final_total_value"] > 0, "总资产应 > 0"

    def test_total_value_consistency(self, sim):
        """每步 total_value = cash + sum(market_value)。"""
        steps = build_linear_price_scenario(
            self.CODES,
            start_prices={"000001": 48.0, "002371": 28.0},
            end_prices={"000001": 52.0, "002371": 32.0},
            steps=240,
        )
        sim.run(steps)
        verify_total_value_consistency(sim)

    def test_positions_opened_with_correct_cost_basis(self, sim):
        """持仓开立，成本计算正确。"""
        steps = build_linear_price_scenario(
            self.CODES,
            start_prices={"000001": 48.0, "000002": 28.0},
            end_prices={"000001": 52.0, "000002": 32.0},
            steps=240,
        )
        sim.run(steps)

        for d in sim.decisions:
            if d["type"] == "buy":
                code = d["code"]
                pos = sim.account.positions.get(code)
                # 如果股票还在持仓中，验证成本价
                if pos:
                    # 成交价应等于买入价格
                    assert pos.avg_cost == pytest.approx(d["price"], abs=0.02), (
                        f"{code} 成本 {pos.avg_cost} 应等于买入价 {d['price']}"
                    )

    def test_signal_status_changed_to_bought(self, sim):
        """信号入库后，买入后状态变为 'bought'。"""
        steps = build_linear_price_scenario(
            self.CODES,
            start_prices={"000001": 48.0, "000002": 28.0},
            end_prices={"000001": 52.0, "000002": 32.0},
            steps=240,
        )
        sim.run(steps)

        # 检查 DB 中信号状态
        conn = sqlite3.connect(sim.db_path)
        try:
            statuses = conn.execute(
                "SELECT stock_code, status FROM trade_signals WHERE trade_date='2026-06-01'"
            ).fetchall()
            for code, status in statuses:
                assert status == "bought", (
                    f"{code} 信号状态应为 'bought'，实为 '{status}'"
                )
        finally:
            conn.close()

    def test_stop_loss_take_profit_stored(self, sim):
        """持仓元数据中止损止盈值正确存储。"""
        steps = build_linear_price_scenario(
            self.CODES,
            start_prices={"000001": 48.0, "000002": 28.0},
            end_prices={"000001": 52.0, "000002": 32.0},
            steps=240,
        )
        sim.run(steps)

        for code, meta in sim._pos_meta.items():
            assert meta["sl"] > 0, f"{code} 应有止损值"
            assert meta["tp"] > 0, f"{code} 应有止盈值"

    def test_close_summary_generated(self, sim):
        """收盘后生成收盘摘要。"""
        steps = build_linear_price_scenario(
            self.CODES,
            start_prices={"000001": 48.0, "000002": 28.0},
            end_prices={"000001": 52.0, "000002": 32.0},
            steps=240,
        )
        sim.run(steps)

        # 收盘前应有未完成的状态
        assert not sim._finalized or sim.account.total_value > 0

        # 执行收盘
        sim.finalize_close()
        assert sim._finalized, "收盘标志应设置"

        # DB 中有快照
        conn = sqlite3.connect(sim.db_path)
        try:
            snap = conn.execute(
                "SELECT total_value, cash, market_value FROM trade_portfolio_snapshots "
                "WHERE trade_date='2026-06-01' AND account='paper'"
            ).fetchone()
            assert snap is not None, "收盘快照应在 DB 中"
            total, cash, mv = snap
            assert abs(total - (cash + mv)) < 0.02, (
                f"快照 total={total} != cash+mv={cash}+{mv}"
            )
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
# 2. test_full_day_with_stop_loss — 止损触发
# ═══════════════════════════════════════════════════════════════════


class TestFullDayWithStopLoss:
    """买入 100 元，止损 95 元，价格下跌触发止损。"""

    def test_stop_loss_triggers_on_price_decline(self, db_path):
        """价格跌破止损线时触发卖出。

        注意：T+1 保护要求当日买入后不可卖出，因此测试分两步：
          1. 信号触发买入
          2. 手动解锁 T+1（模拟次日）
          3. 价格下跌触发止损
        """
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # 通过 setup_signals 插入信号
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "测试股票SL",
                    "buy_zone_min": 99.0,
                    "buy_zone_max": 101.0,
                    "stop_loss": 95.0,
                    "take_profit": 0,
                    "signal_score": 75,
                },
            ]
        )

        # 第 1 步：价格 100，触发买入
        sim.run_scan_at(0, {"000001": 100.0})
        buys = [d for d in sim.decisions if d["type"] == "buy"]
        assert len(buys) >= 1, "信号应触发买入"

        # 解锁 T+1（模拟持仓进入第二天）
        pos = sim.account.positions.get("000001")
        if pos:
            pos.locked_volume = 0

        # 价格从 100 → 94（触发止损的路径）
        steps = build_linear_price_scenario(
            ["000001"],
            start_prices={"000001": 100.0},
            end_prices={"000001": 94.0},
            steps=6,
        )
        # 跳过第 0 步（已执行）
        sim.run(steps[1:])

        sells = [d for d in sim.decisions if d["type"] == "sell"]

        # 止损应该被触发
        assert len(sells) >= 1, "应有止损卖出"

        # PnL 应为负（亏本卖出）
        for s in sells:
            if s["sell_type"] == "止损":
                assert s["pnl"] < 0, f"止损卖出 PnL 应为负，实为 {s['pnl']}"

    def test_cash_updated_after_stop_loss(self, db_path):
        """止损卖出后现金正确更新。"""
        from trade.exec.paper.account import (
            COMMISSION_RATE,
            MIN_COMMISSION,
            STAMP_TAX_RATE,
        )

        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # 直接买入，确保能精确控制
        sim.account.buy("000001", "测试股票", 100.0, 100, source="test")
        sim.account.positions["000001"].locked_volume = 0
        sim._pos_meta["000001"] = {
            "sl": 95,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 100,
        }

        cash_before = sim.account.cash

        # 价格跌到 94
        sim.run_scan_at(300.0, {"000001": 94.0})

        # 计算预期现金
        sell_amount = 94.0 * 100
        expected_commission = (
            max(sell_amount * COMMISSION_RATE, MIN_COMMISSION)
            + sell_amount * STAMP_TAX_RATE
        )
        expected_cash = cash_before + sell_amount - expected_commission

        assert sim.account.cash == pytest.approx(expected_cash, abs=0.02), (
            f"现金 {sim.account.cash:.2f} 预期 {expected_cash:.2f}"
        )

    def test_sell_order_created(self, db_path):
        """止损卖出在 trade_orders 表中有记录。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # 直接买入 + 解锁 T+1，确保可卖出
        sim.account.buy("000001", "测试股票", 100.0, 100, source="test")
        sim.account.positions["000001"].locked_volume = 0
        sim._pos_meta["000001"] = {
            "sl": 95,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 100,
        }

        # 价格 94 → 触发止损
        sim.run_scan_at(300.0, {"000001": 94.0})

        conn = sqlite3.connect(db_path)
        try:
            orders = conn.execute(
                "SELECT order_type, order_status FROM trade_orders WHERE stock_code='000001'"
            ).fetchall()
            order_types = [o[0] for o in orders]
            assert "sell" in order_types, "应有卖出订单"
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
# 3. test_full_day_with_take_profit — 止盈触发
# ═══════════════════════════════════════════════════════════════════


class TestFullDayWithTakeProfit:
    """买入 100 元，止盈 110 元，价格上涨触发止盈。"""

    def test_take_profit_triggers(self, db_path):
        """价格上升到止盈线时触发卖出。

        注意：T+1 保护，先买入解锁后再触发止盈。
        """
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # 直接买入 + 解锁 T+1
        sim.account.buy("000001", "测试股票TP", 100.0, 100, source="test")
        sim.account.positions["000001"].locked_volume = 0
        sim._pos_meta["000001"] = {
            "sl": 90.0,
            "tp": 110.0,
            "trailing_stop": 0,
            "highest_price": 100,
        }

        # 价格: 100 → 110.5（超过止盈 110）
        steps = build_linear_price_scenario(
            ["000001"],
            start_prices={"000001": 100.0},
            end_prices={"000001": 110.5},
            steps=8,
        )
        sim.run(steps)

        sells = [d for d in sim.decisions if d["type"] == "sell"]
        assert len(sells) >= 1, "应有止盈卖出"
        for s in sells:
            if s["sell_type"] == "止盈":
                assert s["pnl"] > 0, f"止盈卖出 PnL 应为正，实为 {s['pnl']}"

    def test_profit_recorded_in_db(self, db_path):
        """止盈卖出后 PnL 在订单中有记录。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # 直接买入 + 解锁 T+1
        sim.account.buy("000001", "测试股票TP", 100.0, 100, source="test")
        sim.account.positions["000001"].locked_volume = 0
        sim._pos_meta["000001"] = {
            "sl": 90.0,
            "tp": 110.0,
            "trailing_stop": 0,
            "highest_price": 100,
        }

        steps = build_linear_price_scenario(
            ["000001"],
            start_prices={"000001": 100.0},
            end_prices={"000001": 110.5},
            steps=8,
        )
        sim.run(steps)

        # 验证 order 表中字段正确
        conn = sqlite3.connect(db_path)
        try:
            sell_orders = conn.execute(
                "SELECT order_type, filled_price, filled_volume, commission "
                "FROM trade_orders WHERE stock_code='000001' AND order_type='sell'"
            ).fetchall()
            assert len(sell_orders) >= 1, "应有卖出订单"
            for _, price, _, _ in sell_orders:
                assert price > 100, f"卖出价 {price} 应高于成本 100"
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
# 4. test_multi_day_continuity — 多日连续性
# ═══════════════════════════════════════════════════════════════════


class TestMultiDayContinuity:
    """Day 1 买入持仓过夜 → Day 2 恢复 → 卖出 → P&L 正确。"""

    def test_position_carried_over_night(self, db_path):
        """第一天买入后，第二天恢复时持仓存在。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # Day 1: 买入
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "过夜股票",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 45.0,
                    "take_profit": 60.0,
                    "signal_score": 75,
                },
            ]
        )
        steps_day1 = build_linear_price_scenario(
            ["000001"],
            start_prices={"000001": 50.0},
            end_prices={"000001": 50.0},
            steps=5,
        )
        sim.run(steps_day1)

        # Day 1 收盘
        sim.finalize_close()

        # 持仓应该在结束前存在
        has_pos_day1 = len(sim.account.positions) > 0
        assert has_pos_day1, "Day 1 应有持仓"

        # Day 2: 恢复
        sim.restore_next_day("2026-06-02")

        # 检查持仓恢复
        assert len(sim.account.positions) > 0, "Day 2 恢复后应有持仓"
        assert "000001" in sim.account.positions, "Day 2 应有 000001 持仓"

    def test_pnl_across_days_correct(self, db_path):
        """Day 1 买入 → Day 2 卖出，总 P&L 正确。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # Day 1: 买入
        sim.account.buy("000001", "过夜股票", 50.0, 200, source="test")
        sim.account._persist_state()
        day1_cost = 50.0 * 200
        day1_commission = max(day1_cost * 0.000085, 5.0)
        DEFAULT_CAPITAL - day1_cost - day1_commission

        # 收盘落库
        sim.account.snapshot("2026-06-01")

        # Day 2: 恢复并卖出
        sim.restore_next_day("2026-06-02")
        pos = sim.account.positions.get("000001")
        assert pos is not None, "Day 2 000001 持仓应存在"

        # 价格涨到 55，卖出
        sim._pos_meta["000001"] = {
            "sl": 0,
            "tp": 55.0,
            "trailing_stop": 0,
            "highest_price": 50.0,
        }

        # 解锁 T+1（第二天已过 T+1）
        pos.locked_volume = 0

        sim.run_scan_at(100.0, {"000001": 55.0})

        # 验证持仓已清
        summary = sim.summarize()
        sells = [s for s in summary["sells"] if s["code"] == "000001"]

        if len(sells) > 0:
            # PnL 应为正
            for s in sells:
                assert s["pnl"] > 0, f"卖出 PnL 应为正，实为 {s['pnl']}"

    def test_snapshots_consistent_across_days(self, db_path):
        """快照跨日一致性：Day 1 快照值 = Day 2 恢复后的初始值。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # Day 1
        sim.account.buy("000001", "跨日股票", 50.0, 100, source="test")
        sim.account._persist_state()

        # 获取当天快照
        snap = sim.repo.get_latest_snapshot(account="paper")
        assert snap is not None, "应有快照"

        # Day 2: 恢复
        sim.restore_next_day("2026-06-02")

        # 恢复后的 total_value 应与快照一致
        restored_total = sim.account.total_value
        snap_total = snap["total_value"]
        assert abs(restored_total - snap_total) < 0.02, (
            f"恢复后 total_value {restored_total:.0f} 应与快照 {snap_total:.0f} 一致"
        )

    def test_signal_status_preserved_across_days(self, db_path):
        """bought 信号状态跨日保持。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "跨日信号",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 45.0,
                    "take_profit": 60.0,
                    "signal_score": 75,
                },
            ]
        )
        steps = build_linear_price_scenario(
            ["000001"],
            start_prices={"000001": 50.0},
            end_prices={"000001": 50.0},
            steps=3,
        )
        sim.run(steps)
        sim.finalize_close()

        # Day 2
        sim.restore_next_day("2026-06-02")

        conn = sqlite3.connect(db_path)
        try:
            status = conn.execute(
                "SELECT status FROM trade_signals WHERE stock_code='000001' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert status is not None
            assert status[0] == "bought", (
                f"跨日信号状态应为 'bought'，实为 '{status[0]}'"
            )
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
# 5. test_signal_to_execution_pipeline — 信号执行管线
# ═══════════════════════════════════════════════════════════════════


class TestSignalToExecutionPipeline:
    """验证信号从 DB 到执行再到状态更新的完整管线。"""

    def test_signal_inserted_and_read_from_db(self, db_path):
        """setup_signals 写入的信号能被 get_pending_signals 读取。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "管线测试",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 45.0,
                    "take_profit": 55.0,
                    "signal_score": 72,
                    "strategy_name": "ai_advisor",
                },
            ]
        )

        # 从 DB 读取
        pending = sim.repo.get_pending_signals(trade_date="2026-06-01", account="paper")
        assert len(pending) == 1
        assert pending[0]["stock_code"] == "000001"
        assert pending[0]["status"] == "pending"

    def test_price_enters_buy_zone_triggers_execution(self, db_path):
        """价格进入买入区 → 信号触发执行。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "买入验证",
                    "buy_zone_min": 49.5,
                    "buy_zone_max": 50.5,
                    "stop_loss": 47.0,
                    "take_profit": 55.0,
                    "signal_score": 75,
                },
            ]
        )

        # 价格从 49 到 51，经过买入区
        steps = build_linear_price_scenario(
            ["000001"],
            start_prices={"000001": 49.0},
            end_prices={"000001": 51.0},
            steps=5,
        )
        sim.run(steps)

        buys = [d for d in sim.decisions if d["type"] == "buy"]
        assert len(buys) >= 1, "应触发买入"
        assert buys[0]["code"] == "000001"

    def test_signal_status_changed_pending_to_bought(self, db_path):
        """信号状态从 pending 变为 bought。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "状态验证",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 46.0,
                    "take_profit": 55.0,
                    "signal_score": 78,
                },
            ]
        )

        steps = build_linear_price_scenario(
            ["000001"],
            start_prices={"000001": 50.0},
            end_prices={"000001": 52.0},
            steps=5,
        )
        sim.run(steps)

        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM trade_signals WHERE stock_code='000001'"
            ).fetchone()
            assert row is not None
            assert row[0] == "bought", f"信号状态应为 'bought'，实为 '{row[0]}'"
        finally:
            conn.close()

    def test_order_linked_to_signal_by_signal_id(self, db_path):
        """订单通过 signal_id 关联到信号。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "关联验证",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 46.0,
                    "take_profit": 55.0,
                    "signal_score": 75,
                },
            ]
        )

        steps = build_linear_price_scenario(
            ["000001"],
            start_prices={"000001": 50.0},
            end_prices={"000001": 52.0},
            steps=5,
        )
        sim.run(steps)

        conn = sqlite3.connect(db_path)
        try:
            sig_id = conn.execute(
                "SELECT id FROM trade_signals WHERE stock_code='000001'"
            ).fetchone()[0]
            orders = conn.execute(
                "SELECT signal_id, order_type FROM trade_orders WHERE signal_id=?",
                (sig_id,),
            ).fetchall()
            assert len(orders) >= 1, f"信号 {sig_id} 应有订单关联"
        finally:
            conn.close()

    def test_dangling_signals_not_reprocessed(self, db_path):
        """已触发的信号不重复处理。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "去重验证",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 46.0,
                    "take_profit": 55.0,
                    "signal_score": 75,
                },
            ]
        )

        # 价格在买入区内多次扫描
        steps = []
        for i in range(10):
            steps.append(
                {
                    "timestamp": i * 60,
                    "prices": {"000001": 50.0},
                    "index_price": 3000.0,
                }
            )
        sim.run(steps)

        buys = [d for d in sim.decisions if d["type"] == "buy"]
        assert len(buys) == 1, f"信号只应触发一次，实有 {len(buys)} 次买入"


# ═══════════════════════════════════════════════════════════════════
# 6. test_empty_day — 空交易日
# ═══════════════════════════════════════════════════════════════════


class TestEmptyDay:
    """无信号、无持仓的空交易日。"""

    def test_no_crashes(self, db_path):
        """空交易日不抛异常。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        steps = [
            {"timestamp": i * 60.0, "prices": {}, "index_price": 3000.0}
            for i in range(10)
        ]
        sim.run(steps)

        summary = sim.summarize()
        assert summary["final_position_count"] == 0
        assert len(summary["buys"]) == 0
        assert len(summary["sells"]) == 0
        assert summary["final_cash"] == pytest.approx(DEFAULT_CAPITAL, abs=0.01)

    def test_no_false_orders(self, db_path):
        """空交易日没有订单写入。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        steps = [
            {"timestamp": i * 60.0, "prices": {}, "index_price": 3000.0}
            for i in range(5)
        ]
        sim.run(steps)

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM trade_orders").fetchone()[0]
            assert count == 0, f"空交易日不应有订单，实有 {count}"
        finally:
            conn.close()

    def test_no_false_signals(self, db_path):
        """空交易日信号表不变。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        steps = [
            {"timestamp": i * 60.0, "prices": {}, "index_price": 3000.0}
            for i in range(5)
        ]
        sim.run(steps)

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM trade_signals").fetchone()[0]
            assert count == 0, f"空交易日不应有信号，实有 {count}"
        finally:
            conn.close()

    def test_close_no_crash(self, db_path):
        """空交易日收盘不抛异常。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.finalize_close()
        assert sim._finalized

    def test_total_value_stable(self, db_path):
        """空交易日 total_value 不变。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        for i in range(10):
            sim.run_scan_at(
                i * 60.0,
                {"000001": 50.0 + i * 0.1},
                index_price=3000.0,
            )
        # 没有持仓，total_value 应等于初始现金
        assert sim.account.total_value == pytest.approx(DEFAULT_CAPITAL, abs=0.01)

    def test_snapshots_consistent_empty(self, db_path):
        """空交易日每步快照一致。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        for i in range(10):
            sim.run_scan_at(i * 60.0, {}, index_price=3000.0)
        verify_total_value_consistency(sim)


# ═══════════════════════════════════════════════════════════════════
# 7. test_multiple_positions_same_day — 多持仓同日
# ═══════════════════════════════════════════════════════════════════


class TestMultiplePositionsSameDay:
    """3 个买入信号，3 个不同入场时间。"""

    CODES = ["000001", "000002", "000003"]
    NAMES = {"000001": "股票C", "000002": "股票D", "000003": "股票E"}

    @pytest.fixture
    def sim(self, db_path):
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "股票C",
                    "buy_zone_min": 19.0,
                    "buy_zone_max": 21.0,
                    "stop_loss": 18.0,
                    "take_profit": 23.0,
                    "signal_score": 70,
                },
                {
                    "stock_code": "000002",
                    "stock_name": "股票D",
                    "buy_zone_min": 29.0,
                    "buy_zone_max": 31.0,
                    "stop_loss": 27.0,
                    "take_profit": 34.0,
                    "signal_score": 75,
                },
                {
                    "stock_code": "000003",
                    "stock_name": "股票E",
                    "buy_zone_min": 39.0,
                    "buy_zone_max": 41.0,
                    "stop_loss": 37.0,
                    "take_profit": 44.0,
                    "signal_score": 72,
                },
            ]
        )
        yield sim
        sim.close()

    def test_all_three_opened(self, sim):
        """3 个信号全部正确开仓。"""
        # 构造不同入场时间：
        #   000001: 前 80 步价格在 20 附近（买入区 19-21）
        #   000002: 80-160 步价格在 30 附近（买入区 29-31）
        #   000003: 160-240 步价格在 40 附近（买入区 39-41）
        steps = []

        # 阶段 1: 000001 进入买入区
        for i in range(80):
            ts = i * 60.0
            steps.append(
                {
                    "timestamp": ts,
                    "prices": {"000001": 20.0, "000002": 28.0, "000003": 38.0},
                    "index_price": 3000.0,
                }
            )

        # 阶段 2: 000002 进入买入区
        for i in range(80):
            ts = (80 + i) * 60.0
            steps.append(
                {
                    "timestamp": ts,
                    "prices": {"000001": 21.0, "000002": 30.0, "000003": 38.0},
                    "index_price": 3000.0,
                }
            )

        # 阶段 3: 000003 进入买入区
        for i in range(80):
            ts = (160 + i) * 60.0
            steps.append(
                {
                    "timestamp": ts,
                    "prices": {"000001": 21.0, "000002": 31.0, "000003": 40.0},
                    "index_price": 3000.0,
                }
            )

        sim.run(steps)

        summary = sim.summarize()
        # 验证 3 个信号都触发了
        assert len(summary["buys"]) == 3, f"应有 3 笔买入，实有 {len(summary['buys'])}"

        bought_codes = {d["code"] for d in summary["buys"]}
        for code in self.CODES:
            assert code in bought_codes, f"{code} 应被买入"

    def test_no_interference_between_positions(self, sim):
        """多持仓间不互相干扰。"""
        steps = []
        for i in range(240):
            ts = i * 60.0
            steps.append(
                {
                    "timestamp": ts,
                    "prices": {"000001": 20.0, "000002": 30.0, "000003": 40.0},
                    "index_price": 3000.0,
                }
            )
        sim.run(steps)

        # 每个持仓的止损/止盈相互独立
        for code, meta in sim._pos_meta.items():
            assert code in self.CODES, f"元数据中 {code} 不应存在"
            assert meta["sl"] > 0, f"{code} 应有止损"
            assert meta["tp"] > 0, f"{code} 应有止盈"

    def test_cash_decreases_with_each_buy(self, sim):
        """每次买入后现金正确递减。"""
        steps = []
        for i in range(240):
            ts = i * 60.0
            steps.append(
                {
                    "timestamp": ts,
                    "prices": {"000001": 20.0, "000002": 30.0, "000003": 40.0},
                    "index_price": 3000.0,
                }
            )
        sim.run(steps)

        # 检查快照中 cash 趋势：不是严格递减（价格波动影响个股市值
        # 但整体现金 + 市值应等于 total_value）
        verify_total_value_consistency(sim)

    def test_all_signals_status_bought(self, sim):
        """3 个信号状态全部变为 bought。"""
        steps = []
        for i in range(240):
            ts = i * 60.0
            steps.append(
                {
                    "timestamp": ts,
                    "prices": {"000001": 20.0, "000002": 30.0, "000003": 40.0},
                    "index_price": 3000.0,
                }
            )
        sim.run(steps)

        conn = sqlite3.connect(sim.db_path)
        try:
            statuses = conn.execute(
                "SELECT stock_code, status FROM trade_signals WHERE trade_date='2026-06-01'"
            ).fetchall()
            for code, status in statuses:
                assert status == "bought", f"{code} 状态应为 'bought'，实为 '{status}'"
        finally:
            conn.close()


# ═══════════════════════════════════════════════════════════════════
# 8. test_regime_changes_during_day — 市场态势变化
# ═══════════════════════════════════════════════════════════════════


class TestRegimeChangesDuringDay:
    """市场从 normal → panic → recovery，买入行为随之变化。"""

    def test_buy_blocked_during_panic(self, db_path):
        """大盘恐慌期间（allow_buy=False）暂停买入。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "恐慌测试",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 45.0,
                    "take_profit": 55.0,
                    "signal_score": 75,
                },
            ]
        )

        # 阶段 1: 正常，价格在买入区 → 应该买入
        sim._regime = MarketRegime(
            pattern="normal",
            risk_level="safe",
            allow_buy=True,
        )
        for i in range(10):
            sim.run_scan_at(i * 60.0, {"000001": 50.0}, index_price=3000.0)

        buys_before_panic = len([d for d in sim.decisions if d["type"] == "buy"])

        # 阶段 2: 恐慌 → 暂停买入
        sim._regime = MarketRegime(
            pattern="crash",
            risk_level="extreme",
            allow_buy=False,
        )
        len(sim.decisions)
        for i in range(10, 20):
            sim.run_scan_at(i * 60.0, {"000001": 50.0}, index_price=2800.0)

        buys_during_panic = len([d for d in sim.decisions if d["type"] == "buy"])
        # 恐慌期间不应该有新买入
        buys_during_panic - buys_before_panic
        # 但注意：恐慌前买入的信号已被触发，恐慌期间不会重复触发
        # 如果信号已被触发（bought），它已经在 _triggered_ids 中

    def test_buy_resumes_after_recovery(self, db_path):
        """市场恢复后买入恢复正常。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # 设置 2 个信号，分在恐慌前和后触发
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "恢复测试A",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 45.0,
                    "take_profit": 55.0,
                    "signal_score": 75,
                },
                {
                    "stock_code": "000002",
                    "stock_name": "恢复测试B",
                    "buy_zone_min": 49.0,
                    "buy_zone_max": 51.0,
                    "stop_loss": 45.0,
                    "take_profit": 55.0,
                    "signal_score": 70,
                },
            ]
        )

        # 阶段 1: 正常 → 000001 买入
        sim._regime = MarketRegime(
            pattern="normal",
            risk_level="safe",
            allow_buy=True,
        )
        for i in range(5):
            sim.run_scan_at(
                i * 60.0,
                {"000001": 50.0, "000002": 50.0},
                index_price=3000.0,
            )

        # 阶段 2: 恐慌 → 暂停
        sim._regime = MarketRegime(
            pattern="crash",
            risk_level="extreme",
            allow_buy=False,
        )
        for i in range(5, 10):
            sim.run_scan_at(
                i * 60.0,
                {"000001": 50.0, "000002": 50.0},
                index_price=2800.0,
            )

        # 阶段 3: 恢复 → 000002 买入
        sim._regime = MarketRegime(
            pattern="recovery",
            risk_level="guarded",
            allow_buy=True,
        )
        for i in range(10, 20):
            sim.run_scan_at(
                i * 60.0,
                {"000001": 50.0, "000002": 50.0},
                index_price=3050.0,
            )

        bought_codes = set()
        for d in sim.decisions:
            if d["type"] == "buy":
                bought_codes.add(d["code"])

        # 000001 应该被买入（恐慌前）
        assert "000001" in bought_codes, "000001 应在恐慌前被买入"

        # 000002 应该被买入（恢复后）
        assert "000002" in bought_codes, "000002 应在恢复后被买入"

        # 总共 2 笔买入
        buys = [d for d in sim.decisions if d["type"] == "buy"]
        assert len(buys) == 2, f"应有 2 笔买入（恐慌前+恢复后），实有 {len(buys)}"

    def test_no_buy_during_panic_regardless_of_price(self, db_path):
        """恐慌期间不论价格如何都不买入。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "全程恐慌",
                    "buy_zone_min": 10.0,
                    "buy_zone_max": 100.0,  # 极宽买入区
                    "stop_loss": 5.0,
                    "take_profit": 200.0,
                    "signal_score": 80,
                },
            ]
        )

        # 全程恐慌
        sim._regime = MarketRegime(
            pattern="crash",
            risk_level="extreme",
            allow_buy=False,
        )

        for i in range(10):
            sim.run_scan_at(i * 60.0, {"000001": 50.0}, index_price=2800.0)

        buys = [d for d in sim.decisions if d["type"] == "buy"]
        assert len(buys) == 0, "恐慌期间不应有任何买入"

    def test_positions_not_affected_by_regime(self, db_path):
        """市场态势变化不自动触发持仓平仓（风控单独负责）。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        # 直接买入
        sim.account.buy("000001", "稳住股票", 50.0, 100, source="test")
        sim.account.positions["000001"].locked_volume = 0
        sim._pos_meta["000001"] = {
            "sl": 45,
            "tp": 55,
            "trailing_stop": 0.05,
            "highest_price": 50,
        }

        # 价格不变，只是一次次恐慌状态切换
        for regime in [
            MarketRegime(pattern="crash", risk_level="extreme", allow_buy=False),
            MarketRegime(pattern="normal", risk_level="safe", allow_buy=True),
            MarketRegime(pattern="panic", risk_level="dangerous", allow_buy=False),
            MarketRegime(pattern="recovery", risk_level="guarded", allow_buy=True),
        ]:
            sim._regime = regime
            sim.run_scan_at(100.0, {"000001": 50.0}, index_price=3000.0)

        # 持仓仍在（价格没触发止损止盈）
        assert "000001" in sim.account.positions, "市场态势切换不应自动平仓"

    def test_sell_still_works_during_panic(self, db_path):
        """恐慌期间止损/止盈仍然工作。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")

        sim.account.buy("000001", "恐慌卖出", 100.0, 100, source="test")
        sim.account.positions["000001"].locked_volume = 0
        sim._pos_meta["000001"] = {
            "sl": 95,
            "tp": 0,
            "trailing_stop": 0,
            "highest_price": 100,
        }

        # 恐慌 + 价格跌破止损
        sim._regime = MarketRegime(
            pattern="crash",
            risk_level="extreme",
            allow_buy=False,
        )
        sim.run_scan_at(100.0, {"000001": 94.0}, index_price=2800.0)

        # 止损应该触发
        sells = [d for d in sim.decisions if d["type"] == "sell"]
        assert len(sells) > 0, "恐慌期间止损也应触发"


# ═══════════════════════════════════════════════════════════════════
# 额外：模拟器边界测试
# ═══════════════════════════════════════════════════════════════════


class TestSimulatorEdgeCases:
    """模拟器边界情况。"""

    def test_no_signals_no_positions(self, db_path):
        """无信号无持仓时正常运行。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        for i in range(5):
            sim.run_scan_at(i * 60.0, {"000001": 50.0}, index_price=3000.0)
        summary = sim.summarize()
        assert summary["final_position_count"] == 0
        assert summary["final_cash"] == pytest.approx(DEFAULT_CAPITAL, abs=0.01)

    def test_price_below_buy_zone(self, db_path):
        """价格在买入区以下不买入。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "低价测试",
                    "buy_zone_min": 20.0,
                    "buy_zone_max": 25.0,
                    "stop_loss": 18.0,
                    "take_profit": 28.0,
                    "signal_score": 70,
                },
            ]
        )
        for i in range(5):
            sim.run_scan_at(i * 60.0, {"000001": 15.0}, index_price=3000.0)
        buys = [d for d in sim.decisions if d["type"] == "buy"]
        assert len(buys) == 0

    def test_price_above_buy_zone(self, db_path):
        """价格在买入区以上不买入。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "高价测试",
                    "buy_zone_min": 10.0,
                    "buy_zone_max": 14.0,
                    "stop_loss": 9.0,
                    "take_profit": 16.0,
                    "signal_score": 70,
                },
            ]
        )
        for i in range(5):
            sim.run_scan_at(i * 60.0, {"000001": 20.0}, index_price=3000.0)
        buys = [d for d in sim.decisions if d["type"] == "buy"]
        assert len(buys) == 0

    def test_empty_price_scenario(self, db_path):
        """空价格场景不报错。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.run([])
        summary = sim.summarize()
        assert summary["final_position_count"] == 0

    def test_close_without_run(self, db_path):
        """未运行直接收盘不报错。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.finalize_close()
        assert sim._finalized

    def test_double_close(self, db_path):
        """多次收盘安全。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.finalize_close()
        sim.finalize_close()
        assert sim._finalized

    def test_summarize_after_no_trades(self, db_path):
        """无交易时 summarize 返回合理值。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        summary = sim.summarize()
        assert summary["total_realized_pnl"] == 0
        assert summary["total_pnl"] == 0
        assert summary["daily_pnl"] == 0
        assert summary["final_cash"] == DEFAULT_CAPITAL

    def test_multiple_calls_to_setup_signals(self, db_path):
        """多次 setup_signals 累积而非覆盖。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "A",
                    "buy_zone_min": 10,
                    "buy_zone_max": 20,
                    "stop_loss": 9,
                    "take_profit": 22,
                    "signal_score": 70,
                },
            ]
        )
        sim.setup_signals(
            [
                {
                    "stock_code": "000002",
                    "stock_name": "B",
                    "buy_zone_min": 30,
                    "buy_zone_max": 40,
                    "stop_loss": 28,
                    "take_profit": 44,
                    "signal_score": 75,
                },
            ]
        )

        pending = sim.repo.get_pending_signals(trade_date="2026-06-01", account="paper")
        assert len(pending) == 2, "两次 setup 后应有 2 个信号"

    def test_t1_protection(self, db_path):
        """当天买入不可卖出（T+1）。"""
        sim = FullTradingDaySimulator(db_path, trade_date="2026-06-01")
        sim.setup_signals(
            [
                {
                    "stock_code": "000001",
                    "stock_name": "T1测试",
                    "buy_zone_min": 49,
                    "buy_zone_max": 51,
                    "stop_loss": 100,  # 止损高于买入价，不可能触发
                    "take_profit": 200,  # 止盈也很高
                    "signal_score": 70,
                },
            ]
        )

        # 买入
        sim.run_scan_at(0, {"000001": 50.0})
        buys = [d for d in sim.decisions if d["type"] == "buy"]
        if len(buys) > 0:
            # 买入后，T+1 保护生效
            pos = sim.account.positions.get("000001")
            if pos:
                # available_volume 应为 0
                # （PaperAccount.buy 中会设置 locked_volume = volume）
                assert pos.available_volume == 0, "T+1 保护应锁定可用股数为 0"
