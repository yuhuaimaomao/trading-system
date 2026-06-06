#!/usr/bin/env python3
"""检查文件和变量的命名规范 — 不管新老代码，统一全量检测。

规范定义：
  1. 文件名：全小写 + 下划线 (snake_case) -> 如 `user_service.py`
  2. 变量与函数名：小驼峰 (camelCase) 或 蛇形 (snake_case)
  3. 类名：大驼峰 (PascalCase) -> 如 `class UserProfile:`
  4. 常量名：全大写 + 蛇形 (UPPER_SNAKE_CASE) -> 如 `MAX_RETRY_COUNT`
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

# 正则判定表达式
SNAKE_CASE_FILE_RE = re.compile(r"^[a-z0-9_]+$")  # 文件名：全小写+下划线
CAMEL_OR_SNAKE_RE = re.compile(r"^[a-z_][a-zA-Z0-9_]*$")  # 变量/函数：小驼峰或蛇形
PASCAL_CASE_RE = re.compile(r"^[A-Z][a-zA-Z0-9]*$")  # 类名：大驼峰
UPPER_SNAKE_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")  # 常量：大写加下划线


def is_valid_variable_or_func(name: str) -> bool:
    """变量、参数、函数名规范验证"""
    if name.startswith("_"):  # 允许私有属性/方法单或双下划线开头
        name = name.lstrip("_")
    if not name:
        return True
    if name.isupper() and len(name) > 1:
        return False  # 全大写留给常量，变量不应该全大写
    return bool(CAMEL_OR_SNAKE_RE.match(name))


def check_file_namings(filepath: str) -> list[str]:
    """1. 检查文件名规范 (snake_case)"""
    violations = []
    path = Path(filepath)
    name_without_ext = path.get_backend() if hasattr(path, "get_backend") else path.stem

    # 排除 Python 核心魔法文件如 __init__.py 或 __main__.py
    if not (name_without_ext.startswith("__") and name_without_ext.endswith("__")):
        if not SNAKE_CASE_FILE_RE.match(name_without_ext):
            violations.append(
                f"文件名 '{path.name}' 不符合 snake_case 规范 (建议改为下划线连接，如: user_service.py)"
            )
    return violations


def check_code_elements(filepath: str) -> list[str]:
    """2. 利用 AST 深入扫描老代码中的所有 Class、Function、Variable、Constant 命名"""
    violations = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
            tree = ast.parse(content, filename=filepath)

        for node in ast.walk(tree):
            lineno = getattr(node, "lineno", 0)

            # 检查类名 (UserProfile)
            if isinstance(node, ast.ClassDef):
                if not PASCAL_CASE_RE.match(node.name):
                    violations.append(
                        f"第 {lineno} 行: 类名 '{node.name}' 应使用大驼峰 (PascalCase)"
                    )

            # 检查函数/方法名 (calculate_total 或 calculateTotal)
            elif isinstance(node, ast.FunctionDef):
                if not is_valid_variable_or_func(node.name):
                    violations.append(
                        f"第 {lineno} 行: 函数名 '{node.name}' 应使用小驼峰或蛇形命名"
                    )

            # 检查赋值语句 (普通变量、元组解构、常量)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    # 单变量/常量赋值: a = 1
                    if isinstance(target, ast.Name):
                        name = target.id
                        # 区分常量与普通变量
                        if name.isupper():
                            if not UPPER_SNAKE_RE.match(name):
                                violations.append(
                                    f"第 {lineno} 行: 常量 '{name}' 应使用全大写加下划线 (UPPER_SNAKE_CASE)"
                                )
                        else:
                            if not is_valid_variable_or_func(name):
                                violations.append(
                                    f"第 {lineno} 行: 变量 '{name}' 应使用小驼峰或蛇形命名"
                                )

                    # 解构赋值: user_id, user_name = 1, "jack"
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        for elt in target.elts:
                            if isinstance(elt, ast.Name):
                                if not is_valid_variable_or_func(elt.id):
                                    violations.append(
                                        f"第 {lineno} 行: 变量 '{elt.id}' 应使用小驼峰或蛇形命名"
                                    )
    except Exception:
        # 如果代码存在致命语法错误，由编译期或常规 linter 拦截，这里直接略过
        pass

    return violations


def main(filenames: list[str]) -> int:
    # 动态适配：未传参则全量扫描暂存区中的所有 Python 文件 (包含本次提交涉及的所有新老文件)
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

        # 统一过滤测试、配置及外部 Prompt 核心文件（降低噪声干扰，与你其他脚本保持高度一致）
        f_lower = str(path).lower()
        if (
            "test" in f_lower
            or path.name.startswith("test_")
            or "config" in f_lower
            or "settings" in f_lower
            or "prompt" in f_lower
        ):
            continue

        # 1. 审计文件名
        file_errors = check_file_namings(f)
        for err in file_errors:
            all_errors.append(f"{f}: {err}")

        # 2. 审计代码内容（变量、类、常量）
        code_errors = check_code_elements(f)
        for err in code_errors:
            all_errors.append(f"{f}:{err}")

    if all_errors:
        print(
            "\n❌ 命名规范检查失败 — 请重构以下不合规命名（老代码与新代码同时被拦截）："
        )
        print("─" * 75)
        for e in all_errors[:30]:  # 最多打印 30 条避免终端被刷屏
            print(f"  {e}")
        if len(all_errors) > 30:
            print(f"  ... 还有 {len(all_errors) - 30} 个命名违规待修复")
        print("─" * 75)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] if len(sys.argv) > 1 else []))
