# -*- coding: utf-8 -*-
"""变量追踪器 — 记录 _scan() 每步后的变量快照。"""

import json
from pathlib import Path


def snapshot(watcher, scan: int, clock_str: str) -> dict:
    """提取 Watcher 当前状态的所有关键变量。

    返回一个嵌套 dict，结构对应 TEST_PLAN.md 中定义的 7 大类。
    所有浮点数保留 2 位小数以减小 JSON 体积。
    """
    w = watcher
    r2 = lambda v: round(v, 2) if isinstance(v, float) else v

    snap = {
        "scan": scan,
        "clock": clock_str,
    }

    # ── A. 大盘状态 ──
    idx_prices = getattr(w, '_index_prices', [])
    regime = getattr(w, '_regime', None)
    snap["market_state"] = {
        "index_price": r2(idx_prices[-1]) if idx_prices else 0,
        "index_prices_len": len(idx_prices),
        "index_high": r2(getattr(w, '_index_high', 0)),
        "index_low": r2(getattr(w, '_index_low', 0)),
        "market_turnovers_len": len(getattr(w, '_market_turnovers', [])),
        "regime_pattern": regime.pattern if regime else None,
        "regime_risk_level": regime.risk_level if regime else None,
        "regime_allow_buy": regime.allow_buy if regime else None,
        "regime_position_mult": r2(regime.position_mult) if regime else None,
        "regime_entry_rule": regime.entry_rule if regime else None,
        "regime_stop_mult": r2(regime.stop_mult) if regime else None,
        "regime_urgent_action": regime.urgent_action if regime else None,
        "index_alerted_downtrend": getattr(w, '_index_alerted_downtrend', False),
        "volume_alerted_divergence": getattr(w, '_volume_alerted_divergence', False),
        "index_tech_state": dict(getattr(w, '_index_tech_state', {})),
    }

    # ── B. 情景引擎 ──
    probs = getattr(w, '_scenario_probs', {})
    outlook = getattr(w, '_scenario_prev_outlook', None)
    snap["scenario"] = {
        "probs": {k: r2(v) for k, v in probs.items()} if probs else {},
        "primary_name": outlook.primary.name if outlook else None,
        "primary_prob": r2(outlook.primary.probability) if outlook else None,
        "urgency": outlook.urgency if outlook else None,
        "bias": outlook.bias if outlook else None,
    }

    # ── C. 持仓风控 — 每只持仓 ──
    positions = {}
    portfolio = getattr(w, 'portfolio', None)
    bought_watch = getattr(w, '_bought_watch', {})
    sl_reminders = getattr(w, '_sl_reminders', {})

    if portfolio:
        for code, pos in portfolio.positions.items():
            bw = bought_watch.get(code, {})
            pos_data = {
                "stock_name": pos.stock_name,
                "volume": pos.volume,
                "avg_cost": r2(pos.avg_cost),
                "current_price": r2(pos.current_price),
                "pnl_pct": r2(pos.pnl_pct),
                "stop_loss": r2(pos.stop_loss),
                "take_profit": r2(pos.take_profit),
                "trailing_stop": r2(pos.trailing_stop),
                "highest_price": r2(pos.highest_price),
                "entry_date": pos.entry_date,
                "sector_code": pos.sector_code,
                # _bought_watch
                "bw_max_profit_pct": r2(bw.get("max_profit_pct", 0)),
                "bw_status": bw.get("status", "watching"),
                "bw_exit_target": r2(bw.get("exit_target", 0)) if bw.get("exit_target") else None,
            }
            positions[code] = pos_data

    snap["positions"] = positions

    # SL 提醒队列
    reminders = {}
    for key, rem in sl_reminders.items():
        reminders[key] = {
            "code": rem.get("code"),
            "type": rem.get("type"),
            "status": rem.get("status"),
            "trigger": r2(rem.get("trigger", 0)),
        }
    snap["sl_reminders"] = reminders

    # ── D. 买入决策快照 — 记录关键因子的概要 ──
    # 详细因子值在 scenario 中逐只定义，此处记录触发了哪些决策
    snap["buy_signals"] = {}  # code -> 简要决策结果

    # ── E. 板块趋势 — 每 3 轮 ──
    sector_stats = getattr(w, '_sector_stats', {})
    snap["sectors"] = {}
    for ind, stats in sector_stats.items():
        snap["sectors"][ind] = {
            "change_pct": r2(stats.get("change_pct", 0)),
            "relative": r2(stats.get("relative", 0)),
            "up": stats.get("up", 0),
            "down": stats.get("down", 0),
            "breadth": r2(stats.get("breadth", 0)),
            "continuity": stats.get("continuity", 0),
            "vol_ratio": r2(stats.get("vol_ratio", 1.0)),
        }

    # ── F. 消息推送 ──
    # 由 sim_telegram 独立记录，此处不重复

    # ── G. Portfolio 总览 ──
    if portfolio:
        snap["portfolio"] = {
            "cash": r2(portfolio.cash),
            "total_value": r2(portfolio.total_value),
            "position_ratio": r2(portfolio.position_ratio),
            "daily_pnl": r2(portfolio.daily_pnl),
            "drawdown": r2(portfolio.drawdown),
            "position_count": len(portfolio.positions),
        }

    return snap


def save_snapshot(snap: dict, path: Path):
    """保存快照到 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)


def load_expected(path: Path) -> dict:
    """加载预期值 JSON 文件。"""
    if not path.exists():
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)
