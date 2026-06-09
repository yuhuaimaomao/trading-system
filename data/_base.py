"""数据访问基类 — 连接管理、列校验、值处理。

所有 data/ 子模块的共享基类。
"""

import sqlite3
from contextlib import contextmanager


def round_val(v):
    """浮点数统一保留 4 位小数。"""
    if isinstance(v, float):
        return round(v, 4)
    return v


def validate_cols(allowed: frozenset, keys):
    """校验所有列名均在白名单中，否则抛出 ValueError。"""
    invalid = [k for k in keys if k not in allowed]
    if invalid:
        raise ValueError(f"非法列名: {invalid}")


def build_insert_sql(table: str, cols: list[str]) -> str:
    """构建 INSERT OR REPLACE SQL 语句。"""
    col_str = ", ".join(cols)
    placeholders = ", ".join(["?" for _ in cols])
    return f"INSERT OR REPLACE INTO {table} ({col_str}) VALUES ({placeholders})"


def dict_from_row(cols: list[str], row: tuple) -> dict:
    """将数据库行转为 dict（按列名）。"""
    return dict(zip(cols, row))


def cols_from_str(col_str: str) -> list[str]:
    """将 'id, trade_date, foo' 字符串转为列名列表。"""
    return col_str.replace(" ", "").split(",")


class BaseRepository:
    """数据访问基类 — 提供连接管理和通用 CRUD 模式。"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _insert(self, table: str, data: dict, allowed_cols: frozenset) -> int:
        """通用插入。返回 lastrowid。"""
        validate_cols(allowed_cols, data.keys())
        cols = list(data.keys())
        vals = [round_val(v) for v in data.values()]
        sql = build_insert_sql(table, cols)
        with self._conn() as conn:
            cursor = conn.execute(sql, vals)
            conn.commit()
            return cursor.lastrowid

    def _select_all(self, sql: str, params: list = None, col_str: str = "") -> list[dict]:
        """通用查询，返回 dict 列表。"""
        cols = cols_from_str(col_str) if col_str else []
        with self._conn() as conn:
            rows = conn.execute(sql, params or []).fetchall()
        return [dict_from_row(cols, row) for row in rows]

    def _execute(self, sql: str, params: list = None) -> int:
        """执行 UPDATE/DELETE，返回影响行数。"""
        with self._conn() as conn:
            cursor = conn.execute(sql, params or [])
            conn.commit()
            return cursor.rowcount


# 便捷函数：供业务代码获取连接（避免直接 sqlite3.connect）
@contextmanager
def get_db_conn(db_path: str = None):
    """获取数据库连接上下文。

    用法:
        from data._base import get_db_conn
        with get_db_conn() as conn:
            data = StockReader.get_stock_basic(conn, code)
    """
    from system.config.settings import DATABASE_PATH

    path = db_path or DATABASE_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def connect(db_path: str = None):
    """获取数据库连接（非 context manager，由调用方负责关闭）。

    用于需要跨方法传递 conn 的场景。新代码优先使用 get_db_conn()。
    """
    import sqlite3

    from system.config.settings import DATABASE_PATH

    path = db_path or DATABASE_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn
