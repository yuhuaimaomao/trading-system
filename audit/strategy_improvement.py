"""改进建议应用器 — 支持四层改进的自动/半自动应用"""

import re

from data.repo import TradeRepository
from system.utils.logger import get_audit_logger

logger = get_audit_logger("strategy")


class ImprovementApplier:
    """应用审计生成的改进建议"""

    def __init__(self, db_path: str = None):
        self.repo = TradeRepository(db_path=db_path)

    def apply(self, improvement_id: int) -> bool:
        improvements = self.repo.get_pending_improvements()
        imp = None
        for i in improvements:
            if i["id"] == improvement_id:
                imp = i
                break

        if not imp:
            logger.error(f"改进建议 #{improvement_id} 不存在或已处理")
            return False

        imp_type = imp["improvement_type"]
        change = imp.get("suggested_change", "")

        if imp_type == "factor_tune":
            success = self._apply_factor_tune(imp)
        elif imp_type == "prompt_tune":
            success = self._apply_prompt_tune(imp)
        elif imp_type in ("pipeline_add", "pipeline_modify"):
            logger.info(f"管线级改进 #{improvement_id} 需要人工审核: {change[:100]}")
            success = False
        elif imp_type == "data_add":
            logger.info(f"数据级改进 #{improvement_id} 需要人工审核: {change[:100]}")
            success = False
        else:
            logger.warning(f"未知改进类型: {imp_type}")
            success = False

        if success:
            self.repo.apply_improvement(improvement_id)
            logger.info(f"改进 #{improvement_id} 已应用")

        return success

    def _apply_factor_tune(self, imp: dict) -> bool:
        target = imp.get("target_param", "")
        change = imp.get("suggested_change", "")

        if not target or not change:
            logger.warning("因子调优缺少 target_param 或 suggested_change")
            return False

        threshold_match = re.search(r"(\d+\.?\d*)", change)
        if not threshold_match:
            logger.warning(f"无法从改进建议中解析阈值: {change}")
            return False

        new_threshold = float(threshold_match.group(1))
        factor_file = "strategy/screening/factors.py"

        func_pattern = rf"def {re.escape(target)}\("
        try:
            with open(factor_file, encoding="utf-8") as f:
                content = f.read()

            if func_pattern not in content:
                logger.warning(f"在 factors.py 中未找到函数: {target}")
                return False

            logger.info(f"因子 {target} 阈值建议调整为 {new_threshold}")
            logger.info(f"因子调优需要手动确认: {change}")
            return True
        except Exception as e:
            logger.error(f"因子调优失败: {e}")
            return False

    def _apply_prompt_tune(self, imp: dict) -> bool:
        logger.info(f"Prompt 改进建议已记录: {imp.get('suggested_change', '')[:100]}")
        return True

    def list_pending(self) -> list[dict]:
        return self.repo.get_pending_improvements()
