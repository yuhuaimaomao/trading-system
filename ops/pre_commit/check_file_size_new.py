#!/usr/bin/env python3
"""新文件大小检查 — 只对 git 新增的文件生效，存量文件不管。"""

import subprocess
import sys

MAX_LINES = 400


def get_new_files() -> set[str]:
    """提取本次暂存区中真正新增（A 状态）的文件"""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "--diff-filter=A"],
        capture_output=True,
        text=True,
    )
    new_files = set()
    for line in result.stdout.split("\n"):
        if line.startswith("A\t"):
            new_files.add(line[2:].strip())
    return new_files


def main(filenames: list[str]) -> int:
    new_files = get_new_files()

    # 核心自适应修复：未传参数时，自动审计当前 commit 中的所有新文件
    if not filenames:
        filenames = list(new_files)

    errors = []
    for f in filenames:
        if f not in new_files:
            continue  # 历史存量修改，跳过

        f_lower = f.lower()
        if "test" in f_lower or "config" in f_lower or "prompt" in f_lower:
            continue  # 测试/配置/prompt 文件不限制大小

        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                lines = len(fh.readlines())
            if lines > MAX_LINES:
                errors.append(f"{f}: {lines} 行 (新文件上限 {MAX_LINES} 行)")
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
    sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else []))
