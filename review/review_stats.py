"""
数据采集统计服务

功能：
- 生成采集统计报告
- 数据量阈值告警
- 推送统计报告到 Telegram
"""

from datetime import datetime
from typing import Any, Dict

from system.message import MessageSender
from system.utils.logger import get_review_logger

logger = get_review_logger("analyzer")


class CollectionStatsService:
    """采集统计服务"""

    # A 类采集器：只显示实际数量
    A_CLASS = [
        "lhb",
        "limit_pool",
        "stock_monitor",
        "regulatory_letter",
        "strong_stock",
        "share_holder_change",
        "suspend_resume",
    ]

    # B 类采集器：固定数量
    B_CLASS = {
        "main_index": 10,  # 大盘指数 10 条
    }

    # C 类采集器：从 API 获取总数（需要显示"应采集 X 个，实际 Y 个"）
    C_CLASS_WITH_TOTAL = ["industry", "concept", "stock_basic"]

    # C 类采集器：从 API 获取总数（不显示应采集数量）
    C_CLASS = []

    # 中文名称映射
    NAME_MAP = {
        "lhb": "龙虎榜",
        "limit_pool": "涨跌停",
        "industry": "行业板块",
        "concept": "概念板块",
        "stock_basic": "个股行情",
        "share_holder_change": "股东增减持",
        "suspend_resume": "停复牌",
        "strong_stock": "强势股",
        "regulatory_letter": "监管函",
        "main_index": "大盘指数",
        "stock_monitor": "股票异动",
    }

    def __init__(self):
        from system.config.settings import (
            TELEGRAM_PRIVATE_CHAT_ID,
            TELEGRAM_REPORT_BOT_TOKEN,
        )

        self.sender = MessageSender(
            chat_id=TELEGRAM_PRIVATE_CHAT_ID, bot_token=TELEGRAM_REPORT_BOT_TOKEN
        )

    def generate_summary(self, data: Dict[str, Any], trade_date: str = None) -> str:
        """
        生成采集统计报告

        Args:
            data: 采集的数据字典
            trade_date: 交易日期 (默认今天)

        Returns:
            统计报告字符串
        """
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        summary = []
        summary.append("📊 数据采集统计报告")
        summary.append(f"采集日期：{trade_date}")
        summary.append(f"生成时间：{datetime.now().strftime('%H:%M:%S')}")
        summary.append("")

        total = 0
        failed_items = []

        for name, value in data.items():
            # 支持字典格式（包含 count/total）和列表格式
            if isinstance(value, dict):
                count = value.get("count", 0)
                total_count = value.get("total", count)  # 应采集数量
                actual_count = count  # 实际采集数量
            elif isinstance(value, (list, type(None))):
                count = len(value) if value else 0
                total_count = count
                actual_count = count
            else:
                continue

            total += actual_count

            # 获取中文名称
            cn_name = self.NAME_MAP.get(name, name)

            # 根据分类显示
            if name in self.A_CLASS:
                # A 类：只显示实际数量
                status = "✅" if actual_count > 0 else "❌"
                summary.append(f"{status} {cn_name}: {actual_count}条")

                if actual_count == 0:
                    failed_items.append(cn_name)

            elif name in self.B_CLASS:
                # B 类：固定数量
                expected = self.B_CLASS.get(name, 0)
                status = "✅" if actual_count >= expected else "❌"
                summary.append(
                    f"{status} {cn_name}: 应采集{expected}条，实际{actual_count}条"
                )

                if actual_count < expected:
                    failed_items.append(f"{cn_name}: 缺{expected - actual_count}条")

            elif name in self.C_CLASS_WITH_TOTAL:
                # C 类（行业/概念板块）：显示"应采集 X 个，实际 Y 个"
                if isinstance(value, dict) and not value.get("success"):
                    # 采集失败
                    summary.append(f"❌ {cn_name}: 采集失败")
                    failed_items.append(cn_name)
                else:
                    status = "✅" if actual_count >= total_count else "❌"
                    summary.append(
                        f"{status} {cn_name}: 应采集{total_count}个，实际{actual_count}个"
                    )

                    if actual_count < total_count:
                        failed_items.append(
                            f"{cn_name}: 缺{total_count - actual_count}个"
                        )

            elif name in self.C_CLASS:
                # C 类：其他从 API 获取总数的
                summary.append(f"✅ {cn_name}: {actual_count}条")

        summary.append("")
        summary.append(f"总计：{total}条记录")

        # 添加失败信息
        if failed_items:
            summary.append("")
            summary.append("❌ 采集失败:")
            for item in failed_items:
                summary.append(f"  - {item}")
            summary.append("")
            summary.append("⚠️ 数据未保存，请手动采集失败项后重新执行复盘任务")
        else:
            summary.append("")
            summary.append("✅ 所有采集器完成，数据已保存")

        return "\n".join(summary)

    def send_summary(self, summary: str) -> bool:
        """
        发送统计报告到 Telegram

        Args:
            summary: 统计报告字符串

        Returns:
            是否发送成功
        """
        try:
            self.sender.send(summary)
            logger.info("✅ 统计报告已发送到 Telegram")
            return True
        except Exception as e:
            logger.error(f"❌ 发送统计报告失败：{e}")
            return False

    def check_and_report(
        self,
        data: Dict[str, Any],
        trade_date: str = None,
        send_to_telegram: bool = True,
    ) -> str:
        """
        检查数据并生成报告

        Args:
            data: 采集的数据字典
            trade_date: 交易日期
            send_to_telegram: 是否发送到 Telegram

        Returns:
            统计报告字符串
        """
        # 生成报告
        summary = self.generate_summary(data, trade_date)

        # 打印日志
        logger.info("\n" + summary)

        # 发送到 Telegram
        if send_to_telegram:
            self.send_summary(summary)

        return summary
