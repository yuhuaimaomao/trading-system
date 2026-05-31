#!/usr/bin/env python3
"""新文件大小检查 — 只对 git 新增的文件生效，存量文件不管。"""

import subprocess
import sys

MAX_LINES = 400


def main(filenames: list[str]) -> int:
    # 获取本次 commit 中新增的文件（不是修改的文件）
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "--diff-filter=A"],
        capture_output=True,
        text=True,
    )
    new_files = set()
    for line in result.stdout.split("\n"):
        if line.startswith("A\t"):
            new_files.add(line[2:])

    errors = []
    for f in filenames:
        if f not in new_files:
            continue  # 不是新文件，跳过
        if "test" in f or "config" in f or "prompt" in f:
            continue  # 测试/配置/prompt 文件不限制大小
        try:
            with open(f) as fh:
                lines = len(fh.readlines())
            if lines > MAX_LINES:
                errors.append(f"{f}: {lines} 行 (新文件上限 {MAX_LINES})")
        except Exception:
            pass

    if errors:
        print(f"\n❌ 新文件过大 (上限 {MAX_LINES} 行):")
        print("─" * 60)
        for e in errors:
            print(f"  {e}")
        print("─" * 60)
        return 1
    return 0


if __name__ == "__main__":
    filenames = sys.argv[1:] if len(sys.argv) > 1 else []
    sys.exit(main(filenames))
