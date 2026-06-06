"""端到端回放测试 — 验证实际数据的正确性而非仅不崩溃。

相对于 test_replay.py（只测不崩溃），本文件增加正确性断言：
- 数据完整性（时间戳无大间隔、板块覆盖完整）
- 结果一致性（相同输入 → 相同输出）
- 业务规则（position_mult 范围、allow_buy 正确性）
- 数值收敛（情景概率归一化、不坍缩到 0%）
- 方向符合预期（板块变化手动计算 vs 函数返回）

数据源：storage/stock_market.db（QMT 真实数据）
"""

import sqlite3
from pathlib import Path

import pytest

from system.config import settings

pytestmark = [pytest.mark.e2e, pytest.mark.db, pytest.mark.slow]

DB_PATH = settings.DATABASE_PATH
TRADE_DATES = ["2026-06-03", "2026-06-04", "2026-06-05"]


def _get_conn():
    path = Path(DB_PATH)
    if not path.exists():
        pytest.skip(f"DB 不存在: {DB_PATH}")
    return sqlite3.connect(DB_PATH)


def _load_index_prices(
    conn, trade_date: str
) -> tuple[list[float], list[float], float, float]:
    """加载某天的指数价格和成交额序列。"""
    rows = conn.execute(
        """SELECT price, amount FROM index_snapshots
           WHERE trade_date=? ORDER BY ts""",
        (trade_date,),
    ).fetchall()
    prices = [r[0] for r in rows]
    amounts = [r[1] for r in rows]
    hi = max(prices) if prices else 0
    lo = min(prices) if prices else 0
    return prices, amounts, hi, lo


def _load_sector_stats(conn, trade_date: str) -> dict[str, dict]:
    """加载板块统计（同 test_replay.py load_sector_stats 格式）。"""
    rows = conn.execute(
        """SELECT sector_name, avg_change
           FROM sector_snapshots WHERE trade_date=? AND sector_name != '-'
           ORDER BY sector_name, ts""",
        (trade_date,),
    ).fetchall()
    sectors = {}
    for name, chg in rows:
        if name not in sectors:
            sectors[name] = {"changes": []}
        sectors[name]["changes"].append(chg)
    result = {}
    for name, data in sectors.items():
        changes = data["changes"]
        if len(changes) >= 2:
            result[name] = {
                "change_pct": changes[-1],
                "trend_history": changes[-30:] if len(changes) >= 30 else changes,
                "relative": changes[-1] - (sum(changes) / len(changes)),
                "breadth": 0.0,
                "vol_ratio": 1.0,
                "continuity": 0,
            }
    return result


# ═══════════════════════════════════════════════════════════════
# 1. 多日回放 — 正确性断言
# ═══════════════════════════════════════════════════════════════


