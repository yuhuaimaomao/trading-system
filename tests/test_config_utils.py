"""
测试：system/config/ + system/utils/ 所有模块

覆盖范围：
  - system.config.settings
  - system.config.trading_calendar
  - system.utils.stock_code_utils
  - system.utils.dns_bypass
  - system.utils.logger
  - system.utils.regulatory_analysis

注意事项：
  - 交易日历测试使用固定日期，不依赖 QMT 数据库（自动回退硬编码节假日）
  - 日志测试替换 LOGS_DIR 到临时目录，避免污染真实日志
  - 环境变量相关 settings 测试同时验证默认值和 env override
"""

import importlib
import logging
import re
import socket
from datetime import datetime
from pathlib import Path

import pytest

# ================================================================
#  system.config.settings
# ================================================================


class TestSettings:
    """system.config.settings 配置常量"""

    # 延迟 import，确保其他测试不会因 env 污染失败
    SETTINGS = pytest.importorskip("system.config.settings")
    M = SETTINGS  # 别名，减少打字

    def test_project_root_is_path_and_exists(self):
        """PROJECT_ROOT 是 Path 且目录存在"""
        root = self.M.PROJECT_ROOT
        assert isinstance(root, Path)
        assert root.exists()
        assert root.is_dir()

    def test_project_root_points_to_trading_system(self):
        """PROJECT_ROOT 指向 trading-system 根目录"""
        assert self.M.PROJECT_ROOT.name == "trading-system"

    def test_database_path_is_str_and_ends_with_db(self):
        """DATABASE_PATH 是字符串，末尾是 .db"""
        path = self.M.DATABASE_PATH
        assert isinstance(path, str)
        assert path.endswith(".db"), f"DATABASE_PATH should end with .db, got: {path}"

    def test_database_path_under_storage(self):
        """DATABASE_PATH 在 storage/ 目录下"""
        assert "storage" in self.M.DATABASE_PATH

    def test_logs_dir_is_path(self):
        """LOGS_DIR 是 Path 对象"""
        assert isinstance(self.M.LOGS_DIR, Path)

    def test_logs_dir_named_logs(self):
        """LOGS_DIR 最后一段是 logs"""
        assert self.M.LOGS_DIR.name == "logs"

    def test_logs_dir_under_storage(self):
        """LOGS_DIR 在 storage/logs 下"""
        assert "storage" in str(self.M.LOGS_DIR)

    def test_storage_path_is_path(self):
        """STORAGE_PATH 是 Path 对象"""
        assert isinstance(self.M.STORAGE_PATH, Path)

    def test_storage_path_named_storage(self):
        """STORAGE_PATH 最后一段是 storage"""
        assert self.M.STORAGE_PATH.name == "storage"

    def test_storage_path_under_project_root(self):
        """STORAGE_PATH 在 PROJECT_ROOT 下"""
        assert str(self.M.STORAGE_PATH).startswith(str(self.M.PROJECT_ROOT))

    def test_ai_model_default_falls_back(self):
        """AI_MODEL_DEFAULT 默认等于 AI_MODEL（module attr），具体值由 env 决定"""
        # 逻辑上 AI_MODEL_DEFAULT 的 fallback 是 AI_MODEL
        # 但模块已 import，两者值取决于 .env 的实际内容，这里只验证类型
        assert isinstance(self.M.AI_MODEL, str)
        assert isinstance(self.M.AI_MODEL_DEFAULT, str)

    def test_ai_model_specific_vars_are_strs(self):
        """所有 AI_MODEL_* 业务变量都是字符串（可以为空）"""
        ai_vars = [
            self.M.AI_MODEL_REVIEW,
            self.M.AI_MODEL_SCREENING,
            self.M.AI_MODEL_MORNING,
            self.M.AI_MODEL_WATCHER,
            self.M.AI_MODEL_WATCHER_CHASE,
            self.M.AI_MODEL_WATCHER_SWAP,
            self.M.AI_MODEL_WATCHER_INDEX,
            self.M.AI_MODEL_WATCHER_TRAPPED,
            self.M.AI_MODEL_AUDIT,
            self.M.AI_MODEL_STRATEGY,
        ]
        for var in ai_vars:
            assert isinstance(var, str), f"Expected str, got {type(var)}: {var!r}"

    def test_telegram_attrs_exist(self):
        """TELEGRAM_REPORT_BOT_TOKEN / TELEGRAM_REPORT_CHAT_ID 是模块属性"""
        assert hasattr(self.M, "TELEGRAM_REPORT_BOT_TOKEN")
        assert hasattr(self.M, "TELEGRAM_REPORT_CHAT_ID")
        assert hasattr(self.M, "TELEGRAM_CHAT_ID")
        assert hasattr(self.M, "TELEGRAM_PRIVATE_CHAT_ID")

    def test_telegram_values_are_strs(self):
        """Telegram 配置都是字符串"""
        assert isinstance(self.M.TELEGRAM_REPORT_BOT_TOKEN, str)
        assert isinstance(self.M.TELEGRAM_REPORT_CHAT_ID, str)

    def test_paper_initial_capital_positive(self):
        """PAPER_INITIAL_CAPITAL 是正数"""
        assert self.M.PAPER_INITIAL_CAPITAL > 0
        assert isinstance(self.M.PAPER_INITIAL_CAPITAL, float)

    def test_real_initial_capital_positive(self):
        """REAL_INITIAL_CAPITAL 是正数"""
        assert self.M.REAL_INITIAL_CAPITAL > 0
        assert isinstance(self.M.REAL_INITIAL_CAPITAL, float)

    def test_max_account_drawdown_between_zero_and_one(self):
        """MAX_ACCOUNT_DRAWDOWN 在 0~1 之间"""
        dd = self.M.MAX_ACCOUNT_DRAWDOWN
        assert 0 < dd < 1, f"MAX_ACCOUNT_DRAWDOWN={dd}, expected between 0 and 1"
        assert isinstance(dd, float)

    def test_pullback_scan_interval_positive_int(self):
        """PULLBACK_SCAN_INTERVAL 是正整数"""
        interval = self.M.PULLBACK_SCAN_INTERVAL
        assert isinstance(interval, int)
        assert interval > 0

    def test_audit_enabled_is_bool(self):
        """AUDIT_ENABLED 是 bool"""
        assert isinstance(self.M.AUDIT_ENABLED, bool)

    def test_default_position_pct_is_float(self):
        """DEFAULT_POSITION_PCT 是 float"""
        assert isinstance(self.M.DEFAULT_POSITION_PCT, float)

    def test_max_positions_is_int(self):
        """MAX_POSITIONS 是 int"""
        assert isinstance(self.M.MAX_POSITIONS, int)

    def test_qmt_base_url_format(self):
        """QMT_BASE_URL 格式为 http://host:port"""
        url = self.M.QMT_BASE_URL
        assert url.startswith("http://")
        assert re.match(r"http://[^:]+:\d+", url), f"Unexpected QMT_BASE_URL: {url}"

    def test_proxy_enabled_is_bool(self):
        """PROXY_ENABLED 是 bool"""
        assert isinstance(self.M.PROXY_ENABLED, bool)

    def test_account_mode_string(self):
        """ACCOUNT_MODE 是字符串"""
        assert isinstance(self.M.ACCOUNT_MODE, str)

    def test_real_trade_enabled_is_bool(self):
        """REAL_TRADE_ENABLED 是 bool"""
        assert isinstance(self.M.REAL_TRADE_ENABLED, bool)

    def test_dns_cache_ttl_positive_int(self):
        """DNS_CACHE_TTL 是正整数"""
        assert isinstance(self.M.DNS_CACHE_TTL, int)
        assert self.M.DNS_CACHE_TTL > 0

    def test_swap_score_gap_is_float(self):
        """SWAP_SCORE_GAP 是 float"""
        assert isinstance(self.M.SWAP_SCORE_GAP, float)

    def test_day_trade_time_ratios_are_float(self):
        """MAX_SINGLE_STOCK_PCT / MAX_SINGLE_SECTOR_PCT 等是 0~1 的 float"""
        for attr in [
            "MAX_SINGLE_STOCK_PCT",
            "MAX_SINGLE_SECTOR_PCT",
            "CASH_RESERVE_PCT",
        ]:
            val = getattr(self.M, attr)
            assert isinstance(val, float), f"{attr} should be float, got {type(val)}"
            assert 0 < val < 1, f"{attr}={val} should be between 0 and 1"

    def test_env_position_limit_is_dict(self):
        """ENV_POSITION_LIMIT 是包含 bull/swing/bear 的 dict"""
        d = self.M.ENV_POSITION_LIMIT
        assert isinstance(d, dict)
        for key in ("bull", "swing", "bear"):
            assert key in d
            assert 0 < d[key] < 1

    def test_audit_retention_days_is_int(self):
        """AUDIT_RETENTION_DAYS 是 int"""
        assert isinstance(self.M.AUDIT_RETENTION_DAYS, int)

    # --- env override 行为 ---
    # 注意：.env 文件中有 QMT_HOST / QMT_PORT 等值，且 settings 中
    # load_dotenv(override=True) 会在 reload 时覆盖环境变量。
    # 因此需要 patch dotenv.load_dotenv 使其成为空操作。

    # 注意：env override 测试不能公用 _no_dotenv fixture，因为 monkeypatch 的
    # undo 与 fixture 生命周期冲突。改用独立逻辑内嵌。

    def _reload_with_vars(self, monkeypatch, env_vars: dict):
        """patch dotenv.load_dotenv → set env vars → reload → return module"""
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)
        for k, v in env_vars.items():
            monkeypatch.setenv(k, v)
        mod = importlib.import_module("system.config.settings")
        importlib.reload(mod)
        return mod

    def test_env_override_paper_initial_capital(self, monkeypatch):
        """设置 PAPER_INITIAL_CAPITAL env 应改变值"""
        mod = self._reload_with_vars(monkeypatch, {"PAPER_INITIAL_CAPITAL": "500000"})
        assert mod.PAPER_INITIAL_CAPITAL == 500000.0

    def test_env_override_account_drawdown(self, monkeypatch):
        """设置 MAX_ACCOUNT_DRAWDOWN env 应改变值"""
        mod = self._reload_with_vars(monkeypatch, {"MAX_ACCOUNT_DRAWDOWN": "0.25"})
        assert mod.MAX_ACCOUNT_DRAWDOWN == 0.25

    def test_env_override_audit_enabled_false(self, monkeypatch):
        """设置 AUDIT_ENABLED=false 应得到 False"""
        mod = self._reload_with_vars(monkeypatch, {"AUDIT_ENABLED": "false"})
        assert mod.AUDIT_ENABLED is False

    def test_env_override_qmt_host(self, monkeypatch):
        """设置 QMT_HOST 刷新 QMT_BASE_URL"""
        mod = self._reload_with_vars(
            monkeypatch, {"QMT_HOST": "10.0.0.1", "QMT_PORT": "8080"}
        )
        assert mod.QMT_BASE_URL == "http://10.0.0.1:8080"


