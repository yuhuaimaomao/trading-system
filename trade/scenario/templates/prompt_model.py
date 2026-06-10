"""模板数据结构 — 无依赖，可被所有模板文件安全 import。"""

from dataclasses import dataclass


@dataclass
class PromptTemplate:
    scenario: str
    system_prompt: str  # AI 角色定义 + 核心判断原则
    user_template: str  # {field} 占位模板
    required_fields: list[str]  # 模板必填字段（格式化前校验）
    max_tokens: int = None  # None 则由 API 自行决定
    dedupe: bool = True  # 同名 key 是否替换旧任务
