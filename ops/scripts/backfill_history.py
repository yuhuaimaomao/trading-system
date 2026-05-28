"""从 Sina API (stock_zh_a_daily) 回填 stock_basic 历史日线数据

用途: RPS_60/RPS_120 需要 120+ 天历史。

用法:
  python ops/scripts/backfill_history.py           # 回填最近 120 天
  python ops/scripts/backfill_history.py --days 60  # 回填 60 天
  python ops/scripts/backfill_history.py --dry-run  # 预览
  python ops/scripts/backfill_history.py --stats    # 覆盖度统计
  python ops/scripts/backfill_history.py --reset    # 清除断点重来
"""

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import akshare as ak

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from system.config import settings

logger = logging.getLogger("backfill")
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(sys.stdout))

CHUNK_SIZE = 20
SLEEP_BETWEEN = 0.2
API_TIMEOUT = 15
MAX_RETRIES = 3

PROGRESS_FILE = Path(settings.DATABASE_PATH).parent / "backfill_progress.json"


def code_to_sina_prefix(code: str) -> str:
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith("6"):
        return f"sh{code}"
    return ""


class _TimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _TimeoutError()


def _fetch_worker(code: str, start: str, end: str) -> list[dict] | None:
	"""在线程中执行 akshare 调用"""
	sina_code = code_to_sina_prefix(code)
	if not sina_code:
		return []
	df = ak.stock_zh_a_daily(symbol=sina_code, start_date=start, end_date=end, adjust="qfq")
	if df is None or df.empty:
		return None
	rows = []
	for _, row in df.iterrows():
		rows.append({
			"trade_date": str(row["date"]),
			"open": float(row.get("open", 0) or 0),
			"high": float(row.get("high", 0) or 0),
			"low": float(row.get("low", 0) or 0),
			"price": float(row.get("close", 0) or 0),
			"volume": float(row.get("volume", 0) or 0),
		})
	return rows


def fetch_one(code: str, start: str, end: str, timeout: int = API_TIMEOUT) -> list[dict] | None:
	"""用线程超时机制获取单只股票日线（不等待死线程退出）"""
	from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
	executor = ThreadPoolExecutor(max_workers=1)
	try:
		future = executor.submit(_fetch_worker, code, start, end)
		return future.result(timeout=timeout)
	except FutureTimeout:
		logger.debug(f"  {code} 超时 ({timeout}s)")
		return None
	except Exception as e:
		logger.debug(f"  {code} fetch error: {e}")
		return None
	finally:
		executor.shutdown(wait=False)


