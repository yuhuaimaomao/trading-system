"""
复盘服务

采集 12 个模块：行业板块、概念板块、个股行情、强势股、龙虎榜、涨跌停、
监管函、大盘指数、股票异动、停复牌、股东增减持、隔夜宏观
"""

import json
import subprocess
import sys
from datetime import datetime

from data._base import connect
from data.collect.events.cls_digest_collector import CLSDigestCollector
from data.collect.events.lhb_collector import LHBCollector
from data.collect.events.limit_pool_collector import LimitPoolCollector
from data.collect.events.notice_collector import NoticeCollector
from data.collect.events.regulatory_letter_collector import RegulatoryLetterCollector
from data.collect.events.share_holder_change_collector import (
    ShareHolderChangeCollector,
)
from data.collect.events.stock_monitor_collector import StockMonitorCollector
from data.collect.events.strong_stock_collector import StrongStockCollector
from data.collect.macro.macro_collector import MacroCollector
from data.collect.market.concept_board_collector import ConceptBoardCollector
from data.collect.market.industry_board_collector import IndustryBoardCollector
from data.collect.market.main_index_collector import MainIndexCollector
from data.collect.market.stock_basic_collector import StockBasicCollector
from data.collect.market.suspend_resume_collector import SuspendResumeCollector
from review.review_stats import CollectionStatsService
from system.config.settings import DATABASE_PATH, PROJECT_ROOT
from system.utils.logger import get_task_logger, set_current_task

logger = get_task_logger("review")


