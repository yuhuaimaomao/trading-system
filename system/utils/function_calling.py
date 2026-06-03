"""
Function Calling 引擎

支持多轮工具调用，让 AI 可以实时查询股票数据
"""

import json
from typing import Any, Dict, List

from system.utils.logger import get_system_logger
from system.utils.stock_tools import TOOLS_DEFINITION, StockTools

logger = get_system_logger("function_calling")


class FunctionCallingEngine:
    """Function Calling 引擎"""

    def __init__(self, db_path: str = None):
        """
        初始化引擎

        Args:
            db_path: 数据库路径
        """
        self.stock_tools = StockTools(db_path)

        # 工具名称到函数的映射
        self.tool_functions = {
            "get_cls_digest_news": self.stock_tools.get_cls_digest_news,
            "get_telegraph_news": self.stock_tools.get_telegraph_news,
            "get_market_cap": self.stock_tools.get_market_cap,
            "get_stock_info": self.stock_tools.get_stock_info,
            "get_sector_stocks": self.stock_tools.get_sector_stocks,
            "get_sector_zhongjun": self.stock_tools.get_sector_zhongjun,
            "get_lhb_seats": self.stock_tools.get_lhb_seats,
            "get_regulatory_risks": self.stock_tools.get_regulatory_risks,
            "get_yesterday_limit_ups": self.stock_tools.get_yesterday_limit_ups,
            "get_unusual_stocks": self.stock_tools.get_unusual_stocks,
            "get_hotspot_stocks": self.stock_tools.get_hotspot_stocks,
            "get_yesterday_review": self.stock_tools.get_yesterday_review,
            "get_yesterday_picks_performance": self.stock_tools.get_yesterday_picks_performance,
            "get_historical_calibration": self.stock_tools.get_historical_calibration,
            "get_learning_lessons": self.stock_tools.get_learning_lessons,
            "get_prediction_accuracy": self.stock_tools.get_prediction_accuracy,
            "get_pending_signals": self.stock_tools.get_pending_signals,
            "search_stock": self.stock_tools.search_stock,
            "search_sector": self.stock_tools.search_sector,
        }

        logger.info(
            f"✅ Function Calling 引擎初始化完成（{len(self.tool_functions)}个工具）"
        )

    def execute_tool(self, tool_name: str, arguments: Dict) -> Any:
        """
        执行工具函数

        Args:
            tool_name: 工具名称
            arguments: 工具参数

        Returns:
            工具执行结果
        """
        if tool_name not in self.tool_functions:
            error_msg = f"❌ 未找到工具：{tool_name}"
            logger.error(error_msg)
            return {"error": error_msg}

        try:
            func = self.tool_functions[tool_name]
            result = func(**arguments)
            logger.info(f"✅ 工具执行成功：{tool_name}({arguments})")
            return result
        except TypeError as e:
            error_msg = f"❌ 工具参数错误：{tool_name} - {e}"
            logger.error(error_msg)
            return {"error": error_msg}
        except Exception as e:
            error_msg = f"❌ 工具执行失败：{tool_name} - {e}"
            logger.error(error_msg)
            return {"error": error_msg}

    def process_tool_calls(self, tool_calls: List) -> List[Dict]:
        """
        处理 AI 返回的工具调用请求

        Args:
            tool_calls: AI 返回的工具调用列表（可能是 dict 或对象）

        Returns:
            Tool Message 列表（用于返回给 AI）
        """
        tool_messages = []

        for tool_call in tool_calls:
            # 兼容 dict 和对象两种格式
            if isinstance(tool_call, dict):
                tool_call_id = tool_call.get("id", "")
                function_data = tool_call.get("function", {})
                tool_name = function_data.get("name", "")
                arguments_str = function_data.get("arguments", "{}")
            else:
                # 对象格式
                tool_call_id = tool_call.id
                tool_name = tool_call.function.name
                arguments_str = tool_call.function.arguments

            # 解析参数
            try:
                arguments = json.loads(arguments_str)
            except (json.JSONDecodeError, TypeError):
                arguments = {}

            logger.info(f"🔧 收到工具调用：{tool_name}({arguments})")

            # 执行工具
            result = self.execute_tool(tool_name, arguments)

            # 构造 Tool Message（OpenAI 格式）
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": json.dumps(result, ensure_ascii=False),
            }

            tool_messages.append(tool_message)
            logger.info(f"✅ 工具调用完成：{tool_name} - {result.get('error', '成功')}")

        return tool_messages

    def get_tools_definition(self) -> List[Dict]:
        """
        获取工具定义（用于传递给 AI）

        Returns:
            工具定义列表（OpenAI 格式）
        """
        return TOOLS_DEFINITION
