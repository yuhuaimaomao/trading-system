"""
复盘服务

采集 12 个模块：行业板块、概念板块、个股行情、强势股、龙虎榜、涨跌停、监管函、大盘指数、股票异动、停复牌、股东增减持、隔夜宏观
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from system.utils.logger import get_task_logger, set_current_task

logger = get_task_logger("review")

from analysis.review.stats import CollectionStatsService
from data.collectors.events.cls_digest_collector import CLSDigestCollector
from data.collectors.events.lhb_collector import LHBCollector
from data.collectors.events.limit_pool_collector import LimitPoolCollector
from data.collectors.events.notice_collector import NoticeCollector
from data.collectors.events.regulatory_letter_collector import RegulatoryLetterCollector
from data.collectors.events.share_holder_change_collector import (
    ShareHolderChangeCollector,
)
from data.collectors.events.stock_monitor_collector import StockMonitorCollector
from data.collectors.events.strong_stock_collector import StrongStockCollector
from data.collectors.macro.macro_collector import MacroCollector
from data.collectors.market.concept_board_collector import ConceptBoardCollector
from data.collectors.market.industry_board_collector import IndustryBoardCollector
from data.collectors.market.main_index_collector import MainIndexCollector
from data.collectors.market.stock_basic_collector import StockBasicCollector
from data.collectors.market.suspend_resume_collector import SuspendResumeCollector
from system.config.settings import DATABASE_PATH, LOGS_DIR


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

    def collect(self):
        """
        采集复盘数据（11 个模块）
        """
        import time

        start_time = time.time()
        trade_date = datetime.now().strftime("%Y-%m-%d")

        # 确保所有采集器使用正确的交易日期
        for col in [
            self.industry_collector,
            self.concept_collector,
            self.stock_basic_collector,
            self.strong_stock_collector,
            self.suspend_resume_collector,
            self.share_holder_change_collector,
        ]:
            col.trade_date = trade_date

        logger.info("=" * 60)
        logger.info("开始采集复盘数据...")
        logger.info("=" * 60)

        def _collect_with_retry(name, collector):
            """代理采集器：失败后等 120s 重试一次，应对代理池整点刷新"""
            t0 = time.time()
            for attempt in (1, 2):
                try:
                    result = collector.fetch_and_save()
                except Exception as e:
                    logger.error(f"❌ {name}采集异常：{e}")
                    result = {"success": False, "count": 0, "total": 0, "data": []}
                if result.get("success") and result.get("count", 0) > 0:
                    logger.info(f"  ✅ {name}完成，耗时 {time.time() - t0:.1f}秒")
                    return result
                if attempt == 1:
                    logger.warning(f"  ⚠️ {name}第1次失败，等10s后重试...")
                    time.sleep(10)
            logger.error(f"  ❌ {name}重试后仍失败，跳过")
            return result

        # 采集行业板块
        logger.info("【1/13】采集行业板块...")
        industry_result = _collect_with_retry("行业板块", self.industry_collector)

        # 采集概念板块
        logger.info("【2/13】采集概念板块...")
        concept_result = _collect_with_retry("概念板块", self.concept_collector)

        # 采集个股行情
        logger.info("【3/13】采集个股行情...")
        stock_basic_result = _collect_with_retry("个股行情", self.stock_basic_collector)

        # 采集强势股
        logger.info("【4/13】采集强势股...")
        t0 = time.time()

        try:
            strong_stock_result = self.strong_stock_collector.fetch_and_save()
        except Exception as e:
            logger.error(f"❌ 强势股采集异常：{e}")
            strong_stock_result = {"success": False, "count": 0, "total": 0, "data": []}
        logger.info(f"  ✅ 强势股完成，耗时 {time.time() - t0:.1f}秒")

        # 采集龙虎榜
        logger.info("【5/13】采集龙虎榜...")
        t0 = time.time()

        try:
            lhb_result = self.lhb_collector.fetch_and_save(date=trade_date)
        except Exception as e:
            logger.error(f"❌ 龙虎榜采集异常：{e}")
            lhb_result = {"success": False, "count": 0, "total": 0, "data": []}
        logger.info(f"  ✅ 龙虎榜完成，耗时 {time.time() - t0:.1f}秒")

        # 采集涨跌停
        logger.info("【6/13】采集涨跌停...")
        t0 = time.time()

        try:
            limit_pool_result = self.limit_pool_collector.fetch_and_save(
                trade_date=trade_date
            )
        except Exception as e:
            logger.error(f"❌ 涨跌停采集异常：{e}")
            limit_pool_result = {"success": False, "count": 0, "total": 0, "data": []}
        logger.info(f"  ✅ 涨跌停完成，耗时 {time.time() - t0:.1f}秒")

        # 采集监管函
        logger.info("【7/13】采集监管函...")
        t0 = time.time()

        try:
            regulatory_letter_result = self.regulatory_letter_collector.fetch_and_save(
                trade_date=trade_date
            )
        except Exception as e:
            logger.error(f"❌ 监管函采集异常：{e}")
            regulatory_letter_result = {
                "success": False,
                "count": 0,
                "total": 0,
                "data": [],
            }
        logger.info(f"  ✅ 监管函完成，耗时 {time.time() - t0:.1f}秒")

        # 采集大盘指数
        logger.info("【8/13】采集大盘指数...")
        t0 = time.time()

        try:
            main_index_result = self.main_index_collector.fetch_and_save(
                trade_date=trade_date
            )
        except Exception as e:
            logger.error(f"❌ 大盘指数采集异常：{e}")
            main_index_result = {"success": False, "count": 0, "total": 0, "data": []}
        logger.info(f"  ✅ 大盘指数完成，耗时 {time.time() - t0:.1f}秒")

        # 采集股票异动
        logger.info("【9/13】采集股票异动...")
        t0 = time.time()

        try:
            stock_monitor_result = self.stock_monitor_collector.fetch_and_save(
                trade_date=trade_date
            )
        except Exception as e:
            logger.error(f"❌ 股票异动采集异常：{e}")
            stock_monitor_result = {
                "success": False,
                "count": 0,
                "total": 0,
                "data": [],
            }
        logger.info(f"  ✅ 股票异动完成，耗时 {time.time() - t0:.1f}秒")

        # 采集停复牌
        logger.info("【10/13】采集停复牌...")
        t0 = time.time()

        try:
            suspend_resume_result = self.suspend_resume_collector.fetch_and_save()
        except Exception as e:
            logger.error(f"❌ 停复牌采集异常：{e}")
            suspend_resume_result = {
                "success": False,
                "count": 0,
                "total": 0,
                "data": [],
            }
        logger.info(f"  ✅ 停复牌完成，耗时 {time.time() - t0:.1f}秒")

        # 采集股东增减持
        logger.info("【11/13】采集股东增减持...")
        t0 = time.time()

        try:
            share_holder_change_result = (
                self.share_holder_change_collector.fetch_and_save()
            )
        except Exception as e:
            logger.error(f"❌ 股东增减持采集异常：{e}")
            share_holder_change_result = {
                "success": False,
                "count": 0,
                "total": 0,
                "data": [],
            }
        logger.info(f"  ✅ 股东增减持完成，耗时 {time.time() - t0:.1f}秒")

        # 采集公告
        logger.info("【12/13】采集公告...")
        t0 = time.time()

        try:
            notice_result = self.notice_collector.fetch_and_save(trade_date=trade_date)
        except Exception as e:
            logger.error(f"❌ 公告采集异常：{e}")
            notice_result = {"success": False, "count": 0, "total": 0, "data": []}
        logger.info(f"  ✅ 公告采集完成，耗时 {time.time() - t0:.1f}秒")

        # 采集隔夜宏观
        logger.info("【13/15】采集隔夜宏观...")
        t0 = time.time()
        macro_result = {"success": False, "count": 0}
        try:
            macro_data = self.macro_collector.collect_all()
            self.macro_collector.save_to_db(macro_data, trade_date)
            macro_result = {"success": True, "count": 1}
            logger.info(f"  ✅ 隔夜宏观完成，耗时 {time.time() - t0:.1f}秒")
        except Exception as e:
            logger.error(f"  ❌ 隔夜宏观采集异常：{e}")

        # 采集 CLS 复盘新闻（焦点复盘 + 每日收评）并落盘
        logger.info("【14/15】采集 CLS 复盘新闻...")
        t0 = time.time()
        news_result = {"success": False, "sections": []}
        try:
            news_data = self.news_collector.collect_review()
            if news_data:
                news_dir = Path(LOGS_DIR) / trade_date / "collectors"
                news_dir.mkdir(parents=True, exist_ok=True)
                news_path = news_dir / "cls_digest.json"
                with open(news_path, "w", encoding="utf-8") as f:
                    json.dump(news_data, f, ensure_ascii=False, indent=2)
                sections = [k for k in news_data if news_data[k]]
                news_result = {"success": True, "sections": sections}
                logger.info(
                    f"  ✅ CLS 复盘新闻完成（{sections}），落盘 {news_path}，耗时 {time.time() - t0:.1f}秒"
                )
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
        self.stats_service.check_and_report(
            stats_data, trade_date=trade_date, send_to_telegram=True
        )

        # 更新板块信息表
        try:
            from data.processors.sector_info_processor import SectorInfoProcessor

            SectorInfoProcessor.run(trade_date, industry_result, concept_result)
        except Exception as e:
            logger.error(f"❌ sector_info 更新失败：{e}", exc_info=True)

        # 计算昨日涨停今日表现
        logger.info("\n【15/15】计算昨日涨停今日表现...")
        t0 = time.time()
        try:
            from data.processors.zt_performance_processor import ZTPerformanceProcessor

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
        from analysis.review.analyzer import ReviewAnalyzer

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
        from system.utils.telegram import MessageSender

        targets = []
        if group and TELEGRAM_REPORT_CHAT_ID:
            targets.append((TELEGRAM_REPORT_CHAT_ID, "群"))
        if TELEGRAM_PRIVATE_CHAT_ID:
            targets.append((TELEGRAM_PRIVATE_CHAT_ID, "私聊"))

        for chat_id, label in targets:
            logger.info(f"推送复盘报告到 Telegram {label} (chat_id={chat_id})...")
            try:
                sender = MessageSender(
                    chat_id=chat_id, bot_token=TELEGRAM_REPORT_BOT_TOKEN
                )
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
        conn = sqlite3.connect(str(DATABASE_PATH))
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
                from analysis.tracker import StockTracker
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
                msg = f"⚠️ {trade_date} 复盘数据不全，以下模块采集失败：{'、'.join(missing)}\n跳过 AI 复盘分析，请检查后重试。"
                logger.warning(msg)
                self.send(msg, group=False)
                return False

            # AI 分析（从 DB 读取 + 实时抓新闻）
            logger.info("\n【阶段 3/4】AI 分析")
            analysis, stock_pool = self.analyze()

            # 判断 AI 分析是否成功
            ai_success = bool(stock_pool) and not analysis.startswith("AI 分析失败")

            # 推送消息（AI 失败只发私聊不发群）
            logger.info("\n【阶段 4/4】消息推送")
            self.send(analysis, group=ai_success)

            # 记录复盘推荐的股票到追踪表（AI 成功后才有的数据）
            if stock_pool:
                try:
                    from analysis.tracker import StockTracker

                    tracker = StockTracker()
                    stocks = tracker.enrich_stock_pool(stock_pool)
                    if stocks:
                        tracker.record_stocks(
                            stocks, trade_date, analysis, source="复盘"
                        )
                        logger.info(f"✅ 已记录 {len(stocks)} 只复盘股票到追踪表")
                except Exception as e:
                    logger.error(f"⚠️ 股票池记录失败：{e}")
            else:
                logger.warning("⚠️ 复盘未解析到股票池，跳过记录")

            logger.info("=" * 70)
            logger.info("✅ 复盘执行完成")
            logger.info("=" * 70)

            # 自动核验旧预测（T-2 预测 vs T-1 实际，此时数据已齐全）
            try:
                from analysis.review.prediction_verifier import PredictionVerifier
                from system.config.trading_calendar import get_previous_trading_day

                prev_prev = get_previous_trading_day(
                    get_previous_trading_day(trade_date)
                )
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