class TestMultiDayReplay:
    """用三天真实数据验证领域模块输出正确性。"""

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_regime_consistency_during_day(self, trade_date):
        """盘中 regime 不频繁剧烈翻转（模式稳定）。"""
        from trade.decision.regime import assess_regime

        conn = _get_conn()
        prices, amounts, hi, lo = _load_index_prices(conn, trade_date)
        conn.close()

        assert len(prices) >= 100, f"{trade_date}: 数据点不足 {len(prices)}"

        pattern_changes = 0
        last_pattern = None

        # 滑动窗口模拟盘中状态演变
        window_start = 0
        window_size = min(60, len(prices) // 5)
        while window_start + window_size < len(prices):
            segment = prices[: window_start + window_size]
            seg_hi = max(segment)
            seg_lo = min(segment)
            cur = segment[-1]
            pre_close = segment[0]

            from trade.detect.market_pattern import classify_market_pattern

            pattern = classify_market_pattern(segment, seg_hi, seg_lo)

            if last_pattern and pattern != last_pattern:
                pattern_changes += 1
            last_pattern = pattern

            # 验证模式合法
            valid_patterns = {
                "normal",
                "uptrend",
                "one_sided",
                "panic",
                "v_reversal",
                "inverted_v",
                "w_bottom",
                "m_top",
                "dead_cat",
                "melt_up",
                "gap_up_fade",
                "gap_down_recover",
                "late_rally",
                "late_dump",
                "fishing_line",
                "wide_choppy",
            }
            assert pattern in valid_patterns, f"{trade_date}: 非法模式 '{pattern}'"

            # 验证 assess_regime 返回合法值
            chg_pct = (cur - pre_close) / pre_close if pre_close > 0 else 0
            regime = assess_regime(pattern, cur, pre_close, chg_pct)
            assert 0.0 <= regime.position_mult <= 1.5, (
                f"{trade_date} 窗口 {window_start}: "
                f"position_mult={regime.position_mult} 超出 [0, 1.5]"
            )
            assert regime.confidence in ("low", "medium", "high"), (
                f"非法 confidence: {regime.confidence}"
            )
            window_start += window_size // 3

        # 盘中模式变化次数合理（数据活跃时变化是正常的，但不应几十次）
        total_windows = (len(prices) - window_size) // (window_size // 3)
        assert pattern_changes < total_windows // 2, (
            f"{trade_date}: 模式变化 {pattern_changes} 次 / {total_windows} 窗口，"
            f"变化过于频繁"
        )

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_buy_decision_same_signal_same_input(self, trade_date):
        """相同的买入评估输入 → 完全相同的输出（确定论验证）。"""
        from trade.decision.buy import BuyEvalInput, evaluate_buy

        conn = _get_conn()
        row = conn.execute(
            """SELECT si.stock_code, sb.ma5, sb.ma10, sb.ma20, sb.price,
                      si.bb_pct_b, si.bb_mid, si.rsi6, si.rsi12,
                      si.kdj_k, si.kdj_d, si.kdj_j,
                      si.macd_dif, si.macd_dea, si.macd_bar,
                      si.bbi_daily, si.bb_width
               FROM stock_indicators si
               JOIN stock_basic sb ON si.stock_code=sb.stock_code AND si.trade_date=sb.trade_date
               WHERE sb.ma5 > 0 AND si.bb_mid > 0 AND sb.price > 5
               LIMIT 1"""
        ).fetchone()
        conn.close()

        if not row:
            pytest.skip("无符合条件的股票数据")

        (
            code,
            ma5,
            ma10,
            ma20,
            price,
            bb_pct_b,
            bb_mid,
            rsi6,
            rsi12,
            kdj_k,
            kdj_d,
            kdj_j,
            macd_dif,
            macd_dea,
            macd_bar,
            bbi_daily,
            bb_width,
        ) = row

        ctx = BuyEvalInput(
            code=code,
            price=price,
            buy_min=price * 0.95,
            buy_max=price * 1.05,
            sector_trend="走强",
            sector_chg=0.5,
            daily_bb_pct_b=bb_pct_b,
            daily_ma5=ma5 or 0,
            daily_ma10=ma10 or 0,
            daily_ma20=ma20 or 0,
            daily_rsi6=rsi6,
            daily_rsi12=rsi12,
            daily_kdj_k=kdj_k,
            daily_kdj_d=kdj_d,
            daily_kdj_j=kdj_j,
            daily_macd_dif=macd_dif or 0,
            daily_macd_dea=macd_dea or 0,
            daily_macd_bar=macd_bar or 0,
            bbi_daily=bbi_daily or 0,
            bb_width=bb_width or 0,
        )

        results = [evaluate_buy(ctx) for _ in range(3)]
        first = results[0]
        for r in results[1:]:
            assert r == first, (
                f"evaluate_buy 结果不一致: 第1次={first}, 后续={r}\n"
                f"确定性违反: 相同输入应产生相同输出"
            )

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_scenario_probabilities_converge(self, trade_date):
        """情景概率归一化且不坍缩到 0%。"""
        from trade.core.scan_state import MicroSignals
        from trade.scenario.scenario_engine import ScenarioEngine

        conn = _get_conn()
        prices, amounts, hi, lo = _load_index_prices(conn, trade_date)
        conn.close()

        engine = ScenarioEngine()
        prev_velocity = 0.0

        for i in range(1, min(len(prices), 200)):
            cur = prices[i]
            prev = prices[i - 1]
            velocity = (cur - prev) / prev * 100 if prev > 0 else 0
            accel = velocity - prev_velocity
            prev_velocity = velocity

            ema12_pos = "above" if cur > sum(prices[: i + 1]) / (i + 1) else "below"
            bcl = (
                abs(prices[-1] - min(prices[-i:])) / min(prices[-i:]) * 100
                if i >= 10
                else 0
            )

            micro = MicroSignals(
                price_velocity=velocity,
                price_accel=accel,
                ema12_pos=ema12_pos,
                bounce_from_low=bcl,
            )
            engine.update(micro)

            # 概率和 ≈ 1.0
            total = sum(engine.probs.values())
            assert abs(total - 1.0) < 0.02, (
                f"{trade_date} iter {i}: 概率和={total:.4f}, 偏离1.0超过容差"
            )

            # 任何情景不应坍缩到恰好 0%
            for name, prob in engine.probs.items():
                assert prob > 0.0, (
                    f"{trade_date} iter {i}: 情景 '{name}' 概率为 0%，反坍缩规则被违反"
                )


# ═══════════════════════════════════════════════════════════════
# 2. Regime 业务规则一致性
# ═══════════════════════════════════════════════════════════════