class ReviewService:
    """复盘服务"""

    def __init__(self):
        self.industry_collector = IndustryBoardCollector()
        self.concept_collector = ConceptBoardCollector()
        self.stock_basic_collector = StockBasicCollector()
        self.strong_stock_collector = StrongStockCollector()
        self.lhb_collector = LHBCollector()
        self.limit_pool_collector = LimitPoolCollector()
        self.regulatory_letter_collector = RegulatoryLetterCollector()
        self.main_index_collector = MainIndexCollector()
        self.stock_monitor_collector = StockMonitorCollector()
        self.suspend_resume_collector = SuspendResumeCollector()
        self.share_holder_change_collector = ShareHolderChangeCollector()
        self.notice_collector = NoticeCollector()
        self.news_collector = CLSDigestCollector()
        self.macro_collector = MacroCollector()

        self.stats_service = CollectionStatsService()

        logger.info("ReviewService 初始化完成（14 个采集器）")

    @staticmethod
    def _save_review_signals(stock_pool: list, review_date: str) -> int:
        """将复盘 STOCKS JSON 转为 REVIEW 信号写入 trade_signals。

        盯盘管线只从 trade_signals 读 REVIEW 信号，不再查 stock_tracker。

        信号 trade_date 使用下一个交易日——今日复盘得出的买入建议，明天开盘执行。
        """
        import re

        from data.repo import TradeRepository
        from system.config.trading_calendar import get_next_trading_day

        signal_date = get_next_trading_day(review_date)
        repo = TradeRepository()
        # 先清理目标日期旧 REVIEW 信号（复盘可能重跑）
        repo.expire_old_pending_signals(signal_date)

        saved = 0
        for s in stock_pool:
            code = s.get("股票代码", "")
            name = s.get("股票名称", "")
            if not code:
                continue

            buy_cond = s.get("买入条件", "")
            sl = s.get("止损位", "")
            tp = s.get("目标位", "")

            # 从买入条件提取参考价格
            buy_min, buy_max = None, None
            ma_match = re.search(r"约(\d+\.?\d*)", buy_cond)
            if ma_match:
                ref = float(ma_match.group(1))
                buy_min = round(ref * 0.99, 2)
                buy_max = round(ref * 1.02, 2)
            elif sl:
                sl_val = float(sl)
                buy_min = round(sl_val * 1.02, 2)
                buy_max = round(sl_val * 1.06, 2)

            # 止损/止盈缺省时从技术指标补算
            sl_val = float(sl) if sl else 0
            tp_val = float(tp) if tp else 0
            if sl_val <= 0 or tp_val <= 0:
                calc_sl, calc_tp = ReviewService._calc_fallback_sl_tp(code, review_date)
                if sl_val <= 0:
                    sl_val = calc_sl
                if tp_val <= 0:
                    tp_val = calc_tp

            signal_dict = {
                "trade_date": signal_date,
                "created_at": datetime.now().isoformat(),
                "signal_type": "BUY",
                "signal_source": "REVIEW",
                "stock_code": code,
                "stock_name": name,
                "buy_zone_min": buy_min,
                "buy_zone_max": buy_max,
                "target_position": 0.05,  # REVIEW_PICK_POSITION_PCT
                "stop_loss": sl_val if sl_val > 0 else None,
                "take_profit": tp_val if tp_val > 0 else None,
                "trailing_stop": 0.03,
                "signal_score": 70,
                "strategy_name": "review_trend_pick",
                "reason": buy_cond[:80] if buy_cond else "",
                "status": "pending",
                "account": "paper",
            }
            if repo.insert_signal(signal_dict):
                saved += 1

        logger.info(f"✅ REVIEW 信号入库: {saved}/{len(stock_pool)} 只")
        return saved

    @staticmethod
    def _calc_fallback_sl_tp(code: str, trade_date: str) -> tuple:
        """AI 未给止损/止盈时，从技术指标自动补算。"""
        from data.repo import TradeRepository

        sl, tp = 0.0, 0.0
        try:
            repo = TradeRepository()
            price = repo.get_stock_price(code, trade_date) or 0
            if price > 0:
                sr = repo.get_support_resistance(code, price)
                for sup in sr.get("supports", []):
                    sl = round(sup[0] * 0.99, 2)
                    break
                for res in sr.get("resistances", []):
                    tp = round(res[0], 2)
                    break
        except Exception:
            pass
        if sl <= 0:
            sl = round(price * 0.93, 2) if price > 0 else 0
        if tp <= 0:
            tp = round(price * 1.10, 2) if price > 0 else 0
        return sl, tp

    @staticmethod
    def _run_in_subprocess(module_path, class_name, method, trade_date, method_kwargs=None, timeout=900):
        """在独立子进程中执行采集器，返回 {success, count, total, error?}

        子进程退出后 OS 强制回收该进程的所有 fd（包括 TIME_WAIT socket），
        主进程 fd 数不增长。
        """
        # 构建方法调用参数
        kw_lines = ""
        if method_kwargs:
            kw_parts = [f"{k}={json.dumps(v)}" for k, v in method_kwargs.items()]
            kw_lines = ", ".join(kw_parts)

        code = f"""
import json, sys
sys.path.insert(0, {json.dumps(str(PROJECT_ROOT))})
from {module_path} import {class_name}
c = {class_name}()
c.trade_date = {json.dumps(trade_date)}
try:
    result = c.{method}({kw_lines})
    if isinstance(result, dict):
        stats = {{"success": result.get("success", False), "count": result.get("count", 0), "total": result.get("total", 0)}}
    elif isinstance(result, list):
        stats = {{"success": True, "count": len(result), "total": len(result)}}
    else:
        stats = {{"success": bool(result), "count": 0, "total": 0}}
except Exception as e:
    stats = {{"success": False, "count": 0, "total": 0, "error": str(e)}}
print(json.dumps(stats))
"""

        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )

        if proc.returncode != 0:
            return {"success": False, "count": 0, "total": 0, "error": proc.stderr[:500]}

        try:
            return json.loads(proc.stdout.strip())
        except json.JSONDecodeError:
            return {"success": False, "count": 0, "total": 0, "error": f"JSON解析失败: {proc.stdout[:200]}"}

    def collect(self):
        """
        采集复盘数据（14 个模块）

        网络密集型采集器在独立子进程中执行：子进程退出时 OS 强制回收所有 fd，
        保证主进程 fd 数稳定，避免 [Errno 24] Too many open files。
        """
        import time

        start_time = time.time()
        trade_date = datetime.now().strftime("%Y-%m-%d")

        logger.info("=" * 60)
        logger.info("开始采集复盘数据...")
        logger.info("=" * 60)

        def _collect_network(name, module_path, class_name, method="fetch_and_save", method_kwargs=None, timeout=900):
            """网络采集器：子进程执行 + 失败后等 10s 重试一次"""
            t0 = time.time()
            for attempt in (1, 2):
                result = ReviewService._run_in_subprocess(
                    module_path,
                    class_name,
                    method,
                    trade_date,
                    method_kwargs=method_kwargs,
                    timeout=timeout,
                )
                if result.get("success") and result.get("count", 0) > 0:
                    logger.info(f"  ✅ {name}完成，耗时 {time.time() - t0:.1f}秒")
                    return result
                err = result.get("error", "")
                if attempt == 1:
                    logger.warning(f"  ⚠️ {name}第1次失败（{err}），等10s后重试...")
                    time.sleep(10)
            logger.error(f"  ❌ {name}重试后仍失败，跳过")
            return result

        # 采集行业板块
        logger.info("【1/13】采集行业板块...")
        industry_result = _collect_network(
            "行业板块",
            "data.collect.market.industry_board_collector",
            "IndustryBoardCollector",
        )

        # 采集概念板块
        logger.info("【2/13】采集概念板块...")
        concept_result = _collect_network(
            "概念板块",
            "data.collect.market.concept_board_collector",
            "ConceptBoardCollector",
        )

        # 采集个股行情
        logger.info("【3/13】采集个股行情...")
        stock_basic_result = _collect_network(
            "个股行情",
            "data.collect.market.stock_basic_collector",
            "StockBasicCollector",
            timeout=900,
        )

        # 采集强势股
        logger.info("【4/13】采集强势股...")
        strong_stock_result = _collect_network(
            "强势股",
            "data.collect.events.strong_stock_collector",
            "StrongStockCollector",
        )

        # 采集龙虎榜
        logger.info("【5/13】采集龙虎榜...")
        lhb_result = _collect_network(
            "龙虎榜",
            "data.collect.events.lhb_collector",
            "LHBCollector",
            method_kwargs={"date": trade_date},
        )

        # 采集涨跌停
        logger.info("【6/13】采集涨跌停...")
        limit_pool_result = _collect_network(
            "涨跌停",
            "data.collect.events.limit_pool_collector",
            "LimitPoolCollector",
            method_kwargs={"trade_date": trade_date},
        )

        # 采集监管函
        logger.info("【7/13】采集监管函...")
        regulatory_letter_result = _collect_network(
            "监管函",
            "data.collect.events.regulatory_letter_collector",
            "RegulatoryLetterCollector",
            method_kwargs={"trade_date": trade_date},
        )

        # 采集大盘指数
        logger.info("【8/13】采集大盘指数...")
        main_index_result = _collect_network(
            "大盘指数",
            "data.collect.market.main_index_collector",
            "MainIndexCollector",
            method_kwargs={"trade_date": trade_date},
        )

        # 采集股票异动
        logger.info("【9/13】采集股票异动...")
        stock_monitor_result = _collect_network(
            "股票异动",
            "data.collect.events.stock_monitor_collector",
            "StockMonitorCollector",
            method_kwargs={"trade_date": trade_date},
        )

        # 采集停复牌
        logger.info("【10/13】采集停复牌...")
        suspend_resume_result = _collect_network(
            "停复牌",
            "data.collect.market.suspend_resume_collector",
            "SuspendResumeCollector",
        )

        # 采集股东增减持
        logger.info("【11/13】采集股东增减持...")
        share_holder_change_result = _collect_network(
            "股东增减持",
            "data.collect.events.share_holder_change_collector",
            "ShareHolderChangeCollector",
        )

        # 采集公告
        logger.info("【12/13】采集公告...")
        notice_result = _collect_network(
            "公告",
            "data.collect.events.notice_collector",
            "NoticeCollector",
            method_kwargs={"trade_date": trade_date},
        )

        # 采集隔夜宏观（两步操作：collect_all + save_to_db，在子进程中连续执行）
        logger.info("【13/15】采集隔夜宏观...")
        t0 = time.time()
        macro_code = f"""
import json, sys
sys.path.insert(0, {json.dumps(str(PROJECT_ROOT))})
from data.collect.macro.macro_collector import MacroCollector
c = MacroCollector()
c.trade_date = {json.dumps(trade_date)}
try:
    data = c.collect_all()
    c.save_to_db(data, {json.dumps(trade_date)})
    print(json.dumps({{"success": True, "count": 1, "total": 1}}))
except Exception as e:
    print(json.dumps({{"success": False, "count": 0, "total": 0, "error": str(e)}}))
"""
        macro_proc = subprocess.run(
            [sys.executable, "-c", macro_code],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        if macro_proc.returncode == 0:
            macro_result = json.loads(macro_proc.stdout.strip())
        else:
            macro_result = {"success": False, "count": 0, "error": macro_proc.stderr[:200]}
        if macro_result.get("success"):
            logger.info(f"  ✅ 隔夜宏观完成，耗时 {time.time() - t0:.1f}秒")
        else:
            logger.error(f"  ❌ 隔夜宏观采集异常：{macro_result.get('error', '')}")

        # 采集 CLS 复盘新闻（焦点复盘 + 每日收评）并落盘
        logger.info("【14/15】采集 CLS 复盘新闻...")
        t0 = time.time()
        news_result = {"success": False, "sections": []}
        try:
            cls_code = f"""
import json, sys
from pathlib import Path
sys.path.insert(0, {json.dumps(str(PROJECT_ROOT))})
from system.config.settings import LOGS_DIR
from data.collect.events.cls_digest_collector import CLSDigestCollector
c = CLSDigestCollector()
try:
    news_data = c.collect_review()
    if news_data:
        news_dir = Path(LOGS_DIR) / {json.dumps(trade_date)} / "collectors"
        news_dir.mkdir(parents=True, exist_ok=True)
        news_path = news_dir / "cls_digest.json"
        with open(str(news_path), "w", encoding="utf-8") as f:
            json.dump(news_data, f, ensure_ascii=False, indent=2)
        sections = [k for k in news_data if news_data[k]]
        print(json.dumps({{"success": True, "sections": sections}}))
    else:
        print(json.dumps({{"success": False, "sections": []}}))
except Exception as e:
    print(json.dumps({{"success": False, "sections": [], "error": str(e)}}))
"""
            cls_proc = subprocess.run(
                [sys.executable, "-c", cls_code],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(PROJECT_ROOT),
            )
            if cls_proc.returncode == 0:
                news_result = json.loads(cls_proc.stdout.strip())
            sections = news_result.get("sections", [])
            if news_result.get("success"):
                logger.info(f"  ✅ CLS 复盘新闻完成（{sections}），耗时 {time.time() - t0:.1f}秒")
            else:
                logger.warning(f"  ⚠️ CLS 复盘新闻为空，耗时 {time.time() - t0:.1f}秒")
        except Exception as e:
            logger.error(f"  ❌ CLS 复盘新闻采集异常：{e}")

        # 准备返回数据（传递完整结果给统计服务）
        stats_data = {
            "industry": industry_result,
            "concept": concept_result,
            "stock_basic": stock_basic_result,
            "strong_stock": strong_stock_result,
            "lhb": lhb_result,
            "limit_pool": limit_pool_result,
            "regulatory_letter": regulatory_letter_result,
            "main_index": main_index_result,
            "stock_monitor": stock_monitor_result,
            "suspend_resume": suspend_resume_result,
            "share_holder_change": share_holder_change_result,
            "notice": notice_result,
            "macro": macro_result,
            "cls_news": news_result,
        }

        # 生成并推送统计报告
        self.stats_service.check_and_report(stats_data, trade_date=trade_date, send_to_telegram=True)

        # 更新板块信息表
        try:
            from data.process.sector_info_processor import SectorInfoProcessor

            SectorInfoProcessor.run(trade_date, industry_result, concept_result)
        except Exception as e:
            logger.error(f"❌ sector_info 更新失败：{e}", exc_info=True)

        # 计算昨日涨停今日表现
        logger.info("\n【15/15】计算昨日涨停今日表现...")
        t0 = time.time()
        try:
            from data.process.zt_performance_processor import ZTPerformanceProcessor

            ZTPerformanceProcessor.run(trade_date)
        except Exception as e:
            logger.error(f"  ❌ 昨日涨停表现计算失败：{e}")
        logger.info(f"  ✅ 昨日涨停表现完成，耗时 {time.time() - t0:.1f}秒")

        # CLS 复盘新闻已在采集阶段落盘（模块 13），电报仍由 ReviewAnalyzer 从 DB 查询

        total_time = time.time() - start_time
        logger.info("=" * 60)
        logger.info(f"✅ 数据采集完成，总耗时：{total_time:.1f}秒")
        logger.info("=" * 60)

    def analyze(self) -> tuple:
        """AI 分析 — ReviewAnalyzer 从 DB 读数据 + 实时抓新闻"""
        from review.review_analyzer import ReviewAnalyzer

        logger.info("开始 AI 分析 v3.0（刺客风格）...")
        report, stock_pool = ReviewAnalyzer().generate()
        return report, stock_pool

    def send(self, message: str, group: bool = True) -> None:
        """推送消息到 Telegram，默认群+私聊双推，group=False 时只发私聊"""
        from system.config.settings import (
            TELEGRAM_PRIVATE_CHAT_ID,
            TELEGRAM_REPORT_BOT_TOKEN,
            TELEGRAM_REPORT_CHAT_ID,
        )
        from system.message import MessageSender

        targets = []
        if group and TELEGRAM_REPORT_CHAT_ID:
            targets.append((TELEGRAM_REPORT_CHAT_ID, "群"))
        if TELEGRAM_PRIVATE_CHAT_ID:
            targets.append((TELEGRAM_PRIVATE_CHAT_ID, "私聊"))

        for chat_id, label in targets:
            logger.info(f"推送复盘报告到 Telegram {label} (chat_id={chat_id})...")
            try:
                sender = MessageSender(chat_id=chat_id, bot_token=TELEGRAM_REPORT_BOT_TOKEN)
                sender.send(message)
                logger.info(f"✅ 复盘报告推送成功 ({label})")
            except Exception as e:
                logger.error(f"❌ 推送失败 ({label})：{e}", exc_info=True)

    def _check_data_complete(self, trade_date: str) -> list:
        """检查 AI 分析所需的 DB 数据是否齐全，返回缺失的数据项列表"""
        missing = []
        checks = {
            "个股行情": (
                "SELECT COUNT(*) FROM stock_basic WHERE trade_date = ?",
                "stock_basic",
            ),
            "行业板块": (
                "SELECT COUNT(*) FROM sector_industry WHERE trade_date = ?",
                "sector_industry",
            ),
            "概念板块": (
                "SELECT COUNT(*) FROM sector_concept WHERE trade_date = ?",
                "sector_concept",
            ),
        }
        conn = connect(DATABASE_PATH)
        try:
            for label, (sql, _) in checks.items():
                cursor = conn.execute(sql, (trade_date,))
                cnt = cursor.fetchone()[0]
                if cnt == 0:
                    missing.append(label)
        finally:
            conn.close()
        return missing

    def generate_and_send(self, analyze_only: bool = False) -> bool:
        """完整流程：采集 → 分析 → 推送 → 补充 Excel，返回 AI 分析是否成功"""
        set_current_task("review")
        logger.info("=" * 70)
        tag = "分析" if analyze_only else "盘后复盘报告"
        logger.info(f"🍎 股票量化系统 - {tag}")
        logger.info("=" * 70)

        try:
            if analyze_only:
                logger.info("\n【跳过采集】仅执行分析")
            else:
                logger.info("\n【阶段 1/4】数据采集")
                self.collect()

            trade_date = datetime.now().strftime("%Y-%m-%d")

            # 校验数据完整性
            missing = self._check_data_complete(trade_date)

            # 股票追踪更新：无论数据是否完整都要跑，补上之前遗漏的 t_open
            logger.info("\n【阶段 2/4】股票追踪数据更新")
            try:
                from review.stock_tracker import StockTracker
                from system.config.trading_calendar import get_previous_trading_day

                tracker = StockTracker()
                tracker.update_daily_data(trade_date)
                logger.info("✅ 当日行情数据已补充")

                yesterday = get_previous_trading_day(trade_date)
                tracker.update_next_day_data(yesterday, trade_date)
                logger.info("✅ 次日表现数据已补充")
            except Exception as e:
                logger.error(f"⚠️ 股票追踪数据补充失败：{e}")

            if missing:
                msg = (
                    f"⚠️ {trade_date} 复盘数据不全，以下模块采集失败：{'、'.join(missing)}"
                    "\n跳过 AI 复盘分析，请检查后重试。"
                )
                logger.warning(msg)
                self.send(msg, group=False)
                return False

            # AI 分析（从 DB 读取 + 实时抓新闻）
            logger.info("\n【阶段 3/4】AI 分析")
            analysis, stock_pool = self.analyze()

            # Batch 模式：已提交异步处理，子进程负责推送+记录，主进程只需保证策略继续跑
            is_batch = analysis.startswith("报告正在通过批处理生成")
            if is_batch:
                logger.info("📤 Batch 已提交，跳过推送（子进程稍后推送）")
                logger.info("=" * 70)
                logger.info("✅ 复盘主流程完成（Batch 异步模式）")
                logger.info("=" * 70)
                return True

            # 判断 AI 分析是否成功
            ai_success = bool(stock_pool) and not analysis.startswith("AI 分析失败")

            # 推送消息（AI 失败只发私聊不发群）
            logger.info("\n【阶段 4/4】消息推送")
            self.send(analysis, group=ai_success)

            # 记录复盘推荐的股票到追踪表（AI 成功后才有的数据）
            if stock_pool:
                try:
                    from review.stock_tracker import StockTracker

                    tracker = StockTracker()
                    stocks = tracker.enrich_stock_pool(stock_pool)
                    if stocks:
                        tracker.record_stocks(stocks, trade_date, analysis, source="复盘")
                        logger.info(f"✅ 已记录 {len(stocks)} 只复盘股票到追踪表")
                except Exception as e:
                    logger.error(f"⚠️ 股票池记录失败：{e}")

                # 同步写入 trade_signals（REVIEW 信号）供盯盘管线使用
                try:
                    ReviewService._save_review_signals(stock_pool, trade_date)
                except Exception as e:
                    logger.error(f"⚠️ REVIEW 信号入库失败：{e}")
            else:
                logger.warning("⚠️ 复盘未解析到股票池，跳过记录")

            logger.info("=" * 70)
            logger.info("✅ 复盘执行完成")
            logger.info("=" * 70)

            # 自动核验旧预测（T-2 预测 vs T-1 实际，此时数据已齐全）
            try:
                from review.prediction_verifier import PredictionVerifier
                from system.config.trading_calendar import get_previous_trading_day

                prev_prev = get_previous_trading_day(get_previous_trading_day(trade_date))
                if prev_prev:
                    PredictionVerifier().verify(prev_prev)
                    logger.info("✅ 历史预测自动核验完成")
            except Exception as e:
                logger.warning(f"预测自动核验失败（不影响主流程）: {e}")

            return ai_success

        except Exception as e:
            logger.error("=" * 70)
            logger.error(f"❌ 复盘执行失败：{e}")
            logger.error("=" * 70)
            return False
            raise
