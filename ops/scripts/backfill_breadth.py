"""回填 market_breadth 历史数据 — 纯 SQL 聚合，不调 API

用法:
  python ops/scripts/backfill_breadth.py           # 回填所有缺失日期
  python ops/scripts/backfill_breadth.py --dry-run # 预览
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from analysis.screening.breadth import MarketBreadth
from system.config import settings


def main():
    parser = argparse.ArgumentParser(description="回填 market_breadth 历史数据")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    mb = MarketBreadth()
    conn = sqlite3.connect(settings.DATABASE_PATH)

    # 取所有有 stock_basic 数据的交易日
    dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_basic ORDER BY trade_date"
        ).fetchall()
    ]

    # 过滤已存在的日期
    existing = set(
        r[0] for r in conn.execute("SELECT trade_date FROM market_breadth").fetchall()
    )

    pending = [d for d in dates if d not in existing]
    print(f"共 {len(dates)} 个交易日, 已存在 {len(existing)}, 待回填 {len(pending)}")

    if args.dry_run:
        print(f"将回填: {pending[0]} ~ {pending[-1]}" if pending else "无需回填")
        conn.close()
        return

    if not pending:
        print("无需回填")
        conn.close()
        return

    for i, d in enumerate(pending):
        result = mb.save(d)
        print(
            f"  [{i + 1}/{len(pending)}] {d}  "
            f"涨{result['up_count']} 跌{result['down_count']} "
            f"涨停{result['limit_up_count']} 跌停{result['limit_down_count']} "
            f"状态:{result['market_state']}"
        )

    conn.close()
    print(f"完成, 回填 {len(pending)} 条")


if __name__ == "__main__":
    main()