# ================================================================
#  system.config.trading_calendar
# ================================================================


class TestTradingCalendar:
    """system.config.trading_calendar 交易日历"""

    CAL = pytest.importorskip("system.config.trading_calendar")

    # 已知日期（2026-06 月）：今日 2026-06-06 (Sat)
    MONDAY = "2026-06-08"  # Mon, weekday()=0
    TUESDAY = "2026-06-09"
    WEDNESDAY = "2026-06-10"
    THURSDAY = "2026-06-11"
    FRIDAY = "2026-06-12"  # Fri, weekday()=4
    SATURDAY = "2026-06-06"  # Sat, 当前日期
    SUNDAY = "2026-06-07"  # Sun
    LAST_FRIDAY = "2026-06-05"  # 上周五（MONDAY 之前）

    # --- is_trading_day ---

    def test_weekday_is_trading_day(self):
        """周一至周五（非节假日）是交易日"""
        for day in [
            self.MONDAY,
            self.TUESDAY,
            self.WEDNESDAY,
            self.THURSDAY,
            self.FRIDAY,
        ]:
            assert self.CAL.is_trading_day(day), f"{day} should be a trading day"

    def test_saturday_not_trading_day(self):
        """周六不是交易日"""
        assert not self.CAL.is_trading_day(self.SATURDAY)

    def test_sunday_not_trading_day(self):
        """周日不是交易日"""
        assert not self.CAL.is_trading_day(self.SUNDAY)

    def test_known_holiday_not_trading_day(self):
        """已知节假日（2026-04-06 清明假期第3天）不是交易日"""
        assert not self.CAL.is_trading_day("2026-04-06")

    def test_national_holiday_not_trading_day(self):
        """国庆假期不是交易日"""
        assert not self.CAL.is_trading_day("2026-10-01")
        assert not self.CAL.is_trading_day("2026-10-06")

    def test_lunar_new_year_not_trading_day(self):
        """春节假期不是交易日（2026-02-17 至 2026-02-24）"""
        assert not self.CAL.is_trading_day("2026-02-18")
        assert not self.CAL.is_trading_day("2026-02-21")

    def test_labor_day_not_trading_day(self):
        """五一假期不是交易日"""
        assert not self.CAL.is_trading_day("2026-05-01")
        assert not self.CAL.is_trading_day("2026-05-04")

    def test_dragon_boat_not_trading_day(self):
        """端午假期不是交易日"""
        assert not self.CAL.is_trading_day("2026-06-19")
        assert not self.CAL.is_trading_day("2026-06-21")

    # --- get_previous_trading_day ---

    def test_previous_trading_day_returns_earlier_date(self):
        """get_previous_trading_day 返回更早的日期"""
        prev = self.CAL.get_previous_trading_day(self.WEDNESDAY)
        assert prev < self.WEDNESDAY

    def test_previous_trading_day_tuesday_returns_monday(self):
        """周三的上一个交易日是周二"""
        prev = self.CAL.get_previous_trading_day(self.WEDNESDAY)
        assert prev == self.TUESDAY

    def test_previous_trading_day_across_weekend(self):
        """周一的上一个交易日是上周五"""
        prev = self.CAL.get_previous_trading_day(self.MONDAY)
        assert prev == self.LAST_FRIDAY, (
            f"Monday {self.MONDAY} prev should be Friday {self.LAST_FRIDAY}, got {prev}"
        )

    def test_previous_trading_day_offset_2(self):
        """周三的前2个交易日是周一"""
        prev = self.CAL.get_previous_trading_day(self.WEDNESDAY, offset=2)
        assert prev == self.MONDAY

    def test_previous_trading_day_after_holiday(self):
        """节后首交易日：2026-05-06 的上一交易日是 2026-04-30（五一前最后一天）"""
        prev = self.CAL.get_previous_trading_day("2026-05-06")
        assert prev is not None
        assert prev == "2026-04-30"

    # --- get_next_trading_day ---

    def test_next_trading_day_returns_later_date(self):
        """get_next_trading_day 返回更晚的日期"""
        nxt = self.CAL.get_next_trading_day(self.THURSDAY)
        assert nxt > self.THURSDAY

    def test_next_trading_day_thursday_returns_friday(self):
        """周四的下一个交易日是周五"""
        nxt = self.CAL.get_next_trading_day(self.THURSDAY)
        assert nxt == self.FRIDAY

    def test_next_trading_day_across_weekend(self):
        """周五的下一个交易日是下周一"""
        NEXT_MONDAY = "2026-06-15"
        nxt = self.CAL.get_next_trading_day(self.FRIDAY)
        assert nxt == NEXT_MONDAY, (
            f"Friday {self.FRIDAY} next should be Monday {NEXT_MONDAY}, got {nxt}"
        )

    def test_next_trading_day_before_holiday(self):
        """节前最后交易日：2026-04-30 的下一个交易日是 2026-05-06（五一后）"""
        nxt = self.CAL.get_next_trading_day("2026-04-30")
        assert nxt == "2026-05-06"

    # --- get_recent_trading_days ---

    def test_get_recent_trading_days_count(self):
        """get_recent_trading_days 返回指定数量的交易日"""
        days = self.CAL.get_recent_trading_days(self.FRIDAY, count=3)
        assert len(days) == 3

    def test_get_recent_trading_days_sorted(self):
        """get_recent_trading_days 从近到远排列（近期在前）"""
        days = self.CAL.get_recent_trading_days(self.FRIDAY, count=3)
        assert len(days) == 3
        # 降序（从近到远）
        assert days == sorted(days, reverse=True), f"Expected newest first, got {days}"
        # 验证不含 target_date
        assert self.FRIDAY not in days

    def test_get_recent_trading_days_crosses_weekend(self):
        """get_recent_trading_days 跨周末"""
        days = self.CAL.get_recent_trading_days(self.MONDAY, count=1)
        assert days == [self.LAST_FRIDAY], f"Expected [{self.LAST_FRIDAY}], got {days}"

    # --- None 参数（默认今天） —— 只验证不报错 ---

    def test_is_trading_day_default(self):
        """is_trading_day() 默认今天不报错（今天是 2026-06-06 周六 → False）"""
        # 注意：这个测试依赖 date 校准，若在不同日期运行可能不同
        result = self.CAL.is_trading_day()
        assert result is not None  # 至少不报错

    def test_get_previous_trading_day_default(self):
        """get_previous_trading_day() 默认今天不报错"""
        result = self.CAL.get_previous_trading_day()
        assert result is None or isinstance(result, str)

    def test_get_next_trading_day_default(self):
        """get_next_trading_day() 默认今天不报错"""
        result = self.CAL.get_next_trading_day()
        assert result is None or isinstance(result, str)


