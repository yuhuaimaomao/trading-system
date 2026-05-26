# Phase 1: 基础架构搭建

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建 trading-system 新目录骨架，迁移通用工具和配置，清理旧 `trading/` 扁平结构，建立可运行的 CLI 入口

**Architecture:** 功能平铺——一个目录一件事。risk/、portfolio/、strategy/、execution/、monitor/ 各司其职，通过 db/ 和信号模型通信。qmt/ 作为独立基础设施层被多模块共享。

**Tech Stack:** Python 3.10+, SQLite, requests, pandas, numpy

---

### Task 1: 创建新目录结构

**说明:** 清空旧的空壳 `__init__.py`（strategy/screening/timing/rules, execution, backtest, pipeline），创建新目录骨架

**Files:**
- Create: 一批 `__init__.py` 文件
- Delete: 旧空壳目录

- [ ] **Step 1: 删除旧的无用空壳目录**

```bash
cd ~/trading-system
# 删除旧 strategy 空壳子目录（会被 strategy/screening/ 替代）
rm -rf trading/strategies/rules trading/strategies/screening trading/strategies/timing
# 删除旧空壳（会在新位置重建）
rm -rf trading/backtest trading/pipeline trading/execution
```

- [ ] **Step 2: 创建新的一级目录和 __init__.py**

```bash
cd ~/trading-system
mkdir -p collectors/proxy collectors/market collectors/events collectors/macro
mkdir -p review/screening review/chapters review/readers
mkdir -p strategy/screening strategy/factors strategy/backtest
mkdir -p risk/rules
mkdir -p execution
mkdir -p portfolio
mkdir -p monitor
mkdir -p db
mkdir -p storage/logs storage/cache storage/reports
mkdir -p scheduler
mkdir -p tests

# 所有目录加 __init__.py
for d in collectors/proxy collectors/market collectors/events collectors/macro \
         review/screening review/chapters review/readers \
         strategy/screening strategy/factors strategy/backtest \
         risk/rules execution monitor db; do
    touch "$d/__init__.py"
done
```

- [ ] **Step 3: 验证目录结构**

```bash
cd ~/trading-system && find . -type d -not -path '*/.git/*' -not -path '*/__pycache__/*' -not -path '*/docs/*' | sort
```

预期看到完整的新目录树。

- [ ] **Step 4: Commit**

```bash
cd ~/trading-system && git init && git add -A && git commit -m "feat: create Phase 1 directory skeleton"
```

---

### Task 2: 迁移 config/ 配置

**说明:** 合并 quant-system 和 trading-system 的 settings，迁入 akshare/proxy 配置，创建 prompts/ 目录

**Files:**
- Modify: `config/settings.py`
- Create: `config/akshare_config.py`, `config/proxy_config.py`, `config/prompts/__init__.py`
- Create: `config/prompts/review.py`（从 quant-system 迁入）

- [ ] **Step 1: 合并 settings.py**

先读两份 settings 确认差异：

```bash
cat ~/trading-system/config/settings.py
cat ~/quant-system/config/settings.py
```

- [ ] **Step 2: 写合并后的 settings.py**

```python
"""trading-system 统一配置"""

import os
from pathlib import Path

# ===== 项目根目录 =====
PROJECT_ROOT = Path(__file__).parent.parent

# ===== 路径 =====
DATABASE_PATH = os.environ.get(
    "TRADING_DB_PATH",
    os.path.expanduser("~/quant-system/storage/stock_market.db"),
)
LOGS_DIR = os.environ.get(
    "TRADING_LOGS_DIR",
    str(PROJECT_ROOT / "storage" / "logs"),
)
STORAGE_PATH = os.environ.get(
    "STORAGE_PATH",
    str(PROJECT_ROOT / "storage"),
)

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
ACCOUNT_MODE = os.environ.get("ACCOUNT_MODE", "manual")  # "manual" | "paper" | "live"
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
```

- [ ] **Step 3: 迁入 akshare 和 proxy 配置**

```bash
cp ~/quant-system/config/akshare_config.py ~/trading-system/config/akshare_config.py
cp ~/quant-system/config/proxy_config.py ~/trading-system/config/proxy_config.py
```

- [ ] **Step 4: 迁入复盘 Prompt 模板**

```bash
cp ~/quant-system/config/review_report_prompt.py ~/trading-system/config/prompts/review.py
touch ~/trading-system/config/prompts/__init__.py
```

