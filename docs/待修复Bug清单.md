# 待修复 Bug 清单

> 2026-05-30 梳理，来源于盯盘涨跌幅计算排查。

## Bug 1: sector_context / sector_heat 中 changePct 格式歧义链 ✅ 已修复

- **涉及文件**: `qmt_collector.py:251`, `watcher.py:346`
- **问题**: QMT `all_quotes` 返回的 `changePct` 被直接透传，上下游对格式假设互相矛盾
- **修复**: `qmt_collector.py` 统一归一化为小数格式（`abs > 1` 时除以 100）；`watcher.py` 活跃板块阈值从 `> 1` 改为 `> 0.01`

## Bug 2: watcher.py 活跃板块阈值可能永不触发 ✅ 已修复

- **涉及文件**: `watcher.py:346`
- **修复**: 阈值从 `abs(chg) > 1` 改为 `abs(chg) > 0.01`（适配小数格式）

## Bug 3: AI 指数分析总变动用 prices[0] 当基准 ✅ 已修复

- **涉及文件**: `market_state.py:1979`
- **修复**: 优先用 `_last_index_quote["pre_close"]` 计算总变动，fallback 到 `prices[0]`

## Bug 4: _index_trend_desc 变幅 + QMT changePct 启发式 ✅ 已修复

- `market_state.py:1885` — `chg = (prices[-1] - prices[0]) / prices[0] * 100` → 改用 `pre_close`
- `qmt_collector.py:272` — `abs(raw) > 1` 启发式格式检测 → 改用 `(price-pre_close)/pre_close`

## Bug 10: detect_divergence IndexError（E2E 测试发现）✅ 已修复

- **涉及文件**: `analysis/screening/indicators.py:172-177`
- **问题**: `closes[-lookback:]` 和 `dif[-lookback:]` 切片长度不一致时，循环中 `d[i]` 越界
- **根因**: `calc_macd_series` 返回的 DIF 序列可能比价格序列短（数据不足时）
- **修复**: 取 `min(len(c), len(d))` 对齐两个序列

## Bug 5: QMT 服务端并发（来自说明文档）

- **问题**: 数据采集（`cmd_collect`）和交易盯盘（`cmd_monitor`）是两个独立进程，各自持有独立 QMTClient 实例，高峰期可能服务端排队或超时
- **修复方向**: 考虑给数据采集和交易系统使用不同的 QMT 端口或错峰调度

## Bug 6: QMT /all_quotes amount 字段待验证（来自说明文档）

- **问题**: 板块资金量能计算依赖 `amount` 字段，已做降级处理但未在 Windows QMT 上验证
- **上下文**: `ssh win` → 192.168.1.33，需在 QMT 终端上确认 `/all_quotes` 是否返回 amount
- **影响**: 若字段为空，板块量比（vol_ratio）始终为 1.0，量能维度失效

## Bug 7: 情景预测引擎参数待实测校准（来自说明文档）

- **问题**: 8 种情景的 confirm/reject 权重（+0.15/-0.25）、时间衰减（0.92）、紧急阈值（70%/55%/35%）均为理论值
- **状态**: 已通过 96 个场景模拟测试，但真实市场噪声和边界情况可能与模拟不同
- **修复方向**: 在真实交易环境中观察并调优

## Bug 8: 三层联动因子待量化验证（来自说明文档）

- **问题**: 板块放大系数的分段（1.2~1.4× 共振、0.4~0.6× 对冲）基于经验设定，`buy_zone_shift` 最大 15% 的上下限也未经足够样本验证
- **修复方向**: 收集更多市场数据校准参数

## Bug 9: 预测性告警误报率待观察（来自说明文档）

- **问题**: 止损/止盈接近告警和动态目标修正在真实交易中可能产生过多告警
- **状态**: 去重间隔（15/20 轮）在模拟测试中合理，但实盘中可能需要调整
- **修复方向**: 实盘观察误报频率，调整去重间隔或增加抑制条件