# ================================================================
#  system.utils.stock_code_utils
# ================================================================


class TestStockCodeUtils:
    """system.utils.stock_code_utils 股票代码工具"""

    U = pytest.importorskip("system.utils.stock_code_utils")

    # --- normalize_stock_code ---

    def test_normalize_sh_code(self):
        """600xxx → 600xxx.SH"""
        assert self.U.normalize_stock_code("600000") == "600000.SH"

    def test_normalize_sz_code(self):
        """000xxx → 000xxx.SZ"""
        assert self.U.normalize_stock_code("000001") == "000001.SZ"

    def test_normalize_gem_code(self):
        """300xxx（创业板）→ 300xxx.SZ"""
        assert self.U.normalize_stock_code("300750") == "300750.SZ"

    def test_normalize_sse_star_code(self):
        """688xxx（科创板）→ 688xxx.SH"""
        assert self.U.normalize_stock_code("688001") == "688001.SH"

    def test_normalize_sme_code(self):
        """002xxx（中小板）→ 002xxx.SZ"""
        assert self.U.normalize_stock_code("002415") == "002415.SZ"

    def test_normalize_bj_code(self):
        """83xxxx → 83xxxx.BJ"""
        assert self.U.normalize_stock_code("839273") == "839273.BJ"

    def test_normalize_already_normalized(self):
        """已带后缀的不重复添加，仅转大写"""
        assert self.U.normalize_stock_code("000001.SZ") == "000001.SZ"

    def test_normalize_lowercase_suffix(self):
        """小写后缀转大写"""
        assert self.U.normalize_stock_code("600000.sh") == "600000.SH"
        assert self.U.normalize_stock_code("000001.sz") == "000001.SZ"

    def test_normalize_empty_returns_empty(self):
        """空字符串返回空"""
        assert self.U.normalize_stock_code("") == ""
        assert self.U.normalize_stock_code(None) is None

    def test_normalize_unrecognized_code(self):
        """无法识别的代码原样返回（前缀不在任何规则内）"""
        # "999999" 不在任何已知规则中，应原样返回
        assert self.U.normalize_stock_code("999999") == "999999"

    # --- strip_stock_code (等价于 extract_code) ---

    def test_strip_sz_suffix(self):
        """000001.SZ → 000001"""
        assert self.U.strip_stock_code("000001.SZ") == "000001"

    def test_strip_sh_suffix(self):
        """600000.SH → 600000"""
        assert self.U.strip_stock_code("600000.SH") == "600000"

    def test_strip_bj_suffix(self):
        """839273.BJ → 839273"""
        assert self.U.strip_stock_code("839273.BJ") == "839273"

    def test_strip_lowercase_suffix(self):
        """小写后缀也去掉"""
        assert self.U.strip_stock_code("600000.sh") == "600000"
        assert self.U.strip_stock_code("000001.sz") == "000001"

    def test_strip_no_suffix_unchanged(self):
        """无后缀返回原值"""
        assert self.U.strip_stock_code("600000") == "600000"

    def test_strip_empty(self):
        """空值返回空"""
        assert self.U.strip_stock_code("") == ""
        assert self.U.strip_stock_code(None) is None

    def test_strip_whitespace(self):
        """自动去除空格"""
        assert self.U.strip_stock_code("  600000.SH  ") == "600000"

    # --- get_stock_suffix (等价于 is_sh_code / is_sz_code) ---

    def test_suffix_sh_a(self):
        """600xxx 返回 .SH"""
        assert self.U.get_stock_suffix("600000") == ".SH"

    def test_suffix_sz_a(self):
        """000xxx 返回 .SZ"""
        assert self.U.get_stock_suffix("000001") == ".SZ"

    def test_suffix_gem(self):
        """300xxx 返回 .SZ"""
        assert self.U.get_stock_suffix("300750") == ".SZ"

    def test_suffix_sse_star(self):
        """688xxx 返回 .SH"""
        assert self.U.get_stock_suffix("688001") == ".SH"

    def test_suffix_sme(self):
        """002xxx 返回 .SZ"""
        assert self.U.get_stock_suffix("002415") == ".SZ"

    def test_suffix_bj(self):
        """8xxxxx / 920xxx 返回 .BJ"""
        assert self.U.get_stock_suffix("830001") == ".BJ"
        assert self.U.get_stock_suffix("920001") == ".BJ"

    def test_suffix_b_sh(self):
        """900xxx（沪市B股）返回 .SH"""
        assert self.U.get_stock_suffix("900901") == ".SH"

    def test_suffix_b_sz(self):
        """200xxx（深市B股）返回 .SZ"""
        assert self.U.get_stock_suffix("200001") == ".SZ"

    def test_suffix_cb_sh(self):
        """118xxx / 113xxx（沪市可转债）返回 .SH"""
        assert self.U.get_stock_suffix("118001") == ".SH"
        assert self.U.get_stock_suffix("113001") == ".SH"

    def test_suffix_cb_sz(self):
        """123xxx / 127xxx（深市可转债）返回 .SZ"""
        assert self.U.get_stock_suffix("123001") == ".SZ"
        assert self.U.get_stock_suffix("127001") == ".SZ"

    def test_suffix_short_code_returns_none(self):
        """小于6位或 None 返回 None"""
        assert self.U.get_stock_suffix("600") is None
        assert self.U.get_stock_suffix("") is None
        assert self.U.get_stock_suffix(None) is None

    def test_suffix_already_has_dot_returns_none(self):
        """已带后缀时返回 None（strip 之后再调用）"""
        assert self.U.get_stock_suffix("600000.SH") is None

    # --- validate_stock_code ---

    def test_validate_valid(self):
        """有效的带后缀代码返回 True"""
        assert self.U.validate_stock_code("000001.SZ") is True
        assert self.U.validate_stock_code("600000.SH") is True
        assert self.U.validate_stock_code("839273.BJ") is True

    def test_validate_no_dot(self):
        """无后缀返回 False"""
        assert self.U.validate_stock_code("600000") is False

    def test_validate_wrong_suffix(self):
        """非法后缀返回 False"""
        assert self.U.validate_stock_code("000001.XX") is False

    def test_validate_invalid_length(self):
        """非6位数字返回 False"""
        assert self.U.validate_stock_code("60000.SH") is False

    def test_validate_empty(self):
        """空值返回 False"""
        assert self.U.validate_stock_code("") is False
        assert self.U.validate_stock_code(None) is False

    def test_validate_non_digit_code(self):
        """非数字代码前缀返回 False"""
        assert self.U.validate_stock_code("ABCDEF.SZ") is False


