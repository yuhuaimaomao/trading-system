# E2E 全流程测试计划

> 状态: 规划中 | 最后更新: 2026-05-30

---

## 1. 测试目标

用生产数据库副本，模拟连续两天完整交易（9:25-15:00，每天约 240 轮扫描），加速执行。对每一步的**所有内部变量**做快照，与预先计算的已知答案逐个对比。100% 准确率要求。

---

## 2. 测试环境

### 2.1 目录结构

```
tests/e2e/
├── TEST_PLAN.md                    # 本文件
├── __init__.py
├── conftest.py                     # Pytest fixtures: db copy, clock, qmt, telegram
├── sim_clock.py                    # 可控时钟：替换 datetime.now() + time.sleep()
├── sim_qmt.py                      # 模拟 QMT：按预设价格序列返回行情
├── sim_telegram.py                 # 模拟 Telegram：捕获所有消息 + 模拟用户回复
├── tracer.py                       # 变量追踪器：记录 _scan() 每步前后的变量快照
├── assertions.py                   # 断言引擎：逐项比对实际值 vs 预期值
├── db_setup.py                     # 数据库准备：复制生产DB + 记录初始状态
├── db_cleanup.py                   # 清理：罗列测试产生的数据 + 删除测试DB
├── run_e2e.py                      # 主入口：python run_e2e.py
├── scenarios/
│   ├── __init__.py
│   ├── base.py                     # 场景基类
│   ├── day1.py                     # Day1 场景：全部输入数据 + 全部预期输出
│   └── day2.py                     # Day2 场景：全部输入数据 + 全部预期输出
├── expected/
│   ├── day1_scan_000.json          # 每轮扫描的预期变量值
│   ├── day1_scan_001.json
│   ├── ...
│   └── day2_scan_239.json
└── reports/
    └── (运行时生成) run_YYYYMMDD_HHMMSS.log
```

### 2.2 DB 副本

- 源: `storage/stock_market.db` (~231MB)
- 目标: `tests/e2e/test_db/stock_market.db`
- 使用 `shutil.copy2` 完整复制
- 所有读写操作仅针对副本
- 测试结束后不自动删除（保留用于问题排查），提供 `db_cleanup.py` 手动清理

### 2.3 加速机制

- `time.sleep()` → 替换为空操作
- `datetime.now()` → 返回模拟时钟时间
- `scan_interval = 60` → 运行时设为 0.001（不影响计算，只影响 sleep）
- 每轮扫描仍然是真实 Python 执行，只是不等 60 秒

---

## 3. 模拟时钟 (SimClock)

### 3.1 接口

```python
class SimClock:
    def __init__(self, start: datetime)
    def now(self) -> datetime          # 替换 datetime.now()
    def time(self) -> dt_time           # 替换 datetime.now().time()
    def advance(self, minutes: int)     # 前进 N 分钟
    def sleep(self, seconds: float)     # 替换 time.sleep()，空操作
    def set(self, dt: datetime)         # 直接设置时间
```

### 3.2 替换方式

使用 `unittest.mock.patch` 全局替换：
```python
patch('datetime.datetime', SimClock.datetime_proxy)
patch('time.sleep', SimClock.sleep)
```

### 3.3 时间线

```
Day1:
  09:24:00  Watcher 启动（cron 拉起）
  09:25:00  第 1 轮扫描（集合竞价后）
  09:26:00  第 2 轮
  ...
  11:30:00  上午收盘
  13:00:00  下午开盘
  ...
  15:00:00  收盘，_finalize_close()

Day2:
  09:24:00  重新启动 Watcher（模拟 cron）
  ...
  15:00:00  收盘
```

---

## 4. 模拟 QMT (SimQMT)

### 4.1 数据生成策略

不使用随机数据。对每只股票，基于其真实昨收价（从 `stock_basic` 读取），按预设轨迹生成价格序列。

