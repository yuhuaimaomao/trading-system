#!/usr/bin/env python3
"""检查变更行中的魔法数字 — 只检查 git diff 中新增的行。

白名单（这些值不需要进配置）:
  0, 1, -1, 0.0, 0.5, 1.0, -1.0  — 数学常量
  100 的整数倍 — 股数倍数
  2 — 布林带标准差倍数、简单序列长度
  0.25, 0.75 — 常用分位

凡是 >= 2 的整数（非 100 倍数）或 >= 0.001 的小数都应进配置。
"""

import re
import subprocess
import sys
from pathlib import Path

WHITELIST = {0, 1, -1, 2, 3, 4, 0.5, -1.0, 0.25, 0.75}  # 值 → 允许的原因


def is_volume_multiple(v: float) -> bool:
    """整数且是 100 的倍数 → 可能是股数，放行"""
    return v > 0 and v % 100 == 0


def is_safe_decimal(v: float) -> bool:
    """纯小数，仅作比例用（如 0.5=50%），放行"""
    return abs(v) < 1.0 and v in WHITELIST


def check_line(line: str) -> list[str]:
    """检查一行代码中的数字字面量，返回违规列表"""
    violations = []
    # 跳过注释和文档字符串
    clean_line = line.strip()
    if (
        clean_line.startswith("#")
        or clean_line.startswith('"""')
        or clean_line.startswith("'''")
    ):
        return violations

    # 1. 拦截科学计数法 (如 1e-4, 5e6)
    for m in re.finditer(r"(?<![\w.])(-?\d+(?:\.\d+)?[eE]-?\d+)(?![\w.])", line):
        try:
            v = float(m.group(1))
            violations.append(f"科学计数法 {m.group(1)} ({v}) — 应进配置")
        except ValueError:
            pass

    # 2. 匹配独立的小数（排除已被科学计数法匹配的部分）
    for m in re.finditer(r"(?<![\w.])-?(\d+\.\d+)(?![\w.][eE]-?)", line):
        v = float(m.group(1))
        if abs(v) >= 0.001 and not is_safe_decimal(v):
            violations.append(f"小数 {v} — 应进配置")

    # 3. 匹配独立的整数（排除常规非零起点变量命名干扰）
    for m in re.finditer(r"(?<![\w.])-?(\d+)(?![\w.][\deE_])", line):
        v = int(m.group(1))
        if abs(v) >= 2 and not is_volume_multiple(abs(v)) and abs(v) not in WHITELIST:
            violations.append(f"整数 {v} — 应进配置")

    return violations


def get_changed_lines(filename: str) -> list[tuple[int, str]]:
    """获取指定文件在 git diff（staged）中新增的行并精准计算行号"""
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
        elif line.startswith(
            " "
        ):  # 修复核心：只有真正的上下文行才递增行号，避开 \ No newline 干扰
            line_num += 1

    return changed


def main(filenames: list[str]) -> int:
    # 动态适配：未传参则全量扫描暂存区 Python 文件
    if not filenames:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
        )
        filenames = [f for f in result.stdout.split("\n") if f.endswith(".py")]

    all_errors = []
    for f in filenames:
        path = Path(f)
        if not path.exists():
            continue
        # 统一过滤测试、配置及 Prompt 文件
        f_lower = str(path).lower()
        if (
            "test" in f_lower
            or path.name.startswith("test_")
            or "config" in f_lower
            or path.name.endswith("_config.py")
            or "prompt" in f_lower
            or path.name.endswith("_prompt.py")
        ):
            continue

        changed = get_changed_lines(f)
        for line_num, text in changed:
            violations = check_line(text)
            for v in violations:
                all_errors.append(f"{path}:{line_num}: {v}\n  → {text.strip()[:80]}")

    if all_errors:
        print("\n❌ 魔法数字 — 这些值应进 config/defaults.yaml:")
        print("─" * 60)
        for e in all_errors[:20]:
            print(e)
        if len(all_errors) > 20:
            print(f"  ... 还有 {len(all_errors) - 20} 个")
        print("─" * 60)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else []))
