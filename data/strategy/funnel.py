"""选股漏斗 + AI决策 + 经验教训 + 改进建议 表数据访问

策略线数据层。
"""

from datetime import date

from data._base import BaseRepository, cols_from_str, validate_cols

_FUNNEL_COLS = frozenset(
    {
        "push_date",
        "trade_date",
        "stock_code",
        "stock_name",
        "rank_position",
        "raw_snapshot",
        "factors_passed",
        "factors_detail",
        "scenarios",
        "trend_mode",
        "score",
        "open_price",
        "close_price",
        "day_change_pct",
        "bought",
        "buy_price",
        "day_pnl_pct",
        "created_at",
    }
)

_AI_DECISION_COLS = frozenset(
    {
        "push_date",
        "trade_date",
        "stock_code",
        "stock_name",
        "rank_in_prompt",
        "verdict",
        "confidence",
        "what_i_see",
        "what_concerns_me",
        "decisive_factor",
        "skip_reason",
        "would_reconsider_if",
        "buy_zone_min",
        "buy_zone_max",
        "stop_loss",
        "take_profit",
        "pricing_logic",
        "signal_id",
        "day_change_pct",
        "day_pnl_pct",
        "created_at",
    }
)

_FUNNEL_ALL_COLS = (
    "id, push_date, trade_date, stock_code, stock_name, rank_position, "
    "raw_snapshot, factors_passed, factors_detail, scenarios, trend_mode, "
    "score, open_price, close_price, day_change_pct, bought, buy_price, "
    "day_pnl_pct, created_at"
)

_AI_DECISION_ALL_COLS = (
    "id, push_date, trade_date, stock_code, stock_name, rank_in_prompt, "
    "verdict, confidence, what_i_see, what_concerns_me, decisive_factor, "
    "skip_reason, would_reconsider_if, buy_zone_min, buy_zone_max, "
    "stop_loss, take_profit, pricing_logic, signal_id, day_change_pct, "
    "day_pnl_pct, created_at"
)

_IMPROVEMENT_ALL_COLS = (
    "id, push_date, improvement_type, target_module, target_param, "
    "suggested_change, code_diff, rationale, evidence_ids, status, "
    "applied_date, effectiveness_check, created_at"
)


class StrategyRepo(BaseRepository):
    """strategy_funnel + strategy_ai_decisions + strategy_lessons + strategy_improvements"""

    # -- funnel --

    def insert_funnel_batch(self, rows: list[dict]):
        if not rows:
            return
        cols = list(rows[0].keys())
        validate_cols(_FUNNEL_COLS, cols)
        col_str = ", ".join(cols)
        placeholders = ", ".join(["?" for _ in cols])
        sql = f"INSERT INTO strategy_funnel ({col_str}) VALUES ({placeholders})"
        with self._conn() as conn:
            for r in rows:
                conn.execute(sql, [r.get(c) for c in cols])
            conn.commit()

    def backfill_funnel_close(self, trade_date: str, updates: list[dict]):
        with self._conn() as conn:
            for u in updates:
                conn.execute(
                    "UPDATE strategy_funnel SET close_price=?, day_change_pct=?, "
                    "open_price=?, bought=?, buy_price=?, day_pnl_pct=? "
                    "WHERE push_date=? AND stock_code=?",
                    (
                        u.get("close_price"),
                        u.get("day_change_pct"),
                        u.get("open_price"),
                        u.get("bought", 0),
                        u.get("buy_price"),
                        u.get("day_pnl_pct"),
                        trade_date,
                        u["stock_code"],
                    ),
                )
            conn.commit()

    def get_funnel_records(self, push_date: str) -> list[dict]:
        cols = cols_from_str(_FUNNEL_ALL_COLS.replace(" ", ""))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {_FUNNEL_ALL_COLS} FROM strategy_funnel WHERE push_date=? ORDER BY rank_position",
                (push_date,),
            ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    # -- ai_decisions --

    def insert_ai_decisions_batch(self, decisions: list[dict]):
        if not decisions:
            return
        cols = list(decisions[0].keys())
        validate_cols(_AI_DECISION_COLS, cols)
        col_str = ", ".join(cols)
        placeholders = ", ".join(["?" for _ in cols])
        sql = f"INSERT INTO strategy_ai_decisions ({col_str}) VALUES ({placeholders})"
        with self._conn() as conn:
            for d in decisions:
                conn.execute(sql, [d.get(c) for c in cols])
            conn.commit()

    def get_ai_decisions(self, push_date: str, verdict: str = None) -> list[dict]:
        cols = cols_from_str(_AI_DECISION_ALL_COLS.replace(" ", ""))
        with self._conn() as conn:
            if verdict:
                rows = conn.execute(
                    f"SELECT {_AI_DECISION_ALL_COLS} FROM strategy_ai_decisions "
                    "WHERE push_date=? AND verdict=? ORDER BY rank_in_prompt",
                    (push_date, verdict),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_AI_DECISION_ALL_COLS} FROM strategy_ai_decisions "
                    "WHERE push_date=? ORDER BY rank_in_prompt",
                    (push_date,),
                ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def backfill_ai_decisions_close(self, trade_date: str, updates: list[dict]):
        with self._conn() as conn:
            for u in updates:
                conn.execute(
                    "UPDATE strategy_ai_decisions SET day_change_pct=?, day_pnl_pct=? "
                    "WHERE push_date=? AND stock_code=?",
                    (
                        u.get("day_change_pct"),
                        u.get("day_pnl_pct"),
                        trade_date,
                        u["stock_code"],
                    ),
                )
            conn.commit()

    # -- improvements --

    def insert_improvement(self, imp_dict: dict) -> int:
        return self._insert("strategy_improvements", imp_dict, frozenset(imp_dict.keys()))

    def get_pending_improvements(self) -> list[dict]:
        cols = cols_from_str(_IMPROVEMENT_ALL_COLS.replace(" ", ""))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {_IMPROVEMENT_ALL_COLS} FROM strategy_improvements WHERE status='pending' ORDER BY id",
            ).fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def apply_improvement(self, imp_id: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE strategy_improvements SET status='applied', applied_date=? WHERE id=?",
                (date.today().isoformat(), imp_id),
            )
            conn.commit()
