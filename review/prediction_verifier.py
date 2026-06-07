"""预测核验器 — 收盘对比 AI 预测 vs 次日实际，写入 is_correct 形成闭环。"""

import re
import sqlite3
from datetime import datetime
from typing import Optional

from system.config.settings import DATABASE_PATH
from system.config.trading_calendar import get_next_trading_day
from system.utils.logger import get_review_logger

logger = get_review_logger("tracker")

# 指数名称 → index_realtime_data 代码
INDEX_CODE_MAP = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "上证50": "sh000016",
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "中证1000": "sh000852",
    "科创50": "sh000688",
}

# ── 各指数方向阈值 ──────────────────────────────────────────────
# 格式: {方向标签: (chg_lo, chg_hi, 额外条件)}
# 额外条件: "yang"=阳线(close>open), "yin"=阴线(close<open), None=不需要
# 上证/深证/沪深300等大盘指数波动小，创业板/科创波动大
_EXTRA = {
    "yang": lambda c, o: c > o,
    "yin": lambda c, o: c < o,
    None: lambda c, o: True,
}

# (chg_lo, chg_hi, extra_key) — chg_lo ≤ 实际涨跌幅 ≤ chg_hi
_INDEX_THRESHOLDS = {
    "上证指数": {
        "偏多": (0, 0.3, None),
        "偏弱": (-0.3, 0, None),
        "震荡偏多": (0, 0.8, None),
        "震荡偏空": (-0.8, 0, None),
        "单边上涨": (0.8, 99, "yang"),
        "单边下跌": (-99, -0.8, "yin"),
    },
    "深证成指": {
        "偏多": (0, 0.5, None),
        "偏弱": (-0.5, 0, None),
        "震荡偏多": (0, 1.0, None),
        "震荡偏空": (-1.0, 0, None),
        "单边上涨": (1.0, 99, "yang"),
        "单边下跌": (-99, -1.0, "yin"),
    },
    "创业板指": {
        "偏多": (0, 1.0, None),
        "偏弱": (-1.0, 0, None),
        "震荡偏多": (0, 1.5, None),
        "震荡偏空": (-1.5, 0, None),
        "单边上涨": (1.5, 99, "yang"),
        "单边下跌": (-99, -1.5, "yin"),
        "暴跌": (-99, -3.0, "yin"),
    },
    "科创50": {
        "偏多": (0, 1.0, None),
        "偏弱": (-1.0, 0, None),
        "震荡偏多": (0, 1.5, None),
        "震荡偏空": (-1.5, 0, None),
        "单边上涨": (1.5, 99, "yang"),
        "单边下跌": (-99, -1.5, "yin"),
        "暴跌": (-99, -3.0, "yin"),
    },
}

# 模糊关键词 → 按指数查找对应的精确标签
_DIRECTION_ALIAS = {
    "偏多": "偏多",
    "偏强": "偏多",
    "偏空": "偏弱",
    "偏弱": "偏弱",
    "上涨": "单边上涨",
    "下跌": "单边下跌",
    "震荡": "震荡偏多",  # 先试偏多范围，不行试偏空
}
_DEFAULT_THRESHOLD = _INDEX_THRESHOLDS["上证指数"]

# 板块方向判定规则
SECTOR_DIRECTION_RULES = {
    "主线延续": lambda chg, rank, up, down: chg > 0 and (rank is None or rank <= 10),
    "分歧后回流": lambda chg, rank, up, down: chg > 0,
    "一日游风险": lambda chg, rank, up, down: chg < 0
    or (rank is not None and rank > 15),
    "新方向发酵": lambda chg, rank, up, down: chg > 0,
    "退潮": lambda chg, rank, up, down: chg < 0,
}

# 板块方向模糊匹配
SECTOR_DIRECTION_KEYWORDS = [
    ("延续", lambda chg, r, u, d: chg > 0),
    ("回流", lambda chg, r, u, d: chg > 0),
    ("一日游", lambda chg, r, u, d: chg < 0 or (r is not None and r > 10)),
    ("退潮", lambda chg, r, u, d: chg < 0),
    ("发酵", lambda chg, r, u, d: chg > 0),
]