价格轨迹类型：
- `flat` — 横盘，±0.5% 波动
- `slow_rise` — 缓慢上涨，每分钟 +0.02%
- `slow_fall` — 缓慢下跌，每分钟 -0.02%
- `sharp_fall` — 急跌，每分钟 -0.1%
- `sharp_rise` — 急拉，每分钟 +0.1%
- `v_shape` — V 型：跌→涨
- `inverted_v` — 倒 V：涨→跌
- `gap_down` — 跳空低开 -2%，然后缓慢回升
- `gap_up` — 跳空高开 +2%，然后持续回落
- `limit_up` — 涨停封板
- `limit_down` — 跌停封板
- `w_shape` — W 型双底
- `m_shape` — M 型双顶
- `fishing` — 钓鱼线（全天慢涨 → 尾盘急跌）
- `late_rally_path` — 前 80% 无波动，尾盘急拉
- `late_dump_path` — 前 80% 横盘，尾盘急跌

### 4.2 接口

```python
class SimQMT:
    def define_stock(self, code: str, base_price: float, trajectory: str,
                     amplitude: float = 0.02, sector: str = "")
    def get_realtime(self, codes: list[str]) -> dict   # 标准 QMT 接口
    def get_quote_detail(self, code: str) -> dict
    def get_minute_kline(self, code: str, count: int) -> list
    def get_kline(self, code: str, period: str, count: int) -> list
    def get_ticks(self, code: str) -> list
    def get_instrument(self, code: str) -> dict
```

### 4.3 价格计算

每只股票在第 N 轮扫描时的价格 = f(trajectory, base_price, scan_number, amplitude)。

所有价格**预先计算并存储为列表**，`get_realtime()` 直接按索引返回，保证确定性。

涨跌停价 = base_price × (1 ± limit_pct)，其中 limit_pct = 0.20 (688/300) 或 0.10 (其他)。

五档盘口：基于当前价格 ±1% 均匀分布 5 档，买卖量随机但比例受 trajectory 影响（涨势中买盘略大，跌势中卖盘略大）。

---

## 5. 变量追踪器 (Tracer)

### 5.1 追踪范围

对 `_scan()` 中的**每一步**，在步骤执行前后记录以下类别的变量：

#### A. 大盘状态 (market_state.py)
```
_index_prices[-1], _index_prices 长度, _index_high, _index_low,
_market_turnovers[-1], _market_turnovers 长度,
_regime.pattern, _regime.risk_level, _regime.allow_buy,
_regime.position_mult, _regime.entry_rule, _regime.stop_mult,
_regime.urgent_action, _regime.alert_level,
_index_alerted_downtrend, _volume_alerted_divergence,
_index_last_fluctuation_price,
_index_tech_state (每个 key),
_classify_market_pattern() 返回值,
_is_index_downtrend() 返回值,
_analyze_index_fluctuation() 返回值 (AI 调用 mock),
_check_volume_divergence 的 price_change 和 vol_change
```

#### B. 情景引擎 (market_state.py)
```
_scenario_probs (8 个情景各自的概率),
_scenario_scan_count,
_scenario_prev_outlook.primary.name,
_scenario_prev_outlook.primary.probability,
_scenario_prev_outlook.urgency,
_scenario_prev_outlook.bias,
MicroSignals (全部 18 个字段),
_update_scenario_engine() 中每个情景的 scores[name],
_update_scenario_engine() 中 raw[name] (归一化前),
_update_scenario_engine() 中 归一化后概率,
_push_scenario_alert 的 should_alert 判断条件
```

#### C. 持仓风控 — 每只持仓 (position_risk.py)
```
risk_level, pattern,
base_sl_tighten, base_tp_lower, base_trail_tighten,
sl_tighten (板块修正前),
sl_tighten (板块修正后),
tp_lower (板块修正前),
tp_lower (板块修正后),
trail_tighten (板块修正前),
trail_tighten (板块修正后),
is_today_buy, is_sector_weak, is_sector_accel_down,
loss_width, effective_sl, effective_sl floor,
profit_width, effective_tp,
trail_price, trailing_stop 值,
_bought_watch[code].max_profit_pct,
_bought_watch[code].status,
retracement: max_profit, keep_ratio, bonus, threshold, current_profit,
_classify_holding_status() 返回值,
_analyze_exit_context() 返回值 (三层分析文本),
_calc_exit_target() 返回值 (target_price, target_label),
_check_retracement_stop() 返回值 (key, kwargs),
_check_predictive_proximity() 的 sl_dist, tp_dist, market_bearish,
_check_dynamic_targets() 的 cealing, floor, new_tp, new_sl,
_handle_stop_signal() 调用参数 (key, code, stype, price, trigger),
_sl_reminders 队列状态 (每个 key 的 status, last_push, wake_at),
handle_sl_command() 返回值
```

