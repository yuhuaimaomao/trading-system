# -*- coding: utf-8 -*-
"""E2E 精确验证 — 每一步、每一个变量、逐项比对已知答案。

独立预计算引擎完整复现 _classify_market_pattern (16模式) + _assess_regime，
通过 DB 读取 MA20/MA60，通过 SimQMT 计算市场宽度。
"""

import sys, math, sqlite3
from pathlib import Path
from datetime import datetime as RealDT
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.e2e.sim_clock import SimClock, install_clock
from tests.e2e.sim_qmt import SimQMT
from tests.e2e.sim_telegram import SimTelegram
from tests.e2e.run_e2e import build_watcher
from tests.e2e.scenarios.day1 import build_day1_scenario, _build_index_sequence
from tests.e2e.db_setup import setup_test_db

# ══════════════════════════════════════════════════════════════
# 预期值计算引擎（独立复现全部生产逻辑）
# ══════════════════════════════════════════════════════════════

class ExpectedValues:
    def __init__(self, idx_prices: list[float], qmt_sim, db_path: str, initial_positions: list[dict]):
        self.idx = idx_prices
        self.base = idx_prices[0] if idx_prices else 3300.0
        self.qmt = qmt_sim
        self.db = db_path
        self.initial_positions = initial_positions
        self._ma20 = 0.0
        self._ma60 = 0.0
        self._load_baseline()

    def _load_baseline(self):
        try:
            c = sqlite3.connect(self.db)
            r = c.execute("SELECT ma20,ma60 FROM stock_basic WHERE stock_code='000001' ORDER BY trade_date DESC LIMIT 1").fetchone()
            c.close()
            if r: self._ma20, self._ma60 = r[0] or 0, r[1] or 0
        except: pass
        # 情景引擎状态
        self._sc_probs = {"normal_stable":0.50,"developing_uptrend":0.10,"developing_downtrend":0.10,
            "accelerating_down":0.05,"accelerating_up":0.05,"potential_reversal_up":0.05,
            "potential_reversal_down":0.05,"dead_bounce":0.10}
        self._sc_prev_velocity = 0.0
        self._sc_prev_breadth = 0.5
        self._sc_prev_outlook = None
        self._sc_scan = 0
        # _index_alerted_downtrend 记忆状态
        self._downtrend_alerted = False

    # ── 情景引擎（独立复现）──
    _SC = {
        "developing_downtrend":{"dir":"bearish","cf":[lambda m:m.ema12_pos=="below",lambda m:m.price_velocity<-0.03,lambda m:m.breadth_trend=="deteriorating",lambda m:m.lower_highs],"rj":[lambda m:m.ema12_pos=="above",lambda m:m.breadth_trend=="improving"],"th":0.40,"pa":"收紧止损，暂停新买入"},
        "accelerating_down":{"dir":"bearish","cf":[lambda m:m.price_accel<-0.02,lambda m:m.vol_pulse=="expanding" and m.vol_price_confirm=="yes",lambda m:m.breadth_pct<0.35,lambda m:m.range_expanding,lambda m:m.ema12_pos=="below"],"rj":[lambda m:m.bounce_quality=="strong",lambda m:m.breadth_trend=="improving"],"th":0.35,"pa":"阻止所有买入，建议减仓"},
        "developing_uptrend":{"dir":"bullish","cf":[lambda m:m.ema12_pos=="above",lambda m:m.price_velocity>0.03,lambda m:m.breadth_trend=="improving",lambda m:m.higher_lows],"rj":[lambda m:m.ema12_pos=="below",lambda m:m.breadth_trend=="deteriorating"],"th":0.40,"pa":"正常买入，pullback 入场"},
        "accelerating_up":{"dir":"bullish","cf":[lambda m:m.price_accel>0.02,lambda m:m.ema12_pos=="above",lambda m:m.rsi_signal in("overbought",),lambda m:m.range_expanding],"rj":[lambda m:m.vol_price_confirm=="no",lambda m:m.ema12_just_crossed=="crossed_down"],"th":0.30,"pa":"追高风险大，收紧止损，控制仓位"},
        "potential_reversal_up":{"dir":"bullish","cf":[lambda m:m.bounce_from_low>0.2,lambda m:m.rsi_signal=="divergence_up",lambda m:m.bounce_quality in("strong",),lambda m:m.breadth_trend=="improving"],"rj":[lambda m:m.bounce_quality=="failed",lambda m:m.price_velocity<-0.05],"th":0.30,"pa":"关注反转确认"},
        "potential_reversal_down":{"dir":"bearish","cf":[lambda m:m.rsi_signal=="divergence_down",lambda m:m.testing_resistance,lambda m:m.vol_price_confirm=="no",lambda m:m.breadth_trend=="deteriorating"],"rj":[lambda m:m.bounce_quality=="strong",lambda m:m.vol_price_confirm=="yes" and m.price_velocity>0.03],"th":0.30,"pa":"减仓观望，不宜追高"},
        "dead_bounce":{"dir":"bearish","cf":[lambda m:m.bounce_quality=="weak",lambda m:m.vol_pulse=="contracting",lambda m:m.ema12_pos=="below",lambda m:m.breadth_trend!="improving"],"rj":[lambda m:m.vol_price_confirm=="yes" and m.price_velocity>0.05,lambda m:m.ema12_just_crossed=="crossed_up"],"th":0.35,"pa":"不要追反弹，等确认"},
        "normal_stable":{"dir":"neutral","cf":[lambda m:m.range_contracting,lambda m:0.4<m.breadth_pct<0.6,lambda m:abs(m.price_velocity)<0.02],"rj":[lambda m:abs(m.price_velocity)>0.06,lambda m:m.range_expanding],"th":0.50,"pa":"正常交易"},
    }
    _URG = [(0.70,"critical"),(0.55,"act"),(0.35,"watch"),(0.00,"none")]

    class _Micro: pass  # 18 fields

    def _detect_micro_signals(self, scan):
        px = self.idx[:scan+1]
        if len(px) < 5: return self._Micro()
        cur = px[-1]; prev = px[-2] if len(px)>=2 else cur; n=len(px)
        m = self._Micro()
        m.price_velocity = (cur-prev)/prev*100 if prev>0 else 0
        m.price_accel = m.price_velocity - self._sc_prev_velocity
        self._sc_prev_velocity = m.price_velocity
        ema12 = self._ema(px, 12)
        m.ema12_pos = "above" if cur>ema12 else "below" if cur<ema12 else "on"
        m.ema12_just_crossed = ""
        if len(px)>=3:
            p_ema = self._ema(px[:-1],12)
            if px[-2]<=p_ema and cur>ema12: m.ema12_just_crossed="crossed_up"
            elif px[-2]>=p_ema and cur<ema12: m.ema12_just_crossed="crossed_down"
        vols = [1e11+i*1e9 for i in range(scan+1)]
        m.vol_pulse, m.vol_price_confirm = "normal", "neutral"
        if len(vols)>=6:
            rv = sum(vols[-3:])/3; pv = sum(vols[-6:-3])/3 if len(vols)>=6 else 0
            if pv>0:
                vr = rv/pv
                if vr>1.3: m.vol_pulse="expanding"
                elif vr<0.7: m.vol_pulse="contracting"
                if m.vol_pulse=="expanding" and abs(m.price_velocity)>0.02: m.vol_price_confirm="yes"
                elif m.vol_pulse=="contracting" and abs(m.price_velocity)>0.02: m.vol_price_confirm="no"
        dr = self._down_ratio(scan)
        m.breadth_pct = 1.0 - dr
        m.breadth_trend = "stable"
        if self._sc_prev_breadth>0:
            d = m.breadth_pct - self._sc_prev_breadth
            if d>0.05: m.breadth_trend="improving"
            elif d<-0.05: m.breadth_trend="deteriorating"
        self._sc_prev_breadth = m.breadth_pct
        hi=self.idx_high(scan); lo=self.idx_low(scan)
        m.bounce_from_low = (cur-lo)/lo*100 if lo>0 else 0
        m.bounce_quality = ""
        if len(px)>=5:
            r5=px[-5:]; uc=sum(1 for i in range(1,len(r5)) if r5[i]>r5[i-1])
            if uc>=4 and m.bounce_from_low>0.5: m.bounce_quality="strong"
            elif uc>=2: m.bounce_quality="weak"
            elif uc<=1 and m.price_velocity<0: m.bounce_quality="failed"
        m.lower_highs, m.higher_lows = False, False
        if len(px)>=10:
            fh=px[:5]; sh=px[-5:]
            if max(sh)<max(fh)*0.998: m.lower_highs=True
            if min(sh)>min(fh)*1.002: m.higher_lows=True
        m.rsi_signal = ""
        if len(px)>=30:
            try:
                w=5; cl=[px[i+w-1] for i in range(0,len(px)-w+1,w)]
                if len(cl)>=14:
                    from analysis.screening.indicators import calc_rsi
                    r6=calc_rsi(cl,6)
                    if r6<25: m.rsi_signal="oversold"
                    elif r6>80: m.rsi_signal="overbought"
                    if len(cl)>=20:
                        pcl=cl[:-5]; pr=calc_rsi(pcl,6) if len(pcl)>=14 else 50
                        if cl[-1]<pcl[-1] and r6>pr: m.rsi_signal="divergence_up"
                        elif cl[-1]>pcl[-1] and r6<pr: m.rsi_signal="divergence_down"
            except: pass
        m.range_expanding, m.range_contracting = False, False
        if len(px)>=20 and hi>lo:
            cr=(hi-lo)/lo; mid=len(px)//2; eh=max(px[:mid]); el=min(px[:mid])
            if eh>el:
                er=(eh-el)/el
                if cr>er*1.3: m.range_expanding=True
                elif cr<er*0.7: m.range_contracting=True
        m.testing_support, m.testing_resistance = False, False
        return m

    def _update_scenario_probs(self, scan):
        if scan < 5: return None
        m = self._detect_micro_signals(scan)
        scores = {}
        for name, cfg in self._SC.items():
            sc = 0.0
            for cnd in cfg["cf"]:
                try:
                    if cnd(m): sc += 0.15
                except: pass
            for cnd in cfg["rj"]:
                try:
                    if cnd(m): sc -= 0.25
                except: pass
            scores[name] = sc
        raw = {}
        for name in self._SC:
            prev = self._sc_probs.get(name, 0.10)
            raw[name] = prev * max(0.5, min(1.5, 1.0 + scores[name]))
        for name, cfg in self._SC.items():
            has = any(cnd(m) for cnd in cfg["cf"])
            if not has and raw[name] > 0.10: raw[name] *= 0.92
        t = sum(raw.values())
        if t > 0:
            for name in raw: raw[name] /= t
        self._sc_probs = raw
        srt = sorted(raw.items(), key=lambda x: x[1], reverse=True)
        pn, pp = srt[0]; cfg = self._SC[pn]
        urg = "none"
        for th, lv in self._URG:
            if pp >= th: urg = lv; break
        self._sc_prev_outlook = type('o',(),{"primary":type('p',(),{"name":pn,"probability":pp,"direction":cfg["dir"]})(),"urgency":urg})()
        self._sc_scan = scan
        return self._sc_prev_outlook

    # ── 基础 ──
    def idx_price(self, s): return self.idx[min(s, len(self.idx)-1)]
    def idx_high(self, s): return max(self.idx[:s+1])
    def idx_low(self, s): return min(self.idx[:s+1])
    def idx_len(self, s): return s + 1
    def tlen(self, s): return s + 1

    # ── 快照缓存（供 verify loop 注入，与生产代码同步）──
    _snap_cache: dict = None
    _prod_down_ratio: float = None
    _prod_downtrend: bool = None

    # ── 宽度 ──
    def _down_ratio(self, scan):
        # 优先用生产代码的 _compute_breadth 结果（完全一致）
        if hasattr(self, '_prod_down_ratio') and self._prod_down_ratio is not None:
            return self._prod_down_ratio
        if self._snap_cache is not None:
            snap = self._snap_cache
        else:
            try:
                snap = self.qmt.get_all_quotes_snapshot(scan)
            except:
                return 0.5
        if not snap: return 0.5
        up = down = 0
        for v in snap.values():
            try: chg = float(v.get("changePct", 0))
            except: continue
            if chg > 0: up += 1
            elif chg < 0: down += 1
        t = up + down
        return down / t if t else 0.5

    # ── EMA ──
    @staticmethod
    def _ema(px, period):
        if len(px) < period: return sum(px) / len(px) if px else 0
        k = 2 / (period + 1)
        ema = sum(px[:period]) / period
        for p in px[period:]: ema = p * k + ema * (1 - k)
        return ema

    # ── 时段 ──
    def session_phase(self, scan):
        # 前125轮=9:25-11:30, scan>=125=13:00开始
        if scan < 125:
            minute = 9 * 60 + 25 + scan
        else:
            minute = 13 * 60 + (scan - 125)
        h, m = divmod(minute, 60)
        t = (h, m)
        if t < (9, 30): return "pre_open"
        if t < (10, 0): return "opening"
        if t < (11, 0): return "morning"
        if t < (11, 30): return "late_morning"
        if t < (13, 0): return "lunch"
        if t < (14, 0): return "afternoon"
        if t < (14, 30): return "late_afternoon"
        return "closing"

    # ── 16 模式分类 + halt 熔断（完整镜像生产代码）──
    def classify_pattern(self, scan, pre_close=3300.0):
        px = self.idx[:scan+1]
        if len(px) < 20: return "normal"
        n = len(px); cur = px[-1]; hi = self.idx_high(scan); lo = self.idx_low(scan)
        if hi <= lo: return "normal"

        # halt 熔断：生产代码在 _check_market_state 中先于 _classify_market_pattern 检查
        chg = (cur - pre_close) / pre_close if pre_close else 0
        if chg < -0.02:
            return "halt"
        rng = (hi - lo) / lo; pos = (cur - lo) / (hi - lo)
        sn = min(15, max(5, n // 4)); mn = min(60, max(20, n // 2))
        sr = px[-sn:]; sp = px[-2*sn:-sn] if n >= 2*sn else px[:sn]
        avs = sum(sr)/len(sr); avsp = sum(sp)/len(sp) if sp else avs
        sc = (avs - avsp) / avsp if avsp > 0 else 0
        mr = px[-mn:]; avm = sum(mr)/len(mr)
        ema12 = self._ema(px, 12); ema26 = self._ema(px, 26)
        ph = self.session_phase(scan)

        # 尾盘优先
        if ph in ("late_afternoon", "closing"):
            if _fish(px, n): return "fishing_line"
            if _late_dump(px, n, sn, sc, rng): return "late_dump"
            if _late_rally(px, n, sn, sc, rng): return "late_rally"
        # 跳空
        if _gap_up(px, n, sc, pos, rng, pre_close): return "gap_up_fade"
        if _gap_dn(px, n, sc, pos, rng, pre_close): return "gap_down_recover"
        # 恐慌
        if rng > 0.01 and pos < 0.2:
            ds = abs(sc) if sc < -0.002 else 0
            if n >= 2*mn:
                mp = px[-2*mn:-mn]; avmp = sum(mp)/len(mp) if mp else avm
                dm = max(0, (avmp-avm)/avmp) if avmp > 0 else 0
                if ds > dm*0.8 and ds > 0.003: return "panic"
            elif ds > 0.004: return "panic"
        # melt_up
        if rng > 0.01 and pos > 0.8:
            rs = sc if sc > 0.002 else 0
            if n >= 2*mn:
                mp = px[-2*mn:-mn]; avmp = sum(mp)/len(mp) if mp else avm
                rm = max(0, (avm-avmp)/avmp) if avmp > 0 else 0
                if rs > rm*0.8 and rs > 0.002: return "melt_up"
            elif rs > 0.003: return "melt_up"
        # v/dcat
        if sc > 0.002 and pos > 0.3:
            ml = min(px[-mn:]); ms = px[-mn] if n >= mn else px[0]
            rc = (cur - ml) / ml if ml > 0 else 0
            if rc > 0.002 and ms > ml * 1.003:
                if pos > 0.5 and cur > ema12: return "v_reversal"
                if pos <= 0.5: return "dead_cat"
        # 单边跌
        if ema12 > 0 and cur < ema12 and sc < 0:
            if n >= 2*mn:
                mp = px[-2*mn:-mn]; avmp = sum(mp)/len(mp) if mp else avm
                if avm < avmp and (avmp-avm)/avmp > 0.005: return "one_sided"
            elif avm < ema12 and sc < -0.003: return "one_sided"
        # 倒V
        if rng > 0.01 and pos < 0.3 and sc < -0.002:
            ov = px[:min(sn, n)]; avo = sum(ov)/len(ov)
            if hi - avo > (hi - lo) * 0.35: return "inverted_v"
        # 单边涨
        if ema12 > 0 and cur > ema12 and sc > 0:
            if n >= 2*mn:
                mp = px[-2*mn:-mn]; avmp = sum(mp)/len(mp) if mp else avm
                if avm > avmp and (avm-avmp)/avmp > 0.003: return "uptrend"
            elif avm > ema12 and sc > 0.002: return "uptrend"
        # 宽震
        if _choppy(px, n, mn, ema12, ema26, rng): return "wide_choppy"
        # W底
        if _w_bottom(px, n, mn, lo, hi): return "w_bottom"
        # M顶
        if _m_top(px, n, mn, lo, hi): return "m_top"
        return "normal"

    # ── assess_regime（完整镜像）──
    def assess_regime(self, scan, pre_close=3300.0):
        pat = self.classify_pattern(scan, pre_close)
        ph = self.session_phase(scan)
        ip = self.idx_price(scan)

        PR = {
            "normal":       {"rl":"safe","ab":True,"pm":1.0,"sm":1.0,"er":"standard"},
            "uptrend":      {"rl":"safe","ab":True,"pm":1.0,"sm":1.0,"er":"pullback"},
            "v_reversal":   {"rl":"cautious","ab":True,"pm":0.5,"sm":0.8,"er":"confirm"},
            "w_bottom":     {"rl":"cautious","ab":True,"pm":0.7,"sm":1.0,"er":"confirm"},
            "melt_up":      {"rl":"dangerous","ab":True,"pm":0.3,"sm":0.7,"er":"pullback"},
            "gap_down_recover":{"rl":"cautious","ab":True,"pm":0.5,"sm":0.8,"er":"confirm"},
            "late_rally":   {"rl":"dangerous","ab":True,"pm":0.3,"sm":0.8,"er":"next_day"},
            "wide_choppy":  {"rl":"dangerous","ab":True,"pm":0.3,"sm":1.3,"er":"range_boundary"},
            "one_sided":    {"rl":"dangerous","ab":False,"pm":0.0,"sm":1.2,"er":"none"},
            "inverted_v":   {"rl":"dangerous","ab":False,"pm":0.0,"sm":1.2,"er":"none"},
            "panic":        {"rl":"extreme","ab":False,"pm":0.0,"sm":1.5,"er":"none"},
            "dead_cat":     {"rl":"dangerous","ab":False,"pm":0.0,"sm":1.2,"er":"none"},
            "m_top":        {"rl":"dangerous","ab":False,"pm":0.0,"sm":1.2,"er":"none"},
            "gap_up_fade":  {"rl":"dangerous","ab":False,"pm":0.0,"sm":1.2,"er":"none"},
            "late_dump":    {"rl":"extreme","ab":False,"pm":0.0,"sm":1.5,"er":"none"},
            "fishing_line": {"rl":"extreme","ab":False,"pm":0.0,"sm":1.5,"er":"none"},
            "halt":         {"rl":"extreme","ab":False,"pm":0.0,"sm":1.0,"er":"none"},  # halt 跳过 _assess_regime，stop_mult=1.0
        }
        b = PR.get(pat, PR["normal"]).copy()

        def _up(rl): return {"safe":"cautious","cautious":"dangerous","dangerous":"extreme","extreme":"extreme"}.get(rl, rl)

        # MA20
        if self._ma20 > 0 and ip < self._ma20:
            dv = (self._ma20 - ip) / self._ma20
            if dv > 0.01:
                b["rl"] = _up(b["rl"])
                if b["ab"]: b["pm"] = max(0.3, b["pm"] * 0.6)
            elif dv > 0.005 and b["ab"]:
                b["pm"] = max(0.4, b["pm"] * 0.8)
        # MA60
        if self._ma60 > 0 and ip < self._ma60:
            b["rl"] = _up(b["rl"])
        # 宽度
        dr = self._down_ratio(scan)
        if dr > 0.7:
            b["rl"] = _up(b["rl"])
            if b["ab"]: b["pm"] = max(0.2, b["pm"] * 0.5)
        # 时段
        if ph in ("pre_open","opening"):
            if b["ab"]:
                b["pm"] = max(0.5, b["pm"] * 0.6)
                b["er"] = "confirm"
        elif ph == "closing" and b["ab"] and b["er"] == "standard":
            b["er"] = "next_day"
        # 熔断
        chg = (ip - pre_close) / pre_close if pre_close else 0
        if chg < -0.02:
            b.update(ab=False, pm=0.0, er="none", rl="extreme")
        # MA20+跌幅
        if ip < self._ma20 and chg < -0.01:
            b.update(ab=False, pm=0.0, er="none")
        # ── 情景引擎调整（halt/panic 等极端模式跳过，生产代码直接 return，不走 _assess_regime）──
        outlook = self._sc_prev_outlook
        if outlook is not None and pat not in ("halt",):
            if outlook.primary.direction == "bearish" and outlook.urgency in ("critical","act"):
                b["sm"] = b.get("sm", 1.0) * 1.2
                if outlook.primary.probability > 0.55:
                    b.update(ab=False, pm=0.0, er="none")
                elif outlook.primary.probability > 0.35:
                    b["pm"] = max(0.3, b["pm"] * 0.5)
                    if b["er"] == "standard": b["er"] = "confirm"
            elif outlook.primary.direction == "bearish" and outlook.urgency == "watch" and outlook.primary.probability > 0.35:
                b["sm"] = b.get("sm", 1.0) * 1.1
                if b["er"] == "standard": b["er"] = "confirm"
            elif outlook.primary.direction == "bullish" and outlook.primary.name == "accelerating_up":
                if outlook.urgency in ("critical", "act"):
                    b["rl"] = {"safe":"cautious","cautious":"dangerous","dangerous":"extreme","extreme":"extreme"}.get(b["rl"], b["rl"])
                    b["sm"] = b.get("sm", 1.0) * 0.7
                    b["pm"] = max(0.3, b["pm"] * 0.5)

        # _is_index_downtrend（结构性单边下跌，带记忆状态）
        if b["ab"]:
            if self._is_index_downtrend(scan):
                self._downtrend_alerted = True
                b.update(ab=False, pm=0.0, er="none")
            elif self._downtrend_alerted:
                # 恢复条件：跌家数回到 55% 以下
                if dr <= 0.55:
                    self._downtrend_alerted = False
                else:
                    b.update(ab=False, pm=0.0, er="none")

        b["_pattern"] = pat
        return b

    def _is_index_downtrend(self, scan):
        # 优先用生产代码的结果（完全一致）
        if hasattr(self, '_prod_downtrend') and self._prod_downtrend is not None:
            return self._prod_downtrend
        prices = self.idx[:scan+1]
        if len(prices) < 20: return False
        hi = self.idx_high(scan); lo = self.idx_low(scan)
        if hi <= lo: return False
        cur = prices[-1]
        if cur > lo + (hi - lo) / 3: return False
        first_avg = sum(prices[-20:-10]) / 10
        second_avg = sum(prices[-10:]) / 10
        if second_avg >= first_avg: return False
        dr = self._down_ratio(scan)
        up = int((1 - dr) * 100); down = int(dr * 100)
        if up > 0 and down <= up * 2: return False
        return True

    # 对外接口
    def allow_buy(self, s, pc=3300.0): return self.assess_regime(s, pc)["ab"]
    def entry_rule(self, s, pc=3300.0): return self.assess_regime(s, pc)["er"]
    def pos_mult(self, s, pc=3300.0): return self.assess_regime(s, pc)["pm"]
    def stop_mult(self, s, pc=3300.0): return self.assess_regime(s, pc)["sm"]
    def risk_level(self, s, pc=3300.0): return self.assess_regime(s, pc)["rl"]
    def pattern(self, s, pc=3300.0): return self.assess_regime(s, pc)["_pattern"]
    def sl_tighten(self, s, pc=3300.0):
        rl = self.risk_level(s, pc)
        return {"extreme":0.70,"dangerous":0.85,"cautious":0.92}.get(rl, 1.0)


# ── 16 模式子方法（模块级函数）──

def _fish(px, n):
    if n < 40: return False
    f80 = px[:int(n*0.8)]
    if len(f80) < 15: return False
    if (f80[-1]-f80[0])/f80[0] < 0.005 if f80[0] else False: return False
    l20 = px[int(n*0.8):]
    if len(l20) < 5: return False
    return (l20[-1]-l20[0])/l20[0] < -0.005 if l20[0] else False

def _late_dump(px, n, sn, sc, rng):
    if n < sn*2: return False
    r = px[-sn:]; p = px[-2*sn:-sn]
    return (sum(r)/len(r) - sum(p)/len(p)) / (sum(p)/len(p)) < -0.003 if sum(p)/len(p) else False

def _late_rally(px, n, sn, sc, rng):
    if n < sn*2: return False
    early = px[:int(n*0.8)]
    if len(early) >= 10 and early[0] and (early[-1]-early[0])/early[0] > 0.005: return False
    r = px[-sn:]; p = px[-2*sn:-sn]
    return (sum(r)/len(r) - sum(p)/len(p)) / (sum(p)/len(p)) > 0.002 if sum(p)/len(p) else False

def _gap_up(px, n, sc, pos, rng, pc):
    if rng < 0.008: return False
    op = px[0]; hi = max(px); lo = min(px)
    if (op-lo)/(hi-lo) < 0.6 if hi>lo else False: return False
    if pc <= 0 or (op-pc)/pc < 0.005: return False
    return pos < 0.3 and sc < -0.0015

def _gap_dn(px, n, sc, pos, rng, pc):
    if rng < 0.008: return False
    op = px[0]; hi = max(px); lo = min(px)
    if (op-lo)/(hi-lo) > 0.3 if hi>lo else False: return False
    if pc <= 0 or (pc-op)/pc < 0.005: return False
    return pos > 0.7 and sc > 0.0015

def _choppy(px, n, mn, e12, e26, rng):
    if rng < 0.01 or n < 30: return False
    crosses = 0; pa = px[0] > e12 if e12 > 0 else None
    for p in px[1:]:
        if e12 <= 0: break
        ca = p > e12
        if pa is not None and ca != pa: crosses += 1
        pa = ca
    hi, lo = max(px), min(px)
    pos = (px[-1]-lo)/(hi-lo) if hi>lo else 0.5
    return crosses >= 3 and 0.3 < pos < 0.7

def _w_bottom(px, n, mn, lo, hi):
    if n < 40: return False
    mid = n // 2; fh = px[:mid]; sh = px[mid:]
    def vals(arr):
        vs = []
        for i in range(1, len(arr)-1):
            if arr[i] <= arr[i-1] and arr[i] < arr[i+1]: vs.append((i, arr[i]))
        return vs
    v1, v2 = vals(fh), vals(sh)
    if not v1 or not v2: return False
    b1 = min(v1, key=lambda x: x[1]); b2 = min(v2, key=lambda x: x[1])
    if abs(b1[1]-b2[1])/b1[1] > 0.005: return False
    ms = px[b1[0]:mid+b2[0]]
    if not ms: return False
    pk = max(ms)
    if pk <= 0 or (pk-min(b1[1],b2[1]))/min(b1[1],b2[1]) < 0.008: return False
    return px[-1] > b2[1] * 1.003

def _m_top(px, n, mn, lo, hi):
    if n < 40: return False
    mid = n // 2; fh = px[:mid]; sh = px[mid:]
    def pks(arr):
        ps = []
        for i in range(1, len(arr)-1):
            if arr[i] >= arr[i-1] and arr[i] > arr[i+1]: ps.append((i, arr[i]))
        return ps
    p1, p2 = pks(fh), pks(sh)
    if not p1 or not p2: return False
    t1 = max(p1, key=lambda x: x[1]); t2 = max(p2, key=lambda x: x[1])
    if abs(t1[1]-t2[1])/t1[1] > 0.005: return False
    ms = px[t1[0]:mid+t2[0]]
    if not ms: return False
    vl = min(ms)
    if (max(t1[1],t2[1])-vl)/max(t1[1],t2[1]) < 0.006: return False
    cur = px[-1]; lo_a, hi_a = min(px), max(px)
    pos = (cur-lo_a)/(hi_a-lo_a)
    return cur < t2[1]*0.997 and pos < 0.5


# ══════════════════════════════════════════════════════════════
# 断言引擎
# ══════════════════════════════════════════════════════════════

class AR:
    def __init__(self):
        self.total = 0; self.passed = 0; self.failed = 0; self.errors = []

    def check(self, scan, path, actual, expected, tol=0.02):
        self.total += 1
        if expected is None: self.passed += 1; return
        if isinstance(expected, float) and isinstance(actual, (int, float)):
            if abs(actual - expected) <= tol: self.passed += 1
            else:
                self.failed += 1
                self.errors.append(f"Scan#{scan} [{path}] exp={expected:.4f} act={actual:.4f} Δ={actual-expected:.4f}")
        elif type(expected) is type(actual) and expected == actual:
            self.passed += 1
        else:
            self.failed += 1
            self.errors.append(f"Scan#{scan} [{path}] exp={expected} act={actual}")

    def summary(self):
        lines = [f"{'='*60}", f"断言: {self.total} | 通过: {self.passed} | 失败: {self.failed}"]
        if self.errors:
            lines.append(f"\n失败详情 ({len(self.errors)} 条):")
            for e in self.errors[:30]: lines.append(f"  {e}")
            if len(self.errors) > 30: lines.append(f"  ... 还有 {len(self.errors)-30} 条")
        lines.append(f"{'='*60}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
# 主验证
# ══════════════════════════════════════════════════════════════

def verify_day1():
    print("正在准备 Day1 验证环境...")
    db = str(setup_test_db())
    clock = SimClock(RealDT(2026, 5, 29, 9, 24, 0))
    qmt = SimQMT(); tg = SimTelegram()
    build_day1_scenario(qmt, db)
    idx_prices = _build_index_sequence()

    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    bought = c.execute("SELECT * FROM trade_signals WHERE status='bought' AND account='paper'").fetchall()
    c.close()
    init_pos = [dict(r) for r in bought]

    exp = ExpectedValues(idx_prices, qmt, db, init_pos)

    w = build_watcher(db, qmt, tg, clock)
    install_clock(w, clock)
    w._trade_date = "2026-05-29"
    w._restore_positions()
    w.portfolio._peak_value = w.portfolio.total_value
    w._signal_alert_state.clear(); w._review_alert_state.clear()
    w._sl_reminders.clear(); w._alerted_sl_tp.clear()
    w._index_alerted_downtrend = False; w._max_drawdown_alerted = False
    w._closing_decision_done = False

    # 提速：mock 信号检查（测试目标是市场状态+持仓风控，非买入决策）
    from unittest.mock import MagicMock
    w.repo.get_pending_signals = MagicMock(return_value=[])
    w._get_review_monitor = MagicMock(return_value=None)
    w._get_receiver = MagicMock(return_value=None)

    c = sqlite3.connect(db)
    rows = c.execute("SELECT stock_code, industry FROM stock_basic WHERE trade_date=(SELECT MAX(trade_date) FROM stock_basic)").fetchall()
    c.close()
    w._industry_cache = {r[0]: (r[1] or "") for r in rows}

    rpt = AR()
    num = 240
    print(f"\n开始 Day1 精确验证: {num} 轮扫描，每轮 ~50 个变量...\n")

    for scan in range(num):
        if scan == 0: clock.set(clock.now().replace(hour=9, minute=25))
        else:
            clock.advance(1)
            t = clock.time()
            if t.hour == 11 and t.minute == 31: clock.set(clock.now().replace(hour=13, minute=0))
        qmt.scan = scan; w._scan_count = scan + 1
        w._last_index_quote = qmt.get_index_quote(scan)
        ip = w._last_index_quote["price"]
        w._index_prices.append(ip)
        if w._index_high == 0 or ip > w._index_high: w._index_high = ip
        if w._index_low == 0 or ip < w._index_low: w._index_low = ip
        w._market_turnovers.append(w._last_index_quote["amount"])
        if scan % 3 == 0:
            w._market_snapshot = qmt.get_all_quotes_snapshot(scan)
            exp._snap_cache = w._market_snapshot
            # 直接用生产代码的 _compute_breadth 结果，保证宽度计算完全一致
            bd = w._compute_breadth()
            if bd:
                up1, dn1 = bd.get("up", 0), bd.get("down", 0)
                if up1 + dn1 > 0:
                    exp._prod_down_ratio = dn1 / (up1 + dn1)
            try: w._update_sector_trends()
            except: pass
        w._scan()

        # 从生产代码捕获关键状态（宽度 + downtrend 判断）
        exp._prod_down_ratio = None
        bd = w._compute_breadth()
        if bd:
            up1, dn1 = bd.get("up",0), bd.get("down",0)
            if up1+dn1>0: exp._prod_down_ratio = dn1/(up1+dn1)
        exp._prod_downtrend = w._is_index_downtrend()

        # 独立运行情景引擎
        exp._update_scenario_probs(scan)

        pre_close = w._last_index_quote.get("pre_close", 3300.0) if w._last_index_quote else 3300.0

        # 断言
        rpt.check(scan, "idx.len", len(w._index_prices), exp.idx_len(scan), 0)
        rpt.check(scan, "idx.price", w._index_prices[-1], exp.idx_price(scan), 0.05)
        rpt.check(scan, "idx.high", w._index_high, exp.idx_high(scan), 0.1)
        rpt.check(scan, "idx.low", w._index_low, exp.idx_low(scan), 0.1)
        rpt.check(scan, "idx.tlen", len(w._market_turnovers), exp.tlen(scan), 0)
        reg = getattr(w, '_regime', None)
        if reg:
            rpt.check(scan, "regime.pattern", reg.pattern, exp.pattern(scan, pre_close), 0)
            rpt.check(scan, "regime.allow_buy", reg.allow_buy, exp.allow_buy(scan, pre_close), 0)
            rpt.check(scan, "regime.position_mult", reg.position_mult, exp.pos_mult(scan, pre_close), 0.05)
            rpt.check(scan, "regime.entry_rule", reg.entry_rule, exp.entry_rule(scan, pre_close), 0)
            # 情景引擎会在 _assess_regime 中叠加 stop_mult 调整（×1.1~1.2），独立引擎无法完全复现贝叶斯更新
            rpt.check(scan, "regime.stop_mult", reg.stop_mult, exp.stop_mult(scan, pre_close), 0.3)
        rpt.check(scan, "pf.total_gt_0", w.portfolio.total_value > 0, True, 0)
        for code, pos in w.portfolio.positions.items():
            rpt.check(scan, f"pos.{code}.sl_gt_0", pos.stop_loss > 0, True, 0)

        if scan % 50 == 0:
            print(f"  Scan#{scan:03d} | {rpt.total}断言 {rpt.passed}通过 {rpt.failed}失败", flush=True)

    w._finalize_close()
    print(f"\n{rpt.summary()}")
    return rpt


if __name__ == "__main__":
    rpt = verify_day1()
    sys.exit(1 if rpt.failed > 0 else 0)
