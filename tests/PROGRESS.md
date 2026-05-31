# 测试进度记录

> 最后更新: 2026-05-29 22:51 | 全量: 762 passed, 0 failed

## 已完成 (20 个文件, 738 tests)

---

### 1. test_portfolio.py (40+ tests)
**组合管理器单元测试 — 开仓/平仓/持仓/T+1/风控**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestPortfolioBasic | 初始总资产=现金、空持仓、开仓扣减现金+记录position、重复开仓累加volume、开仓金额>现金返回False、volume=0/负的边界情况 |
| TestPortfolioClose | 平仓恢复现金+删除position、部分平仓按比例减volume、平仓量>持仓量忽略、平仓记录已实现盈亏 |
| TestPortfolioT1 | T+1当日买入不可卖(is_tradable=False)、隔日可卖、混合持仓(T+1+非T+1)的部分可卖判断 |
| TestPortfolioRisk | 最大回撤计算(drawdown property)、日内最大回撤更新、总风险敞口(持仓市值/总资产)、单票集中度上限 |
| TestPortfolioMarketValue | update_prices批量更新持仓市值、停牌股skip、恢复上市自动加入、除权除息成本调整 |

---

### 2. test_risk_engine.py (30+ tests)
**风控引擎单元测试 — 仓位计算/止损止盈/最大回撤/集中度**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestPositionSize | 凯利公式计算仓位、单票上限5%/10%/20%、大盘风险系数(正常1.0/危险0.5/熔断0)、组合已持3票后再开仓拒绝 |
| TestStopLoss | 固定止损价、ATR动态止损(2倍ATR)、移动止盈(最高价回撤8%)、分区止损(盈利>5%用浅止损) |
| TestMaxDrawdown | 总回撤>15%硬上限阻止开仓、回撤>10%减半仓位、回撤<5%正常开仓 |
| TestConcentration | 同板块3票触发集中度警告、单票>20%拒绝、行业分散度评分 |

---

### 3. test_collector.py (25+ tests)
**QMT 数据采集器单元测试 — 采集/写入/恢复**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestQMTFetch | get_all_quotes正常返回、网络超时重试3次、返回空数据skip、单只quote获取 |
| TestDBWrite | market_snapshots写入、批量insert优化、trade_date分区、重复ts去重 |
| TestRecovery | QMT断连自动重连(指数退避)、采集crash后从DB恢复最后快照、启动时指数K线回填 |

---

### 4. test_paper_trader.py (30+ tests)
**模拟盘交易器单元测试 — 买入/卖出/换仓/信号处理**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestPaperBuy | 限价买入(price<=buy_max)、买入区外跳过、资金不足减仓买入、MAX_POSITIONS达上限拒绝 |
| TestPaperSell | 止损卖出、止盈卖出、T+1不可卖、部分卖出 |
| TestSwap | 换仓评估(新旧对比打分)、换仓后旧票卖出+新票买入、换仓失败回滚 |
| TestSignalHandling | pending信号处理、expired信号跳过、信号去重(同code只保留最新) |

---

### 5. test_manual_executor.py (20+ tests)
**手动确认/实盘下单流程单元测试**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestManualConfirm | 用户回复「成交 000001」→ 确认实盘已执行、超时未确认加入提醒队列、确认后更新DB状态 |
| TestManualDelay | 「再等 N 000001」→ 推迟N分钟再提醒、多次推迟累计上限 |
| TestOrderExecution | 实盘下单参数校验(价格/数量/方向)、下单失败告警、成交回报匹配 |

---

