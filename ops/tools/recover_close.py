"""收盘恢复脚本 — 补执行 _finalize_close 中因 crash 未完成的步骤。

用法:  python scripts/recover_close.py
"""

import sqlite3
import sys
from datetime import datetime

DB = "storage/stock_market.db"
TODAY = "2026-06-02"
NEXT_DATE = "2026-06-03"
ACCOUNT = "paper"

# ── 当前持仓（从订单净额推算） ──

BUYS = {
    "000539": ("粤电力Ａ", 1100, 9.68),
    "000600": ("建投能源", 900, 11.60),
    "002354": ("天娱数科", 700, 7.28),
    "002995": ("天地在线", 600, 24.84),
}

# ── 收盘价（从 market_snapshots 最后一批） ──

CLOSE_PRICES = {
    "000539": 9.13,
    "000600": 11.76,
    "002354": 6.70,
    "002995": 25.95,
}

INITIAL_CASH = 200_000


def get_yesterday_snapshot(conn):
    row = conn.execute(
        """SELECT * FROM trade_portfolio_snapshots
           WHERE trade_date < ? AND account=?
           ORDER BY id DESC LIMIT 1""",
        (TODAY, ACCOUNT),
    ).fetchone()
    return row


def get_today_filled_orders(conn):
    rows = conn.execute(
        """SELECT * FROM trade_orders
           WHERE trade_date=? AND account=? AND order_status='filled'
           ORDER BY id""",
        (TODAY, ACCOUNT),
    ).fetchall()
    return rows


def get_pending_signals(conn):
    rows = conn.execute(
        "SELECT id, stock_code, stock_name FROM trade_signals WHERE status='pending'"
    ).fetchall()
    return rows


def compute_cash(yesterday_snap, orders):
    cash = yesterday_snap["cash"]
    for o in orders:
        if o["order_type"] == "buy":
            cash -= o["filled_amount"] + o["commission"]
        else:
            cash += o["filled_amount"] - o["commission"]
    return cash


