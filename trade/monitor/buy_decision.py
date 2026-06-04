"""买入决策管线 — 信号/复盘候选 → 多维评分 → 模拟盘执行。

Mixin 方式混入 Watcher，所有 self.xxx 直接访问 Watcher 属性。
"""

import logging
import sqlite3
import time

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

        # 市场宽度修正
        breadth = getattr(self, "_market_breadth", {})
        up, down = breadth.get("up", 0), breadth.get("down", 0)
        if up + down > 0:
            down_ratio = down / (up + down)
            if down_ratio > 0.7:
                base = max(base * 0.3, 5000)
                reason += " 普跌" if reason else "普跌"
            elif down_ratio > 0.6:
                base = max(base * 0.5, 5000)
                reason += " 偏弱" if reason else "偏弱"

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

        # 早盘 AI 板块倾向修正（叠加在实时板块趋势之上）
        industry = self._industry_cache.get(code, "")
        if industry and industry in self._morning_sector_bias:
            b = self._morning_sector_bias[industry]
            b_mult = b.get("size_mult", 1.0)
            if b["bias"] == "focus":
                base = min(int(base * b_mult), 16000)
                reason += (
                    f" AI聚焦({b_mult:.1f}x)" if reason else f"AI聚焦({b_mult:.1f}x)"
                )
            elif b["bias"] == "avoid":
                base = max(int(base * b_mult), 3000)
                reason += (
                    f" AI回避({b_mult:.1f}x)" if reason else f"AI回避({b_mult:.1f}x)"
                )

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

            intra_parts = [f"RSI6={r6:.0f} RSI12={r12:.0f}"]
            intra_parts.append(f"MACD={macd_dir}(bar={intra['macd_bar']:.2f})")
            intra_parts.append(
                f"KDJ K={intra['kdj_k']:.1f} D={intra['kdj_d']:.1f} J={j:.1f}"
            )
            if vs_ma5 != 0:
                side = "上" if vs_ma5 > 0 else "下"
                intra_parts.append(f"价在MA5{side}{abs(vs_ma5):.1f}%")
            parts.append(f"日内: {' | '.join(intra_parts)}")

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
        sector_reject_pct = getattr(settings, "SECTOR_REJECT_PCT", -1.0)
        sector_chg = self._get_sector_change(code)
        trend = self._get_sector_trend(code)
        decline = self._get_sector_decline(code)
        recovery_risk = self._get_sector_recovery_risk(code)
        if not trend or "数据不足" in trend or "数据积累中" in trend:
            reject_reasons.append(f"板块数据不足，开盘初期暂不买入{trend}")
            size_mul = 0.0
        elif sector_chg is not None and sector_chg <= sector_reject_pct:
            reject_reasons.append(f"板块跌幅 {sector_chg:+.1f}%，拒绝买入")
            size_mul = 0.0
        elif decline is not None and decline >= 1.5:
            reject_reasons.append(f"板块冲高回落 {decline:+.1f}%，拒绝追入")
            size_mul = 0.0
        elif recovery_risk is not None:
            reject_reasons.append(
                f"板块从日内低点反弹 {recovery_risk:+.1f}%，疑似死猫跳不追"
            )
            size_mul = 0.0
        elif "持续走弱" in trend:
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

        # 板块强度标记（后续技术指标评估放宽阈值）
        sector_strong = trend and ("持续走强" in trend or "走强" in trend)
        sector_very_strong = sector_strong and (sector_chg or 0) > 1.5

        # 早盘 AI 板块倾向：focus → 强制 sector_very_strong；avoid → 追加判断
        stock_industry = self._industry_cache.get(code, "")
        if stock_industry and stock_industry in self._morning_sector_bias:
            bias = self._morning_sector_bias[stock_industry]
            if bias["bias"] == "focus":
                sector_very_strong = True  # 强制放宽技术指标阈值
                size_mul = min(1.0, size_mul * bias.get("size_mult", 1.0))
            elif bias["bias"] == "avoid":
                if "持续走弱" in trend:
                    reject_reasons.append(f"AI回避+板块持续走弱: {stock_industry}")
                    size_mul = 0.0
                else:
                    size_mul *= bias.get("size_mult", 0.5)
                    warn_reasons.append(f"AI建议回避{stock_industry}")

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

                # 布林 %B：极高=超买有回调风险。强板块放宽
                b_reject = 95 if sector_very_strong else 90
                b_warn = 85 if sector_very_strong else 75
                if pct_b is not None and pct_b >= b_reject:
                    reject_reasons.append(f"布林带超买(%B={pct_b:.0f})，回调风险高")
                elif pct_b is not None and pct_b >= b_warn:
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

        # 3b. 日线级指标（KDJ/RSI — 从 stock_indicators 读昨日收盘数据）
        try:
            conn = sqlite3.connect(self.db_path)
            daily = conn.execute(
                """SELECT rsi6, rsi12, kdj_k, kdj_d, kdj_j, ma5, ma10, ma20
                   FROM stock_indicators WHERE stock_code=? AND ma5 > 0
                   ORDER BY trade_date DESC LIMIT 1""",
                (code,),
            ).fetchone()
            conn.close()
            if daily:
                d_rsi6, d_rsi12, d_k, d_d, d_j, d_ma5, d_ma10, d_ma20 = daily
                if d_j is not None and d_j > 100:
                    reject_reasons.append(f"日线KDJ极度超买(J={d_j:.0f})")
                elif d_j is not None and d_j > 85:
                    warn_reasons.append(f"日线KDJ超买(J={d_j:.0f})")
                    size_mul *= 0.6
                if d_rsi6 is not None and d_rsi6 >= 80:
                    reject_reasons.append(f"日线RSI6超买({d_rsi6:.0f})，不宜追高")
                elif d_rsi6 is not None and d_rsi6 >= 70:
                    warn_reasons.append(f"日线RSI6偏高({d_rsi6:.0f})")
                    size_mul *= 0.7
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
                size_mul *= 0.7 if not sector_strong else 0.85
            elif r6 <= 20:
                size_mul = min(1.0, size_mul * 1.1)

            # MACD — 强板块放宽阈值
            macd_reject_bar = -0.8 if sector_very_strong else -0.5
            macd_warn_bar = -0.3 if sector_very_strong else -0.1
            if (
                intra["macd_direction"] == "bearish"
                and intra["macd_bar"] < macd_reject_bar
            ):
                reject_reasons.append("日内MACD强烈空头，下跌动能未衰竭")
            elif (
                intra["macd_direction"] == "bearish"
                and intra["macd_bar"] < macd_warn_bar
            ):
                warn_reasons.append(f"日内MACD空头(bar={intra['macd_bar']:.2f})")
                size_mul *= 0.8 if not sector_strong else 0.9
            elif intra["macd_direction"] == "bullish" and intra["macd_bar"] > 0.2:
                size_mul = min(1.0, size_mul * 1.1)

            # KDJ
            j = intra["kdj_j"]
            k, d = intra["kdj_k"], intra["kdj_d"]
            if j > 100:
                reject_reasons.append(f"日内KDJ极度超买(J={j:.0f})")
            elif j > 85:
                warn_reasons.append(f"日内KDJ超买(J={j:.0f})")
                size_mul *= 0.7 if not sector_strong else 0.85
            elif j < 0:
                size_mul = min(1.0, size_mul * 1.1)

            # KDJ 死叉 — J<0(深度超卖)时 K<D 是正常现象，不惩罚
            if k < d and j < 50 and j >= 0:
                warn_reasons.append("日内KDJ死叉")
                size_mul *= 0.85 if not sector_strong else 0.95

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

            # 昨日布林带宽：波动率背景。强板块中高波动正常
            bb_w = df["bb_width"]
            bb_warn = 70 if sector_very_strong else 40
            if bb_w > bb_warn:
                warn_reasons.append(f"布林带宽({bb_w:.0f})，波动剧烈")
                size_mul *= 0.85 if sector_strong else 0.8

        # 9. 价格走势：是否在止跌
        price_action, price_action_desc = self._get_recent_price_action(code)
        if price_action == "declining":
            reject_reasons.append(f"10分钟内{price_action_desc}，等待止跌再买")
        elif price_action == "reversing":
            size_mul = min(1.0, size_mul * 1.15)  # 确认反弹，放宽
        elif price_action == "stabilizing":
            pass  # 横盘不加不减，由其他维度决定

        # 10. 汇总
        if reject_reasons:
            return False, "; ".join(reject_reasons), 0
        if warn_reasons:
            return True, "; ".join(warn_reasons), max(0.5, size_mul)
        return True, "条件符合", size_mul

    def _get_sector_change(self, code: str) -> float | None:
        """返回股票所属行业的平均涨跌幅，数据不足返回 None。"""
        try:
            industry = getattr(self, "_industry_cache", {}).get(code, "")
            if not industry:
                return None
            stats = getattr(self, "_sector_stats", {}).get(industry)
            if not stats:
                return None
            return stats.get("change_pct")
        except Exception:
            return None

    def _get_sector_decline(self, code: str) -> float | None:
        """返回板块从近期高点回落的幅度（正数=回落多少），数据不足返回 None。

        用 trend_history 最近 5 个采样点，对比当前值与区间高点。
        """
        try:
            industry = getattr(self, "_industry_cache", {}).get(code, "")
            if not industry:
                return None
            stats = getattr(self, "_sector_stats", {}).get(industry)
            if not stats:
                return None
            history = stats.get("trend_history", [])
            if len(history) < 3:
                return None
            recent = history[-5:]  # 最近 5 个采样点
            peak = max(recent)
            current = recent[-1]
            decline = peak - current
            return round(decline, 2) if decline > 0 else None
        except Exception:
            return None

    def _get_sector_recovery_risk(self, code: str) -> float | None:
        """检测板块是否从日内深跌中反弹（死猫跳风险）。

        用完整 trend_history，如果板块从日内最低点反弹超过阈值，
        说明当前强势可能是假象。返回反弹幅度（正数=反弹多少），None=安全。
        """
        try:
            industry = getattr(self, "_industry_cache", {}).get(code, "")
            if not industry:
                return None
            stats = getattr(self, "_sector_stats", {}).get(industry)
            if not stats:
                return None
            history = stats.get("trend_history", [])
            if len(history) < 6:
                return None
            # 用所有历史数据，找日内最低点
            intra_low = min(history)
            current = history[-1]
            recovery = current - intra_low  # 反弹幅度
            # 反弹超过 2% 说明板块日内波动剧烈，当前强势不可靠
            if recovery > 2.0:
                return round(recovery, 2)
            return None
        except Exception:
            return None

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

        # 后5分钟最低价 < 前5分钟最低价 → 还在创新低
        if second_low < first_low * 0.995:
            drop_pct = (second_low - first_high) / first_high * 100
            return "declining", f"持续创新低({drop_pct:.1f}%)"

        # 后5分钟振幅 < 1% → 横盘止跌
        second_range = (
            (second_high - second_low) / second_low * 100 if second_low > 0 else 0
        )
        if second_range < 1.0:
            return "stabilizing", f"横盘止跌(振幅{second_range:.1f}%)"

        # 后5分钟均价 > 前5分钟均价 → 反弹
        if second_avg > first_avg * 1.005:
            return (
                "reversing",
                f"低点抬高+{((second_avg - first_avg) / first_avg * 100):.1f}%",
            )

        return "stabilizing", f"波动收敛(振幅{second_range:.1f}%)"

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
        sector_reject_pct = getattr(settings, "SECTOR_REJECT_PCT", -1.0)
        sector_chg = self._get_sector_change(code)
        decline = self._get_sector_decline(code)
        trend = self._get_sector_trend(code)
        if not trend or "数据不足" in trend or "数据积累中" in trend:
            return "watching", f"板块数据不足，开盘初期暂不买入{trend}", None
        if sector_chg is not None and sector_chg <= sector_reject_pct:
            return "watching", f"板块跌幅 {sector_chg:+.1f}%，拒绝买入", None
        if decline is not None and decline >= 1.5:
            return "watching", f"板块冲高回落 {decline:+.1f}%，拒绝追入", None
        # 日内深跌反弹风险：从日内最低点反弹超 2% → 当前强势不可靠
        recovery_risk = self._get_sector_recovery_risk(code)
        if recovery_risk is not None:
            return (
                "watching",
                f"板块从日内低点反弹 {recovery_risk:+.1f}%，疑似死猫跳不追",
                None,
            )
        if "持续走弱" in trend:
            return "watching", f"板块持续走弱，不买入{trend}", None
        if "走弱" in trend:
            score -= 3
        elif "持续走强" in trend:
            score += 3
        elif "走强" in trend:
            score += 1

        # 6b. 概念板块趋势
        concept_score, concept_reason = self._get_concept_trend_score(code)
        if concept_score <= -2:
            return "watching", f"多数概念板块走弱{concept_reason}，不买入", None
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

        # ━━━ 9. 价格走势：是否止跌企稳 ━━━
        price_action, price_action_desc = self._get_recent_price_action(code)
        if price_action == "declining":
            return "watching", f"10分钟内{price_action_desc}，等待止跌", None
        elif price_action == "reversing":
            score += 5  # 确认反弹，大幅加分
        elif price_action == "stabilizing":
            score += 3  # 横盘止跌，小幅加分
        # no_data 不加分不扣分

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

    def _check_buy_candidates(self, candidates: list[dict], regime):
        """统一买入候选处理：信号 + 复盘推荐共用管线。

        每个 candidate 字段:
            code, name, price, buy_min, buy_max, sl, tp, score, trend,
            source: "signal" | "review" (决定告警前缀 + 状态管理)
            alert_key: 去重 key（signal 用 sid, review 用 code）
            signal_id: int|None（DB 信号 ID，用于 expire/update）

        regime: MarketRegime 对象（非旧版 bool），逐票决策时读取 allow_buy/position_mult/entry_rule
        """
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

        # 市场宽度过滤：下跌/上涨 > BREADTH_DOWN_UP_RATIO 且指数跌时暂停新开仓
        breadth_blocked = False
        breadth = self._market_breadth
        if breadth and breadth.get("up", 0) > 0:
            down_up = breadth.get("down", 0) / breadth["up"]
            idx_quote = getattr(self, "_last_index_quote", None) or {}
            idx_change = idx_quote.get("change_pct", 0)
            if (
                down_up > getattr(settings, "BREADTH_DOWN_UP_RATIO", 3.0)
                and idx_change < 0
            ):
                breadth_blocked = True
                if not getattr(self, "_breadth_block_alerted", False):
                    self._breadth_block_alerted = True
                    logger.warning(
                        f"市场宽度过滤: 下跌/上涨={down_up:.1f} 指数{idx_change:+.2%}，暂停新开仓"
                    )
                    self._alert(
                        f"🛑 市场宽度预警\n"
                        f"   下跌/上涨: {down_up:.1f}  指数变化: {idx_change:+.2%}\n"
                        f"   → 多数个股下跌，暂停新开仓位"
                    )
        # 宽度恢复后重置告警标记
        if not breadth_blocked:
            self._breadth_block_alerted = False

        # 早盘 AI 板块倾向：focus 板块候选优先处理
        if self._morning_sector_bias:
            candidates.sort(
                key=lambda c: -self._morning_sector_bias.get(
                    self._industry_cache.get(c["code"], ""), {}
                ).get("priority", 0)
                if self._morning_sector_bias.get(
                    self._industry_cache.get(c["code"], ""), {}
                ).get("bias")
                == "focus"
                else 0
            )

        for c in candidates:
            if breadth_blocked:
                continue
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
            adj_buy_min, adj_buy_max, adj_reason = self._calc_dynamic_buy_zone(
                code, price, buy_min, buy_max, trend
            )
            buy_min, buy_max = adj_buy_min, adj_buy_max

            in_zone = buy_min <= price <= buy_max
            below_zone = price < buy_min
            above_zone = price > buy_max

            # ━━━ 高于买入区 — 预测性接近 + 板块走强提醒 ━━━
            if above_zone:
                above_pct = (price - buy_max) / buy_max * 100
                logger.info(
                    f"买入评估 [{code} {name}] 高于买入区 {above_pct:+.1f}% "
                    f"价格{price:.2f} 区间{buy_min:.2f}~{buy_max:.2f} 板块{trend}"
                )

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
                        if self._scan_count - last_scan >= 15:
                            alert_state[approach_key] = self._scan_count
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
                is_sector_strong = "持续走强" in trend or (
                    "走强" in trend and "弱" not in trend
                )
                is_sector_weak = any(
                    w in trend for w in ("持续走弱", "弱于大盘", "普跌", "横盘")
                )
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
                            intra_parts.append(
                                f"MACD={macd_dir}({intra['macd_bar']:.2f})"
                            )
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
                        )
                        # 立即标记已触发，防止重复提交
                        alert_state[c["alert_key"]] = (price, True)
                        alert_state[chase_key] = self._scan_count
                continue

            if not in_zone and not below_zone:
                continue

            # ━━━ 低于买入区 ━━━
            if below_zone and not in_zone:
                below_pct = (buy_min - price) / buy_min * 100

                # 信号类候选（盘前生成）：价格低于买入区 0.5%+ 视为 zone 失效
                if source == "signal" and below_pct > 0.5:
                    logger.info(
                        f"买入决策 [{code} {name}] 放弃 信号买入区失效 "
                        f"低于{buy_min:.2f} {below_pct:.1f}%"
                    )
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
                below_action, below_reason, below_mul = self._evaluate_below_zone(
                    code, price, buy_min, buy_max
                )

                if below_action == "abandon":
                    logger.info(
                        f"买入决策 [{code} {name}] 放弃 低于买入区{below_pct:.1f}% → {below_reason}"
                    )
                    alert_state[c["alert_key"]] = (price, True)
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
                    logger.info(
                        f"买入决策 [{code} {name}] 观察 低于买入区{below_pct:.1f}% → {below_reason}"
                    )
                    continue

                else:
                    # below_zone 也要遵守 entry_rule（next_day/none 禁止买入）
                    if entry_rule in ("next_day", "none"):
                        alert_state[c["alert_key"]] = (price, True)
                        logger.info(
                            f"买入决策 [{code} {name}] 低于买入区但 entry_rule={entry_rule}，跳过"
                        )
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

            # ━━━ 买入区内 ━━━
            prev_state = alert_state.get(c["alert_key"])
            if prev_state is not None and prev_state[1]:
                prev_price = prev_state[0]
                if prev_price > 0 and abs(price - prev_price) / prev_price < 0.005:
                    continue

            if not market_ok:
                alert_state[c["alert_key"]] = (price, True)
                market_advice = regime_alert_msg or self._get_market_risk_advice()
                self._alert(
                    f"⏸️ {tag}大盘风险 — {code} {name}\n"
                    f"   现价: {price:.2f}  买入区: {buy_min:.2f}~{buy_max:.2f}\n"
                    f"   板块:{trend}\n"
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
                self._alert(
                    f"⏸️ {tag}暂缓买入 — {code} {name}\n"
                    f"   现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}\n"
                    f"   板块:{trend}\n"
                    f"   → {entry_skip_reason}"
                )
                continue

            if self._is_limit_up(code, price):
                alert_state[c["alert_key"]] = (price, True)
                self._alert(
                    f"🚫 涨停无法买入 — {code} {name}\n"
                    f"   涨停价: {self._limit_cache.get(code, {}).get('limit_up', 0):.2f}\n"
                    f"   板块:{trend}\n"
                    f"   → 封涨停板，不建议排板"
                )
                continue

            decision_allowed, decision_reason, size_mul = self._evaluate_buy_decision(
                code, price, buy_min, buy_max
            )

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
                    parts.append(
                        f"KDJ K={intra['kdj_k']:.1f} D={intra['kdj_d']:.1f} J={intra['kdj_j']:.1f}"
                    )
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
                )
                self._alert(
                    f"⏸️ 暂不买入 — {code} {name}\n"
                    f"   现价: {price:.2f}  区间: {buy_min:.2f}~{buy_max:.2f}  止损: {sl:.2f}  止盈: {tp:.2f}\n"
                    f"   板块:{trend}{intra_str}\n"
                    f"   ⛔ {decision_reason}"
                )
                continue

            context = self._analyze_buy_context(code, price, buy_min, buy_max)

            # 提前计算仓位，用于消息显示
            max_amount, _size_reason = self._calculate_position_size(
                code, price, buy_min, buy_max, pattern, trend
            )
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
            actual_pct = (
                max_amount / self.paper_account.total_value
                if self.paper_account.total_value > 0
                else 0
            )

            # 仓位行（全仓/减仓都显示）
            position_line = f"\n   📦 仓位: {actual_pct:.1%} (约¥{max_amount:,})"

            # 理由行（仅减仓时显示）
            reason_line = ""
            if size_mul < 1.0:
                reason_line = f"\n   ⚠️ 理由: {decision_reason}"
                logger.info(
                    f"买入决策 [{code} {name}] 减仓 价格{price:.2f} 仓位{actual_pct:.1%} → {decision_reason}"
                )
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
            name = s.get("stock_name", "")
            if not name or name == code:
                name = self._resolve_name(code)

            sl = s.get("stop_loss", 0) or 0
            tp = s.get("take_profit", 0) or 0
            if sl <= 0 or tp <= 0:
                logger.warning(
                    f"  信号 {code} {name} 缺少止损/止盈 (sl={sl}, tp={tp})，跳过"
                )
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
        # 名额质量门控：名额越少要求越高，防止低质量信号占坑
        filled = len(self.paper_account.positions)
        remaining = settings.MAX_POSITIONS - filled
        min_mul = {0: 0.99, 1: 0.80, 2: 0.65, 3: 0.55, 4: 0.50}.get(remaining, 0.50)
        if size_mul < min_mul:
            logger.info(
                f"买入决策 [{code} {name}] 放弃 名额{filled}/{settings.MAX_POSITIONS} "
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
            # 决策日志
            try:
                self._log_buy_trigger(
                    signal_id=signal_id,
                    stock_code=code,
                    price=price,
                    buy_min=buy_min,
                    buy_max=buy_max,
                    position_size=max_amount,
                    entry_rule=getattr(regime, "entry_rule", "standard")
                    if regime
                    else "standard",
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
        existing.update(
            self._recently_sold.keys() if hasattr(self, "_recently_sold") else []
        )
        try:
            for s in self.repo.get_pending_signals(account="paper"):
                existing.add(s.get("stock_code", ""))
        except Exception:
            pass

        self._ensure_industry_cache()

        candidates = []
        for sector in hot_sectors[:5]:  # 取热度前 5 的板块
            sname = sector["name"]

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
                zone_pct = max(1.5, 3.0 - sector["score"] * 0.3)
                buy_min = round(price_f * (1 - zone_pct / 100), 2)
                buy_max = round(price_f * (1 + zone_pct / 100) * 0.5, 2)
                sl = round(price_f * 0.94, 2)
                tp = round(price_f * (1.08 + sector["score"] * 0.01), 2)

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
        excluded.update(
            self._recently_sold.keys() if hasattr(self, "_recently_sold") else []
        )
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
            zone_pct = max(1.5, 3.0 - op["pullback_depth"] * 0.2)
            buy_min = round(price_f * (1 - zone_pct / 100), 2)
            buy_max = round(price_f * (1 + zone_pct / 100) * 0.5, 2)
            sl = round(price_f * 0.93, 2)
            tp = round(price_f * 1.08, 2)

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

    # ======================== 第二层：板块热度 ========================