### 6. test_market_state.py (35+ tests)
**大盘状态单元测试 — 指数/择时信号/熔断/MA20**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestIndexQuote | 上证指数实时行情、昨收价比较、涨跌幅计算、成交额获取 |
| TestMarketPattern | EMA12/EMA26日内计算、多头排列判断、MACD金叉死叉、RSI超买(>80)/超卖(<20) |
| TestIndexHalt | 指数跌幅>2%触发熔断预警、熔断期间不买入、熔断恢复后恢复买入 |
| TestMA20Danger | 指数<MA20且MA20下行→危险信号、指数>MA20正常、MA20走平中性 |
| TestTimingSignal | 择时信号综合评分(趋势+量能+情绪)、评分<30空仓、30-60半仓、>60满仓 |
| TestIntradayPattern | V型反转识别(跌>1%后回升>50%)、单边下跌(重心持续下移)、横盘震荡(窄幅区间) |

---

### 7. test_position_risk.py (25+ tests)
**持仓风控单元测试 — 止损/止盈/移动止损/利润回撤**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestStopLoss | 跌破止损价触发告警、未触及不触发、止损价=0跳过、limit down不可卖 |
| TestTakeProfit | 突破止盈价触发告警、未触及不触发、T+1跳过止盈 |
| TestTrailingStop | 移动止盈:最高价回撤8%触发、highest_price持续更新、从未盈利不触发 |
| TestRetracement | 利润回撤止盈(T1>15%回撤40%触发、T2>10%回撤45%、T3>5%回撤50%)、分级阈值边界 |
| TestAlertDedup | _sl_reminders已有key不重复告警、不同code独立key |

---

### 8. test_ai_advisor.py (50+ tests)
**AI 选股/信号生成单元测试 — JSON解析/候选排序/因子评估**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestJSONParse | 正常JSON解析、缺字段用默认值、嵌套JSON、markdown代码块包裹、混入说明文字 |
| TestSignalGenerate | AI返回多只候选、信号评分排序、重复code去重、空返回处理 |
| TestCandidateRank | 综合评分=信号分+趋势分+板块分、score<50过滤、同板块数量限制 |
| TestFactorEval | PE/PB/ROE因子评估、市值因子、动量因子、因子缺失降权处理 |

---

### 9. test_watcher.py (55+ tests)
**盯盘单元测试 — 扫描流程各环节独立测试**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestCollectorData | _recv_collector_data处理index消息、market消息更新snapshot、ts去重 |
| TestWatchCodes | _get_watch_codes合并信号+复盘+持仓、缓存复用(_watch_codes_stale=False)、缓存失效重建 |
| TestRealtimePrices | QMT正常返回价格、网络异常返回{}、部分code缺失的容错、批量分组(50只/组) |
| TestSignalCheck | pending信号→候选生成、买入区内/低于区/高于区的分类处理、信号过期(expired) |
| TestBoughtWatch | 买入后_max_profit跟踪最高浮盈、新买入初始化、隔日重置 |
| TestClosing | _check_closing尾盘时间判断、14:30前不触发、已执行标记防止重复、T+1不可卖跳过 |
| TestSectorTrends | _update_sector_trends板块统计、collector连接时跳过(由push触发)、断开时fallback自算 |
| TestLunchBreak | 午休时间检测(11:30-13:00)、午休期间降低扫描频率 |

---

### 10. test_telegram.py (30+ tests)
**Telegram Bot 单元测试 — 消息接收/命令解析/轮询**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestGetUpdates | 长轮询获取消息、offset偏移去重、超时重试、网络异常恢复 |
| TestCommandParse | /status→状态查询、/position→持仓查询、/signal→信号列表、/help→帮助、未知命令提示 |
| TestReply | 群聊消息回复(reply_to)、私聊消息、多行消息分割(>4096字符) |
| TestRateLimit | 发送频率限制(20条/分钟)、超限等待、队列积压处理 |

---

### 11. test_strategy_e2e.py (30+ tests)
**策略端到端单元测试 — 选股流水线/因子计算/排序**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestFullPipeline | 完整选股管线:A股全量→初筛(市值/流动性)→因子计算→综合排序→Top N |
| TestFactorCalc | 动量因子(20/60/120日)、波动率因子、质量因子(ROE/毛利率)、价值因子(PE/PB分位) |
| TestRanking | 多因子加权排序、因子缺失降权、行业中性化、市值分层 |
| TestCornerCases | 全市场停牌(返回空列表)、仅有ST股的过滤、新股(上市<60天)跳过 |