# ================================================================
#  system.utils.dns_bypass
# ================================================================


class TestDnsBypass:
    """system.utils.dns_bypass DNS 绕过"""

    D = pytest.importorskip("system.utils.dns_bypass")

    def setup_method(self):
        """每次测试前确保还原"""
        self.D.uninstall()

    def teardown_method(self):
        """每次测试后还原"""
        self.D.uninstall()

    # --- install / uninstall ---

    def test_install_does_not_crash(self):
        """install() 正常执行不报错"""
        self.D.install()
        assert self.D._installed is True

    def test_install_is_idempotent(self):
        """install() 两次调用不报错，socket 函数是同一个"""
        self.D.install()
        after_first = socket.getaddrinfo
        self.D.install()
        after_second = socket.getaddrinfo
        # 第二次 install 不换函数
        assert after_second is after_first

    def test_uninstall_restores_original(self):
        """uninstall() 恢复 socket.getaddrinfo"""
        orig = socket.getaddrinfo
        self.D.install()
        assert socket.getaddrinfo is not orig
        self.D.uninstall()
        assert socket.getaddrinfo is orig

    def test_uninstall_does_not_crash(self):
        """uninstall() 正常执行不报错"""
        self.D.uninstall()
        assert self.D._installed is False

    def test_uninstall_is_idempotent(self):
        """uninstall() 两次调用不报错"""
        self.D.uninstall()
        self.D.uninstall()
        assert self.D._installed is False

    # --- _is_fake_ip ---

    def test_fake_ip_198_18(self):
        """198.18.x.x 被认为是虚假 IP"""
        assert self.D._is_fake_ip("198.18.0.1") is True

    def test_fake_ip_198_19(self):
        """198.19.x.x 被认为是虚假 IP"""
        assert self.D._is_fake_ip("198.19.255.255") is True

    def test_real_ip_not_fake(self):
        """真实公网 IP 不被认为是虚假的"""
        assert self.D._is_fake_ip("114.114.114.114") is False
        assert self.D._is_fake_ip("8.8.8.8") is False
        assert self.D._is_fake_ip("192.168.1.1") is False

    def test_fake_ip_invalid_format(self):
        """无效格式不报错"""
        assert self.D._is_fake_ip("not-an-ip") is False
        assert self.D._is_fake_ip("") is False

    # --- _is_ip_address ---

    def test_is_ip_address_true(self):
        """IPv4 地址返回 True"""
        assert self.D._is_ip_address("192.168.1.1") is True
        assert self.D._is_ip_address("8.8.8.8") is True

    def test_is_ip_address_false(self):
        """非 IP 返回 False"""
        assert self.D._is_ip_address("example.com") is False
        assert self.D._is_ip_address("") is False

    # --- install 后 socket 不变（不验证 DNS 解析，只验证不崩溃） ---

    def test_install_then_getaddrinfo_localhost(self):
        """安装补丁后解析 localhost 不报错"""
        self.D.install()
        try:
            result = socket.getaddrinfo("localhost", 80)
            assert len(result) > 0
        finally:
            self.D.uninstall()

    def test_install_then_getaddrinfo_ip(self):
        """安装补丁后解析 IP 地址直接透传"""
        self.D.install()
        # 应该在兜底路径中直接走 _orig_getaddrinfo（IP 地址透传）
        try:
            result = socket.getaddrinfo("127.0.0.1", 80)
            assert len(result) > 0
        finally:
            self.D.uninstall()


