"""
代理配置

统一管理代理相关配置
"""

from pathlib import Path

from system.config.settings import STORAGE_PATH

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent

# 缓存目录（proxy 专用）
CACHE_DIR = Path(STORAGE_PATH) / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 注意：STORAGE_PATH 和 LOGS_DIR 已在 config/settings.py 中定义，无需重复创建

# ==================== 天启代理 API 配置 ====================

# API 基础 URL
TIANQI_API_URL = "http://api.tianqiip.com/getip"

# API 参数（从环境变量读取，避免硬编码）
TIANQI_API_PARAMS = {
    "secret": "mg4sahur6t0o7a9o",
    "num": 1,
    "type": "json",
    "port": 1,
    "time": 3,
    "mr": 1,
    "sign": "9395e0f628a5529a7c10b2ca901a85f2",
}

# ==================== 请求配置 ====================

# 代理请求超时时间（秒）
PROXY_TIMEOUT = 10

# 东财请求超时时间（秒）
REQUEST_TIMEOUT = 30

# 最大重试次数
MAX_RETRIES = 3

# 重试间隔（秒）
RETRY_DELAY = 2

# ==================== 缓存配置 ====================

# 是否启用缓存
CACHE_ENABLED = True

# 缓存过期时间（小时）- 改为 7 天
CACHE_EXPIRE_HOURS = 168  # 7 天 * 24 小时