---

### 12. test_trend_screening.py (40+ tests)
**趋势筛选单元测试 — 技术指标/形态识别**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestEMATrend | EMA12>EMA26多头、EMA12<EMA26空头、EMA缠绕横盘、多周期EMA(60/120)确认 |
| TestMACDPattern | 金叉(快线上穿慢线)、死叉、零轴上下方含义、柱状图放大/缩小 |
| TestRSIZone | RSI>80超买区、RSI<20超卖区、RSI背离(价格新高RSI未新高)、RSI修复 |
| TestKDJCross | KDJ金叉/死叉、J值>100超买、J值<0超卖、KDJ与MACD共振 |
| TestBollinger | 布林带收窄(变盘前兆)、价格触及上轨/下轨、带宽扩张/收缩趋势 |
| TestVolumePrice | 放量上涨(健康)、缩量上涨(背离)、放量下跌(恐慌)、地量筑底 |

---

### 13. test_buy_decision.py (30+ tests)
**买入决策单元测试 — 仓位分配/信号合并/风控审批**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestCandidateMerge | 信号+复盘双源候选合并、同code取最高分、去重逻辑 |
| TestPositionSize | _calculate_position_size根据评分和波动率计算仓位、高波动减仓、单票上限 |
| TestBelowZone | 低于买入区评估:浅跌(2%内)打折买入、深跌(5%+)放弃、中间观望 |
| TestAboveZone | 高于买入区+板块持续走强→追高提醒(不自动买)、普通走强跳过 |
| TestRiskGate | risk_engine.can_open审批、大盘熔断全拒、回撤>15%全拒、正常市场逐票审批 |
| TestPaperBuy | _execute_paper_buy完整流程:算仓位→风控→下单→更新DB→加入_bought_watch |

---

### 14. test_indicators.py (40 tests)
**技术指标计算单元测试 — EMA/MACD/RSI/KDJ/Bollinger/ATR/Divergence**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestEMA | EMA12/EMA26计算、周期边界(<12返回简单均线)、空序列返回None |
| TestMACD | DIF/DEA/柱状图、金叉点识别、死叉点识别、零轴穿越 |
| TestRSI | RSI6/RSI12/RSI24、超买>80、超卖<20、多周期RSI背离 |
| TestKDJ | K/D/J三线计算、RSV原始值、J值>100/<0 |
| TestBollinger | 中轨(MA20)、上下轨(±2σ)、带宽百分比、%B指标 |
| TestATR | ATR(14)计算、ATR百分比、用于止损的ATR倍数 |
| TestDivergence | 顶背离(价格新高+RSI下降)、底背离(价格新低+RSI上升)、MACD背离 |

---

### 15. test_repo.py (23 tests)
**TradeRepository 单元测试 — 信号CRUD/订单/快照/因子/持仓/复盘**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestSignalCRUD | insert_signal→get_pending_signals、update_signal_status(pending→executed/expired)、按account过滤(paper/live) |
| TestTradeOrders | insert_order记录买卖、按stock_code聚合持仓、filled_volume/filled_price、trade_date |
| TestMarketSnapshots | insert_snapshot+get_latest、同ts覆盖、trade_date范围查询 |
| TestDailyFactors | insert_daily_factor、按code+date查询、因子缺失返回None |
| TestReviewPicks | insert_review_pick、get_pending_review_picks按日期过滤、update状态 |

---

### 16. test_abnormal.py (22 tests)
**异动检测单元测试 — 异动扫描/板块热度/换仓评估/快照构建**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestBuildMarketSnapshot | 空价格→空快照、价格→含price+timestamp的dict、格式匹配检测器输入 |
| TestGetAbnormalDetector | 类不存在时import失败→返回None(优雅降级)、cached复用 |
| TestGetSectorMonitor | 首次调用创建SectorHeatMonitor实例、cached复用 |
| TestCheckSectorHeat | 空快照跳过、有快照调monitor.check、无monitor跳过、有结果推送告警 |
| TestCheckAbnormal | 市场快照+detector检测异动、与prev_snapshot对比、无detector跳过 |
| TestEvaluateSwaps | 无pending信号跳过、价格在买入区内评估换仓、持仓<3跳过、换仓执行后失效缓存 |