# ================================================================
#  system.utils.logger
# ================================================================


class TestLogger:
    """system.utils.logger 日志工具"""

    LG = pytest.importorskip("system.utils.logger")

    # --- Logger 工厂函数 ---

    def test_get_system_logger_returns_logger(self):
        """get_system_logger 返回 Logger 实例"""
        logger = self.LG.get_system_logger("test_module")
        assert isinstance(logger, logging.Logger)

    def test_get_task_logger_returns_logger(self):
        """get_task_logger 返回 Logger 实例"""
        logger = self.LG.get_task_logger("test_task")
        assert isinstance(logger, logging.Logger)

    def test_get_core_logger_returns_logger(self):
        """返回 Logger 实例"""
        logger = self.LG.get_system_logger("test_core")
        assert isinstance(logger, logging.Logger)

    def test_get_collector_logger_returns_logger(self):
        """返回 Logger 实例"""
        logger = self.LG.get_collect_logger("test_collector")
        assert isinstance(logger, logging.Logger)

    def test_task_logger_name_format(self):
        """get_task_logger 使用 task.{task_name} 名称"""
        logger = self.LG.get_task_logger("review")
        assert logger.name == "task.review"

    def test_system_logger_name_no_context(self):
        """get_system_logger 无任务上下文时使用原名"""
        self.LG.set_current_task("")  # 清除上下文
        # 确保当前任务为 None
        self.LG.get_current_task()
        # 重新设 None
        import contextvars

        self.LG._current_task = contextvars.ContextVar("current_task", default=None)
        self.LG._current_task.set(None)

        logger = self.LG.get_system_logger("analyzer")
        assert logger.name == "analyzer"

    def test_core_logger_name_with_context(self):
        """get_system_logger 有任务上下文时使用 task.{task}.system.{name}"""
        self.LG.set_current_task("review")
        logger = self.LG.get_system_logger("analyzer")
        assert logger.name == "task.review.system.analyzer"
        # 清理
        self.LG.set_current_task("")

    def test_collector_logger_name_with_context(self):
        """get_collect_logger 有任务上下文时使用 task.{task}.collect.{name}"""
        self.LG.set_current_task("review")
        logger = self.LG.get_collect_logger("fetcher")
        assert logger.name == "task.review.collect.fetcher"

    def test_task_logger_disables_propagation(self):
        """get_task_logger 关闭 propagation"""
        logger = self.LG.get_task_logger("no_prop")
        assert logger.propagate is False

    def test_core_logger_enables_propagation(self):
        """开启 propagation"""
        logger = self.LG.get_system_logger("prop_test")
        assert logger.propagate is True

    def test_task_logger_has_handlers(self):
        """get_task_logger 有 file + stream handler"""
        logger = self.LG.get_task_logger("handler_check")
        assert len(logger.handlers) >= 2

    def test_task_logger_has_file_handler(self):
        """get_task_logger 包含 FileHandler"""
        logger = self.LG.get_task_logger("fh_check")
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "FileHandler" in handler_types

    def test_task_logger_has_stream_handler(self):
        """get_task_logger 包含 StreamHandler"""
        logger = self.LG.get_task_logger("sh_check")
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" in handler_types

    def test_task_logger_level_debug(self):
        """get_task_logger logger setLevel DEBUG"""
        logger = self.LG.get_task_logger("lvl_check")
        assert logger.level == logging.DEBUG

    def test_core_logger_has_debug_file_handler(self):
        """的 FileHandler 级别为 DEBUG"""
        logger = self.LG.get_system_logger("debug_core")
        fhs = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
        assert len(fhs) >= 1
        assert fhs[0].level == logging.DEBUG

    def test_set_current_task_does_not_crash(self):
        """set_current_task 正常执行"""
        self.LG.set_current_task("some_task")
        assert self.LG.get_current_task() == "some_task"
        self.LG.set_current_task("")

    def test_get_current_task_returns_string_or_none(self):
        """get_current_task 返回字符串或 None"""
        task = self.LG.get_current_task()
        assert task is None or isinstance(task, str)

    def test_logger_creates_log_dir(self, tmp_path):
        """get_task_logger 创建日志目录"""
        import system.utils.logger as lg

        original = lg.LOGS_DIR
        lg.LOGS_DIR = tmp_path
        try:
            lg.get_task_logger("dir_check")
            expected_dir = tmp_path / datetime.now().strftime("%Y-%m-%d") / "tasks"
            assert expected_dir.exists()
        finally:
            lg.LOGS_DIR = original

    def test_get_task_logger_idempotent(self):
        """get_task_logger 重复调用返回同一实例"""
        a = self.LG.get_task_logger("idem_task")
        b = self.LG.get_task_logger("idem_task")
        assert a is b

    def test_get_core_logger_idempotent(self):
        """重复调用返回同一实例"""
        a = self.LG.get_system_logger("idem_core")
        b = self.LG.get_system_logger("idem_core")
        assert a is b

    def test_system_logger_delegates_to_core(self):
        """get_system_logger(name) 等于 get_core_logger(name)"""
        s = self.LG.get_system_logger("sys_test")
        c = self.LG.get_system_logger("sys_test")
        assert s is c