class PredictionVerifier:
    """收盘后核验复盘预测 vs 次日实际市场数据"""

    def verify(self, push_date: str) -> dict:
        """核验指定日期所有未核验预测。返回 dict(total/correct/incorrect/accuracy/details/...)."""
        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.row_factory = sqlite3.Row

        next_date = get_next_trading_day(push_date)
        if not next_date:
            conn.close()
            logger.error(f"❌ 无法找到 {push_date} 的下一个交易日")
            return self._empty_report("无法找到下一个交易日")

        # 拉取未核验预测
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM review_predictions WHERE push_date = ? AND checked_date IS NULL",
                (push_date,),
            ).fetchall()
        ]

        if not rows:
            conn.close()
            logger.info(f"✅ {push_date} 无待核验预测")
            return self._empty_report(None)

        logger.info(
            f"开始核验 {push_date} 的 {len(rows)} 条预测（对比 {next_date} 实际数据）…"
        )

        details = []
        correct = 0
        incorrect = 0
        unmatched = 0
        unmatched_sectors = []
        idx_correct = 0
        idx_total = 0
        sec_correct = 0
        sec_total = 0

        for pred in rows:
            ptype = pred["pred_type"]

            if ptype == "index":
                actual, ok = self._verify_index(conn, pred, next_date)
                idx_total += 1
                if ok:
                    idx_correct += 1
            elif ptype == "sector":
                actual, ok = self._verify_sector(conn, pred, next_date)
                sec_total += 1
                if ok:
                    sec_correct += 1
                if not ok and "均未找到" in actual:
                    unmatched += 1
                    unmatched_sectors.append(pred["target_name"])
            elif ptype == "scenario":
                actual, ok = self._verify_scenario(
                    conn,
                    pred,
                    next_date,
                    idx_correct,
                    sec_correct,
                    idx_total,
                    sec_total,
                )
            else:
                actual = f"未知预测类型: {ptype}"
                ok = False

            if ok:
                correct += 1
            else:
                incorrect += 1

            # UPDATE 回 DB
            conn.execute(
                "UPDATE review_predictions SET actual_result=?, is_correct=?, checked_date=? WHERE id=?",
                (
                    actual,
                    1 if ok else 0,
                    datetime.now().strftime("%Y-%m-%d"),
                    pred["id"],
                ),
            )

            details.append(
                {
                    "id": pred["id"],
                    "pred_type": ptype,
                    "target_name": pred["target_name"],
                    "pred_direction": pred["pred_direction"],
                    "actual_result": actual,
                    "is_correct": ok,
                }
            )

            icon = "✅" if ok else "❌"
            logger.info(f"  {icon} [{ptype}] {pred['target_name']}: {actual}")

        conn.commit()
        conn.close()

        total_verifiable = correct + incorrect - unmatched
        accuracy = (correct / total_verifiable * 100) if total_verifiable > 0 else 0

        report = {
            "push_date": push_date,
            "checked_date": next_date,
            "total": len(rows),
            "correct": correct,
            "incorrect": incorrect,
            "unmatched": unmatched,
            "index_count": idx_total,
            "sector_count": sec_total,
            "scenario_count": sum(1 for r in rows if r["pred_type"] == "scenario"),
            "accuracy": round(accuracy, 1),
            "details": details,
            "unmatched_sectors": unmatched_sectors,
            "error": None,
        }

        logger.info(
            f"✅ 核验完成: {correct}/{total_verifiable} 正确 ({accuracy:.1f}%), "
            f"未匹配 {unmatched} 条"
        )
        return report

    # ------------------------------------------------------------------
    # 指数核验
    # ------------------------------------------------------------------

    def _verify_index(self, conn, pred: dict, next_date: str) -> tuple[str, bool]:
        target = pred["target_name"]  # e.g. "上证指数"
        direction = pred.get("pred_direction", "")
        detail = pred.get("pred_detail", "")

        index_code = INDEX_CODE_MAP.get(target)
        if not index_code:
            return (f"未知指数「{target}」", False)

        row = conn.execute(
            """SELECT close_price, open_price, high_price, low_price, change_percent
               FROM index_realtime_data
               WHERE index_code = ? AND trade_date = ? LIMIT 1""",
            (index_code, next_date),
        ).fetchone()

        if not row:
            return (f"{next_date} 无 {target} 数据", False)

        close = row["close_price"] or 0
        open_ = row["open_price"] or 0
        high = row["high_price"] or 0
        low = row["low_price"] or 0
        change_pct = row["change_percent"] or 0

        # 方向判定
        dir_ok = self._match_index_direction(
            direction, change_pct, close, open_, high, low, target
        )

        # 支撑/压力判定
        support, resistance = self._parse_support_resistance(detail)
        support_ok = True
        resistance_ok = True
        if support is not None:
            support_ok = low >= support * 0.99
        if resistance is not None:
            resistance_ok = high <= resistance * 1.01

        is_correct = dir_ok and support_ok and resistance_ok

        # 格式化结果
        chg_word = "涨" if change_pct >= 0 else "跌"
        parts = [f"{target}次日{chg_word}{abs(change_pct):+.2f}%"]

        actual_label = self._classify_actual(
            change_pct, close, open_, high, low, target
        )
        parts.append(f"实际={actual_label}")

        if support is not None:
            if support_ok:
                parts.append(f"最低{low:.0f}未破支撑{support:.0f}")
            else:
                pct_break = (low - support) / support * 100
                parts.append(f"最低{low:.0f}跌破支撑{support:.0f}({pct_break:+.1f}%)")

        if resistance is not None:
            if resistance_ok:
                parts.append(f"最高{high:.0f}未触压力{resistance:.0f}")
            else:
                pct_over = (high - resistance) / resistance * 100
                parts.append(
                    f"最高{high:.0f}突破压力{resistance:.0f}({pct_over:+.1f}%)"
                )

        if not dir_ok:
            parts.append(f"方向预测[{direction}]与实际[{actual_label}]不符")

        return ("，".join(parts), is_correct)

    # ------------------------------------------------------------------
    # 板块核验
    # ------------------------------------------------------------------

    def _verify_sector(self, conn, pred: dict, next_date: str) -> tuple[str, bool]:
        target = pred["target_name"]
        direction = pred.get("pred_direction", "")

        sector = self._find_sector(conn, target, next_date)
        if not sector:
            return (f"板块「{target}」在行业/概念表中均未找到", False)

        change_pct = sector["change_percent"] or 0
        rank = sector.get("rank")
        up_count = sector.get("up_count", 0)
        down_count = sector.get("down_count", 0)
        sname = sector["sector_name"]
        source = sector["_source_table"]

        is_correct = self._check_sector_direction(
            direction, change_pct, rank, up_count, down_count
        )

        chg_word = "涨" if change_pct >= 0 else "跌"
        rank_str = f"排名第{rank}" if rank is not None else "排名未知"
        source_label = "行业" if source == "sector_industry" else "概念"

        result = (
            f"{sname}({source_label})次日{chg_word}{abs(change_pct):+.1f}%，{rank_str}"
        )
        if not is_correct:
            result += f"，预测[{direction}]与实际不符"

        return (result, is_correct)

    # ------------------------------------------------------------------
    # 主导情景核验
    # ------------------------------------------------------------------

    def _verify_scenario(
        self,
        conn,
        pred: dict,
        next_date: str,
        idx_correct: int,
        sec_correct: int,
        idx_total: int,
        sec_total: int,
    ) -> tuple[str, bool]:
        direction = pred.get("pred_direction", "")

        row = conn.execute(
            "SELECT change_percent FROM index_realtime_data "
            "WHERE index_code = 'sh000001' AND trade_date = ? LIMIT 1",
            (next_date,),
        ).fetchone()

        if not row:
            return (f"{next_date} 无上证数据", False)

        sh_change = row["change_percent"] or 0

        is_correct = False
        if (
            "主线延续" in direction
            and sh_change > 0
            or "退潮" in direction
            and sh_change < 0
        ):
            is_correct = True
        else:
            # 综合判定：板块准确率 >= 60%
            total_sec = sec_total or 1
            sec_acc = sec_correct / total_sec
            if sec_acc >= 0.6:
                is_correct = True

        chg_word = "涨" if sh_change >= 0 else "跌"
        result = (
            f"主导情景[{direction}]：上证次日{chg_word}{abs(sh_change):+.2f}%"
            f"，板块核验{sec_correct}/{sec_total}"
        )

        return (result, is_correct)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_support_resistance(
        detail: str,
    ) -> tuple[Optional[float], Optional[float]]:
        """从 pred_detail 解析支撑位和压力位。格式: '支撑3340/压力3400'"""
        if not detail:
            return (None, None)
        support = None
        resistance = None
        sm = re.search(r"支撑(\d+\.?\d*)", detail)
        if sm:
            support = float(sm.group(1))
        rm = re.search(r"压力(\d+\.?\d*)", detail)
        if rm:
            resistance = float(rm.group(1))
        return (support, resistance)

    @staticmethod
    def _get_thresholds(index_name: str) -> dict:
        """获取某指数的方向阈值表，未定义的指数用上证阈值。"""
        return _INDEX_THRESHOLDS.get(index_name, _DEFAULT_THRESHOLD)

    @staticmethod
    def _match_index_direction(
        pred_dir: str,
        change_pct: float,
        close: float,
        open_: float,
        high: float,
        low: float,
        index_name: str,
    ) -> bool:
        """判断预测方向是否与实际匹配。先精确匹配标签，再通过 alias 模糊匹配。"""
        if not pred_dir:
            return False
        thresholds = PredictionVerifier._get_thresholds(index_name)

        # 尝试直接匹配
        rule = thresholds.get(pred_dir)
        if not rule:
            # 通过别名映射 → 真实标签
            for kw, label in _DIRECTION_ALIAS.items():
                if kw in pred_dir:
                    rule = thresholds.get(label)
                    break
        if not rule:
            return False

        lo, hi, extra_key = rule
        if not (lo <= change_pct <= hi):
            return False
        return _EXTRA[extra_key](close, open_)

    @staticmethod
    def _classify_actual(
        change_pct: float,
        close: float,
        open_: float,
        high: float,
        low: float,
        index_name: str,
    ) -> str:
        """将实际走势归类为方向标签（按指数阈值）。"""
        thresholds = PredictionVerifier._get_thresholds(index_name)
        # 按优先级: 暴跌 > 单边 > 震荡偏 > 偏
        for label in (
            "暴跌",
            "单边上涨",
            "单边下跌",
            "震荡偏多",
            "震荡偏空",
            "偏多",
            "偏弱",
        ):
            rule = thresholds.get(label)
            if (
                rule
                and rule[0] <= change_pct <= rule[1]
                and _EXTRA[rule[2]](close, open_)
            ):
                return label
        chg_word = "涨" if change_pct >= 0 else "跌"
        return f"{chg_word}{abs(change_pct):.1f}%"

    @staticmethod
    def _check_sector_direction(
        direction: str,
        change_pct: float,
        rank: Optional[int],
        up_count: int,
        down_count: int,
    ) -> bool:
        """判断板块预测方向是否与实际匹配。"""
        if not direction:
            return False

        rule = SECTOR_DIRECTION_RULES.get(direction)
        if rule:
            return rule(change_pct, rank, up_count, down_count)

        # 模糊匹配
        for keyword, fn in SECTOR_DIRECTION_KEYWORDS:
            if keyword in direction:
                return fn(change_pct, rank, up_count, down_count)

        return False

    @staticmethod
    def _find_sector(conn, target_name: str, trade_date: str) -> Optional[dict]:
        """
        在 sector_industry 和 sector_concept 表中按名称匹配板块。
        精确匹配优先，其次模糊匹配；两表同时存在时取 abs(change_pct) 更大者。
        """
        best = None

        for table in ("sector_industry", "sector_concept"):
            # 精确匹配
            row = conn.execute(
                f"SELECT sector_name, change_percent, rank, up_count, down_count "
                f"FROM {table} WHERE trade_date = ? AND sector_name = ? LIMIT 1",
                (trade_date, target_name),
            ).fetchone()
            if row:
                d = dict(row)
                d["_source_table"] = table
                d["_match_type"] = "exact"
                best = PredictionVerifier._pick_best_sector(best, d)
                continue  # 精确匹配了就不要模糊匹配同一表

            # 模糊匹配
            rows = conn.execute(
                f"SELECT sector_name, change_percent, rank, up_count, down_count "
                f"FROM {table} WHERE trade_date = ? AND sector_name LIKE ?",
                (trade_date, f"%{target_name}%"),
            ).fetchall()
            for row in rows:
                d = dict(row)
                d["_source_table"] = table
                d["_match_type"] = "fuzzy"
                best = PredictionVerifier._pick_best_sector(best, d)

        return best

    @staticmethod
    def _pick_best_sector(current: Optional[dict], candidate: dict) -> dict:
        """在多个板块匹配结果中选择最佳：精确 > 模糊，同级别取 abs(change_pct) 更大。"""
        if current is None:
            return candidate

        # 精确优先
        if candidate["_match_type"] == "exact" and current["_match_type"] != "exact":
            return candidate
        if current["_match_type"] == "exact" and candidate["_match_type"] != "exact":
            return current

        # 同级别取变化更大的
        cur_chg = abs(current.get("change_percent") or 0)
        cand_chg = abs(candidate.get("change_percent") or 0)
        if cand_chg > cur_chg:
            return candidate
        return current

    @staticmethod
    def _empty_report(error: Optional[str]) -> dict:
        return {
            "push_date": None,
            "checked_date": None,
            "total": 0,
            "correct": 0,
            "incorrect": 0,
            "unmatched": 0,
            "index_count": 0,
            "sector_count": 0,
            "scenario_count": 0,
            "accuracy": 0.0,
            "details": [],
            "unmatched_sectors": [],
            "error": error,
        }