---

### 17. test_closing.py (10 tests)
**尾盘决策单元测试 — 盈亏止损/止盈减仓/T+1锁定**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestClosingStop | 深亏(>5%)→强制止损、小幅亏损(<3%)→持有、临界值(3-5%)→提示但不强制 |
| TestClosingProfit | 浮盈>10%减半仓锁利、浮盈>20%减75%、浮盈<5%继续持有 |
| TestClosingT1 | T+1当日买入不参与尾盘卖出、多持仓混合(T+1+非T+1)仅非T+1可操作 |
| TestClosingTime | 14:30前_check_closing直接返回、14:30-15:00执行一次(_closing_decision_done标记)、次日重置 |

---

### 18. test_sector_context.py (18 tests)
**板块上下文单元测试 — 概念趋势/板块趋势/上下文构建/开盘决策**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestConceptTrendScore | 空缓存→(0,"")、全涨概念→score>=2偏强、全跌→score<=-2偏弱、混合→±3间、score clamp在±3 |
| TestSectorTrend | 无行业信息→""、有板块数据返回含行业名+方向的描述、breadth涨跌比包含在描述中 |
| TestBuildSectorContext | 空codes→""、多code构建含板块涨跌排行文本、按abs(change)排序 |
| TestRebuildFromSectorSnapshots | 空rows→空dict、多行重建trend_history、同方向连续性计算 |
| TestSendOpeningDecision | 无持仓无信号→仅推送市场概况、有持仓展示个股权重和盈亏、同板块>=3触发集中度警告 |

---

### 19. test_market_scenarios.py (96 tests) ★★★
**大盘+个股走势场景模拟 — 全维度覆盖，从大盘模式识别到个股行为交叉组合**

`ScenarioSimulator` 类绑定了 5 个 mixin 的方法（MarketState/SectorContext/PositionRisk/ClosingDecision/AbnormalMonitor），支持 `advance_scan(index_price, stock_prices)` 推进一轮完整扫描。

#### 大盘模式识别 (5 tests)
识别代码中 5 种模式的边界条件。

#### 大盘状态分层 (4 tests)
熔断、MA20 危险、正常、单边阻断。

#### 持仓风控场景 (5 tests)
止损/止盈/移动止损/多持仓同步。

#### 尾盘决策场景 (4 tests)
深亏止损、浮盈减仓、T+1 锁定、正常持有。

#### 板块趋势场景 (3 tests)
普跌、轮动、概念走弱。

#### 全链路端到端 (5 tests)
5 种完整交易日模拟（单边下跌、V 型反转、缓涨、震荡、恐慌→企稳→缓涨）。

#### 死猫跳模式 (2 tests) — 代码有但之前漏测
弱反弹识别 + 阻断买入。

#### 代码缺失的走势模式 (8 tests)
记录代码当前无法识别的走势：冲高回落/A 型、单边上涨、加速上涨/melt-up、尾盘跳水/拉升、跳空高开/低开、宽幅震荡。测试当前行为，暴露缺口。

#### 量价关系 (4 tests)
价升量缩(诱多)、价跌量增(恐慌放量)、价量齐升(健康)、数据不足不误报。

#### 单边趋势 × 个股交叉 (10 tests) — NEW

