"""收盘恢复 — 2026-06-10 Watcher 异常退出，补执行 _finalize_close。

用法:  python ops/tools/recover_close_20260610.py
"""

import sqlite3
from datetime import datetime

DB = "storage/stock_market.db"
TODAY = "2026-06-10"
NEXT_DATE = "2026-06-11"
ACCOUNT = "paper"
INITIAL_CASH = 200_000


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    now = datetime.now().isoformat()
    today_dt = datetime.now()

    print("=" * 60)
    print(f"  收盘恢复  {TODAY}")
    print("=" * 60)

    # ── 1. 获取昨日快照作为基准 ──
    yesterday = conn.execute(
        """SELECT * FROM trade_portfolio_snapshots
           WHERE trade_date < ? AND account=?
           ORDER BY id DESC LIMIT 1""",
        (TODAY, ACCOUNT),
    ).fetchone()
    if yesterday:
        print(
            f"\n昨日快照({yesterday['trade_date']}): "
            f"total={yesterday['total_value']:,.0f}  "
            f"cash={yesterday['cash']:,.0f}  "
            f"mv={yesterday['market_value']:,.0f}  "
            f"pos={yesterday['position_count']}"
        )
        prev_total = yesterday["total_value"]
    else:
        print("⚠️ 无昨日快照，以初始资金为基准")
        prev_total = INITIAL_CASH

    # ── 2. 获取当日成交 ──
    orders = conn.execute(
        """SELECT * FROM trade_orders
           WHERE trade_date=? AND account=? AND order_status='filled'
           ORDER BY id""",
        (TODAY, ACCOUNT),
    ).fetchall()
    buys = [o for o in orders if o["order_type"] == "buy"]
    sells = [o for o in orders if o["order_type"] == "sell"]
    print(f"今日成交: {len(buys)} 买 + {len(sells)} 卖 = {len(orders)} 笔")

    # ── 3. 获取当前持仓 ──
    positions = conn.execute(
        """SELECT * FROM trade_portfolio_positions
           WHERE trade_date=? AND account=?
           ORDER BY stock_code""",
        (TODAY, ACCOUNT),
    ).fetchall()

    if positions:
        # 用今日最后一批 market_snapshots 收盘价更新持仓
        for pos in positions:
            code = pos["stock_code"]
            # 取今日最后一条行情快照的收盘价
            snap = conn.execute(
                """SELECT close FROM market_snapshots
                   WHERE stock_code=? AND trade_date=?
                   ORDER BY id DESC LIMIT 1""",
                (code, TODAY),
            ).fetchone()
            if snap and snap[0]:
                close_price = float(snap[0])
                vol = pos["volume"]
                cost = pos["avg_cost"]
                mv = close_price * vol
                pnl = (close_price - cost) * vol
                pnl_pct = (close_price - cost) / cost if cost > 0 else 0
                conn.execute(
                    """UPDATE trade_portfolio_positions
                       SET current_price=?, market_value=?, pnl=?, pnl_pct=?
                       WHERE trade_date=? AND account=? AND stock_code=?""",
                    (round(close_price, 2), round(mv, 2), round(pnl, 2), round(pnl_pct, 4), TODAY, ACCOUNT, code),
                )
                print(f"  更新 {code} {pos['stock_name']}: close={close_price:.2f} pnl={pnl:+,.0f}")
    else:
        print("无持仓")

    # 重新读取更新后的持仓
    positions = conn.execute(
        """SELECT * FROM trade_portfolio_positions
           WHERE trade_date=? AND account=?
           ORDER BY stock_code""",
        (TODAY, ACCOUNT),
    ).fetchall()

    # ── 4. 计算当前状态 ──
    # 从昨日快照现金推算当日现金
    if yesterday:
        cash = yesterday["cash"]
    else:
        cash = INITIAL_CASH
    for o in orders:
        if o["order_type"] == "buy":
            cash -= (o["filled_amount"] or 0) + (o["commission"] or 0)
        else:
            cash += (o["filled_amount"] or 0) - (o["commission"] or 0)
    cash_from_orders = cash

    total_mv = sum(p["market_value"] for p in positions)
    total_value = cash_from_orders + total_mv
    total_pnl = total_value - INITIAL_CASH
    daily_pnl = total_value - prev_total

    position_count = len(positions)

    print("\n当前状态:")
    print(f"  现金: {cash_from_orders:,.0f}")
    print(f"  市值: {total_mv:,.0f}")
    print(f"  总资产: {total_value:,.0f}")
    print(f"  总盈亏: {total_pnl:+,.0f} ({total_pnl / INITIAL_CASH * 100:+.2f}%)")
    print(f"  当日盈亏: {daily_pnl:+,.0f}")
    print(f"  持仓: {position_count} 只")
    if total_value > 0:
        print(f"  仓位: {total_mv / total_value * 100:.0f}%")

    # ── 5. 保存快照 ──
    # 先删除今天已有的非收盘快照（14:36那个）
    conn.execute(
        """DELETE FROM trade_portfolio_snapshots
           WHERE trade_date=? AND account=? AND created_at < ?""",
        (TODAY, ACCOUNT, today_dt.strftime("%Y-%m-%dT15:00:00")),
    )
    conn.execute(
        """INSERT OR REPLACE INTO trade_portfolio_snapshots
           (trade_date, total_value, cash, market_value, daily_pnl, total_pnl,
            drawdown, position_count, sector_exposure, account, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, '{}', ?, ?)""",
        (
            TODAY,
            round(total_value, 2),
            round(cash_from_orders, 2),
            round(total_mv, 2),
            round(daily_pnl, 2),
            round(total_pnl, 2),
            position_count,
            ACCOUNT,
            now,
        ),
    )
    print("\n✅ 步骤1: 收盘快照已保存")

    # ── 6. 复制持仓到下一个交易日 ──
    for p in positions:
        conn.execute(
            """INSERT OR REPLACE INTO trade_portfolio_positions
               (trade_date, account, stock_code, stock_name, volume, avg_cost,
                current_price, market_value, pnl, pnl_pct, pre_close, daily_pnl,
                holding_days, entry_date, locked_volume, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?)""",
            (
                NEXT_DATE,
                ACCOUNT,
                p["stock_code"],
                p["stock_name"],
                p["volume"],
                p["avg_cost"],
                p["current_price"],
                p["market_value"],
                0,  # pnl 重置
                0,  # pnl_pct 重置
                p["current_price"],  # pre_close = 今日收盘价
                (p["holding_days"] or 0) + 1,
                p["entry_date"],
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
            round(cash_from_orders, 2),
            round(total_mv, 2),
            round(total_pnl, 2),
            position_count,
            ACCOUNT,
            now,
        ),
    )
    print(f"✅ 步骤2: 持仓已复制到 {NEXT_DATE} ({position_count} 只)")

    # ── 7. 过期旧 pending 信号（trade_date < TODAY 的） ──
    old_pending = conn.execute(
        "SELECT id, stock_code, stock_name FROM trade_signals WHERE status='pending' AND trade_date < ?",
        (TODAY,),
    ).fetchall()
    if old_pending:
        ids = [s["id"] for s in old_pending]
        conn.execute(
            f"UPDATE trade_signals SET status='expired', executed_at=? WHERE id IN ({','.join('?' for _ in ids)})",
            [now] + ids,
        )
        print(f"✅ 步骤3: 过期 {len(old_pending)} 个旧 pending 信号")
        for s in old_pending:
            print(f"       {s['id']} {s['stock_code']} {s['stock_name']}")
    else:
        print("   (无旧 pending 信号需过期)")

    conn.commit()

    # ── 8. 打印收盘报告 ──
    print()
    print("=" * 60)
    print("  📊 模拟盘收盘持仓报告")
    print("=" * 60)
    print(f"   {TODAY}")
    print("   " + "─" * 50)
    print(
        f"   总资产: {total_value:,.0f}  现金: {cash_from_orders:,.0f}  "
        f"持仓: {position_count} 只  "
        f"仓位: {total_mv / total_value * 100:.0f}%"
        if total_value > 0
        else "仓位: 0%"
    )
    print(f"   总盈亏: {total_pnl:+,.0f} ({total_pnl / INITIAL_CASH * 100:+.2f}%)  当日盈亏: {daily_pnl:+,.0f}")

    if positions:
        print()
        for p in positions:
            pnl_pct = p["pnl_pct"]
            emoji = "🔴" if pnl_pct > 0.005 else ("🟢" if pnl_pct < -0.005 else "🟡")
            print(
                f"   {emoji} {p['stock_code']} {p['stock_name']}  "
                f"收盘 {p['current_price']:.2f}  成本 {p['avg_cost']:.2f}  "
                f"盈亏 {p['pnl']:+,.0f} ({pnl_pct * 100:+.2f}%)"
            )
    else:
        print("\n   📦 空仓")

    if orders:
        print(f"\n   今日成交 {len(orders)} 笔")
        for o in orders:
            otype = "买入" if o["order_type"] == "buy" else "卖出"
            print(
                f"   {otype} {o['stock_code']}  "
                f"{o['filled_price']:.2f} × {o['filled_volume']}股  "
                f"金额 {o['filled_amount']:,.0f}"
            )

    # ── 9. Telegram 推送 ──
    print()
    print("=" * 60)
    print("  📨 Telegram 推送")
    print("=" * 60)
    try:
        from system.message import MessageSender

        telegram = MessageSender()
        report_lines = [
            f"📊 收盘持仓报告  {TODAY}",
            "   ─────────────────────────",
            f"   总资产: {total_value:,.0f}  现金: {cash_from_orders:,.0f}  "
            f"持仓: {position_count} 只  "
            f"仓位: {total_mv / total_value * 100:.0f}%"
            if total_value > 0
            else "仓位: 0%",
            f"   总盈亏: {total_pnl:+,.0f} ({total_pnl / INITIAL_CASH * 100:+.2f}%)  当日盈亏: {daily_pnl:+,.0f}",
        ]
        if not positions:
            report_lines.append("\n   📦 空仓（今日无交易）")

        msg = "\n".join(report_lines)
        telegram.send(msg)
        print("✅ 模拟盘收盘报告已推送")

        # 实盘报告
        from system.config import settings

        if settings.REAL_TRADE_ENABLED:
            real_orders = conn.execute(
                """SELECT * FROM trade_orders
                   WHERE trade_date=? AND account='real' AND order_status='filled'
                   ORDER BY id""",
                (TODAY,),
            ).fetchall()
            if real_orders:
                real_lines = [
                    f"📊 实盘持仓报告  {TODAY}",
                    "   ─────────────────────────",
                ]
                for o in real_orders:
                    otype = "买入" if o["order_type"] == "buy" else "卖出"
                    real_lines.append(
                        f"   {otype} {o['stock_code']}  "
                        f"{o['filled_price']:.2f} × {o['filled_volume']}股  "
                        f"金额 {o['filled_amount']:,.0f}"
                    )
                from system.config.settings import TELEGRAM_PRIVATE_CHAT_ID

                if TELEGRAM_PRIVATE_CHAT_ID:
                    private_tg = MessageSender(chat_id=TELEGRAM_PRIVATE_CHAT_ID)
                    private_tg.send("\n".join(real_lines))
                    print("✅ 实盘收盘报告已推送（私聊）")
            else:
                print("   (今日无实盘成交)")
        else:
            print("   实盘未启用，跳过")
    except Exception as e:
        print(f"⚠️ Telegram 推送失败: {e}")

    # ── 10. 收盘审计 ──
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
                    n_imps = len(result.get("improvements", []))
                    n_lessons = len(result.get("lessons", []))
                    print(f"  AI 审计: {n_imps} 改进, {n_lessons} 教训")

                    imps = repo.get_pending_watcher_improvements()
                    if imps:
                        from audit.watcher_improvement import format_improvement_card

                        cards = [format_improvement_card(i) for i in imps[-5:]]
                        audit_msg = (
                            f"🔧 收盘审计 {TODAY}\n"
                            f"   规则审计发现 → AI 生成 {len(imps)} 条改进建议\n\n"
                            + "\n".join(cards)
                            + "\n\n   💡 使用 /apply N 应用具体改进"
                        )
                        try:
                            from system.config.settings import TELEGRAM_PRIVATE_CHAT_ID
                            from system.message import MessageSender

                            if TELEGRAM_PRIVATE_CHAT_ID:
                                private_tg = MessageSender(chat_id=TELEGRAM_PRIVATE_CHAT_ID)
                                private_tg.send(audit_msg)
                                print("✅ 审计报告已推送（私聊）")
                        except Exception as e:
                            print(f"⚠️ 审计推送失败: {e}")
            print("✅ 步骤5: 审计完成")
        except Exception as e:
            print(f"⚠️ 审计异常: {e}")
    else:
        print("  审计未启用 (AUDIT_ENABLED=False)")

    conn.close()

    print()
    print("=" * 60)
    print("  收盘恢复完成 ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