#### D. 买入决策 — 每只候选信号 (buy_decision.py)
```
每个因子的评分贡献:
  因子1 (板块): trend, is_sector_weak, size_mul 贡献
  因子1b (概念): concept_score, concept_reason
  因子2 (买入区位置): zone_pos, zone_range
  因子3 (布林带): pct_b, bb_upper, bb_lower, bb_mid
  因子4 (均线): price vs ma5/ma10/ma20, bearish_alignment
  因子5 (日内指标): rsi6, rsi12, macd_direction, macd_bar, kdj_j, kdj_k, kdj_d, price_vs_ma5
  因子6 (盘口): ob_ratio, ob_reason
  因子7 (大单): big_ratio, big_reason
  因子8 (涨跌停): room_pct, risk_pct
  因子9 (昨日+今日): mf_ratio, ma5_angle, day_pos, daily_macd, daily_kdj, bbi, m5_macd

_evaluate_buy_decision(): reject_reasons, warn_reasons, size_mul (每个因子后)
_evaluate_below_zone(): 每个因子的 score, 总分, action
_calc_dynamic_buy_zone(): adj, shift, new_min, new_max, shift_pct
_calculate_position_size(): pattern, BLOCKED/CAUTIOUS, base, sector_adjustment, zone_adjustment, final_amount
_entry_rule 过滤: entry_rule 值, zone_pos, entry_skip_reason
_is_limit_up/code(): limit_up, limit_down, pre_close, 判断结果
_analyze_buy_context(): 返回的每一行文本
_execute_paper_buy(): stop_mult, effective_sl, max_amount, size_mul, target_pct, risk_result
_get_intraday_indicators(): rsi6, rsi12, macd_dif, macd_dea, macd_bar, macd_direction, kdj_k, kdj_d, kdj_j, price_vs_ma5
_get_context_factors(): 返回的全部字段（特别是 m5_macd_dif 等 5分钟因子）
_get_order_book_imbalance(): bid_ratio, bid_ratio 原始值
_get_big_order_direction(): big_ratio, big_ratio 原始值
_get_instrument_info(): up_stop, down_stop, float_share
```

#### E. 板块趋势 — 每 3 轮 (sector_context.py)
```
_update_sector_trends(): 每个板块的 changes 列表, avg, market_avg,
_sector_trend_history[industry] 长度,
_sector_trend_continuity[industry],
_sector_trend_last_dir[industry],
_sector_stats[industry]: change_pct, relative, up, down, breadth, continuity, amount, vol_ratio,
_concept_stats[concept]: change_pct, up, down, amount, vol_ratio,
_get_sector_trend(code): 返回值完整字符串的各个组成部分:
  direction (持续走强/走弱/走强/走弱/横盘),
  cumulative, slope, n,
  accel (加速/趋缓/无),
  rel_str (强于大盘/弱于大盘/无),
  breadth_str (普涨/普跌/无),
  vol_str (放量/缩量/无),
  concept_parts,
_get_concept_trend_score(code): score, reason, weak_count, strong_count
```

#### F. 消息推送
```
每次 _alert() 调用的完整消息文本
每次 _alert_private() 调用的完整消息文本
消息计数: 买入信号、止损卖出、止盈卖出、大盘告警、技术拐点、异动等
去重状态: _alerted_sl_tp, _signal_alert_state, _review_alert_state
```

#### G. 跨日状态 (Day1 收盘 → Day2 启动)
```
Day1 收盘:
  portfolio: cash, total_value, positions (每只的 avg_cost, volume, stop_loss, take_profit)
  _bought_watch: 每个 code 的 max_profit_pct, status
  _signal_alert_state 大小
  _review_alert_state 大小
  _sl_reminders 大小
  _alerted_sl_tp 大小
  _index_alerted_downtrend
  _max_drawdown_alerted
  _closing_decision_done
  trade_signals 表: status 分布
  trade_orders 表: 当日新增
  trade_portfolio_snapshots 表: 当日快照
  trade_portfolio_positions 表: 当日持仓

Day2 启动:
  _restore_positions() 恢复的持仓列表
  portfolio._prev_total (应从 Day1 快照恢复)
  portfolio._peak_value (应从历史快照恢复)
  以上全部跨日状态变量是否已清空/重置
```