| 大盘 | 个股行为 | 测试 |
|------|---------|------|
| 单边上涨 | 同步涨 | 浮盈扩大 → 止盈 |
| 单边上涨 | 领涨(涨更多) | 更快触发止盈 |
| 单边上涨 | 弱跟(涨更少) | 跑输大盘，不止盈 |
| 单边上涨 | 横盘 | 无告警 |
| 单边上涨 | 逆势下跌 | 止损触发 |
| 单边下跌 | 同步跌 | 止损触发 |
| 单边下跌 | 领跌(跌更多) | 止损+利润回撤 |
| 单边下跌 | 抗跌 | 大盘阻断买入，持仓安然 |
| 单边下跌 | 逆势涨 | 独立行情，可能止盈 |
| 加速上涨 | 涨停 | 无法买入 |

#### 反转走势 × 个股交叉 (7 tests) — NEW

| 大盘 | 个股行为 | 测试 |
|------|---------|------|
| V 型反转 | 跟随 V | 先止损→后止盈 |
| V 型反转 | 不跟涨 | 大盘恢复个股仍低位 |
| 倒 V/A 型 | 跟随 A | 浮盈→回吐→移动止盈 |
| 倒 V/A 型 | 不跟跌 | 个股横住，抗跌 |
| M 型 | 跟随 M | 两次假突破，不重复告警 |
| N 型 | 跟随 N | 回调是买入机会 |
| W 型 | 跟随 W | 两次探底，第二次不创新低 |

#### 跳空开盘 × 个股交叉 (6 tests) — NEW

| 大盘 | 个股行为 | 测试 |
|------|---------|------|
| 高开高走 | 同步高开 | 浮盈快速扩大 |
| 高开低走 | 跟随回落 | 浮盈→浮亏，尾盘止损 |
| 高开低走 | 个股抗跌 | 大盘回落个股横住 |
| 低开低走 | 同步低开 | 开盘即触发止损 |
| 低开高走 | 跟随回升 | 收复失地，无告警 |
| 低开高走 | 不跟涨 | 大盘恢复个股继续跌 |

#### 冲高回落/探底 × 个股交叉 (4 tests) — NEW

| 大盘 | 个股行为 | 测试 |
|------|---------|------|
| 冲高回落 | 高点开仓 | T+1 买入即套 |
| 冲高回落 | 老持仓过山车 | 移动止盈触发 |
| 探底回升 | 未跟随下跌 | 抗跌持有 |
| 探底回升 | 涨超大盘 | 独立走强，触发止盈 |

#### 尾盘异动 × 个股交叉 (4 tests) — NEW

| 大盘 | 个股行为 | 测试 |
|------|---------|------|
| 尾盘跳水 | 多只从盈转亏 | 尾盘止损建议 |
| 尾盘拉升 | 浮盈扩大 | 减仓锁利建议 |
| 尾盘拉升 | T+1 锁定 | 浮盈但不能卖 |
| 尾盘异动 | 混合持仓 | 浮亏/浮盈/T+1 分别决策 |

#### 极端行情 × 个股极端 (4 tests) — NEW

| 大盘 | 个股行为 | 测试 |
|------|---------|------|
| 恐慌 | 跌停无法卖出 | 特殊告警 |
| 恐慌 | 跌停打开 | 恢复正常止损 |
| 暴涨 | 涨停无法买入 | _is_limit_up 正确 |
| 死猫跳 | 趁反弹出货 | 止损不触发(价格回升) |

#### 两极分化/多持仓联动 (5 tests) — NEW

指数横盘但持仓暴跌(独立利空)、暴涨(独立利好)、同板块集中崩盘、跨板块背离(银行涨科技跌)、半数触发半数正常。

#### 全链路多持仓 (3 tests) — NEW

冲高回落+3只混合持仓、跳空→慢回升+部分止损部分熬过、极端波动 5% 日内。

#### 跨日状态 (3 tests) — NEW

昨日已清仓今日空仓、新交易日重置 _closing_decision_done、多日累计回撤逼近上限。

---

### 20. test_watcher_integration.py (57 tests)
**盯盘全链路集成测试 — scan管线/信号到买入/持仓风控/Collector/异常恢复/状态转换/缓存/告警**

