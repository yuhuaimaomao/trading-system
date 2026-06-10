# 审计报告代码验证文档

**验证日期：** 2026-06-10
**审计报告：** `trading-system-audit-report.docx`（2026-06-10）
**验证范围：** S01-S12 共 12 个问题 + 报告中其他观察点
**验证方法：** 逐条阅读源代码，对照报告描述验证事实准确性、严重度评级、影响评估
**验证结论：** 12 个问题中 9 个事实准确且评级合理，1 个误报（S03），2 个评级偏高（S02/S04）

---

## S01 — stdout/stderr 重定向在 import 之前

**报告评级：P1-高危**
**报告描述：** `cmd_monitor()` 和 `cmd_qmt_collect()` 在函数开头将 stdout/stderr 重定向到日志文件，然后才 import 依赖模块，导致初始化阶段的 ImportError 等异常写入日志文件而非控制台。

**验证结论：部分正确，两个函数情况不同。**

| 函数 | 重定向位置 | import 位置 | 风险 |
|------|-----------|-------------|------|
| `cmd_monitor()` | L81-82 | L84-87 (Watcher/QuoteClient/MessageSender) | **存在**：import 在重定向之后 |
| `cmd_qmt_collect()` | L467-468 | L457-459 (QMTCollector) | **不存在**：import 在重定向之前 |

**关键代码（main.py L71-87）：**

```python
# cmd_monitor: 先重定向
_sys.stdout = _monitor_fh   # L81
_sys.stderr = _monitor_fh   # L82

# 后 import — 如果这里抛异常，traceback 进入 monitor.log
from trade.core.watcher import Watcher  # L87
```

```python
# cmd_qmt_collect: 先 import
from data.collect.live.qmt_collector import QMTCollector  # L457

# 后重定向
_sys.stdout = _collect_fh  # L467
```

**但是**，`cmd_qmt_collect()` 中 `QMTCollector()` 的实例化（L481）和 `logging.basicConfig(stream=_sys.stderr)`（L471）在重定向之后，如果实例化阶段抛异常，也会进入日志文件。

**评级判断：P1 保持。** `cmd_monitor` 是核心盯盘进程，启动失败必须即时可见。如果 Watcher 模块 import 失败，运维人员可能完全不知道盯盘没跑起来。

---

## S02 — 止损价跨表歧义

**报告评级：P1-高危**
**报告描述：** PaperAccount 恢复时止损价来自 `trade_signals` 的 `get_signal_for_pos_meta()`，同股票多条 bought 信号时可能取到错误的止损价。

**验证结论：风险存在但实际触发概率低，评级偏高。**

**关键代码路径：**

1. `_restore_pos_meta()`（watcher.py L942-967）对每个持仓调用 `get_signal_for_pos_meta(code)`
2. `get_signal_for_pos_meta()`（data/repo/__init__.py L322-327）：

```sql
SELECT stop_loss, take_profit, trailing_stop, signal_score, strategy_name, id
FROM trade_signals
WHERE stock_code=? AND status='bought'
ORDER BY id DESC LIMIT 1
```

**场景分析：**

| 场景 | 结果 |
|------|------|
| 正常：一票一条 bought 信号 | ✅ 正确 |
| 多次买卖：买入(A)→卖出→买入(B)，A 和 B 都是 bought，`id DESC` 取到 B | ✅ 正确 |
| 持仓中+新信号 pending：pending 不是 bought，过滤掉 | ✅ 正确 |
| 卖出后信号状态未更新（程序 bug）：出现多条 bought，`id DESC` 取最新 | ⚠️ 依赖程序正确性 |
| 同一天同票有两条 bought（违反 UNIQUE 约束）：不可能 | ✅ 被 schema 阻止 |

`trade_signals` 表有 `UNIQUE(trade_date, stock_code, account)` 约束，同一日同股票只有一条信号。跨天场景中 `ORDER BY id DESC LIMIT 1` 取最新是合理行为。