- [ ] **Step 5: 验证 config 导入**

```bash
cd ~/trading-system && python -c "from config.settings import DATABASE_PATH, LOGS_DIR, DASHSCOPE_API_KEY; print('OK:', DATABASE_PATH)"
```

- [ ] **Step 6: Commit**

```bash
cd ~/trading-system && git add config/ && git commit -m "feat: merge and extend config with quant-system settings"
```

---

### Task 3: 迁移 utils/ 工具层

**说明:** 用 quant-system 的三层日志替换 trading-system 的简化版，迁入缺失的工具文件

**Files:**
- Modify: `utils/logger.py`（替换）
- Modify: `utils/telegram_bot.py` → `utils/telegram.py`（重命名，统一命名）
- Create: `utils/stock_code_utils.py`, `utils/decorators.py`, `utils/function_calling.py`, `utils/stock_tools.py`

- [ ] **Step 1: 替换 logger.py 为 quant-system 版本**

trading-system 版 logger 的 docstring 里提的是 "auction"，quant-system 版是 "review"，功能完全一致。直接覆盖：

```bash
cp ~/quant-system/utils/logger.py ~/trading-system/utils/logger.py
```

- [ ] **Step 2: 重命名 telegram_bot.py 为 telegram.py**

```bash
cd ~/trading-system
mv utils/telegram_bot.py utils/telegram.py
```

- [ ] **Step 3: 更新 telegram.py 中的跨模块引用**

`utils/telegram.py` 内部引用了 `utils/logger.py`，路径不变，无需修改。检查一下：

```bash
grep -n "from utils" ~/trading-system/utils/telegram.py
```

- [ ] **Step 4: 迁入 stock_code_utils.py, decorators.py, function_calling.py, stock_tools.py**

```bash
cp ~/quant-system/utils/stock_code_utils.py ~/trading-system/utils/
cp ~/quant-system/utils/decorators.py ~/trading-system/utils/
cp ~/quant-system/utils/function_calling.py ~/trading-system/utils/
cp ~/quant-system/utils/stock_tools.py ~/trading-system/utils/
```

- [ ] **Step 5: 验证所有 utils 模块可导入**

```bash
cd ~/trading-system && python -c "
from utils.logger import set_current_task, get_task_logger, get_collector_logger, get_core_logger, get_system_logger
from utils.telegram import MessageSender
from utils.stock_code_utils import strip_suffix
from utils.decorators import retry
print('OK: all utils imported')
"
```

注：`function_calling.py` 和 `stock_tools.py` 可能依赖 `config` 下的其他模块，导入失败不要紧——阶段 3 用到时才修。

- [ ] **Step 6: Commit**

```bash
cd ~/trading-system && git add utils/ && git commit -m "feat: migrate utils from quant-system (logger v3.1, telegram, stock tools)"
```

---

### Task 4: 设置 qmt/ 基础设施层

**说明:** 将现有 `qmt/http_client.py` 重命名为 `qmt/client.py`，新增 `quotes.py`、`calendar.py`、`orders.py` 占位

**Files:**
- Modify: `qmt/http_client.py` → `qmt/client.py`
- Create: `qmt/quotes.py`, `qmt/calendar.py`, `qmt/orders.py`
- Modify: `qmt/__init__.py`

- [ ] **Step 1: 重命名 http_client.py**

```bash
cd ~/trading-system
mv qmt/http_client.py qmt/client.py
```

- [ ] **Step 2: 更新 client.py 内部引用**

现有 `client.py` 没有内部跨模块引用，检查确认：

```bash
grep -n "from qmt\|import qmt\|from . " ~/trading-system/qmt/client.py || echo "no internal refs"
```

- [ ] **Step 3: 创建 quotes.py — 行情接口封装**