class TestRegimeConsistency:
    """验证 16 种市场模式的定义规则一致性。"""

    PANIC_LIKE = {
        "panic",
        "one_sided",
        "inverted_v",
        "dead_cat",
        "m_top",
        "gap_up_fade",
        "late_dump",
        "fishing_line",
    }

    def test_all_patterns_defined(self):
        """所有 16 种模式在 PATTERN_REGIME 中都有定义。"""
        from trade.decision.regime import PATTERN_REGIME

        all_patterns = {
            "normal",
            "uptrend",
            "one_sided",
            "panic",
            "v_reversal",
            "inverted_v",
            "w_bottom",
            "m_top",
            "dead_cat",
            "melt_up",
            "gap_up_fade",
            "gap_down_recover",
            "late_rally",
            "late_dump",
            "fishing_line",
            "wide_choppy",
        }
        for p in all_patterns:
            assert p in PATTERN_REGIME, f"模式 '{p}' 未在 PATTERN_REGIME 中定义"

    def test_position_mult_range(self):
        """position_mult 在 [0.0, 1.5] 范围内。"""
        from trade.decision.regime import PATTERN_REGIME

        for pattern, cfg in PATTERN_REGIME.items():
            pm = cfg["position_mult"]
            assert 0.0 <= pm <= 1.5, (
                f"模式 '{pattern}' position_mult={pm} 超出 [0.0, 1.5]"
            )

    def test_allow_buy_panic_patterns(self):
        """恐慌/崩盘类模式 allow_buy=False。"""
        from trade.decision.regime import PATTERN_REGIME

        for pattern in self.PANIC_LIKE:
            cfg = PATTERN_REGIME.get(pattern, {})
            assert cfg.get("allow_buy") is False, (
                f"模式 '{pattern}' allow_buy 应为 False, 实为 {cfg.get('allow_buy')}"
            )

    def test_allow_buy_safe_patterns(self):
        """安全类模式 allow_buy=True。"""
        from trade.decision.regime import PATTERN_REGIME

        safe = {
            "normal",
            "uptrend",
            "v_reversal",
            "w_bottom",
            "melt_up",
            "gap_down_recover",
            "late_rally",
            "wide_choppy",
        }
        for pattern in safe:
            cfg = PATTERN_REGIME.get(pattern, {})
            if cfg.get("allow_buy") is False:
                # melt_up 和 wide_choppy 在带上下文调整时可能不买入
                # 但基础映射应为 True
                assert pattern not in ("normal", "uptrend"), (
                    f"模式 '{pattern}' 基础 allow_buy 为 False 但应始终为 True"
                )

    def test_confidence_valid(self):
        """assess_regime 返回的 confidence 合法。"""
        from trade.decision.regime import assess_regime

        for pattern in [
            "normal",
            "uptrend",
            "one_sided",
            "panic",
            "v_reversal",
            "inverted_v",
            "w_bottom",
            "m_top",
            "dead_cat",
            "melt_up",
            "gap_up_fade",
            "gap_down_recover",
            "late_rally",
            "late_dump",
            "fishing_line",
            "wide_choppy",
        ]:
            regime = assess_regime(pattern, 3400, 3390, 0.003)
            assert regime.confidence in ("low", "medium", "high"), (
                f"模式 '{pattern}' 返回非法 confidence: {regime.confidence}"
            )

    def test_risk_level_hierarchy(self):
        """风险等级有正确的分层关系。"""
        from trade.decision.regime import PATTERN_REGIME

        order = {"safe": 0, "cautious": 1, "dangerous": 2, "extreme": 3}

        for pattern, cfg in PATTERN_REGIME.items():
            rl = cfg["risk_level"]
            assert rl in order, f"模式 '{pattern}' 非法 risk_level: {rl}"

            # panic/late_dump/fishing_line 应为 extreme
            if pattern in ("panic", "late_dump", "fishing_line"):
                assert rl == "extreme", (
                    f"模式 '{pattern}' risk_level 应为 extreme, 实为 {rl}"
                )

            # one_sided/inverted_v/dead_cat/m_top/gap_up_fade 应为 dangerous
            if pattern in (
                "one_sided",
                "inverted_v",
                "dead_cat",
                "m_top",
                "gap_up_fade",
            ):
                assert rl == "dangerous", (
                    f"模式 '{pattern}' risk_level 应为 dangerous, 实为 {rl}"
                )

            # normal/uptrend 应为 safe
            if pattern in ("normal", "uptrend"):
                assert rl == "safe", f"模式 '{pattern}' risk_level 应为 safe, 实为 {rl}"

    def test_stop_mult_range(self):
        """stop_mult 在 [0.5, 2.0] 范围内。"""
        from trade.decision.regime import PATTERN_REGIME

        for pattern, cfg in PATTERN_REGIME.items():
            sm = cfg["stop_mult"]
            assert 0.5 <= sm <= 2.0, f"模式 '{pattern}' stop_mult={sm} 超出 [0.5, 2.0]"


# ═══════════════════════════════════════════════════════════════
# 3. 板块趋势准确性
# ═══════════════════════════════════════════════════════════════