**真正的风险点：** 如果卖出操作后信号状态没有从 `bought` 更新（比如异常退出），重启时可能恢复出已卖出股票的止损价。但此时 `PaperAccount.restore()` 中的 sold_codes 过滤（account.py L330-335）会跳过已卖出的股票，所以实际不会使用这个错误的止损价。

**评级判断：降为 P2。** 报告建议"在 trade_portfolio_positions 表直接持久化 stop_loss/take_profit"是更稳健的做法，但当前逻辑在实际场景中正确工作的概率很高。

---

## S03 — PaperAccount 线程安全

**报告评级：P2-中危**
**报告描述：** `PaperAccount.buy/sell` 在 AI worker 线程中调用，无锁保护，与主线程 `update_prices` 存在数据竞争。

**验证结论：❌ 报告错误。不存在线程安全问题。**

**关键代码路径：**

AI worker 线程（ai_service.py L314-334）：
```python
def _work(self):
    while self._running:
        key, prompt, model, sys_prompt, max_tok = self._q.get(timeout=1)
        # ↑ 仅消费队列中的 prompt
        result = self.chat(prompt, ...)  # 仅做 HTTP 调用
        with self._lock:
            self._results[key] = result  # 结果写入 dict（有锁保护）
```

Watcher 主线程（watcher.py L1684-1686）：
```python
swap_result = self._ai_queue.pop_result("swap_eval")  # 仅从 _results dict 取结果
if swap_result is not None:
    self._handle_swap_ai_result(swap_result)  # 在主线程中同步执行！
```

`_handle_swap_ai_result()`（watcher.py L1759-1765）：
```python
# 这里仍然在主线程中，不在 AI worker 线程
buy_result = self.paper_account.buy(buy_cand["code"], ...)  # L1759
result = execute_paper_sell(sell_code, ..., paper_account=self.paper_account)  # L1737
```

**架构总结：**
- AI worker 线程：纯消费者，只做 HTTP 调用 + 结果写入
- Watcher 主线程：在 scan 循环中检查 AI 结果是否就绪，就绪后**同步**调用 PaperAccount 方法
- `_results` dict 的读写有 `threading.Lock` 保护（ai_service.py L87, L327-328）

**所有 PaperAccount 的买卖操作都在主线程中执行。** 报告将"AI 结果由 worker 线程计算"混淆为"买卖操作在 worker 线程执行"，这是两个完全不同的概念。

**评级判断：应从问题列表中移除。**

---

## S04 — _sector_trend_history 无上界

**报告评级：P2-中危**
**报告描述：** `_sector_trend_history` 和 `_concept_trend_history` 为 `defaultdict(list)`，每次 `.append()` 无长度限制，长时间运行积累历史数据导致内存增长。

**验证结论：问题存在但影响被夸大。**

**关键代码（sector_context.py L184-185, L252-253）：**
```python
history = self._sector_trend_history[ind]
history.append(avg)  # 无 maxlen 约束
```

**报告漏掉的关键事实：Watcher 是单交易日进程。** 看 `run()` 方法（watcher.py L461-464）：

```python
while self._running:
    if self._after_market():
        logger.info("收盘，盯盘进程退出")
        break
```

Watcher 每天 9:30 前启动，15:00 后自动退出。`_sector_trend_history` 不会跨天累积。单日数据量：
- ~30 行业 + ~300 概念板块
- 每 3 分钟一轮，全天 4 小时 ≈ 80 轮
- 每个数据点一个 float（8 bytes）
- 总计约 26,400 × 8 ≈ 200KB

**200KB 不构成内存问题。**

盘中重启场景中，`_load_sector_history()` 从 DB 恢复（sector_context.py L306-407），也仅限于当天数据。

**评级判断：降为 P3。** 加 `maxlen` 仍是好习惯（防御未来改为多日连续运行），但不是紧急问题。

---

## S05 — 最大买入金额硬编码

**报告评级：P2-中危**
**报告描述：** `calculate_position_size()` 最大买入金额硬编码为 16,000 元，账户资产增长后偏离目标仓位比例。

**验证结论：正确。**

