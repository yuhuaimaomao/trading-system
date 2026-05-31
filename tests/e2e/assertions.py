# -*- coding: utf-8 -*-
"""断言引擎 — 逐项比对实际快照 vs 预期值。"""

import math
from pathlib import Path


FLOAT_TOLERANCE = 0.02  # 浮点比较容差


class AssertionReport:
    """断言结果收集器。"""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures: list[str] = []

    def check(self, path: str, actual, expected, tolerance: float = None):
        """比较单个变量。"""
        tol = tolerance if tolerance is not None else FLOAT_TOLERANCE
        ok, msg = _compare(actual, expected, tol)
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(f"[{path}] {msg}")

    def check_dict(self, prefix: str, actual: dict, expected: dict):
        """递归比较两个 dict。"""
        # 检查 expected 中的每个 key
        for key, exp_val in expected.items():
            act_val = actual.get(key)
            full_path = f"{prefix}.{key}"
            if isinstance(exp_val, dict) and isinstance(act_val, dict):
                self.check_dict(full_path, act_val, exp_val)
            elif isinstance(exp_val, list) and isinstance(act_val, list):
                if len(exp_val) != len(act_val):
                    self.failures.append(
                        f"[{full_path}] 列表长度: actual={len(act_val)} expected={len(exp_val)}"
                    )
                    self.failed += 1
                else:
                    for i, (e, a) in enumerate(zip(exp_val, act_val)):
                        self.check(f"{full_path}[{i}]", a, e)
            else:
                self.check(full_path, act_val, exp_val)

        # 检查 actual 中是否有 expected 没有的 key（不影响通过，只记录）
        for key in actual:
            if key not in expected:
                pass  # 额外字段不报错

    def is_pass(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        lines = [
            f"断言结果: {self.passed + self.failed} 项检查",
            f"  通过: {self.passed}",
            f"  失败: {self.failed}",
        ]
        if self.failures:
            lines.append("\n失败详情:")
            lines.extend(f"  {f}" for f in self.failures[:50])
            if len(self.failures) > 50:
                lines.append(f"  ... 还有 {len(self.failures) - 50} 条")
        return "\n".join(lines)


def _compare(actual, expected, tolerance: float) -> tuple[bool, str]:
    """比较两个值。"""
    if expected is None:
        # expected 为 None 意味着不检查此项
        return True, ""

    if actual is None and expected is not None:
        return False, f"expected={expected}, actual=None"

    if isinstance(expected, float) and isinstance(actual, (int, float)):
        if math.isnan(expected) and math.isnan(actual):
            return True, ""
        if abs(actual - expected) <= tolerance:
            return True, ""
        return False, f"expected={expected}, actual={actual}, delta={actual - expected:.3f}"

    if isinstance(expected, bool) and isinstance(actual, bool):
        if actual == expected:
            return True, ""
        return False, f"expected={expected}, actual={actual}"

    if isinstance(expected, str) and isinstance(actual, str):
        if actual == expected:
            return True, ""
        return False, f"expected='{expected[:50]}', actual='{actual[:50]}'"

    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if actual == expected:
            return True, ""
        return False, f"expected={expected}, actual={actual}"

    # 类型不匹配
    return False, f"type mismatch: expected={type(expected).__name__}({expected}), actual={type(actual).__name__}({actual})"


def compare_snapshots(actual_snap: dict, expected_snap: dict) -> AssertionReport:
    """比较完整快照。"""
    report = AssertionReport()
    report.check_dict("", actual_snap, expected_snap)
    return report