def upsert_rows(conn: sqlite3.Connection, code: str, name: str, rows: list[dict]):
    for r in rows:
        prev = conn.execute(
            "SELECT price FROM stock_basic WHERE stock_code=? AND trade_date=?",
            (code, r["trade_date"]),
        ).fetchone()
        if prev:
            continue

        prev_row = conn.execute(
            "SELECT price FROM stock_basic WHERE stock_code=? AND trade_date < ? "
            "ORDER BY trade_date DESC LIMIT 1",
            (code, r["trade_date"]),
        ).fetchone()
        prev_price = prev_row[0] if prev_row else None
        change_pct = 0.0
        if prev_price and prev_price > 0:
            change_pct = round((r["price"] - prev_price) / prev_price * 100, 2)

        conn.execute(
            """INSERT INTO stock_basic
               (stock_code, stock_name, trade_date,
                price, open, high, low, prev_close, change_pct, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, name, r["trade_date"],
             r["price"], r["open"], r["high"], r["low"],
             prev_price, change_pct, r["volume"]),
        )


def get_codes_needing_data(conn: sqlite3.Connection, start: str) -> list[tuple[str, str]]:
    today_str = date.today().strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT DISTINCT b.stock_code, b.stock_name
           FROM stock_basic b
           WHERE b.trade_date >= date(?, '-10 days')
             AND b.stock_code NOT IN (
                 SELECT stock_code FROM stock_basic WHERE trade_date <= ?
             )
           ORDER BY b.stock_code""",
        (today_str, start),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def show_stats(conn: sqlite3.Connection, start: str):
    rows = conn.execute("""
        SELECT
            CASE
                WHEN min_date <= ? THEN '120天完整'
                WHEN min_date <= date(?, '-90 days') THEN '90-119天'
                WHEN min_date <= date(?, '-60 days') THEN '60-89天'
                WHEN min_date <= date(?, '-30 days') THEN '30-59天'
                ELSE '<30天'
            END as coverage,
            COUNT(*) as cnt
        FROM (SELECT stock_code, MIN(trade_date) as min_date FROM stock_basic GROUP BY stock_code)
        GROUP BY coverage ORDER BY MIN(min_date)
    """, (start, start, start, start)).fetchall()

    print(f"\n数据覆盖度分布 (目标: {start} ~ {date.today()}):")
    total = sum(r[1] for r in rows)
    for coverage, cnt in rows:
        print(f"  {coverage}: {cnt} 只 ({cnt/total*100:.1f}%)")
    print(f"  总计: {total} 只")

    need = conn.execute(
        "SELECT COUNT(DISTINCT stock_code) FROM stock_basic "
        "WHERE stock_code NOT IN (SELECT stock_code FROM stock_basic WHERE trade_date <= ?)",
        (start,),
    ).fetchone()[0]
    est_min = need * 1.5 / 60
    print(f"\n需补数据: {need} 只, 预估 {est_min:.0f} 分钟")


def load_progress() -> set[str]:
    if PROGRESS_FILE.exists():
        try:
            data = json.loads(PROGRESS_FILE.read_text())
            return set(data.get("done", []))
        except Exception:
            pass
    return set()


def save_progress(done: set[str], start_date: str, total_new: int, fail_count: int):
    PROGRESS_FILE.write_text(json.dumps({
        "done": sorted(done),
        "start_date": start_date,
        "total_new_rows": total_new,
        "fail_count": fail_count,
        "updated": date.today().isoformat(),
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="回填 stock_basic 历史日线")
    parser.add_argument("--days", type=int, default=120, help="回填天数（默认 120）")
    parser.add_argument("--timeout", type=int, default=API_TIMEOUT, help=f"API 超时秒数（默认 {API_TIMEOUT}）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写库")
    parser.add_argument("--stats", action="store_true", help="仅显示覆盖度统计")
    parser.add_argument("--reset", action="store_true", help="清除断点，从头开始")
    args = parser.parse_args()

    db_path = settings.DATABASE_PATH
    conn = sqlite3.connect(db_path)

    end_date = date.today().strftime("%Y-%m-%d")
    start_date = (date.today() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    if args.stats:
        show_stats(conn, start_date)
        conn.close()
        return

    todo = get_codes_needing_data(conn, start_date)
    logger.info(f"需补数据: {len(todo)} 只 (目标范围: {start_date} ~ {end_date})")

    if args.dry_run:
        logger.info("[DRY RUN] 不会写入数据库")
        for code, name in todo[:10]:
            logger.info(f"  {code} {name}")
        if len(todo) > 10:
            logger.info(f"  ... 共 {len(todo)} 只")
        conn.close()
        return

    if args.reset and PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    done = load_progress()
    pending = [(c, n) for c, n in todo if c not in done]
    logger.info(f"已恢复 {len(done)} 只, 剩余 {len(pending)} 只")

    if not pending:
        logger.info("所有股票数据已完整，无需回填")
        conn.close()
        return

    total = 0
    count = 0
    fail_count = 0
    start_time = time.time()
    consec_fails = 0  # 连续失败计数，用于 API 限流检测


    for idx, (code, name) in enumerate(pending):
        # 连续失败 >= 10 只 → API 限流，冷却 60 秒
        if consec_fails >= 10:
            logger.warning(f"连续 {consec_fails} 只失败，疑似 API 限流，冷却 60s...")
            time.sleep(60)
            consec_fails = 0

        # retry loop
        rows = None
        retry_sleep = 3
        for attempt in range(MAX_RETRIES):
            try:
                rows = fetch_one(code, start_date, end_date, timeout=args.timeout)
                if rows is not None:
                    break
            except Exception:
                rows = None
            if attempt < MAX_RETRIES - 1:
                time.sleep(retry_sleep)
                retry_sleep *= 2

        if rows is None:
            fail_count += 1
            consec_fails += 1
            done.add(code)
        else:
            upsert_rows(conn, code, name, rows)
            total += len(rows)
            count += 1
            consec_fails = 0
            done.add(code)

        processed = idx + 1
        if processed % CHUNK_SIZE == 0 or processed == len(pending):
            conn.commit()
            save_progress(done, start_date, total, fail_count)
            elapsed = time.time() - start_time
            rate = processed / elapsed if elapsed > 0 else 0
            eta = (len(pending) - processed) / rate / 60 if rate > 0 else 0
            logger.info(
                f"  进度: {len(done)}/{len(todo)} ({processed}/{len(pending)}) "
                f"新增 {total} 条, 失败 {fail_count}, "
                f"{rate:.1f}只/s, ETA {eta:.0f}min, 耗时 {elapsed/60:.1f}min"
            )

        time.sleep(SLEEP_BETWEEN)

    conn.commit()
    save_progress(done, start_date, total, fail_count)
    conn.close()

    elapsed = (time.time() - start_time) / 60
    logger.info(f"完成: {count} 只有新数据, 失败 {fail_count}, 新增 {total} 条, 耗时 {elapsed:.1f}min")
    if fail_count > 0:
        logger.info(f"失败列表见 {PROGRESS_FILE}")

    if PROGRESS_FILE.exists() and fail_count == 0:
        PROGRESS_FILE.unlink()


if __name__ == "__main__":
    main()