```python
# -*- coding: utf-8 -*-
"""QMT 行情接口封装 — 实时快照、Tick、K线"""

from qmt.client import QMTClient


class QuoteClient:
    """实时行情客户端，封装 QMT HTTP 行情接口"""

    def __init__(self, client: QMTClient = None):
        self._client = client or QMTClient()

    def get_realtime(self, codes: list[str]) -> dict:
        """批量获取实时行情快照，返回 {code: {price, volume, ...}}"""
        result = self._client.quotes(codes)
        if not result.get("success", True):
            return {}
        data = result.get("data", result)
        return {item.get("code", ""): item for item in data} if isinstance(data, list) else data

    def get_price(self, code: str) -> float | None:
        """获取单只股票最新价"""
        result = self._client.quote(code)
        if not result.get("success", True):
            return None
        data = result.get("data", result)
        return data.get("last_price") or data.get("lastPrice") or data.get("price")

    def get_minute_kline(self, code: str, count: int = 240) -> list[dict]:
        """获取分钟K线"""
        result = self._client.minute_kline(code, count=count)
        if not result.get("success", True):
            return []
        return result.get("data", result) or []

    def get_history(self, code: str, period: str = "1d",
                    start: str = None, end: str = None, count: int = None) -> list[dict]:
        """获取历史K线"""
        result = self._client.history(code, period=period, start=start, end=end, count=count)
        if not result.get("success", True):
            return []
        return result.get("data", result) or []
```

- [ ] **Step 4: 创建 calendar.py — 交易日历**

```python
# -*- coding: utf-8 -*-
"""交易日历 — 通过 QMT /calendar 接口查询，替代静态 trading_calendar.py"""

from datetime import date
from qmt.client import QMTClient


class TradingCalendar:
    """交易日历查询"""

    _cache_date: str = ""
    _cache_dates: set[str] = set()

    def __init__(self, client: QMTClient = None):
        self._client = client or QMTClient()

    def is_trading_day(self, d: date = None) -> bool:
        """判断是否为交易日"""
        if d is None:
            d = date.today()
        ds = d.isoformat()
        self._ensure_cache()
        return ds in self._cache_dates

    def _ensure_cache(self):
        """加载交易日列表，每天只查一次"""
        today = date.today().isoformat()
        if self._cache_date == today:
            return
        result = self._client.calendar("sh")
        if result.get("success") is False:
            return
        data = result.get("data", result)
        if isinstance(data, list):
            self._cache_dates = set(data)
        elif isinstance(data, dict):
            self._cache_dates = set(data.get("trade_days", data.get("dates", [])))
        self._cache_date = today
```

- [ ] **Step 5: 创建 orders.py — 下单接口占位**

```python
# -*- coding: utf-8 -*-
"""QMT 下单接口 — 预留，待 QMT 实测后实现"""


class OrderClient:
    """QMT 订单客户端"""

    def __init__(self, client=None):
        pass

    def buy(self, code: str, price: float, volume: int) -> dict:
        """限价买入"""
        raise NotImplementedError("待 QMT 下单接口实测后实现")

    def sell(self, code: str, price: float, volume: int) -> dict:
        """限价卖出"""
        raise NotImplementedError("待 QMT 下单接口实测后实现")

    def cancel(self, order_id: str) -> dict:
        """撤单"""
        raise NotImplementedError("待 QMT 下单接口实测后实现")
```

- [ ] **Step 6: 更新 qmt/__init__.py**

```python
from qmt.client import QMTClient
from qmt.quotes import QuoteClient
from qmt.calendar import TradingCalendar
from qmt.orders import OrderClient
```

- [ ] **Step 7: 验证 qmt 模块可导入**

```bash
cd ~/trading-system && python -c "
from qmt import QMTClient, QuoteClient, TradingCalendar, OrderClient
c = QMTClient()
print('OK: qmt module')
"
```

- [ ] **Step 8: Commit**

```bash
cd ~/trading-system && git add qmt/ && git commit -m "feat: restructure qmt/ with client, quotes, calendar, orders placeholder"
```

---

### Task 5: 移动 risk/ 和 portfolio/（从旧 trading/ 迁移）

**说明:** 现有风控引擎和组合管理代码功能完整，直接搬到新位置，更新 import 路径

**Files:**
- Move: `trading/risk/` → `risk/`
- Move: `trading/portfolio/` → `portfolio/`

- [ ] **Step 1: 移动 risk/ 目录**

```bash
cd ~/trading-system
# 移动所有风控文件，保留已有 rules/ 子目录
cp -r trading/risk/rules/* risk/rules/
cp trading/risk/engine.py risk/engine.py
# 删除旧的 risk/__init__.py（Step 1 已建新壳）
rm risk/__init__.py && touch risk/__init__.py
```

- [ ] **Step 2: 更新 risk/engine.py 的 import 路径**

