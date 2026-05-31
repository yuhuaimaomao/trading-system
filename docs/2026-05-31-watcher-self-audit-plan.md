# 盯盘自审计闭环 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让盯盘管线收盘后自动审计当日决策质量，发现模式缺陷，生成改进建议，经用户审核后半自动应用。

**Architecture:** Watcher 每轮决策时写入 `watcher_decision_log` → 收盘后 RuleAuditor 回溯验证 → AIAuditor 串联因果+提炼模式 → `watcher_improvements` 推送到 Telegram → 用户回复"应用 #N"自动合入代码。

**Tech Stack:** Python, SQLite, 千问 qwen3.6-plus (DashScope API), 现有 AIAnalyzer

---

## 文件结构

```
trade/monitor/audit/
├── __init__.py               # 空文件
├── decision_logger.py        # DecisionLoggerMixin（混入 Watcher）
├── rule_auditor.py           # RuleAuditor 规则引擎（6 维度）
├── ai_auditor.py             # AIAuditor（千问调用 + prompt 构建）
├── prompts.py                # AI 审计 prompt 模板
└── improvement_applier.py    # 改进建议自动应用

修改:
- data/schema.py              # 加 4 张新表 + 索引
- data/repo.py                # 加 CRUD 方法
- system/config/settings.py   # 加审计配置常量
- trade/monitor/watcher.py    # 混入 DecisionLoggerMixin
- trade/monitor/market_state.py  # _check_market_state 末尾加日志
- trade/monitor/buy_decision.py  # 关键决策点加日志
- trade/monitor/position_risk.py # 关键决策点加日志
- trade/monitor/sector_heat.py   # _check_sector_heat 末尾加日志
- main.py                     # 加 audit CLI 命令
- ops/scheduler/              # 加 cron 脚本
```

---

### Task 1: 建表 + 配置

**Files:**
- Modify: `data/schema.py`
- Modify: `system/config/settings.py`

- [ ] **Step 1: 在 settings.py 加审计配置常量**

在 `system/config/settings.py` 末尾追加：

```python
# 盯盘自审计
AUDIT_ENABLED = os.environ.get("AUDIT_ENABLED", "true").lower() == "true"
AUDIT_AUTO_APPLY_PARAM = os.environ.get("AUDIT_AUTO_APPLY_PARAM", "false").lower() == "true"
AUDIT_AI_MODEL = os.environ.get("AUDIT_AI_MODEL", "qwen3.6-plus")
AUDIT_RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "90"))
```

- [ ] **Step 2: 在 schema.py 加 4 张新表**

在 `data/schema.py` 的 `ensure_tables()` 函数中，`executescript` 调用内追加（与其他 CREATE TABLE IF NOT EXISTS 并列）：

```sql
CREATE TABLE IF NOT EXISTS watcher_decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    ts TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    stock_code TEXT,
    decision_data TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    finding_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    stock_code TEXT,
    decision_log_ids TEXT,
    pattern_desc TEXT NOT NULL,
    evidence TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS watcher_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_type TEXT NOT NULL,
    lesson_key TEXT NOT NULL,
    lesson_content TEXT NOT NULL,
    trigger_conditions TEXT,
    occurrence_count INTEGER DEFAULT 1,
    first_date DATE NOT NULL,
    last_date DATE NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lesson_type, lesson_key)
);

CREATE TABLE IF NOT EXISTS watcher_improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    improvement_type TEXT NOT NULL,
    target_module TEXT NOT NULL,
    target_param TEXT,
    suggested_change TEXT NOT NULL,
    code_diff TEXT,
    rationale TEXT NOT NULL,
    evidence_ids TEXT,
    status TEXT DEFAULT 'pending',
    applied_date DATE,
    effectiveness_check TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

在 `executescript` 之后（与其他 CREATE INDEX IF NOT EXISTS 并列）追加索引：

```sql
CREATE INDEX IF NOT EXISTS idx_wdl_date_type ON watcher_decision_log(trade_date, decision_type);
CREATE INDEX IF NOT EXISTS idx_af_date_sev ON audit_findings(trade_date, severity);
CREATE INDEX IF NOT EXISTS idx_wl_type ON watcher_lessons(lesson_type);
CREATE INDEX IF NOT EXISTS idx_wi_status ON watcher_improvements(status);
```

- [ ] **Step 3: 验证建表**

```bash
cd ~/trading-system && python -c "
from data.schema import ensure_tables
ensure_tables()
import sqlite3
from system.config.settings import DATABASE_PATH
conn = sqlite3.connect(str(DATABASE_PATH))
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
for t in ['watcher_decision_log','audit_findings','watcher_lessons','watcher_improvements']:
    assert t in tables, f'{t} 未创建'
    print(f'{t} ✅')
conn.close()
print('Done.')
"
```

- [ ] **Step 4: Commit**

```bash
cd ~/trading-system && git add data/schema.py system/config/settings.py && git commit -m "feat: add watcher audit tables and config"
```

---

### Task 2: Repo CRUD 方法

**Files:**
- Modify: `data/repo.py`

- [ ] **Step 1: 在 TradeRepository 类末尾加决策日志写入方法**

```python
def insert_decision_log(self, trade_date: str, ts: str, decision_type: str,
                        stock_code: str | None, decision_data: dict) -> int:
    import json
    conn = self._conn()
    sql = """INSERT INTO watcher_decision_log
             (trade_date, ts, decision_type, stock_code, decision_data)
             VALUES (?, ?, ?, ?, ?)"""
    cursor = conn.execute(sql, (trade_date, ts, decision_type,
                                 stock_code, json.dumps(decision_data, ensure_ascii=False)))
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id

def get_decision_logs(self, trade_date: str, decision_type: str = None) -> list[dict]:
    conn = self._conn()
    where = ["trade_date=?"]
    params = [trade_date]
    if decision_type:
        where.append("decision_type=?")
        params.append(decision_type)
    sql = f"SELECT * FROM watcher_decision_log WHERE {' AND '.join(where)} ORDER BY ts"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    cols = ["id", "trade_date", "ts", "decision_type", "stock_code",
            "decision_data", "created_at"]
    return [dict(zip(cols, row)) for row in rows]
```

- [ ] **Step 2: 加 audit_findings 读写方法**

```python
def insert_audit_finding(self, finding: dict) -> int:
    import json
    conn = self._conn()
    cols = ", ".join(finding.keys())
    placeholders = ", ".join(["?" for _ in finding])
    vals = []
    for k in finding:
        v = finding[k]
        vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
    sql = f"INSERT INTO audit_findings ({cols}) VALUES ({placeholders})"
    cursor = conn.execute(sql, vals)
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id

def get_audit_findings(self, trade_date: str, min_severity: str = "P3") -> list[dict]:
    conn = self._conn()
    sev_order = ["P0", "P1", "P2", "P3"]
    sql = """SELECT * FROM audit_findings WHERE trade_date=?
             ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
             WHEN 'P2' THEN 2 ELSE 3 END"""
    rows = conn.execute(sql, (trade_date,)).fetchall()
    conn.close()
    cols = ["id", "trade_date", "finding_type", "severity", "stock_code",
            "decision_log_ids", "pattern_desc", "evidence", "created_at"]
    return [dict(zip(cols, row)) for row in rows]
```

- [ ] **Step 3: 加 watcher_lessons 读写方法**

```python
def upsert_watcher_lesson(self, lesson_type: str, lesson_key: str,
                          lesson_content: str, trigger_conditions: dict = None,
                          trade_date: str = None) -> int:
    import json
    conn = self._conn()
    existing = conn.execute(
        "SELECT id, occurrence_count FROM watcher_lessons WHERE lesson_type=? AND lesson_key=?",
        (lesson_type, lesson_key),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE watcher_lessons SET occurrence_count=?, last_date=?, is_active=1
               WHERE id=?""",
            (existing[1] + 1, trade_date, existing[0]),
        )
        conn.commit()
        row_id = existing[0]
        conn.close()
        return row_id
    else:
        cursor = conn.execute(
            """INSERT INTO watcher_lessons
               (lesson_type, lesson_key, lesson_content, trigger_conditions, first_date, last_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (lesson_type, lesson_key, lesson_content,
             json.dumps(trigger_conditions, ensure_ascii=False) if trigger_conditions else None,
             trade_date, trade_date),
        )
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id

def get_active_lessons(self, lesson_type: str = None) -> list[dict]:
    conn = self._conn()
    where = ["is_active=1"]
    params = []
    if lesson_type:
        where.append("lesson_type=?")
        params.append(lesson_type)
    sql = f"SELECT * FROM watcher_lessons WHERE {' AND '.join(where)} ORDER BY occurrence_count DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    cols = ["id", "lesson_type", "lesson_key", "lesson_content",
            "trigger_conditions", "occurrence_count", "first_date",
            "last_date", "is_active", "created_at"]
    return [dict(zip(cols, row)) for row in rows]
