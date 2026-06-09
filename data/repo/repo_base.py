"""数据访问基类 — 已迁移到 data._base，此处保留向后兼容。"""

# 向后兼容：所有符号从新路径 re-export
from data._base import (  # noqa: F401
    BaseRepository,
    build_insert_sql,
    cols_from_str,
    dict_from_row,
    round_val,
    validate_cols,
)
