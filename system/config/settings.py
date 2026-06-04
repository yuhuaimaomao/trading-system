"""trading-system 统一配置"""

import os
from pathlib import Path

from dotenv import load_dotenv

# ===== 项目根目录 =====
PROJECT_ROOT = Path(__file__).parent.parent.parent

# 加载 .env 文件
load_dotenv(PROJECT_ROOT / ".env", override=True)

# ===== 路径 =====
DATABASE_PATH = os.environ.get(
    "TRADING_DB_PATH",
    str(PROJECT_ROOT / "storage" / "stock_market.db"),
)
LOGS_DIR = Path(
    os.environ.get(
        "TRADING_LOGS_DIR",
        str(PROJECT_ROOT / "storage" / "logs"),
    )
)
STORAGE_PATH = Path(
    os.environ.get(
        "STORAGE_PATH",
        str(PROJECT_ROOT / "storage"),
    )
)

# ===== API Keys =====
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

# ===== AI 模型统一配置 =====
# 全局默认模型，所有 AI 调用（复盘/早报/审计/电报/盯盘）都从此处取。
# .env 中设置 AI_MODEL，例: AI_MODEL=deepseek-v4-pro
# 默认值由环境变量强控，不设默认模型名——未配置则启动报错。
AI_MODEL = os.environ.get("AI_MODEL", "")
AI_PROVIDER = os.environ.get("AI_PROVIDER", "")  # dashscope / deepseek / auto

# Provider 端点
DASHSCOPE_ENDPOINT = os.environ.get(
    "DASHSCOPE_ENDPOINT",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
)
DEEPSEEK_ENDPOINT = os.environ.get(
    "DEEPSEEK_ENDPOINT",
    "https://api.deepseek.com/v1/chat/completions",
)

# 向下兼容（旧环境变量仍生效）
DASHSCOPE_MODEL = os.environ.get("DASHSCOPE_MODEL", AI_MODEL)
DASHSCOPE_ANALYSIS_MODEL = os.environ.get("DASHSCOPE_ANALYSIS_MODEL", AI_MODEL)
AUDIT_AI_MODEL = os.environ.get("AUDIT_AI_MODEL", AI_MODEL)

# ===== Telegram =====
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_REPORT_CHAT_ID = os.environ.get("TELEGRAM_REPORT_CHAT_ID", "")
TELEGRAM_REPORT_BOT_TOKEN = os.environ.get("TELEGRAM_REPORT_BOT_TOKEN", "")
TELEGRAM_PRIVATE_CHAT_ID = os.environ.get("TELEGRAM_PRIVATE_CHAT_ID", "")

# ===== QMT =====
QMT_HOST = os.environ.get("QMT_HOST", "192.168.1.33")
QMT_PORT = os.environ.get("QMT_PORT", "5000")
QMT_BASE_URL = f"http://{QMT_HOST}:{QMT_PORT}"

# ===== 代理 =====
PROXY_ENABLED = os.environ.get("PROXY_ENABLED", "false").lower() == "true"

# ===== 交易 =====
ACCOUNT_MODE = os.environ.get("ACCOUNT_MODE", "manual")
MAX_SINGLE_STOCK_PCT = 0.20
MAX_SINGLE_SECTOR_PCT = 0.70
CASH_RESERVE_PCT = 0.20
ENV_POSITION_LIMIT = {"bull": 0.80, "swing": 0.50, "bear": 0.20}
MAX_DAILY_LOSS = 0.03
DEFAULT_TRAILING_STOP = 0.05
DEFAULT_TAKE_PROFIT_RATIO = 0.20
DEFAULT_SLIPPAGE = 0.001
DEFAULT_COMMISSION_RATE = 0.000085  # 万0.85
STAMP_TAX_RATE = 0.0005  # 万分之五（2023年8月减半后标准，卖出单边征收）
MIN_COMMISSION = 5.0
MIN_DAILY_AMOUNT = 100_000_000
MIN_LISTED_DAYS = 60

# ===== 模拟盘 / 实盘 =====
PAPER_INITIAL_CAPITAL = float(os.environ.get("PAPER_INITIAL_CAPITAL", 200_000))
REAL_INITIAL_CAPITAL = float(os.environ.get("REAL_INITIAL_CAPITAL", 200_000))
REAL_TRADE_ENABLED = os.environ.get("REAL_TRADE_ENABLED", "false").lower() == "true"
DEFAULT_POSITION_PCT = float(os.environ.get("DEFAULT_POSITION_PCT", 0.16))
MAX_POSITIONS = int(os.environ.get("MAX_POSITIONS", 5))
SWAP_SCORE_GAP = float(os.environ.get("SWAP_SCORE_GAP", 15))
MAX_ACCOUNT_DRAWDOWN = float(os.environ.get("MAX_ACCOUNT_DRAWDOWN", 0.15))
REVIEW_PICK_POSITION_PCT = float(os.environ.get("REVIEW_PICK_POSITION_PCT", 0.08))

# ===== 异动检测阈值 =====
ABNORMAL_RAPID_RISE_PCT = 1.0  # 急速拉升: 当前涨幅 - 上轮涨幅 > 1%
ABNORMAL_VOLUME_SURGE_RATIO = 3.0  # 量比暴增: 当前成交量 > 上轮成交量 × 3
ABNORMAL_NEAR_LIMIT_PCT = 8.5  # 逼近涨停: 涨幅 > 8.5%

