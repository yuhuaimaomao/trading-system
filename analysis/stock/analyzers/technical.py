"""技术面分析器 — 复用 analysis/indicators + data/readers。"""

import sqlite3

from analysis.indicators import calc_bollinger, calc_kdj, calc_macd, calc_rsi
from analysis.stock.analyzers import BaseAnalyzer
from analysis.stock.schemas import AnalysisResult
from data.readers.stock_reader import StockReader
from system.config import settings


class TechnicalAnalyzer(BaseAnalyzer):
    """技术面分析：K线/均线/布林/RSI/MACD/KDJ/量价关系。"""

    name = "technical"

    def analyze(self, symbol: str, **params) -> AnalysisResult:
        db_path = params.get("db_path", settings.DATABASE_PATH)
        conclusions = []
        risk_flags = []

        try:
            conn = sqlite3.connect(db_path)
            ind = StockReader.get_daily_indicators(conn, symbol)
            conn.close()
        except Exception as e:
            return AnalysisResult(
                dimension="technical", ok=False, data={},
                conclusions=[], risk_flags=["数据获取失败"], error=str(e),
            )

        if not ind:
            return AnalysisResult(
                dimension="technical", ok=False, data={},
                conclusions=["无技术指标数据"], risk_flags=["数据缺失"],
            )

        # ── 均线排列 ──
        ma5, ma10, ma20, ma60 = (
            ind.get("ma5", 0) or 0, ind.get("ma10", 0) or 0,
            ind.get("ma20", 0) or 0, ind.get("ma60", 0) or 0,
        )
        if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
            conclusions.append("均线多头排列(MA5>MA10>MA20)，趋势向上")
        elif ma5 and ma10 and ma20 and ma5 < ma10 < ma20:
            risk_flags.append("均线空头排列")
            conclusions.append("均线空头排列(MA5<MA10<MA20)，趋势偏弱")
        else:
            conclusions.append("均线交织，方向不明")

        if ma20 and ma60 and ma20 > ma60:
            conclusions.append("MA20在MA60上方，中期趋势偏多")
        elif ma20 and ma60 and ma20 < ma60:
            risk_flags.append("MA20下穿MA60")

        # ── MACD ──
        macd_dif = ind.get("macd_dif", 0) or 0
        macd_dea = ind.get("macd_dea", 0) or 0
        macd_bar = ind.get("macd_bar", 0) or 0
        if macd_dif > macd_dea:
            conclusions.append(f"MACD日线多头(DIF={macd_dif:.2f}, DEA={macd_dea:.2f})")
        else:
            risk_flags.append("MACD日线空头")
            conclusions.append(f"MACD日线空头(DIF={macd_dif:.2f}, DEA={macd_dea:.2f})")

        # ── RSI ──
        rsi6 = ind.get("rsi6")
        rsi12 = ind.get("rsi12")
        if rsi6 is not None:
            if rsi6 > 80:
                risk_flags.append(f"RSI6超买({rsi6:.0f})")
            elif rsi6 < 30:
                conclusions.append(f"RSI6超卖({rsi6:.0f})，可能反弹")
        if rsi12 is not None:
            if rsi12 > 70:
                risk_flags.append(f"RSI12偏高({rsi12:.0f})")

        # ── KDJ ──
        kdj_j = ind.get("kdj_j")
        if kdj_j is not None:
            if kdj_j > 100:
                risk_flags.append(f"KDJ极度超买(J={kdj_j:.0f})")
            elif kdj_j < 0:
                conclusions.append(f"KDJ极度超卖(J={kdj_j:.0f})，反弹概率高")

        # ── 布林带 ──
        bb_pct_b = ind.get("bb_pct_b")
        if bb_pct_b is not None:
            if bb_pct_b > 90:
                risk_flags.append(f"布林带上轨运行(%B={bb_pct_b:.0f})")
            elif bb_pct_b < 10:
                conclusions.append(f"布林带下轨运行(%B={bb_pct_b:.0f})，超卖区域")

        return AnalysisResult(
            dimension="technical",
            ok=len(risk_flags) == 0,
            data={"indicators": ind},
            conclusions=conclusions,
            risk_flags=risk_flags,
        )
