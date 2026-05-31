# -*- coding: utf-8 -*-
"""数据库准备 — 复制生产 DB + 记录初始状态。"""

import shutil
import sqlite3
from pathlib import Path
from datetime import datetime


PROD_DB = Path(__file__).parent.parent.parent / "storage" / "stock_market.db"
TEST_DB_DIR = Path(__file__).parent / "test_db"
TEST_DB = TEST_DB_DIR / "stock_market.db"


def setup_test_db() -> Path:
    """复制生产数据库到测试目录。返回测试 DB 路径。"""
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    print(f"复制生产 DB ({PROD_DB.stat().st_size / 1024 / 1024:.0f}MB)...")
    shutil.copy2(str(PROD_DB), str(TEST_DB))
    print(f"  → {TEST_DB}")
    return TEST_DB


def record_initial_state(db_path: Path) -> dict:
    """记录测试前的数据库初始状态，用于测试结束后对比。"""
    conn = sqlite3.connect(str(db_path))
    state = {}
    for table in ["trade_signals", "trade_orders", "trade_portfolio_snapshots",
                   "trade_portfolio_positions", "trade_holdings_review",
                   "market_snapshots", "sector_snapshots", "index_snapshots"]:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            max_id = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] if count > 0 else 0
            state[table] = {"count": count, "max_id": max_id}
        except Exception:
            state[table] = {"count": 0, "max_id": 0}
    conn.close()
    return state


def record_test_changes(db_path: Path, initial_state: dict) -> dict:
    """对比测试前后的数据库变化。"""
    conn = sqlite3.connect(str(db_path))
    changes = {}
    for table, init in initial_state.items():
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            max_id = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0] if count > 0 else 0
            new_rows = count - init["count"]
            if new_rows > 0:
                changes[table] = {"new_rows": new_rows, "new_ids": f"{init['max_id']+1}..{max_id}"}
        except Exception:
            pass
    conn.close()
    return changes


def get_signal_ids(db_path: Path, status: str = None) -> list[int]:
    """获取 trade_signals 表中指定状态的 ID 列表。"""
    conn = sqlite3.connect(str(db_path))
    if status:
        rows = conn.execute(
            "SELECT id FROM trade_signals WHERE status=?", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM trade_signals").fetchall()
    conn.close()
    return [r[0] for r in rows]


def cleanup_test_db():
    """删除测试数据库。"""
    if TEST_DB.exists():
        TEST_DB.unlink()
        print(f"已删除 {TEST_DB}")
    if TEST_DB_DIR.exists():
        try:
            TEST_DB_DIR.rmdir()
        except OSError:
            pass
