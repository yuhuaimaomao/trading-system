"""模拟盘账户 — 纯执行层：买卖、快照、恢复、查询。

不参与任何决策（仓位计算/止损止盈/换仓评估均由盯盘系统负责）。
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from data.repo import TradeRepository
from system.config import settings
from trade.portfolio.portfolio import Portfolio

logger = logging.getLogger(__name__)

# 费率
COMMISSION_RATE = 0.000085  # 万0.85
MIN_COMMISSION = 5.0
STAMP_TAX_RATE = 0.0005  # 万分之五（卖出单边）


@dataclass
class BuyResult:
    success: bool
    volume: int = 0
    cost: float = 0.0  # price * volume + commission
    commission: float = 0.0
    reason: str = ""


@dataclass
class SellResult:
    success: bool
    pnl: float = 0.0
    pnl_pct: float = 0.0
    proceeds: float = 0.0  # price * volume - commission
    commission: float = 0.0
    reason: str = ""


class PaperAccount:
    """模拟盘账户 — 纯执行层。

    盯盘系统调用流程:
      restore(trade_date) → 恢复状态
      buy(code, name, price, volume, ...) → 执行买入
      sell(code, price, reason) → 执行卖出
      snapshot(trade_date) → 收盘落库
    """

    def __init__(self, db_path: str, telegram_bot=None, initial_capital: float = None):
        self._portfolio = Portfolio(
            initial_cash=initial_capital or settings.PAPER_INITIAL_CAPITAL
        )
        self.db_path = db_path
        self.telegram = telegram_bot
        self.repo = TradeRepository(db_path=db_path)

    # ===== 属性（兼容 Portfolio duck typing，供 RiskEngine 等使用）=====

    @property
    def positions(self) -> dict:
        return self._portfolio.positions

    @property
    def cash(self) -> float:
        return self._portfolio.cash

    @property
    def total_value(self) -> float:
        return self._portfolio.total_value

    @property
    def position_ratio(self) -> float:
        return self._portfolio.position_ratio

    @property
    def total_pnl(self) -> float:
        return self._portfolio.total_pnl

    @property
    def initial_cash(self) -> float:
        return self._portfolio.initial_cash

    @property
    def daily_pnl(self) -> float:
        return self._portfolio.daily_pnl

    @property
    def drawdown(self) -> float:
        return self._portfolio.drawdown

    @property
    def position_count(self) -> int:
        return len(self._portfolio.positions)

    def update_prices(self, prices: dict[str, float]):
        self._portfolio.update_prices(prices)

    def get_sector_exposure(
        self, sector_map: dict[str, str] = None
    ) -> dict[str, float]:
        return self._portfolio.get_sector_exposure(sector_map)

    # ===== 买卖执行 =====

    @property
    def _trade_date(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def buy(
        self,
        code: str,
        name: str,
        price: float,
        volume: int,
        signal_id: int = None,
        source: str = "",
    ) -> BuyResult:
        """执行买入：扣现金、建持仓、写订单、发通知。"""
        # 股数校验
        if volume <= 0 or volume % 100 != 0:
            return BuyResult(success=False, reason=f"无效股数 {volume}")

        amount = price * volume
        commission = max(amount * COMMISSION_RATE, MIN_COMMISSION)
        cost = amount + commission

        if cost > self._portfolio.cash:
            return BuyResult(
                success=False,
                volume=volume,
                cost=cost,
                commission=commission,
                reason=f"现金不足: 需{cost:,.0f} 仅{self._portfolio.cash:,.0f}",
            )

        ok = self._portfolio.open_position(
            stock_code=code,
            stock_name=name,
            volume=volume,
            price=price,
            entry_date=self._trade_date,
            commission=commission,
        )
        if not ok:
            return BuyResult(success=False, reason="开仓失败")

        self._record_order(
            code,
            name,
            "buy",
            volume,
            price,
            source,
            commission=commission,
            signal_id=signal_id,
        )

        if self.telegram:
            self._notify_buy(code, name, price, volume, amount, commission, source)

        self._persist_state()
        logger.info(
            f"模拟盘买入: {code} {name} {volume}股 @{price:.2f} 佣金{commission:.0f}"
        )
        return BuyResult(success=True, volume=volume, cost=cost, commission=commission)

    def sell(self, code: str, price: float, reason: str = "") -> SellResult:
        """执行卖出：T+1 检查、加现金、删持仓、写订单、发通知。"""
        pos = self._portfolio.positions.get(code)
        if not pos:
            return SellResult(success=False, reason=f"无持仓 {code}")

        if pos.available_volume <= 0:
            return SellResult(
                success=False,
                reason=f"T+1 保护，当日买入不可卖出 {code}（持仓 {pos.volume} 股，可用 0）",
            )

        # 在 close_position 之前保存所有需要的属性（close_position 会 del self.positions[code]）
        stock_name = pos.stock_name
        volume = pos.volume
        avg_cost = pos.avg_cost

        amount = price * volume
        commission = (
            max(amount * COMMISSION_RATE, MIN_COMMISSION) + amount * STAMP_TAX_RATE
        )

        self._portfolio.close_position(code, price, reason, commission=commission)
        self._record_order(
            code,
            stock_name,
            "sell",
            volume,
            price,
            reason,
            commission=commission,
        )

        pnl = (price - avg_cost) * volume - commission
        # pnl_pct 统一为百分数（0-100），供 SellResult 和 Telegram 通知使用
        pnl_pct = (price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0

        if self.telegram:
            self._notify_sell(
                code,
                stock_name,
                price,
                volume,
                avg_cost,
                pnl,
                pnl_pct,
                commission,
                reason,
            )

        self._persist_state()
        logger.info(f"模拟盘卖出: {code} {stock_name} 盈亏{pnl:+.0f}")
        return SellResult(
            success=True,
            pnl=pnl,
            pnl_pct=pnl_pct,
            proceeds=amount - commission,
            commission=commission,
        )

    # ===== 快照 =====

    def snapshot(self, trade_date: str):
        """收盘落库：快照表 + 持仓表。"""
        snap = self._portfolio.snapshot(trade_date)
        self.repo.insert_snapshot(snap.to_db_dict(account="paper"))

        pos_rows = []
        for code, pos in self._portfolio.positions.items():
            pos_rows.append(
                {
                    "stock_code": code,
                    "stock_name": pos.stock_name,
                    "volume": pos.volume,
                    "avg_cost": pos.avg_cost,
                    "current_price": pos.current_price,
                    "market_value": pos.market_value,
                    "pnl": pos.pnl,
                    "pnl_pct": pos.pnl_pct,
                    "entry_date": pos.entry_date,
                    "locked_volume": getattr(pos, "locked_volume", 0),
                }
            )
        self.repo.insert_positions(trade_date, "paper", pos_rows)
        logger.info(
            f"模拟盘快照已保存: 总资产{snap.total_value:,.0f} 仓位{len(pos_rows)}只"
        )

    # ===== 恢复 =====

    def restore(self, trade_date: str):
        """启动时从 DB 恢复状态：快照→现金，持仓表→持仓。

        每笔买卖实时落库，所以直接读最新快照就是当前状态，无需重放订单。"""
        from trade.portfolio.portfolio import Position

        snap = self.repo.get_latest_snapshot(account="paper")

        if snap:
            self._portfolio.cash = snap["cash"]
            self._portfolio._peak_value = max(
                self._portfolio._peak_value, snap.get("total_value", 0) or 0
            )
            self._portfolio._prev_total = snap.get("total_value", 0) or 0
            snap_date = snap["trade_date"]
            logger.info(
                f"从快照恢复: trade_date={snap_date} cash={snap['cash']:,.0f} "
                f"total_value={snap['total_value']:,.0f}"
            )
        else:
            snap_date = None
            init_cash = self._portfolio.initial_cash
            self.repo.insert_snapshot(
                {
                    "trade_date": trade_date,
                    "total_value": init_cash,
                    "cash": init_cash,
                    "market_value": 0,
                    "daily_pnl": 0,
                    "total_pnl": 0,
                    "drawdown": 0,
                    "position_count": 0,
                    "sector_exposure": "{}",
                    "account": "paper",
                    "created_at": datetime.now().isoformat(),
                }
            )
            logger.info(f"首次运行，写入初始快照: cash={init_cash:,.0f}")

        # 从持仓表恢复
        if snap_date:
            pos_rows = self.repo.get_positions_by_date(snap_date, "paper")
            for row in pos_rows:
                code = row["stock_code"]
                self._portfolio.positions[code] = Position(
                    stock_code=code,
                    stock_name=row.get("stock_name", ""),
                    volume=row.get("volume", 0),
                    avg_cost=row.get("avg_cost", 0),
                    current_price=row.get("current_price", 0),
                    market_value=row.get("market_value", 0),
                    pnl=row.get("pnl", 0),
                    pnl_pct=row.get("pnl_pct", 0),
                    entry_date=row.get("entry_date", snap_date),
                    locked_volume=row.get("locked_volume", 0),
                )
                logger.info(
                    f"恢复持仓: {code} {row.get('stock_name', '')} "
                    f"{row.get('volume', 0)}股 成本{row.get('avg_cost', 0):.2f}"
                )

    # ===== 查询 =====

    def get_position_summary(self) -> list[str]:
        lines = []
        for code, pos in self._portfolio.positions.items():
            lines.append(
                f"  {code} {pos.stock_name} {pos.volume}股 "
                f"成本{pos.avg_cost:.2f} 现价{pos.current_price:.2f} "
                f"盈亏{pos.pnl:+.0f}({pos.pnl_pct:+.1f}%)"
            )
        return lines

    def portfolio_summary(self) -> str:
        p = self._portfolio
        return (
            f"总资产 {p.total_value:.0f}  "
            f"现金 {p.cash:.0f}  "
            f"总盈亏 {p.total_pnl:+.0f}({p.total_pnl / p.initial_cash * 100:+.1f}%)"
        )

    # ===== 内部 =====

    def _persist_state(self):
        """实时落库：每次买卖后更新快照表和持仓表，盘中随时可查到当前状态。

        不调 portfolio.snapshot()，避免搅乱 daily_pnl / _prev_total。
        """
        p = self._portfolio
        trade_date = self._trade_date
        self.repo.insert_snapshot(
            {
                "trade_date": trade_date,
                "total_value": p.total_value,
                "cash": p.cash,
                "market_value": sum(pos.market_value for pos in p.positions.values()),
                "daily_pnl": p.daily_pnl,
                "total_pnl": p.total_pnl,
                "drawdown": p.drawdown,
                "position_count": len(p.positions),
                "sector_exposure": "{}",
                "account": "paper",
                "created_at": datetime.now().isoformat(),
            }
        )
        pos_rows = []
        for code, pos in p.positions.items():
            pos_rows.append(
                {
                    "stock_code": code,
                    "stock_name": pos.stock_name,
                    "volume": pos.volume,
                    "avg_cost": pos.avg_cost,
                    "current_price": pos.current_price,
                    "market_value": pos.market_value,
                    "pnl": pos.pnl,
                    "pnl_pct": pos.pnl_pct,
                    "entry_date": pos.entry_date,
                    "locked_volume": getattr(pos, "locked_volume", 0),
                }
            )
        self.repo.insert_positions(trade_date, "paper", pos_rows)

    def _record_order(
        self,
        code,
        name,
        order_type,
        volume,
        price,
        source="",
        commission=0,
        signal_id=None,
    ):
        try:
            self.repo.insert_order(
                {
                    "trade_date": self._trade_date,
                    "order_time": datetime.now().isoformat(),
                    "stock_code": code,
                    "order_type": order_type,
                    "order_price": price,
                    "order_volume": volume,
                    "order_status": "filled",
                    "filled_volume": volume,
                    "filled_price": price,
                    "filled_amount": price * volume,
                    "commission": commission,
                    "strategy_name": f"paper_{source}" if source else None,
                    "signal_id": signal_id,
                    "account": "paper",
                }
            )
        except Exception as e:
            logger.warning(f"模拟盘订单记录失败: {e}")

    @staticmethod
    @lru_cache(maxsize=settings.NAME_RESOLVE_CACHE_SIZE)
    def _resolve_name(code: str) -> str:
        try:
            conn = sqlite3.connect(settings.DATABASE_PATH)
            row = conn.execute(
                """SELECT stock_name FROM stock_basic
                   WHERE stock_code=? AND trade_date=(SELECT MAX(trade_date) FROM stock_basic)
                   LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            return row[0] if row else code
        except Exception:
            return code

    def _notify_buy(self, code, name, price, volume, amount, commission, source):
        p = self._portfolio
        pos_count = len(p.positions)
        pos_pct = (p.total_value - p.cash) / p.total_value if p.total_value > 0 else 0
        self.telegram.send(
            f"📝 模拟盘买入 — {code} {name}\n"
            f"   价格: {price:.2f} × {volume} 股  金额: {amount:,.0f}  佣金: {commission:.0f}\n"
            f"   来源: {source}\n"
            f"   📦 持仓: {pos_count}/{settings.MAX_POSITIONS}  仓位: {pos_pct:.0%}  总资产: {p.total_value:,.0f}"
        )

    def _notify_sell(
        self, code, name, price, volume, avg_cost, pnl, pnl_pct, commission, reason
    ):
        p = self._portfolio
        pos_count = len(p.positions)
        pos_pct = (p.total_value - p.cash) / p.total_value if p.total_value > 0 else 0
        emoji = "✅" if pnl > 0 else "⚠️"
        self.telegram.send(
            f"{emoji} 模拟盘卖出 — {code} {name}\n"
            f"   价格: {price:.2f} × {volume} 股  成本: {avg_cost:.2f}  盈亏: {pnl:+,.0f} ({pnl_pct:+.1f}%)\n"
            f"   费用: {commission:.1f}  原因: {reason}\n"
            f"   📦 持仓: {pos_count}/{settings.MAX_POSITIONS}  仓位: {pos_pct:.0%}  总资产: {p.total_value:,.0f}"
        )
