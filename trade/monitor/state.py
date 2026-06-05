"""盯盘运行时状态 — 所有跨模块共享的状态集中定义。

每个字段对应 watcher.__init__ 中的一个 self._xxx 变量。
重构中逐步将 self._xxx 访问改为 self.state.xxx，使函数签名显式化。
"""

from collections import defaultdict
from dataclasses import dataclass, field

from trade.monitor.market_state import MarketRegime


@dataclass
class ScanState:
    """盯盘运行时全部状态。由 Watcher 持有，传递给各领域模块。"""

    # ═══════════════════════════════════════════════════════════════
    # 核心
    # ═══════════════════════════════════════════════════════════════
    running: bool = False
    trade_date: str = ""
    scan_count: int = 0

    # ═══════════════════════════════════════════════════════════════
    # 子监控器（懒加载）
    # ═══════════════════════════════════════════════════════════════
    review_monitor: object | None = None
    sector_monitor: object | None = None
    abnormal_detector: object | None = None
    receiver: object | None = None
    executor: object | None = None

    # ═══════════════════════════════════════════════════════════════
    # 指数日内走势追踪
    # ═══════════════════════════════════════════════════════════════
    index_prices: list[float] = field(default_factory=list)
    index_high: float = 0.0
    index_low: float = 0.0
    index_map: dict[str, dict] = field(default_factory=dict)
    index_close_high: float = 0.0
    index_close_low: float = 0.0
    index_alerted_downtrend: bool = False
    index_alerted_ma20: int = 0
    index_last_fluctuation_price: float = 0.0

    # 指数技术指标拐点检测状态
    index_tech_state: dict[str, str | None] = field(default_factory=lambda: {
        "macd_cross": None,
        "rsi6_zone": "normal",
        "rsi12_zone": "normal",
        "kdj_cross": None,
        "kdj_j_zone": "normal",
        "divergence": None,
    })

    # ═══════════════════════════════════════════════════════════════
    # 市场宽度 + 量能
    # ═══════════════════════════════════════════════════════════════
    market_breadth: dict = field(default_factory=lambda: {"up": 0, "down": 0, "flat": 0, "total": 0})
    market_turnovers: list[float] = field(default_factory=list)
    volume_alerted_divergence: bool = False

    # ═══════════════════════════════════════════════════════════════
    # 市场状态
    # ═══════════════════════════════════════════════════════════════
    regime: MarketRegime | None = None
    closing_decision_done: bool = False
    max_drawdown_alerted: bool = False

    # ═══════════════════════════════════════════════════════════════
    # 数据就绪
    # ═══════════════════════════════════════════════════════════════
    data_ready: bool = False
    data_ready_at: float = 0
    data_missing_rounds: int = 0
    market_snapshot: dict[str, dict] = field(default_factory=dict)
    collector_client: object | None = None
    last_index_quote: dict | None = None
    last_db_ts: float = 0

    # ═══════════════════════════════════════════════════════════════
    # 板块趋势跟踪
    # ═══════════════════════════════════════════════════════════════
    sector_trend_history: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    sector_trend_continuity: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    sector_trend_last_dir: dict[str, str] = field(default_factory=dict)
    sector_trend_start: dict[str, str] = field(default_factory=dict)
    concept_trend_history: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    concept_trend_continuity: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    concept_trend_last_dir: dict[str, str] = field(default_factory=dict)
    concept_trend_start: dict[str, str] = field(default_factory=dict)

    # 板块/概念缓存
    industry_cache: dict[str, str] = field(default_factory=dict)
    concept_cache: dict[str, list[str]] = field(default_factory=dict)
    sector_stats: dict[str, dict] = field(default_factory=dict)
    concept_stats: dict[str, dict] = field(default_factory=dict)

    # 共振/逆势分析
    last_resonance_push_scan: int = -100
    last_resonance_index_dir: str = ""

    # ═══════════════════════════════════════════════════════════════
    # 信号/复盘提醒状态（防重复推送）
    # ═══════════════════════════════════════════════════════════════
    triggered_ids: set[int] = field(default_factory=set)
    alerted_sl_tp: set[str] = field(default_factory=set)
    alert_fingerprints: dict[str, int] = field(default_factory=dict)
    signal_alert_state: dict[int, tuple[float, bool]] = field(default_factory=dict)
    review_alert_state: dict[str, tuple[float, bool]] = field(default_factory=dict)
    prev_snapshot: dict[str, dict] = field(default_factory=dict)

    # ═══════════════════════════════════════════════════════════════
    # 卖出冷却
    # ═══════════════════════════════════════════════════════════════
    recently_sold: dict[str, int] = field(default_factory=dict)

    # ═══════════════════════════════════════════════════════════════
    # 持仓追踪
    # ═══════════════════════════════════════════════════════════════
    pos_meta: dict[str, dict] = field(default_factory=dict)
    bought_watch: dict[str, dict] = field(default_factory=dict)
    recent_prices: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    snapshot_price_history: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    # 止损提醒循环
    sl_reminders: dict[str, dict] = field(default_factory=dict)

    # ═══════════════════════════════════════════════════════════════
    # 缓存
    # ═══════════════════════════════════════════════════════════════
    ma_baseline_cache: tuple | None = None
    limit_cache: dict[str, dict] = field(default_factory=dict)
    instrument_cache: dict[str, dict] = field(default_factory=dict)
    intraday_cache: dict[str, dict] = field(default_factory=dict)
    intraday_cache_scan: int = -1
    daily_factor_cache: dict[str, dict] = field(default_factory=dict)

    # ═══════════════════════════════════════════════════════════════
    # 监控列表
    # ═══════════════════════════════════════════════════════════════
    cached_db_watch_codes: set[str] = field(default_factory=set)
    watch_codes_stale: bool = True

    # ═══════════════════════════════════════════════════════════════
    # 回踩机会发现
    # ═══════════════════════════════════════════════════════════════
    pullback_scan_count: int = 0
    pullback_alerted_today: set[str] = field(default_factory=set)

    # ═══════════════════════════════════════════════════════════════
    # AI 异步
    # ═══════════════════════════════════════════════════════════════
    pending_chase: dict[str, dict] = field(default_factory=dict)
    pending_index_ai: dict = field(default_factory=dict)
    morning_sector_bias: dict[str, dict] = field(default_factory=dict)

    # ═══════════════════════════════════════════════════════════════
    # 推送冷却
    # ═══════════════════════════════════════════════════════════════
    push_cooldown: dict[str, tuple[int, float]] = field(default_factory=dict)
    health_alert_seen: dict[str, int] = field(default_factory=dict)

    # ═══════════════════════════════════════════════════════════════
    # 场景引擎状态（由 market_state 管理，暂存于此）
    # ═══════════════════════════════════════════════════════════════
    scenario_probs: dict[str, float] = field(default_factory=dict)
    scenario_scan_count: int = 0
    scenario_last_alert_scan: int = 0
    scenario_prev_velocity: float = 0.0
    scenario_recent_lows: list[float] = field(default_factory=list)
    scenario_recent_highs: list[float] = field(default_factory=list)
    scenario_prev_breadth: float = 0.0
    scenario_prev_outlook: object | None = None
    scenario_next_confirmation_scan: int = 9999
    scenario_last_confirmed_at: float = 0.0
    scenario_confirmation_boost: float = 0.0

    # 模式去重
    pattern_last_alert: dict[str, int] = field(default_factory=dict)
    last_logged_pattern: str = ""
    regime_pending_pattern: str = ""
    regime_confirm_count: int = 0
    regime_switch_times: list[float] = field(default_factory=list)
    last_index_alert_scan: int = 0
    last_index_alert_advice: str = ""
    breadth_block_alerted: bool = False
    _last_resonance_labels: dict[str, str] = field(default_factory=dict)
    _scenario_recent_prices: list[float] = field(default_factory=list)
    _prev_ind_amounts: dict[str, float] = field(default_factory=dict)
    _prev_con_amounts: dict[str, float] = field(default_factory=dict)