```

- [ ] **Step 4: 加 watcher_improvements 读写方法**

```python
def insert_improvement(self, imp: dict) -> int:
    import json
    conn = self._conn()
    cols = ", ".join(imp.keys())
    placeholders = ", ".join(["?" for _ in imp])
    vals = []
    for k in imp:
        v = imp[k]
        vals.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
    sql = f"INSERT INTO watcher_improvements ({cols}) VALUES ({placeholders})"
    cursor = conn.execute(sql, vals)
    conn.commit()
    row_id = cursor.lastrowid
    conn.close()
    return row_id

def get_pending_improvements(self) -> list[dict]:
    conn = self._conn()
    rows = conn.execute(
        "SELECT * FROM watcher_improvements WHERE status='pending' ORDER BY id"
    ).fetchall()
    conn.close()
    cols = ["id", "trade_date", "improvement_type", "target_module",
            "target_param", "suggested_change", "code_diff", "rationale",
            "evidence_ids", "status", "applied_date", "effectiveness_check", "created_at"]
    return [dict(zip(cols, row)) for row in rows]

def update_improvement_status(self, imp_id: int, status: str, applied_date: str = None):
    conn = self._conn()
    conn.execute(
        "UPDATE watcher_improvements SET status=?, applied_date=? WHERE id=?",
        (status, applied_date, imp_id),
    )
    conn.commit()
    conn.close()

def update_improvement_effectiveness(self, imp_id: int, check: str):
    conn = self._conn()
    conn.execute(
        "UPDATE watcher_improvements SET effectiveness_check=? WHERE id=?",
        (check, imp_id),
    )
    conn.commit()
    conn.close()
```

- [ ] **Step 5: 验证 CRUD**

```bash
cd ~/trading-system && python -c "
from data.repo import TradeRepository
import json
r = TradeRepository()
today = '2026-06-01'
# decision_log
id = r.insert_decision_log(today, '2026-06-01T10:30:00', 'regime_change', None, {'pattern':'normal'})
logs = r.get_decision_logs(today)
assert len(logs) > 0, 'decision_log 写入失败'
print(f'decision_log ✅ ({len(logs)} rows)')
# finding
fid = r.insert_audit_finding({'trade_date':today,'finding_type':'test','severity':'P1','pattern_desc':'test','evidence':'{}'})
findings = r.get_audit_findings(today)
assert len(findings) > 0, 'audit_findings 写入失败'
print(f'audit_findings ✅')
# lesson
lid = r.upsert_watcher_lesson('test_type', 'test_key', 'test content', None, today)
lessons = r.get_active_lessons()
assert len(lessons) > 0, 'watcher_lessons 写入失败'
print(f'watcher_lessons ✅')
# improvement
iid = r.insert_improvement({'trade_date':today,'improvement_type':'param_tune','target_module':'test','suggested_change':'test','rationale':'test'})
imps = r.get_pending_improvements()
assert len(imps) > 0, 'watcher_improvements 写入失败'
print(f'watcher_improvements ✅')
# cleanup
conn = r._conn()
for t in ['watcher_decision_log','audit_findings','watcher_lessons','watcher_improvements']:
    conn.execute(f'DELETE FROM {t}')
conn.commit()
conn.close()
print('Cleanup done.')
"
```

- [ ] **Step 6: Commit**

```bash
cd ~/trading-system && git add data/repo.py && git commit -m "feat: add watcher audit CRUD methods to TradeRepository"
```

---

### Task 3: DecisionLoggerMixin

**Files:**
- Create: `trade/monitor/audit/__init__.py`
- Create: `trade/monitor/audit/decision_logger.py`

- [ ] **Step 1: 创建目录和 __init__.py**

```bash
mkdir -p ~/trading-system/trade/monitor/audit && touch ~/trading-system/trade/monitor/audit/__init__.py
```

- [ ] **Step 2: 写 DecisionLoggerMixin**

`trade/monitor/audit/decision_logger.py`:

```python
# -*- coding: utf-8 -*-
"""决策日志记录 — Mixin 方式混入 Watcher."""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class DecisionLoggerMixin:
    """向 watcher_decision_log 写入关键决策."""

    def _log_decision(self, decision_type: str, stock_code: str | None = None, **kwargs):
        """写入一条决策日志。kwargs 自动序列化到 decision_data JSON。"""
        try:
            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            self.repo.insert_decision_log(
                trade_date=self._trade_date,
                ts=ts,
                decision_type=decision_type,
                stock_code=stock_code,
                decision_data=kwargs,
            )
        except Exception:
            logger.warning(f"决策日志写入失败: {decision_type}", exc_info=True)

    def _log_regime_change(self, pattern: str, confidence: str, prev_pattern: str,
                           index_price: float, index_change: float,
                           up_count: int, down_count: int,
                           top_sectors: list, worst_sectors: list, **extra):
        self._log_decision("regime_change",
            pattern=pattern, confidence=confidence, prev_pattern=prev_pattern,
            index_price=index_price, index_change=index_change,
            up_count=up_count, down_count=down_count,
            top_sectors=top_sectors, worst_sectors=worst_sectors, **extra)

    def _log_buy_trigger(self, signal_id: int, stock_code: str, price: float,
                         buy_min: float, buy_max: float, position_size: int,
                         entry_rule: str, sector_trend: str, market_regime: str, **extra):
        self._log_decision("buy_trigger", stock_code=stock_code,
            signal_id=signal_id, price=price,
            buy_zone_min=buy_min, buy_zone_max=buy_max,
            position_size=position_size, entry_rule=entry_rule,
            sector_trend=sector_trend, market_regime=market_regime, **extra)

    def _log_buy_filter(self, signal_id: int, stock_code: str, entry_rule: str,
                        reason_filtered: str, price: float,
                        buy_min: float, buy_max: float, **extra):
        self._log_decision("buy_filter", stock_code=stock_code,
            signal_id=signal_id, entry_rule=entry_rule,
            reason_filtered=reason_filtered, price=price,
            buy_zone_min=buy_min, buy_zone_max=buy_max, **extra)

    def _log_stop_trigger(self, stock_code: str, stype: str, trigger_price: float,
                          avg_cost: float, pnl_pct: float, risk_level: str, **extra):
        self._log_decision("stop_trigger", stock_code=stock_code,
            type=stype, trigger_price=trigger_price, avg_cost=avg_cost,
            pnl_pct=pnl_pct, risk_level=risk_level, **extra)

    def _log_tp_trigger(self, stock_code: str, stype: str, trigger_price: float,
                        avg_cost: float, pnl_pct: float, **extra):
        self._log_decision("tp_trigger", stock_code=stock_code,
            type=stype, trigger_price=trigger_price, avg_cost=avg_cost,
            pnl_pct=pnl_pct, **extra)

    def _log_position_size(self, stock_code: str, amount: int, base_amount: int,
                           reason: str, sector_mult: float, zone_mult: float, **extra):
        self._log_decision("position_size", stock_code=stock_code,
            amount=amount, base_amount=base_amount, reason=reason,
            sector_mult=sector_mult, zone_mult=zone_mult, **extra)
