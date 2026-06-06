"""批量 AI 结构化电报（一次性脚本）"""

import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, ".")

from data.collect.events.telegraph_collector import TelegraphCollector
from system.utils.logger import get_collector_logger

logger = get_collector_logger("telegraph_batch")

tc = TelegraphCollector()
db = tc.db_path

# 先标记噪声
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cursor = conn.execute("""
    SELECT telegraph_id, title, content, level, category, trade_date
    FROM cls_telegraph
    WHERE trade_date = '2026-05-25' AND (ai_status = 'pending' OR ai_status = 'failed')
    ORDER BY ctime ASC
""")
all_rows = [dict(r) for r in cursor.fetchall()]
conn.close()

noise_ids = [r["telegraph_id"] for r in all_rows if tc._is_noise_telegraph(r)]
if noise_ids:
    tc._mark_skipped(noise_ids)
    print(f"[{datetime.now():%H:%M:%S}] Skipped {len(noise_ids)} noise telegraphs")

# 循环批次处理
batch_num = 0
trade_date = "2026-05-25"
while True:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("""
        SELECT COUNT(*) as cnt FROM cls_telegraph
        WHERE trade_date = '2026-05-25' AND (ai_status = 'pending' OR ai_status = 'failed')
    """)
    remaining = cursor.fetchone()["cnt"]
    conn.close()

    if remaining == 0:
        break

    batch_num += 1
    print(f"[{datetime.now():%H:%M:%S}] Batch {batch_num}: {remaining} remaining...")

    try:
        tc._ai_structure_batch([], trade_date)
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] Batch {batch_num} failed: {e}")

# 最终统计
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cursor = conn.execute("""
    SELECT ai_status, COUNT(*) as cnt FROM cls_telegraph
    WHERE trade_date = '2026-05-25'
    GROUP BY ai_status ORDER BY ai_status
""")
stats = [(r["ai_status"], r["cnt"]) for r in cursor.fetchall()]
conn.close()

print(f"\n[{datetime.now():%H:%M:%S}] Done! {batch_num} batches")
for status, cnt in stats:
    print(f"  {status}: {cnt}")
