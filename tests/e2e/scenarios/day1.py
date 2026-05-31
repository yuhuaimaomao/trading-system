# -*- coding: utf-8 -*-
"""Day1 场景定义 — 16 种大盘模式全覆盖，30 只个股。

所有价格序列均预先计算，保证确定性。
"""

from tests.e2e.sim_qmt import (
    SimQMT, StockTrajectory, IndexTrajectory,
)


def _build_index_sequence(base: float = 3300.0) -> list[float]:
    """生成 240 轮上证指数价格序列，覆盖 16 种模式。

    模式分布（按扫描轮次）:
      0:   启动
      1-10:   normal         3300→3305  (+0.15%)
      11-25:  uptrend        3305→3320  (+0.45%)
      26-40:  melt_up        3320→3340  (+1.2%)
      41-55:  inverted_v     3340→3300  (-1.2%)
      56-75:  panic          3300→3250  (-1.5%)
      76-85:  one_sided      3250→3240  (-0.3%)
      86-100: w_bottom       3240→3250  (先跌后涨)
      101-125: (午休)
      126-145: v_reversal    3250→3300  (+1.5%)
      146-165: gap_up_fade   3300→3290  (高开低走模拟)
      166-185: wide_choppy   3290→3310  (震荡)
      186-210: gap_down_rec  3300→3320  (低开高走)
      211-225: late_rally    3310→3320  (尾盘拉升)
      226-240: late_dump     3320→3250  (尾盘跳水→收盘)
    """
    prices = []
    for scan in range(240):
        if scan == 0:
            p = base
        elif scan <= 10:     # normal: 横盘微涨
            p = base + (scan - 1) * 0.5
        elif scan <= 25:     # uptrend: 缓涨
            p = 3305 + (scan - 11) * 1.0
        elif scan <= 40:     # melt_up: 加速冲顶
            p = 3320 + (scan - 26) * 1.33
        elif scan <= 55:     # inverted_v: 高位回落
            p = 3340 - (scan - 41) * 2.67
        elif scan <= 75:     # panic: 恐慌下跌
            p = 3300 - (scan - 56) * 2.5
        elif scan <= 85:     # one_sided: 单边阴跌
            p = 3250 - (scan - 76) * 1.0
        elif scan <= 100:    # w_bottom: 二次探底回升
            frac = (scan - 86) / 14
            # 先跌到 3230 再回到 3250
            if frac < 0.5:
                p = 3240 - frac * 2 * 10  # 跌 10 点
            else:
                p = 3230 + (frac - 0.5) * 2 * 20  # 涨 20 点
        elif scan <= 125:    # 午休前震荡
            p = 3250
        elif scan <= 145:    # v_reversal: V型反转
            p = 3250 + (scan - 126) * 2.5
        elif scan <= 165:    # gap_up_fade: 模拟高开低走
            start_high = 3310  # 模拟高开
            frac = (scan - 146) / 19
            p = start_high - frac * 20  # 从 3310 回落到 3290
        elif scan <= 185:    # wide_choppy: 宽幅震荡
            import math as _m
            frac = (scan - 166) / 19
            p = 3300 + _m.sin(frac * 4 * _m.pi) * 15
        elif scan <= 210:    # gap_down_recover: 模拟低开高走
            start_low = 3290   # 模拟低开
            frac = (scan - 186) / 24
            p = start_low + frac * 30  # 从 3290 涨到 3320
        elif scan <= 225:    # late_rally: 尾盘拉升
            p = 3310 + (scan - 211) * 0.67
        else:                # late_dump: 尾盘跳水
            p = 3320 - (scan - 226) * 4.67

        prices.append(round(p, 2))

    return prices