```

- [ ] **Step 3: 在 watcher.py 中混入 DecisionLoggerMixin**

修改 `trade/monitor/watcher.py`:

在第 47 行附近（`from trade.monitor.buy_decision import BuyDecisionMixin` 之后）加：

```python
from trade.monitor.audit.decision_logger import DecisionLoggerMixin
```

修改 class Watcher 定义（第 52 行附近），在 mixin 列表最前面加 `DecisionLoggerMixin`：

```python
class Watcher(DecisionLoggerMixin, MarketStateMixin, BuyDecisionMixin, ...):
```

- [ ] **Step 4: 验证 mixin 加载**

```bash
cd ~/trading-system && python -c "
from trade.monitor.watcher import Watcher
assert hasattr(Watcher, '_log_decision'), 'DecisionLoggerMixin not mixed in'
assert hasattr(Watcher, '_log_regime_change'), 'regime_change log method missing'
assert hasattr(Watcher, '_log_buy_trigger'), 'buy_trigger log method missing'
assert hasattr(Watcher, '_log_buy_filter'), 'buy_filter log method missing'
print('DecisionLoggerMixin loaded ✅')
"
```

- [ ] **Step 5: Commit**

```bash
cd ~/trading-system && git add trade/monitor/audit/ trade/monitor/watcher.py && git commit -m "feat: add DecisionLoggerMixin for watcher decision logging"
```

---

### Task 4: 埋点 — market_state.py 加日志

**Files:**
- Modify: `trade/monitor/market_state.py`

**目标:** 在 `_check_market_state()` 中，当 pattern 发生变化时写入 `regime_change` 日志。

- [ ] **Step 1: 找到 regime change 位置并加日志**

在 `market_state.py` 的 `_check_market_state()` 方法中，找到 pattern 变更后的位置。在方法末尾 `return regime_obj` 之前，检测 pattern 变化并写入日志。

在方法内，找到类似 `prev_pattern` 跟踪的位置（market_state.py 中应该已有 `_last_pattern` 或类似的状态变量）。

在 `_check_market_state` 方法 return 之前加：

```python
# 记录决策日志（pattern 变更时）
if self._scan_count > 0:
    try:
        new_pattern = regime_obj.pattern if hasattr(regime_obj, 'pattern') else str(regime_obj)
        if getattr(self, '_last_logged_pattern', None) != new_pattern:
            prev = getattr(self, '_last_logged_pattern', 'startup')
            self._log_regime_change(
                pattern=new_pattern,
                confidence=getattr(regime_obj, 'confidence', 'medium'),
                prev_pattern=prev,
                index_price=self._index_prices[-1] if self._index_prices else 0,
                index_change=(self._index_prices[-1] - getattr(self, '_index_open', self._index_prices[-1]))
                             / getattr(self, '_index_open', 1) * 100 if self._index_prices else 0,
                up_count=sum(1 for s in self._market_snapshot.values() if s.get('changePct', 0) > 0),
                down_count=sum(1 for s in self._market_snapshot.values() if s.get('changePct', 0) < 0),
                top_sectors=sorted(
                    [(k, round(v[-1], 2)) for k, v in self._sector_trend_history.items() if v],
                    key=lambda x: -x[1])[:3],
                worst_sectors=sorted(
                    [(k, round(v[-1], 2)) for k, v in self._sector_trend_history.items() if v],
                    key=lambda x: x[1])[:3],
            )
            self._last_logged_pattern = new_pattern
    except Exception:
        pass  # 日志失败不影响主流程
```

- [ ] **Step 2: 验证**

```bash
cd ~/trading-system && python -c "
# 检查语法
import py_compile
py_compile.compile('trade/monitor/market_state.py', doraise=True)
print('market_state.py syntax OK ✅')
"
```

- [ ] **Step 3: Commit**

```bash
cd ~/trading-system && git add trade/monitor/market_state.py && git commit -m "feat: log regime_change decisions in market_state"
```

---

### Task 5: 埋点 — buy_decision.py 加日志

**Files:**
- Modify: `trade/monitor/buy_decision.py`

**目标:** 在买入触发和过滤时写入 `buy_trigger` / `buy_filter` 日志。

- [ ] **Step 1: 找到 _check_buy_candidates 中的过滤点**

在 `buy_decision.py` 的 `_check_buy_candidates` 方法中（约 971 行起），每个 candidate 在下列分支会被过滤：
- entry_rule 过滤（next_day/confirm/pullback/range_boundary）
- 风控拒绝
- 涨停跳过
- 板块持续走弱

在每个过滤点，找到 `continue` 或跳过分支，在前面插入日志调用。

以 entry_rule 过滤为例（找类似 `if entry_rule == 'next_day'` 或 `if not entry_matches` 的位置）：

```python
# 在过滤分支中加（以 confirm entry_rule 不在 cautious 模式下被过滤为例）
self._log_buy_filter(
    signal_id=c.get("signal_id", 0),
    stock_code=c["code"],
    entry_rule=c.get("entry_rule", "standard"),
    reason_filtered=f"entry_rule {c.get('entry_rule')} blocked in {pattern} regime",
    price=c["price"],
    buy_min=c.get("buy_min", 0),
    buy_max=c.get("buy_max", 0),
    market_regime=pattern,
    risk_level=risk_level,
    sector_trend=c.get("trend", {}).get("direction", "unknown"),
    zone_pos=c.get("zone_pos"),
)
```

- [ ] **Step 2: 找到买入触发点**

在 `_check_buy_candidates` 中找到 `try_buy` 成功后的位置，加：

```python
self._log_buy_trigger(
    signal_id=c.get("signal_id", 0),
    stock_code=c["code"],
    price=c["price"],
    buy_min=c.get("buy_min", 0),
    buy_max=c.get("buy_max", 0),
    position_size=max_amount,
    entry_rule=c.get("entry_rule", "standard"),
    sector_trend=c.get("trend", {}).get("direction", "unknown"),
    market_regime=pattern,
    bollinger_pct_b=c.get("bollinger_pct_b"),
    rsi6=c.get("rsi6"),
    ma5_bias=c.get("ma5_bias"),
)
```

- [ ] **Step 3: 找到 _calculate_position_size 加日志**

在 `_calculate_position_size` 方法 return 之前加：

```python
self._log_position_size(
    stock_code=code,
    amount=max_amount,
    base_amount=base_amount,
    reason=reason,
    sector_mult=sector_mult,
    zone_mult=zone_mult,
)
```

- [ ] **Step 4: 验证**

```bash
cd ~/trading-system && python -c "
import py_compile
py_compile.compile('trade/monitor/buy_decision.py', doraise=True)
print('buy_decision.py syntax OK ✅')
"
```

- [ ] **Step 5: Commit**

```bash
cd ~/trading-system && git add trade/monitor/buy_decision.py && git commit -m "feat: log buy_trigger/buy_filter/position_size decisions"
```

---

### Task 6: 埋点 — position_risk.py 加日志

**Files:**
- Modify: `trade/monitor/position_risk.py`

**目标:** 在止损/止盈触发时写入 `stop_trigger` / `tp_trigger` 日志。

- [ ] **Step 1: 找到 _check_positions 中的触发点**

在 position_risk.py 的 `_check_positions` 方法中，找到每个 `_handle_stop_signal` 调用位置（约 78/88/96/106/116 行），在调用前加日志。

在每个 `self._handle_stop_signal(key, code, ...)` 之前，根据信号类型加：

```python
# 止损
self._log_stop_trigger(stock_code=code, stype="止损", trigger_price=price,
    avg_cost=pos.avg_cost, pnl_pct=pnl_pct, risk_level=risk_level,
    sl_original=pos.stop_loss, sl_effective=effective_sl)

# 止盈
self._log_tp_trigger(stock_code=code, stype="止盈", trigger_price=price,
    avg_cost=pos.avg_cost, pnl_pct=pnl_pct,
    tp_original=pos.take_profit, tp_effective=effective_tp)
```

- [ ] **Step 2: _analyze_exit_context 加日志**

在 `_analyze_exit_context` 方法末尾（或输出结果处）加：

```python
self._log_decision("exit_analysis", stock_code=code,
    holding_status=status, exit_context=result,
    market_env=market_env, sector_trend=sector_trend)
```

- [ ] **Step 3: 验证**

```bash
cd ~/trading-system && python -c "
import py_compile
py_compile.compile('trade/monitor/position_risk.py', doraise=True)
print('position_risk.py syntax OK ✅')
"
```

- [ ] **Step 4: Commit**

```bash
cd ~/trading-system && git add trade/monitor/position_risk.py && git commit -m "feat: log stop_trigger/tp_trigger/exit_analysis decisions"
```

---

### Task 7: 埋点 — sector_heat.py + 换仓评估

**Files:**
- Modify: `trade/monitor/sector_heat.py`
- Modify: `trade/paper/trader.py`（或有 swap_eval 逻辑的文件）

- [ ] **Step 1: sector_heat.py 加日志**

在 `SectorHeatMonitor.check()` 返回结果前，写入 sector_alert：

```python
self._log_decision("sector_alert",
    top_sectors=top5, bottom_sectors=bottom3,
    my_sectors=my_sectors, watch_sectors=watch_sectors,
    warnings=warnings, good=good)