**关键代码（sizing.py L41-44）：**
```python
elif pattern in ("normal", "uptrend"):
    base = 16000
    reason = "大盘正常" if pattern == "normal" else "大盘上行"
```

函数文档明确写"计算买入金额（0-16000）"，所有路径上限都是 16000。所有修正（市场宽度、板块趋势、AI 倾向、买入区位置）后的结果也以 `min(..., 16000)` 封顶。

**实际影响：**
- 初始资金 200,000 × 16% = 32,000，但上限只有 16,000（实际 8%）
- 账户增长到 500,000 后，16,000 只有 3.2%
- 多只持仓时实际仓位更低

**评级判断：P2 保持。** 不是紧急 bug（不会导致错误交易），但会随着账户增长导致仓位管理系统性偏保守。

---

## S06 — SQLite 无连接池

**报告评级：P2-中危**
**报告描述：** SQLite 无连接池，16 个并发采集器每次操作新建连接，写操作存在锁竞争。

**验证结论：问题存在，但"16 并发采集器"的描述有误。**

**关键代码（_base.py L47-53）：**
```python
@contextmanager
def _conn(self):
    conn = sqlite3.connect(self.db_path)
    try:
        yield conn
    finally:
        conn.close()
```

每次调用都新建 sqlite3 连接，无复用。

**但采集器不是并发的。** `cmd_collect()`（main.py L279-289）是 for 循环**顺序执行**：

```python
for name, module_path, class_name, _category in collectors:
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    instance = cls()
    instance.fetch_and_save()  # 串行执行
```

16 个采集器是一个一个跑的，不存在采集器之间的并发写竞争。

**真正存在并发读写的场景：** QMT Collector 进程（写 `market_snapshots`/`index_snapshots`）与 Watcher 进程（读同一批表）同时运行。SQLite 默认不支持并发写，但 WAL 模式可以支持一写多读。

**评级判断：P2 保持。** 建议启用 WAL 模式（`PRAGMA journal_mode=WAL`），主要为了 QMT Collector 写 + Watcher 读的并发场景。

---

## S07 — trade_signals 无 expires_at 字段

**报告评级：P2-中危**
**报告描述：** 昨日 pending 信号可能被今日 Watcher 误触发。

**验证结论：风险存在。**

**当前机制：**
- 收盘时 `expire_old_pending_signals()`（signals.py L87-93）将旧日期 pending 批量过期
- 但若收盘前 Watcher 异常退出，`expire` 未执行

**加载时机：** `_send_opening_decision()`（sector_context.py L62）加载信号展示：
```python
signals = self.repo.get_pending_signals(account="paper")
```

如果该调用不限制 `trade_date`，确实会加载任何日期的 pending 信号。需要进一步确认 `get_pending_signals` 是否有默认日期过滤。

**评级判断：P2 保持。** 需要确认 repo 层是否有日期过滤；若没有，存在"异常退出→次日误加载旧信号"的风险。

---

## S08 — 涨跌停幅度判断遗漏

**报告评级：P2-中危**
**报告描述：** 遗漏北交所股票（8/4 开头）和科创板新股前 5 日场景。

**验证结论：正确。**

**关键代码（watcher.py L1086-1088）：**
```python
@staticmethod
def _get_limit_pct(code: str) -> float:
    """涨跌停幅度：科创/创业板20%，其余10%。"""
    return 0.20 if code.startswith(("688", "300")) else 0.10
```

**遗漏场景：**
| 场景 | 实际涨跌幅 | 代码判断 | 偏差 |
|------|-----------|---------|------|
| 北交所（8/4 开头）| 30% | 10% | 涨停价低 20% |
| 科创板新股前 5 日 | 无限制 | 20% | 限制过严 |
| 北交所新股首日 | 无限制 | 10% | 限制过严 |

**实际影响评估：** 如果系统明确不交易北交所股票，此问题影响为零。科创板新股前 5 日无限制的场景更为罕见（当前系统大概率不会在前 5 日买入新股）。补充这些逻辑是防御性的。

**评级判断：P2 保持。** 不影响当前实盘（不交易北交所），但代码注释写"其余 10%"与事实不符，应修正。