engine.py 内部引用 `trading.portfolio.portfolio` 和 `trading.risk.rules.*`，需要改为新路径：

```bash
cd ~/trading-system
# 批量替换 import 路径
sed -i '' 's/from trading.portfolio.portfolio import/from portfolio.portfolio import/g' risk/engine.py
sed -i '' 's/from trading.risk.rules./from risk.rules./g' risk/engine.py
sed -i '' 's/from trading.risk.rules import/from risk.rules import/g' risk/engine.py
```

检查替换结果：

```bash
grep -n "trading\." ~/trading-system/risk/engine.py || echo "no old imports"
```

- [ ] **Step 3: 同样更新所有 risk/rules/ 下的 import**

```bash
cd ~/trading-system
for f in risk/rules/*.py; do
    sed -i '' 's/from trading.risk.rules./from risk.rules./g' "$f"
done
grep -rn "trading\." risk/ || echo "no old imports"
```

- [ ] **Step 4: 移动 portfolio/ 目录**

```bash
cd ~/trading-system
cp trading/portfolio/portfolio.py portfolio/
cp trading/portfolio/performance.py portfolio/
rm portfolio/__init__.py && touch portfolio/__init__.py
```

- [ ] **Step 5: 更新 portfolio/ 的 import**

portfolio.py 和 performance.py 没有 `trading.` 前缀的跨模块引用，都是相对独立的。检查：

```bash
grep -rn "trading\." ~/trading-system/portfolio/ || echo "no old imports"
```

- [ ] **Step 6: 验证 risk 和 portfolio 可导入**

```bash
cd ~/trading-system && python -c "
from portfolio.portfolio import Portfolio, Position, PortfolioSnapshot
from portfolio.performance import calc_max_drawdown, calc_sharpe_ratio, calc_win_rate, calc_profit_loss_ratio
from risk.engine import RiskEngine, RiskResult
from risk.rules.stop_loss import check_stop_loss
from risk.rules.take_profit import check_take_profit, check_trailing_stop
from risk.rules.max_drawdown import check_daily_loss_limit
from risk.rules.concentration import check_concentration
from risk.rules.market_env import get_market_environment, get_max_position
from risk.rules.blacklist import is_blacklisted
print('OK: risk and portfolio migrated')
"
```

- [ ] **Step 7: Commit**

```bash
cd ~/trading-system && git add risk/ portfolio/ && git commit -m "feat: migrate risk engine and portfolio to top-level modules"
```

---

### Task 6: 设置 strategy/（信号模型 + 因子框架）

**说明:** 将 `trading/signal/model.py` 移到 `strategy/signals.py`，`trading/factors/` 移到 `strategy/factors/`，更新 import

**Files:**
- Move: `trading/signal/model.py` → `strategy/signals.py`
- Move: `trading/factors/` → `strategy/factors/`

- [ ] **Step 1: 移动 signals 和 factors**

```bash
cd ~/trading-system
cp trading/signal/model.py strategy/signals.py
cp trading/factors/base.py strategy/factors/
cp trading/factors/registry.py strategy/factors/
cp trading/factors/preprocess.py strategy/factors/ 2>/dev/null || echo "no preprocess.py"
cp -r trading/factors/technical strategy/factors/ 2>/dev/null || echo "no technical dir"
cp -r trading/factors/fundamental strategy/factors/ 2>/dev/null || echo "no fundamental dir"
cp -r trading/factors/alternative strategy/factors/ 2>/dev/null || echo "no alternative dir"
rm -f strategy/factors/__init__.py && touch strategy/factors/__init__.py
```

- [ ] **Step 2: 更新 signals.py 的 import**

signals.py 原本没有 `trading.` 前缀的内部引用，是独立的 dataclass 定义。检查：

```bash
grep -n "trading\." ~/trading-system/strategy/signals.py || echo "no old imports"
```

- [ ] **Step 3: 更新 strategy/factors/ 的 import**

```bash
cd ~/trading-system
for f in strategy/factors/*.py; do
    sed -i '' 's/from trading.factors./from strategy.factors./g' "$f"
done
grep -rn "trading\." strategy/factors/ || echo "no old imports"
```

- [ ] **Step 4: 验证 strategy 模块可导入**

```bash
cd ~/trading-system && python -c "
from strategy.signals import OrderSignal, SignalType, SignalSource
from strategy.factors.base import FactorBase
from strategy.factors.registry import FactorRegistry, factor_registry
print('OK: strategy module')
"
```

