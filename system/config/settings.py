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
DEFAULT_COMMISSION_RATE = 0.000085  # 万0.85
STAMP_TAX_RATE = 0.001  # 单边千分之一（卖出征收）
MIN_COMMISSION = 5.0
MIN_DAILY_AMOUNT = 1_0000_0000
MIN_LISTED_DAYS = 60

# ===== 异动检测阈值 =====
ABNORMAL_RAPID_RISE_PCT = 3.0       # 急速拉升: 当前涨幅 - 上轮涨幅 > 3%
ABNORMAL_VOLUME_SURGE_RATIO = 5.0   # 量比暴增: 当前成交量 > 上轮成交量 × 5
ABNORMAL_NEAR_LIMIT_PCT = 7.0       # 逼近涨停: 涨幅 > 7%

# ===== 市场环境判定 =====
MA_PERIOD = 20
SWING_THRESHOLD = 0.03

# ===== 筛选策略 =====
STOCK_BASIC_RETENTION_DAYS = 120  # stock_basic 保留天数（RPS 计算需要，当前 21 天自然增长中）
SCREENING_MIN_MCAP_YI = 50        # 市值下限（亿）
RPS_THRESHOLD_TOP = 0.20          # RPS 前 20% 为强势
RPS_RESONANCE_THRESHOLD = 0.30    # RPS 多周期共振阈值（前 30%）
MARKET_BREADTH_BULL = 3000        # 普涨: 上涨家数 > 3000
MARKET_BREADTH_DIVIDE = 1500      # 分化: 上涨 1500~3000
MARKET_BREADTH_BEAR = 800         # 普跌/恐慌: 上涨 < 800
MARKET_BREADTH_BOUNCE = 2000      # 连跌修复: 恐慌后首日涨家数 > 2000