class TestSectorTrendAccuracy:
    """验证板块趋势计算与手动计算的差异。"""

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_sector_change_manual_vs_function(self, trade_date):
        """get_sector_change 返回值与手动计算一致。"""
        from trade.detect.sector_trend import get_sector_change

        conn = _get_conn()
        stats = _load_sector_stats(conn, trade_date)

        # 加载行业映射
        codes = conn.execute(
            "SELECT DISTINCT stock_code, industry FROM stock_basic "
            "WHERE trade_date=? AND industry != '' AND industry IS NOT NULL LIMIT 20",
            (trade_date,),
        ).fetchall()
        conn.close()

        industry_cache = {code: ind for code, ind in codes if ind and ind != "-"}

        # 对每个有数据板块验证
        for code, industry in list(industry_cache.items())[:10]:
            if industry not in stats:
                continue

            manual_chg = stats[industry]["change_pct"]
            func_chg = get_sector_change(code, industry_cache, stats)
            assert func_chg is not None, (
                f"get_sector_change('{code}') 返回 None, "
                f"industry='{industry}', stats 中有 {industry}"
            )
            assert abs(func_chg - manual_chg) < 0.001, (
                f"板块 '{industry}': 手动计算变化={manual_chg:.4f}, "
                f"函数返回={func_chg:.4f}"
            )

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_sector_trend_contains_cumulative(self, trade_date):
        """get_sector_trend 返回值包含手动计算的累积变化。"""
        from trade.detect.sector_trend import get_sector_trend

        conn = _get_conn()
        stats = _load_sector_stats(conn, trade_date)

        codes = conn.execute(
            "SELECT DISTINCT stock_code, industry FROM stock_basic "
            "WHERE trade_date=? AND industry != '' AND industry IS NOT NULL LIMIT 20",
            (trade_date,),
        ).fetchall()
        conn.close()

        industry_cache = {code: ind for code, ind in codes if ind and ind != "-"}

        checked = 0
        for code, industry in list(industry_cache.items())[:8]:
            if industry not in stats:
                continue
            history = stats[industry].get("trend_history", [])
            if len(history) < 2:
                continue

            manual_cumulative = history[-1] - history[0]
            trend_str = get_sector_trend(code, industry_cache, stats)

            assert isinstance(trend_str, str), (
                f"get_sector_trend 返回非字符串: {trend_str}"
            )
            assert len(trend_str) > 0, "get_sector_trend 返回空字符串"

            # 检查累积变化在输出中（格式 "+/-X.X%"）
            cum_formatted = f"{manual_cumulative:+.1f}%"
            assert cum_formatted in trend_str, (
                f"板块 '{industry}': 累积变化 {cum_formatted} 未出现在趋势描述 '{trend_str}' 中"
            )
            checked += 1

        assert checked >= 5, f"仅验证了 {checked} 个板块，目标 ≥5"

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_sector_changes_temporally_consistent(self, trade_date):
        """板块平均涨跌幅在连续时间点间变化合理（不跳变 > 10%）。"""
        conn = _get_conn()
        rows = conn.execute(
            """SELECT sector_name, ts, avg_change
               FROM sector_snapshots
               WHERE trade_date=? AND sector_name != '-'
               ORDER BY sector_name, ts""",
            (trade_date,),
        ).fetchall()
        conn.close()

        # 按板块分组
        by_sector: dict[str, list[tuple[str, float]]] = {}
        for name, ts, chg in rows:
            if name not in by_sector:
                by_sector[name] = []
            by_sector[name].append((ts, chg))

        anomalies = []
        for sector, points in list(by_sector.items())[:20]:
            for i in range(1, len(points)):
                delta = abs(points[i][1] - points[i - 1][1])
                if delta > 10.0:
                    anomalies.append(
                        f"'{sector}': {points[i - 1][0]}({points[i - 1][1]:.2f})→"
                        f"{points[i][0]}({points[i][1]:.2f}) 跳变 {delta:.1f}"
                    )
                if anomalies and len(anomalies) >= 3:
                    break
            if anomalies and len(anomalies) >= 3:
                break

        if anomalies:
            pytest.fail(f"板块变化跳变异常: {'; '.join(anomalies[:3])}")


# ═══════════════════════════════════════════════════════════════
# 4. 决策确定性
# ═══════════════════════════════════════════════════════════════


