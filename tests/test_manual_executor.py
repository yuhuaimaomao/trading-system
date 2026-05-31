"""ManualExecutor.parse_reply() 测试 — 覆盖所有分支"""

import pytest

from trade.execution.manual import ManualExecutor


class TestParseReply:
    """parse_reply() 单元测试"""

    def test_code_and_account_paper(self):
        """六位代码 + 模拟盘"""
        r = ManualExecutor.parse_reply("模拟盘 000001 1000股 12.50元")
        assert r["stock_code"] == "000001"
        assert r["account"] == "paper"
        assert r["volume"] == 1000
        assert r["price"] == 12.50

    def test_code_and_account_real(self):
        """六位代码 + 实盘"""
        r = ManualExecutor.parse_reply("实盘 000001 800股 12.48元")
        assert r["stock_code"] == "000001"
        assert r["account"] == "real"
        assert r["volume"] == 800
        assert r["price"] == 12.48

    def test_not_filled(self):
        """没成交 → status=rejected"""
        r = ManualExecutor.parse_reply("000001 没成交")
        assert r["status"] == "rejected"

    def test_not_filled_variants(self):
        """各种未成交表达"""
        for kw in ["没成交", "未成交", "没买到", "未买到"]:
            r = ManualExecutor.parse_reply(f"000002 {kw}")
            assert r["status"] == "rejected", f"Failed for: {kw}"

    def test_bought_no_account(self):
        """买了但不指定账户 → status=filled, account=None"""
        r = ManualExecutor.parse_reply("000001 买了 1000股 12.50")
        assert r["status"] == "filled"
        assert r["account"] is None

    def test_name_instead_of_code(self):
        """用股票名称而非代码"""
        r = ManualExecutor.parse_reply("拓普集团 买了 500股 72.77")
        assert r["stock_name"] == "拓普集团"
        assert r["stock_code"] is None
        assert r["status"] == "filled"

    def test_name_with_account(self):
        """名称 + 模拟盘"""
        r = ManualExecutor.parse_reply("模拟盘 贵州茅台 100股 1800.00元")
        assert r["stock_name"] == "贵州茅台"
        assert r["account"] == "paper"
        assert r["volume"] == 100
        assert r["price"] == 1800.00

    def test_operation_word_not_matched_as_name(self):
        """操作词"没成交"不会被误识别为股票名称"""
        r = ManualExecutor.parse_reply("000001 没成交")
        # stock_name 不应是 "没成交"
        assert r["stock_name"] != "没成交"
        assert r["status"] == "rejected"

    def test_operation_word_not_matched_as_name_2(self):
        """操作词"未买到"不会被误识别为股票名称"""
        r = ManualExecutor.parse_reply("未买到")
        assert r["stock_name"] != "未买到"

    def test_price_with_yuan(self):
        """价格带"元"字"""
        r = ManualExecutor.parse_reply("000001 买了 1000股 12.50元")
        assert r["price"] == 12.50

    def test_account_default_filled(self):
        """有账户标记但无状态词 → 默认 filled"""
        r = ManualExecutor.parse_reply("模拟盘 000001 1000股 12.50元")
        assert r["status"] == "filled"
        assert r["account"] == "paper"

    def test_real_with_paper_keyword(self):
        """real 关键词 → account=real"""
        r = ManualExecutor.parse_reply("real 000001 800股 12.48")
        assert r["account"] == "real"

    @pytest.mark.parametrize(
        "text,expected_code,expected_name",
        [
            ("000001 买了", "000001", None),
            ("600519 成交 100股", "600519", None),
            ("没成交", None, None),  # 操作词已被过滤
        ],
    )
    def test_various_formats(self, text, expected_code, expected_name):
        r = ManualExecutor.parse_reply(text)
        assert r["stock_code"] == expected_code
        assert r["stock_name"] == expected_name