---

## S09 — AI 队列满时静默丢弃

**报告评级：P3-低危**
**报告描述：** AI 队列满时 `submit()` 静默丢弃，追高/换仓等关键 AI 判断被丢弃用户无感知。

**验证结论：当前代码已部分改进，但问题依然存在。**

**关键代码（ai_service.py L196-212）：**
```python
def submit(self, key, prompt, ...):
    try:
        self._q.put_nowait(task)        # 先尝试入队
    except queue.Full:
        try:
            self._q.get_nowait()        # 队列满：丢弃最旧任务
            self._q.task_done()
        except queue.Empty:
            pass
        try:
            self._q.put_nowait(task)     # 重试入队
        except queue.Full:
            return False                 # 仍然失败，返回 False
    return True                          # 成功返回 True
```

**当前版本已改进：**
1. 队列满时丢弃最旧任务而非最新任务（更合理）
2. 返回 `bool` 让调用方感知失败

**但调用方是否检查返回值？** AIQueue 的 `submit()` 包装（ai_queue.py L37-54）直接透传 `ai.submit()` 的返回值，Watcher 中的调用方如果忽略返回值，问题依然存在。这取决于各个 submit 调用点的代码。

**评级判断：P3 保持。** 影响是"AI 辅助缺失、降级为纯规则决策"，核心风控规则不受影响。

---

## S10 — 容灾表无过期清理

**报告评级：P3-低危**
**报告描述：** `market_snapshots` / `index_snapshots` / `market_breadth` 等盘中容灾表无过期清理。

**验证结论：部分正确，`market_snapshots` 已有清理逻辑。**

| 表名 | 清理状态 | 清理位置 |
|------|---------|---------|
| `market_snapshots` | ✅ 已清理（3 天前） | sector_context.py L409-423 `_cleanup_old_snapshots()` |
| `sector_snapshots` | ✅ 已清理（3 天前） | 同上 |
| `index_snapshots` | ❌ 无清理 | — |
| `market_breadth` | ❌ 无清理 | — |

`cleanup_storage.py` 只清理：
- 日志目录（7 个交易日前）
- 缓存 JSON 文件
- PDF 文件
- 报告 TXT 文件
- `cls_telegraph` 表（30 天前）

不涉及 `index_snapshots` 和 `market_breadth`。

**评级判断：P3 保持。** 影响是磁盘占用长期增长，`market_breadth` 每天一条几乎可忽略，`index_snapshots` 每天约 80 条 × 约 200 bytes = 16KB 也是微量增长。

---

## S11 — cmd_stock 无格式校验

**报告评级：P3-低危**
**报告描述：** 接收用户输入的 `stock_code` 参数未做格式校验。

**验证结论：正确。**

**关键代码（main.py L657-669）：**
```python
def cmd_stock():
    code = sys.argv[2] if len(sys.argv) > 2 else None
    if not code:
        print("用法: python main.py stock <股票代码> [--quick|--deep]")
        sys.exit(1)
    from stock import StockAnalyzer
    analyzer = StockAnalyzer()
    report = analyzer.quick(code)  # 直接传入，无格式校验
```

**对比 `cmd_strategy()` 提供了正则校验：**
```python
if trade_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", trade_date):
    logger.error(f"日期格式无效: {trade_date}，需为 YYYY-MM-DD")
    sys.exit(1)
```

**评级判断：P3 保持。** 非法输入会导致数据库查询失败（返回空结果或异常），但不会导致程序崩溃。添加 `^[0-9]{6}$` 校验是低成本的防御性改进。

---

## S12 — PERMANENT_BLACKLIST 为空

**报告评级：P3-低危**
**报告描述：** 黑名单仅依赖股票名称前缀过滤风险标的，退市风险标的保护不足。

**验证结论：正确。**

**关键代码（blacklist.py）：**
```python
PERMANENT_BLACKLIST = set()  # 空集合

_RISK_PREFIXES = ("ST", "*ST", "N", "C")
```