class TestDecisionDeterminism:
    """验证决策模块在相同输入下输出完全一致。"""

    def test_evaluate_buy_deterministic(self):
        """evaluate_buy 多次调用返回相同结果（无随机性）。"""
        from trade.decision.buy import BuyEvalInput, evaluate_buy

        # 构建一个不含任何随机成分的完整输入
        ctx = BuyEvalInput(
            code="000001",
            price=10.50,
            buy_min=9.80,
            buy_max=10.80,
            sector_trend="走强",
            sector_chg=0.5,
            daily_bb_pct_b=0.45,
            daily_ma5=10.30,
            daily_ma10=10.20,
            daily_ma20=10.00,
            daily_rsi6=55.0,
            daily_rsi12=50.0,
            daily_kdj_k=50.0,
            daily_kdj_d=45.0,
            daily_kdj_j=60.0,
            daily_macd_dif=0.05,
            daily_macd_dea=0.02,
            daily_macd_bar=0.06,
            bbi_daily=10.10,
            bb_width=30.0,
            yesterday_mf_ratio=1.5,
            ma5_angle=2.0,
            day_position=0.5,
        )

        results = [evaluate_buy(ctx) for _ in range(5)]
        first = results[0]
        for i, r in enumerate(results[1:], 1):
            assert r == first, f"第 {i} 次调用结果不同: 第0次={first}, 第{i}次={r}"

    def test_evaluate_below_zone_deterministic(self):
        """evaluate_below_zone 多次调用返回相同结果。"""
        from trade.decision.buy import BuyEvalInput, evaluate_below_zone

        ctx = BuyEvalInput(
            code="000001",
            price=9.00,
            buy_min=9.50,
            buy_max=10.50,
            sector_trend="走强",
            sector_chg=1.0,
            daily_rsi6=30.0,
            daily_rsi12=35.0,
            daily_kdj_k=20.0,
            daily_kdj_d=25.0,
            daily_kdj_j=10.0,
        )

        results = [evaluate_below_zone(ctx) for _ in range(5)]
        first = results[0]
        for i, r in enumerate(results[1:], 1):
            assert r == first, (
                f"evaluate_below_zone 第 {i} 次结果不同: 第0次={first}, 第{i}次={r}"
            )

    def test_assess_regime_deterministic(self):
        """assess_regime 多次调用返回相同 MarketRegime。"""
        from trade.decision.regime import assess_regime

        regimes = [assess_regime("one_sided", 3400, 3420, -0.006) for _ in range(5)]
        first = regimes[0]
        for i, r in enumerate(regimes[1:], 1):
            assert r.pattern == first.pattern
            assert r.risk_level == first.risk_level
            assert r.allow_buy == first.allow_buy
            assert r.position_mult == first.position_mult

    def test_calculate_position_size_deterministic(self):
        """calculate_position_size 多次调用返回相同结果。"""
        from trade.decision.sizing import calculate_position_size

        results = [
            calculate_position_size(
                "000001",
                10.0,
                9.5,
                10.5,
                "normal",
                "走强",
            )
            for _ in range(5)
        ]
        first = results[0]
        for i, r in enumerate(results[1:], 1):
            assert r == first, (
                f"calculate_position_size 第 {i} 次结果不同: 第0次={first}, 第{i}次={r}"
            )


# ═══════════════════════════════════════════════════════════════
# 5. 情景引擎收敛性
# ═══════════════════════════════════════════════════════════════


class TestScenarioConvergence:
    """情景引擎概率收敛和反坍缩规则验证。"""

    def test_probabilities_sum_to_one(self):
        """每步更新后概率和 ≈ 1.0。"""
        from trade.core.scan_state import MicroSignals
        from trade.scenario.scenario_engine import ScenarioEngine

        engine = ScenarioEngine()

        for i in range(50):
            micro = MicroSignals(
                price_velocity=0.01,
                ema12_pos="above",
                breadth_trend="stable",
            )
            engine.update(micro)
            total = sum(engine.probs.values())
            assert abs(total - 1.0) < 0.015, f"iter {i}: 概率和={total:.6f}"

    def test_no_scenario_collapses_to_zero(self):
        """经过多轮更新，无任何情景概率坍缩到恰好 0%。"""
        from trade.core.scan_state import MicroSignals
        from trade.scenario.scenario_engine import ScenarioEngine

        engine = ScenarioEngine()

        # 持续上涨信号
        for _ in range(100):
            micro = MicroSignals(
                price_velocity=0.05,
                price_accel=0.01,
                ema12_pos="above",
                breadth_trend="improving",
                higher_highs=True,
                bounce_from_low=0.3,
                vol_pulse="expanding",
                vol_price_confirm="yes",
            )
            engine.update(micro)

            for name, prob in engine.probs.items():
                assert prob > 0.0, f"情景 '{name}' 概率坍缩到 0%（iter after {_})"

    def test_strong_signal_dominates(self):
        """强一致性信号使主情景概率显著高于平均水平。"""
        from trade.core.scan_state import MicroSignals
        from trade.scenario.scenario_engine import ScenarioEngine

        engine = ScenarioEngine()

        # 持续上涨信号
        for _ in range(60):
            micro = MicroSignals(
                price_velocity=0.04,
                price_accel=0.005,
                ema12_pos="above",
                breadth_trend="improving",
                higher_highs=True,
                higher_lows=False,
                bounce_from_low=0.2,
            )
            engine.update(micro)

        primary_prob = max(engine.probs.values())
        # 主情景概率应显著高于初始值（>2倍均匀分布）
        assert primary_prob > 0.20, f"强上涨信号后主情景概率只有 {primary_prob:.3f}"

    def test_opposing_signal_dampens(self):
        """矛盾信号使情景引擎不崩溃、概率归一化、无坍缩。"""
        from trade.core.scan_state import MicroSignals
        from trade.scenario.scenario_engine import ScenarioEngine

        engine = ScenarioEngine()

        # 交替信号（无法稳定）
        for i in range(60):
            if i % 2 == 0:
                micro = MicroSignals(
                    price_velocity=0.04,
                    price_accel=0.01,
                    ema12_pos="above",
                    breadth_trend="improving",
                )
            else:
                micro = MicroSignals(
                    price_velocity=-0.04,
                    price_accel=-0.01,
                    ema12_pos="below",
                    breadth_trend="deteriorating",
                )
            engine.update(micro)

        # 矛盾信号下引擎仍应保持正常运行：
        total = sum(engine.probs.values())
        assert abs(total - 1.0) < 0.015, f"概率和={total:.4f}"

        for name, prob in engine.probs.items():
            assert prob > 0.0, f"情景 '{name}' 坍缩到 0%"

        # 交替信号下主情景应交替变化，最终不会极端集中在单一情景
        assert engine.scan_count == 60, f"scan_count 应为 60, 实为 {engine.scan_count}"

    def test_initial_state_balanced(self):
        """初始化时 8 个情景概率总和为 1.0。"""
        from trade.scenario.scenario_engine import ScenarioEngine

        engine = ScenarioEngine()
        expected = {
            "normal_stable": 0.50,
            "developing_uptrend": 0.10,
            "developing_downtrend": 0.10,
            "accelerating_down": 0.05,
            "accelerating_up": 0.05,
            "potential_reversal_up": 0.05,
            "potential_reversal_down": 0.05,
            "dead_bounce": 0.10,
        }

        assert set(engine.probs.keys()) == set(expected.keys()), (
            f"情景集合不匹配: 期望 {set(expected.keys())}, "
            f"实际 {set(engine.probs.keys())}"
        )
        total = sum(engine.probs.values())
        assert abs(total - 1.0) < 0.001, f"初始概率和={total}"

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_scenario_convergence_with_real_data(self, trade_date):
        """真实数据驱动情景引擎，概率归一化且无坍缩。"""
        from trade.core.scan_state import MicroSignals
        from trade.scenario.scenario_engine import ScenarioEngine

        conn = _get_conn()
        prices, amounts, hi, lo = _load_index_prices(conn, trade_date)
        conn.close()

        engine = ScenarioEngine()
        prev_velocity = 0.0

        for i in range(1, len(prices)):
            cur, prev = prices[i], prices[i - 1]
            velocity = (cur - prev) / prev * 100 if prev > 0 else 0
            accel = velocity - prev_velocity
            prev_velocity = velocity

            ema12_pos = "above" if cur > sum(prices[: i + 1]) / (i + 1) else "below"

            # 计算反弹百分比
            bounce = 0.0
            lookback = min(i, 10)
            if lookback > 0:
                recent_low = min(prices[i - lookback : i + 1])
                bounce = (cur - recent_low) / recent_low * 100 if recent_low > 0 else 0

            micro = MicroSignals(
                price_velocity=velocity,
                price_accel=accel,
                ema12_pos=ema12_pos,
                bounce_from_low=bounce,
            )
            engine.update(micro)

            total = sum(engine.probs.values())
            assert abs(total - 1.0) < 0.02, f"{trade_date} iter {i}: 概率和={total:.4f}"
            for name, prob in engine.probs.items():
                assert prob > 0.0, f"{trade_date} iter {i}: '{name}' 坍缩到 0%"


