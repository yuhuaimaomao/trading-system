"""trading-system 统一配置"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ===== 项目根目录 =====
PROJECT_ROOT = Path(__file__).parent.parent.parent

# 加载 .env 文件
load_dotenv(PROJECT_ROOT / ".env")

# ===== 路径 =====
DATABASE_PATH = os.environ.get(
    "TRADING_DB_PATH",
    str(PROJECT_ROOT / "storage" / "stock_market.db"),
)
LOGS_DIR = Path(os.environ.get(
    "TRADING_LOGS_DIR",
    str(PROJECT_ROOT / "storage" / "logs"),
))
STORAGE_PATH = Path(os.environ.get(
    "STORAGE_PATH",
    str(PROJECT_ROOT / "storage"),
))

# ===== API Keys =====
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_MODEL = os.environ.get("DASHSCOPE_MODEL", "qwen-plus")
DASHSCOPE_ANALYSIS_MODEL = os.environ.get("DASHSCOPE_ANALYSIS_MODEL", "qwen3.6-plus")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# ===== Telegram =====
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_CHAT_ID = os.environ.get("TELEGRAM_REPORT_CHAT_ID", "")
TELEGRAM_REPORT_BOT_TOKEN = os.environ.get("TELEGRAM_REPORT_BOT_TOKEN", "")

# ===== QMT =====
QMT_HOST = os.environ.get("QMT_HOST", "192.168.1.33")
QMT_PORT = os.environ.get("QMT_PORT", "5000")
QMT_BASE_URL = f"http://{QMT_HOST}:{QMT_PORT}"

# ===== 代理 =====
PROXY_ENABLED = os.environ.get("PROXY_ENABLED", "false").lower() == "true"

# ===== 交易 =====
ACCOUNT_MODE = os.environ.get("ACCOUNT_MODE", "manual")
MAX_SINGLE_STOCK_PCT = 0.20
MAX_SINGLE_SECTOR_PCT = 0.30
CASH_RESERVE_PCT = 0.20
ENV_POSITION_LIMIT = {"bull": 0.80, "swing": 0.50, "bear": 0.20}
MAX_DAILY_LOSS = 0.03
DEFAULT_TRAILING_STOP = 0.05
DEFAULT_TAKE_PROFIT_RATIO = 0.20
DEFAULT_SLIPPAGE = 0.001
DEFAULT_COMMISSION_RATE = 0.0001
STAMP_TAX_RATE = 0.005
MIN_COMMISSION = 5.0
MIN_DAILY_AMOUNT = 1_0000_0000
MIN_LISTED_DAYS = 60

# ===== 市场环境判定 =====
MA_PERIOD = 20
SWING_THRESHOLD = 0.03
