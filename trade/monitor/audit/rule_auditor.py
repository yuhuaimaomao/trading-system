# -*- coding: utf-8 -*-
"""RuleAuditor — 纯规则审计引擎，不做 AI 推理。

逐决策回溯验证：当时判断 vs 后续实际走势。
6 维度：市场模式 / 买入信号 / 止损 / 止盈 / 仓位 / 板块+共振
"""

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

    # ---- 数据查询 ----

    def _get_index_snapshots(self, trade_date: str) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT ts, price FROM index_snapshots WHERE trade_date=? ORDER BY ts",
            (trade_date,),
        ).fetchall()
        conn.close()
        return [{"ts": r[0], "price": r[1]} for r in rows]

    def _get_index_after(self, trade_date: str, after_ts: str, minutes: int = 30) -> list[dict]:
        from_ts = datetime.fromisoformat(after_ts)
        to_ts = from_ts + timedelta(minutes=minutes)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT ts, price FROM index_snapshots WHERE trade_date=? AND ts>=? AND ts<=? ORDER BY ts",
            (trade_date, from_ts.timestamp(), to_ts.timestamp()),
        ).fetchall()
        conn.close()
        return [{"ts": r[0], "price": r[1]} for r in rows]

    def _get_close(self, trade_date: str, code: str) -> float | None:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT close FROM stock_basic WHERE trade_date=? AND stock_code=?",
            (trade_date, code),
        ).fetchone()
        conn.close()
        return float(row[0]) if row and row[0] else None

    # ---- 维度 1: 市场模式 ----

    def _audit_regime(self, trade_date: str) -> list[dict]:
        findings = []
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM watcher_decision_log WHERE trade_date=? AND decision_type='regime_change' ORDER BY ts",
            (trade_date,),
        ).fetchall()
        conn.close()
        cols = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        for row in rows:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            pattern = data.get("pattern", "")
            ts = log["ts"]

            snaps = self._get_index_after(trade_date, ts, minutes=30)
            if len(snaps) < 5:
                continue

            start_p, end_p = snaps[0]["price"], snaps[-1]["price"]
            change = (end_p - start_p) / start_p * 100 if start_p else 0
            mid = len(snaps) // 2
            first_avg = sum(s["price"] for s in snaps[:mid]) / mid
            second_avg = sum(s["price"] for s in snaps[mid:]) / len(snaps[mid:]) if snaps[mid:] else end_p
            cg_shift = "down" if second_avg < first_avg else "up"

            result = self._eval_regime(pattern, change, cg_shift)
            if result:
                findings.append({
                    "trade_date": trade_date, "finding_type": "regime_misclass",
                    "severity": result["severity"], "stock_code": None,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": result["desc"],
                    "evidence": json.dumps({
                        "decision": data, "actual": {
                            "30min_change": round(change, 4), "cg_shift": cg_shift,
                            "start_price": start_p, "end_price": end_p,
                        }, "deviation": result["deviation"],
                    }, ensure_ascii=False),
                })
        return findings

    @staticmethod
    def _eval_regime(pattern: str, change: float, cg_shift: str) -> dict | None:
        if pattern == "one_sided":
            if cg_shift == "down" and change < -0.3:
                return None
            if cg_shift == "up" and change > 0.5:
                return {"severity": "P1", "desc": f"判 one_sided 但 30min 内反弹 {change:+.2f}%", "deviation": "opposite"}
            if abs(change) < 0.2:
                return {"severity": "P2", "desc": "判 one_sided 但后续横盘", "deviation": "unclear"}
        elif pattern == "v_reversal":
            if cg_shift == "up" and change > 0.3:
                return None
            if cg_shift == "down":
                return {"severity": "P1", "desc": f"判 v_reversal 但继续跌 {change:+.2f}%", "deviation": "opposite"}
        elif pattern == "dead_cat":
            if change < -0.2:
                return None
            if change > 0.5:
                return {"severity": "P1", "desc": f"判 dead_cat 但持续反弹 {change:+.2f}%", "deviation": "opposite"}
        elif pattern == "normal":
            if abs(change) < 0.8:
                return None
            if change < -1.5:
                return {"severity": "P0", "desc": f"判 normal 但 30min 内暴跌 {change:+.2f}%", "deviation": "opposite"}
        return None

    # ---- 维度 2: 买入信号 ----

    def _audit_buy_signals(self, trade_date: str) -> list[dict]:
        findings = []
        conn = sqlite3.connect(self.db_path)
        cols = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        # 买入触发
        triggers = conn.execute(
            "SELECT * FROM watcher_decision_log WHERE trade_date=? AND decision_type='buy_trigger'",
            (trade_date,),
        ).fetchall()
        for row in triggers:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            close = self._get_close(trade_date, code)
            if close is None:
                continue
            price = data.get("price", 0)
            pnl_pct = (close - price) / price * 100 if price else 0
            if pnl_pct < -3:
                findings.append({
                    "trade_date": trade_date, "finding_type": "buy_bad",
                    "severity": "P1", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"买入 {code} 当日亏损 {pnl_pct:+.2f}%（仓位 {data.get('position_size', 0)}）",
                    "evidence": json.dumps({"buy_price": price, "close": close,
                        "pnl_pct": round(pnl_pct, 2), "entry_rule": data.get("entry_rule")}, ensure_ascii=False),
                })

        # 买入过滤（反事实：如果买入会赚多少）
        filters = conn.execute(
            "SELECT * FROM watcher_decision_log WHERE trade_date=? AND decision_type='buy_filter'",
            (trade_date,),
        ).fetchall()
        for row in filters:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            close = self._get_close(trade_date, code)
            if close is None:
                continue
            price = data.get("price", 0)
            pnl_pct = (close - price) / price * 100 if price else 0
            if pnl_pct > 3:
                findings.append({
                    "trade_date": trade_date, "finding_type": "buy_missed",
                    "severity": "P1", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"过滤掉 {code} 但收盘涨 {pnl_pct:+.2f}%（{data.get('reason_filtered', '')}）",
                    "evidence": json.dumps({"filter_price": price, "close": close,
                        "pnl_pct": round(pnl_pct, 2), "reason_filtered": data.get("reason_filtered")}, ensure_ascii=False),
                })

        conn.close()
        return findings

    # ---- 维度 3: 止损 ----

    def _audit_stop_loss(self, trade_date: str) -> list[dict]:
        findings = []
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM watcher_decision_log WHERE trade_date=? AND decision_type='stop_trigger'",
            (trade_date,),
        ).fetchall()
        conn.close()
        cols = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        for row in rows:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            trigger_price = data.get("trigger_price", 0)
            ts = log["ts"]

            # 查后续30分钟该股走势（从 market_snapshots）
            from_ts = datetime.fromisoformat(ts)
            to_ts = from_ts + timedelta(minutes=30)
            conn2 = sqlite3.connect(self.db_path)
            snaps = conn2.execute(
                "SELECT price FROM market_snapshots WHERE trade_date=? AND code=? AND ts>=? AND ts<=? ORDER BY ts",
                (trade_date, code, from_ts.timestamp(), to_ts.timestamp()),
            ).fetchall()
            conn2.close()

            if len(snaps) < 3:
                continue
            prices = [s[0] for s in snaps]
            post_low, post_high = min(prices), max(prices)
            rebound_pct = (post_high - trigger_price) / trigger_price * 100 if trigger_price else 0

            if rebound_pct > 2:
                findings.append({
                    "trade_date": trade_date, "finding_type": "stop_early",
                    "severity": "P1", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"{code} 止损触发后反弹 {rebound_pct:+.2f}%，可能过早止损",
                    "evidence": json.dumps({"trigger_price": trigger_price, "post_low": post_low,
                        "post_high": post_high, "rebound_pct": round(rebound_pct, 2)}, ensure_ascii=False),
                })
            elif post_low < trigger_price * 0.97:
                drop = abs((post_low - trigger_price) / trigger_price * 100)
                findings.append({
                    "trade_date": trade_date, "finding_type": "stop_late",
                    "severity": "P1", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"{code} 止损后继续跌 {drop:.1f}%，止损设太宽",
                    "evidence": json.dumps({"trigger_price": trigger_price, "post_low": post_low,
                        "further_drop": round(drop, 2)}, ensure_ascii=False),
                })
        return findings

    # ---- 维度 4: 止盈 ----

    def _audit_take_profit(self, trade_date: str) -> list[dict]:
        findings = []
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM watcher_decision_log WHERE trade_date=? AND decision_type='tp_trigger'",
            (trade_date,),
        ).fetchall()
        conn.close()
        cols = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        for row in rows:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            trigger_price = data.get("trigger_price", 0)
            ts = log["ts"]

            from_ts = datetime.fromisoformat(ts)
            to_ts = from_ts + timedelta(minutes=30)
            conn2 = sqlite3.connect(self.db_path)
            snaps = conn2.execute(
                "SELECT price FROM market_snapshots WHERE trade_date=? AND code=? AND ts>=? AND ts<=? ORDER BY ts",
                (trade_date, code, from_ts.timestamp(), to_ts.timestamp()),
            ).fetchall()
            conn2.close()

            if len(snaps) < 3:
                continue
            post_high = max(s[0] for s in snaps)
            further_up = (post_high - trigger_price) / trigger_price * 100 if trigger_price else 0

            if further_up > 2:
                findings.append({
                    "trade_date": trade_date, "finding_type": "tp_early",
                    "severity": "P2", "stock_code": code,
                    "decision_log_ids": json.dumps([log["id"]]),
                    "pattern_desc": f"{code} 止盈后继续涨 {further_up:+.2f}%，可能卖飞了",
                    "evidence": json.dumps({"trigger_price": trigger_price,
                        "post_high": post_high, "further_up": round(further_up, 2)}, ensure_ascii=False),
                })
        return findings

    # ---- 维度 5: 仓位 ----

    def _audit_position_size(self, trade_date: str) -> list[dict]:
        findings = []
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM watcher_decision_log WHERE trade_date=? AND decision_type='buy_trigger'",
            (trade_date,),
        ).fetchall()
        conn.close()
        cols = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        entries = []
        for row in rows:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            code = log["stock_code"]
            close = self._get_close(trade_date, code)
            if close is None:
                continue
            price = data.get("price", 0)
            size = data.get("position_size", 0)
            pnl_pct = (close - price) / price * 100 if price else 0
            entries.append({"code": code, "size": size, "pnl_pct": pnl_pct, "id": log["id"]})

        if len(entries) < 3:
            return findings

        entries.sort(key=lambda x: x["size"])
        n = len(entries)
        small = entries[:n // 3]
        large = entries[2 * n // 3:]
        large_avg = sum(e["pnl_pct"] for e in large) / len(large)
        small_avg = sum(e["pnl_pct"] for e in small) / len(small)

        if small_avg > large_avg + 2:
            findings.append({
                "trade_date": trade_date, "finding_type": "size_mismatch",
                "severity": "P2", "stock_code": None,
                "decision_log_ids": json.dumps([e["id"] for e in entries]),
                "pattern_desc": f"小仓位组均盈利 {small_avg:+.2f}% > 大仓位组 {large_avg:+.2f}%，分配方向可能反了",
                "evidence": json.dumps({"small_avg": round(small_avg, 2),
                    "large_avg": round(large_avg, 2)}, ensure_ascii=False),
            })
        return findings

    # ---- 维度 6: 板块 + 共振 ----

    def _audit_sector(self, trade_date: str) -> list[dict]:
        findings = []
        conn = sqlite3.connect(self.db_path)
        cols = ["id", "trade_date", "ts", "decision_type", "stock_code", "decision_data", "created_at"]

        # 共振告警审计
        rows = conn.execute(
            "SELECT * FROM watcher_decision_log WHERE trade_date=? AND decision_type='resonance_alert' ORDER BY ts",
            (trade_date,),
        ).fetchall()
        for row in rows:
            log = dict(zip(cols, row))
            data = json.loads(log["decision_data"])
            ts = log["ts"]

            # 取30分钟后 sector_snapshots 作为"实况"对比
            from_dt = datetime.fromisoformat(ts)
            to_dt = from_dt + timedelta(minutes=30)
            conn2 = sqlite3.connect(self.db_path)
            end_rows = conn2.execute(
                "SELECT sector_name, avg_change FROM sector_snapshots WHERE trade_date=? AND ts>=? AND ts<=? ORDER BY ts DESC LIMIT 20",
                (trade_date, from_dt.strftime("%Y-%m-%dT%H:%M:%S"), to_dt.strftime("%Y-%m-%dT%H:%M:%S")),
            ).fetchall()
            conn2.close()

            if not end_rows:
                continue
            sector_end = {r[0]: r[1] for r in end_rows}

            # 检查盘中判为逆势走强的板块，30分钟后是否真的比大盘强
            for name, sector_chg in data.get("counter_up", []):
                end_chg = sector_end.get(name)
                if end_chg is not None and end_chg < 0:
                    findings.append({
                        "trade_date": trade_date, "finding_type": "sector_misjudge",
                        "severity": "P2", "stock_code": None,
                        "decision_log_ids": json.dumps([log["id"]]),
                        "pattern_desc": f"盘中判 {name} 逆势走强，但30min后已转跌 {end_chg:+.2f}%",
                        "evidence": json.dumps({"name": name, "at_alert": sector_chg,
                            "30min_later": end_chg}, ensure_ascii=False),
                    })

            for name, sector_chg in data.get("resonance_down", []):
                end_chg = sector_end.get(name)
                if end_chg is not None and end_chg > 0.5:
                    findings.append({
                        "trade_date": trade_date, "finding_type": "sector_misjudge",
                        "severity": "P2", "stock_code": None,
                        "decision_log_ids": json.dumps([log["id"]]),
                        "pattern_desc": f"盘中判 {name} 共振下行，但30min后已回升 {end_chg:+.2f}%",
                        "evidence": json.dumps({"name": name, "at_alert": sector_chg,
                            "30min_later": end_chg}, ensure_ascii=False),
                    })

        conn.close()
        return findings
