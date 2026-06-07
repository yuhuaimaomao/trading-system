"""规则审计引擎 — 纯 Python 统计层，不调用 AI

分析维度：
1. 因子胜率 — 每个因子标签的 buy 票实际表现
2. 因子交互 — 多因子组合的胜率
3. 阈值分析 — 因子阈值的敏感性（margin 小的 vs 实际结果）
4. skip 反事实 — skip 的票如果买入会怎样
5. 场景分析 — 按场景标签统计 buy/skip 收益
"""

import json
import sqlite3

from data.repo import TradeRepository
from system.config.settings import DATABASE_PATH
from system.utils.logger import get_audit_logger

logger = get_audit_logger("strategy")


class RuleAuditor:
    """规则层审计：因子胜率、交互、阈值分析"""

    def __init__(self, db_path: str = None):
        self.repo = TradeRepository(db_path=db_path)
        self.db_path = db_path or DATABASE_PATH

    def audit(self, push_date: str) -> list[dict]:
        """执行所有统计维度审计，返回 findings 列表"""
        findings = []

        findings += self._factor_winrate(push_date)
        findings += self._factor_interaction(push_date)
        findings += self._threshold_analysis(push_date)
        findings += self._skip_counterfactual(push_date)
        findings += self._scenario_analysis(push_date)

        logger.info(f"规则审计完成: {len(findings)} 条发现")
        return findings

    # ----------------------------------------------------------------
    # 1. 因子胜率
    # ----------------------------------------------------------------

    def _factor_winrate(self, push_date: str) -> list[dict]:
        """每个因子标签的 buy 票平均涨跌"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT d.stock_code, d.verdict, d.day_change_pct, f.factors_passed
               FROM strategy_ai_decisions d
               JOIN strategy_funnel f ON d.push_date=f.push_date AND d.stock_code=f.stock_code
               WHERE d.push_date=?""",
            (push_date,),
        ).fetchall()
        conn.close()

        if not rows:
            return []

        factor_stats: dict[str, dict] = {}
        for code, verdict, chg, factors_json in rows:
            factors = json.loads(factors_json or "[]")
            for factor in factors:
                if factor not in factor_stats:
                    factor_stats[factor] = {
                        "buy_returns": [],
                        "skip_returns": [],
                        "count": 0,
                    }
                factor_stats[factor]["count"] += 1
                if chg is not None and verdict == "buy":
                    factor_stats[factor]["buy_returns"].append(chg)
                elif chg is not None and verdict == "skip":
                    factor_stats[factor]["skip_returns"].append(chg)

        findings = []
        for factor, stats in factor_stats.items():
            buy_avg = (
                sum(stats["buy_returns"]) / len(stats["buy_returns"])
                if stats["buy_returns"]
                else 0
            )
            skip_avg = (
                sum(stats["skip_returns"]) / len(stats["skip_returns"])
                if stats["skip_returns"]
                else 0
            )

            if stats["buy_returns"] and stats["skip_returns"] and buy_avg < skip_avg:
                findings.append(
                    {
                        "type": "factor_misleading",
                        "severity": "P1",
                        "factor": factor,
                        "buy_avg_return": round(buy_avg, 2),
                        "skip_avg_return": round(skip_avg, 2),
                        "evidence": f"因子「{factor}」: buy票均收益{buy_avg:.1f}% < skip票均收益{skip_avg:.1f}%",
                    }
                )

        return findings

    # ----------------------------------------------------------------
    # 2. 因子交互
    # ----------------------------------------------------------------

    def _factor_interaction(self, push_date: str) -> list[dict]:
        """分析多因子组合的胜率"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT d.verdict, d.day_change_pct, f.factors_passed, f.score
               FROM strategy_ai_decisions d
               JOIN strategy_funnel f ON d.push_date=f.push_date AND d.stock_code=f.stock_code
               WHERE d.push_date=? AND d.day_change_pct IS NOT NULL""",
            (push_date,),
        ).fetchall()
        conn.close()

        if len(rows) < 5:
            return []

        combo_stats: dict[str, dict] = {}
        for verdict, chg, factors_json, score in rows:
            factors = sorted(json.loads(factors_json or "[]"))
            for i in range(len(factors)):
                for j in range(i + 1, len(factors)):
                    combo = f"{factors[i]}+{factors[j]}"
                    if combo not in combo_stats:
                        combo_stats[combo] = {"returns": [], "count": 0}
                    combo_stats[combo]["returns"].append(chg)
                    combo_stats[combo]["count"] += 1

        findings = []
        for combo, stats in combo_stats.items():
            if stats["count"] >= 2:
                avg = sum(stats["returns"]) / len(stats["returns"])
                if avg > 2.0:
                    findings.append(
                        {
                            "type": "combo_effective",
                            "severity": "P2",
                            "combo": combo,
                            "avg_return": round(avg, 2),
                            "count": stats["count"],
                        }
                    )

        return findings

    # ----------------------------------------------------------------
    # 3. 阈值敏感性
    # ----------------------------------------------------------------

    def _threshold_analysis(self, push_date: str) -> list[dict]:
        """分析因子阈值敏感性：margin 小的票实际表现"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT f.stock_code, f.factors_detail, f.score, d.verdict, d.day_change_pct
               FROM strategy_funnel f
               JOIN strategy_ai_decisions d ON f.push_date=d.push_date AND f.stock_code=d.stock_code
               WHERE f.push_date=?""",
            (push_date,),
        ).fetchall()
        conn.close()

        findings = []
        for code, factors_detail_json, score, verdict, chg in rows:
            if not factors_detail_json:
                continue
            try:
                detail = json.loads(factors_detail_json)
            except json.JSONDecodeError:
                continue

            for factor_name, factor_data in detail.items():
                if not isinstance(factor_data, dict):
                    continue
                margin = factor_data.get("margin_pct", 0)
                passed = factor_data.get("passed", True)
                if passed and margin is not None and abs(margin) < 5:
                    if verdict == "buy" and chg is not None and chg < 0:
                        findings.append(
                            {
                                "type": "threshold_sensitive",
                                "severity": "P2",
                                "stock_code": code,
                                "factor": factor_name,
                                "margin_pct": margin,
                                "day_change_pct": chg,
                            }
                        )

        return findings

    # ----------------------------------------------------------------
    # 4. Skip 反事实
    # ----------------------------------------------------------------

    def _skip_counterfactual(self, push_date: str) -> list[dict]:
        """统计 skip 票如果买入的假想收益"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT stock_code, stock_name, skip_reason, day_change_pct
               FROM strategy_ai_decisions
               WHERE push_date=? AND verdict='skip' AND day_change_pct IS NOT NULL""",
            (push_date,),
        ).fetchall()
        conn.close()

        findings = []
        for code, name, reason, chg in rows:
            if chg and chg > 3:
                findings.append(
                    {
                        "type": "skip_missed_gain",
                        "severity": "P1",
                        "stock_code": code,
                        "stock_name": name,
                        "skip_reason": reason,
                        "missed_return": round(chg, 2),
                        "evidence": f"skip {code} {name}（理由: {reason}），当日涨{chg:.1f}%",
                    }
                )
            elif chg and chg < -3:
                findings.append(
                    {
                        "type": "skip_correct",
                        "severity": "P3",
                        "stock_code": code,
                        "stock_name": name,
                        "skip_reason": reason,
                        "avoided_loss": round(abs(chg), 2),
                    }
                )

        return findings

    # ----------------------------------------------------------------
    # 5. 场景分析
    # ----------------------------------------------------------------

    def _scenario_analysis(self, push_date: str) -> list[dict]:
        """按场景标签统计 buy/skip 收益"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT d.verdict, d.day_change_pct, f.scenarios
               FROM strategy_ai_decisions d
               JOIN strategy_funnel f ON d.push_date=f.push_date AND d.stock_code=f.stock_code
               WHERE d.push_date=? AND d.day_change_pct IS NOT NULL""",
            (push_date,),
        ).fetchall()
        conn.close()

        scenario_stats: dict[str, dict] = {}
        for verdict, chg, scenarios_json in rows:
            scenarios = json.loads(scenarios_json or "[]")
            for sc in scenarios:
                if sc not in scenario_stats:
                    scenario_stats[sc] = {
                        "buy_returns": [],
                        "skip_returns": [],
                        "buy_count": 0,
                        "skip_count": 0,
                    }
                if verdict == "buy":
                    scenario_stats[sc]["buy_returns"].append(chg)
                    scenario_stats[sc]["buy_count"] += 1
                else:
                    scenario_stats[sc]["skip_returns"].append(chg)
                    scenario_stats[sc]["skip_count"] += 1

        findings = []
        for sc, stats in scenario_stats.items():
            buy_avg = (
                sum(stats["buy_returns"]) / len(stats["buy_returns"])
                if stats["buy_returns"]
                else 0
            )
            if stats["buy_count"] >= 2:
                f = {
                    "type": "scenario_performance",
                    "severity": "P2",
                    "scenario": sc,
                    "buy_avg_return": round(buy_avg, 2),
                    "buy_count": stats["buy_count"],
                    "skip_count": stats["skip_count"],
                }
                if buy_avg < -1:
                    f["severity"] = "P1"
                    f["evidence"] = f"场景「{sc}」buy票均收益{buy_avg:.1f}%，需要关注"
                findings.append(f)

        return findings