- [ ] **Step 5: Commit**

```bash
cd ~/trading-system && git add strategy/ && git commit -m "feat: migrate signal model and factor framework to strategy/"
```

---

### Task 7: 设置 db/ 数据访问层

**说明:** 将 `trading/db/` 移到顶层 `db/`，更新 import，给 trade_ 表加 `account` 字段

**Files:**
- Move: `trading/db/schema.py` → `db/schema.py`
- Move: `trading/db/repository.py` → `db/repository.py`

- [ ] **Step 1: 移动 db 文件**

```bash
cd ~/trading-system
cp trading/db/schema.py db/schema.py
cp trading/db/repository.py db/repository.py
rm db/__init__.py && touch db/__init__.py
```

- [ ] **Step 2: 更新 schema.py 和 repository.py 的 import**

```bash
cd ~/trading-system
sed -i '' 's/from config.settings import/from config.settings import/g' db/schema.py
sed -i '' 's/from config.settings import/from config.settings import/g' db/repository.py
```

repository.py 有一处 `from config.settings import DATABASE_PATH`，路径正确不需要改。但第30行附近有一段死代码（broken list comprehension），顺手修复。

- [ ] **Step 3: 修复 repository.py 中 `get_pending_signals` 的 bug**

现有实现有两段重复代码且有 bug。替换为：

```python
def get_pending_signals(self, trade_date: str) -> list[dict]:
    conn = self._conn()
    rows = conn.execute(
        "SELECT * FROM trade_signals WHERE trade_date=? AND status='pending'",
        (trade_date,),
    ).fetchall()
    conn.close()
    cols = ["id", "trade_date", "created_at", "signal_type", "signal_source",
            "stock_code", "stock_name", "buy_zone_min", "buy_zone_max",
            "target_position", "stop_loss", "take_profit", "trailing_stop",
            "signal_score", "strategy_name", "reason", "status", "executed_at"]
    return [dict(zip(cols, row)) for row in rows]
```

- [ ] **Step 4: 给 trade_ 表加 account 字段**

在 `db/schema.py` 的 `ensure_tables()` 末尾、`conn.close()` 之前，加入迁移 SQL：

```python
# 添加 account 字段（幂等迁移）
for table in ["trade_signals", "trade_orders", "trade_portfolio_snapshots",
              "trade_factor_values", "trade_strategy_metrics"]:
    try:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN account TEXT DEFAULT 'real'")
    except sqlite3.OperationalError:
        pass  # 字段已存在
```

- [ ] **Step 5: 验证 db 模块**

```bash
cd ~/trading-system && python -c "
from db.schema import ensure_tables
from db.repository import TradeRepository
ensure_tables()
repo = TradeRepository()
print('OK: db module')
"
```

- [ ] **Step 6: Commit**

```bash
cd ~/trading-system && git add db/ && git commit -m "feat: migrate db layer, add account field, fix get_pending_signals bug"
```

---

### Task 8: 设置 execution/ 执行层骨架

**说明:** 创建 manual.py（实盘手动）和 paper.py（模拟盘自动）的骨架，qmt.py 预留自动下单

**Files:**
- Create: `execution/manual.py`, `execution/paper.py`, `execution/qmt.py`

- [ ] **Step 1: 创建 manual.py — 实盘手动执行器**

