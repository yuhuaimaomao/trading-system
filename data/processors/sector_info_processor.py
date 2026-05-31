"""
板块信息更新处理器

职责：检查行业/概念板块采集完整性 → 调用 update_sector_info_daily
"""

from typing import Any, Dict

from system.utils.logger import get_system_logger

logger = get_system_logger("sector_info_processor")


class SectorInfoProcessor:
    """板块信息更新处理器"""

    @classmethod
    def run(
        cls,
        trade_date: str,
        industry_result: Dict[str, Any],
        concept_result: Dict[str, Any],
    ) -> bool:
        """
        检查采集完整性后更新 sector_info 表

        Args:
            trade_date: 交易日期
            industry_result: 行业板块采集结果
            concept_result: 概念板块采集结果

        Returns:
            是否成功更新
        """
        industry_ok = industry_result.get("count", 0) > 0 and industry_result.get(
            "count", 0
        ) == industry_result.get("total", 0)
        concept_ok = concept_result.get("count", 0) > 0 and concept_result.get(
            "count", 0
        ) == concept_result.get("total", 0)

        if not (industry_ok and concept_ok):
            logger.warning("板块数据采集不完整，跳过 sector_info 更新")
            logger.warning(
                f"  行业板块：{industry_result.get('count', 0)}/{industry_result.get('total', 0)}"
            )
            logger.warning(
                f"  概念板块：{concept_result.get('count', 0)}/{concept_result.get('total', 0)}"
            )
            return False

        from ops.scripts.update_sector_info import update_sector_info_daily

        logger.info("行业和概念板块采集完整，开始更新 sector_info 表...")
        update_sector_info_daily(trade_date)
        logger.info("sector_info 表更新完成")
        return True
