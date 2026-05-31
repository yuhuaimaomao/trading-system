"""买入决策管线 — 信号/复盘候选 → 多维评分 → 模拟盘执行。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import logging
import sqlite3

from system.config import settings

logger = logging.getLogger(__name__)


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
        """根据盘面动态计算买入金额（0-16000），返回 (金额, 决策理由)。"""
        # 禁止买入的模式
        BLOCKED = (
            "panic",
            "one_sided",
            "dead_cat",
            "inverted_v",
            "m_top",
            "gap_up_fade",
            "late_dump",
            "fishing_line",
        )
        if pattern in BLOCKED:
            return 0, f"市场{pattern}模式，暂停买入"

        # 基础额度
        CAUTIOUS = (
            "v_reversal",
            "w_bottom",
            "melt_up",
            "late_rally",
            "wide_choppy",
            "gap_down_recover",
        )
        if pattern in CAUTIOUS:
            base = 8000
            reason = f"市场{pattern}模式，谨慎参与"
        elif pattern == "normal":
            base = 16000
            reason = "大盘正常"
        elif pattern == "uptrend":
            base = 16000
            reason = "大盘上行"
        else:
            base = 16000
            reason = ""

        # 板块趋势修正
        if "持续走弱" in sector_trend:
            base = max(base * 0.3, 5000)
            reason += " 板块持续走弱" if reason else "板块持续走弱"
        elif "走弱" in sector_trend:
            base = max(base * 0.6, 5000)
            reason += " 板块走弱" if reason else "板块走弱"
        elif "持续走强" in sector_trend:
            base = min(base * 1.3, 16000)
        elif "走强" in sector_trend:
            base = min(base * 1.2, 16000)

        # 买入区位置修正（下沿1/3 → 激进，上沿1/3 → 保守）
        zone_range = buy_max - buy_min if buy_max > buy_min else 1
        position_in_zone = (price - buy_min) / zone_range
        if position_in_zone <= 0.33:
            # 价格在买入区下沿，可更激进
            base = min(base * 1.1, 16000)
            reason += " 买入区下沿"
        elif position_in_zone >= 0.67:
            # 价格在买入区上沿，偏保守
            base = max(base * 0.7, 5000)
            reason += " 买入区上沿"

        return int(base // 100 * 100), reason.strip()

    def _analyze_buy_context(
        self, code: str, price: float, buy_min: float, buy_max: float
    ) -> str:
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
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_upper, bb_mid, bb_lower, bb_pct_b, ma5, ma10, ma20
                   FROM stock_indicators WHERE stock_code=? AND bb_mid > 0
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            if row:
                bb_upper, bb_mid, bb_lower, pct_b, ma5, ma10, ma20 = row
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

            intra_parts = [f"日内RSI6={r6:.0f} RSI12={r12:.0f}"]
            intra_parts.append(f"MACD={macd_dir}(bar={intra['macd_bar']:.2f})")
            intra_parts.append(
                f"KDJ K={intra['kdj_k']:.1f} D={intra['kdj_d']:.1f} J={j:.1f}"
            )
            if vs_ma5 != 0:
                side = "上" if vs_ma5 > 0 else "下"
                intra_parts.append(f"价在日内MA5{side}{abs(vs_ma5):.1f}%")
            parts.append(f"📉 日内: {' | '.join(intra_parts)}")

        # 7. 盘口 + 大单
        ob_ratio, ob_reason = self._get_order_book_imbalance(code, price)
        if ob_reason:
            parts.append(f"📊 盘口: {ob_reason}(买盘{ob_ratio:.0%})")
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
        if (
            self._intraday_cache_scan == self._scan_count
            and code in self._intraday_cache
        ):
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
            from analysis.screening.indicators import (
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
            conn = sqlite3.connect(self.db_path)

            # 昨日主力资金（有延续性，今天大概率方向一致）
            row = conn.execute(
                """SELECT main_force_net, main_force_ratio,
                          super_large_net, large_net,
                          ma5_angle, pe_dynamic, circ_market_cap
                   FROM stock_basic WHERE stock_code=?
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()

            if row:
                (mf_net, mf_ratio, sl_net, l_net, ma5_angle, pe, circ_cap) = row
                factors["yesterday_mf_net"] = mf_net or 0
                factors["yesterday_mf_ratio"] = mf_ratio or 0
                factors["yesterday_sl_net"] = sl_net or 0
                factors["yesterday_l_net"] = l_net or 0
                factors["ma5_angle"] = ma5_angle or 0
                factors["pe_dynamic"] = pe or 0
                factors["circ_market_cap"] = circ_cap or 0
                factors["available"] = True

            # 昨日技术指标（日线级别趋势，非日内）
            row2 = conn.execute(
                """SELECT macd_dif, macd_dea, macd_bar,
                          kdj_k, kdj_d, kdj_j,
                          rsi6, rsi24,
                          bbi_daily, bbi_weekly, bb_width, ma120
                   FROM stock_indicators WHERE stock_code=?
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()

            if row2:
                (
                    macd_dif,
                    macd_dea,
                    macd_bar,
                    kdj_k,
                    kdj_d,
                    kdj_j,
                    rsi6,
                    rsi24,
                    bbi_daily,
                    bbi_weekly,
                    bb_width,
                    ma120,
                ) = row2
                factors["daily_macd_dif"] = macd_dif or 0
                factors["daily_macd_dea"] = macd_dea or 0
                factors["daily_macd_bar"] = macd_bar or 0
                factors["daily_kdj_k"] = kdj_k or 50
                factors["daily_kdj_d"] = kdj_d or 50
                factors["daily_kdj_j"] = kdj_j or 50
                factors["daily_rsi6"] = rsi6 or 50
                factors["daily_rsi24"] = rsi24 or 50
                factors["bbi_daily"] = bbi_daily or 0
                factors["bbi_weekly"] = bbi_weekly or 0
                factors["bb_width"] = bb_width or 0
                factors["ma120"] = ma120 or 0

            conn.close()

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
                            factors["day_change_pct"] = (
                                (price - do) / do * 100 if do > 0 else 0
                            )
                except Exception:
                    pass

            # 5分钟K线 MACD（今日实时）
            if self.qmt:
                try:
                    from analysis.screening.indicators import calc_macd, calc_rsi

                    raw_5m = self.qmt.get_kline(code, period="5m", count=50)
                    if raw_5m:
                        if isinstance(raw_5m, list) and len(raw_5m) >= 26:
                            c5 = [
                                float(b.get("close", 0))
                                for b in raw_5m
                                if b.get("close")
                            ]
                            if len(c5) >= 26:
                                m5 = calc_macd(c5)
                                factors["m5_macd_dif"] = m5["dif"]
                                factors["m5_macd_dea"] = m5["dea"]
                                factors["m5_macd_bar"] = m5["bar"]
                                factors["m5_rsi6"] = calc_rsi(c5, 6)
                                m5_ma20 = (
                                    sum(c5[-20:]) / 20 if len(c5) >= 20 else c5[-1]
                                )
                                factors["m5_vs_ma20"] = (
                                    (c5[-1] - m5_ma20) / m5_ma20 * 100
                                    if m5_ma20 > 0
                                    else 0
                                )
                except Exception:
                    pass

        except Exception:
            pass

        self._daily_factor_cache[code] = factors
        return factors

    def _get_order_book_imbalance(self, code: str, price: float) -> tuple[float, str]:
        """五档盘口买卖力量对比。返回 (bid_ratio, reason)。

        bid_ratio = 买盘总量 / (买盘总量 + 卖盘总量)，>0.5 买方占优。
        """
        if not self.qmt:
            return 0.5, ""
        try:
            detail = self.qmt.get_quote_detail(code)
            if not detail:
                return 0.5, ""

            ask_vols = detail.get("askVol", [])
            bid_vols = detail.get("bidVol", [])
            if not ask_vols or not bid_vols:
                return 0.5, ""

            total_bid = sum(float(v) for v in bid_vols[:5] if v)
            total_ask = sum(float(v) for v in ask_vols[:5] if v)
            total = total_bid + total_ask
            if total <= 0:
                return 0.5, ""

            ratio = total_bid / total

            if ratio >= 0.7:
                return ratio, "买盘强劲"
            elif ratio >= 0.55:
                return ratio, "买盘略强"
            elif ratio <= 0.3:
                return ratio, "卖盘沉重"
            elif ratio <= 0.45:
                return ratio, "卖盘略强"
            return ratio, "买卖均衡"
        except Exception:
            return 0.5, ""

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

    def _evaluate_buy_decision(
        self, code: str, price: float, buy_min: float, buy_max: float
    ) -> tuple[bool, str, float]:
        """多维买入决策评估。返回 (allowed, reason, size_multiplier)。

        不只看买入区，综合板块、均线、布林带、是否接飞刀等因素。
        """
        reject_reasons = []
        warn_reasons = []
        size_mul = 1.0

        # 1. 板块趋势（行业 + 概念）
        trend = self._get_sector_trend(code)
        if "持续走弱" in trend:
            reject_reasons.append(f"板块持续走弱，不买入{trend}")
            size_mul = 0.0
        elif "走弱" in trend:
            warn_reasons.append(f"板块偏弱{trend}")
            size_mul *= 0.5
        elif "持续走强" in trend:
            size_mul = min(1.0, size_mul * 1.2)

        # 1b. 概念板块趋势（叠加判断）
        concept_score, concept_reason = self._get_concept_trend_score(code)
        if concept_score <= -2:
            reject_reasons.append(f"多数概念板块走弱{concept_reason}")
            size_mul = 0.0
        elif concept_score < 0:
            warn_reasons.append(f"概念板块偏弱{concept_reason}")
            size_mul *= 0.6

        # 2. 买入区位置
        zone_range = buy_max - buy_min if buy_max > buy_min else 1
        zone_pos = (price - buy_min) / zone_range
        if zone_pos >= 0.85:
            reject_reasons.append(f"买入区顶部({zone_pos:.0%})，不追高")
        elif zone_pos >= 0.65:
            warn_reasons.append(f"买入区偏上({zone_pos:.0%})")
            size_mul *= 0.7

        # 3. 均线 & 布林带
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_upper, bb_mid, bb_lower, bb_pct_b, ma5, ma10, ma20
                   FROM stock_indicators WHERE stock_code=? AND bb_mid > 0
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()

            if row:
                bb_upper, bb_mid, bb_lower, pct_b, ma5, ma10, ma20 = row

                # 布林 %B：极高=超买有回调风险
                if pct_b is not None and pct_b >= 90:
                    reject_reasons.append(f"布林带超买(%B={pct_b:.0f})，回调风险高")
                elif pct_b is not None and pct_b >= 75:
                    warn_reasons.append(f"布林带偏上(%B={pct_b:.0f})")
                    size_mul *= 0.8
                elif pct_b is not None and pct_b <= 15:
                    # 超卖区域，可能是反弹机会
                    pass  # 不拒绝，下轨附近是好的买点

                # 均线：判断是否接飞刀（价格在所有均线之下且均线空头排列）
                if ma5 and ma10 and ma20 and ma5 > 0 and ma10 > 0 and ma20 > 0:
                    below_all = price < ma5 and price < ma10 and price < ma20
                    bearish_alignment = ma5 < ma10 < ma20  # 空头排列
                    if below_all and bearish_alignment:
                        reject_reasons.append("均线空头排列+价格破位，疑似接飞刀")
                    elif below_all:
                        warn_reasons.append("价格低于所有均线，趋势偏弱")
                        size_mul *= 0.7
                    elif price > ma5 and ma5 > ma10:
                        # 多头排列+站上MA5，顺势
                        pass

                # 回踩关键支撑 → 加分项
                near_support = False
                if bb_lower and abs(price - bb_lower) / bb_lower < 0.03:
                    near_support = True
                if ma20 and abs(price - ma20) / ma20 < 0.03:
                    near_support = True
                if near_support and not reject_reasons:
                    # 在支撑位附近买入，安全性较高
                    size_mul = min(1.0, size_mul * 1.2)
        except Exception:
            pass

        # 4. 日内分钟级指标（RSI/MACD/KDJ/价格vs均线）
        intra = self._get_intraday_indicators(code)
        if intra["available"]:
            # RSI
            r6, r12 = intra["rsi6"], intra["rsi12"]
            if r6 >= 85:
                reject_reasons.append(f"日内RSI6极度超买({r6:.0f})，追高风险极大")
            elif r6 >= 75:
                warn_reasons.append(f"日内RSI6超买({r6:.0f})")
                size_mul *= 0.7
            elif r6 <= 20:
                # 深跌超卖，可能是反弹前好买点
                size_mul = min(1.0, size_mul * 1.1)

            # MACD — 严格条件优先于宽松条件
            if intra["macd_direction"] == "bearish" and intra["macd_bar"] < -0.5:
                reject_reasons.append("日内MACD强烈空头，下跌动能未衰竭")
            elif intra["macd_direction"] == "bearish" and intra["macd_bar"] < -0.1:
                warn_reasons.append(f"日内MACD空头(bar={intra['macd_bar']:.2f})")
                size_mul *= 0.8
            elif intra["macd_direction"] == "bullish" and intra["macd_bar"] > 0.2:
                size_mul = min(1.0, size_mul * 1.1)

            # KDJ
            j = intra["kdj_j"]
            k, d = intra["kdj_k"], intra["kdj_d"]
            if j > 100:
                reject_reasons.append(f"日内KDJ极度超买(J={j:.0f})")
            elif j > 85:
                warn_reasons.append(f"日内KDJ超买(J={j:.0f})")
                size_mul *= 0.7
            elif j < 0:
                # 深跌超卖
                size_mul = min(1.0, size_mul * 1.1)

            # KDJ 死叉确认
            if k < d and j < 50:
                warn_reasons.append("日内KDJ死叉")
                size_mul *= 0.8

            # 价格 vs 日内MA5
            vs_ma5 = intra["price_vs_ma5"]
            if vs_ma5 < -3:
                reject_reasons.append(
                    f"价格远离日内MA5({vs_ma5:+.1f}%)，短期急跌接飞刀"
                )
            elif vs_ma5 < -1.5:
                warn_reasons.append(f"价格低于日内MA5({vs_ma5:+.1f}%)")

        # 5. 五档盘口买卖力量
        ob_ratio, ob_reason = self._get_order_book_imbalance(code, price)
        if ob_ratio <= 0.3 and ob_reason:
            reject_reasons.append(f"盘口卖盘沉重(买盘{ob_ratio:.0%})")
        elif ob_ratio <= 0.42 and ob_reason:
            warn_reasons.append(f"盘口卖压偏大(买盘{ob_ratio:.0%})")
            size_mul *= 0.85
        elif ob_ratio >= 0.7:
            size_mul = min(1.0, size_mul * 1.1)

        # 6. 大单流向
        big_ratio, big_reason = self._get_big_order_direction(code)
        if big_ratio <= 0.35 and big_reason:
            reject_reasons.append(big_reason)
        elif big_ratio <= 0.45 and big_reason:
            warn_reasons.append(big_reason)
            size_mul *= 0.8
        elif big_ratio >= 0.65 and big_reason:
            size_mul = min(1.0, size_mul * 1.1)

        # 7. 涨跌停空间
        inst = self._get_instrument_info(code)
        up_stop = inst.get("up_stop", 0)
        down_stop = inst.get("down_stop", 0)
        if up_stop > 0 and price > 0:
            room_pct = (up_stop - price) / price * 100
            if room_pct < 2:
                reject_reasons.append(f"距涨停仅{room_pct:.1f}%，追板风险极高")
            elif room_pct < 4:
                warn_reasons.append(f"距涨停{room_pct:.1f}%，上行空间有限")
                size_mul *= 0.8
        if down_stop > 0 and price > 0:
            risk_pct = (price - down_stop) / price * 100
            if risk_pct > 15:
                reject_reasons.append(f"距跌停{risk_pct:.0f}%，下方风险空间过大")

        # 8. 昨日趋势背景 + 今日实时因子
        df = self._get_context_factors(code, price)
        if df["available"]:
            # 昨日主力资金（有延续性，作为背景参考）
            mf_ratio = df["yesterday_mf_ratio"]
            if mf_ratio > 5:
                size_mul = min(1.0, size_mul * 1.1)
            elif mf_ratio < -5:
                reject_reasons.append(f"昨日主力大幅流出({mf_ratio:.1f}%)，今日承压")
            elif mf_ratio < -2:
                warn_reasons.append(f"昨日主力流出({mf_ratio:.1f}%)")
                size_mul *= 0.85

            # 昨日 MA5 斜率：趋势方向
            ma5_ang = df["ma5_angle"]
            if ma5_ang < -2:
                reject_reasons.append(f"MA5加速下行(角{ma5_ang:.1f})，趋势向空")
            elif ma5_ang < 0:
                warn_reasons.append(f"MA5拐头向下(角{ma5_ang:.1f})")
                size_mul *= 0.85

            # 今日日内位置（QMT实时）
            day_pos = df.get("day_position")
            if day_pos is not None:
                if day_pos < 0.15 and ma5_ang > 1:
                    size_mul = min(1.0, size_mul * 1.1)  # 日低+趋势向上
                elif day_pos > 0.9:
                    warn_reasons.append("价格接近日内高点")
                    size_mul *= 0.85

            # 昨日 MACD：中期趋势背景
            dm_dif = df["daily_macd_dif"]
            dm_dea = df["daily_macd_dea"]
            dm_bar = df["daily_macd_bar"]
            if dm_dif < dm_dea and dm_bar < -0.3:
                warn_reasons.append("日线MACD空头")
                size_mul *= 0.85
            elif dm_dif > dm_dea and dm_bar > 0.2:
                size_mul = min(1.0, size_mul * 1.05)

            # 昨日 KDJ
            dk_j = df["daily_kdj_j"]
            if dk_j > 100:
                reject_reasons.append(f"日线KDJ极度超买(J={dk_j:.0f})")
            elif dk_j > 85:
                warn_reasons.append(f"日线KDJ超买(J={dk_j:.0f})")
                size_mul *= 0.8
            elif dk_j < 0:
                size_mul = min(1.0, size_mul * 1.1)

            # 昨日 BBI 多空线
            bbi = df["bbi_daily"]
            if bbi > 0 and price < bbi * 0.95:
                warn_reasons.append("价格低于BBI多空线")
                size_mul *= 0.85

            # 今日 5分钟周期 MACD（实时）
            if "m5_macd_dif" in df:
                m5_dif = df["m5_macd_dif"]
                m5_dea = df["m5_macd_dea"]
                m5_bar = df["m5_macd_bar"]
                if m5_dif < m5_dea and m5_bar < -0.2:
                    warn_reasons.append("5min MACD空头")
                    size_mul *= 0.85
                elif m5_dif > m5_dea and m5_bar > 0.1:
                    size_mul = min(1.0, size_mul * 1.05)

            # 昨日布林带宽：波动率背景
            bb_w = df["bb_width"]
            if bb_w > 40:
                warn_reasons.append(f"布林带宽({bb_w:.0f})，波动剧烈")
                size_mul *= 0.8

        # 9. 汇总
        if reject_reasons:
            return False, "; ".join(reject_reasons), 0
        if warn_reasons:
            return True, "; ".join(warn_reasons), max(0.5, size_mul)
        return True, "条件符合", size_mul

    def _evaluate_below_zone(
        self, code: str, price: float, buy_min: float, buy_max: float
    ) -> tuple[str, str, float | None]:
        """价格低于买入区时的综合判断。返回 (action, reason, size_mul|None)。

        action: "opportunity" — 回调买入机会，可以下单
                "watching"   — 继续观察，不下单但保留信号
                "abandon"    — 破位放弃，标记信号过期
        """
        zone_range = buy_max - buy_min if buy_max > buy_min else 1
        below_pct = (buy_min - price) / buy_min * 100  # 低于买入区下沿的百分比

        score = 0  # 正=偏向机会，负=偏向放弃

        # ━━━ 1. 偏离幅度 ━━━
        if below_pct <= 2:
            score += 2  # 微幅偏离，接近买入区
        elif below_pct <= 4:
            score += 0  # 中等偏离
        elif below_pct <= 7:
            score -= 2  # 较大偏离
        else:
            score -= 5  # 大幅偏离，买入区很可能已失效

        # ━━━ 2. 距离关键支撑 ━━━
        try:
            conn = sqlite3.connect(self.db_path)
            row = conn.execute(
                """SELECT bb_lower, bb_mid, ma20, ma60
                   FROM stock_indicators WHERE stock_code=? AND bb_mid > 0
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()

            if row:
                bb_lower, bb_mid, ma20, ma60 = row
                near_support = False
                support_name = ""

                if bb_lower and abs(price - bb_lower) / bb_lower < 0.02:
                    near_support = True
                    support_name = "布林下轨"
                elif ma20 and abs(price - ma20) / ma20 < 0.02:
                    near_support = True
                    support_name = "MA20"
                elif ma60 and abs(price - ma60) / ma60 < 0.03:
                    near_support = True
                    support_name = "MA60"

                if near_support:
                    score += 4  # 在关键支撑附近止跌，反弹概率高
                elif ma20 and price > ma20:
                    score += 1  # 还在 MA20 上方，趋势没坏
                elif ma60 and price < ma60:
                    score -= 3  # 跌破 MA60，中期趋势转弱
        except Exception:
            pass

        # ━━━ 3. 日内指标：是否出现止跌信号 ━━━
        intra = self._get_intraday_indicators(code)
        if intra["available"]:
            r6 = intra["rsi6"]
            j = intra["kdj_j"]
            k, d = intra["kdj_k"], intra["kdj_d"]

            if r6 <= 25:
                score += 3  # RSI 超卖，卖方力量衰竭
            elif r6 <= 35:
                score += 1
            elif r6 >= 70:
                score -= 1  # 还没超卖就低于买入区？可能刚开始跌

            if j < 0:
                score += 2  # KDJ 极度超卖
            if k > d and j < 20:
                score += 2  # KDJ 低位金叉，反弹启动

            if intra["macd_direction"] == "bullish":
                score += 1
            elif intra["macd_direction"] == "bearish" and intra["macd_bar"] < -0.3:
                score -= 2

            vs_ma5 = intra["price_vs_ma5"]
            if vs_ma5 < -5:
                score -= 3  # 急跌中，不要接飞刀
            elif vs_ma5 < -2:
                score -= 1

        # ━━━ 4. 大单方向：有没有主力在接 ━━━
        big_ratio, big_reason = self._get_big_order_direction(code)
        if big_reason:
            if big_ratio >= 0.6:
                score += 3  # 大单在买，有资金承接
            elif big_ratio <= 0.4:
                score -= 3  # 大单在卖，主力出货

        # ━━━ 5. 盘口支撑 ━━━
        ob_ratio, ob_reason = self._get_order_book_imbalance(code, price)
        if ob_ratio >= 0.65:
            score += 2  # 买盘厚实，有支撑
        elif ob_ratio <= 0.35:
            score -= 2  # 卖盘压力大

        # ━━━ 6. 板块趋势 ━━━
        trend = self._get_sector_trend(code)
        if "持续走弱" in trend:
            return "watching", f"板块持续走弱，不追回调{trend}", None
        if "持续走强" in trend:
            score += 3
        elif "走强" in trend:
            score += 1
        elif "走弱" in trend:
            score -= 1

        # 6b. 概念板块趋势
        concept_score, concept_reason = self._get_concept_trend_score(code)
        if concept_score <= -2:
            return "watching", f"多数概念板块走弱{concept_reason}，不追回调", None
        score += concept_score

        # ━━━ 7. 成交量验证（从 tick 数据判断量能变化）━━━
        if self.qmt:
            try:
                ticks = self.qmt.get_ticks(code)
                if ticks and len(ticks) >= 40:
                    # 取最近 20 笔和之前 20 笔的成交量对比
                    half = len(ticks) // 2
                    recent = ticks[-half:]
                    earlier = ticks[:half]
                    recent_vol = sum(
                        (
                            float(recent[i].get("amount", 0))
                            - float(recent[i - 1].get("amount", 0))
                        )
                        for i in range(1, len(recent))
                        if float(recent[i].get("amount", 0))
                        > float(recent[i - 1].get("amount", 0))
                    )
                    earlier_vol = sum(
                        (
                            float(earlier[i].get("amount", 0))
                            - float(earlier[i - 1].get("amount", 0))
                        )
                        for i in range(1, len(earlier))
                        if float(earlier[i].get("amount", 0))
                        > float(earlier[i - 1].get("amount", 0))
                    )
                    if earlier_vol > 0 and recent_vol > 0:
                        vol_ratio = recent_vol / earlier_vol
                        if vol_ratio < 0.5:
                            score += 2  # 缩量下跌，正常回调
                        elif vol_ratio > 2:
                            score -= 2  # 放量下跌，恐慌抛售
            except Exception:
                pass

        # ━━━ 8. 昨日趋势背景 + 今日实时因子 ━━━
        df = self._get_context_factors(code, price)
        if df["available"]:
            # 昨日主力资金：流入却在跌=洗盘，流出+跌=真破位
            mf_ratio = df["yesterday_mf_ratio"]
            if mf_ratio > 3:
                score += 3  # 昨日主力大幅流入，今天下跌可能是洗盘
            elif mf_ratio < -3:
                score -= 3  # 昨日主力大幅流出+今天破位=真跌

            ma5_ang = df["ma5_angle"]
            if ma5_ang < -3:
                score -= 3  # MA5 加速下行
            elif ma5_ang > 1:
                score += 1  # MA5 仍向上，短期回调

            day_pos = df.get("day_position")
            if day_pos is not None and day_pos < 0.1:
                score += 1  # 接近日内低点

            dm_dif = df["daily_macd_dif"]
            dm_dea = df["daily_macd_dea"]
            if dm_dif > dm_dea:
                score += 1  # 日线 MACD 多头，中期趋势未坏
            elif dm_dif < dm_dea and df["daily_macd_bar"] < -0.5:
                score -= 2  # 日线 MACD 强空头

            dk_j = df["daily_kdj_j"]
            if dk_j < 0:
                score += 2  # 日线 KDJ 极度超卖，反弹概率高
            elif dk_j > 90:
                score -= 1  # 日线 KDJ 还在高位，下跌可能刚开始

            bbi = df["bbi_daily"]
            if bbi > 0 and price < bbi * 0.9:
                score -= 2  # 远低于 BBI 多空线

            if "m5_macd_dif" in df:
                m5_dif = df["m5_macd_dif"]
                m5_dea = df["m5_macd_dea"]
                if m5_dif > m5_dea:
                    score += 1
                elif m5_dif < m5_dea:
                    score -= 1

        # ━━━ 汇总判断 ━━━
        if score >= 6:
            mul = min(1.0, 0.5 + score * 0.05)  # 最高 1.0
            return "opportunity", f"回调至支撑区(评分{score})，择机买入", mul
        elif score >= 3:
            mul = 0.5 + score * 0.05
            return (
                "opportunity",
                f"回调偏深但止跌迹象(评分{score})，小仓位试探",
                min(0.7, mul),
            )
        elif score >= 0:
            return "watching", f"下方偏离未企稳(评分{score})，继续观察", None
        elif score >= -4:
            return "watching", f"偏弱(评分{score})，等待更明确信号", None
        else:
            return "abandon", f"破位下行(评分{score})，买入区已失效", None

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

        # 如果价格已经在新买入区内 → 不告警，直接使用修正区间
        in_new_zone = new_min <= price <= new_max
        below_new_zone = price < new_min

        # 修正幅度 < 2% → 不告警但静默使用
        shift_pct = (buy_min - new_min) / buy_min * 100
        if shift_pct < 2.0:
            return new_min, new_max, ""

        # 构建告警理由
        parts = [
            f"原区间 {buy_min:.2f}~{buy_max:.2f} → 修正 {new_min:.2f}~{new_max:.2f}"
        ]
        parts.append(f"🔮 {adj['reason']}")

        if in_new_zone:
            parts.append("→ 价格已进入修正区间")
        elif below_new_zone:
            below = (new_min - price) / price * 100
            parts.append(f"→ 价格低于修正区间 {below:.1f}%")

        reason = " | ".join(parts)
        return new_min, new_max, reason

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

    def _check_buy_candidates(self, candidates: list[dict], regime):
        """统一买入候选处理：信号 + 复盘推荐共用管线。

        每个 candidate 字段:
            code, name, price, buy_min, buy_max, sl, tp, score, trend,
            source: "signal" | "review" (决定告警前缀 + 状态管理)
            alert_key: 去重 key（signal 用 sid, review 用 code）
            signal_id: int|None（DB 信号 ID，用于 expire/update）

        regime: MarketRegime 对象（非旧版 bool），逐票决策时读取 allow_buy/position_mult/entry_rule
        """
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

        for c in candidates:
            source = c["source"]
            if source == "signal":
                alert_state = self._signal_alert_state
                tag = ""

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

            code = c["code"]
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

            # —— 动态买入区修正：三层联动，市场偏空时下调买入区 ——
            adj_buy_min, adj_buy_max, adj_reason = self._calc_dynamic_buy_zone(
                code, price, buy_min, buy_max, trend
            )
            if adj_reason:
                # 节流告警
                dyn_key = f"dyn_buy_zone:{c['alert_key']}"
                last_dyn = alert_state.get(dyn_key, 0)
                if self._scan_count - last_dyn >= 20:
                    alert_state[dyn_key] = self._scan_count
                    self._alert(
                        f"🔽 {tag}买入区修正 — {code} {name}\n"
                        f"   现价: {price:.2f}  {adj_reason}"
                    )
                # 使用修正后的买入区做判断
                buy_min, buy_max = adj_buy_min, adj_buy_max

            in_zone = buy_min <= price <= buy_max
            below_zone = price < buy_min
            above_zone = price > buy_max

            # ━━━ 高于买入区 — 预测性接近 + 板块走强提醒 ━━━
            if above_zone:
                above_pct = (price - buy_max) / buy_max * 100

                # 情景引擎：市场预测回调 + 距买入区 < 3% → 提前预告准备入场
                if above_pct <= 3.0:
                    outlook = getattr(self, "_scenario_prev_outlook", None)
                    if (
                        outlook
                        and outlook.primary.direction == "bearish"
                        and outlook.urgency in ("critical", "act")
                    ):
                        approach_key = f"approach:{c['alert_key']}"
                        last_scan = alert_state.get(approach_key, 0)
                        if self._scan_count - last_scan >= 15:
                            alert_state[approach_key] = self._scan_count
                            self._alert(
                                f"🔔 {tag}买入区接近 — {code} {name}\n"
                                f"   现价: {price:.2f}  距区间: {above_pct:.1f}%  "
                                f"区间: {buy_min:.2f}~{buy_max:.2f}\n"
                                f"   止损: {sl:.2f}  止盈: {tp:.2f}{trend}\n"
                                f"   🔮 {outlook.primary.label} ({outlook.primary.probability:.0%})"
                                f"  → 市场预测回调，准备入场"
                            )
                        continue

                if "持续走强" in trend:
                    alert_state[c["alert_key"]] = (price, True)
                    above_pct = (price - buy_max) / buy_max * 100
                    self._alert(
                        f"📈 {tag}追高提醒 — {code} {name}\n"
                        f"   现价: {price:.2f}  高于区间上限: {buy_max:.2f}  超出: {above_pct:+.1f}%\n"
                        f"   止损: {sl:.2f}  止盈: {tp:.2f}{trend}\n"
                        f"   → 不自动追高，建议判断是否上调买入区"
                    )
                continue

            if not in_zone and not below_zone:
                continue

            # ━━━ 低于买入区 ━━━
            if below_zone and not in_zone:
                below_action, below_reason, below_mul = self._evaluate_below_zone(
                    code, price, buy_min, buy_max
                )

                if below_action == "abandon":
                    alert_state[c["alert_key"]] = (price, True)
                    self._alert(
                        f"❌ {tag}信号放弃 — {code} {name}\n"
                        f"   现价: {price:.2f}  低于区间: {buy_min:.2f}~{buy_max:.2f}{trend}\n"
                        f"   → {below_reason}"
                    )
                    if on_abandon:
                        on_abandon()
                    continue

                elif below_action == "watching":
                    continue

                else:
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
                        f"   📐 {below_reason}  模拟盘减仓至 {below_mul:.0%}{market_note}"
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

            # ━━━ 买入区内 ━━━
            prev_state = alert_state.get(c["alert_key"])
            if prev_state is not None and prev_state[1]:
                continue

            if not market_ok:
                alert_state[c["alert_key"]] = (price, True)
                market_advice = regime_alert_msg or self._get_market_risk_advice()
                self._alert(
                    f"⏸️ {tag}大盘风险 — {code} {name}\n"
                    f"   现价: {price:.2f}  买入区: {buy_min:.2f}~{buy_max:.2f}{trend}\n"
                    f"   → {market_advice}"
                )
                continue

            # ── entry_rule 过滤（大盘环境决定入场策略） ──
            zone_pos = (
                (price - buy_min) / (buy_max - buy_min) if buy_max > buy_min else 0.5
            )
            entry_skip_reason = ""
            if entry_rule == "next_day":
                entry_skip_reason = "尾盘拉升/次日再看，今日不追"
            elif entry_rule == "confirm":
                if zone_pos > 0.5:
                    entry_skip_reason = (
                        f"需确认信号(zone_pos={zone_pos:.0%})，等回调到区间下半部"
                    )
            elif entry_rule == "pullback":
                if zone_pos > 0.4:
                    entry_skip_reason = f"等回调买入(zone_pos={zone_pos:.0%})，暂不追高"
            elif entry_rule == "range_boundary":
                if zone_pos > 0.25:
                    entry_skip_reason = (
                        f"宽幅震荡(zone_pos={zone_pos:.0%})，等区间下沿再入场"
                    )

            if entry_skip_reason:
                alert_state[c["alert_key"]] = (price, True)
                self._alert(
                    f"⏸️ {tag}暂缓买入 — {code} {name}\n"
                    f"   现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}{trend}\n"
                    f"   → {entry_skip_reason}"
                )
                continue

            if self._is_limit_up(code, price):
                alert_state[c["alert_key"]] = (price, True)
                self._alert(
                    f"🚫 涨停无法买入 — {code} {name}\n"
                    f"   涨停价: {self._limit_cache.get(code, {}).get('limit_up', 0):.2f}{trend}\n"
                    f"   → 封涨停板，不建议排板"
                )
                continue

            context = self._analyze_buy_context(code, price, buy_min, buy_max)
            alert_state[c["alert_key"]] = (price, True)

            decision_allowed, decision_reason, size_mul = self._evaluate_buy_decision(
                code, price, buy_min, buy_max
            )
            decision_line = ""
            if not decision_allowed:
                decision_line = f"\n   ⛔ 模拟盘跳过: {decision_reason}"
            elif size_mul < 1.0:
                decision_line = f"\n   ⚠️ 模拟盘减仓至 {size_mul:.0%}: {decision_reason}"

            self._alert(
                f"🔴 {tag}买入信号 — {code} {name}\n"
                f"   现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}  止损: {sl:.2f}  止盈: {tp:.2f}\n"
                f"   板块:{trend}{decision_line}\n"
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

    def _check_signals(self, prices: dict[str, float], regime):
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
            sl = s.get("stop_loss", 0) or 0
            tp = s.get("take_profit", 0) or 0
            if sl <= 0 or tp <= 0:
                logger.warning(
                    f"  信号 {code} {name} 缺少止损/止盈 (sl={sl}, tp={tp})，跳过"
                )
                continue

            name = s.get("stock_name", "")
            if not name or name == code:
                name = self._resolve_name(code)

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
            self._check_buy_candidates(candidates, regime)

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
        # 兼容旧版 bool market_ok
        if isinstance(regime, bool):
            if not regime:
                return
        if size_mul <= 0:
            return

        # 大盘 stop_mult 调整止损宽度
        stop_mult = (
            getattr(regime, "stop_mult", 1.0)
            if regime and not isinstance(regime, bool)
            else 1.0
        )
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
        if max_amount < 5000:
            return

        target_pct = (
            max_amount / self.paper_account.total_value
            if self.paper_account.total_value > 0
            else 0.10
        )
        sector = (
            self._industry_cache.get(code, "")
            if hasattr(self, "_industry_cache")
            else ""
        )
        risk_result = self.risk_engine.can_open(
            code,
            target_pct,
            sector_code=sector,
            portfolio=self.paper_account,
        )
        if not risk_result.allowed:
            return

        # 计算股数（盯盘决策，模拟盘只管执行）
        capital = min(
            max_amount, self.paper_account.total_value * settings.DEFAULT_POSITION_PCT
        )
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
            # 写入 _pos_meta（盯盘决策数据）
            self._pos_meta[code] = {
                "sl": sl,
                "tp": tp,
                "trailing_stop": 0.05,
                "highest_price": price,
                "sector": sector,
                "score": score,
                "signal_id": signal_id,
            }
            existing = self._bought_watch.get(code, {})
            self._bought_watch[code] = {
                "entry_price": price,
                "last_alert_scan": self._scan_count,
                "status": "watching",
                "alert_count": 0,
                "max_profit_pct": existing.get("max_profit_pct", 0),
            }
            self._invalidate_watch_codes_cache()

    def _check_review_picks(self, prices: dict[str, float], regime):
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
            self._check_buy_candidates(candidates, regime)

    # ------------------------------------------------------------------
    # 结构化买入区间加载（优先于 MA 动态计算）
    # ------------------------------------------------------------------

    def _load_review_signal_zones(self) -> dict[str, tuple[float, float, float, float]]:
        """从 trade_signals 加载 REVIEW 信号的结构化买入区间。
        返回 {code: (buy_min, buy_max, sl, tp)}。
        """
        try:
            rows = (
                sqlite3.connect(self.db_path)
                .execute(
                    """SELECT stock_code, buy_zone_min, buy_zone_max, stop_loss, take_profit
                   FROM trade_signals
                   WHERE trade_date=? AND signal_source='REVIEW' AND status='pending' AND account='paper'""",
                    (self._trade_date,),
                )
                .fetchall()
            )
            return {
                r[0]: (r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0)
                for r in rows
                if r[1] and r[2]
            }
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 开盘决策汇总（集合竞价后第一轮，替代之前的两个开盘参考）
    # ------------------------------------------------------------------

    def _get_review_monitor(self):
        if self._review_monitor is None:
            try:
                from trade.monitor.review_picks import ReviewPickMonitor

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
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                """SELECT stock_code, stock_name, stop_loss, target_price, abandon_condition
                   FROM stock_tracker
                   WHERE push_date = (
                       SELECT MAX(push_date) FROM stock_tracker WHERE source='复盘'
                   )"""
            ).fetchall()
            conn.close()
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

    # ======================== 第二层：板块热度 ========================