| 测试类 | 场景覆盖 |
|--------|----------|
| TestWatcherInit (5) | 核心属性赋值、telegram/qmt注入、flags默认值(_running/_scan_count/_closing_decision_done)、mixin方法可用性 |
| TestScanPipeline (11) | 完整_scan管线:recv→replies→watch_codes→prices→market_state→positions→signals→closing;空watch_codes跳过;空prices跳过;scan_count==1开盘决策;market_ok=False传递;drawdown>15%阻断;scan_count%50板块热度;scan_count%3异动;scan_count%15换仓 |
| TestSignalToBuyPipeline (5) | pending信号+价格在买入区→生成候选;高于买入区→候选被创建(zone过滤在_check_buy_candidates内);buy_zone=0跳过;repo异常捕获;价格缺失跳过 |
| TestPositionRiskPipeline (5) | 止损触发告警;止盈触发告警;T+1不触发;_sl_reminders去重;多持仓独立评估 |
| TestCollectorIntegration (4) | _handle_collector_index更新_last_index_quote+_index_prices;market消息更新_market_snapshot;ts去重(<=_last_db_ts跳过);disconnected不崩溃 |
| TestErrorResilience (6) | _check_market_state处理空价格返回bool;空价格下positions跳过;repo异常_signals捕获;QMT异常返回{};DB写入异常买入仍完成;scan中get_pending_signals异常 |
| TestStateTransitions (4) | _closing_decision_done阻止重复;空持仓尾盘跳过;午休检测;恢复持仓空DB |
| TestCacheInvalidation (5) | _watch_codes_stale=True重建;合并信号+复盘+持仓+bought_watch;缓存未失效复用;repo异常返回空回退;_invalidate_watch_codes_cache重置标记 |
| TestAlertIntegration (3) | _alert调telegram;无telegram不崩溃;telegram异常捕获 |
| TestMultiScanAccumulation (3) | _index_prices跨轮累积;market_snapshot跨轮持久;bought_watch跟踪最高浮盈 |
| TestReviewPicksIntegration (2) | 复盘推荐收到market_ok;load_review_picks空不崩溃 |
| TestBuyDecisionPipeline (4) | 候选过风控→执行买入;风控拒绝→不买;market_ok=False→不买;买入加入_bought_watch |

---

## 源码 Bug 修复

| 文件 | 问题 | 修复 |
|------|------|------|
| `trade/monitor/buy_decision.py` | 缺少 `from trade.paper.trader import MAX_POSITIONS` | 添加 import |
| `trade/monitor/abnormal.py` | `_build_market_snapshot` 用了 `time.time()` 但没有 `import time` | 添加 `import time` |

## 已知设计问题

| 位置 | 问题 | 影响 |
|------|------|------|
| `trade/monitor/abnormal.py` | `from trade.monitor.abnormal import AbnormalDetector` 自引用，类不存在 | `_get_abnormal_detector()` 始终返回 None，异动检测不可用 |
| `trade/monitor/watcher.py:_scan()` | 各步骤无独立 try/except，任一步骤抛异常会阻断后续 | 目前各内部方法自己处理异常，风险较低 |

---

## 完成度

全量 **808 tests, 0 failed**。所有模块从单元到集成到全链路场景已覆盖。

---

## 测试复盘与经验总结

### 一、测试方法论

**正确的做法：从真实市场出发，反向验证代码。**

1. 先枚举真实市场中所有可能的走势类型（大盘 30 种 × 个股 18 种），不被代码现有能力限制
2. 对每种走势，检查代码是否处理、如何处理、是否正确
3. 代码处理的 → 写测试验证
4. 代码不处理的 → 写测试记录当前行为（`TestMissingPatternGaps`），标注缺口

**错误的做法：从代码出发，只测试代码写了的那几个场景。**
这样会遗漏大量真实走势，给人一种"已经测完了"的错觉。

### 二、代码当前能识别的大盘模式（5 种）

