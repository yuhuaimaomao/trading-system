# 电报采集后 AI 结构化方案

## 问题

当前电报采集后直接入库，复盘时通过 Function Calling 工具 `get_telegraph_news(stock_code)` 按 `stock_tags` 字段匹配查询。但 CLS API 返回的 `stock_tags` 覆盖率仅 13%（2346 条中 306 条有标签），导致 87% 的电报对复盘不可见。

## 方案

每次 `TelegraphCollector.collect()` 入库后，将新增电报（去重后 2-5 条）发送给 AI 做结构化分析，结果落回 `cls_telegraph` 表。

## 调用时机

**方案 A（同步）**：`_save_to_db` 返回新增记录列表后，立刻发给 AI 做结构化，结果 UPDATE 回对应行。单次 2-3 秒延迟，在 5 分钟轮询间隔内完全可接受。AI 挂了有 fallback（原始数据仍在），下一轮会重试。

## AI 输出字段

| 字段 | 说明 | 复盘用途 |
|---|---|---|
| `ai_summary` | 一句话摘要 | 替代 content[:80] 截断 |
| `ai_sentiment` | 利好/利空/中性 | 快速筛选方向 |
| `ai_impact` | 对 A 股的具体影响（50 字内） | AI 复盘时判断是否影响持仓 |
| `ai_stocks` | `[{code, name, relevance}]` | 替代 CLS 的 stock_tags 做查询匹配 |
| `ai_importance` | 1-5 AI 打分 | 替代现有 score 系统 |
| `ai_direction` | 宏观/政策/行业/个股/市场情绪/其他 | 替代 CLS 的 category |

前 4 个必须，后 2 个建议保留。

## 模型与成本

- 百炼 qwen-turbo，从 env `TELEGRAPH_AI_MODEL` 可配
- 每轮 ~500 token in + 200 token out，每天 ~7 万 token，成本可忽略

## Prompt 方向

以 A 股交易员视角读电报：
- 提取关联股票时，优先 6 位数字代码，名称要准确
- 利好利空判断从 A 股市场角度出发
- 重要性按实质影响打分，不因标题惊悚而加分

## 复盘查询改造

`get_telegraph_news(stock_code)` 不再依赖 CLS `stock_tags`，改为查 `ai_stocks` JSON 字段。覆盖率从 13% → 接近 100%。

## 状态

方案已讨论，未实现。