```python
# -*- coding: utf-8 -*-
"""实盘手动执行器 — Telegram 推送 → 用户确认 → 记录持仓"""

from datetime import datetime
from db.repository import TradeRepository
from strategy.signals import OrderSignal


class ManualExecutor:
    """实盘手动执行：推送 Telegram 提示，等待用户确认后记录"""

    def __init__(self, telegram_sender=None):
        self.repo = TradeRepository()
        self.telegram = telegram_sender

    def submit(self, signal: OrderSignal, account: str = "real") -> int:
        """将信号写入 DB，返回 signal_id。用户确认前状态为 pending"""
        signal_dict = {
            "trade_date": signal.trade_date,
            "created_at": datetime.now().isoformat(),
            "signal_type": signal.signal_type.name,
            "signal_source": signal.source.name,
            "stock_code": signal.stock_code,
            "stock_name": signal.stock_name,
            "buy_zone_min": signal.buy_zone_min,
            "buy_zone_max": signal.buy_zone_max,
            "target_position": signal.target_position,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "trailing_stop": signal.trailing_stop,
            "signal_score": signal.signal_score,
            "strategy_name": signal.strategy_name,
            "reason": signal.reason,
            "status": "pending",
            "account": account,
        }
        signal_id = self.repo.insert_signal(signal_dict)
        return signal_id

    def notify(self, signal: OrderSignal):
        """生成 Telegram 推送文本"""
        if self.telegram is None:
            return
        msg = signal.__repr__()
        self.telegram.send(msg)

    def confirm(self, signal_id: int, price: float, volume: int):
        """用户确认买入后记录成交"""
        self.repo.update_signal_status(signal_id, "executed")
        # 记录订单
        self.repo.insert_order({
            "signal_id": signal_id,
            "trade_date": datetime.now().strftime("%Y-%m-%d"),
            "order_time": datetime.now().isoformat(),
            "stock_code": "",
            "order_type": "buy",
            "order_price": price,
            "order_volume": volume,
            "order_status": "filled",
            "filled_volume": volume,
            "filled_price": price,
            "filled_amount": price * volume,
            "strategy_name": "",
            "updated_at": datetime.now().isoformat(),
            "account": "real",
        })
```

- [ ] **Step 2: 创建 paper.py — 模拟盘自动执行器**

```python
# -*- coding: utf-8 -*-
"""模拟盘自动执行器 — 模拟成交+滑点+佣金，自动执行"""

from datetime import datetime
from db.repository import TradeRepository
from strategy.signals import OrderSignal


class PaperExecutor:
    """模拟盘：自动按当前价+滑点成交"""

    def __init__(self, slippage: float = 0.001, commission_rate: float = 0.0001):
        self.repo = TradeRepository()
        self.slippage = slippage
        self.commission_rate = commission_rate

    def execute_buy(self, signal: OrderSignal, current_price: float,
                    account: str = "paper") -> int | None:
        """模拟买入：当前价×（1+滑点）成交"""
        fill_price = current_price * (1 + self.slippage)
        # 计算可买股数（整百）
        # TODO: 待 portfolio 对接后实现仓位计算
        return None

    def execute_sell(self, signal: OrderSignal, current_price: float,
                     account: str = "paper") -> int | None:
        """模拟卖出：当前价×（1-滑点）成交"""
        fill_price = current_price * (1 - self.slippage)
        # TODO: 待 portfolio 对接后实现
        return None
```

- [ ] **Step 3: 创建 qmt.py — QMT 自动下单（预留）**

```python
# -*- coding: utf-8 -*-
"""QMT 自动下单执行器 — 预留，待 QMT 实测后实现"""

from strategy.signals import OrderSignal


class QMTExecutor:
    """QMT 全自动执行"""

    def __init__(self):
        pass

    def execute(self, signal: OrderSignal, account: str = "live") -> bool:
        raise NotImplementedError("待 QMT 下单接口实测后实现")
```

- [ ] **Step 4: 更新 execution/__init__.py**

```python
from execution.manual import ManualExecutor
from execution.paper import PaperExecutor
from execution.qmt import QMTExecutor
```

- [ ] **Step 5: 验证 execution 模块可导入**

```bash
cd ~/trading-system && python -c "
from execution import ManualExecutor, PaperExecutor, QMTExecutor
print('OK: execution module')
"
```

- [ ] **Step 6: Commit**

```bash
cd ~/trading-system && git add execution/ && git commit -m "feat: create execution layer (manual + paper + qmt placeholder)"
```

---

### Task 9: 创建 main.py CLI 入口骨架

**说明:** 创建命令路由器，每个命令先占位，阶段 2-4 逐步填充

**Files:**
- Create: `main.py`

- [ ] **Step 1: 写 main.py**