### 5.2 追踪实现

使用装饰器 + 上下文管理器，在每个方法的入口和出口记录 `self.__dict__` 中相关 key 的变化：

```python
@trace_vars(scope="market_state", vars=[
    "_index_prices", "_index_high", "_index_low", "_regime", ...
])
def _check_market_state(self, prices):
    ...
```

装饰器自动记录方法执行前后的变量值差异，写入 `tracer.snapshots[scan_count][step_name]`。

---

## 6. 场景定义

### 6.1 Day1 场景: 16 种大盘模式全覆盖

总扫描轮次: ~240 轮（9:25-11:30 = 125 分钟, 13:00-15:00 = 120 分钟）

#### 个股分配

从生产 DB 选取 30 只股票，覆盖不同板块、不同信号状态：

**已 bought 持仓 (2 只) — 测试止损止盈:**
| 代码 | 名称 | 板块 | 成本 | 止损 | 止盈 | trajectory |
|------|------|------|------|------|------|-----------|
| 300727 | 润禾材料 | 化工 | 37.45 | 35.50 | 45.00 | slow_fall → 触发止损 |
| 000791 | 甘肃能源 | 电力 | 9.70 | 8.70 | 12.00 | slow_rise → flat → 验证移动止盈+利润回撤 |

**pending 买入信号 (15 只) — 测试买入触发:**
| 代码 | 名称 | 买入区 | 止损 | trajectory | 预期 |
|------|------|--------|------|-----------|------|
| 301568 | 思泰克 | 67.5-70.0 | 64.0 | slow_fall→入区 | 触发买入 |
| 300408 | 三环集团 | 112-115.35 | 105.0 | flat→不入区 | 不触发 |
| 301366 | 一博科技 | 45.0-46.8 | 42.0 | slow_fall→入区 | 触发买入 |
| 002106 | 莱宝高科 | 11.5-11.9 | 11.0 | sharp_fall→低于区 | 回调评估→abandon or watching |
| 002859 | 洁美科技 | 62.99-66.2 | 59.23 | slow_rise→高于区 | 不追高 |
| 002185 | 华天科技 | 15.84-16.32 | 14.8 | slow_fall→入区 | 触发买入 |
| 002156 | 通富微电 | 62.37-64.26 | 60.0 | slow_rise→高于区 | 板块走强时追高提醒 |
| 603005 | 晶方科技 | 43.86-45.19 | 37.7 | v_shape→入区 | V反后触发 |
| 600578 | 京能电力 | 8.22-8.47 | 8.0 | flat→入区 | 正常触发 |
| 600726 | 华电能源 | 6.63-6.83 | 6.4 | sharp_fall→低于区 | 回调abandon |
| 600584 | 长电科技 | 66.3-68.9 | 65.0 | slow_fall→入区 | 正常触发 |
| 000988 | 华工科技 | 159.39-164.22 | 152.0 | slow_rise→入区 | 正常触发 |
| 603806 | 福斯特 | 17.92-18.46 | 17.0 | slow_fall→入区 | 正常触发 |
| 300623 | 捷捷微电 | 34.8-36.0 | 33.9 | limit_up→涨停 | 涨停跳过 |
| 300319 | 麦捷科技 | 14.2-14.8 | 13.5 | w_shape→入区 | W底后confirm入场 |

**板块趋势对照 (8 只) — 测试板块计算:**
从 stock_basic 选取不同行业的代表性股票：

| 代码 | 行业 | trajectory |
|------|------|-----------|
| 000001 | 银行(上证) | 指数轨迹 |
| 600519 | 白酒 | slow_fall |
| 300750 | 锂电池 | slow_rise |
| 002371 | 半导体 | v_shape |
| 601899 | 黄金 | sharp_rise |
| 600036 | 银行(个股) | flat |
| 000858 | 食品 | slow_fall |
| 300274 | 光伏 | sharp_fall |

#### 大盘走势时间线 (Day1)