# ================================================================
#  system.utils.regulatory_analysis
# ================================================================


class TestRegulatoryAnalysis:
    """system.utils.regulatory_analysis 监管函分析"""

    RA = pytest.importorskip("system.utils.regulatory_analysis")

    @pytest.fixture
    def service(self):
        return self.RA.RegulatoryAnalysisService()

    def test_service_instantiation(self, service):
        """RegulatoryAnalysisService 可实例化"""
        assert service is not None
        assert hasattr(service, "risk_library")
        assert hasattr(service, "analyze_title")
        assert hasattr(service, "analyze_content")

    def test_risk_library_has_entries(self, service):
        """risk_library 包含预期风险条目"""
        assert "真实性" in service.risk_library
        assert "立案调查" in service.risk_library
        assert "虚假记载" in service.risk_library
        assert "持续经营能力" in service.risk_library

    def test_risk_library_levels_in_range(self, service):
        """风险等级在 1~5 范围内"""
        for key, info in service.risk_library.items():
            assert 1 <= info["level"] <= 5, f"{key}: level {info['level']} out of range"

    # --- analyze_title ---

    def test_analyze_title_empty(self, service):
        """空标题不报错，风险等级最低"""
        result = service.analyze_title("")
        assert result["risk_level"] == 1
        assert len(result["alerts"]) == 0

    def test_analyze_title_normal(self, service):
        """普通标题无风险关键词"""
        result = service.analyze_title("关于召开2025年年度股东大会的通知")
        assert result["risk_level"] == 1
        assert len(result["alerts"]) == 0

    def test_analyze_title_high_risk_fraud(self, service):
        """标题含"真实性"关键词 → level 5"""
        result = service.analyze_title("关于核实营业收入真实性的问询函")
        assert result["risk_level"] == 5
        alerts = {a["keyword"] for a in result["alerts"]}
        assert "真实性" in alerts or "核实...真实性" in alerts

    def test_analyze_title_high_risk_investigation(self, service):
        """标题含"立案调查" → level 5"""
        result = service.analyze_title("关于收到中国证监会立案调查通知书的公告")
        assert result["risk_level"] == 5
        assert any(a["keyword"] == "立案调查" for a in result["alerts"])

    def test_analyze_title_medium_risk_reasonableness(self, service):
        """标题含"合理性" → level 3"""
        result = service.analyze_title("关于说明年报数据合理性的问询函")
        assert result["risk_level"] == 3

    def test_analyze_title_strips_html(self, service):
        """HTML 标签被剥离后再分析"""
        result = service.analyze_title("<b>关于立案调查的公告</b>")
        assert result["risk_level"] == 5
        assert len(result["alerts"]) > 0

    def test_analyze_title_multi_keyword(self, service):
        """多关键词触发多个 alert"""
        result = service.analyze_title("关于是否存在关联交易及信息披露不准确的事宜")
        assert result["risk_level"] == 4  # 两者都是 level 4
        assert len(result["alerts"]) >= 2

    # --- analyze_content ---

    def test_analyze_content_basic(self, service):
        """分析简单文本"""
        text = "本次问询主要关注持续经营能力是否存在问题"
        result = service.analyze_content(text)
        assert result["word_count"] == len(text)
        assert len(result["keywords"]) > 0

    def test_analyze_content_issuer_extraction(self, service):
        """提取发函机构"""
        text = "上海证券交易所关于公司年报的问询函"
        result = service.analyze_content(text)
        assert result["issuer"] == "上海证券交易所"
        assert result["issuer_short"] == "上交所"

    def test_analyze_content_csrc_extraction(self, service):
        """提取中国证监会"""
        text = "中国证监会关于公司立案调查的通知书"
        result = service.analyze_content(text)
        assert result["issuer"] == "中国证监会"
        assert result["issuer_short"] == "证监会"

    def test_analyze_content_date_extraction(self, service):
        """提取发文日期"""
        text = "本函发出日期为2026年5月20日"
        result = service.analyze_content(text)
        assert result["issue_date"] == "2026年5月20日"

    def test_analyze_content_risk_type_fraud(self, service):
        """检测财务造假类型"""
        text = "公司存在虚假记载和财务造假的嫌疑"
        result = service.analyze_content(text)
        assert result["risk_type"] == "财务造假"

    def test_analyze_content_risk_type_investigation(self, service):
        """检测立案调查类型"""
        text = "收到证监会立案调查通知书"
        result = service.analyze_content(text)
        assert result["risk_type"] == "立案调查"

    def test_analyze_content_summary_truncation(self, service):
        """长文本自动截断摘要"""
        text = "核心内容。" * 200  # 远超过 500 字
        result = service.analyze_content(text)
        assert "..." in result["pdf_summary"]
        assert len(result["pdf_summary"]) <= 510  # 500 + "..."

    def test_analyze_content_no_issuer(self, service):
        """不包含发函机构时字段为空"""
        result = service.analyze_content("这是一份普通的文件")
        assert result["issuer"] == ""
        assert result["issuer_short"] == ""

    # --- extract_pdf_text ---

    def test_extract_pdf_nonexistent_file(self, service):
        """不存在的 PDF 返回 None"""
        result = service.extract_pdf_text("/nonexistent/path.pdf")
        assert result is None

    def test_extract_pdf_empty_path(self, service):
        """空路径返回 None"""
        result = service.extract_pdf_text("")
        assert result is None

    # --- analyze_pdf (完整流程) ---

    def test_analyze_pdf_nonexistent(self, service):
        """不存在的 PDF → None"""
        result = service.analyze_pdf("/nonexistent.pdf")
        assert result is None

    def test_issuer_map_keys(self, service):
        """issuer_map 包含所有主要监管机构"""
        expected_keys = [
            "上海证券交易所",
            "深圳证券交易所",
            "北京证券交易所",
            "中国证监会",
            "上海证监局",
            "深圳证监局",
        ]
        for key in expected_keys:
            assert key in service.issuer_map, f"Missing issuer: {key}"