```python
# -*- coding: utf-8 -*-
"""trading-system CLI 入口"""

import sys
from datetime import date

COMMANDS = ["review", "morning", "monitor", "collect", "cleanup",
            "portfolio", "trade", "backtest", "test"]


def cmd_review():
    print("[review] 待实现 — 盘后全流程（采集+筛选+AI+报告+推送）")

def cmd_morning():
    print("[morning] 待实现 — 盘前简报（隔夜宏观+候选池确认+推送）")

def cmd_monitor():
    print("[monitor] 待实现 — 盘中盯盘")

def cmd_collect():
    print("[collect] 待实现 — 数据采集")

def cmd_cleanup():
    print("[cleanup] 待实现 — 周清理")

def cmd_portfolio():
    print("[portfolio] 待实现 — 持仓查询")
    from portfolio.portfolio import Portfolio
    p = Portfolio()
    print(f"  现金: {p.cash:.2f}  总资产: {p.total_value:.2f}  持仓数: {len(p.positions)}")

def cmd_trade():
    print("[trade] 待实现 — 手动录入交易")
    print("  python main.py trade --add --code 000001 --price 12.34 --volume 1000 --account real")

def cmd_backtest():
    print("[backtest] 待实现 — 回测")

def cmd_test():
    print("[test] 配置检查...")
    from config.settings import DATABASE_PATH, LOGS_DIR, DASHSCOPE_API_KEY
    print(f"  DB: {DATABASE_PATH}")
    print(f"  Logs: {LOGS_DIR}")
    print(f"  千问 API: {'已配置' if DASHSCOPE_API_KEY else '未配置'}")
    print("  OK")


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py <command> [options]")
        print(f"Commands: {', '.join(COMMANDS)}")
        sys.exit(1)

    cmd = sys.argv[1]
    {
        "review": cmd_review, "morning": cmd_morning,
        "monitor": cmd_monitor, "collect": cmd_collect,
        "cleanup": cmd_cleanup, "portfolio": cmd_portfolio,
        "trade": cmd_trade, "backtest": cmd_backtest, "test": cmd_test,
    }.get(cmd, lambda: print(f"Unknown: {cmd}"))()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 测试 CLI 命令**

```bash
cd ~/trading-system
python main.py                  # 无参数应输出帮助
python main.py test             # 配置检查
python main.py portfolio        # 持仓查询（空持仓）
python main.py review           # 占位输出
```

- [ ] **Step 3: Commit**

```bash
cd ~/trading-system && git add main.py && git commit -m "feat: create main.py CLI skeleton with 9 commands"
```

---

### Task 10: 设置 storage/ 和 scheduler/

**说明:** 创建 .env.example、requirements.txt 合并、scheduler 占位脚本

**Files:**
- Create: `.env.example`
- Modify: `requirements.txt`
- Create: `scheduler/start_review.sh`, `scheduler/start_morning.sh`, `scheduler/start_monitor.sh`, `scheduler/start_news_collection.sh`, `scheduler/start_cleanup.sh`

- [ ] **Step 1: 创建 .env.example**

```bash
cat > ~/trading-system/.env.example << 'EOF'
# Trading System - 环境变量配置
# 复制此文件为 .env 并填写实际值

# ===== API Keys =====
DASHSCOPE_API_KEY=sk-xxx
DASHSCOPE_MODEL=qwen-plus
DASHSCOPE_ANALYSIS_MODEL=qwen3.6-plus
DEEPSEEK_API_KEY=sk-xxx
DEEPSEEK_MODEL=deepseek-chat

# ===== Telegram 配置 =====
TELEGRAM_CHAT_ID=-xxx
TELEGRAM_REPORT_CHAT_ID=-xxx
TELEGRAM_REPORT_BOT_TOKEN=xxx

# ===== QMT =====
QMT_HOST=192.168.1.33
QMT_PORT=5000

# ===== 存储 =====
DATABASE_PATH=~/quant-system/storage/stock_market.db
STORAGE_PATH=~/trading-system/storage

# ===== 代理 =====
PROXY_ENABLED=false

# ===== 交易 =====
ACCOUNT_MODE=manual
EOF
```

- [ ] **Step 2: 合并 requirements.txt**

```bash
cat > ~/trading-system/requirements.txt << 'EOF'
# Trading System - Python 依赖
# Python 版本：3.10+

# ===== 核心依赖 =====
pandas>=2.0.0
numpy>=1.24.0
requests>=2.28.0
beautifulsoup4>=4.12.0
lxml>=4.9.0

# ===== 数据源 =====
akshare>=1.10.0

# ===== 配置管理 =====
python-dotenv>=1.0.0
PyYAML>=6.0

# ===== 工具库 =====
python-dateutil>=2.8.2
openpyxl>=3.1.0
curl_cffi>=0.5.0

# ===== 金融数据 =====
yfinance>=0.2.30

# ===== 机器学习 =====
scikit-learn>=1.3
EOF
```

- [ ] **Step 3: 创建 scheduler 脚本**

```bash
cd ~/trading-system