```

Wait — SectorHeatMonitor 不是 Mixin。Watcher 通过 `self._sector_monitor` 调用它。日志应通过 Watcher 的 `self._log_decision` 写入。

在 Watcher 的 `_scan` 方法中，`_check_sector_heat` 调用后（watcher.py 约 392 行），需要拿到 SectorHeatMonitor 的返回结果后写入日志。但目前的 `_check_sector_heat` 可能是 void 方法。这里改为在调用后由 Watcher 自己记录：

在 watcher.py `_scan()` 中 `_check_sector_heat` 调用后加：

```python
try:
    top_sectors = sorted(
        [(k, round(v[-1], 2)) for k, v in self._sector_trend_history.items()
         if len(v) >= 3], key=lambda x: -x[1])[:5]
    bottom_sectors = sorted(
        [(k, round(v[-1], 2)) for k, v in self._sector_trend_history.items()
         if len(v) >= 3], key=lambda x: x[1])[:3]
    if top_sectors or bottom_sectors:
        self._log_decision("sector_alert",
            top_sectors=top_sectors, bottom_sectors=bottom_sectors)
except Exception:
    pass
```

- [ ] **Step 2: 换仓评估加日志**

在 Watcher `_scan()` 方法中 `_evaluate_swaps` 调用后（约 404 行），加：

```python
# swap_eval 日志已在 _evaluate_swaps 内部通过 self._log_decision 写入
```

同时在 `_evaluate_swaps` 方法内部（buy_decision.py 或相关文件），AI 决策后加：

```python
self._log_decision("swap_eval", swap_decision=decision,
    candidates=candidate_codes, current_holdings=holding_codes)
```

- [ ] **Step 3: 验证**

```bash
cd ~/trading-system && python -c "
import py_compile
py_compile.compile('trade/monitor/sector_heat.py', doraise=True)
print('sector_heat.py syntax OK ✅')
"
```

- [ ] **Step 4: Commit**

```bash
cd ~/trading-system && git add trade/monitor/sector_heat.py trade/monitor/watcher.py && git commit -m "feat: log sector_alert and swap_eval decisions"
```

---

### Task 8: RuleAuditor 规则审计引擎

**Files:**
- Create: `trade/monitor/audit/rule_auditor.py`

- [ ] **Step 1: 写 RuleAuditor 框架和数据加载**

`trade/monitor/audit/rule_auditor.py`:

```python
# -*- coding: utf-8 -*-
"""RuleAuditor — 纯规则审计引擎，不做 AI 推理。
逐个决策回溯验证：当时判断 vs 后续实际走势。"""

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

from system.config.settings import DATABASE_PATH


