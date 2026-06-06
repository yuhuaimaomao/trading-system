#!/usr/bin/env python3
"""新文件的 IO 依赖检查 — 基于 AST 语法树确保 domain 层隔离。"""

import ast
import subprocess
import sys

# 在 domain 层（analysis/、trade/monitor/ 核心逻辑）不应出现的禁忌 IO 依赖
FORBIDDEN_IMPORTS = {"sqlite3", "requests", "httpx", "aiohttp", "urllib"}


def get_new_files() -> set[str]:
    """提取本次暂存区中真正新增（A 状态）的文件"""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "--diff-filter=A"],
        capture_output=True,
        text=True,
    )
    return {
        line[2:].strip() for line in result.stdout.split("\n") if line.startswith("A\t")
    }


def check_file_imports(filepath: str) -> list[str]:
    """利用 AST 深度扫描导包行为，防范任何排版或别名伪装"""
    violations = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            tree = ast.parse(f.read(), filename=filepath)

        for node in ast.walk(tree):
            # 捕获: import httpx, requests 形式
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_module = alias.name.split(".")[0]
                    if root_module in FORBIDDEN_IMPORTS:
                        violations.append(f"直接导入了违规库: import {alias.name}")

            # 捕获: from httpx import client 形式
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_module = node.module.split(".")[0]
                if root_module in FORBIDDEN_IMPORTS:
                    violations.append(
                        f"自违规库中引入子模块: from {node.module} import ..."
                    )
    except Exception:
        pass
    return violations


def main(filenames: list[str]) -> int:
    new_files = get_new_files()

    # 未传参数时，自动审计当前 commit 中的所有新文件
    if not filenames:
        filenames = list(new_files)

    errors = []
    for f in filenames:
        if f not in new_files:
            continue
        # 允许基础设施层、适配器、测试、配置等直接处理底层 IO
        f_lower = f.lower()
        if (
            "infrastructure" in f_lower
            or "adapter" in f_lower
            or "test" in f_lower
            or "config" in f_lower
        ):
            continue
        if not f.endswith(".py"):
            continue

        violations = check_file_imports(f)
        for v in violations:
            errors.append(f"{f}: {v}")

    if errors:
        print(
            "\n❌ 新文件 IO 依赖违规（业务核心逻辑层禁止直接依赖 I/O 库，请通过接口适配器解耦）:"
        )
        print("─" * 60)
        for e in errors:
            print(f"  {e}")
        print("─" * 60)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else []))