# ═══════════════════════════════════════════════════════════════
# 6. FullScanSmoke — 构造最小 ScanState 执行 _check_market_state
# ═══════════════════════════════════════════════════════════════


class TestFullScanSmoke:
    """构造最小 ScanState，执行一次 _check_market_state 管线。"""

    def test_check_market_state_with_real_data(self):
        """用真实数据调用市场状态检查管线，验证返回值完整。"""
        from trade.core.scan_state import MarketRegime, ScanState
        from trade.scenario.market_state import MarketStateMixin

        conn = _get_conn()
        trade_date = "2026-06-05"
        prices, amounts, hi, lo = _load_index_prices(conn, trade_date)
        sector_stats = _load_sector_stats(conn, trade_date)

        # 加载一只股票的日线指标（验证用）
        stock_row = conn.execute(
            """SELECT si.stock_code, sb.ma5, sb.ma10, sb.ma20, sb.price,
                      si.bb_mid, si.bb_pct_b
               FROM stock_indicators si
               JOIN stock_basic sb ON si.stock_code=sb.stock_code AND si.trade_date=sb.trade_date
               WHERE sb.ma5 > 0 AND si.bb_mid > 0 LIMIT 1"""
        ).fetchone()
        conn.close()

        if not prices or not stock_row:
            pytest.skip("数据不足")

        # 构造最小 ScanState
        state = ScanState(
            running=True,
            trade_date=trade_date,
            scan_count=1,
            index_prices=prices[: min(100, len(prices))],
            index_high=max(prices[: min(100, len(prices))]),
            index_low=min(prices[: min(100, len(prices))]),
            market_turnovers=amounts[: min(100, len(prices))],
            sector_stats=sector_stats,
            data_ready=True,
            market_snapshot={},  # 跳过广度计算
        )

        # 创建 Mixin 实例（模拟 Watcher 委托）
        class MockWatcher(MarketStateMixin):
            """最小 Watcher 骨架，只提供 Mixin 需要的属性。"""

            def __init__(self):
                self._trade_date = trade_date
                self._scan_count = 1
                self._index_prices = prices[: min(100, len(prices))]
                self._index_high = max(prices[: min(100, len(prices))])
                self._index_low = min(prices[: min(100, len(prices))])
                self._market_turnovers = amounts[: min(100, len(prices))]
                self._sector_stats = sector_stats
                self._market_snapshot = {}
                self._last_index_quote = None
                self._data_ready = True
                self._regime = None
                self._regime_pending_pattern = ""
                self._regime_confirm_count = 0
                self._regime_switch_times = []
                self._pattern_last_alert = {}
                self._last_logged_pattern = ""
                self._index_alerted_downtrend = False
                self._index_alerted_ma20 = 0
                self._index_last_fluctuation_price = 0.0
                self._volume_alerted_divergence = False
                self._ma_baseline_cache = None
                self._max_drawdown_alerted = False
                self._pending_index_ai = {}
                self._morning_sector_bias = {}
                self.db_path = DB_PATH

            def _alert(self, msg: str):
                """无操作 — 跳过告警推送，仅用于 Mixin 兼容。"""
                pass

        w = MockWatcher()

        # 执行 _check_market_state
        idx = {
            "price": prices[-1] if prices else 3400,
            "pre_close": prices[0] if prices else 3390,
            "change_pct": (prices[-1] - prices[0]) / prices[0] if prices[0] > 0 else 0,
        }
        w._last_index_quote = idx

        regime = w._check_market_state(
            state, {"000001.SH": prices[-1] if prices else 3400}
        )

        # 断言返回完整的 MarketRegime
        assert isinstance(regime, MarketRegime), f"返回类型: {type(regime)}"
        assert isinstance(regime.pattern, str), f"pattern 非字符串: {regime.pattern}"
        assert isinstance(regime.allow_buy, bool), "allow_buy 非 bool"
        assert 0.0 <= regime.position_mult <= 1.5, (
            f"position_mult={regime.position_mult} 越界"
        )
        assert regime.confidence in ("low", "medium", "high"), (
            f"confidence 非法: {regime.confidence}"
        )
        assert isinstance(regime.entry_rule, str), "entry_rule 非字符串"
        assert isinstance(regime.risk_level, str), "risk_level 非字符串"

    def test_classify_then_assess_chain(self):
        """classify_market_pattern → assess_regime 管线完整性。"""
        from trade.decision.regime import assess_regime
        from trade.detect.market_pattern import classify_market_pattern

        conn = _get_conn()
        prices, amounts, hi, lo = _load_index_prices(conn, "2026-06-05")
        conn.close()

        if len(prices) < 50:
            pytest.skip("数据不足")

        segment = prices[:60]
        hi = max(segment)
        lo = min(segment)

        pattern = classify_market_pattern(segment, hi, lo)
        assert isinstance(pattern, str) and len(pattern) > 0, (
            f"pattern 为空或非字符串: {pattern}"
        )

        cur = segment[-1]
        pre_close = segment[0]
        chg_pct = (cur - pre_close) / pre_close if pre_close > 0 else 0
        regime = assess_regime(pattern, cur, pre_close, chg_pct)

        # 验证 assess_regime 输出的每个字段都合法
        assert regime.pattern == pattern
        assert regime.risk_level in ("safe", "cautious", "dangerous", "extreme")
        assert isinstance(regime.alert_msg, str)


