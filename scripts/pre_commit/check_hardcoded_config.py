#!/usr/bin/env python3
"""检查新增的配置硬编码 — 常量赋值被直接写在代码里而不是读配置文件。

检测模式：
  - CONSTANT = 200_000  (应改为从 config 或 settings 读取)
  - MAX_POSITIONS = 5
  - RATE = 0.000085
"""

import re
import subprocess
import sys


def get_changed_lines(filename: str) -> list[tuple[int, str]]:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--", filename],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    changed = []
    line_num = 0
    for line in result.stdout.split("\n"):
        if line.startswith("@@ "):
            m = re.search(r"\+(\d+)", line)
            line_num = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            changed.append((line_num, line[1:]))
            line_num += 1
        elif not line.startswith("-"):
            line_num += 1
    return changed


def main(filenames: list[str]) -> int:
    errors = []
    # 匹配 UPPER_CASE = 数字 的模式
    pattern = re.compile(r"^\+?\s*([A-Z_][A-Z_0-9]*)\s*=\s*(\d[\d_]*(?:\.\d+)?)")

    for f in filenames:
        if "config" in f or "settings" in f or "test" in f:
            continue
        changed = get_changed_lines(f)
        for line_num, text in changed:
            # 跳过注释
            if text.strip().startswith("#"):
                continue
            m = pattern.match(text)
            if m:
                name = m.group(1)
                # 白名单：明确允许的常量
                if name in ("MAX_LINES", "TIMEOUT", "RETRIES", "ONE", "ZERO"):
                    continue
                errors.append(
                    f"{f}:{line_num}: {name} = {m.group(2)} — 硬编码常量，应进配置"
                )

    if errors:
        print("\n❌ 配置硬编码 — 这些常量应进 config/ 而不是写在代码里:")
        print("─" * 60)
        for e in errors:
            print(f"  {e}")
        print("─" * 60)
        return 1
    return 0


if __name__ == "__main__":
    filenames = sys.argv[1:] if len(sys.argv) > 1 else []
    if not filenames:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
        )
        filenames = [f for f in result.stdout.split("\n") if f.endswith(".py")]
    sys.exit(main(filenames))
