"""
review_predictions 表数据访问

复盘线 — 预测记录 CRUD + 验证。
"""


class PredictionRepo:
    """review_predictions 表 CRUD（静态方法，传入 conn）"""

    @staticmethod
    def insert_index_prediction(
        conn,
        push_date: str,
        target_name: str,
        pred_direction: str,
        pred_detail: str,
        prob: float,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO review_predictions "
            "(push_date, pred_type, target_name, pred_direction, pred_detail, prob) "
            "VALUES (?, 'index', ?, ?, ?, ?)",
            (push_date, target_name, pred_direction, pred_detail, prob),
        )
        return cursor.lastrowid

    @staticmethod
    def insert_sector_prediction(
        conn,
        push_date: str,
        target_name: str,
        pred_direction: str,
        prob: float,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO review_predictions "
            "(push_date, pred_type, target_name, pred_direction, pred_detail, prob) "
            "VALUES (?, 'sector', ?, ?, '', ?)",
            (push_date, target_name, pred_direction, prob),
        )
        return cursor.lastrowid

    @staticmethod
    def insert_scenario_prediction(
        conn,
        push_date: str,
        pred_direction: str,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO review_predictions "
            "(push_date, pred_type, target_name, pred_direction, pred_detail, prob) "
            "VALUES (?, 'scenario', '主导情景', ?, '第五节日均推演', NULL)",
            (push_date, pred_direction),
        )
        return cursor.lastrowid

    @staticmethod
    def get_unverified(conn, push_date: str) -> list:
        """查询某日未验证的预测。"""
        rows = conn.execute(
            "SELECT * FROM review_predictions WHERE push_date = ? AND checked_date IS NULL",
            (push_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def update_verification(
        conn,
        pred_id: int,
        actual_result: float,
        is_correct: int,
        checked_date: str,
    ):
        conn.execute(
            "UPDATE review_predictions SET actual_result=?, is_correct=?, checked_date=? WHERE id=?",
            (actual_result, is_correct, checked_date, pred_id),
        )

    @staticmethod
    def get_recent_correct(conn, limit: int = 100) -> list:
        """查询最近已验证的预测。"""
        rows = conn.execute(
            "SELECT pred_type, target_name, pred_direction, prob, is_correct "
            "FROM review_predictions "
            "WHERE push_date < ? AND is_correct IS NOT NULL "
            "ORDER BY push_date DESC LIMIT ?",
            ("2099-01-01", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_active_lessons(conn) -> list:
        """查询活跃经验教训。"""
        rows = conn.execute(
            "SELECT id, lesson_type, lesson_key, lesson_content, "
            "occurrence_count, first_date, last_date "
            "FROM review_lessons WHERE is_active=1 "
            "ORDER BY lesson_type, occurrence_count DESC",
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def upsert_lesson(
        conn,
        lesson_type: str,
        lesson_key: str,
        lesson_content: str,
        trade_date: str,
    ) -> int:
        """更新或插入经验教训。返回 id。"""
        existing = conn.execute(
            "SELECT id, occurrence_count FROM review_lessons WHERE lesson_type=? AND lesson_key=?",
            (lesson_type, lesson_key),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE review_lessons "
                "SET occurrence_count=occurrence_count+1, "
                "last_date=?, lesson_content=?, is_active=1 "
                "WHERE lesson_type=? AND lesson_key=?",
                (trade_date, lesson_content, lesson_type, lesson_key),
            )
            return existing[0]
        else:
            cur = conn.execute(
                "INSERT INTO review_lessons "
                "(lesson_type, lesson_key, lesson_content, "
                "occurrence_count, first_date, last_date, is_active) "
                "VALUES (?, ?, ?, 1, ?, ?, 1)",
                (lesson_type, lesson_key, lesson_content, trade_date, trade_date),
            )
            return cur.lastrowid