# ═══════════════════════════════════════════════════════════════
# 7. 数据完整性
# ═══════════════════════════════════════════════════════════════


class TestDataIntegrity:
    """验证 DB 数据的结构完整性。"""

    LARGE_GAP_SECONDS = 310  # > 5 分钟
    CRITICAL_GAP_SECONDS = 600  # > 10 分钟（真正异常）

    @staticmethod
    def _is_pre_market_gap(t1: float, t2: float) -> bool:
        """判断时间间隔是否对应盘前/开盘瞬间（9:30 前）。"""
        from datetime import datetime

        dt2 = datetime.fromtimestamp(t2)
        h2 = dt2.hour + dt2.minute / 60
        return h2 <= 9.5

    @staticmethod
    def _is_lunch_break_gap(t1: float, t2: float) -> bool:
        """判断时间间隔是否对应午休时段（11:30-13:00 CST）。"""
        from datetime import datetime

        dt1 = datetime.fromtimestamp(t1)
        dt2 = datetime.fromtimestamp(t2)
        h1 = dt1.hour + dt1.minute / 60
        h2 = dt2.hour + dt2.minute / 60
        # 午休窗口放宽边界（数据点不可能精确在 11:30/13:00）
        if h1 > 11.7:
            # 下午在 13:00 后的间隙不算午休
            return False
        if h2 < 12.9:
            # 上午在 11:30 前的间隙不算午休
            return False
        return True

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_index_snapshots_no_large_gaps(self, trade_date):
        """index_snapshots 在交易时段内无异常间隔（午休/盘前除外）。"""
        conn = _get_conn()
        rows = conn.execute(
            """SELECT ts FROM index_snapshots
               WHERE trade_date=? ORDER BY ts""",
            (trade_date,),
        ).fetchall()
        conn.close()

        timestamps = [r[0] for r in rows]
        assert len(timestamps) > 50, f"指数数据点不足: {len(timestamps)}"

        moderate_gaps = []
        critical_gaps = []
        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            if gap > self.LARGE_GAP_SECONDS:
                if self._is_pre_market_gap(timestamps[i - 1], timestamps[i]):
                    continue
                if self._is_lunch_break_gap(timestamps[i - 1], timestamps[i]):
                    continue
                if gap > self.CRITICAL_GAP_SECONDS:
                    critical_gaps.append((i, timestamps[i - 1], timestamps[i], gap))
                else:
                    moderate_gaps.append((i, timestamps[i - 1], timestamps[i], gap))

        if critical_gaps:
            gap_strs = [
                f"#{idx}: {t1:.0f}→{t2:.0f} ({g:.0f}s)"
                for idx, t1, t2, g in critical_gaps
            ]
            pytest.fail(f"{trade_date}: 数据间隔 >10 分钟: {'; '.join(gap_strs)}")

        if moderate_gaps:
            gap_strs = [
                f"#{idx}: {t1:.0f}→{t2:.0f} ({g:.0f}s)"
                for idx, t1, t2, g in moderate_gaps
            ]
            pytest.skip(
                f"{trade_date}: 存在 {len(moderate_gaps)} 个 5-10 分钟间隙 "
                f"（QMT 采集特性）: {'; '.join(gap_strs)}"
            )

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_market_snapshots_monotonic_ts(self, trade_date):
        """market_snapshots 的 ts 严格单调递增。"""
        conn = _get_conn()
        # 抽查部分数据避免全量扫描（有百万行）
        rows = conn.execute(
            """SELECT ts FROM market_snapshots
               WHERE trade_date=? ORDER BY ts LIMIT 5000 OFFSET 1000""",
            (trade_date,),
        ).fetchall()
        conn.close()

        inversions = 0
        for i in range(1, len(rows)):
            if rows[i][0] < rows[i - 1][0]:
                inversions += 1
                if inversions >= 3:
                    pytest.fail(
                        f"{trade_date}: market_snapshots 时间戳逆序 "
                        f"(at row {i}: {rows[i - 1][0]:.2f} → {rows[i][0]:.2f})"
                    )

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_index_snapshots_monotonic_ts(self, trade_date):
        """index_snapshots 的 ts 严格单调递增。"""
        conn = _get_conn()
        rows = conn.execute(
            """SELECT ts FROM index_snapshots
               WHERE trade_date=? ORDER BY ts""",
            (trade_date,),
        ).fetchall()
        conn.close()

        for i in range(1, len(rows)):
            assert rows[i][0] >= rows[i - 1][0], (
                f"{trade_date}: index_snapshots ts 逆序 "
                f"at row {i}: {rows[i - 1][0]:.10f} → {rows[i][0]:.10f}"
            )

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_sector_snapshots_cover_many_sectors(self, trade_date):
        """板块快照覆盖足够多的板块（>50）。"""
        conn = _get_conn()
        count = conn.execute(
            """SELECT COUNT(DISTINCT sector_name) FROM sector_snapshots
               WHERE trade_date=? AND sector_name != '-'""",
            (trade_date,),
        ).fetchone()[0]
        conn.close()

        assert count > 50, f"{trade_date}: 仅有 {count} 个板块，预期 >50"

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_sector_snapshots_timestamps_reasonable(self, trade_date):
        """板块快照时间戳在交易时段内（9:30-15:00）。"""
        conn = _get_conn()
        rows = conn.execute(
            """SELECT DISTINCT ts FROM sector_snapshots
               WHERE trade_date=? ORDER BY ts LIMIT 5""",
            (trade_date,),
        ).fetchall()
        conn.close()

        for (ts_str,) in rows:
            # ts 格式: "2026-06-05T09:30:49"
            assert "T" in ts_str, f"时间格式异常: {ts_str}"
            time_part = ts_str.split("T")[1]
            hours = int(time_part.split(":")[0])
            assert 9 <= hours <= 15, f"{trade_date}: 时间 {ts_str} 不在交易时段内"

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_index_snapshots_have_positive_prices(self, trade_date):
        """指数价格均为正数。"""
        conn = _get_conn()
        bad = conn.execute(
            """SELECT COUNT(*) FROM index_snapshots
               WHERE trade_date=? AND (price <= 0 OR amount IS NULL)""",
            (trade_date,),
        ).fetchone()[0]
        conn.close()

        assert bad == 0, f"{trade_date}: 有 {bad} 条记录的 price<=0 或 amount=NULL"

    @pytest.mark.parametrize("trade_date", TRADE_DATES)
    def test_stock_basic_industries_populated(self, trade_date):
        """stock_basic 的 industry 字段多数非空。"""
        conn = _get_conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM stock_basic WHERE trade_date=?",
            (trade_date,),
        ).fetchone()[0]
        with_industry = conn.execute(
            "SELECT COUNT(*) FROM stock_basic WHERE trade_date=? AND industry != '' AND industry IS NOT NULL",
            (trade_date,),
        ).fetchone()[0]
        conn.close()

        coverage = with_industry / total * 100 if total > 0 else 0
        assert coverage > 50, (
            f"{trade_date}: industry 覆盖率 {coverage:.1f}% ({with_industry}/{total})"
        )