# ===== 板块共振/逆势分析 =====
RESONANCE_INDEX_DIRECTION_THRESHOLD = 0.001  # 指数方向判定: 变化率 > 0.1% 视为有方向
RESONANCE_SECTOR_DIRECTION_THRESHOLD = 0.1  # 板块方向判定: 百分点差 > 0.1pp 视为有方向
RESONANCE_VOLATILITY_TRIGGER = 0.003  # 独立推送触发: 大盘波动 ≥ 0.3%
RESONANCE_PUSH_WINDOW_ENTRIES = 4  # 独立推送窗口: 板块趋势条目数 (~12分钟)
RESONANCE_TOP5_WINDOW_ENTRIES = 17  # TOP5 标签窗口: 板块趋势条目数 (~50分钟)
RESONANCE_TOP_N = 5  # 各分类取前N名
RESONANCE_LEADER_COUNT = 3  # 领涨/领跌股数量
RESONANCE_PUSH_COOLDOWN_ROUNDS = 15  # 独立推送冷却轮数
RESONANCE_VOL_SURGE_RATIO = 1.5  # 放量标签: 量比 > 1.5
RESONANCE_VOL_SHRINK_RATIO = 0.5  # 缩量标签: 量比 < 0.5
RESONANCE_INDEX_WINDOW_MAX = 30  # 指数窗口最大点数
RESONANCE_INDEX_MIN_POINTS = 12  # 指数最小数据点数

# ===== 盯盘自审计 =====
AUDIT_ENABLED = os.environ.get("AUDIT_ENABLED", "true").lower() == "true"
AUDIT_AUTO_APPLY_PARAM = (
    os.environ.get("AUDIT_AUTO_APPLY_PARAM", "false").lower() == "true"
)
AUDIT_RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "90"))

# ===== 市场环境判定 =====
MA_PERIOD = 20
SWING_THRESHOLD = 0.03

# ===== 筛选策略 =====
STOCK_BASIC_RETENTION_DAYS = (
    120  # stock_basic 保留天数（RPS 计算需要，当前 21 天自然增长中）
)
SCREENING_MIN_MCAP_YI = 50  # 市值下限（亿）
RPS_THRESHOLD_TOP = 0.20  # RPS 前 20% 为强势
RPS_RESONANCE_THRESHOLD = 0.30  # RPS 多周期共振阈值（前 30%）
MARKET_BREADTH_BULL = 3000  # 普涨: 上涨家数 > 3000
MARKET_BREADTH_DIVIDE = 1500  # 分化: 上涨 1500~3000
MARKET_BREADTH_BEAR = 800  # 普跌/恐慌: 上涨 < 800
MARKET_BREADTH_BOUNCE = 2000  # 连跌修复: 恐慌后首日涨家数 > 2000
REGIME_STABLE_SCANS = 8  # 新 regime 需连续 N 轮一致才确认切换（~8分钟）
REGIME_JITTER_WINDOW = 5  # 5 分钟内切换超过 REGIME_JITTER_MAX 次触发告警
REGIME_JITTER_MAX = 3
BREADTH_DOWN_UP_RATIO = 3.0  # 下跌/上涨 > 此值且指数跌时暂停新开仓

# ===== 自适应交易 =====
# Phase 1: 早盘 AI 板块倾向
MORNING_SECTOR_BIAS_ENABLED = (
    os.environ.get("MORNING_SECTOR_BIAS_ENABLED", "true").lower() == "true"
)
# Phase 2: 盘中动态板块发现
DYNAMIC_SECTOR_DISCOVERY_ENABLED = (
    os.environ.get("DYNAMIC_SECTOR_DISCOVERY_ENABLED", "true").lower() == "true"
)
DYNAMIC_SECTOR_HEAT_THRESHOLD = int(
    os.environ.get("DYNAMIC_SECTOR_HEAT_THRESHOLD", "3")
)
DYNAMIC_SECTOR_MAX_CANDIDATES = int(
    os.environ.get("DYNAMIC_SECTOR_MAX_CANDIDATES", "10")
)
# Phase 3: 板块轮动
SECTOR_ROTATION_ENABLED = (
    os.environ.get("SECTOR_ROTATION_ENABLED", "false").lower() == "true"
)
SECTOR_ROTATION_COOLDOWN_SCANS = int(
    os.environ.get("SECTOR_ROTATION_COOLDOWN_SCANS", "30")
)

# ===== Phase 4: 盘中回踩机会发现 =====
PULLBACK_SCAN_ENABLED = (
    os.environ.get("PULLBACK_SCAN_ENABLED", "true").lower() == "true"
)
PULLBACK_SCAN_INTERVAL = int(
    os.environ.get("PULLBACK_SCAN_INTERVAL", "12")  # 每12轮约12分钟
)
PULLBACK_SECTOR_MIN_CHANGE = float(
    os.environ.get("PULLBACK_SECTOR_MIN_CHANGE", "0.5")  # 板块涨幅>0.5%
)
PULLBACK_PRICE_MIN = float(
    os.environ.get("PULLBACK_PRICE_MIN", "10.0")  # 最低价格过滤
)

# ===== 工具/缓存 =====
DNS_CACHE_TTL = 600  # DNS 绕过缓存 TTL（秒）
NAME_RESOLVE_CACHE_SIZE = 512  # 股票名称→代码 LRU 缓存上限