| 模式 | 条件 | _check_market_state 响应 |
|------|------|--------------------------|
| `normal` | 默认 | market_ok=True（后续可能被 MA20/熔断 覆盖） |
| `panic` | 短期加速下跌 + 日内低位 | market_ok=False，建议减仓 |
| `one_sided` | EMA12 下方 + 重心下移 | market_ok=False，暂停买入 |
| `v_reversal` | 深跌后回升到 50%+ + EMA12 + 技术确认 | market_ok=True，恢复买入 |
| `dead_cat` | 弱反弹未过 50% 分位 + <EMA12 | market_ok=False，不跟进 |

### 三、代码缺失的大盘模式及建议

以下模式在真实市场中频繁出现，目前统一归为 `normal`。建议后续补充：

| 缺失模式 | 真实场景 | 建议处理 |
|----------|---------|---------|
| **单边上涨** (uptrend) | 全天持续走高 | 可保持买入但提醒不要追高 |
| **加速上涨** (melt-up) | 越涨越快 | 类比 panic 的对称面，提醒冲顶风险 |
| **倒 V/A 型** | 上午涨→下午全跌回 | 应阻断买入，警惕诱多 |
| **尾盘跳水** | 最后 30 分钟急跌 | 触发尾盘紧急止损 |
| **尾盘拉升** | 最后 30 分钟急涨 | 可能是次日利好信号，也可能是操纵 |
| **跳空低开** | 开盘大幅低于昨收 | 开盘即评估风险，可能直接触发熔断 |
| **跳空高开** | 开盘大幅高于昨收 | 注意高开低走风险 |
| **宽幅震荡** | 振幅 >2% 无方向 | 高波动环境，减半仓位 |
| **两极分化** | 指数稳个股崩 | 不能只看指数，需要 breadth 指标 |
| **N/M/W 型** | 多波反转 | 拒绝追涨杀跌，等趋势确认 |
| **连续多日下跌** | 跨日趋势累积 | 昨日已触发止损，今日应继续谨慎 |

### 四、个股相对大盘的行为矩阵

个股在不同大盘环境下的响应逻辑总结：

```
           大盘涨     大盘跌     大盘横盘
个股领涨    止盈✓      关注(异常)  止盈✓
个股同步    浮盈       止损触发    持有
个股弱跟    持有       止损触发    持有
个股逆势    关注(利好)  持有       关注(利好)
个股横盘    持有       持有       持有
个股跌停    --         无法卖出    --
个股涨停    无法买入   --         --
```

**关键发现：**
1. **大盘阻断买入 不等于 触发个股止损** —— 止损取决于个股价格是否跌破止损线，与 market_ok 无关
2. **T+1 保护在极端市场中有双重效果** —— 防止了恐慌卖出但也锁定了损失
3. **多持仓 + 同板块集中** —— 同板块 3 只同时触发止损时，系统应有集中度告警

### 五、已知设计缺口

| # | 问题 | 影响 | 优先级 |
|---|------|------|--------|
| 1 | `_classify_market_pattern` 缺少 10+ 种模式 | 危险走势被归为 normal，继续买入 | 高 |
| 2 | `_check_market_state` 只看涨跌不看跳空 | 跳空低开 1.5% 可能不触发任何阻断 | 高 |
| 3 | 尾盘异动无大盘层面检测 | 尾盘跳水时大盘模式仍是 normal | 中 |
| 4 | breadth(涨跌比) 只在 `_is_index_downtrend` 中使用 | 两极分化日（指数稳个股崩）无法检测 | 中 |
| 5 | `AbnormalDetector` 类不存在 | 异动检测功能不可用 | 低 |
| 6 | `_scan()` 各步骤无 try/except | 单步异常阻断后续扫描 | 低（各方法内部已有 try/except） |

### 六、后续工作方向

1. **代码增强**：根据上述缺失模式，扩展 `_classify_market_pattern` 和 `_check_market_state`
2. **跨日测试**：当前测试主要集中在单日，需要补充多日连续场景（连续阴跌、隔日跳空等）
3. **性能测试**：808 个测试中全链路测试较少，可增加超长交易日（240 轮扫描）的压力测试
