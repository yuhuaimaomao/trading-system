"""板块归因分析器 — 板块归属/相关性/强度。"""

import sqlite3

from analysis.stock.analyzers import BaseAnalyzer
from analysis.stock.schemas import AnalysisResult
from system.config import settings


class SectorAttrAnalyzer(BaseAnalyzer):
    """板块归因：所属行业+概念板块的强度和趋势。"""

    name = "sector_attr"

    def analyze(self, symbol: str, **params) -> AnalysisResult:
        db_path = params.get("db_path", settings.DATABASE_PATH)
        conclusions = []
        risk_flags = []

        try:
            conn = sqlite3.connect(db_path)
            # 查行业和概念
            row = conn.execute(
                """SELECT industry, concepts FROM stock_basic
                   WHERE stock_code=? ORDER BY trade_date DESC LIMIT 1""",
                (symbol,),
            ).fetchone()
            conn.close()
        except Exception as e:
            return AnalysisResult(
                dimension="sector_attr", ok=False, data={},
                conclusions=[], risk_flags=["数据获取失败"], error=str(e),
            )

        if not row:
            return AnalysisResult(
                dimension="sector_attr", ok=False, data={},
                conclusions=["无板块数据"], risk_flags=["数据缺失"],
            )

        industry = row[0] or ""
        concepts_raw = row[1] or ""

        # 解析概念（逗号或竖线分隔）
        concepts = [c.strip() for c in concepts_raw.replace("|", ",").split(",") if c.strip()]

        data = {
            "industry": industry,
            "concepts": concepts,
        }

        # ── 行业 ──
        if industry:
            conclusions.append(f"所属行业：{industry}")
        else:
            risk_flags.append("无行业分类")

        # ── 概念板块 ──
        if concepts:
            if len(concepts) <= 3:
                conclusions.append(f"概念板块：{'、'.join(concepts)}")
            else:
                conclusions.append(f"概念板块：{'、'.join(concepts[:3])}等{len(concepts)}个")
                conclusions.append(f"概念覆盖广，主题催化概率高")
        else:
            risk_flags.append("无概念板块归属，缺乏主题催化")

        # ── 概念集中度 ──
        if len(concepts) >= 5:
            conclusions.append("概念标签多（≥5个），板块轮动时受益面广")
        elif len(concepts) <= 1:
            risk_flags.append("概念单一，板块轮动时缺乏弹性")

        return AnalysisResult(
            dimension="sector_attr",
            ok=len(risk_flags) == 0,
            data=data,
            conclusions=conclusions,
            risk_flags=risk_flags,
        )