| 扫描轮次 | 钟表时间 | 上证指数 | 模式 | 触发事件 |
|---------|---------|---------|------|---------|
| 0 | 09:24 | - | - | 启动，_restore_positions |
| 1 | 09:25 | 3300 +0.0% | normal | 第一轮，_send_opening_decision |
| 2-10 | 09:26-09:34 | 3300-3305 | normal | 正常扫描，买入信号检查 |
| 11-25 | 09:35-09:49 | 3305-3320 | uptrend | 缓涨，pullback 入场 |
| 26-40 | 09:50-10:04 | 3320-3340 | melt_up | 加速冲顶，追高风险 |
| 41-55 | 10:05-10:19 | 3340→3300 | inverted_v | 高位回落，暂停买入 |
| 56-75 | 10:20-10:39 | 3300-3250 | panic | 加速下跌，熔断 |
| 76-85 | 10:40-11:00 | 3250-3240 | one_sided | 重心持续下移 |
| 86-100 | 11:00-11:25 | 3240-3250 | w_bottom | 二次探底，confirm入场 |
| 101-125 | 11:26-11:30 | (午休) | - | - |
| 126-145 | 13:00-13:19 | 3250-3300 | v_reversal | V型反转，半仓 |
| 146-165 | 13:20-13:39 | 3300-3320 | gap_up_fade | (模拟跳空高开回落) |
| 166-185 | 13:40-13:59 | 3300-3320 | wide_choppy | 宽幅震荡 |
| 186-210 | 14:00-14:24 | 3310-3320 | gap_down_recover | (模拟跳空低开回升) |
| 211-225 | 14:25-14:39 | 3320 | late_rally | 尾盘拉升 |
| 226-240 | 14:40-15:00 | 3320→3250 | late_dump | 尾盘跳水→收盘 |

#### 每轮大盘数据预设

对上述时间线，上证指数的具体价格序列按以下公式生成：

```python
def index_price(scan: int) -> float:
    if scan <= 10:    return 3300 + (scan - 1) * 0.5          # normal: 3300→3305
    if scan <= 25:    return 3305 + (scan - 11) * 1.0         # uptrend: 3305→3320
    if scan <= 40:    return 3320 + (scan - 26) * 1.33        # melt_up: 3320→3340
    if scan <= 55:    return 3340 - (scan - 40) * 2.67        # inverted_v: 3340→3300
    if scan <= 75:    return 3300 - (scan - 55) * 2.5         # panic: 3300→3250
    if scan <= 85:    return 3250 - (scan - 75) * 1.0         # one_sided: 3250→3240
    if scan <= 100:   return 3240 + (scan - 85) * 0.67        # w_bottom: 先跌后涨
    if scan <= 145:   return 3250 + (scan - 125) * 2.5        # v_reversal: 3250→3300
    if scan <= 165:   return 3300                            # gap_up_fade模拟
    if scan <= 185:   return 3300 + sin((scan-165)*0.3)*15    # wide_choppy: 震荡
    if scan <= 210:   return 3300 + (scan - 185) * 1.0        # gap_down_recover
    if scan <= 225:   return 3310 + (scan - 210) * 0.67       # late_rally
    return 3320 - (scan - 225) * 4.67                        # late_dump: 3320→3250
```

同时需要预设：
- `change_pct` = (price - 3300) / 3300（相对开盘价）
- `amount` = 1e11 + scan * 1e9（累计成交额递增）
- 涨跌家数比根据模式调整（panic 时跌>涨，uptrend 时涨>跌）

### 6.2 Day2 场景: 跨日状态 + 极端行情

Day1 收盘状态由 Day1 的运行结果决定。Day2 启动前：

1. 从 Day1 的 `trade_portfolio_snapshots` 恢复 `_prev_total`
2. 从 Day1 的 `trade_orders` 恢复持仓
3. 清空所有跨日状态变量
4. 保留 `_bought_watch` 中的 `max_profit_pct`（从 DB 恢复）

Day2 大盘走势：