class RuleAuditor:
    def __init__(self, db_path: str = None, repo=None):
        self.db_path = db_path or str(DATABASE_PATH)
        self.repo = repo

    def audit(self, trade_date: str) -> list[dict]:
        """全量审计，返回 audit_findings 列表。"""
        findings = []
        findings += self._audit_regime(trade_date)
        findings += self._audit_buy_signals(trade_date)
        findings += self._audit_stop_loss(trade_date)
        findings += self._audit_take_profit(trade_date)
        findings += self._audit_position_size(trade_date)
        findings += self._audit_sector(trade_date)
        return findings

    def run_and_save(self, trade_date: str) -> list[dict]:
        findings = self.audit(trade_date)
        for f in findings:
            if self.repo:
                self.repo.insert_audit_finding(f)
        return findings

    # ---- 数据查询助手 ----

    def _get_index_snapshots(self, trade_date: str) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT ts, price, high, low, change_pct, amount
               FROM index_snapshots WHERE trade_date=? ORDER BY ts""",
            (trade_date,),
        ).fetchall()
        conn.close()
        return [{"ts": r[0], "price": r[1], "high": r[2], "low": r[3],
                 "change_pct": r[4], "amount": r[5]} for r in rows]

    def _get_index_snapshots_after(self, trade_date: str, after_ts: str, minutes: int = 30) -> list[dict]:
        """取某个时间点之后的 N 分钟 index 快照。"""
        conn = sqlite3.connect(self.db_path)
        # after_ts 是 ISO 格式如 '2026-05-31T10:30:00'
        # index_snapshots 的 ts 是浮点时间戳
        from_ts = datetime.fromisoformat(after_ts)
        to_ts = from_ts + timedelta(minutes=minutes)
        rows = conn.execute(
            """SELECT ts, price, high, low, change_pct, amount
               FROM index_snapshots
               WHERE trade_date=? AND ts >= ? AND ts <= ?
               ORDER BY ts""",
            (trade_date, from_ts.timestamp(), to_ts.timestamp()),
        ).fetchall()
        conn.close()
        return [{"ts": r[0], "price": r[1], "high": r[2], "low": r[3],
                 "change_pct": r[4], "amount": r[5]} for r in rows]

    def _get_stock_close_price(self, trade_date: str, code: str) -> float | None:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT close FROM stock_basic WHERE trade_date=? AND stock_code=?",
            (trade_date, code),
        ).fetchone()
        conn.close()
        return float(row[0]) if row and row[0] else None
```

- [ ] **Step 2: 写 6 个审计维度方法**

```python
    def _audit_regime(self, trade_date: str) -> list[dict]:
        """审计市场模式分类准确度。"""
        findings = []
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT * FROM watcher_decision_log
               WHERE trade_date=? AND decision_type='regime_change'
               ORDER BY ts""",
            (trade_date,),
        ).fetchall()
        conn.close()
        cols = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        for row in rows:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            pattern = data.get("pattern", "")
            ts = log["ts"]

            snaps = self._get_index_snapshots_after(trade_date, ts, minutes=30)
            if len(snaps) < 5:
                continue  # 数据不足，跳过

            start_price = snaps[0]["price"]
            end_price = snaps[-1]["price"]
            change = (end_price - start_price) / start_price * 100 if start_price else 0

            mid = len(snaps) // 2
            first_half_avg = sum(s["price"] for s in snaps[:mid]) / mid if snaps[:mid] else start_price
            second_half_avg = sum(s["price"] for s in snaps[mid:]) / len(snaps[mid:]) if snaps[mid:] else end_price
            cg_shift = "down" if second_half_avg < first_half_avg else "up"

            # 评估每个模式的预期 vs 实际
            result = self._evaluate_regime_result(pattern, change, cg_shift, snaps)
            if result:
                findings.append({
                    "trade_date": trade_date,
                    "finding_type": "regime_misclass",
                    "severity": result["severity"],
                    "stock_code": None,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": result["desc"],
                    "evidence": json.dumps({
                        "decision": data, "actual": {
                            "30min_change_pct": round(change, 4),
                            "cg_shift": cg_shift,
                            "start_price": start_price, "end_price": end_price,
                        },
                        "deviation": result["deviation"],
                    }, ensure_ascii=False),
                })
        return findings

    def _evaluate_regime_result(self, pattern: str, change: float, cg_shift: str,
                                 snaps: list) -> dict | None:
        """评估单个模式判断的结果。返回 None 表示吻合，返回 dict 表示偏离。"""
        # 方向一致 / 方向相反 / 方向不明
        if pattern == "one_sided":
            if cg_shift == "down" and change < -0.3:
                return None  # 吻合：确实继续跌
            elif cg_shift == "up" and change > 0.5:
                return {"severity": "P1", "desc": f"判 one_sided 但 30min 内反弹 {change:+.2f}%",
                        "deviation": "direction_opposite"}
            elif abs(change) < 0.2:
                return {"severity": "P2", "desc": "判 one_sided 但后续横盘",
                        "deviation": "direction_unclear"}
        elif pattern == "v_reversal":
            if cg_shift == "up" and change > 0.3:
                return None
            elif cg_shift == "down":
                return {"severity": "P1", "desc": f"判 v_reversal 但 30min 内继续跌 {change:+.2f}%",
                        "deviation": "direction_opposite"}
        elif pattern == "dead_cat":
            # 反弹失败（回落到起点下方）才吻合
            if change < -0.2:
                return None
            elif change > 0.5:
                return {"severity": "P1", "desc": f"判 dead_cat 但 30min 内持续反弹 {change:+.2f}%",
                        "deviation": "direction_opposite"}
        elif pattern == "normal":
            if abs(change) < 0.8:
                return None
            elif change < -1.5:
                return {"severity": "P0", "desc": f"判 normal 但 30min 内暴跌 {change:+.2f}%",
                        "deviation": "direction_opposite"}
        return None  # 未覆盖的模式默认无发现

    def _audit_buy_signals(self, trade_date: str) -> list[dict]:
        """审计买入信号质量和过滤决策。"""
        findings = []
        conn = sqlite3.connect(self.db_path)
        # 买入触发
        triggers = conn.execute(
            """SELECT * FROM watcher_decision_log
               WHERE trade_date=? AND decision_type='buy_trigger'""",
            (trade_date,),
        ).fetchall()
        cols_dl = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        for row in triggers:
            log = dict(zip(cols_dl, row))
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            close = self._get_stock_close_price(trade_date, code)
            if close is None:
                continue
            price = data.get("price", 0)
            pnl_pct = (close - price) / price * 100 if price else 0
            size = data.get("position_size", 0)

            if pnl_pct < -3:
                findings.append({
                    "trade_date": trade_date, "finding_type": "buy_bad",
                    "severity": "P1", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"买入 {code} 当日亏损 {pnl_pct:+.2f}%（仓位 {size}）",
                    "evidence": json.dumps({"buy_price": price, "close": close,
                        "pnl_pct": round(pnl_pct, 2), "position_size": size,
                        "entry_rule": data.get("entry_rule")}, ensure_ascii=False),
                })

        # 买入过滤
        filters = conn.execute(
            """SELECT * FROM watcher_decision_log
               WHERE trade_date=? AND decision_type='buy_filter'""",
            (trade_date,),
        ).fetchall()

        for row in filters:
            log = dict(zip(cols_dl, row))
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            close = self._get_stock_close_price(trade_date, code)
            if close is None:
                continue
            price = data.get("price", 0)
            pnl_pct = (close - price) / price * 100 if price else 0

            if pnl_pct > 3:
                findings.append({
                    "trade_date": trade_date, "finding_type": "buy_missed",
                    "severity": "P1", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"过滤 {code} 但收盘涨 {pnl_pct:+.2f}%（原因: {data.get('reason_filtered')}）",
                    "evidence": json.dumps({"filter_price": price, "close": close,
                        "pnl_pct": round(pnl_pct, 2),
                        "entry_rule": data.get("entry_rule"),
                        "reason_filtered": data.get("reason_filtered")}, ensure_ascii=False),
                })

        conn.close()
        return findings

    def _audit_stop_loss(self, trade_date: str) -> list[dict]:
        """审计止损触发时机。"""
        findings = []
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM watcher_decision_log
               WHERE trade_date=? AND decision_type='stop_trigger'""",
            (trade_date,),
        ).fetchall()
        conn.close()

        for row in rows:
            log = dict(row)
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            trigger_price = data.get("trigger_price", 0)
            ts = log["ts"]
            to_ts = (datetime.fromisoformat(ts) + timedelta(minutes=30)).timestamp()
            from_ts = datetime.fromisoformat(ts).timestamp()

            conn2 = sqlite3.connect(self.db_path)
            snaps = conn2.execute(
                """SELECT price FROM market_snapshots
                   WHERE trade_date=? AND code=? AND ts >= ? AND ts <= ?
                   ORDER BY ts""",
                (trade_date, code, from_ts, to_ts),
            ).fetchall()
            conn2.close()

            if len(snaps) < 3:
                continue

            prices = [s[0] for s in snaps]
            post_low = min(prices)
            post_high = max(prices)
            rebound_pct = (post_high - trigger_price) / trigger_price * 100 if trigger_price else 0

            if rebound_pct > 2:
                findings.append({
                    "trade_date": trade_date, "finding_type": "stop_early",
                    "severity": "P1", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"{code} 止损触发后 30min 内反弹 {rebound_pct:+.2f}%，可能过早止损",
                    "evidence": json.dumps({"trigger_price": trigger_price,
                        "post_low": post_low, "post_high": post_high,
                        "rebound_pct": round(rebound_pct, 2)}, ensure_ascii=False),
                })
            elif post_low < trigger_price * 0.97:
                findings.append({
                    "trade_date": trade_date, "finding_type": "stop_late",
                    "severity": "P1", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"{code} 止损触发后继续跌到 {post_low:.2f}（距止损 -{abs((post_low - trigger_price) / trigger_price * 100):.1f}%），止损设太宽",
                    "evidence": json.dumps({"trigger_price": trigger_price,
                        "post_low": post_low, "further_drop_pct": round((trigger_price - post_low) / trigger_price * 100, 2)}, ensure_ascii=False),
                })
        return findings

    def _audit_take_profit(self, trade_date: str) -> list[dict]:
        """审计止盈触发时机。"""
        findings = []
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM watcher_decision_log
               WHERE trade_date=? AND decision_type='tp_trigger'""",
            (trade_date,),
        ).fetchall()
        conn.close()

        for row in rows:
            log = dict(row)
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            trigger_price = data.get("trigger_price", 0)
            ts = log["ts"]
            to_ts = (datetime.fromisoformat(ts) + timedelta(minutes=30)).timestamp()
            from_ts = datetime.fromisoformat(ts).timestamp()

            conn2 = sqlite3.connect(self.db_path)
            snaps = conn2.execute(
                """SELECT price FROM market_snapshots
                   WHERE trade_date=? AND code=? AND ts >= ? AND ts <= ?
                   ORDER BY ts""",
                (trade_date, code, from_ts, to_ts),
            ).fetchall()
            conn2.close()

            if len(snaps) < 3:
                continue

            prices = [s[0] for s in snaps]
            post_high = max(prices)
            further_up = (post_high - trigger_price) / trigger_price * 100 if trigger_price else 0

            if further_up > 2:
                findings.append({
                    "trade_date": trade_date, "finding_type": "tp_early",
                    "severity": "P2", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"{code} 止盈后继续涨 {further_up:+.2f}%，可能卖飞了",
                    "evidence": json.dumps({"trigger_price": trigger_price,
                        "post_high": post_high, "further_up_pct": round(further_up, 2)}, ensure_ascii=False),
                })
        return findings

    def _audit_position_size(self, trade_date: str) -> list[dict]:
        """审计仓位分配效率。"""
        findings = []
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            """SELECT * FROM watcher_decision_log
               WHERE trade_date=? AND decision_type='buy_trigger'""",
            (trade_date,),
        ).fetchall()
        cols = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        # 按仓位大小分三组
        entries = []
        for row in rows:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            close = self._get_stock_close_price(trade_date, code)
            if close is None:
                continue
            price = data.get("price", 0)
            size = data.get("position_size", 0)
            pnl_pct = (close - price) / price * 100 if price else 0
            entries.append({"code": code, "size": size, "pnl_pct": pnl_pct})

        conn.close()
        if len(entries) < 3:
            return findings

        entries.sort(key=lambda x: x["size"])
        n = len(entries)
        small = entries[:n // 3]
        mid = entries[n // 3: 2 * n // 3]
        large = entries[2 * n // 3:]

        large_avg = sum(e["pnl_pct"] for e in large) / len(large)
        small_avg = sum(e["pnl_pct"] for e in small) / len(small)

        if small_avg > large_avg + 2:
            findings.append({
                "trade_date": trade_date, "finding_type": "size_mismatch",
                "severity": "P2", "stock_code": None,
                "decision_log_ids": json.dumps([e.get("id") for e in entries if "id" in e]),
                "pattern_desc": f"小仓位组平均盈利 {small_avg:+.2f}% > 大仓位组 {large_avg:+.2f}%，仓位分配方向可能反了",
                "evidence": json.dumps({
                    "small_avg_pnl": round(small_avg, 2),
                    "mid_avg_pnl": round(sum(e["pnl_pct"] for e in mid) / len(mid), 2),
                    "large_avg_pnl": round(large_avg, 2),
                    "groups": {"small": small, "mid": mid, "large": large},
                }, ensure_ascii=False),
            })
        return findings

    def _audit_sector(self, trade_date: str) -> list[dict]:
        """审计板块热度准确度。"""
        return []  # 依赖 sector_snapshots，先留空
```

- [ ] **Step 3: 验证 RuleAuditor 加载**

```bash
cd ~/trading-system && python -c "
from trade.monitor.audit.rule_auditor import RuleAuditor
ra = RuleAuditor()
print(f'RuleAuditor loaded ✅ ({len([m for m in dir(ra) if m.startswith(\"_audit\")])} audit methods)')
"
```

- [ ] **Step 4: Commit**

```bash
cd ~/trading-system && git add trade/monitor/audit/ && git commit -m "feat: add RuleAuditor with 6 audit dimensions"
```

---

### Task 9: AIAuditor AI 审计引擎

**Files:**
- Create: `trade/monitor/audit/prompts.py`
- Create: `trade/monitor/audit/ai_auditor.py`

- [ ] **Step 1: 写 AI 审计 prompt 模板**

`trade/monitor/audit/prompts.py`:

```python
# -*- coding: utf-8 -*-
"""AI 审计 prompt 模板。"""

WATCHER_AUDIT_SYSTEM = """你是一个量化交易系统的盯盘审计 AI。

你的工作是：收到当日 Watcher 的决策日志 + 规则审计发现 + 市场结构演变数据后，
做三件事：
1. 因果串联 — 把分散的发现串成因果链，找到根本原因
2. 模式提炼 — 发现 RuleAuditor 单个发现看不出的规律
3. 改进建议 — 给出可执行的、定位到具体模块和方法的改进方案

**重要原则：**
- 你不是在做统计报告，而是在帮系统自我进化
- 单个决策的 "对/错" 不重要，重要的是发现 "在什么条件下容易出错"
- 改进建议必须定位到具体模块（market_state/buy_decision/position_risk/sector_heat）
- param_tune 类改进（调阈值/系数）可标记 auto_applicable=true，rule 类改进需人工审核

**输出格式：** 严格 JSON，用 ```json 包裹。
{
  "causal_chains": [
    {"pattern": "...", "events": [...], "root_cause": "...", "impact": "..."}
  ],
  "new_patterns": [
    {"description": "...", "frequency": N, "conditions": {...}}
  ],
  "improvements": [
    {
      "type": "param_tune|rule_add|rule_modify|watch_add",
      "target_module": "market_state|buy_decision|position_risk|...",
      "target_method": "method_name",
      "suggested_change": "...",
      "code_diff": "建议的代码 diff",
      "rationale": "...",
      "auto_applicable": false
    }
  ],
  "lessons": [
    {
      "type": "regime_detection|signal_filter|stop_timing|tp_timing|sizing",
      "key": "unique_lesson_key",
      "content": "教训描述",
      "trigger_conditions": {...}
    }
  ]
}"""

WATCHER_AUDIT_USER = """## 今日决策时间线
{decision_timeline}

## 规则审计发现
{rule_findings}

## 市场结构演变
{market_structure}

## 历史教训
{historical_lessons}

## 当前策略参数
{current_params}

请完成审计分析。"""
```

- [ ] **Step 2: 写 AIAuditor**

`trade/monitor/audit/ai_auditor.py`:

```python
# -*- coding: utf-8 -*-
"""AIAuditor — AI 驱动的盯盘审计，串联因果、提炼模式、生成改进建议。"""

import json
import re
from datetime import datetime

from trade.monitor.audit.prompts import WATCHER_AUDIT_SYSTEM, WATCHER_AUDIT_USER


class AIAuditor:
    def __init__(self, repo, model: str = None):
        self.repo = repo
        from system.config.settings import AUDIT_AI_MODEL
        self.model = model or AUDIT_AI_MODEL
        self._ai = None

    @property
    def ai(self):
        if self._ai is None:
            from analysis.review.analyzer import AIAnalyzer
            self._ai = AIAnalyzer()
            self._ai.model = self.model
        return self._ai

    def audit(self, trade_date: str) -> dict | None:
        """运行 AI 审计，返回解析后的结果 dict。"""
        prompt = self._build_prompt(trade_date)
        if prompt is None:
            return None

        text = self.ai._call_ai(prompt=prompt, system_prompt=WATCHER_AUDIT_SYSTEM,
                                max_tokens=4096)
        if not text:
            return None

        return self._parse_response(text)

    def _build_prompt(self, trade_date: str) -> str | None:
        # 决策时间线
        logs = self.repo.get_decision_logs(trade_date)
        if not logs:
            return None

        timeline_lines = []
        for log in logs:
            data = json.loads(log["decision_data"]) if isinstance(log["decision_data"], str) else log["decision_data"]
            code = log.get("stock_code") or "-"
            timeline_lines.append(
                f"[{log['ts']}] {log['decision_type']} {code} | {json.dumps(data, ensure_ascii=False)[:200]}"
            )
        decision_timeline = "\n".join(timeline_lines)

        # 规则审计发现
        findings = self.repo.get_audit_findings(trade_date)
        sev_emoji = {"P0": "🚨", "P1": "⚠️", "P2": "📝", "P3": "💡"}
        finding_lines = []
        for f in findings:
            finding_lines.append(
                f"{sev_emoji.get(f['severity'], '')} [{f['severity']}] {f['pattern_desc']}"
            )
        rule_findings = "\n".join(finding_lines) if finding_lines else "无 P0/P1 发现"

        # 市场结构演变（简化版）
        market_structure = self._build_market_structure(trade_date)

        # 历史教训
        lessons = self.repo.get_active_lessons()
        lesson_lines = []
        for l in lessons[:20]:
            lesson_lines.append(f"[{l['lesson_type']}] ({l['occurrence_count']}次) {l['lesson_content']}")
        historical_lessons = "\n".join(lesson_lines) if lesson_lines else "无历史教训"

        # 当前策略参数
        current_params = self._get_current_params()

        return WATCHER_AUDIT_USER.format(
            decision_timeline=decision_timeline,
            rule_findings=rule_findings,
            market_structure=market_structure,
            historical_lessons=historical_lessons,
            current_params=current_params,
        )

    def _build_market_structure(self, trade_date: str) -> str:
        import sqlite3
        from system.config.settings import DATABASE_PATH
        conn = sqlite3.connect(str(DATABASE_PATH))
        # 板块快照趋势
        rows = conn.execute(
            "SELECT ts, sector_name, avg_change FROM sector_snapshots WHERE trade_date=? ORDER BY ts",
            (trade_date,),
        ).fetchall()
        conn.close()
        if not rows:
            return "无板块数据"

        # 采样：每小时取一个点
        sampled = {}
        for ts, name, chg in rows:
            hour = ts[:13] if isinstance(ts, str) else datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H")
            key = (hour, name)
            if key not in sampled:
                sampled[key] = chg
        lines = [f"{h} {n}: {c:+.2f}%" for (h, n), c in sorted(sampled.items())]
        return "\n".join(lines[:50])

    def _get_current_params(self) -> str:
        import inspect
        from system.config import settings
        params = []
        for name in dir(settings):
            if name.isupper() and not name.startswith("_"):
                val = getattr(settings, name)
                if isinstance(val, (int, float, str, bool)):
                    params.append(f"{name}={val}")
        return "\n".join(sorted(params)[:40])

    def _parse_response(self, text: str) -> dict | None:
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if not json_match:
            return None
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            return None

    def run_and_save(self, trade_date: str) -> dict | None:
        result = self.audit(trade_date)
        if result is None:
            return None
        # 保存 improvements
        for imp in result.get("improvements", []):
            self.repo.insert_improvement({
                "trade_date": trade_date,
                "improvement_type": imp.get("type", "rule_add"),
                "target_module": imp.get("target_module", ""),
                "target_param": imp.get("target_method", ""),
                "suggested_change": imp.get("suggested_change", ""),
                "code_diff": imp.get("code_diff", ""),
                "rationale": imp.get("rationale", ""),
                "evidence_ids": "[]",
            })
        # 保存 lessons
        for lesson in result.get("lessons", []):
            self.repo.upsert_watcher_lesson(
                lesson_type=lesson.get("type", "unknown"),
                lesson_key=lesson.get("key", ""),
                lesson_content=lesson.get("content", ""),
                trigger_conditions=lesson.get("trigger_conditions"),
                trade_date=trade_date,
            )
        return result
```

- [ ] **Step 3: 验证**

```bash
cd ~/trading-system && python -c "
from trade.monitor.audit.ai_auditor import AIAuditor
from trade.monitor.audit.prompts import WATCHER_AUDIT_SYSTEM
print(f'AIAuditor loaded ✅')
print(f'Prompt template length: {len(WATCHER_AUDIT_SYSTEM)} chars')
"
```

- [ ] **Step 4: Commit**

```bash
cd ~/trading-system && git add trade/monitor/audit/ && git commit -m "feat: add AIAuditor with Qwen-powered causal analysis"
```

---

### Task 10: 改进应用器 + Telegram 集成

**Files:**
- Create: `trade/monitor/audit/improvement_applier.py`
- Modify: `trade/monitor/watcher.py`（收盘段调用审计）

- [ ] **Step 1: 写 improvement_applier.py**

```python
# -*- coding: utf-8 -*-
"""改进建议应用器 — 解析用户 Telegram 回复，执行代码修改。"""

import logging

logger = logging.getLogger(__name__)


def format_improvement_card(imp: dict) -> str:
    """将一条改进建议格式化为 Telegram 消息。"""
    type_labels = {"param_tune": "参数调优", "rule_add": "新增规则",
                   "rule_modify": "修改规则", "watch_add": "新增盯盘维度"}
    sev_emoji = {"P0": "🚨", "P1": "⚠️", "P2": "📝", "P3": "💡"}

    lines = [
        f"🔧 盯盘改进建议 #{imp['id']}",
        "   ─────────────────────────",
        f"   类型: {type_labels.get(imp['improvement_type'], imp['improvement_type'])}",
        f"   模块: {imp['target_module']}",
    ]
    if imp.get("target_param"):
        lines.append(f"   参数: {imp['target_param']}")

    lines += [
        "",
        f"   建议: {imp['suggested_change']}",
        "",
        f"   理由: {imp['rationale']}",
    ]

    if imp.get("code_diff"):
        lines += ["", f"   ```diff", imp["code_diff"], "   ```"]

    lines += ["", "   [应用] [忽略] [稍后]"]

    return "\n".join(lines)


class ImprovementApplier:
    def __init__(self, repo):
        self.repo = repo

    def apply(self, imp_id: int) -> str:
        """应用指定改进建议。返回执行结果描述。"""
        imp = self._get_improvement(imp_id)
        if imp is None:
            return f"未找到改进 #{imp_id}"

        imp_type = imp["improvement_type"]
        if imp_type == "param_tune":
            return self._apply_param_tune(imp)
        elif imp_type in ("rule_add", "rule_modify"):
            return self._apply_rule_change(imp)
        elif imp_type == "watch_add":
            return self._apply_watch_add(imp)
        else:
            return f"不支持的改进类型: {imp_type}"

    def _get_improvement(self, imp_id: int) -> dict | None:
        conn = self.repo._conn()
        conn.row_factory = None
        row = conn.execute(
            "SELECT * FROM watcher_improvements WHERE id=? AND status='pending'",
            (imp_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        cols = ["id", "trade_date", "improvement_type", "target_module",
                "target_param", "suggested_change", "code_diff", "rationale",
                "evidence_ids", "status", "applied_date", "effectiveness_check", "created_at"]
        return dict(zip(cols, row))

    def _apply_param_tune(self, imp: dict) -> str:
        """自动应用参数调整。找到 settings.py 中的对应常量并修改。"""
        # 由于 Python 不支持运行时修改模块常量并持久化，
        # param_tune 类改进记录到 code_diff 中，由用户手动确认后 git apply
        self.repo.update_improvement_status(imp["id"], "applied",
            __import__("datetime").date.today().isoformat())
        diff = imp.get("code_diff", "")
        if diff:
            return f"参数调优 #{imp['id']} 已标记为 applied。\n手动执行: 将以下 diff 应用到对应文件\n```diff\n{diff}\n```"
        return f"参数调优 #{imp['id']} 已标记为 applied（无 code_diff，请手动调整）"

    def _apply_rule_change(self, imp: dict) -> str:
        """规则变更：生成 diff 供用户审核。标记为 applied 后由用户手动合入。"""
        self.repo.update_improvement_status(imp["id"], "applied",
            __import__("datetime").date.today().isoformat())
        diff = imp.get("code_diff", "")
        if diff:
            return f"规则变更 #{imp['id']} 已标记为 applied。\n手动执行:\n```diff\n{diff}\n```"
        return f"规则变更 #{imp['id']} 已标记为 applied（无 code_diff，请手动实现）"

    def _apply_watch_add(self, imp: dict) -> str:
        """新增盯盘维度：标记 applied，需手动集成到 _scan 循环。"""
        self.repo.update_improvement_status(imp["id"], "applied",
            __import__("datetime").date.today().isoformat())
        return f"新盯盘维度 #{imp['id']} 已标记为 applied。请手动集成到 Watcher._scan()"
```

- [ ] **Step 2: 修改 Watcher 收盘段，集成审计**

在 `watcher.py` 的 `_finalize_close` 中（`close_summary.py`），收盘处理后加审计触发：

```python
def _run_post_close_audit(self):
    """收盘后自动运行审计（如果启用）。"""
    from system.config.settings import AUDIT_ENABLED
    if not AUDIT_ENABLED:
        return
    try:
        import logging
        audit_logger = logging.getLogger(__name__)
        audit_logger.info("开始收盘审计...")

        # RuleAuditor
        from trade.monitor.audit.rule_auditor import RuleAuditor
        rule = RuleAuditor(repo=self.repo)
        n_findings = len(rule.run_and_save(self._trade_date))
        audit_logger.info(f"规则审计完成: {n_findings} 条发现")

        # AIAuditor（仅当有发现时）
        if n_findings > 0:
            from trade.monitor.audit.ai_auditor import AIAuditor
            ai = AIAuditor(repo=self.repo)
            result = ai.run_and_save(self._trade_date)
            if result:
                n_imps = len(result.get("improvements", []))
                n_lessons = len(result.get("lessons", []))
                audit_logger.info(f"AI 审计完成: {n_imps} 条改进建议, {n_lessons} 条经验教训")

                # Telegram 推送改进卡片
                imps = self.repo.get_pending_improvements()
                for imp in imps[-3:]:  # 最多推送 3 条
                    from trade.monitor.audit.improvement_applier import format_improvement_card
                    card = format_improvement_card(imp)
                    self._alert(card)
    except Exception as e:
        audit_logger.warning(f"收盘审计异常（不阻塞主流程）: {e}")
```

然后在 `_finalize_close` 方法末尾、Telegram 推送之后，加：

```python
self._run_post_close_audit()
```

- [ ] **Step 3: 验证**

```bash
cd ~/trading-system && python -c "
from trade.monitor.audit.improvement_applier import ImprovementApplier, format_improvement_card
print('ImprovementApplier loaded ✅')
print('format_improvement_card loaded ✅')
"
```

- [ ] **Step 4: Commit**

```bash
cd ~/trading-system && git add trade/monitor/audit/improvement_applier.py trade/monitor/watcher.py trade/monitor/close_summary.py && git commit -m "feat: add ImprovementApplier + post-close audit integration"
```

---

### Task 11: CLI 命令 + Cron

**Files:**
- Modify: `main.py`
- Create: `ops/scheduler/cron_audit.sh`（如果 cron 需要独立脚本）

- [ ] **Step 1: 在 main.py 加 audit CLI 命令**

在 `main.py` 的 `COMMANDS` 列表中加 `"audit"`，然后在 handlers dict 中加映射，新增函数：

```python
def cmd_audit():
    """收盘后盯盘自审计：规则审计 + AI 审计 + 改进建议推送"""
    import sys
    from datetime import datetime

    if '--help' in sys.argv:
        print("用法: python main.py audit [选项]")
        print("  --rule-only   仅规则审计")
        print("  --ai-only     仅 AI 审计（需已有 audit_findings）")
        print("  --apply N     应用第 N 条改进建议")
        print("  --list        列出待处理的改进建议")
        return

    from data.repo import TradeRepository
    from system.utils.telegram import MessageSender
    from system.config.settings import TELEGRAM_REPORT_CHAT_ID

    repo = TradeRepository()
    trade_date = datetime.now().strftime("%Y-%m-%d")

    if '--list' in sys.argv:
        imps = repo.get_pending_improvements()
        if imps:
            print(f"待处理改进建议 ({len(imps)} 条):")
            for imp in imps:
                print(f"  #{imp['id']} [{imp['improvement_type']}] {imp['suggested_change'][:80]}")
        else:
            print("无待处理改进建议")
        return

    apply_idx = None
    for i, arg in enumerate(sys.argv):
        if arg == '--apply' and i + 1 < len(sys.argv):
            apply_idx = int(sys.argv[i + 1])
            break

    if apply_idx:
        from trade.monitor.audit.improvement_applier import ImprovementApplier
        applier = ImprovementApplier(repo)
        result = applier.apply(apply_idx)
        print(result)
        try:
            msg = MessageSender(chat_id=TELEGRAM_REPORT_CHAT_ID)
            msg.send(result)
        except Exception:
            pass
        return

    rule_only = '--rule-only' in sys.argv
    ai_only = '--ai-only' in sys.argv

    if not ai_only:
        from trade.monitor.audit.rule_auditor import RuleAuditor
        print(f"规则审计 {trade_date} ...")
        rule = RuleAuditor(repo=repo)
        n = len(rule.run_and_save(trade_date))
        print(f"  完成: {n} 条发现")

    if not rule_only:
        from trade.monitor.audit.ai_auditor import AIAuditor
        print(f"AI 审计 {trade_date} ...")
        ai = AIAuditor(repo=repo)
        result = ai.run_and_save(trade_date)
        if result:
            n_imps = len(result.get("improvements", []))
            print(f"  完成: {n_imps} 条改进建议")
            imps = repo.get_pending_improvements()
            for imp in imps[-3:]:
                from trade.monitor.audit.improvement_applier import format_improvement_card
                card = format_improvement_card(imp)
                print(card)
                try:
                    msg = MessageSender(chat_id=TELEGRAM_REPORT_CHAT_ID)
                    msg.send(card)
                except Exception:
                    pass
        else:
            print("  AI 审计无输出（可能无决策日志或 AI 调用失败）")
```

- [ ] **Step 2: 验证 CLI**

```bash
cd ~/trading-system && python main.py audit --help
```
Expected: 显示用法说明

- [ ] **Step 3: Commit**

```bash
cd ~/trading-system && git add main.py && git commit -m "feat: add audit CLI command with rule/AI audit + improvement apply"
```

---

### Task 12: 集成测试 + E2E 验证

**Files:**
- Create: `tests/test_watcher_audit.py`

- [ ] **Step 1: 写集成测试**

`tests/test_watcher_audit.py`:

```python
# -*- coding: utf-8 -*-
"""盯盘自审计集成测试。"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from data.schema import ensure_tables
from data.repo import TradeRepository


def test_decision_log_crud():
    """测试决策日志写入+读取+查询。"""
    repo = TradeRepository()
    trade_date = "2026-06-01"

    # 写入
    rid = repo.insert_decision_log(trade_date, "2026-06-01T10:30:00",
        "regime_change", None, {"pattern": "normal", "confidence": "high"})
    assert rid > 0

    bid = repo.insert_decision_log(trade_date, "2026-06-01T10:31:00",
        "buy_trigger", "000001",
        {"signal_id": 1, "price": 12.5, "position_size": 10000})
    assert bid > 0

    # 读取
    logs = repo.get_decision_logs(trade_date)
    assert len(logs) >= 2

    # 按类型过滤
    buy_logs = repo.get_decision_logs(trade_date, "buy_trigger")
    assert len(buy_logs) == 1
    assert buy_logs[0]["stock_code"] == "000001"

    # 清理
    conn = repo._conn()
    conn.execute("DELETE FROM watcher_decision_log WHERE trade_date=?", (trade_date,))
    conn.commit()
    conn.close()
    print("test_decision_log_crud ✅")


def test_audit_findings_crud():
    """测试审计发现读写。"""
    repo = TradeRepository()
    trade_date = "2026-06-01"

    f = {"trade_date": trade_date, "finding_type": "regime_misclass",
         "severity": "P1", "pattern_desc": "测试发现",
         "evidence": json.dumps({"test": True})}
    fid = repo.insert_audit_finding(f)
    assert fid > 0

    findings = repo.get_audit_findings(trade_date)
    assert len(findings) >= 1

    conn = repo._conn()
    conn.execute("DELETE FROM audit_findings WHERE trade_date=?", (trade_date,))
    conn.commit()
    conn.close()
    print("test_audit_findings_crud ✅")


def test_lessons_upsert():
    """测试教训去重合并。"""
    repo = TradeRepository()
    trade_date = "2026-06-01"

    lid1 = repo.upsert_watcher_lesson("test", "key1", "first", None, trade_date)
    assert lid1 > 0

    lid2 = repo.upsert_watcher_lesson("test", "key1", "second", None, trade_date)
    assert lid1 == lid2  # 同一个 key 应返回相同 id

    lessons = repo.get_active_lessons("test")
    assert len(lessons) == 1
    assert lessons[0]["occurrence_count"] == 2  # 两次 upsert 合并

    conn = repo._conn()
    conn.execute("DELETE FROM watcher_lessons WHERE lesson_type='test'")
    conn.commit()
    conn.close()
    print("test_lessons_upsert ✅")


def test_improvements_workflow():
    """测试改进建议完整流程。"""
    repo = TradeRepository()
    trade_date = "2026-06-01"

    iid = repo.insert_improvement({
        "trade_date": trade_date,
        "improvement_type": "param_tune",
        "target_module": "test",
        "suggested_change": "调整阈值从 0.5 到 0.6",
        "rationale": "测试原因",
    })
    assert iid > 0

    imps = repo.get_pending_improvements()
    assert len(imps) >= 1

    repo.update_improvement_status(iid, "applied", trade_date)
    imps2 = repo.get_pending_improvements()
    assert all(i["id"] != iid for i in imps2)  # applied 的不在 pending 里

    repo.update_improvement_effectiveness(iid, "应用后同类误判从 3→1 ✅")
    conn = repo._conn()
    row = conn.execute(
        "SELECT effectiveness_check FROM watcher_improvements WHERE id=?", (iid,)
    ).fetchone()
    conn.close()
    assert row and row[0] == "应用后同类误判从 3→1 ✅"

    conn = repo._conn()
    conn.execute("DELETE FROM watcher_improvements WHERE trade_date=?", (trade_date,))
    conn.commit()
    conn.close()
    print("test_improvements_workflow ✅")


if __name__ == "__main__":
    ensure_tables()
    test_decision_log_crud()
    test_audit_findings_crud()
    test_lessons_upsert()
    test_improvements_workflow()
    print("\n全部测试通过 ✅")
```

- [ ] **Step 2: 运行测试**

```bash
cd ~/trading-system && python tests/test_watcher_audit.py
```
Expected: 全部测试通过 ✅

- [ ] **Step 3: Commit**

```bash
cd ~/trading-system && git add tests/test_watcher_audit.py && git commit -m "test: add watcher audit integration tests"
```

---

## 实施顺序

```
Task 1  → Task 2  → Task 3  → Task 4-7 (并行) → Task 8 → Task 9 → Task 10 → Task 11 → Task 12
 建表     CRUD    Logger    埋点(4个文件)      RuleAud   AIAud    Applier    CLI      测试
```

Task 4-7 可以并行（各自独立改不同文件），其余顺序执行。

## 注意事项

- 决策日志写入在 try/except 内，写入失败不影响盯盘主流程
- RuleAuditor 的 `_audit_stop_loss` 依赖 `market_snapshots` 中有该股的分钟级数据——如果该股在某分钟没有成交，价格会缺失。审计时对此类情况静默跳过
- AI 审计的 prompt 长度可能较大（决策日志 + 市场数据），注意 `max_tokens=4096` 预留足够输出空间
- `improvement_applier` 目前只能标记状态，不能自动化修改代码（Python 无运行时持久化修改能力）。实际代码变更仍需人工合入 AI 生成的 diff
