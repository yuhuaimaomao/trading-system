#!/usr/bin/env python3
"""新文件的 IO 依赖检查 — 只对新增文件生效，存量不管。"""

import subprocess
import sys

# 在 domain 层（analysis/、trade/monitor/ 核心逻辑）不应出现的 IO 依赖
FORBIDDEN_IMPORTS = [
    "sqlite3",
    "requests",
    "httpx",
    "aiohttp",
    "urllib",
]


def main(filenames: list[str]) -> int:
    # 获取本次 commit 中新增的文件
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "--diff-filter=A"],
        capture_output=True, text=True
    )
    new_files = set()
    for line in result.stdout.split("\n"):
        if line.startswith("A\t"):
            new_files.add(line[2:])

    errors = []
    for f in filenames:
        if f not in new_files:
            continue
        if "infrastructure" in f or "adapter" in f:
            continue  # IO 适配器本身可以依赖 IO
        if "test" in f or "config" in f:
            continue

        try:
            text = open(f).read()
            for imp in FORBIDDEN_IMPORTS:
                if f"import {imp}" in text or f"from {imp}" in text:
                    errors.append(f"{f}: 新文件不应直接 import {imp}")
        except Exception:
            pass

    if errors:
        print("\n❌ 新文件 IO 依赖违规（应通过接口而非直接 import）:")
        print("─" * 60)
        for e in errors:
            print(f"  {e}")
        print("─" * 60)
        return 1
    return 0


if __name__ == "__main__":
    filenames = sys.argv[1:] if len(sys.argv) > 1 else []
    sys.exit(main(filenames))