# start_review.sh
cat > scheduler/start_review.sh << 'EOF'
#!/bin/bash
set -e
cd ~/trading-system
source venv/bin/activate 2>/dev/null || true
exec python main.py review >> storage/logs/cron_review.log 2>&1
EOF

# start_morning.sh
cat > scheduler/start_morning.sh << 'EOF'
#!/bin/bash
set -e
cd ~/trading-system
source venv/bin/activate 2>/dev/null || true
exec python main.py morning >> storage/logs/cron_morning.log 2>&1
EOF

# start_monitor.sh
cat > scheduler/start_monitor.sh << 'EOF'
#!/bin/bash
set -e
cd ~/trading-system
source venv/bin/activate 2>/dev/null || true
exec python main.py monitor >> storage/logs/cron_monitor.log 2>&1
EOF

# start_news_collection.sh
cat > scheduler/start_news_collection.sh << 'EOF'
#!/bin/bash
set -e
cd ~/trading-system
source venv/bin/activate 2>/dev/null || true
exec python main.py collect --module news >> storage/logs/cron_news.log 2>&1
EOF

# start_cleanup.sh
cat > scheduler/start_cleanup.sh << 'EOF'
#!/bin/bash
set -e
cd ~/trading-system
source venv/bin/activate 2>/dev/null || true
exec python main.py cleanup >> storage/logs/cron_cleanup.log 2>&1
EOF

chmod +x scheduler/*.sh
```

- [ ] **Step 4: 创建 .gitignore**

```bash
cat > ~/trading-system/.gitignore << 'EOF'
.env
venv/
__pycache__/
*.pyc
.DS_Store
storage/logs/
storage/cache/
storage/reports/
EOF
```

- [ ] **Step 5: Commit**

```bash
cd ~/trading-system && git add .env.example requirements.txt scheduler/ .gitignore && git commit -m "feat: setup .env.example, requirements, scheduler scripts, .gitignore"
```

---

### Task 11: 清理旧结构 + 最终验证

**说明:** 删除旧的 `trading/` 目录，确保所有 import 在新路径下正常工作

**Files:**
- Delete: `trading/` 整个目录（已迁移）

- [ ] **Step 1: 删除旧 trading/ 目录**

```bash
cd ~/trading-system
rm -rf trading/
```

- [ ] **Step 2: 全文搜索残留的 `from trading.` 引用**

```bash
cd ~/trading-system && grep -rn "from trading\." --include="*.py" . || echo "no residual refs"
```

- [ ] **Step 3: 全文搜索残留的 `import trading` 引用**

```bash
cd ~/trading-system && grep -rn "import trading" --include="*.py" . || echo "no residual refs"
```

- [ ] **Step 4: 全包导入验证**

写一个脚本递归导入所有模块，确保没有 ImportError：

```bash
cd ~/trading-system && python -c "
import config.settings
import qmt.client, qmt.quotes, qmt.calendar, qmt.orders
import utils.logger, utils.telegram, utils.stock_code_utils, utils.decorators
import db.schema, db.repository
import portfolio.portfolio, portfolio.performance
import risk.engine
from risk.rules import stop_loss, take_profit, max_drawdown, concentration, market_env, blacklist
import strategy.signals
from strategy.factors import base, registry
import execution.manual, execution.paper, execution.qmt
print('ALL IMPORTS PASSED')
"
```

- [ ] **Step 5: 运行 main.py test**

```bash
cd ~/trading-system && python main.py test
```

- [ ] **Step 6: 最终目录结构确认**

```bash
cd ~/trading-system && find . -type f -not -path '*/.git/*' -not -path '*/__pycache__/*' -not -name '.DS_Store' | sort
```

- [ ] **Step 7: Commit**

```bash
cd ~/trading-system && git add -A && git commit -m "chore: remove old trading/ directory, finalize Phase 1 structure"
```

---

## Phase 1 完成检查清单

- [ ] `python main.py` 输出帮助信息
- [ ] `python main.py test` 通过配置检查
- [ ] `python main.py portfolio` 正常输出空持仓
- [ ] 零残留 `from trading.` 引用
- [ ] 所有模块可独立 import
- [ ] quant-system 原封未动
- [ ] DB 共用路径有效
- [ ] git 历史完整（每个 task 一个 commit）