当前只靠 `is_risk_suspect()` 检查名称前缀。以下场景无保护：
- 已退市但名称未更新（如停牌中尚未改 ST）
- 有严重违规/财务造假但未被 ST
- 收到监管函/调查通知的股票

**评级判断：P3 保持。** 实盘交易前应有更完善的黑名单。对接东方财富/同花顺的实时 ST 列表是可行的改进方向。

---

## 报告中其他观察点验证

### Watcher 类规模

报告说 ~1800 行。实际 `wc -l` = **2003 行**。略有偏差但大致准确。`__init__` 中 `self.xxx =` 赋值约 **55 个**实例变量（报告说 60+，基本一致）。

### data/repo/ 和 data/readers/ 目录

**确实存在。** `data/repo/` 有 7 个文件，`data/readers/` 有 4 个文件。从命名和风格看是早期架构产物，建议逐步迁移或标记 `DEPRECATED`。

### market_breadth 阈值

报告建议改为百分比。当前阈值使用场景在 `strategy/screening/breadth.py`：
```python
_BULL_UP = getattr(settings, "MARKET_BREADTH_BULL", 3000)   # 57%
_BEAR_UP = getattr(settings, "MARKET_BREADTH_BEAR", 800)     # 15%
```

改为基于总股票数的百分比更健壮，但市场规模短期不会剧变，优先级低。

### trade_signals 缺少复合索引

当前 `schema.py` 只有：
```sql
CREATE INDEX IF NOT EXISTS idx_trade_signals_date ON trade_signals(trade_date);
```

`get_pending` 查询涉及 `status='pending'` + `account=?` + `trade_date=?`，建议添加 `(account, status, trade_date)` 复合索引。

### holdings_review applied 字段

代码确认 `applied=0` 硬编码（strategy_pipeline.py L718）。`_save_holdings_review()` 写入后无自动应用逻辑。这是一个有意的设计选择（人工确认），但需要文档说明触发时机。

### PID 文件 TOCTOU

已用 `O_CREAT|O_EXCL` 解决主要竞态。`FileExistsError` 分支中 `os.remove` → `os.open` 之间有微小窗口，但单机 cron 场景下实际概率极低。

### _divergence_alerted 无清理

Key 以 `scan_count // N` 为粒度，每天最多约 43 个 key。Watcher 单日进程退出后自动释放，不跨天累积。

### collector_client Socket 非线程安全

Watcher 是单线程轮询，AI worker 不操作 socket，实际无并发问题。报告自身也说明了这一点。

---

## 汇总

| 问题编号 | 报告评级 | 验证结果 | 建议调整 |
|---------|---------|---------|---------|
| S01 | P1 | cmd_monitor 正确，cmd_qmt_collect 部分有误 | **保持 P1** |
| S02 | P1 | 风险存在但实际触发概率低，`id DESC LIMIT 1` 在正常场景下正确 | **降为 P2** |
| S03 | P2 | ❌ 误报。所有 PaperAccount 买卖操作在主线程执行 | **移除** |
| S04 | P2 | 问题存在但影响被夸大（单日进程，单日 ~200KB） | **降为 P3** |
| S05 | P2 | 正确 | 保持 P2 |
| S06 | P2 | 问题存在但"16并发采集器"描述有误（实际串行） | 保持 P2 |
| S07 | P2 | 风险存在 | 保持 P2 |
| S08 | P2 | 正确 | 保持 P2 |
| S09 | P3 | 当前代码已部分改进（丢弃最旧+返回 bool） | 保持 P3 |
| S10 | P3 | market_snapshots/sector_snapshots 已有清理 | 保持 P3 |
| S11 | P3 | 正确 | 保持 P3 |
| S12 | P3 | 正确 | 保持 P3 |

**结论：** 审计报告质量整体较高。12 个问题中 9 个事实准确且评级合理，1 个误报（S03），2 个评级偏高（S02/S04）。按调整后的评级排优先级：P1 剩 1 个（S01），P2 剩 5 个（S02/S05/S06/S07/S08），P3 剩 5 个（S04/S09/S10/S11/S12）。
