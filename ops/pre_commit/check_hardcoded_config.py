#!/usr/bin/env python3
"""检查新增的配置硬编码 — 常量赋值被直接写在代码里而不是读配置文件。

检测模式：
  - CONSTANT = 200_000
  - DB_URL = "sqlite:///quant.db"
"""

import re
import subprocess
import sys

# 支持任意前导缩进，精准捕获大写常量定义
CONSTANT_PATTERN = re.compile(r"^\s*([A-Z_][A-Z_0-9]*)\s*=\s*([\d_\.]+|['\"].*?['\"])")


def get_changed_lines(filename: str) -> list[tuple[int, str]]:
    try:
        result = subprocess.run(
            ["git", "--no-pager", "diff", "--cached", "--", filename],
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
        elif line.startswith(" "):
            line_num += 1
    return changed


def main(filenames: list[str]) -> int:
    if not filenames:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
        )
        filenames = [f for f in result.stdout.split("\n") if f.endswith(".py")]

    errors = []
    for f in filenames:
        f_lower = f.lower()
        if "config" in f_lower or "settings" in f_lower or "test" in f_lower:
            continue

        changed = get_changed_lines(f)
        for line_num, text in changed:
            if text.strip().startswith("#"):
                continue

            m = CONSTANT_PATTERN.match(text)
            if m:
                name = m.group(1)
                # 白名单隔离机制
                if name in (
                    "MAX_LINES",
                    "TIMEOUT",
                    "RETRIES",
                    "ONE",
                    "ZERO",
                    "VERSION",
                ):
                    continue
                errors.append(
                    f"{f}:{line_num}: {name} = {m.group(2).strip()} — 硬编码常量，应进配置"
                )

    if errors:
        print("\n❌ 配置硬编码 — 这些常量/字符串应进 config/ 而不是写在代码里:")
        print("─" * 60)
        for e in errors:
            print(f"  {e}")
        print("─" * 60)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else []))