def build_day1_scenario(qmt: SimQMT, db_path: str):
    """配置 Day1 场景的全部输入数据。

    Args:
        qmt: SimQMT 实例，用于注册个股和指数。
        db_path: 测试 DB 路径，用于读取真实昨收价。
    """
    import sqlite3

    # ── 1. 上证指数 ──
    base = 3300.0
    idx_prices = _build_index_sequence(base)
    idx_traj = IndexTrajectory()
    idx_traj.generate_from_prices(idx_prices, base)
    qmt.set_index(idx_traj)

    # 把上证指数也作为一只个股
    idx_stock = StockTrajectory(
        code="000001", base_price=base, sector="指数", limit_pct=0.10,
    )
    idx_stock.prices = idx_prices
    idx_stock.highs = [round(p * 1.005, 2) for p in idx_prices]
    idx_stock.lows = [round(p * 0.995, 2) for p in idx_prices]
    idx_stock.opens = [idx_prices[0]] + idx_prices[:-1]  # 前一个价格作为开盘
    qmt.add_stock(idx_stock)

    # ── 2. 从 DB 读取信号和持仓的昨收价 ──
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 已 bought 的持仓
    bought_rows = conn.execute(
        """SELECT ts.stock_code, ts.stock_name, ts.stop_loss, ts.take_profit,
                  sb.price as base_price, sb.industry
           FROM trade_signals ts
           LEFT JOIN stock_basic sb ON sb.stock_code = ts.stock_code
             AND sb.trade_date = (SELECT MAX(trade_date) FROM stock_basic)
           WHERE ts.status='bought' AND ts.account='paper'
           ORDER BY ts.id DESC LIMIT 5"""
    ).fetchall()

    # pending 信号
    signal_rows = conn.execute(
        """SELECT ts.stock_code, ts.stock_name, ts.buy_zone_min, ts.buy_zone_max,
                  ts.stop_loss, ts.take_profit, ts.signal_score, ts.signal_source,
                  sb.price as base_price, sb.industry
           FROM trade_signals ts
           LEFT JOIN stock_basic sb ON sb.stock_code = ts.stock_code
             AND sb.trade_date = (SELECT MAX(trade_date) FROM stock_basic)
           WHERE ts.status='pending' AND ts.account='paper'
           ORDER BY ts.id LIMIT 20"""
    ).fetchall()

    conn.close()

    # ── 3. 为持仓股定义轨迹 ──
    # 300727 润禾材料: 缓慢下跌 → 触发止损
    # 000791 甘肃能源: 先涨→回落 → 触发利润回撤止盈
    for row in bought_rows:
        code = row["stock_code"]
        bp = row["base_price"] or 10.0
        sector = row["industry"] or ""
        limit_pct = 0.20 if str(code).startswith(("688", "300")) else 0.10

        t = StockTrajectory(code=code, base_price=bp, sector=sector, limit_pct=limit_pct)

        if code == "300727":
            # 润禾材料: 横盘→缓慢下跌（触发止损）
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 50, "from_pct": 0.0, "to_pct": -0.01},
                {"start_scan": 51, "end_scan": 75, "from_pct": -0.01, "to_pct": -0.06},
                {"start_scan": 76, "end_scan": 240, "from_pct": -0.06, "to_pct": -0.08},
            ])
        elif code == "000791":
            # 甘肃能源: 慢涨→横盘→回落（测试利润回撤止盈）
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 40, "from_pct": 0.0, "to_pct": 0.06},
                {"start_scan": 41, "end_scan": 80, "from_pct": 0.06, "to_pct": 0.18},
                {"start_scan": 81, "end_scan": 100, "from_pct": 0.18, "to_pct": 0.09},
                {"start_scan": 101, "end_scan": 240, "from_pct": 0.09, "to_pct": 0.03},
            ])
        else:
            t.generate_flat(240)

        qmt.add_stock(t)
        qmt.set_minute_kline(code, t.prices)
        qmt.set_ticks(code, buy_ratio=0.5)

    # ── 4. 为 pending 信号定义轨迹 ──
    trajectory_map = {
        # 进入买入区 — 测试正常买入
        "301568": "enter_zone",      # 思泰克
        "301366": "enter_zone",      # 一博科技
        "002185": "enter_zone",      # 华天科技
        "600578": "enter_zone",      # 京能电力
        "600584": "enter_zone",      # 长电科技
        "000988": "enter_zone",      # 华工科技
        "603806": "enter_zone",      # 福斯特
        # 低于买入区 — 测试回调评估
        "002106": "below_zone",      # 莱宝高科
        "600726": "below_zone",      # 华电能源
        # 高于买入区 — 测试不追高
        "002859": "above_zone",      # 洁美科技
        "002156": "above_zone",      # 通富微电
        # 涨停封板 — 测试涨停跳过
        "300623": "limit_up_trap",   # 捷捷微电
        # V型反转后入区
        "603005": "v_then_enter",    # 晶方科技
        # 不入区
        "300408": "flat_outside",    # 三环集团
        "300319": "w_then_enter",    # 麦捷科技
    }

    for row in signal_rows:
        code = row["stock_code"]
        bp = row["base_price"] or 10.0
        sector = row["industry"] or ""
        limit_pct = 0.20 if str(code).startswith(("688", "300")) else 0.10
        traj_type = trajectory_map.get(code, "flat_outside")

        t = StockTrajectory(code=code, base_price=bp, sector=sector, limit_pct=limit_pct)

        if traj_type == "enter_zone":
            # 横盘 → 缓慢下跌 → 进入买入区
            buy_min = row["buy_zone_min"] or (bp * 0.95)
            target_pct = (buy_min + (row["buy_zone_max"] or bp) / 2 - bp) / bp
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 20, "from_pct": 0.0, "to_pct": 0.0},
                {"start_scan": 21, "end_scan": 40, "from_pct": 0.0, "to_pct": target_pct},
                {"start_scan": 41, "end_scan": 240, "from_pct": target_pct, "to_pct": target_pct - 0.01},
            ])
        elif traj_type == "below_zone":
            # 急跌 → 低于买入区（测试回调评估）
            buy_min = row["buy_zone_min"] or (bp * 0.93)
            target_pct = (buy_min * 0.93 - bp) / bp
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 0, "from_pct": 0.0, "to_pct": 0.0},
                {"start_scan": 1, "end_scan": 30, "from_pct": 0.0, "to_pct": target_pct},
                {"start_scan": 31, "end_scan": 240, "from_pct": target_pct, "to_pct": target_pct - 0.02},
            ])
        elif traj_type == "above_zone":
            # 慢涨 → 高于买入区（测试不追高）
            buy_max = row["buy_zone_max"] or (bp * 1.05)
            target_pct = (buy_max * 1.03 - bp) / bp
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 20, "from_pct": 0.0, "to_pct": target_pct},
                {"start_scan": 21, "end_scan": 240, "from_pct": target_pct, "to_pct": target_pct + 0.01},
            ])
        elif traj_type == "limit_up_trap":
            # 快速涨停封板
            limit = limit_pct
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 0, "from_pct": 0.0, "to_pct": 0.0},
                {"start_scan": 1, "end_scan": 5, "from_pct": 0.0, "to_pct": limit * 0.98},
                {"start_scan": 6, "end_scan": 240, "from_pct": limit * 0.98, "to_pct": limit * 0.99},
            ])
        elif traj_type == "v_then_enter":
            # V 型：跌→入区→再涨
            buy_min = row["buy_zone_min"] or (bp * 0.93)
            valley_pct = (buy_min * 0.97 - bp) / bp
            t.generate_v_shape(240, bottom_scan=60, fall_pct=valley_pct, rise_pct=0.01)
        elif traj_type == "w_then_enter":
            # W 型：跌→涨→再跌→再涨→入区
            buy_min = row["buy_zone_min"] or (bp * 0.94)
            target_pct = (buy_min - bp) / bp
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 40, "from_pct": 0.0, "to_pct": -0.02},
                {"start_scan": 41, "end_scan": 60, "from_pct": -0.02, "to_pct": 0.01},
                {"start_scan": 61, "end_scan": 80, "from_pct": 0.01, "to_pct": target_pct},
                {"start_scan": 81, "end_scan": 240, "from_pct": target_pct, "to_pct": target_pct + 0.02},
            ])
        else:
            # flat_outside: 始终不入区
            t.generate_flat(240, noise=0.003)

        qmt.add_stock(t)
        qmt.set_minute_kline(code, t.prices)
        qmt.set_ticks(code, buy_ratio=0.5)

    # ── 5. 板块对照股 ──
    sector_stocks = [
        ("000001", "平安银行", "银行", 12.00),
        ("600519", "贵州茅台", "白酒", 1800.00),
        ("300750", "宁德时代", "锂电池", 200.00),
        ("002371", "北方华创", "半导体", 380.00),
        ("601899", "紫金矿业", "黄金", 18.00),
        ("600036", "招商银行", "银行", 38.00),
        ("000858", "五粮液", "食品", 150.00),
        ("300274", "阳光电源", "光伏", 80.00),
    ]

    trajectory_by_sector = {
        "银行": "flat",
        "白酒": "slow_fall",
        "锂电池": "slow_rise",
        "半导体": "v_shape",
        "黄金": "sharp_rise",
        "食品": "slow_fall",
        "光伏": "sharp_fall",
    }

    for code, name, industry, bp in sector_stocks:
        limit_pct = 0.20 if str(code).startswith(("688", "300")) else 0.10
        t = StockTrajectory(code=code, base_price=bp, sector=industry, limit_pct=limit_pct)
        traj = trajectory_by_sector.get(industry, "flat")

        if traj == "flat":
            t.generate_flat(240, noise=0.002)
        elif traj == "slow_fall":
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 240, "from_pct": 0.0, "to_pct": -0.04},
            ])
        elif traj == "slow_rise":
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 240, "from_pct": 0.0, "to_pct": 0.04},
            ])
        elif traj == "v_shape":
            t.generate_v_shape(240, bottom_scan=80, fall_pct=-0.05, rise_pct=0.02)
        elif traj == "sharp_rise":
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 80, "from_pct": 0.0, "to_pct": 0.08},
                {"start_scan": 81, "end_scan": 240, "from_pct": 0.08, "to_pct": 0.10},
            ])
        elif traj == "sharp_fall":
            t.generate_linear(240, [
                {"start_scan": 0, "end_scan": 60, "from_pct": 0.0, "to_pct": -0.08},
                {"start_scan": 61, "end_scan": 240, "from_pct": -0.08, "to_pct": -0.12},
            ])

        qmt.add_stock(t)
        qmt.set_minute_kline(code, t.prices)
        qmt.set_ticks(code, buy_ratio=0.5)

    print(f"  Day1 场景就绪: {len(qmt._stocks)} 只个股 + 上证指数")