| 扫描轮次 | 时间 | 上证 | 模式 | 验证重点 |
|---------|------|------|------|---------|
| 1 | 09:25 | 3200(-1.5%) | gap_down | 跳空低开，开盘决策 |
| 2-20 | 09:26-09:44 | 3200-3220 | gap_down_recover | 回升，confirm入场 |
| 21-40 | 09:45-10:04 | 3220→3180 | one_sided | 单边跌，暂停 |
| 41-60 | 10:05-10:24 | 3180→3150 | panic | 加速下跌，熔断 |
| 61-80 | 10:25-10:44 | 3150-3200 | dead_cat | 弱反弹，不跟进 |
| 81-100 | 10:45-11:04 | 3200→3250 | fishing_line | 慢涨，警惕 |
| 101-120 | 11:05-11:30 | 3250→3180 | fishing_line | 尾盘急跌！ |
| 121-160 | 13:00-13:39 | 3180-3160 | m_top | M型双顶 |
| 161-200 | 13:40-14:19 | 3160-3190 | dead_cat | 再次弱反弹 |
| 201-240 | 14:20-15:00 | 3190→3140 | late_dump | 收盘跳水 |

---

## 7. 已知答案预计算

### 7.1 计算方法

对每一轮扫描，基于：
1. 预设的大盘价格序列
2. 预设的个股价格轨迹
3. 生产 DB 中的基础数据（昨收价、MA、行业等）

**手工计算**每个变量的预期值。使用 Python 脚本辅助（调用相同的指标计算函数），但结果手工验证后写入 expected JSON。

### 7.2 分阶段预计算

由于变量数量巨大，分批计算：

**Phase 1: 大盘状态 + 情景引擎**（每个 scan 1 个 JSON）
- ~30 个变量，240 轮扫描 = 7,200 个预期值

**Phase 2: 持仓风控**（每只持仓 × 每个 scan）
- ~50 个变量，2 只持仓 × 240 轮 = 24,000 个预期值

**Phase 3: 买入决策**（每只候选信号 × 触发时）
- ~40 个变量，仅在信号进入买入区时计算
- 约 15 只信号 × 平均触发 3 轮 = 1,800 个预期值

**Phase 4: 板块趋势**（每 3 轮）
- ~80 个变量，80 轮 = 6,400 个预期值

**Phase 5: 消息推送**（事件驱动）
- 每触发一个事件，验证完整消息文本

**Phase 6: 跨日状态**（Day1 收盘 + Day2 启动）
- ~50 个变量

### 7.3 Expected JSON 格式

```json
{
  "scan": 47,
  "clock": "09:35",
  "index": {
    "price": 3304.5,
    "change_pct": 0.0014,
    "amount": 1.47e11
  },
  "market_state": {
    "_index_prices_len": 47,
    "_index_high": 3305.0,
    "_index_low": 3300.0,
    "_regime.pattern": "uptrend",
    "_regime.risk_level": "safe",
    "_regime.allow_buy": true,
    "_regime.position_mult": 1.0,
    "_regime.entry_rule": "pullback",
    "_regime.stop_mult": 1.0,
    "_classify_market_pattern.return": "uptrend",
    "_is_index_downtrend.return": false
  },
  "scenario_engine": {
    "_scenario_probs.normal_stable": 0.35,
    "_scenario_probs.developing_uptrend": 0.42,
    "_scenario_probs.developing_downtrend": 0.08,
    "_scenario_probs.accelerating_down": 0.02,
    "_scenario_probs.accelerating_up": 0.05,
    "_scenario_probs.potential_reversal_up": 0.03,
    "_scenario_probs.potential_reversal_down": 0.02,
    "_scenario_probs.dead_bounce": 0.03,
    "micro.price_velocity": 0.015,
    "micro.ema12_pos": "above",
    "micro.breadth_pct": 0.62,
    "outlook.primary.name": "developing_uptrend",
    "outlook.primary.probability": 0.42,
    "outlook.urgency": "watch"
  },
  "positions": {
    "300727": {
      "is_today_buy": false,
      "sector_trend_str": "板块化工 走弱 -1.2%",
      "is_sector_accel_down": false,
      "is_sector_weak": true,
      "base_sl_tighten": 0.70,
      "sl_tighten_after_sector": 0.665,
      "loss_width": 1.95,
      "effective_sl": 36.15,
      "effective_sl_floor": 35.50,
      "effective_sl_final": 36.15,
      "price": 37.60,
      "stop_triggered": false,
      "max_profit_pct": 0.03,
      "holding_status": "watching",
      "retracement.max_profit": 0.03,
      "retracement.keep_ratio": null,
      "retracement.triggered": false
    },
    "000791": {
      "...": "..."
    }
  },
  "buy_decisions": {
    "301568": {
      "in_zone": true,
      "below_zone": false,
      "above_zone": false,
      "price": 68.50,
      "buy_min": 67.50,
      "buy_max": 70.00,
      "zone_pos": 0.40,
      "entry_rule": "pullback",
      "entry_rule_pass": true,
      "factor1_sector.score": 0,
      "factor1b_concept.score": 1,
      "factor2_zone.warn": false,
      "factor3_bb.pct_b": 45,
      "factor4_ma.below_all": false,
      "factor5_intra.rsi6": 52,
      "factor5_intra.macd_bar": 0.15,
      "factor5_intra.kdj_j": 55,
      "factor6_ob.bid_ratio": 0.52,
      "factor7_big.buy_ratio": 0.55,
      "factor8_limit.room_pct": 8.5,
      "factor9_context.mf_ratio": 3.2,
      "factor9_context.ma5_angle": 1.5,
      "factor9_context.m5_macd_dif": 0.12,
      "size_mul_before_decision": 1.0,
      "reject_reasons": [],
      "warn_reasons": [],
      "size_mul_final": 1.0,
      "decision_allowed": true
    }
  },
  "sector_trends": {
    "化工": {
      "history_len": 16,
      "latest_avg": -1.2,
      "slope": -0.008,
      "cumulative": -2.5,
      "direction": "持续走弱",
      "accel": "加速",
      "breadth": -0.55,
      "rel": -1.8,
      "vol_ratio": 1.8
    }
  },
  "messages": [
    "🔴 买入信号 — 301568 思泰克\n..."
  ],
  "alerts_count": 1
}
```

