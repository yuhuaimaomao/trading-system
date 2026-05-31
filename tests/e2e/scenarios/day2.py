# -*- coding: utf-8 -*-
"""Day2 场景定义 — 跨日状态 + 极端行情。

Day1 收盘后，Day2 以新 Watcher 启动，验证：
  1. 持仓从 trade_orders 恢复
  2. _prev_total 从快照恢复
  3. 跨日状态变量全部清空
  4. _bought_watch.max_profit_pct 从 DB 恢复

大盘走势: 跳空低开 → 单边跌 → 恐慌 → 钓鱼线 → M顶 → 死猫跳 → 收盘跳水
"""

from tests.e2e.sim_qmt import (
    SimQMT, StockTrajectory, IndexTrajectory,
)


def _build_day2_index_sequence(base: float = 3250.0) -> list[float]:
    """Day2: 跳空低开，更极端的行情。"""
    import math as _m
    prices = []
    for scan in range(240):
        if scan == 0:
            p = base
        elif scan <= 20:     # gap_down_recover: 低开→回升
            p = base + (scan - 1) * 1.0
        elif scan <= 40:     # one_sided: 单边下跌
            p = 3220 - (scan - 21) * 2.0
        elif scan <= 60:     # panic: 加速下跌
            p = 3180 - (scan - 41) * 1.5
        elif scan <= 80:     # dead_cat: 弱反弹
            p = 3150 + (scan - 61) * 2.5
        elif scan <= 100:    # fishing_line 前半: 慢涨
            p = 3200 + (scan - 81) * 2.5
        elif scan <= 120:    # fishing_line 后半: 急跌
            p = 3250 - (scan - 101) * 3.5
        elif scan <= 160:    # m_top: 双顶
            frac = (scan - 121) / 39
            half = min(frac, 1.0 - frac) if frac <= 0.5 else 1.0 - frac
            p = 3180 + _m.sin(frac * 2 * _m.pi) * 15
        elif scan <= 200:    # dead_cat again
            p = 3160 + (scan - 161) * 0.75
        else:                # late_dump
            p = 3190 - (scan - 201) * 1.25

        prices.append(round(p, 2))
    return prices


def build_day2_scenario(qmt: SimQMT, db_path: str):
    """配置 Day2 场景。Day2 没有新的 pending 信号，只有从 Day1 继承的持仓。"""
    import sqlite3

    # ── 上证指数 ──
    base = 3250.0
    idx_prices = _build_day2_index_sequence(base)
    idx_traj = IndexTrajectory()
    idx_traj.generate_from_prices(idx_prices, base)
    qmt.set_index(idx_traj)

    idx_stock = StockTrajectory(code="000001", base_price=base, sector="指数")
    idx_stock.prices = idx_prices
    idx_stock.highs = [round(p * 1.005, 2) for p in idx_prices]
    idx_stock.lows = [round(p * 0.995, 2) for p in idx_prices]
    idx_stock.opens = [idx_prices[0]] + idx_prices[:-1]
    qmt.add_stock(idx_stock)

    # ── 从 DB 读取 Day1 持仓（bought 信号） ──
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    bought_rows = conn.execute(
        """SELECT ts.stock_code, sb.price as base_price, sb.industry
           FROM trade_signals ts
           LEFT JOIN stock_basic sb ON sb.stock_code = ts.stock_code
             AND sb.trade_date = (SELECT MAX(trade_date) FROM stock_basic)
           WHERE ts.status='bought' AND ts.account='paper'
           ORDER BY ts.id DESC LIMIT 5"""
    ).fetchall()
    conn.close()

    # ── 每只 bought 持仓：继续 Day1 的价格轨迹 ──
    for row in bought_rows:
        code = row["stock_code"]
        bp = row["base_price"] or 10.0
        sector = row["industry"] or ""
        limit_pct = 0.20 if str(code).startswith(("688", "300")) else 0.10

        t = StockTrajectory(code=code, base_price=bp, sector=sector, limit_pct=limit_pct)

        if code == "300727":
            # 继续下跌
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 240, "from_pct": -0.08, "to_pct": -0.12},
            ])
        elif code == "000791":
            # 继续回落
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 240, "from_pct": 0.03, "to_pct": -0.02},
            ])
        else:
            t.generate_flat(240)

        qmt.add_stock(t)
        qmt.set_minute_kline(code, t.prices)
        qmt.set_ticks(code, buy_ratio=0.4)

    # ── 板块对照股（同 Day1） ──
    sector_stocks = [
        ("600519", "贵州茅台", "白酒", 1728.00),
        ("300750", "宁德时代", "锂电池", 208.00),
        ("002371", "北方华创", "半导体", 387.00),
        ("601899", "紫金矿业", "黄金", 19.40),
        ("600036", "招商银行", "银行", 38.00),
        ("000858", "五粮液", "食品", 144.00),
        ("300274", "阳光电源", "光伏", 73.60),
    ]
    for code, name, industry, bp in sector_stocks:
        limit_pct = 0.20 if str(code).startswith(("688", "300")) else 0.10
        t = StockTrajectory(code=code, base_price=bp, sector=industry, limit_pct=limit_pct)
        t.generate_flat(240, noise=0.002)
        qmt.add_stock(t)
        qmt.set_minute_kline(code, t.prices)

    print(f"  Day2 场景就绪: {len(qmt._stocks)} 只个股 + 上证指数")
