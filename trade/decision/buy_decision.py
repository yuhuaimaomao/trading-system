"""买入决策管线 — 信号/复盘候选 → 多维评分 → 模拟盘执行。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import time

from system.config import settings
from system.utils.logger import get_trade_logger

logger = get_trade_logger("decision")


class BuyDecisionMixin:
    """买入决策：候选管线、多维评分、盘口数据、仓位计算。"""

    def _calculate_position_size(
        self,
        code: str,
        price: float,
        buy_min: float,
        buy_max: float,
        pattern: str,
        sector_trend: str,
    ) -> tuple[int, str]:
        """委托至 trade.decision.sizing.calculate_position_size。"""
        from trade.decision.sizing import calculate_position_size

        return calculate_position_size(
            code=code,
            price=price,
            buy_min=buy_min,
            buy_max=buy_max,
            pattern=pattern,
            sector_trend=sector_trend,
            market_breadth=getattr(self, "_market_breadth", {}),
            industry_cache=getattr(self, "_industry_cache", {}),
            morning_sector_bias=self._morning_sector_bias,
            total_value=self.paper_account.total_value,
        )

    def _analyze_buy_context(self, code: str, price: float, buy_min: float, buy_max: float) -> str:
        """分析买入时的盘面上下文，返回人性化的决策提示。

        结合：趋势方向、买入区位置、布林带位置、是否回踩支撑
        """
        parts = []

        # 1. 买入区位置
        zone_range = buy_max - buy_min if buy_max > buy_min else 1
        zone_pos = (price - buy_min) / zone_range
        if zone_pos <= 0.2:
            parts.append("📍 价格在买入区下沿，安全边际较高")
        elif zone_pos <= 0.5:
            parts.append("📍 价格在买入区中段")
        elif zone_pos <= 0.8:
            parts.append("📍 价格接近买入区上沿，注意追高风险")
        else:
            parts.append("⚠️ 价格在买入区顶部，追高需谨慎")

        # 2. 布林带位置（从数据库获取最近指标）
        try:
            row = self.repo.get_daily_indicators(code)
            if row:
                bb_lower = row["bb_lower"]
                pct_b = row["bb_pct_b"]
                ma5 = row["ma5"]
                ma10 = row["ma10"]
                ma20 = row["ma20"]
                if pct_b is not None:
                    if pct_b <= 10:
                        parts.append("📊 布林带(昨): 触及下轨（超卖区域，可能反弹）")
                    elif pct_b <= 30:
                        parts.append("📊 布林带(昨): 偏下部运行，接近支撑")
                    elif pct_b <= 70:
                        parts.append("📊 布林带(昨): 中轨附近运行")
                    elif pct_b <= 90:
                        parts.append("📊 布林带(昨): 偏上部运行，接近压力")
                    else:
                        parts.append("📊 布林带(昨): 触及上轨，注意回调风险")

                # 3. 均线位置
                ma_parts = []
                for label, ma in [("MA5", ma5), ("MA10", ma10), ("MA20", ma20)]:
                    if ma and ma > 0:
                        pct = (price - ma) / ma * 100
                        side = "上" if pct > 0 else "下"
                        ma_parts.append(f"{label}={ma:.2f}({side}{abs(pct):.1f}%)")
                if ma_parts:
                    parts.append(f"📈 均线(昨): {', '.join(ma_parts)}")

                # 4. 判断回踩支撑
                if bb_lower and price > bb_lower * 0.98 and price < bb_lower * 1.03:
                    parts.append("🟢 回踩布林下轨支撑，反弹概率较高")
                elif ma20 and price > ma20 * 0.98 and price < ma20 * 1.03:
                    parts.append("🟡 回踩MA20支撑位，关注是否站稳")
        except Exception:
            pass

        # 5. 板块叠加
        trend = self._get_sector_trend(code)
        if "持续走弱" in trend:
            parts.append("🚫 板块持续走弱，不建议逆势买入")
        elif "走弱" in trend:
            parts.append("⚠️ 板块走弱中，逆势买入需注意风险")
        elif "持续走强" in trend:
            parts.append("✅ 板块持续走强，顺势买入")
        elif "走强" in trend:
            parts.append("✅ 板块走强，顺势买入")

        # 6. 日内分钟级指标
        intra = self._get_intraday_indicators(code)
        if intra["available"]:
            r6, r12 = intra["rsi6"], intra["rsi12"]
            macd_dir = (
                "多头"
                if intra["macd_direction"] == "bullish"
                else "空头"
                if intra["macd_direction"] == "bearish"
                else "震荡"
            )
            j = intra["kdj_j"]
            vs_ma5 = intra["price_vs_ma5"]

            intra_parts = [f"RSI6={r6:.0f} RSI12={r12:.0f}"]
            intra_parts.append(f"MACD={macd_dir}(bar={intra['macd_bar']:.2f})")
            intra_parts.append(f"KDJ K={intra['kdj_k']:.1f} D={intra['kdj_d']:.1f} J={j:.1f}")
            if vs_ma5 != 0:
                side = "上" if vs_ma5 > 0 else "下"
                intra_parts.append(f"价在MA5{side}{abs(vs_ma5):.1f}%")
            parts.append(f"日内: {' | '.join(intra_parts)}")

        # 7. 盘口 + 大单
        ob_ratio, ob_reason, ob_delta, ob_delta_desc = self._get_order_book_imbalance(code, price)
        if ob_reason:
            parts.append(f"📊 盘口: {ob_reason}(买盘{ob_ratio:.0%})")
            if ob_delta_desc:
                parts.append(f"   Δ: {ob_delta_desc}({ob_delta:+.0%})")
        big_ratio, big_reason = self._get_big_order_direction(code)
        if big_reason:
            parts.append(f"💰 大单: {big_reason}")

        # 8. 涨跌停空间
        inst = self._get_instrument_info(code)
        up_stop = inst.get("up_stop", 0)
        if up_stop > 0 and price > 0:
            room = (up_stop - price) / price * 100
            parts.append(f"📏 涨停{up_stop:.2f} 空间{room:.1f}%")

        return "\n".join(parts)

    def _get_intraday_indicators(self, code: str) -> dict:
        """获取个股日内分钟级技术指标。缓存同一扫描轮内复用。"""
        if self._intraday_cache_scan == self._scan_count and code in self._intraday_cache:
            return self._intraday_cache[code]

        result = {
            "rsi6": 50,
            "rsi12": 50,
            "macd_dif": 0,
            "macd_dea": 0,
            "macd_bar": 0,
            "macd_direction": "",
            "kdj_k": 50,
            "kdj_d": 50,
            "kdj_j": 50,
            "price_vs_ma5": 0,
            "available": False,
        }

        if not self.qmt:
            return result

        try:
            from stock.indicators import (
                calc_kdj,
                calc_macd,
                calc_rsi,
            )

            raw = self.qmt.get_minute_kline(code, count=240)
            if not raw or len(raw) < 26:
                return result

            closes = [float(k.get("close", 0)) for k in raw if k.get("close")]
            highs = [float(k.get("high", 0)) for k in raw if k.get("high")]
            lows = [float(k.get("low", 0)) for k in raw if k.get("low")]

            if len(closes) < 26:
                return result

            result["available"] = True
            result["rsi6"] = calc_rsi(closes, 6)
            result["rsi12"] = calc_rsi(closes, 12)
            macd = calc_macd(closes)
            result["macd_dif"] = macd["dif"]
            result["macd_dea"] = macd["dea"]
            result["macd_bar"] = macd["bar"]
            kdj = calc_kdj(highs, lows, closes)
            result["kdj_k"] = kdj["k"]
            result["kdj_d"] = kdj["d"]
            result["kdj_j"] = kdj["j"]

            # MACD 方向
            if macd["dif"] > macd["dea"]:
                result["macd_direction"] = "bullish"
            elif macd["dif"] < macd["dea"]:
                result["macd_direction"] = "bearish"

            # 价格相对 MA5（用收盘价序列的 EMA5 近似）
            ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else closes[-1]
            if ma5 > 0:
                result["price_vs_ma5"] = (closes[-1] - ma5) / ma5 * 100

        except Exception:
            pass

        self._intraday_cache[code] = result
        self._intraday_cache_scan = self._scan_count
        return result

    def _get_context_factors(self, code: str, price: float) -> dict:
        """获取昨日趋势背景 + 今日实时因子。全天缓存。

        注意：stock_basic/stock_indicators 是昨日收盘数据，
        仅用于趋势背景判断，不反映今日实时状态。
        """
        if code in self._daily_factor_cache:
            return self._daily_factor_cache[code]

        factors = {"available": False}
        try:
            # 主力资金（单日 + 连续趋势）
            mf = self.repo.get_money_flow(code)
            if mf:
                factors["yesterday_mf_net"] = mf["main_force_net"]
                factors["yesterday_mf_ratio"] = mf["main_force_ratio"]
                factors["yesterday_sl_net"] = mf["super_large_net"]
                factors["yesterday_l_net"] = mf["large_net"]
                factors["ma5_angle"] = mf["ma5_angle"]
                factors["pe_dynamic"] = mf["pe_dynamic"]
                factors["circ_market_cap"] = mf["circ_market_cap"]
                factors["available"] = True
            # 资金流连续趋势（最近5天）
            try:
                mft = self.repo.get_money_flow_trend(code, days=5)
                if mft:
                    factors["mf_trend_score"] = mft["trend_score"]
                    factors["mf_trend_strength"] = mft["trend_strength"]
                    factors["mf_consecutive_buy"] = mft["consecutive_buy"]
                    factors["mf_total_net"] = mft.get("total_net", 0)
            except Exception:
                factors["mf_trend_score"] = 0
            # 波动率异动
            try:
                vb = self.repo.get_volatility_breakout(code, lookback=20)
                if vb:
                    factors["vol_breakout"] = vb["is_breakout"]
                    factors["vol_ratio"] = vb["vol_ratio"]
                    factors["vol_signal"] = vb["signal"]
            except Exception:
                factors["vol_breakout"] = False
            # 日线技术指标
            ind = self.repo.get_daily_indicators(code)
            if ind:
                factors["daily_macd_dif"] = ind.get("macd_dif", 0) or 0
                factors["daily_macd_dea"] = ind.get("macd_dea", 0) or 0
                factors["daily_macd_bar"] = ind.get("macd_bar", 0) or 0
                factors["daily_kdj_k"] = ind.get("kdj_k", 50) or 50
                factors["daily_kdj_d"] = ind.get("kdj_d", 50) or 50
                factors["daily_kdj_j"] = ind.get("kdj_j", 50) or 50
                factors["daily_rsi6"] = ind.get("rsi6", 50) or 50
                factors["daily_rsi24"] = ind.get("rsi24", 50) or 50
                factors["bbi_daily"] = ind.get("bbi_daily", 0) or 0
                factors["bbi_weekly"] = ind.get("bbi_weekly", 0) or 0
                factors["bb_width"] = ind.get("bb_width", 0) or 0
                factors["ma120"] = ind.get("ma120", 0) or 0

            # ━━━ 以下为今日实时数据 ━━━

            # 日内位置（今日 high/low/open 来自 QMT 实时）
            if self.qmt:
                try:
                    detail = self.qmt.get_quote_detail(code)
                    if detail:
                        dh = float(detail.get("high", 0))
                        dl = float(detail.get("low", 0))
                        do = float(detail.get("open", 0))
                        if dh > dl > 0:
                            day_range = dh - dl
                            factors["day_position"] = (price - dl) / day_range
                            factors["day_high"] = dh
                            factors["day_low"] = dl
                            factors["day_open"] = do
                            factors["day_change_pct"] = (price - do) / do * 100 if do > 0 else 0
                except Exception:
                    pass

            # 5分钟K线 MACD（今日实时）
            if self.qmt:
                try:
                    from stock.indicators import calc_macd, calc_rsi

                    raw_5m = self.qmt.get_kline(code, period="5m", count=50)
                    if raw_5m:
                        if isinstance(raw_5m, list) and len(raw_5m) >= 26:
                            c5 = [float(b.get("close", 0)) for b in raw_5m if b.get("close")]
                            if len(c5) >= 26:
                                m5 = calc_macd(c5)
                                factors["m5_macd_dif"] = m5["dif"]
                                factors["m5_macd_dea"] = m5["dea"]
                                factors["m5_macd_bar"] = m5["bar"]
                                factors["m5_rsi6"] = calc_rsi(c5, 6)
                                m5_ma20 = sum(c5[-20:]) / 20 if len(c5) >= 20 else c5[-1]
                                factors["m5_vs_ma20"] = (c5[-1] - m5_ma20) / m5_ma20 * 100 if m5_ma20 > 0 else 0
                except Exception:
                    pass

        except Exception:
            pass

        self._daily_factor_cache[code] = factors
        return factors

    def _get_order_book_imbalance(self, code: str, price: float) -> tuple[float, str, float, str]:
        """五档盘口买卖力量对比 + 变化率。
        返回 (bid_ratio, reason, ratio_delta, delta_desc)。
        ratio_delta > 0 表示买盘在增强。
        """
        if not self.qmt:
            return 0.5, "", 0, ""
        try:
            detail = self.qmt.get_quote_detail(code)
            if not detail:
                return 0.5, "", 0, ""

            ask_vols = detail.get("askVol", [])
            bid_vols = detail.get("bidVol", [])
            if not ask_vols or not bid_vols:
                return 0.5, "", 0, ""

            total_bid = sum(float(v) for v in bid_vols[:5] if v)
            total_ask = sum(float(v) for v in ask_vols[:5] if v)
            total = total_bid + total_ask
            if total <= 0:
                return 0.5, "", 0, ""

            ratio = total_bid / total

            # 变化率：对比上一次记录的盘口
            if not hasattr(self, "_ob_history"):
                self._ob_history: dict[str, tuple[float, float]] = {}
            prev = self._ob_history.get(code, (ratio, 0))
            delta = ratio - prev[0]
            self._ob_history[code] = (ratio, self._scan_count)

            if delta > 0.10:
                delta_desc = "买盘快速增强"
            elif delta > 0.05:
                delta_desc = "买盘小幅增强"
            elif delta < -0.10:
                delta_desc = "卖盘快速增强"
            elif delta < -0.05:
                delta_desc = "卖盘小幅增强"
            else:
                delta_desc = ""

            if ratio >= 0.7:
                return ratio, "买盘强劲", delta, delta_desc
            elif ratio >= 0.55:
                return ratio, "买盘略强", delta, delta_desc
            elif ratio <= 0.3:
                return ratio, "卖盘沉重", delta, delta_desc
            elif ratio <= 0.45:
                return ratio, "卖盘略强", delta, delta_desc
            return ratio, "买卖均衡", delta, delta_desc
        except Exception:
            return 0.5, "", 0, ""

    def _get_instrument_info(self, code: str) -> dict:
        """获取合约基本信息，缓存整日（盘中不变）。"""
        if code in self._instrument_cache:
            return self._instrument_cache[code]
        info = {}
        if self.qmt:
            try:
                data = self.qmt.get_instrument(code)
                if data:
                    info = {
                        "float_share": float(data.get("floatShare", 0)),
                        "total_share": float(data.get("totalShare", 0)),
                        "up_stop": float(data.get("upStopPrice", 0)),
                        "down_stop": float(data.get("downStopPrice", 0)),
                        "pre_close": float(data.get("preClose", 0)),
                    }
            except Exception:
                pass
        self._instrument_cache[code] = info
        return info

    def _get_big_order_direction(self, code: str) -> tuple[float, str]:
        """逐笔成交大单流向分析。返回 (buy_ratio, reason)。

        统计近200笔成交中大单(>=5万元)的买卖方向，>0.55 主力买入。
        """
        if not self.qmt:
            return 0.5, ""
        try:
            ticks = self.qmt.get_ticks(code)
            if not ticks or len(ticks) < 20:
                return 0.5, ""

            big_buy_amount = 0.0
            big_sell_amount = 0.0

            # amount 是累计值，diff 得到每笔成交额
            prev_amount = None
            for t in ticks:
                amt = float(t.get("amount", 0))
                direction = t.get("direction", "")
                if prev_amount is not None:
                    trade_amt = amt - prev_amount
                    if trade_amt > 50000:  # 大单阈值 5 万
                        if direction == "buy":
                            big_buy_amount += trade_amt
                        elif direction == "sell":
                            big_sell_amount += trade_amt
                prev_amount = amt

            total_big = big_buy_amount + big_sell_amount
            if total_big <= 0:
                return 0.5, ""

            ratio = big_buy_amount / total_big

            if ratio >= 0.65:
                return ratio, f"大单买入主导({ratio:.0%})"
            elif ratio >= 0.55:
                return ratio, f"大单偏买({ratio:.0%})"
            elif ratio <= 0.35:
                return ratio, f"大单卖出主导({1 - ratio:.0%})"
            elif ratio <= 0.45:
                return ratio, f"大单偏卖({1 - ratio:.0%})"
            return ratio, "大单均衡"
        except Exception:
            return 0.5, ""

    def _calc_unified_sl(self, code: str, price: float, trend: str = "", strategy: str = "standard") -> float:
        """委托至统一止损计算（无 DB 查询版本，盘中性能优先）。"""
        from trade.decision.sizing import calc_unified_stop_loss

        return calc_unified_stop_loss(code, price, trend=trend, daily_indicators=None, strategy_type=strategy)

    def _calc_unified_tp(self, code: str, price: float, trend: str = "", strategy: str = "standard") -> float:
        """委托至统一止盈计算（无 DB 查询版本，盘中性能优先）。"""
        from trade.decision.sizing import calc_unified_take_profit

        return calc_unified_take_profit(code, price, trend=trend, daily_indicators=None, strategy_type=strategy)

    def _calc_fallback_sl_tp(self, code: str, price: float) -> tuple[float, float]:
        """AI 未给止损/止盈时，从技术指标自动补算。
        止损 = 最近支撑位下方 1%（不低于现价的 93%）
        止盈 = 最近阻力位（不高于现价的 112%）
        """
        sl = 0.0
        tp = 0.0
        try:
            sr = self.repo.get_support_resistance(code, price)
            supports = sr.get("supports", [])
            resistances = sr.get("resistances", [])
            if supports:
                sl = round(supports[0][0] * 0.99, 2)
            if resistances:
                tp = round(resistances[0][0], 2)
        except Exception:
            pass
        # 兜底：支撑位计算失败时用固定百分比
        if sl <= 0:
            sl = round(price * 0.93, 2)
        else:
            sl = max(sl, round(price * 0.93, 2))  # 不低于 7% 止损
        if tp <= 0:
            tp = round(price * 1.10, 2)
        else:
            tp = min(tp, round(price * 1.12, 2))  # 不高于 12% 止盈
        return sl, tp

    def _evaluate_buy_decision(
        self, code: str, price: float, buy_min: float, buy_max: float
    ) -> tuple[bool, str, float]:
        """委托至 trade.decision.buy.evaluate_buy。"""
        from trade.decision.buy import BuyEvalInput, evaluate_buy

        trend = self._get_sector_trend(code)
        industry = getattr(self, "_industry_cache", {}).get(code, "")
        ai_bias = ""
        ai_size_mult = 1.0
        if industry and industry in self._morning_sector_bias:
            b = self._morning_sector_bias[industry]
            ai_bias = b.get("bias", "")
            ai_size_mult = b.get("size_mult", 1.0)

        sector_strong = trend and ("持续走强" in trend or "走强" in trend)
        sector_chg_val = self._get_sector_change(code)
        sector_very_strong = sector_strong and (sector_chg_val or 0) > 1.5
        if ai_bias == "focus":
            sector_very_strong = True

        concept_score, concept_reason = self._get_concept_trend_score(code)
        intra = self._get_intraday_indicators(code)
        ob_ratio, ob_reason, ob_delta, ob_delta_desc = self._get_order_book_imbalance(code, price)
        big_ratio, big_reason = self._get_big_order_direction(code)
        inst = self._get_instrument_info(code)
        df = self._get_context_factors(code, price)
        price_action, price_action_desc = self._get_recent_price_action(code)

        # 日线布林/均线（从 DB）
        daily_bb_upper = daily_bb_mid = daily_bb_lower = 0.0
        daily_bb_pct_b = None
        daily_ma5 = daily_ma10 = daily_ma20 = 0.0
        daily_rsi6 = daily_rsi12 = None
        daily_kdj_k = daily_kdj_d = daily_kdj_j = None
        try:
            ind = self.repo.get_daily_indicators(code)
            if ind:
                daily_bb_upper = ind.get("bb_upper", 0) or 0
                daily_bb_mid = ind.get("bb_mid", 0) or 0
                daily_bb_lower = ind.get("bb_lower", 0) or 0
                daily_bb_pct_b = ind.get("bb_pct_b")
                daily_ma5 = ind.get("ma5", 0) or 0
                daily_ma10 = ind.get("ma10", 0) or 0
                daily_ma20 = ind.get("ma20", 0) or 0
                daily_rsi6 = ind.get("rsi6")
                daily_rsi12 = ind.get("rsi12")
                daily_kdj_k = ind.get("kdj_k")
                daily_kdj_d = ind.get("kdj_d")
                daily_kdj_j = ind.get("kdj_j")
        except Exception:
            pass

        ctx = BuyEvalInput(
            code=code,
            price=price,
            buy_min=buy_min,
            buy_max=buy_max,
            sector_trend=trend,
            sector_chg=sector_chg_val,
            sector_decline=self._get_sector_decline(code),
            sector_recovery_risk=self._get_sector_recovery_risk(code),
            concept_score=concept_score,
            concept_reason=concept_reason,
            daily_bb_upper=daily_bb_upper or 0,
            daily_bb_mid=daily_bb_mid or 0,
            daily_bb_lower=daily_bb_lower or 0,
            daily_bb_pct_b=daily_bb_pct_b,
            daily_ma5=daily_ma5 or 0,
            daily_ma10=daily_ma10 or 0,
            daily_ma20=daily_ma20 or 0,
            daily_rsi6=daily_rsi6,
            daily_rsi12=daily_rsi12,
            daily_kdj_k=daily_kdj_k,
            daily_kdj_d=daily_kdj_d,
            daily_kdj_j=daily_kdj_j,
            intra_available=intra.get("available", False),
            intra_rsi6=intra.get("rsi6", 50),
            intra_rsi12=intra.get("rsi12", 50),
            intra_macd_direction=intra.get("macd_direction", ""),
            intra_macd_bar=intra.get("macd_bar", 0),
            intra_kdj_k=intra.get("kdj_k", 50),
            intra_kdj_d=intra.get("kdj_d", 50),
            intra_kdj_j=intra.get("kdj_j", 50),
            intra_price_vs_ma5=intra.get("price_vs_ma5", 0),
            ob_ratio=ob_ratio,
            ob_reason=ob_reason,
            ob_delta=ob_delta,
            big_ratio=big_ratio,
            big_reason=big_reason,
            up_stop=inst.get("up_stop", 0),
            down_stop=inst.get("down_stop", 0),
            yesterday_mf_ratio=df.get("yesterday_mf_ratio", 0),
            mf_trend_score=df.get("mf_trend_score", 0),
            mf_trend_strength=df.get("mf_trend_strength", ""),
            ma5_angle=df.get("ma5_angle", 0),
            day_position=df.get("day_position"),
            daily_macd_dif=df.get("daily_macd_dif", 0),
            daily_macd_dea=df.get("daily_macd_dea", 0),
            daily_macd_bar=df.get("daily_macd_bar", 0),
            daily_kdj_j_daily=df.get("daily_kdj_j", 50),
            bbi_daily=df.get("bbi_daily", 0),
            m5_macd_dif=df.get("m5_macd_dif"),
            m5_macd_dea=df.get("m5_macd_dea"),
            m5_macd_bar=df.get("m5_macd_bar"),
            bb_width=df.get("bb_width", 0),
            price_action=price_action,
            price_action_desc=price_action_desc,
            sector_strong=sector_strong,
            sector_very_strong=sector_very_strong,
            ai_bias=ai_bias,
            ai_size_mult=ai_size_mult,
            vol_breakout=df.get("vol_breakout", False),
            vol_ratio=df.get("vol_ratio", 1.0),
            vol_signal=df.get("vol_signal", ""),
            opening_bias=self._get_opening_strength().get("bias", "neutral"),
            opening_score=self._get_opening_strength().get("score", 0),
        )
        return evaluate_buy(ctx)

    def _get_sector_change(self, code: str) -> float | None:
        from trade.detect.sector_trend import get_sector_change

        return get_sector_change(
            code,
            getattr(self, "_industry_cache", {}),
            getattr(self, "_sector_stats", {}),
        )

    def _get_sector_decline(self, code: str) -> float | None:
        from trade.detect.sector_trend import get_sector_decline

        return get_sector_decline(
            code,
            getattr(self, "_industry_cache", {}),
            getattr(self, "_sector_stats", {}),
        )

    def _get_sector_recovery_risk(self, code: str) -> float | None:
        from trade.detect.sector_trend import get_sector_recovery_risk

        return get_sector_recovery_risk(
            code,
            getattr(self, "_industry_cache", {}),
            getattr(self, "_sector_stats", {}),
        )

    def _get_recent_price_action(self, code: str) -> tuple[str, str]:
        """分析最近10分钟价格走势。返回 (action, description)。

        action: "declining"  — 持续创新低，下跌趋势未止
                "stabilizing" — 窄幅横盘，跌势放缓
                "reversing"   — 出现低点抬高/反弹
                "no_data"     — 数据不足
        """
        prices = getattr(self, "_recent_prices", {}).get(code, [])
        if len(prices) < 6:  # 至少 6 个数据点（约2分钟）
            return "no_data", "价格数据不足"

        now = time.time()
        # 分两段：前5分钟 vs 后5分钟
        mid = now - 300
        first_half = [(t, p) for t, p in prices if t <= mid]
        second_half = [(t, p) for t, p in prices if t > mid]

        if len(first_half) < 3 or len(second_half) < 3:
            # 数据不够分段，看整体趋势
            first_price = prices[0][1]
            last_price = prices[-1][1]
            pct = (last_price - first_price) / first_price * 100
            if pct < -2:
                return "declining", f"10分钟跌{pct:.1f}%"
            elif pct < -0.5:
                return "stabilizing", f"10分钟缓跌{pct:.1f}%"
            else:
                return "stabilizing", f"10分钟横盘{pct:+.1f}%"

        first_prices = [p for _, p in first_half]
        second_prices = [p for _, p in second_half]

        first_low = min(first_prices)
        second_low = min(second_prices)
        first_high = max(first_prices)
        second_high = max(second_prices)
        first_avg = sum(first_prices) / len(first_prices)
        second_avg = sum(second_prices) / len(second_prices)

        # 后5分钟最低价 < 前5分钟最低价 * 0.99 → 还在明显创新低（放宽：之前 0.995）
        if second_low < first_low * 0.99:
            drop_pct = (second_low - first_high) / first_high * 100
            return "declining", f"持续创新低({drop_pct:.1f}%)"

        # 后5分钟振幅 < 1.5% → 横盘止跌（放宽：之前 1.0%）
        second_range = (second_high - second_low) / second_low * 100 if second_low > 0 else 0
        if second_range < 1.5:
            return "stabilizing", f"横盘止跌(振幅{second_range:.1f}%)"

        # 后5分钟均价 > 前5分钟均价 * 0.998 → 算反弹（放宽：之前 1.005）
        if second_avg > first_avg * 0.998:
            return (
                "reversing",
                f"低点企稳+{((second_avg - first_avg) / first_avg * 100):.1f}%",
            )

        return "stabilizing", f"波动收敛(振幅{second_range:.1f}%)"

    def _evaluate_below_zone(
        self, code: str, price: float, buy_min: float, buy_max: float
    ) -> tuple[str, str, float | None]:
        """委托至 trade.decision.buy.evaluate_below_zone。"""
        from trade.decision.buy import BuyEvalInput, evaluate_below_zone

        # 支撑位检测（DB查询）
        near_support = False
        near_ma60 = False
        try:
            ind = self.repo.get_daily_indicators(code)
            if ind:
                bb_lower = ind.get("bb_lower")
                ma20 = ind.get("ma20")
                ma60 = ind.get("ma60")
                if (
                    bb_lower
                    and abs(price - bb_lower) / bb_lower < 0.02
                    or ma20
                    and abs(price - ma20) / ma20 < 0.02
                    or ma60
                    and abs(price - ma60) / ma60 < 0.03
                ):
                    near_support = True
                if ma60 and price < ma60:
                    near_ma60 = True
        except Exception:
            pass

        # 量能验证（QMT ticks）
        vol_shrinking = False
        vol_surging = False
        if self.qmt:
            try:
                ticks = self.qmt.get_ticks(code)
                if ticks and len(ticks) >= 40:
                    half = len(ticks) // 2
                    recent = ticks[-half:]
                    earlier = ticks[:half]
                    recent_vol = sum(
                        float(recent[i].get("amount", 0)) - float(recent[i - 1].get("amount", 0))
                        for i in range(1, len(recent))
                        if float(recent[i].get("amount", 0)) > float(recent[i - 1].get("amount", 0))
                    )
                    earlier_vol = sum(
                        float(earlier[i].get("amount", 0)) - float(earlier[i - 1].get("amount", 0))
                        for i in range(1, len(earlier))
                        if float(earlier[i].get("amount", 0)) > float(earlier[i - 1].get("amount", 0))
                    )
                    if earlier_vol > 0 and recent_vol > 0:
                        vol_ratio = recent_vol / earlier_vol
                        if vol_ratio < 0.5:
                            vol_shrinking = True
                        elif vol_ratio > 2:
                            vol_surging = True
            except Exception:
                pass

        # 构建输入（复用 evaluate_buy 的 BuyEvalInput）
        trend = self._get_sector_trend(code)
        intra = self._get_intraday_indicators(code)
        ob_ratio, ob_reason, ob_delta, _ob_delta_desc = self._get_order_book_imbalance(code, price)
        big_ratio, big_reason = self._get_big_order_direction(code)
        df = self._get_context_factors(code, price)
        price_action, price_action_desc = self._get_recent_price_action(code)

        ctx = BuyEvalInput(
            code=code,
            price=price,
            buy_min=buy_min,
            buy_max=buy_max,
            sector_trend=trend,
            sector_chg=self._get_sector_change(code),
            sector_decline=self._get_sector_decline(code),
            sector_recovery_risk=self._get_sector_recovery_risk(code),
            concept_score=self._get_concept_trend_score(code)[0],
            concept_reason=self._get_concept_trend_score(code)[1],
            intra_available=intra.get("available", False),
            intra_rsi6=intra.get("rsi6", 50),
            intra_rsi12=intra.get("rsi12", 50),
            intra_macd_direction=intra.get("macd_direction", ""),
            intra_macd_bar=intra.get("macd_bar", 0),
            intra_kdj_k=intra.get("kdj_k", 50),
            intra_kdj_d=intra.get("kdj_d", 50),
            intra_kdj_j=intra.get("kdj_j", 50),
            intra_price_vs_ma5=intra.get("price_vs_ma5", 0),
            ob_ratio=ob_ratio,
            ob_reason=ob_reason,
            ob_delta=ob_delta,
            big_ratio=big_ratio,
            big_reason=big_reason,
            yesterday_mf_ratio=df.get("yesterday_mf_ratio", 0),
            mf_trend_score=df.get("mf_trend_score", 0),
            mf_trend_strength=df.get("mf_trend_strength", ""),
            ma5_angle=df.get("ma5_angle", 0),
            day_position=df.get("day_position"),
            daily_macd_dif=df.get("daily_macd_dif", 0),
            daily_macd_dea=df.get("daily_macd_dea", 0),
            daily_macd_bar=df.get("daily_macd_bar", 0),
            daily_kdj_j_daily=df.get("daily_kdj_j", 50),
            bbi_daily=df.get("bbi_daily", 0),
            m5_macd_dif=df.get("m5_macd_dif"),
            m5_macd_dea=df.get("m5_macd_dea"),
            m5_macd_bar=df.get("m5_macd_bar"),
            price_action=price_action,
            price_action_desc=price_action_desc,
        )
        return evaluate_below_zone(
            ctx,
            near_support=near_support,
            vol_shrinking=vol_shrinking,
            vol_surging=vol_surging,
            near_ma60=near_ma60,
        )

    def _calc_dynamic_buy_zone(
        self, code: str, price: float, buy_min: float, buy_max: float, trend: str = ""
    ) -> tuple[float, float, str]:
        """动态买入区修正：三层联动（大盘→板块→个股）评估买入区是否需要调整。

        市场偏空 + 板块弱 → 买入区整体下移。返回 (new_min, new_max, reason)。
        如果无需调整，返回 (buy_min, buy_max, "")。
        """
        # 获取三层联动因子
        adj = self._get_market_adjustment(code, trend)
        shift = adj.get("buy_zone_shift", 0)
        if shift <= 0 or not adj.get("reason"):
            return buy_min, buy_max, ""

        # 计算下移后的买入区
        zone_width = buy_max - buy_min
        new_min = round(buy_min * (1 - shift), 2)
        new_max = round(buy_max * (1 - shift), 2)

        # 区间宽度不能太窄
        if new_max - new_min < zone_width * 0.5:
            new_max = round(new_min + zone_width * 0.5, 2)

        # 修正一律静默：用修正后的区间正常盯盘，不推送告警
        return new_min, new_max, ""

    # ======================== 第一层：信号触发 ========================

    def _get_market_risk_advice(self) -> str:
        """大盘风险时的具体建议，含市场状态和操作指引。"""
        pattern = self._classify_market_pattern()
        idx_info = ""
        if self._index_prices:
            cur = self._index_prices[-1]
            ma5, ma10, ma20 = self._get_index_baseline()
            if ma20 > 0:
                vs_ma20 = (cur - ma20) / ma20 * 100
                idx_info = f"上证{cur:.2f}(vsMA20 {vs_ma20:+.1f}%) "
        advice_map = {
            "panic": "恐慌加速下跌，不建议任何买入，已有持仓关注止损位",
            "one_sided": "单边下跌重心下移，等待缩量止跌再考虑，先观察不操作",
            "dead_cat": "弱势反弹不可靠，等站上日内EMA12+成交量放大再考虑",
            "normal": "大盘正常但风控暂停，检查是否触发熔断/仓位上限",
        }
        advice = advice_map.get(pattern, "大盘风险信号，暂停自动买入，关注市场变化")
        return f"{idx_info}{advice}"

    def _check_instant_breadth(self) -> bool:
        """瞬时宽度检查：下跌/上涨 > BREADTH_DOWN_UP_RATIO 且指数跌 → True。"""
        breadth = self._market_breadth
        if not breadth or breadth.get("up", 0) <= 0:
            return False
        down_up = breadth.get("down", 0) / breadth["up"]
        idx_quote = getattr(self, "_last_index_quote", None) or {}
        idx_change = idx_quote.get("change_pct", 0)
        return down_up > getattr(settings, "BREADTH_DOWN_UP_RATIO", 3.0) and idx_change < 0

    def _check_buy_candidates(self, state, candidates: list[dict], regime):
        """统一买入候选处理：信号 + 复盘推荐共用管线。

        每个 candidate 字段:
            code, name, price, buy_min, buy_max, sl, tp, score, trend,
            source: "signal" | "review" (决定告警前缀 + 状态管理)
            alert_key: 去重 key（signal 用 sid, review 用 code）
            signal_id: int|None（DB 信号 ID，用于 expire/update）

        regime: MarketRegime 对象（非旧版 bool），逐票决策时读取 allow_buy/position_mult/entry_rule
        """
        # ═══════════════════════════════════════════════════════════════
        # 买入候选处理管线:
        #   1. 前置门控: data_ready / regime / paper_full / breadth
        #   2. 逐候选处理:
        #      a. 跳过检查: 已持仓 / 近期卖出 / zone无效
        #      b. 高于买入区 → 追高提醒(AI异步) 或 预测接近告警
        #      c. 低于买入区 → 回调评估(abandon/watching/opportunity)
        #      d. 买入区内 → entry_rule过滤 → 多维评估 → 告警+执行
        # ═══════════════════════════════════════════════════════════════

        # ━━ 1. 前置门控 ━━
        # 数据就绪检查：sector 数据未到达前不交易
        if not getattr(self, "_data_ready", False):
            if self._scan_count % 30 == 0:
                logger.info("板块数据未就绪，暂不处理买入信号")
            return

        # 兼容旧版 bool 调用
        if isinstance(regime, bool):
            market_ok = regime
            pattern = self._classify_market_pattern() if market_ok else "halt"
            position_mult = 1.0 if market_ok else 0.0
            entry_rule = "standard" if market_ok else "none"
            regime_alert_msg = ""
            regime_obj = None
        else:
            market_ok = regime.allow_buy
            pattern = regime.pattern
            position_mult = regime.position_mult
            entry_rule = regime.entry_rule
            regime_alert_msg = regime.alert_msg
            regime_obj = regime

        paper_full = len(self.paper_account.positions) >= settings.MAX_POSITIONS

        # 市场宽度过滤：仅对盘前信号/复盘推荐生效
        # 盘中动态发现（板块热点、回踩扫描）自带实时择时能力，不拦
        first_source = candidates[0]["source"] if candidates else ""
        is_premarket = first_source in ("signal", "review")
        breadth_blocked = False
        if is_premarket:
            try:
                if hasattr(self, "_compute_rolling_breadth"):
                    breadth_roll = self._compute_rolling_breadth(window_minutes=settings.BREADTH_ROLLING_WINDOW_SHORT)
                    if breadth_roll and breadth_roll.get("improving"):
                        up_delta = breadth_roll.get("up_delta", 0)
                        if up_delta < settings.BREADTH_IMPROVEMENT_THRESHOLD:
                            breadth_blocked = self._check_instant_breadth()
                    else:
                        breadth_blocked = self._check_instant_breadth()
                else:
                    breadth_blocked = self._check_instant_breadth()
            except Exception:
                breadth_blocked = self._check_instant_breadth()

            # 恐慌衰减 → 即使瞬时宽度仍差，也放开
            if breadth_blocked:
                try:
                    if hasattr(self, "_check_panic_fading"):
                        _fade = self._check_panic_fading()
                        if _fade.get("faded"):
                            breadth_blocked = False
                            if getattr(self, "_breadth_block_alerted", False):
                                self._breadth_block_alerted = False
                except Exception:
                    pass

        if breadth_blocked:
            last_alert_time = getattr(self, "_breadth_block_alerted_at", 0)
            if time.time() - last_alert_time > 1200:  # 20分钟内不重复推
                self._breadth_block_alerted_at = time.time()
                breadth = self._market_breadth
                down_up = breadth.get("down", 0) / max(breadth.get("up", 0), 1)
                idx_quote = getattr(self, "_last_index_quote", None) or {}
                idx_change = idx_quote.get("change_pct", 0)
                logger.warning(f"市场宽度过滤: 下跌/上涨={down_up:.1f} 指数{idx_change:+.2%}，暂停新开仓")
                self._alert(
                    f"🛑 市场宽度预警\n"
                    f"   下跌/上涨: {down_up:.1f}  指数变化: {idx_change:+.2%}\n"
                    f"   → 多数个股下跌，暂停新开仓位"
                )

        # 早盘 AI 板块倾向：focus 板块候选优先处理
        if self._morning_sector_bias:
            candidates.sort(
                key=lambda c: -self._morning_sector_bias.get(self._industry_cache.get(c["code"], ""), {}).get(
                    "priority", 0
                )
                if self._morning_sector_bias.get(self._industry_cache.get(c["code"], ""), {}).get("bias") == "focus"
                else 0
            )

        for c in candidates:
            if breadth_blocked:
                continue
            source = c["source"]
            if source == "signal":
                alert_state = self._signal_alert_state
                tag = ""
                quiet = False

                def on_abandon(sid=c["signal_id"]):
                    if sid:
                        try:
                            self.repo.update_signal_status(sid, "expired")
                        except Exception:
                            pass
            else:
                alert_state = self._review_alert_state
                tag = "复盘"
                on_abandon = None
                quiet = True  # 复盘/板块发现不到下单决策不推送

            code = c["code"]

            # ━━ 2a. 跳过检查 ━━
            # 已持仓股票不推送买入信号
            if code in self._bought_watch:
                continue

            # 当日卖出后 30 轮（约 6 分钟）内不重新买入
            sold_at = getattr(self, "_recently_sold", {}).get(code, 0)
            if sold_at and self._scan_count - sold_at < 30:
                continue

            name = c["name"]
            price = c["price"]
            buy_min = c["buy_min"]
            buy_max = c["buy_max"]
            sl = c["sl"]
            tp = c["tp"]
            score = c["score"]
            trend = c["trend"]

            if buy_min <= 0 or buy_max <= 0:
                continue

            # —— 动态买入区修正：三层联动，市场偏空时下调买入区（静默生效）——
            adj_buy_min, adj_buy_max, adj_reason = self._calc_dynamic_buy_zone(code, price, buy_min, buy_max, trend)
            buy_min, buy_max = adj_buy_min, adj_buy_max

            in_zone = buy_min <= price <= buy_max
            below_zone = price < buy_min
            above_zone = price > buy_max

            # ━━ 2b. 高于买入区 → 追高提醒(异步AI) / 预测接近告警 ━━
            if above_zone:
                above_pct = (price - buy_max) / buy_max * 100
                logger.info(
                    f"买入评估 [{code} {name}] 高于买入区 {above_pct:+.1f}% "
                    f"价格{price:.2f} 区间{buy_min:.2f}~{buy_max:.2f} 板块{trend}"
                )

                # 距买入区 > 3% → 太远没意义，不推送
                if above_pct > 3:
                    continue

                # 情景引擎：市场预测回调 + 距买入区 < 3% → 提前预告准备入场
                # 但如果预判是死猫跳/恐慌，回调不可靠，不提示买入
                if above_pct <= 3.0:
                    outlook = getattr(self, "_scenario_prev_outlook", None)
                    if (
                        outlook
                        and outlook.primary.direction == "bearish"
                        and outlook.urgency in ("critical", "act")
                        and "死猫跳" not in outlook.primary.label
                        and "恐慌" not in outlook.primary.label
                        and "下跌" not in outlook.primary.label
                    ):
                        approach_key = f"approach:{c['alert_key']}"
                        last_scan = alert_state.get(approach_key, 0)
                        if self._scan_count - last_scan >= 15 and not self._should_throttle(code, price):
                            alert_state[approach_key] = self._scan_count
                            if not quiet:
                                self._alert(
                                    f"🔔 {tag}买入区接近 — {code} {name}\n"
                                    f"   现价: {price:.2f}  距区间: {above_pct:.1f}%  "
                                    f"区间: {buy_min:.2f}~{buy_max:.2f}\n"
                                    f"   止损: {sl:.2f}  止盈: {tp:.2f}\n"
                                    f"   板块:{trend}\n"
                                    f"   🔮 {outlook.primary.label} ({outlook.primary.probability:.0%})"
                                    f"  → 市场回调中，关注买入区"
                                )
                        continue

                # 追高提醒：仅板块走强时才有追的价值，弱板块涨了也不跟
                is_sector_strong = "持续走强" in trend or ("走强" in trend and "弱" not in trend)
                is_sector_weak = any(w in trend for w in ("持续走弱", "弱于大盘", "普跌", "横盘"))
                if is_sector_strong and not is_sector_weak:
                    chase_key = f"chase:{c['alert_key']}"
                    last_chase = alert_state.get(chase_key, 0)
                    if self._scan_count - last_chase >= 15:
                        above_pct = (price - buy_max) / buy_max * 100
                        # 日内技术指标（不含买入建议，只给数据）
                        intra_str = ""
                        intra = self._get_intraday_indicators(code)
                        if intra["available"]:
                            r6, r12 = intra["rsi6"], intra["rsi12"]
                            macd_dir = (
                                "多头"
                                if intra["macd_direction"] == "bullish"
                                else "空头"
                                if intra["macd_direction"] == "bearish"
                                else "震荡"
                            )
                            intra_parts = [f"RSI6={r6:.0f} RSI12={r12:.0f}"]
                            intra_parts.append(f"MACD={macd_dir}({intra['macd_bar']:.2f})")
                            intra_parts.append(
                                f"KDJ K={intra['kdj_k']:.1f} D={intra['kdj_d']:.1f} J={intra['kdj_j']:.1f}"
                            )
                            vs_ma5 = intra["price_vs_ma5"]
                            if vs_ma5 != 0:
                                side = "上" if vs_ma5 > 0 else "下"
                                intra_parts.append(f"价在MA5{side}{abs(vs_ma5):.1f}%")
                            intra_str = f"\n   日内: {' | '.join(intra_parts)}"

                        # 异步 AI 评估：提交后立即返回，结果在后续扫描中推送
                        self._ai_chase_opinion(
                            code,
                            name,
                            price,
                            buy_min,
                            buy_max,
                            sl,
                            tp,
                            trend,
                            above_pct,
                            intra_str=intra_str,
                            alert_key=c["alert_key"],
                            chase_key=chase_key,
                            source=source,
                        )
                        # 立即标记已触发，防止重复提交
                        alert_state[c["alert_key"]] = (price, True)
                        alert_state[chase_key] = self._scan_count
                continue

            if not in_zone and not below_zone:
                continue

            # ━━ 2c. 低于买入区 → 回调评估 ━━
            if below_zone and not in_zone:
                below_pct = (buy_min - price) / buy_min * 100

                # 信号类候选（盘前生成）：价格低于买入区 0.5%+ 视为 zone 失效
                if source == "signal" and below_pct > 0.5:
                    logger.info(f"买入决策 [{code} {name}] 放弃 信号买入区失效 低于{buy_min:.2f} {below_pct:.1f}%")
                    alert_state[c["alert_key"]] = (price, True)
                    self._alert(
                        f"❌ {tag}信号放弃 — {code} {name}\n"
                        f"   现价: {price:.2f}  买入区: {buy_min:.2f}~{buy_max:.2f}\n"
                        f"   → 价格低于买入区 {below_pct:.1f}%，zone 已失效"
                    )
                    if on_abandon:
                        on_abandon()
                    continue

                # 去重：已推送且价格变化 < 0.5% 则跳过
                prev_state = alert_state.get(c["alert_key"])
                if prev_state is not None and prev_state[1]:
                    prev_price = prev_state[0]
                    if prev_price > 0 and abs(price - prev_price) / prev_price < 0.005:
                        continue

                below_pct = (buy_min - price) / buy_min * 100
                below_action, below_reason, below_mul = self._evaluate_below_zone(code, price, buy_min, buy_max)

                if below_action == "abandon":
                    logger.info(f"买入决策 [{code} {name}] 放弃 低于买入区{below_pct:.1f}% → {below_reason}")
                    alert_state[c["alert_key"]] = (price, True)
                    if not quiet:
                        self._alert(
                            f"❌ {tag}信号放弃 — {code} {name}\n"
                            f"   现价: {price:.2f}  低于区间: {buy_min:.2f}~{buy_max:.2f}\n"
                            f"   板块:{trend}\n"
                            f"   → {below_reason}"
                        )
                    if on_abandon:
                        on_abandon()
                    continue

                elif below_action == "watching":
                    logger.info(f"买入决策 [{code} {name}] 观察 低于买入区{below_pct:.1f}% → {below_reason}")
                    continue

                else:
                    # below_zone 也要遵守 entry_rule（next_day/none 禁止买入）
                    if entry_rule in ("next_day", "none"):
                        alert_state[c["alert_key"]] = (price, True)
                        logger.info(f"买入决策 [{code} {name}] 低于买入区但 entry_rule={entry_rule}，跳过")
                        continue

                    alert_state[c["alert_key"]] = (price, True)
                    context = self._analyze_buy_context(code, price, buy_min, buy_max)
                    market_note = ""
                    if not market_ok:
                        market_note = f"\n   ⚠️ 大盘风险: {regime_alert_msg or self._get_market_risk_advice()}"
                    self._alert(
                        f"🟢 {tag}回调买入 — {code} {name}\n"
                        f"   现价: {price:.2f}  低于区间: {buy_min:.2f}~{buy_max:.2f}  止损: {sl:.2f}  止盈: {tp:.2f}\n"
                        f"   板块:{trend}\n"
                        f"   ─────────────────────────\n"
                        f"{context}\n"
                        f"   📐 {below_reason}  单票仓位 {settings.DEFAULT_POSITION_PCT * below_mul:.1%}{market_note}"
                    )
                    if not paper_full and market_ok:
                        self._execute_paper_buy(
                            code,
                            name,
                            price,
                            buy_min,
                            buy_max,
                            sl,
                            tp,
                            score,
                            source,
                            c["signal_id"] or 0,
                            below_mul * position_mult,
                            pattern,
                            trend,
                            regime=regime_obj,
                        )
                    continue

            # ━━ 2d. 买入区内 → entry_rule过滤 → 多维评估 → 告警+执行 ━━
            prev_state = alert_state.get(c["alert_key"])
            if prev_state is not None and prev_state[1]:
                prev_price = prev_state[0]
                if prev_price > 0 and abs(price - prev_price) / prev_price < 0.005:
                    continue

            if not market_ok and source != "chase_surge":
                # 大盘不好时不要反复推送"暂停买入"——每 60 轮最多报一次
                # 追涨(chase_surge)豁免：逆势拉涨本身就有信号价值
                reject_key = f"mkt_reject:{c['alert_key']}"
                last_reject = getattr(self, "_review_market_reject", {}).get(reject_key, -999)
                if self._scan_count - last_reject < 60:
                    continue
                if not hasattr(self, "_review_market_reject"):
                    self._review_market_reject = {}
                self._review_market_reject[reject_key] = self._scan_count

                alert_state[c["alert_key"]] = (price, True)
                market_advice = regime_alert_msg or self._get_market_risk_advice()
                if not quiet:
                    self._alert(
                        f"⏸️ {tag}大盘风险 — {code} {name}\n"
                        f"   现价: {price:.2f}  买入区: {buy_min:.2f}~{buy_max:.2f}\n"
                        f"   板块:{trend}\n"
                        f"   → {market_advice}"
                    )
                continue

            # ── entry_rule 过滤（大盘环境决定入场策略） ──
            # regime_unstable_day：强制最保守策略
            _effective_entry_rule = entry_rule
            if getattr(regime_obj, "regime_unstable_day", False):
                _effective_entry_rule = "next_day"
                # 同时将 position_mult 封顶 0.3
                if position_mult > 0.3:
                    position_mult = 0.3

            # 盘前 AI 信号跳过 entry_rule，信任 AI 选股；盘中动态候选遵守 entry_rule
            entry_skip_reason = ""
            if source != "signal":
                zone_pos = (price - buy_min) / (buy_max - buy_min) if buy_max > buy_min else 0.5
                if _effective_entry_rule == "next_day":
                    entry_skip_reason = "尾盘拉升/次日再看，今日不追"
                elif _effective_entry_rule == "confirm":
                    # 动态阈值：极端普跌日放宽，相对强度股票不必等深回调
                    confirm_threshold = 0.5
                    breadth = getattr(self, "_market_breadth", {})
                    up, down = breadth.get("up", 0), breadth.get("down", 0)
                    if up + down > 0 and down / (up + down) > 0.75:
                        # down/up > 3:1，全市场极端普跌
                        sector_pct = (
                            getattr(self, "_sector_stats", {}).get(getattr(self, "_sector_cache", {}).get(code, ""), 0)
                            or 0
                        )
                        if sector_pct > 0:
                            confirm_threshold = 0.75  # 板块走强 → 大幅放宽
                        else:
                            confirm_threshold = 0.60  # 板块一般 → 小幅放宽
                    if zone_pos > confirm_threshold:
                        entry_skip_reason = f"需确认信号(zone_pos={zone_pos:.0%}>{confirm_threshold:.0%})，等回调"
                elif _effective_entry_rule == "pullback":
                    if zone_pos > 0.4:
                        entry_skip_reason = f"等回调买入(zone_pos={zone_pos:.0%})，暂不追高"
                elif _effective_entry_rule == "range_boundary":
                    if zone_pos > 0.25:
                        entry_skip_reason = f"宽幅震荡(zone_pos={zone_pos:.0%})，等区间下沿再入场"
                elif _effective_entry_rule == "standard":
                    # standard/default 也做 zone_pos 检查，防止 pattern=unknown 时裸奔
                    if zone_pos > 0.5:
                        entry_skip_reason = f"zone_pos={zone_pos:.0%}>50%，等回调到区间下半部"

            if entry_skip_reason:
                alert_state[c["alert_key"]] = (price, True)
                try:
                    self._log_buy_filter(
                        signal_id=c.get("signal_id", 0),
                        stock_code=code,
                        entry_rule=entry_rule,
                        reason_filtered=entry_skip_reason,
                        price=price,
                        buy_min=buy_min,
                        buy_max=buy_max,
                        market_regime=pattern,
                        sector_trend=trend,
                        zone_pos=zone_pos,
                    )
                except Exception:
                    pass
                if not quiet:
                    self._alert(
                        f"⏸️ {tag}暂缓买入 — {code} {name}\n"
                        f"   现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}\n"
                        f"   板块:{trend}\n"
                        f"   → {entry_skip_reason}"
                    )
                continue

            if self._is_limit_up(code, price):
                alert_state[c["alert_key"]] = (price, True)
                if not quiet:
                    self._alert(
                        f"🚫 涨停无法买入 — {code} {name}\n"
                        f"   涨停价: {self._limit_cache.get(code, {}).get('limit_up', 0):.2f}\n"
                        f"   板块:{trend}\n"
                        f"   → 封涨停板，不建议排板"
                    )
                continue

            decision_allowed, decision_reason, size_mul = self._evaluate_buy_decision(code, price, buy_min, buy_max)

            alert_state[c["alert_key"]] = (price, True)

            if not decision_allowed:
                # 进入买入区但系统拒绝 → 推送拒绝理由，不含买入建议
                logger.info(
                    f"买入决策 [{code} {name}] 拒绝 价格{price:.2f} 区间{buy_min:.2f}~{buy_max:.2f} → {decision_reason}"
                )
                # 日内技术指标
                intra_str = ""
                intra = self._get_intraday_indicators(code)
                if intra["available"]:
                    macd_dir = (
                        "多头"
                        if intra["macd_direction"] == "bullish"
                        else "空头"
                        if intra["macd_direction"] == "bearish"
                        else "震荡"
                    )
                    parts = [f"RSI6={intra['rsi6']:.0f} RSI12={intra['rsi12']:.0f}"]
                    parts.append(f"MACD={macd_dir}({intra['macd_bar']:.2f})")
                    parts.append(f"KDJ K={intra['kdj_k']:.1f} D={intra['kdj_d']:.1f} J={intra['kdj_j']:.1f}")
                    intra_str = f"\n   日内: {' | '.join(parts)}"
                # 异步 AI 二判：提交后台，结果在后续扫描中推送
                self._ai_chase_opinion(
                    code,
                    name,
                    price,
                    buy_min,
                    buy_max,
                    sl,
                    tp,
                    trend,
                    (price - buy_max) / buy_max * 100,
                    reject_reason=decision_reason,
                    intra_str=intra_str,
                    alert_key=c["alert_key"],
                    chase_key="",
                    source=source,
                )
                if not quiet:
                    self._alert(
                        f"⏸️ 暂不买入 — {code} {name}\n"
                        f"   现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}  止损: {sl:.2f}  止盈: {tp:.2f}\n"
                        f"   板块:{trend}{intra_str}\n"
                        f"   ⛔ {decision_reason}"
                    )
                continue

            context = self._analyze_buy_context(code, price, buy_min, buy_max)

            # position_mult=0 硬阻断：regime 明确禁止开仓时不计算仓位
            if position_mult <= 0:
                alert_state[c["alert_key"]] = (price, True)
                if not quiet:
                    self._alert(
                        f"⛔ 禁止开仓 — {code} {name}\n"
                        f"   现价: {price:.2f}  板块:{trend}\n"
                        f"   → 大盘环境禁止买入 (position_mult={position_mult})"
                    )
                continue

            # 提前计算仓位，用于消息显示
            max_amount, _size_reason = self._calculate_position_size(code, price, buy_min, buy_max, pattern, trend)
            try:
                self._log_position_size(
                    stock_code=code,
                    amount=max_amount,
                    base_amount=max_amount,
                    reason=_size_reason,
                    sector_mult=position_mult,
                    zone_mult=size_mul,
                )
            except Exception:
                pass
            if size_mul < 1.0 and max_amount > 0:
                max_amount = int(max_amount * size_mul // 100 * 100)
            actual_pct = max_amount / self.paper_account.total_value if self.paper_account.total_value > 0 else 0

            # 仓位行（全仓/减仓都显示）
            position_line = f"\n   📦 仓位: {actual_pct:.1%} (约¥{max_amount:,})"

            # 理由行（仅减仓时显示）
            reason_line = ""
            if size_mul < 1.0:
                reason_line = f"\n   ⚠️ 理由: {decision_reason}"
                logger.info(f"买入决策 [{code} {name}] 减仓 价格{price:.2f} 仓位{actual_pct:.1%} → {decision_reason}")
            else:
                logger.info(
                    f"买入决策 [{code} {name}] 全仓 价格{price:.2f} 仓位{actual_pct:.1%} 区间{buy_min:.2f}~{buy_max:.2f}"
                )

            self._alert(
                f"🔴 {tag}买入信号 — {code} {name}\n"
                f"   现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}  止损: {sl:.2f}  止盈: {tp:.2f}"
                f"{position_line}{reason_line}\n"
                f"   板块:{trend}\n"
                f"   ─────────────────────────\n"
                f"{context}"
            )

            if paper_full or not decision_allowed:
                continue

            self._execute_paper_buy(
                code,
                name,
                price,
                buy_min,
                buy_max,
                sl,
                tp,
                score,
                source,
                c["signal_id"] or 0,
                size_mul * position_mult,
                pattern,
                trend,
                regime=regime_obj,
            )
            alert_state[c["alert_key"]] = (price, True)

    def _check_signals(self, state, prices: dict[str, float], regime):
        """检查 pending 信号 → 转换为统一候选 → 送入公共管线。regime: MarketRegime 或旧版 bool。"""
        try:
            signals = self.repo.get_pending_signals(account="paper")
        except Exception as e:
            logger.warning(f"获取待处理信号异常: {e}")
            return

        candidates = []
        for s in signals:
            code = s["stock_code"]
            price = prices.get(code)
            if price is None:
                continue
            buy_min = s.get("buy_zone_min") or 0
            buy_max = s.get("buy_zone_max") or 0
            if buy_min <= 0 or buy_max <= 0:
                continue
            name = s.get("stock_name", "")
            if not name or name == code:
                name = self._resolve_name(code)

            sl = s.get("stop_loss", 0) or 0
            tp = s.get("take_profit", 0) or 0
            # 缺止损/止盈时自动从技术指标补算，不废弃信号
            if sl <= 0 or tp <= 0:
                calc_sl, calc_tp = self._calc_fallback_sl_tp(code, price)
                if sl <= 0:
                    sl = calc_sl
                if tp <= 0:
                    tp = calc_tp
                logger.info(f"  信号 {code} {name} AI未给止损/止盈，自动补算 sl={sl:.2f} tp={tp:.2f}")
                if sl <= 0 and tp <= 0:
                    # 两个都补算失败才废弃
                    logger.warning(f"  信号 {code} {name} 无法补算止损止盈，废弃")
                    try:
                        self.repo.update_signal_status(s["id"], "expired")
                    except Exception:
                        pass
                    continue

            candidates.append(
                {
                    "code": code,
                    "name": name,
                    "price": price,
                    "buy_min": buy_min,
                    "buy_max": buy_max,
                    "sl": sl,
                    "tp": tp,
                    "score": s.get("signal_score", 0),
                    "trend": self._get_sector_trend(code),
                    "source": "signal",
                    "alert_key": s["id"],
                    "signal_id": s["id"],
                }
            )

        if candidates:
            self._check_buy_candidates(state, candidates, regime)

    def _execute_paper_buy(
        self,
        code: str,
        name: str,
        price: float,
        buy_min: float,
        buy_max: float,
        sl: float,
        tp: float,
        score: float,
        source: str,
        signal_id: int,
        size_mul: float,
        pattern: str,
        trend: str,
        regime=None,
    ):
        """统一的模拟盘买入执行：仓位计算 + 风控 + 下单。

        regime: MarketRegime | bool | None。size_mul 已由调用方修正过。
        大盘 stop_mult 用于动态调整止损宽度。
        """
        # 名额质量门控：名额越少要求越高，防止低质量信号占坑
        # 追涨豁免：逆势拉涨的票，用更小的仓位试错也是合理的
        filled = len(self.paper_account.positions)
        remaining = settings.MAX_POSITIONS - filled
        if source == "chase_surge":
            min_mul = 0.15  # 追涨门槛低，用仓位控制风险
        else:
            min_mul = {0: 0.99, 1: 0.80, 2: 0.65, 3: 0.55, 4: 0.50}.get(remaining, 0.50)
        if size_mul < min_mul:
            logger.info(
                f"买入决策 [{code} {name}] 放弃 source={source} 名额{filled}/{settings.MAX_POSITIONS} "
                f"质量不足 size_mul={size_mul:.0%} < {min_mul:.0%}"
            )
            return

        # 兼容旧版 bool market_ok
        if isinstance(regime, bool):
            if not regime:
                return
        if size_mul <= 0:
            return

        # 价格区间校验：成交价低于买入区下沿超 2% 则拒绝
        if price < buy_min * 0.98:
            logger.warning(
                f"买入拒绝 [{code} {name}] 价格{price:.2f}低于买入区下沿{buy_min:.2f}"
                f" ({(buy_min - price) / buy_min * 100:.1f}%)，zone 失效"
            )
            try:
                self.repo.update_signal_status(signal_id, "expired")
            except Exception:
                pass
            return

        # 大盘 stop_mult 调整止损宽度
        stop_mult = getattr(regime, "stop_mult", 1.0) if regime and not isinstance(regime, bool) else 1.0
        if stop_mult != 1.0 and sl > 0 and price > sl:
            stop_width = price - sl
            effective_sl = price - stop_width * stop_mult
            sl = round(effective_sl, 2)
            logger.info(f"止损调整: {code} stop_mult={stop_mult} {sl}")

        max_amount, size_reason = self._calculate_position_size(
            code,
            price,
            buy_min,
            buy_max,
            pattern,
            trend,
        )
        if max_amount <= 0:
            return

        if size_mul < 1.0:
            max_amount = int(max_amount * size_mul // 100 * 100)
        # 追涨允许更小仓位试错
        min_amount = 3000 if source == "chase_surge" else 5000
        if max_amount < min_amount:
            logger.info(f"买入拒绝 [{code} {name}] source={source} 仓位不足 max_amount={max_amount} < {min_amount}")
            return

        target_pct = max_amount / self.paper_account.total_value if self.paper_account.total_value > 0 else 0.10
        sector = self._industry_cache.get(code, "") if hasattr(self, "_industry_cache") else ""
        risk_result = self.risk_engine.can_open(
            code,
            target_pct,
            sector_code=sector,
            portfolio=self.paper_account,
        )
        if not risk_result.allowed:
            return

        # 计算股数（盯盘决策，模拟盘只管执行）
        capital = min(max_amount, self.paper_account.total_value * settings.DEFAULT_POSITION_PCT)
        # 现金约束：留 10% 缓冲，其余全可用
        max_affordable = int(self.paper_account.cash * 0.9 / price / 100) * 100
        volume = min(int(capital / price / 100) * 100, max_affordable)
        if volume < 100:
            logger.info(f"模拟盘资金不足买入 {code}")
            return

        result = self.paper_account.buy(
            code,
            name,
            price,
            volume,
            signal_id=signal_id,
            source=source,
        )
        if result.success:
            try:
                self.repo.update_signal_status(signal_id, "bought")
            except Exception:
                pass
            # 决策日志
            try:
                self._log_buy_trigger(
                    signal_id=signal_id,
                    stock_code=code,
                    price=price,
                    buy_min=buy_min,
                    buy_max=buy_max,
                    position_size=max_amount,
                    entry_rule=getattr(regime, "entry_rule", "standard") if regime else "standard",
                    sector_trend=trend,
                    market_regime=pattern,
                )
            except Exception:
                pass
            # 写入 _pos_meta（盯盘决策数据）
            self._pos_meta[code] = {
                "sl": sl,
                "tp": tp,
                "trailing_stop": 0.05,
                "highest_price": price,
                "sector": sector,
                "score": score,
                "signal_id": signal_id,
                "buy_sector_trend": trend,  # 买入时的板块趋势（用于后续对比）
                "buy_scan": self._scan_count,
            }
            # 同步到持仓对象（持久化止损止盈）
            pos = self.paper_account.positions.get(code)
            if pos:
                pos.stop_loss = sl
                pos.take_profit = tp
            existing = self._bought_watch.get(code, {})
            self._bought_watch[code] = {
                "entry_price": price,
                "last_alert_scan": self._scan_count,
                "buy_scan": self._scan_count,
                "buy_trade_date": self._trade_date,
                "status": "watching",
                "alert_count": 0,
                "max_profit_pct": existing.get("max_profit_pct", 0),
            }
            self._invalidate_watch_codes_cache()

    def _check_review_picks(self, state, prices: dict[str, float], regime):
        """复盘推荐 → 转换为统一候选 → 送入公共管线。regime: MarketRegime 或旧版 bool。"""
        monitor = self._get_review_monitor()
        if monitor is None:
            return
        if not monitor.is_loaded():
            monitor.load_picks()

        # 已入库 trade_signals 的 REVIEW 信号由 _check_signals 处理，这里跳过
        review_zones = self._load_review_signal_zones()

        candidates = []
        for code in monitor.get_codes():
            if code in review_zones:
                continue
            price = prices.get(code)
            if price is None:
                continue
            buy_min, buy_max = monitor.get_buy_zone(code)
            if buy_min <= 0 or buy_max <= 0:
                continue

            pick = monitor.get_pick(code)
            candidates.append(
                {
                    "code": code,
                    "name": pick.get("name", ""),
                    "price": price,
                    "buy_min": buy_min,
                    "buy_max": buy_max,
                    "sl": pick.get("stop_loss", 0) or 0,
                    "tp": pick.get("target_price", 0) or 0,
                    "score": pick.get("score", 0),
                    "trend": self._get_sector_trend(code),
                    "source": "review",
                    "alert_key": code,
                    "signal_id": None,
                }
            )

        if candidates:
            self._check_buy_candidates(state, candidates, regime)

    # ------------------------------------------------------------------
    # 结构化买入区间加载（优先于 MA 动态计算）
    # ------------------------------------------------------------------

    def _load_review_signal_zones(self) -> dict[str, tuple[float, float, float, float]]:
        """从 trade_signals 加载 REVIEW 信号的结构化买入区间。
        返回 {code: (buy_min, buy_max, sl, tp)}。
        """
        try:
            rows = self.repo.get_review_signal_zones(self._trade_date)
            return {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0) for r in rows if r[1] and r[2]}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 开盘决策汇总（集合竞价后第一轮，替代之前的两个开盘参考）
    # ------------------------------------------------------------------

    def _get_review_monitor(self):
        if self._review_monitor is None:
            try:
                from trade.core.review_picks import ReviewPickMonitor

                self._review_monitor = ReviewPickMonitor(
                    db_path=self.db_path,
                    telegram_bot=self.telegram,
                )
            except Exception as e:
                logger.warning(f"复盘推荐监控器初始化失败: {e}")
        return self._review_monitor

    def _load_review_picks(self) -> list[dict]:
        """从 stock_tracker 读最新复盘推荐（含止损/目标价）。"""
        try:
            rows = self.repo.get_review_picks_latest()
            return [
                {
                    "stock_code": r[0],
                    "stock_name": r[1],
                    "stop_loss": r[2] or 0,
                    "target_price": r[3] or 0,
                    "abandon_condition": r[4] or "",
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"加载复盘推荐失败: {e}")
            return []

    def _generate_hot_sector_candidates(self, prices: dict[str, float]) -> list[dict]:
        """盘中动态发现：为热门板块中的领涨股生成买入候选。

        用 _detect_hot_sectors() 找热点板块，遍历 _market_snapshot 实时数据
        找板块内股票，生成动态买入区间后喂入 _check_buy_candidates 统一管线。
        """
        if not settings.DYNAMIC_SECTOR_DISCOVERY_ENABLED:
            return []
        if not self._market_snapshot:
            return []

        hot_sectors = self._detect_hot_sectors()
        if not hot_sectors:
            return []

        # 去重：已在信号/持仓/卖出冷却中的不重复生成
        existing = set(self.paper_account.positions.keys())
        existing.update(self._recently_sold.keys() if hasattr(self, "_recently_sold") else [])
        try:
            for s in self.repo.get_pending_signals(account="paper"):
                existing.add(s.get("stock_code", ""))
        except Exception:
            pass

        self._ensure_industry_cache()

        candidates = []
        for sector in hot_sectors[:5]:  # 取热度前 5 的板块
            sname = sector["name"]
            if any(kw in sname for kw in self._CHASE_EXCLUDED_SECTORS):
                continue

            # 从实时 _market_snapshot 中找该板块股票（替代 stock_basic DB 查询）
            sector_stocks = []
            for code, item in self._market_snapshot.items():
                if code in existing:
                    continue
                if self._industry_cache.get(code, "") != sname:
                    continue
                try:
                    price_f = float(item.get("price", 0))
                    chg = float(item.get("changePct", 0))
                except (ValueError, TypeError):
                    continue
                if price_f <= 10:
                    continue
                if chg < -3:
                    continue
                sector_stocks.append((code, price_f, chg))

            # 按涨跌幅降序，取 top N
            sector_stocks.sort(key=lambda x: -x[2])
            sector_stocks = sector_stocks[: settings.DYNAMIC_SECTOR_MAX_CANDIDATES]

            for code, price_f, day_chg in sector_stocks:
                # 动态买入区：板块越强，区间越窄（越有追击价值）
                # 买入区不对称：下方宽（等回调），上方窄（不追高）
                zone_pct = max(1.5, 3.0 - sector["score"] * 0.3)
                buy_min = round(price_f * (1 - zone_pct / 100), 2)
                buy_max = round(price_f * (1 + zone_pct / 200), 2)
                sl = self._calc_unified_sl(code, price_f, trend="", strategy="chase")
                tp = self._calc_unified_tp(code, price_f, trend="", strategy="chase")

                candidates.append(
                    {
                        "code": code,
                        "name": self._resolve_name(code),
                        "price": price_f,
                        "buy_min": buy_min,
                        "buy_max": buy_max,
                        "sl": sl,
                        "tp": tp,
                        "score": 65 + sector["score"] * 5,
                        "trend": self._get_sector_trend(code),
                        "source": "sector_discovery",
                        "alert_key": f"sd:{code}",
                        "signal_id": None,
                    }
                )

        if candidates:
            names = ", ".join(f"{c['code']} {c['name']}" for c in candidates[:6])
            logger.info(f"动态板块发现(实时): {len(candidates)}只候选 ({names})")
        return candidates

    # 过滤板块：与 strategy/screening/trend.py _SECTOR_BLACKLIST 保持一致
    _CHASE_EXCLUDED_SECTORS = [
        "银行",
        "证券",
        "保险",
        "白酒",
        "酒",
        "食品",
        "消费",
        "饮料",
        "零售",
        "百货",
        "家电",
        "服装",
        "纺织",
        "石油",
        "地产",
        "房地产",
        "农业",
        "养殖",
        "种植",
        "环保",
        "公路",
        "铁路",
        "高速",
        "港口",
        "电力",
        "煤炭",
        "钢铁",
    ]

    def _generate_chase_candidates(self, rapid_hits: list[dict], prices: dict[str, float]) -> list[dict]:
        """急拉放量追涨候选生成：硬规则过滤后喂入 _check_buy_candidates。

        两步：① 异动检测的 rapid_hits（两轮对比，精准但稀少）
             ② 当前快照扫描（单轮涨幅>3%，覆盖持续拉涨的票）
        没有 AI，纯规则判断，毫秒级完成。急拉票时效性第一。
        """
        if not rapid_hits:
            rapid_hits = []

        # 补充扫描：当前快照中涨幅 > 3% 的票（不依赖两轮对比）
        snapshot = getattr(self, "_market_snapshot", {}) or {}
        scanned_codes = {h["code"] for h in rapid_hits}
        for code, item in snapshot.items():
            if code in scanned_codes:
                continue
            try:
                chg = float(item.get("changePct", 0))
                price = float(item.get("price", 0))
                amount = float(item.get("amount", 0) or 0)
            except (ValueError, TypeError):
                continue
            if chg < 3 or price <= 0:
                continue
            industry = (getattr(self, "_industry_cache", {}) or {}).get(code, "")
            rapid_hits.append(
                {
                    "code": code,
                    "delta_pct": 0,  # 非两轮对比，无 delta
                    "vol_ratio": 1.0,
                    "name": "",
                    "industry": industry,
                    "price": price,
                    "change_pct": chg,
                    "amount": amount,
                }
            )

        if not rapid_hits:
            return []

        logger.info(f"急拉追涨: 收到 {len(rapid_hits)} 只候选，开始规则过滤")

        regime = getattr(self, "_regime", None)
        # 追涨只在极端行情暂停（恐慌/熔断），普通偏弱允许参与
        risk_level = getattr(regime, "risk_level", "safe") if regime else "safe"
        pattern = getattr(regime, "pattern", "normal") if regime else "normal"
        chase_blocked = risk_level in ("extreme",) or pattern in ("panic", "halt")
        sector_stats = getattr(self, "_sector_stats", {}) or {}
        industry_cache = getattr(self, "_industry_cache", {}) or {}

        # 板块排名（TOP 10）
        ranked_sectors = sorted(
            sector_stats.items(),
            key=lambda x: x[1].get("change_pct", 0),
            reverse=True,
        )
        top10_sectors = {name for name, _ in ranked_sectors[:10]}

        # 同板块已持仓计数
        held_sectors: dict[str, int] = {}
        for code in self.paper_account.positions:
            ind = industry_cache.get(code, "")
            if ind:
                held_sectors[ind] = held_sectors.get(ind, 0) + 1

        # 已持仓 code 集合
        held_codes = set(self.paper_account.positions.keys())
        recently_sold = getattr(self, "_recently_sold", {}) or {}

        candidates = []
        skipped = {
            "low_chg": 0,
            "high_chg": 0,
            "weak_sector": 0,
            "low_vol": 0,
            "sector_full": 0,
            "held": 0,
            "excluded_sector": 0,
            "near_limit": 0,
            "market_halt": 0,
        }

        for item in rapid_hits:
            code = item["code"]
            name = item.get("name", "")
            price = item.get("price", 0)
            chg = item.get("change_pct", 0)
            vol_ratio = item.get("vol_ratio", 0)
            industry = item.get("industry", "")

            if price <= 0:
                continue

            # ── 大盘极端行情暂停追涨（恐慌/熔断），普通偏弱允许 ──
            if chase_blocked:
                skipped["market_halt"] += 1
                continue

            # ── 已持仓 ──
            if code in held_codes:
                skipped["held"] += 1
                continue

            # ── 近期卖出冷却 ──
            if code in recently_sold and self._scan_count - recently_sold[code] < 60:
                continue

            # ── 板块黑名单 ──
            if any(kw in industry for kw in self._CHASE_EXCLUDED_SECTORS):
                skipped["excluded_sector"] += 1
                continue

            # ── 涨跌幅边界 ──
            limit_pct = 0.20 if code.startswith(("300", "688")) else 0.10
            chg_min = 3.0
            chg_max = limit_pct * 100 - 3  # 离涨停留 3% 空间
            if chg < chg_min:
                skipped["low_chg"] += 1
                continue
            if chg > chg_max:
                skipped["high_chg"] += 1
                continue

            # ── 板块相对强度：个股跑赢板块 > 2%（比板块绝对涨幅更可靠）──
            sec = sector_stats.get(industry, {})
            sec_chg = sec.get("change_pct", 0) if sec else 0
            if sec_chg is None:
                sec_chg = 0
            outperformance = chg - sec_chg  # 个股相对板块的超额收益
            if outperformance < 2.0:
                skipped["weak_sector"] += 1
                continue

            # ── 量能 ──
            # 异动检测来的用 vol_ratio，补充扫描来的检查绝对成交额
            if vol_ratio >= 2.0:
                pass  # 两轮对比放量，可靠
            elif item.get("amount", 0) > 50_000_000:  # 成交额 > 5000万
                pass
            else:
                skipped["low_vol"] += 1
                continue

            # ── 同板块持仓 ≤ 1 ──
            if held_sectors.get(industry, 0) > 1:
                skipped["sector_full"] += 1
                continue

            # ── 通过 → 生成候选 ──
            if not name:
                name = self._resolve_name(code)
            buy_min = round(price * 0.99, 2)
            buy_max = round(price * 1.01, 2)
            sl = round(price * 0.97, 2)
            tp = round(price * 1.05, 2)

            candidates.append(
                {
                    "code": code,
                    "name": name,
                    "price": price,
                    "buy_min": buy_min,
                    "buy_max": buy_max,
                    "sl": sl,
                    "tp": tp,
                    "score": 65,
                    "trend": self._get_sector_trend(code),
                    "source": "chase_surge",
                    "alert_key": f"chase:{code}",
                    "signal_id": None,
                }
            )

        if candidates:
            logger.info(f"急拉追涨: {len(candidates)}只通过 (过滤: {skipped})")
        elif rapid_hits:
            logger.info(f"急拉追涨: 0/{len(rapid_hits)}只通过 (过滤: {skipped})")

        return candidates

    def _scan_pullback_opportunities(self, prices: dict[str, float]) -> list[dict]:
        """盘中回踩机会发现：在强势板块中找正在回踩且止跌企稳的个股。

        遍历 _market_snapshot 全市场数据，筛选：
          1. 板块日内涨幅 > 0.5%（强势）
          2. 个股涨幅 -4% ~ +1%（回踩区）
          3. 10分钟价格走势出现止跌（非持续下跌）
        返回候选列表，喂入 _check_buy_candidates 统一管线。
        """
        if not settings.PULLBACK_SCAN_ENABLED:
            return []
        if not self._market_snapshot or not self._sector_stats:
            return []

        # ━ 1. 找出强势板块 ━
        strong_sectors: list[dict] = []
        for name, stats in self._sector_stats.items():
            chg = stats.get("change_pct", 0)
            if chg > settings.PULLBACK_SECTOR_MIN_CHANGE:
                history = stats.get("trend_history", [])
                if len(history) >= 3:
                    strong_sectors.append(
                        {
                            "name": name,
                            "change_pct": chg,
                            "continuity": stats.get("continuity", 0),
                        }
                    )

        if not strong_sectors:
            return []

        strong_sectors.sort(key=lambda x: -x["change_pct"])
        sector_names = {s["name"] for s in strong_sectors[:5]}

        # ━ 2. 去重排除 ━
        excluded = set(self.paper_account.positions.keys())
        excluded.update(self._recently_sold.keys() if hasattr(self, "_recently_sold") else [])
        try:
            for s in self.repo.get_pending_signals(account="paper"):
                excluded.add(s.get("stock_code", ""))
        except Exception:
            pass
        # 已推送过的当日不再重复
        excluded.update(getattr(self, "_pullback_alerted_today", set()))

        self._ensure_industry_cache()

        # ━ 3. 扫描全市场快照 ━
        opportunities = []
        for code, item in self._market_snapshot.items():
            if code in excluded:
                continue
            industry = self._industry_cache.get(code, "")
            if not industry or industry not in sector_names:
                continue

            try:
                chg = float(item.get("changePct", 0))
                price_f = float(item.get("price", 0))
            except (ValueError, TypeError):
                continue

            # 回踩区：-4% ~ 0%（只选绿的，红的说明已开始反弹或一直在涨）
            if not (-4 < chg <= 0):
                continue
            if price_f < settings.PULLBACK_PRICE_MIN:
                continue

            # 涨跌停过滤
            limit_info = self._limit_cache.get(code, {})
            limit_up = limit_info.get("limit_up", 0)
            limit_down = limit_info.get("limit_down", 0)
            if limit_up > 0 and price_f >= limit_up * 0.99:
                continue
            if limit_down > 0 and price_f <= limit_down * 1.01:
                continue

            # 10分钟止跌检查
            is_stable, _ = self._check_snapshot_stabilization(code)
            if not is_stable:
                continue

            sec = next((s for s in strong_sectors if s["name"] == industry), None)
            if not sec:
                continue

            pullback_depth = sec["change_pct"] - chg

            opportunities.append(
                {
                    "code": code,
                    "name": self._resolve_name(code),
                    "price": price_f,
                    "change_pct": chg,
                    "sector": industry,
                    "sector_change": sec["change_pct"],
                    "sector_continuity": sec["continuity"],
                    "pullback_depth": pullback_depth,
                }
            )

        if not opportunities:
            return []

        # ━ 4. 评分排序：回踩深+板块强 → 优先 ━
        def _score(op):
            return op["pullback_depth"] * 2 + op["sector_continuity"] * 0.5

        opportunities.sort(key=lambda o: -_score(o))
        top = opportunities[:6]  # 最多 6 只候选

        # ━ 5. 生成买入候选（与信号候选同格式，喂入 _check_buy_candidates） ━
        candidates = []
        for op in top:
            price_f = op["price"]
            # 买入区不对称：下方宽（回踩买入），上方窄（不追高）
            zone_pct = max(1.5, 3.0 - op["pullback_depth"] * 0.2)
            buy_min = round(price_f * (1 - zone_pct / 100), 2)
            buy_max = round(price_f * (1 + zone_pct / 200), 2)
            sl = self._calc_unified_sl(op["code"], price_f, trend="", strategy="chase")
            tp = self._calc_unified_tp(op["code"], price_f, trend="", strategy="chase")

            candidates.append(
                {
                    "code": op["code"],
                    "name": op["name"],
                    "price": price_f,
                    "buy_min": buy_min,
                    "buy_max": buy_max,
                    "sl": sl,
                    "tp": tp,
                    "score": 60 + int(op["pullback_depth"] * 5),
                    "trend": self._get_sector_trend(op["code"]),
                    "source": "pullback_scan",
                    "alert_key": f"pb:{op['code']}",
                    "signal_id": None,
                }
            )
            self._pullback_alerted_today.add(op["code"])

        if candidates:
            names = ", ".join(
                f"{c['code']} {c['name']}(回踩{c.get('pullback_depth', c['score'] - 60) / 5:.1f}%)"
                for c in candidates[:4]
            )
            logger.info(f"回踩机会发现: {len(candidates)}只 {names}")

        return candidates

    def _check_snapshot_stabilization(self, code: str) -> tuple[bool, str]:
        """分析全市场快照中某只股票近10分钟价格是否止跌企稳。"""
        hist = getattr(self, "_snapshot_price_history", {}).get(code, [])
        if len(hist) < 4:
            return False, "数据不足"

        prices = [p for _, p in hist]
        first_p = prices[0]
        last_p = prices[-1]
        min_p = min(prices)

        # 一票否决：持续下跌（每个点都比前一个低）
        if all(prices[i] < prices[i - 1] for i in range(1, len(prices))):
            return False, "持续下跌中"

        # 一票否决：加速下跌
        mid = len(prices) // 2
        if len(prices) >= 6:
            first_slope = (prices[mid - 1] - prices[0]) / prices[0] * 100
            second_slope = (prices[-1] - prices[mid]) / prices[mid] * 100
            if second_slope < first_slope - 0.3:
                return False, "跌势加速"

        # 止跌信号 A：最近 3 点走平或回升
        recent = prices[-min(3, len(prices)) :]
        recent_chg = (recent[-1] - recent[0]) / recent[0] * 100 if recent[0] > 0 else 0
        if recent_chg >= -0.3:
            return True, f"走平{recent_chg:+.2f}%"

        # 止跌信号 B：从低点反弹
        if min_p > 0 and min_p < prices[-2]:
            bounce = (last_p - min_p) / min_p * 100
            if bounce > 0.5:
                return True, f"反弹{bounce:+.1f}%"

        # 止跌信号 C：窄幅横盘
        recent_range = (max(recent) - min(recent)) / last_p * 100 if last_p > 0 else 0
        if recent_range < 0.5:
            return True, f"窄幅横盘{recent_range:.1f}%"

        return False, f"整体跌{(last_p - first_p) / first_p * 100:+.1f}%"

    # ======================== 开盘强度分析 ========================

    def _get_opening_strength(self) -> dict:
        """分析开盘强度：用前30分钟指数数据判断当日基调。
        返回 {pattern, bias, score} — bias: bullish/bearish/neutral。
        高开高走 → bullish+5, 高开低走 → bearish-3, 低开高走 → bullish+3。
        """
        index_prices = getattr(self, "_index_prices", []) or []
        if len(index_prices) < 10:
            return {"pattern": "数据不足", "bias": "neutral", "score": 0}

        # 前30分钟大约30个数据点（每分钟一个）
        n = min(30, len(index_prices))
        early = index_prices[:n]
        if len(early) < 5:
            return {"pattern": "数据不足", "bias": "neutral", "score": 0}

        first = early[0]
        last = early[-1]
        high = max(early)
        low = min(early)
        open_chg = (last - first) / first * 100 if first > 0 else 0
        swing = (high - low) / low * 100 if low > 0 else 0

        # 高开判断：首点相对前收盘（用 index_pre_close）
        idx_quote = getattr(self, "_last_index_quote", None) or {}
        pre_close = idx_quote.get("pre_close", first)
        gap = (first - pre_close) / pre_close * 100 if pre_close > 0 else 0

        score = 0
        pattern = ""
        if gap > 0.5 and open_chg > 0.3:
            pattern = "高开高走"
            bias = "bullish"
            score = 5
        elif gap > 0.5 and open_chg < -0.3:
            pattern = "高开低走"
            bias = "bearish"
            score = -3
        elif gap < -0.3 and open_chg > 0.3:
            pattern = "低开高走"
            bias = "bullish"
            score = 3
        elif gap < -0.3 and open_chg < -0.3:
            pattern = "低开低走"
            bias = "bearish"
            score = -5
        elif abs(gap) < 0.3 and abs(open_chg) < 0.3:
            pattern = "平开窄幅"
            bias = "neutral"
            score = 0
        else:
            pattern = f"高开{open_chg:+.1f}%"
            bias = "neutral"
            score = 0

        # 缓存结果（盘中不变）
        if not hasattr(self, "_cached_opening"):
            self._cached_opening = {
                "pattern": pattern,
                "bias": bias,
                "score": score,
                "gap": round(gap, 2),
                "open_chg": round(open_chg, 2),
                "swing": round(swing, 2),
            }
        return self._cached_opening

    # ======================== 第二层：板块热度 ========================