def compute_portfolio():
    """返回 (positions_list, total_mv, total_pnl, daily_pnl, drawdown)"""
    positions = []
    total_mv = 0
    total_cost = 0
    drawdown = 0

    for code, (name, vol, cost) in BUYS.items():
        close = CLOSE_PRICES[code]
        mv = close * vol
        total_mv += mv
        total_cost += cost * vol
        pnl = (close - cost) * vol
        pnl_pct = (close - cost) / cost  # 小数，与 Position.update_price 一致

        # 日内回撤（当日买入用日内最高≈收盘价）
        positions.append(
            {
                "code": code,
                "name": name,
                "vol": vol,
                "cost": cost,
                "close": close,
                "mv": mv,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )

    return positions, total_mv, total_cost


# ═══════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=" * 60)
    print(f"  收盘恢复  {TODAY}")
    print("=" * 60)

    # ── 1. 获取数据 ──
    yesterday = get_yesterday_snapshot(conn)
    if not yesterday:
        print("❌ 找不到昨日快照，无法计算")
        sys.exit(1)
    print(
        f"\n昨日快照: total={yesterday['total_value']:,.0f}  cash={yesterday['cash']:,.0f}  "
        f"mv={yesterday['market_value']:,.0f}  pos={yesterday['position_count']}"
    )

    orders = get_today_filled_orders(conn)
    buys = [o for o in orders if o["order_type"] == "buy"]
    sells = [o for o in orders if o["order_type"] == "sell"]
    print(f"今日成交: {len(buys)} 买 + {len(sells)} 卖 = {len(orders)} 笔")

    # ── 2. 计算当前状态 ──
    cash = compute_cash(yesterday, orders)
    positions, total_mv, total_cost = compute_portfolio()
    total_value = cash + total_mv
    total_pnl = total_value - INITIAL_CASH

    # 当日盈亏 = 总资产变化
    daily_pnl = total_value - yesterday["total_value"]
    daily_pnl_pct = daily_pnl / yesterday["total_value"] * 100

    position_count = len(positions)

    print("\n当前状态:")
    print(f"  现金: {cash:,.0f}")
    print(f"  市值: {total_mv:,.0f}")
    print(f"  总资产: {total_value:,.0f}")
    print(f"  总盈亏: {total_pnl:+,.0f} ({total_pnl / INITIAL_CASH * 100:+.2f}%)")
    print(f"  当日盈亏: {daily_pnl:+,.0f} ({daily_pnl_pct:+.2f}%)")
    print(f"  持仓: {position_count} 只")
    print(f"  仓位: {total_mv / total_value * 100:.0f}%")

    # ── 3. 持久化：更新持仓表（用收盘价 + 当日盈亏） ──
    now = datetime.now().isoformat()
    pos_rows = []
    for p in positions:
        # 当日买入的 daily_pnl = (收盘-成本)×量
        stock_daily = (p["close"] - p["cost"]) * p["vol"]
        pos_rows.append(
            {
                "stock_code": p["code"],
                "stock_name": p["name"],
                "volume": p["vol"],
                "avg_cost": p["cost"],
                "current_price": p["close"],
                "market_value": p["mv"],
                "pnl": p["pnl"],
                "pnl_pct": round(p["pnl_pct"], 4),
                "pre_close": 0,
                "daily_pnl": round(stock_daily, 2),
                "holding_days": 0,
                "entry_date": TODAY,
                "locked_volume": p["vol"],  # T+1
            }
        )

    for pr in pos_rows:
        conn.execute(
            """INSERT OR REPLACE INTO trade_portfolio_positions
               (trade_date, account, stock_code, stock_name, volume, avg_cost,
                current_price, market_value, pnl, pnl_pct, pre_close, daily_pnl,
                holding_days, entry_date, locked_volume, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                TODAY,
                ACCOUNT,
                pr["stock_code"],
                pr["stock_name"],
                pr["volume"],
                pr["avg_cost"],
                pr["current_price"],
                pr["market_value"],
                pr["pnl"],
                pr["pnl_pct"],
                pr["pre_close"],
                pr["daily_pnl"],
                pr["holding_days"],
                pr["entry_date"],
                pr["locked_volume"],
                now,
            ),
        )
    # 清理已卖出但遗留的持仓行
    active_codes = [p["code"] for p in positions]
    conn.execute(
        f"""DELETE FROM trade_portfolio_positions
            WHERE trade_date=? AND account=? AND stock_code NOT IN ({",".join("?" for _ in active_codes)})""",
        [TODAY, ACCOUNT] + active_codes,
    )
    print(f"\n✅ 步骤3: 持仓表已更新 ({len(positions)} 只)，已清理卖出记录")

    # ── 4. 写快照 ──
    conn.execute(
        """INSERT OR REPLACE INTO trade_portfolio_snapshots
           (trade_date, total_value, cash, market_value, daily_pnl, total_pnl,
            drawdown, position_count, sector_exposure, account, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, '{}', ?, ?)""",
        (
            TODAY,
            round(total_value, 2),
            round(cash, 2),
            round(total_mv, 2),
            round(daily_pnl, 2),
            round(total_pnl, 2),
            position_count,
            ACCOUNT,
            now,
        ),
    )
    print("✅ 步骤3: 快照已保存")

    # ── 5. 复制持仓到次日 ──
    for pr in pos_rows:
        conn.execute(
            """INSERT OR REPLACE INTO trade_portfolio_positions
               (trade_date, account, stock_code, stock_name, volume, avg_cost,
                current_price, market_value, pnl, pnl_pct, pre_close, daily_pnl,
                holding_days, entry_date, locked_volume, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?)""",
            (
                NEXT_DATE,
                ACCOUNT,
                pr["stock_code"],
                pr["stock_name"],
                pr["volume"],
                pr["avg_cost"],
                pr["current_price"],
                pr["market_value"],
                0,
                0,
                pr["current_price"],  # pre_close = 今日收盘
                pr["holding_days"] + 1,
                pr["entry_date"],
                now,
            ),
        )

    # 快照也复制到次日
    conn.execute(
        """INSERT OR REPLACE INTO trade_portfolio_snapshots
           (trade_date, total_value, cash, market_value, daily_pnl, total_pnl,
            drawdown, position_count, sector_exposure, account, created_at)
           VALUES (?, ?, ?, ?, 0, ?, 0, ?, '{}', ?, ?)""",
        (
            NEXT_DATE,
            round(total_value, 2),
            round(cash, 2),
            round(total_mv, 2),
            round(total_pnl, 2),
            position_count,
            ACCOUNT,
            now,
        ),
    )
    print(f"✅ 步骤4: 持仓已复制到 {NEXT_DATE}")

    # ── 6. 过期 pending 信号 ──
    pending = get_pending_signals(conn)
    if pending:
        ids = [s["id"] for s in pending]
        conn.execute(
            f"UPDATE trade_signals SET status='expired' WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        )
        print(f"✅ 步骤5: 过期 {len(pending)} 个 pending 信号:")
        for s in pending:
            print(f"       {s['id']} {s['stock_code']} {s['stock_name']}")
    else:
        print("   (无 pending 信号)")

    conn.commit()

    # ── 7. 打印总结 ──
    print()
    print("=" * 60)
    print("  📊 模拟盘收盘持仓报告")
    print("=" * 60)
    print(f"   {TODAY}")
    print("   " + "─" * 50)
    print(
        f"   总资产: {total_value:,.0f}  现金: {cash:,.0f}  总盈亏: {total_pnl:+,.0f} ({total_pnl / INITIAL_CASH * 100:+.2f}%)"
    )
    print(f"   持仓: {position_count} 只  仓位: {total_mv / total_value * 100:.0f}%")
    print(f"   当日盈亏: {daily_pnl:+,.0f} ({daily_pnl_pct:+.2f}%)")
    print()

    for p in positions:
        emoji = (
            "✅"
            if p["pnl_pct"] > 0.03
            else (
                "🟢" if p["pnl_pct"] > 0 else ("🟡" if p["pnl_pct"] > -0.03 else "🔴")
            )
        )
        print(
            f"   {emoji} {p['code']} {p['name']}  "
            f"收盘 {p['close']:.2f}  成本 {p['cost']:.2f}  "
            f"盈亏 {p['pnl']:+,.0f} ({p['pnl_pct'] * 100:+.2f}%)"
        )

    print()
    print(f"   今日成交 {len(orders)} 笔")
    for o in orders:
        otype = "买入" if o["order_type"] == "buy" else "卖出"
        print(
            f"   {otype} {o['stock_code']}  "
            f"{o['filled_price']:.2f} × {o['filled_volume']}股  "
            f"金额 {o['filled_amount']:,.0f}"
        )

    # ── 8. 实盘总结 ──
    real_orders = conn.execute(
        """SELECT * FROM trade_orders
           WHERE trade_date=? AND account='real' AND order_status='filled'
           ORDER BY id""",
        (TODAY,),
    ).fetchall()
    if real_orders:
        print()
        print("=" * 60)
        print("  📊 实盘持仓报告")
        print("=" * 60)
        for o in real_orders:
            otype = "买入" if o["order_type"] == "buy" else "卖出"
            print(
                f"   {otype} {o['stock_code']}  "
                f"{o['filled_price']:.2f} × {o['filled_volume']}股  "
                f"金额 {o['filled_amount']:,.0f}"
            )
    else:
        print("\n   (今日无实盘成交)")

    # ── 9. 审计 ──
    print()
    print("=" * 60)
    print("  🔍 收盘审计")
    print("=" * 60)
    try:
        from system.config.settings import AUDIT_ENABLED
    except Exception:
        AUDIT_ENABLED = False

    if AUDIT_ENABLED:
        try:
            from audit.watcher_rule_auditor import RuleAuditor
            from data.repo import TradeRepository

            repo = TradeRepository(db_path=DB)
            rule = RuleAuditor(repo=repo)
            n = len(rule.run_and_save(TODAY))
            print(f"  规则审计: {n} 条发现")

            if n > 0:
                from audit.watcher_ai_auditor import AIAuditor

                ai = AIAuditor(repo=repo)
                result = ai.run_and_save(TODAY)
                if result:
                    print(
                        f"  AI 审计: {len(result.get('improvements', []))} 改进, "
                        f"{len(result.get('lessons', []))} 教训"
                    )
            print("✅ 步骤7: 审计完成")
        except Exception as e:
            print(f"⚠️ 审计异常: {e}")
    else:
        print("  审计未启用 (AUDIT_ENABLED=False)")

    conn.close()
    print()
    print("=" * 60)
    print("  恢复完成 ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
