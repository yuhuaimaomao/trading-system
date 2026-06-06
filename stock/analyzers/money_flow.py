"""资金面分析器 — 主力资金/北向/龙虎榜/大单流向。"""

import sqlite3

from data.readers.stock_reader import StockReader
from stock.analyzers import BaseAnalyzer
from stock.stock_schemas import AnalysisResult
from system.config import settings


class MoneyFlowAnalyzer(BaseAnalyzer):
    """资金面分析：主力资金、北向资金、龙虎榜。"""

    name = "money_flow"

    def analyze(self, symbol: str, **params) -> AnalysisResult:
        db_path = params.get("db_path", settings.DATABASE_PATH)
        conclusions = []
        risk_flags = []

        try:
            conn = sqlite3.connect(db_path)
            mf = StockReader.get_money_flow(conn, symbol)
            conn.close()
        except Exception as e:
            return AnalysisResult(
                dimension="money_flow",
                ok=False,
                data={},
                conclusions=[],
                risk_flags=["数据获取失败"],
                error=str(e),
            )

        if not mf:
            return AnalysisResult(
                dimension="money_flow",
                ok=False,
                data={},
                conclusions=["无资金流数据"],
                risk_flags=["数据缺失"],
            )

        # ── 主力资金 ──
        mf_net = mf.get("main_force_net", 0) or 0
        mf_ratio = mf.get("main_force_ratio", 0) or 0
        sl_net = mf.get("super_large_net", 0) or 0
        l_net = mf.get("large_net", 0) or 0

        if mf_net > 0:
            if mf_ratio > 5:
                conclusions.append(
                    f"主力大幅流入{mf_net / 1e4:.0f}万（占比{mf_ratio:.1f}%）"
                )
            elif mf_ratio > 2:
                conclusions.append(
                    f"主力温和流入{mf_net / 1e4:.0f}万（占比{mf_ratio:.1f}%）"
                )
            else:
                conclusions.append(f"主力小幅流入{mf_net / 1e4:.0f}万")
        elif mf_net < 0:
            if mf_ratio < -5:
                risk_flags.append(
                    f"主力大幅流出{abs(mf_net) / 1e4:.0f}万（占比{abs(mf_ratio):.1f}%）"
                )
            elif mf_ratio < -2:
                risk_flags.append(
                    f"主力温和流出{abs(mf_net) / 1e4:.0f}万（占比{abs(mf_ratio):.1f}%）"
                )
            conclusions.append(f"主力净流出{abs(mf_net) / 1e4:.0f}万")
        else:
            conclusions.append("主力资金基本平衡")

        # ── 超大单 vs 大单 ──
        if sl_net > 0 and l_net > 0:
            conclusions.append("超大单和大单同步流入，机构建仓信号")
        elif sl_net > 0 > l_net:
            conclusions.append("超大单流入但大单流出，可能是机构对倒")
        elif sl_net < 0 < l_net:
            conclusions.append("超大单流出但大单流入，游资接盘机构出货")

        # ── MA5 斜率（资金趋势配合）──
        ma5_angle = mf.get("ma5_angle", 0) or 0
        if ma5_angle > 2:
            conclusions.append(f"MA5加速上行(角{ma5_angle:.1f}°)，资金推动趋势向上")
        elif ma5_angle < -2:
            risk_flags.append(f"MA5加速下行(角{ma5_angle:.1f}°)，资金出逃趋势向下")

        # ── 市值 ──
        circ_cap = mf.get("circ_market_cap", 0) or 0
        if circ_cap > 0:
            cap_yi = circ_cap / 1e8
            if cap_yi > 1000:
                conclusions.append(
                    f"流通市值{cap_yi:.0f}亿，大盘股，资金推动需较大成交量"
                )
            elif cap_yi > 100:
                conclusions.append(f"流通市值{cap_yi:.0f}亿，中盘股")
            else:
                conclusions.append(f"流通市值{cap_yi:.0f}亿，小盘股，资金敏感度高")

        return AnalysisResult(
            dimension="money_flow",
            ok=len(risk_flags) == 0,
            data={"money_flow": mf},
            conclusions=conclusions,
            risk_flags=risk_flags,
        )
