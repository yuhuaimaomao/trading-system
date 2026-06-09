"""
stock_tracker 表数据访问

复盘线 — 早报/复盘股票追踪记录 CRUD。
"""

from system.utils.logger import get_system_logger

logger = get_system_logger("data")


class TrackerRepo:
    """stock_tracker 表 CRUD（静态方法，传入 conn）"""

    @staticmethod
    def insert(conn, push_date: str, code: str, stock: dict, source: str) -> int:
        """插入追踪记录，返回 rowcount（0=已存在跳过）。"""
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO stock_tracker (
                push_date, stock_code, stock_name, plate, star_rating,
                market_cap, reason_keywords, source,
                sector_code, abandon_condition, stop_loss, target_price
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                push_date,
                code,
                stock.get("股票名称", ""),
                stock.get("所属板块", ""),
                stock.get("star_rating", 0),
                stock.get("market_cap", 0),
                stock.get("推荐理由", ""),
                source,
                stock.get("sector_code", ""),
                stock.get("放弃条件", ""),
                stock.get("止损位", ""),
                stock.get("目标位", ""),
            ),
        )
        return cursor.rowcount

    @staticmethod
    def get_by_push_date(conn, push_date: str) -> list:
        """查询某推送日期的追踪记录。"""
        rows = conn.execute(
            "SELECT stock_code, source, push_date FROM stock_tracker WHERE push_date = ?",
            (push_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_by_push_date_and_source(conn, push_date: str, source: str) -> list:
        """查询某推送日期+来源的追踪记录。"""
        rows = conn.execute(
            "SELECT stock_code, source, push_date FROM stock_tracker WHERE push_date = ? AND source = ?",
            (push_date, source),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_stale_review_records(conn, push_date: str) -> list:
        """查询未填充 t_open 的复盘记录（追赶用）。"""
        rows = conn.execute(
            "SELECT stock_code, source, push_date FROM stock_tracker "
            "WHERE push_date = ? AND source = '复盘' AND t_open IS NULL",
            (push_date,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_by_push_date_full(conn, push_date: str) -> dict:
        """查询某日推送记录含 t_open/t_close。返回 {key: row_dict}。"""
        cursor = conn.execute(
            "SELECT stock_code, source, t_open, t_close FROM stock_tracker WHERE push_date = ?",
            (push_date,),
        )
        result = {}
        for row in cursor.fetchall():
            key = f"{row['stock_code']}|{push_date}"
            result[key] = dict(row)
            result[key]["_push_date"] = push_date
        return result

    @staticmethod
    def update_daily_data(
        conn,
        push_date: str,
        code: str,
        data: dict,
        is_limit_up: int,
    ) -> int:
        """更新当日行情数据。返回 rowcount。"""
        cursor = conn.execute(
            """
            UPDATE stock_tracker
            SET t_open = ?, t_close = ?, t_prev_close = ?,
                t_change_pct = ?, t_open_pct = ?, t_intra_diff = ?,
                is_limit_up = ?, updated_at = CURRENT_TIMESTAMP
            WHERE push_date = ? AND stock_code = ?
        """,
            (
                data["open"],
                data["close"],
                data.get("prev_close"),
                data.get("change_pct"),
                data.get("t_open_pct", 0),
                data.get("t_intra_diff", 0),
                is_limit_up,
                push_date,
                code,
            ),
        )
        return cursor.rowcount

    @staticmethod
    def update_next_day_data(
        conn,
        push_date: str,
        code: str,
        t1_data: dict,
    ) -> int:
        """更新次日表现数据。返回 rowcount。"""
        cursor = conn.execute(
            """
            UPDATE stock_tracker
            SET t1_open = ?, t1_open_pct = ?, t1_avg_price = ?,
                final_return = ?,
                t1_close_return = ?, t1_high_return = ?, t1_low_return = ?,
                avg_price_return = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE push_date = ? AND stock_code = ?
        """,
            (
                t1_data.get("open"),
                t1_data.get("t1_open_pct", 0),
                t1_data.get("avg_price", 0),
                t1_data.get("final_return", 0),
                t1_data.get("t1_close_return", 0),
                t1_data.get("t1_high_return", 0),
                t1_data.get("t1_low_return", 0),
                t1_data.get("avg_price_return", 0),
                push_date,
                code,
            ),
        )
        return cursor.rowcount

    @staticmethod
    def get_review_picks_latest(conn) -> list:
        """查询最新复盘推荐标的。"""
        rows = conn.execute(
            "SELECT stock_code, stock_name, stop_loss, target_price, abandon_condition "
            "FROM stock_tracker WHERE push_date = ("
            "SELECT MAX(push_date) FROM stock_tracker WHERE source='复盘')"
        ).fetchall()
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

    @staticmethod
    def get_stars(conn, code: str) -> dict | None:
        """查询某股最新星级。"""
        row = conn.execute(
            "SELECT star_rating, plate FROM stock_tracker WHERE stock_code = ? ORDER BY push_date DESC LIMIT 1",
            (code,),
        ).fetchone()
        if not row:
            return None
        return {"star_rating": row[0] or 0, "plate": row[1] or ""}

    @staticmethod
    def get_statistics(conn, start_date: str = None, end_date: str = None) -> dict:
        """获取全面统计数据（基础 + 板块 + 强度维度）。"""
        query = """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN final_return > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN final_return < 0 THEN 1 ELSE 0 END) as losses,
                AVG(final_return) as avg_return
            FROM stock_tracker
            WHERE final_return IS NOT NULL
        """
        params = []
        if start_date:
            query += " AND push_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND push_date <= ?"
            params.append(end_date)
        result = conn.execute(query, params).fetchone()

        total = result["total"] or 0
        wins = result["wins"] or 0
        losses = result["losses"] or 0
        avg_return = result["avg_return"] or 0
        win_rate = (wins / total * 100) if total > 0 else 0

        stats = {
            "total": total,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "avg_return": avg_return,
        }

        # 板块维度
        plate_rows = conn.execute("""
            SELECT plate, COUNT(*) as total,
                   SUM(CASE WHEN final_return > 0 THEN 1 ELSE 0 END) as wins,
                   AVG(final_return) as avg_return
            FROM stock_tracker
            WHERE plate IS NOT NULL AND plate != '' AND final_return IS NOT NULL
            GROUP BY plate ORDER BY total DESC
        """).fetchall()
        stats["by_plate"] = [
            {
                "plate": r["plate"],
                "total": r["total"],
                "wins": r["wins"],
                "win_rate": (r["wins"] / r["total"] * 100) if r["total"] > 0 else 0,
                "avg_return": r["avg_return"] or 0,
            }
            for r in plate_rows
        ]

        # 强度维度
        star_rows = conn.execute("""
            SELECT star_rating, COUNT(*) as total,
                   SUM(CASE WHEN final_return > 0 THEN 1 ELSE 0 END) as wins,
                   AVG(final_return) as avg_return
            FROM stock_tracker
            WHERE star_rating IS NOT NULL AND final_return IS NOT NULL
            GROUP BY star_rating ORDER BY star_rating DESC
        """).fetchall()
        stats["by_star"] = [
            {
                "star_rating": r["star_rating"],
                "total": r["total"],
                "wins": r["wins"],
                "win_rate": (r["wins"] / r["total"] * 100) if r["total"] > 0 else 0,
                "avg_return": r["avg_return"] or 0,
            }
            for r in star_rows
        ]

        return stats