---

## 8. 断言引擎

### 8.1 对比策略

对每个变量：
- **浮点数**: `abs(actual - expected) < 0.01`（或根据精度要求调整）
- **字符串**: 完全匹配
- **布尔**: 完全匹配
- **列表长度**: 完全匹配
- **dict**: 逐 key 对比

### 8.2 失败报告

```
FAIL [Day1 Scan#47] positions.300727.effective_sl
  expected: 36.15
  actual:   35.92
  delta:    -0.23
  context:  sl_tighten=0.665, loss_width=1.95, cost=37.45
```

### 8.3 通过标准

所有变量 100% 匹配 = 测试通过。任何一个不匹配 = 测试失败，输出完整的差异报告。

---

## 9. 执行流程

```
1. db_setup.py:  复制 storage/stock_market.db → tests/e2e/test_db/
2. run_e2e.py:   加载 scenarios/day1.py
3. 安装 SimClock (patch datetime + time.sleep)
4. 安装 SimQMT (替换 QuoteClient)
5. 安装 SimTelegram (替换 MessageSender + MessageReceiver)
6. 创建 Watcher (使用测试 DB 路径)
7. 运行 Day1: 240 轮 _scan()
8. 每轮扫描后: Tracer 记录所有变量 → 与 expected JSON 逐项对比
9. Day1 收盘: _finalize_close() → 记录收盘状态
10. 运行 Day2: 重新创建 Watcher → _restore_positions → 240 轮 _scan()
11. Day2 收盘: 记录最终状态
12. db_cleanup.py: 输出测试数据清单
```

---

## 10. 文件分工

| 文件 | 职责 | 预估行数 |
|------|------|---------|
| sim_clock.py | 可控时钟 | ~80 |
| sim_qmt.py | 模拟 QMT + 价格生成器 | ~250 |
| sim_telegram.py | 消息捕获 + 用户回复模拟 | ~80 |
| tracer.py | 变量追踪装饰器 | ~150 |
| assertions.py | 断言引擎 | ~120 |
| db_setup.py | DB 复制 + 初始状态快照 | ~60 |
| db_cleanup.py | 测试数据清单 | ~80 |
| run_e2e.py | 主入口 | ~200 |
| scenarios/day1.py | Day1 全部输入+预期输出 | ~800 |
| scenarios/day2.py | Day2 全部输入+预期输出 | ~600 |
| expected/*.json | 480 个 JSON 文件 | ~50KB |
