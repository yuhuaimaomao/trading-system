"""
Tests for data collectors: proxy (ProxyManager, ProxyRequester, ProxyBaseCollector, IPStats),
events (TelegraphCollector), macro (MacroCollector).

External HTTP calls and AI API calls are fully mocked.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# =====================================================================
# ProxyManager
# =====================================================================


class TestProxyManager:
    """ProxyManager: proxy IP acquisition from Tianqi API"""

    PATCH_PATH = "data.collect.proxy.proxy_manager"

    def _make_pm(self, trade_date=None, collector_name=None):
        """Helper: create ProxyManager with IP_STATS_ENABLED=False."""
        with patch(f"{self.PATCH_PATH}.IP_STATS_ENABLED", False):
            with patch(f"{self.PATCH_PATH}.record_ip_usage", None):
                from data.collect.proxy.proxy_manager import ProxyManager

                return ProxyManager(
                    trade_date=trade_date or "2026-06-01",
                    collector_name=collector_name or "test",
                )

    def _make_get_resp(self, json_data, status_code=200):
        """Helper: create a mock requests.Response."""
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.json.return_value = json_data
        mock_resp.text = json.dumps(json_data)
        return mock_resp

    def test_init(self):
        pm = self._make_pm(trade_date="2026-06-01", collector_name="test_init")
        assert pm.trade_date == "2026-06-01"
        assert pm.collector_name == "test_init"

    def test_get_proxy_success_dict_format(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = self._make_get_resp(
                {"code": 1000, "data": [{"ip": "123.45.67.89", "port": 8080}]}
            )
            result = pm.get_proxy()

        assert result == {
            "http": "http://123.45.67.89:8080",
            "https": "http://123.45.67.89:8080",
        }

    def test_get_proxy_success_list_format(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = self._make_get_resp(
                [{"ip": "5.6.7.8", "port": 3128}]
            )
            result = pm.get_proxy()
        assert result == {
            "http": "http://5.6.7.8:3128",
            "https": "http://5.6.7.8:3128",
        }

    def test_get_proxy_timeout(self):
        import requests

        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.side_effect = requests.exceptions.Timeout()
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_request_exception(self):
        import requests

        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.side_effect = requests.exceptions.ConnectionError(
                "connection refused"
            )
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_http_error_status(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = self._make_get_resp({}, status_code=500)
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_wrong_api_code(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = self._make_get_resp({"code": 1001, "data": []})
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_empty_data_list(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = self._make_get_resp({"code": 1000, "data": []})
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_missing_ip_or_port(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = self._make_get_resp(
                {"code": 1000, "data": [{"ip": "", "port": 0}]}
            )
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_invalid_json(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = MagicMock(status_code=200)
            mock_get.return_value.json.side_effect = ValueError("Invalid JSON")
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_unexpected_format(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = self._make_get_resp("not_a_dict_or_list")
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_missing_ip_or_port_partial(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.return_value = self._make_get_resp(
                {"code": 1000, "data": [{"ip": "1.2.3.4"}]}
            )
            result = pm.get_proxy()
        assert result is None

    def test_get_proxy_records_ip_stats(self):
        record_ip_mock = MagicMock()
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            with patch(f"{self.PATCH_PATH}.IP_STATS_ENABLED", True):
                with patch(f"{self.PATCH_PATH}.record_ip_usage", record_ip_mock):
                    from data.collect.proxy.proxy_manager import ProxyManager

                    pm = ProxyManager(
                        trade_date="2026-06-01", collector_name="test_stats"
                    )
                    mock_get.return_value = self._make_get_resp(
                        {"code": 1000, "data": [{"ip": "1.2.3.4", "port": 8888}]}
                    )
                    result = pm.get_proxy()

        assert result is not None
        record_ip_mock.assert_called_once_with(
            ip="1.2.3.4",
            port=8888,
            trade_date="2026-06-01",
            collector_name="test_stats",
            page=0,
            status="success",
            error=None,
        )

    def test_get_proxy_with_ip_stats_record_exception(self):
        def _raising(*a, **kw):
            raise RuntimeError("db error")

        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            with patch(f"{self.PATCH_PATH}.IP_STATS_ENABLED", True):
                with patch(f"{self.PATCH_PATH}.record_ip_usage", _raising):
                    from data.collect.proxy.proxy_manager import ProxyManager

                    pm = ProxyManager()
                    mock_get.return_value = self._make_get_resp(
                        {"code": 1000, "data": [{"ip": "1.2.3.4", "port": 8888}]}
                    )
                    result = pm.get_proxy()
        assert result is not None

    def test_test_proxy_success(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            result = pm.test_proxy({"http": "http://1.2.3.4:8080"})
        assert result is True

    def test_test_proxy_failure_status(self):
        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_resp = MagicMock()
            mock_resp.status_code = 403
            mock_get.return_value = mock_resp
            result = pm.test_proxy({"http": "http://1.2.3.4:8080"})
        assert result is False

    def test_test_proxy_exception(self):
        import requests

        with patch(f"{self.PATCH_PATH}.requests.get") as mock_get:
            pm = self._make_pm()
            mock_get.side_effect = requests.exceptions.ConnectionError("timeout")
            result = pm.test_proxy({"http": "http://1.2.3.4:8080"})
        assert result is False


# =====================================================================
# ProxyRequester
# =====================================================================


class TestProxyRequester:
    """ProxyRequester: base HTTP requester with proxy rotation and UA masking."""

    REQUIRER_PATH = "data.collect.proxy.proxy_requester"

    def _make_pr(self, trade_date=None, collector_name=None):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                from data.collect.proxy.proxy_requester import ProxyRequester

                return ProxyRequester(
                    trade_date=trade_date or "2026-06-02",
                    collector_name=collector_name or "test_req",
                )

    def test_init(self):
        pr = self._make_pr()
        assert pr.trade_date == "2026-06-02"
        assert pr.collector_name == "test_req"
        assert pr._page_seq == 0

    def test_get_proxy(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager") as mock_pm_cls:
                mock_pm = MagicMock()
                mock_pm.get_proxy.return_value = {
                    "http": "http://1.2.3.4:8080",
                    "https": "http://1.2.3.4:8080",
                }
                mock_pm_cls.return_value = mock_pm

                from data.collect.proxy.proxy_requester import ProxyRequester

                pr = ProxyRequester()
                result = pr._get_proxy()
                assert result == {
                    "http": "http://1.2.3.4:8080",
                    "https": "http://1.2.3.4:8080",
                }

    def test_get_proxy_none(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager") as mock_pm_cls:
                mock_pm = MagicMock()
                mock_pm.get_proxy.return_value = None
                mock_pm_cls.return_value = mock_pm

                from data.collect.proxy.proxy_requester import ProxyRequester

                pr = ProxyRequester()
                result = pr._get_proxy()
                assert result is None

    def test_extract_ip_port(self):
        from data.collect.proxy.proxy_requester import ProxyRequester

        ip, port = ProxyRequester._extract_ip_port({"http": "http://1.2.3.4:3128"})
        assert ip == "1.2.3.4"
        assert port == 3128

    def test_extract_ip_port_missing_port(self):
        from data.collect.proxy.proxy_requester import ProxyRequester

        ip, port = ProxyRequester._extract_ip_port({"http": "http://1.2.3.4"})
        assert ip == "1.2.3.4"
        assert port == 0

    def test_build_headers_from_profile_api(self):
        from data.collect.proxy.proxy_requester import UA_PROFILES, ProxyRequester

        profile = UA_PROFILES[0]
        headers = ProxyRequester._build_headers_from_profile(
            None, profile, referer="https://example.com", api_call=True
        )
        assert headers["User-Agent"] == profile["ua"]
        assert headers["sec-ch-ua"] == profile["sec_ch_ua"]
        assert headers["Referer"] == "https://example.com"
        assert headers["Sec-Fetch-Dest"] == "empty"
        assert headers["Sec-Fetch-Mode"] == "cors"

    def test_build_headers_from_profile_nav(self):
        from data.collect.proxy.proxy_requester import UA_PROFILES, ProxyRequester

        profile = UA_PROFILES[1]
        headers = ProxyRequester._build_headers_from_profile(
            None, profile, api_call=False
        )
        assert headers["Sec-Fetch-Dest"] == "document"
        assert headers["Sec-Fetch-Mode"] == "navigate"

    def test_request_with_session_returns_json(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                from data.collect.proxy.proxy_requester import ProxyRequester

                mock_sess = MagicMock()
                resp = MagicMock()
                resp.status_code = 200
                resp.text = '{"data": {"diff": [{"code": "000001"}]}}'
                resp.json.return_value = {"data": {"diff": [{"code": "000001"}]}}
                mock_sess.get.return_value = resp

                pr = ProxyRequester()
                result = pr._request(
                    url="https://api.example.com",
                    params={"key": "val"},
                    headers={"User-Agent": "test"},
                    proxy={"http": "http://1.2.3.4:8080"},
                    impersonate="chrome124",
                    session=mock_sess,
                )
                assert result == {"data": {"diff": [{"code": "000001"}]}}

    def test_request_without_session_creates_one(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session") as mock_session_cls:
            with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                from data.collect.proxy.proxy_requester import ProxyRequester

                mock_own_sess = MagicMock()
                resp = MagicMock()
                resp.status_code = 200
                resp.text = '{"ok": true}'
                resp.json.return_value = {"ok": True}
                mock_own_sess.get.return_value = resp
                mock_session_cls.return_value = mock_own_sess

                pr = ProxyRequester()
                result = pr._request(
                    url="https://api.example.com",
                    params={"key": "val"},
                    headers={"User-Agent": "test"},
                    proxy={"http": "http://1.2.3.4:8080"},
                    impersonate="chrome124",
                    timeout=15,
                )
                assert result == {"ok": True}
                mock_session_cls.assert_called_once()
                mock_own_sess.close.assert_called_once()

    def test_request_http_error_returns_none(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                from data.collect.proxy.proxy_requester import ProxyRequester

                mock_sess = MagicMock()
                resp = MagicMock()
                resp.status_code = 500
                resp.text = "Internal Server Error"
                mock_sess.get.return_value = resp

                pr = ProxyRequester()
                result = pr._request(
                    url="https://api.example.com",
                    params={},
                    headers={},
                    proxy={},
                    impersonate="chrome124",
                    session=mock_sess,
                )
                assert result is None

    def test_request_exception_returns_none(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                from data.collect.proxy.proxy_requester import ProxyRequester

                mock_sess = MagicMock()
                mock_sess.get.side_effect = RuntimeError("connection broken")

                pr = ProxyRequester()
                result = pr._request(
                    url="https://api.example.com",
                    params={},
                    headers={},
                    proxy={},
                    impersonate="chrome124",
                    session=mock_sess,
                )
                assert result is None

    def test_request_jsonp_parsing(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                from data.collect.proxy.proxy_requester import ProxyRequester

                mock_sess = MagicMock()
                resp = MagicMock()
                resp.status_code = 200
                resp.text = 'jQuery1234567890123_4567890123({"data": "ok"})'
                mock_sess.get.return_value = resp

                pr = ProxyRequester()
                result = pr._request(
                    url="https://api.example.com",
                    params={},
                    headers={},
                    proxy={},
                    impersonate="chrome124",
                    session=mock_sess,
                )
                assert result == {"data": "ok"}

    def test_request_with_retry_first_attempt_succeeds(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session") as mock_sess_cls:
            with patch(f"{self.REQUIRER_PATH}.ProxyManager") as mock_pm_cls:
                from data.collect.proxy.proxy_requester import ProxyRequester

                mock_pm = MagicMock()
                mock_pm.get_proxy.return_value = {
                    "http": "http://1.2.3.4:8080",
                    "https": "http://1.2.3.4:8080",
                }
                mock_pm_cls.return_value = mock_pm

                mock_own_sess = MagicMock()
                resp = MagicMock()
                resp.status_code = 200
                resp.text = '{"data": {"diff": [{"code": "000001"}]}}'
                resp.json.return_value = {"data": {"diff": [{"code": "000001"}]}}
                mock_own_sess.get.return_value = resp
                mock_sess_cls.return_value = mock_own_sess

                pr = ProxyRequester()
                result = pr._request_with_retry(
                    url="https://api.example.com",
                    params={"key": "val"},
                    desc="test",
                )
                assert result == {"data": {"diff": [{"code": "000001"}]}}
                assert pr._page_seq == 1

    def test_request_with_retry_all_fail(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager") as mock_pm_cls:
                from data.collect.proxy.proxy_requester import ProxyRequester

                mock_pm = MagicMock()
                mock_pm.get_proxy.return_value = {
                    "http": "http://1.2.3.4:8080",
                    "https": "http://1.2.3.4:8080",
                }
                mock_pm_cls.return_value = mock_pm

                pr = ProxyRequester()
                with patch.object(pr, "_request", return_value=None):
                    result = pr._request_with_retry(
                        url="https://api.example.com",
                        params={},
                        desc="test_fail",
                    )
                assert result is None

    def test_request_with_retry_no_proxy(self):
        with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
            with patch(f"{self.REQUIRER_PATH}.ProxyManager") as mock_pm_cls:
                from data.collect.proxy.proxy_requester import ProxyRequester

                mock_pm = MagicMock()
                mock_pm.get_proxy.return_value = None
                mock_pm_cls.return_value = mock_pm

                pr = ProxyRequester()
                result = pr._request_with_retry(
                    url="https://api.example.com",
                    params={},
                    desc="no_proxy",
                )
                assert result is None

    def test_record_ip(self):
        with patch(f"{self.REQUIRER_PATH}.IP_STATS_ENABLED", True):
            with patch(f"{self.REQUIRER_PATH}.record_ip_usage") as mock_rec:
                with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
                    with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                        from data.collect.proxy.proxy_requester import (
                            ProxyRequester,
                        )

                        pr = ProxyRequester()
                        pr._record_ip(
                            {"http": "http://1.2.3.4:8080"},
                            page=3,
                            status="success",
                        )
                        mock_rec.assert_called_once_with(
                            ip="1.2.3.4",
                            port=8080,
                            trade_date=pr.trade_date,
                            collector_name=pr.collector_name,
                            page=3,
                            status="success",
                            error=None,
                        )

    def test_record_ip_disabled(self):
        with patch(f"{self.REQUIRER_PATH}.IP_STATS_ENABLED", False):
            with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
                with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                    from data.collect.proxy.proxy_requester import (
                        ProxyRequester,
                    )

                    pr = ProxyRequester()
                    # Should not raise
                    pr._record_ip(
                        {"http": "http://1.2.3.4:8080"},
                        page=1,
                        status="failed",
                        error="timeout",
                    )

    def test_record_ip_exception_swallowed(self):
        def _raising(*a, **kw):
            raise ValueError("bad")

        with patch(f"{self.REQUIRER_PATH}.IP_STATS_ENABLED", True):
            with patch(f"{self.REQUIRER_PATH}.record_ip_usage", _raising):
                with patch(f"{self.REQUIRER_PATH}.curl_requests.Session"):
                    with patch(f"{self.REQUIRER_PATH}.ProxyManager"):
                        from data.collect.proxy.proxy_requester import (
                            ProxyRequester,
                        )

                        pr = ProxyRequester()
                        # Should not raise
                        pr._record_ip(
                            {"http": "http://1.2.3.4:8080"},
                            page=1,
                            status="success",
                        )


# =====================================================================
# ProxyBaseCollector
# =====================================================================


class TestProxyBaseCollector:
    """ProxyBaseCollector: caching, paging, and data collection base."""

    BASE_PATH = "data.collect.proxy.proxy_base_collector"
    REQ_PATH = "data.collect.proxy.proxy_requester"

    def _make_collector(self, cls_attrs=None, logger_name="test_coll", trade_date=None):
        """Create a minimal concrete ProxyBaseCollector subclass with patched deps."""
        # ProxyManager is created by ProxyRequester.__init__, patch it there
        with patch(f"{self.BASE_PATH}.curl_requests.Session"):
            with patch(f"{self.REQ_PATH}.ProxyManager"):
                with patch(f"{self.BASE_PATH}.get_collect_logger"):
                    from data.collect.proxy.proxy_base_collector import (
                        ProxyBaseCollector,
                    )

                    defaults = {
                        "API_URL": "https://api.test.com/endpoint",
                        "API_PARAMS": {"p1": "v1"},
                        "TABLE_NAME": "test_collector",
                        "PAGE_SIZE": 100,
                        "CACHE_FILE": "",
                        "REFERER_URL": "",
                        "DATABASE_PATH": ":memory:",
                    }
                    if cls_attrs:
                        defaults.update(cls_attrs)

                    cls = type("ConcreteCollector", (ProxyBaseCollector,), defaults)
                    return cls(
                        logger_name=logger_name,
                        trade_date=trade_date or "2026-06-03",
                    )

    def test_init(self):
        c = self._make_collector(trade_date="2026-06-03")
        assert c.trade_date == "2026-06-03"
        assert c.collector_name == "test_collector"

    def test_safe_float(self):
        c = self._make_collector()
        assert c._safe_float(None, 0.0) == 0.0
        assert c._safe_float("-", 0.0) == 0.0
        assert c._safe_float("", 0.0) == 0.0
        assert c._safe_float(3.14, 0.0) == 3.14
        assert c._safe_float("2.71", 0.0) == 2.71
        assert c._safe_float("abc", -1.0) == -1.0

    def test_load_cache_no_file(self):
        with patch(f"{self.BASE_PATH}.os.path.exists", return_value=False):
            with patch(f"{self.BASE_PATH}.CACHE_ENABLED", True):
                c = self._make_collector(trade_date="2026-06-03")
                cache = c._load_cache()
                assert cache["trade_date"] == "2026-06-03"
                assert cache["status"] == "incomplete"
                assert cache["data"] == []

    def test_is_cache_valid_mismatched_date(self):
        c = self._make_collector(trade_date="2026-06-04")
        c.cache_data["trade_date"] = "2026-06-03"
        with patch(f"{self.BASE_PATH}.CACHE_ENABLED", True):
            assert c._is_cache_valid() is False

    def test_is_cache_valid_expired(self):
        c = self._make_collector()
        c.cache_data["trade_date"] = c.trade_date
        c.cache_data["status"] = "incomplete"
        stale_time = (datetime.now() - timedelta(hours=200)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        c.cache_data["updated_at"] = stale_time

        with patch(f"{self.BASE_PATH}.CACHE_ENABLED", True):
            with patch(f"{self.BASE_PATH}.os.path.exists", return_value=True):
                assert c._is_cache_valid() is False

    def test_load_cache_disabled(self):
        with patch(f"{self.BASE_PATH}.CACHE_ENABLED", False):
            c = self._make_collector()
            cache = c._load_cache()
            assert cache["status"] == "incomplete"
            assert cache["data"] == []


# =====================================================================
# IPStatsManager
# =====================================================================


class TestIPStatsManager:
    """IPStatsManager: SQLite-based IP usage tracking."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.db_file = str(tmp_path / "test_ip_stats.db")
        with patch("data.collect.proxy.ip_stats.DB_PATH", self.db_file):
            yield

    def test_init_creates_tables(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        conn = sqlite3.connect(self.db_file)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {r[0] for r in tables}
        assert "ip_usage" in table_names
        assert "ip_details" in table_names
        conn.close()

    def test_record_usage(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="1.2.3.4",
            port=8080,
            trade_date="2026-06-01",
            collector_name="test_coll",
            page=1,
            status="success",
        )
        conn = sqlite3.connect(self.db_file)
        rows = conn.execute("SELECT * FROM ip_usage").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "1.2.3.4"
        conn.close()

    def test_record_usage_with_error(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="5.6.7.8",
            port=9999,
            trade_date="2026-06-01",
            collector_name="coll_a",
            page=2,
            status="failed",
            error="timeout",
        )
        conn = sqlite3.connect(self.db_file)
        row = conn.execute("SELECT ip, status, error FROM ip_usage").fetchone()
        assert row[0] == "5.6.7.8"
        assert row[1] == "failed"
        assert row[2] == "timeout"
        conn.close()

    def test_get_ip_usage_filter_trade_date(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        for i in range(5):
            mgr.record_usage(
                ip=f"1.2.3.{i}",
                port=8080,
                trade_date=f"2026-06-{i + 1:02d}",
                collector_name="coll",
                page=1,
                status="success",
            )
        records = mgr.get_ip_usage(trade_date="2026-06-01")
        assert len(records) == 1
        assert records[0]["ip"] == "1.2.3.0"

    def test_get_ip_usage_filter_collector(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="coll_a",
            page=1,
            status="success",
        )
        mgr.record_usage(
            ip="5.6.7.8",
            port=80,
            trade_date="2026-06-01",
            collector_name="coll_b",
            page=1,
            status="success",
        )
        records = mgr.get_ip_usage(collector_name="coll_a")
        assert len(records) == 1
        assert records[0]["ip"] == "1.2.3.4"

    def test_get_ip_usage_filter_ip(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="9.9.9.9",
            port=80,
            trade_date="2026-06-01",
            collector_name="coll",
            page=1,
            status="success",
        )
        mgr.record_usage(
            ip="8.8.8.8",
            port=80,
            trade_date="2026-06-01",
            collector_name="coll",
            page=1,
            status="success",
        )
        records = mgr.get_ip_usage(ip="9.9.9.9")
        assert len(records) == 1
        assert records[0]["ip"] == "9.9.9.9"

    def test_get_ip_stats(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="c1",
            page=1,
            status="success",
        )
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="c1",
            page=2,
            status="success",
        )
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="c1",
            page=3,
            status="failed",
        )
        stats = mgr.get_ip_stats(trade_date="2026-06-01")
        assert len(stats) == 1
        assert stats[0]["total_requests"] == 3
        assert stats[0]["success_count"] == 2
        assert stats[0]["failed_count"] == 1

    def test_get_collector_stats(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="coll_a",
            page=1,
            status="success",
        )
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="coll_a",
            page=2,
            status="success",
        )
        mgr.record_usage(
            ip="5.6.7.8",
            port=80,
            trade_date="2026-06-01",
            collector_name="coll_b",
            page=1,
            status="failed",
        )
        stats = mgr.get_collector_stats(trade_date="2026-06-01")
        stats_map = {s["collector_name"]: s for s in stats}
        assert stats_map["coll_a"]["total_pages"] == 2
        assert stats_map["coll_b"]["total_pages"] == 1

    def test_update_ip_detail(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.update_ip_detail(
            ip="1.2.3.4",
            country="中国",
            province="北京",
            city="北京",
            isp="电信",
        )
        conn = sqlite3.connect(self.db_file)
        row = conn.execute("SELECT * FROM ip_details WHERE ip='1.2.3.4'").fetchone()
        assert row is not None
        assert row[1] == "中国"
        conn.close()

    def test_get_daily_report(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="c1",
            page=1,
            status="success",
        )
        report = mgr.get_daily_report("2026-06-01")
        assert report["date"] == "2026-06-01"
        assert report["basic"]["unique_ips"] == 1
        assert report["basic"]["total_requests"] == 1

    def test_clear_old_data(self):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2020-01-01",
            collector_name="old",
            page=1,
            status="success",
        )
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="new",
            page=1,
            status="success",
        )
        deleted = mgr.clear_old_data(days=30)
        assert deleted == 1
        remaining = mgr.get_ip_usage()
        assert len(remaining) == 1
        assert remaining[0]["trade_date"] == "2026-06-01"

    def test_export_to_json(self, tmp_path):
        from data.collect.proxy.ip_stats import IPStatsManager

        mgr = IPStatsManager()
        mgr.record_usage(
            ip="1.2.3.4",
            port=80,
            trade_date="2026-06-01",
            collector_name="c1",
            page=1,
            status="success",
        )
        out_file = str(tmp_path / "export_test.json")
        result = mgr.export_to_json(trade_date="2026-06-01", output_file=out_file)
        assert result == out_file
        with open(out_file, encoding="utf-8") as f:
            data = json.load(f)
        assert data["trade_date"] == "2026-06-01"
        assert len(data["usage_records"]) == 1

    def test_record_ip_usage_shortcut(self, tmp_path):
        """Use local tmp_path and reset singleton to ensure fresh DB state."""
        from data.collect.proxy import ip_stats as ip_stats_mod
        from data.collect.proxy.ip_stats import record_ip_usage

        # Reset the module-level singleton so it picks up our patched DB_PATH
        ip_stats_mod._stats_instance = None

        db_file = str(tmp_path / "test_shortcut.db")
        with patch("data.collect.proxy.ip_stats.DB_PATH", db_file):
            record_ip_usage(
                ip="9.9.9.9",
                port=8888,
                trade_date="2026-06-01",
                collector_name="shortcut",
                page=1,
                status="success",
            )

        conn = sqlite3.connect(db_file)
        row = conn.execute("SELECT ip, port FROM ip_usage").fetchone()
        assert row[0] == "9.9.9.9"
        assert row[1] == 8888
        conn.close()


# =====================================================================
# TelegraphCollector
# =====================================================================


class TestTelegraphCollector:
    """TelegraphCollector: CLS telegraph data collection and AI structuring."""

    TEL_PATH = "data.collect.events.telegraph_collector"

    def _make_tc(self, db_path=":memory:"):
        with patch(f"{self.TEL_PATH}.requests.Session"):
            with patch(f"{self.TEL_PATH}.get_collect_logger"):
                from data.collect.events.telegraph_collector import (
                    TelegraphCollector,
                )

                return TelegraphCollector(db_path=db_path)

    def test_init(self):
        tc = self._make_tc(db_path=":memory:")
        assert tc.db_path == ":memory:"

    def test_fetch_telegraph_list(self):
        with patch(f"{self.TEL_PATH}.requests.Session") as mock_session_cls:
            with patch(f"{self.TEL_PATH}.get_collect_logger"):
                from data.collect.events.telegraph_collector import (
                    TelegraphCollector,
                )

                mock_sess = MagicMock()
                mock_resp = MagicMock()
                mock_resp.json.return_value = {
                    "data": {
                        "roll_data": [
                            {"id": 1, "title": "Test", "level": "A"},
                            {"id": 2, "title": "Test2", "level": "B"},
                        ]
                    }
                }
                mock_sess.get.return_value = mock_resp
                mock_session_cls.return_value = mock_sess

                tc = TelegraphCollector(db_path=":memory:")
                result = tc._fetch_telegraph_list()
                assert len(result) == 2

    def test_fetch_telegraph_list_exception(self):
        with patch(f"{self.TEL_PATH}.requests.Session") as mock_session_cls:
            with patch(f"{self.TEL_PATH}.get_collect_logger"):
                from data.collect.events.telegraph_collector import (
                    TelegraphCollector,
                )

                mock_sess = MagicMock()
                mock_sess.get.side_effect = RuntimeError("network error")
                mock_session_cls.return_value = mock_sess

                tc = TelegraphCollector(db_path=":memory:")
                result = tc._fetch_telegraph_list()
                assert result == []

    def test_fetch_article_detail(self):
        with patch(f"{self.TEL_PATH}.requests.Session") as mock_session_cls:
            with patch(f"{self.TEL_PATH}.get_collect_logger"):
                from data.collect.events.telegraph_collector import (
                    TelegraphCollector,
                )

                mock_sess = MagicMock()
                resp = MagicMock()
                resp.text = '<div class="detail-content">正文内容<br>第二行</div>'
                mock_sess.get.return_value = resp
                mock_session_cls.return_value = mock_sess

                tc = TelegraphCollector(db_path=":memory:")
                result = tc._fetch_article_detail("12345")
                assert "正文内容" in result
                assert result == "正文内容\n第二行"

    def test_fetch_article_detail_no_match(self):
        with patch(f"{self.TEL_PATH}.requests.Session") as mock_session_cls:
            with patch(f"{self.TEL_PATH}.get_collect_logger"):
                from data.collect.events.telegraph_collector import (
                    TelegraphCollector,
                )

                mock_sess = MagicMock()
                resp = MagicMock()
                resp.text = "<html><body>no detail</body></html>"
                mock_sess.get.return_value = resp
                mock_session_cls.return_value = mock_sess

                tc = TelegraphCollector(db_path=":memory:")
                result = tc._fetch_article_detail("99999")
                assert result == ""

    def test_format_stock_tags(self):
        from data.collect.events.telegraph_collector import TelegraphCollector

        stock_list = [
            {"StockID": "sh600519", "name": "贵州茅台"},
            {"StockID": "sz000001", "name": "平安银行"},
        ]
        result = TelegraphCollector._format_stock_tags(stock_list)
        assert result == [
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000001", "name": "平安银行"},
        ]

    def test_format_stock_tags_empty(self):
        from data.collect.events.telegraph_collector import TelegraphCollector

        assert TelegraphCollector._format_stock_tags([]) == []
        assert TelegraphCollector._format_stock_tags(None) == []

    def test_format_subject_tags(self):
        from data.collect.events.telegraph_collector import TelegraphCollector

        subjects = [
            {"subject_name": "半导体"},
            {"subject_name": "新能源"},
        ]
        result = TelegraphCollector._format_subject_tags(subjects)
        assert result == ["半导体", "新能源"]

    def test_format_plate_tags(self):
        from data.collect.events.telegraph_collector import TelegraphCollector

        plates = [{"plate_name": "半导体板块"}, {"name": "新能源板块"}]
        result = TelegraphCollector._format_plate_tags(plates)
        assert result == ["半导体板块", "新能源板块"]

    def test_derive_category(self):
        from data.collect.events.telegraph_collector import TelegraphCollector

        assert (
            TelegraphCollector._derive_category(["期货市场情报", "有色金属"])
            == "有色金属"
        )
        assert (
            TelegraphCollector._derive_category(["互动平台精选", "期货市场情报"])
            == "互动平台精选"
        )
        assert TelegraphCollector._derive_category([]) == "其他"

    def test_score(self):
        from data.collect.events.telegraph_collector import TelegraphCollector

        assert TelegraphCollector._score("A", 0) == 5
        assert TelegraphCollector._score("B", 0) == 3
        assert TelegraphCollector._score("C", 0) == 0
        assert TelegraphCollector._score("A", 1_000_000) == 8  # 5 + 3
        assert TelegraphCollector._score("B", 300_000) == 5  # 3 + 2
        assert TelegraphCollector._score("C", 80_000) == 1  # 0 + 1

    def test_is_noise_telegraph(self):
        from data.collect.events.telegraph_collector import TelegraphCollector

        assert (
            TelegraphCollector._is_noise_telegraph(
                {
                    "category": "盘面直播",
                    "title": "收评：三大指数...",
                    "level": "C",
                }
            )
            is True
        )
        assert (
            TelegraphCollector._is_noise_telegraph(
                {
                    "category": "盘面直播",
                    "title": "涨停分析：芯片板块...",
                    "level": "A",
                }
            )
            is False
        )
        assert (
            TelegraphCollector._is_noise_telegraph(
                {"category": "行业观察", "title": "半导体涨价"}
            )
            is False
        )

    def test_collect_success(self, tmp_path):
        db_file = str(tmp_path / "test_telegraph.db")
        conn = sqlite3.connect(db_file)
        conn.execute(
            """
            CREATE TABLE cls_telegraph (
                telegraph_id TEXT PRIMARY KEY, trade_date TEXT, ctime INTEGER,
                level TEXT, title TEXT, content TEXT, reading_num INTEGER,
                stock_tags TEXT, subject_tags TEXT, plate_tags TEXT,
                category TEXT, score INTEGER, content_hash TEXT,
                ai_summary TEXT, ai_sentiment TEXT, ai_impact TEXT,
                ai_stocks TEXT, ai_sectors TEXT, ai_importance INTEGER,
                ai_direction TEXT, ai_status TEXT, created_at TEXT)
        """
        )
        conn.close()

        with patch(f"{self.TEL_PATH}.requests.Session") as mock_session_cls:
            with patch(f"{self.TEL_PATH}.get_collect_logger"):
                from data.collect.events.telegraph_collector import (
                    TelegraphCollector,
                )

                mock_sess = MagicMock()
                mock_resp = MagicMock()
                mock_resp.json.return_value = {
                    "data": {
                        "roll_data": [
                            {
                                "id": 1001,
                                "title": "重要公告",
                                "level": "A",
                                "brief": "这是一条重要公告",
                                "ctime": 1750000000,
                                "reading_num": 500000,
                                "stock_list": [
                                    {"StockID": "sh600519", "name": "贵州茅台"}
                                ],
                                "subjects": [{"subject_name": "白酒"}],
                                "plate_list": [],
                            },
                        ]
                    }
                }
                mock_sess.get.return_value = mock_resp
                mock_session_cls.return_value = mock_sess

                tc = TelegraphCollector(db_path=db_file)
                with patch.object(tc, "_ai_structure_batch") as mock_ai:
                    result = tc.collect(trade_date="2026-06-05")

        assert result["success"] is True
        assert len(result["data"]) == 1
        assert result["data"][0]["telegraph_id"] == "1001"
        assert result["data"][0]["score"] == 8
        mock_ai.assert_called_once()

    def test_collect_empty_list(self):
        with patch(f"{self.TEL_PATH}.requests.Session") as mock_session_cls:
            with patch(f"{self.TEL_PATH}.get_collect_logger"):
                from data.collect.events.telegraph_collector import (
                    TelegraphCollector,
                )

                mock_sess = MagicMock()
                mock_resp = MagicMock()
                mock_resp.json.return_value = {"data": {"roll_data": []}}
                mock_sess.get.return_value = mock_resp
                mock_session_cls.return_value = mock_sess

                tc = TelegraphCollector(db_path=":memory:")
                result = tc.collect(trade_date="2026-06-05")
                assert result["success"] is False
                assert result["count"] == 0

    def test_collect_fetch_exception(self):
        with patch(f"{self.TEL_PATH}.requests.Session") as mock_session_cls:
            with patch(f"{self.TEL_PATH}.get_collect_logger"):
                from data.collect.events.telegraph_collector import (
                    TelegraphCollector,
                )

                mock_sess = MagicMock()
                mock_sess.get.side_effect = RuntimeError("fail")
                mock_session_cls.return_value = mock_sess

                tc = TelegraphCollector(db_path=":memory:")
                result = tc.collect(trade_date="2026-06-05")
                assert result["success"] is False

    def test_ai_structure_batch_skips_noise(self, tmp_path):
        db_file = str(tmp_path / "test_ai.db")
        conn = sqlite3.connect(db_file)
        conn.execute(
            """
            CREATE TABLE cls_telegraph (
                telegraph_id TEXT PRIMARY KEY, trade_date TEXT, ctime INTEGER,
                level TEXT, title TEXT, content TEXT, reading_num INTEGER,
                stock_tags TEXT, subject_tags TEXT, plate_tags TEXT,
                category TEXT, score INTEGER, content_hash TEXT,
                ai_summary TEXT, ai_sentiment TEXT, ai_impact TEXT,
                ai_stocks TEXT, ai_sectors TEXT, ai_importance INTEGER,
                ai_direction TEXT, ai_status TEXT, created_at TEXT)
        """
        )
        conn.execute(
            """
            INSERT INTO cls_telegraph (telegraph_id, trade_date, ctime, level,
                title, content, reading_num, category, score, content_hash, ai_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "999",
                "2026-06-05",
                1750000000,
                "C",
                "收评：三大指数集体收跌",
                "收评内容",
                50000,
                "盘面直播",
                0,
                "hash1",
                "pending",
            ),
        )
        conn.commit()
        conn.close()

        tc = self._make_tc(db_path=db_file)
        with patch.object(tc, "_mark_skipped") as mock_skipped:
            with patch.object(tc, "_mark_failed") as mock_failed:
                tc._ai_structure_batch(["999"], "2026-06-05")

        mock_skipped.assert_called_once_with(["999"])
        mock_failed.assert_not_called()

    def test_ai_structure_batch_success(self, tmp_path):
        db_file = str(tmp_path / "test_ai2.db")
        conn = sqlite3.connect(db_file)
        conn.execute(
            """
            CREATE TABLE cls_telegraph (
                telegraph_id TEXT PRIMARY KEY, trade_date TEXT, ctime INTEGER,
                level TEXT, title TEXT, content TEXT, reading_num INTEGER,
                stock_tags TEXT, subject_tags TEXT, plate_tags TEXT,
                category TEXT, score INTEGER, content_hash TEXT,
                ai_summary TEXT, ai_sentiment TEXT, ai_impact TEXT,
                ai_stocks TEXT, ai_sectors TEXT, ai_importance INTEGER,
                ai_direction TEXT, ai_status TEXT, created_at TEXT)
        """
        )
        conn.execute(
            """
            INSERT INTO cls_telegraph (telegraph_id, trade_date, ctime, level,
                title, content, reading_num, category, score, content_hash, ai_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2001",
                "2026-06-05",
                1750000000,
                "A",
                "半导体行业利好",
                "内容详情",
                500000,
                "行业",
                8,
                "hash2",
                "pending",
            ),
        )
        conn.commit()
        conn.close()

        mock_ai_response = json.dumps(
            [
                {
                    "telegraph_id": "2001",
                    "ai_summary": "半导体利好",
                    "ai_sentiment": "利好",
                    "ai_impact": "利好半导体板块",
                    "ai_stocks": [{"code": "002371", "name": "北方华创"}],
                    "ai_sectors": [{"sector_code": "BK0717", "sector_name": "半导体"}],
                    "ai_importance": 4,
                    "ai_direction": "行业",
                }
            ]
        )

        tc = self._make_tc(db_path=db_file)

        # The AI service object (system.ai.ai) — set up its return value
        ai_service_mock = MagicMock()
        ai_service_mock.chat_with_tools_raw.return_value = {
            "content": mock_ai_response,
            "tool_calls": [],
        }

        # The system.ai module — "from system.ai import ai" gets `ai` attr
        ai_module_mock = MagicMock()
        ai_module_mock.ai = ai_service_mock

        # All AI imports happen inside _ai_structure_batch's function body.
        # Pre-populate sys.modules so those imports find our mocks.
        mock_prompts = MagicMock()
        mock_prompts.TELEGRAPH_STRUCTURE_PROMPT = "{telegraphs}"
        mock_prompts.TELEGRAPH_AI_SYSTEM = ""
        mock_prompts.TELEGRAPH_FC_TOOLS = []

        mock_stock_tools = MagicMock()
        mock_fc_engine = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "system.ai": ai_module_mock,
                "system.ai.prompts": MagicMock(),
                "system.ai.prompts.telegraph": mock_prompts,
                "system.ai.stock_tools": mock_stock_tools,
                "system.ai.function_calling": mock_fc_engine,
            },
        ):
            tc._ai_structure_batch(["2001"], "2026-06-05")

        conn = sqlite3.connect(db_file)
        row = conn.execute(
            "SELECT ai_summary, ai_sentiment, ai_status FROM cls_telegraph WHERE telegraph_id='2001'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "半导体利好"
        assert row[1] == "利好"
        assert row[2] == "done"

    def test_ai_structure_batch_no_new_ids(self, tmp_path):
        db_file = str(tmp_path / "test_ai3.db")
        conn = sqlite3.connect(db_file)
        conn.execute(
            """
            CREATE TABLE cls_telegraph (
                telegraph_id TEXT PRIMARY KEY, trade_date TEXT, ctime INTEGER,
                level TEXT, title TEXT, content TEXT, reading_num INTEGER,
                stock_tags TEXT, subject_tags TEXT, plate_tags TEXT,
                category TEXT, score INTEGER, content_hash TEXT,
                ai_summary TEXT, ai_sentiment TEXT, ai_impact TEXT,
                ai_stocks TEXT, ai_sectors TEXT, ai_importance INTEGER,
                ai_direction TEXT, ai_status TEXT, created_at TEXT)
        """
        )
        conn.close()

        tc = self._make_tc(db_path=db_file)
        # Should not raise
        tc._ai_structure_batch([], "2026-06-05")

    def test_get_for_review(self, tmp_path):
        db_file = str(tmp_path / "test_review.db")
        conn = sqlite3.connect(db_file)
        conn.execute(
            """
            CREATE TABLE cls_telegraph (
                telegraph_id TEXT PRIMARY KEY, trade_date TEXT, ctime INTEGER,
                level TEXT, title TEXT, content TEXT, reading_num INTEGER,
                stock_tags TEXT, subject_tags TEXT, plate_tags TEXT,
                category TEXT, score INTEGER, content_hash TEXT,
                ai_summary TEXT, ai_sentiment TEXT, ai_impact TEXT,
                ai_stocks TEXT, ai_sectors TEXT, ai_importance INTEGER,
                ai_direction TEXT, ai_status TEXT, created_at TEXT)
        """
        )
        conn.execute(
            """
            INSERT INTO cls_telegraph (telegraph_id, trade_date, ctime, level,
                title, content, reading_num, stock_tags, subject_tags,
                plate_tags, category, score, content_hash)
            VALUES ('1', '2026-06-05', 1750000000, 'A', 'High', 'c', 500000,
                '[]', '[]', '[]', '行业', 8, 'h1')
        """
        )
        conn.execute(
            """
            INSERT INTO cls_telegraph (telegraph_id, trade_date, ctime, level,
                title, content, reading_num, stock_tags, subject_tags,
                plate_tags, category, score, content_hash)
            VALUES ('2', '2026-06-05', 1750000001, 'C', 'Low', 'c', 1000,
                '[]', '[]', '[]', '其他', 1, 'h2')
        """
        )
        conn.commit()
        conn.close()

        tc = self._make_tc(db_path=db_file)
        results = tc.get_for_review("2026-06-05", min_score=3)
        assert len(results) == 1
        assert results[0]["telegraph_id"] == "1"

    def test_get_stats(self, tmp_path):
        db_file = str(tmp_path / "test_stats.db")
        conn = sqlite3.connect(db_file)
        conn.execute(
            """
            CREATE TABLE cls_telegraph (
                telegraph_id TEXT PRIMARY KEY, trade_date TEXT, ctime INTEGER,
                level TEXT, title TEXT, content TEXT, reading_num INTEGER,
                stock_tags TEXT, subject_tags TEXT, plate_tags TEXT,
                category TEXT, score INTEGER, content_hash TEXT,
                ai_summary TEXT, ai_sentiment TEXT, ai_impact TEXT,
                ai_stocks TEXT, ai_sectors TEXT, ai_importance INTEGER,
                ai_direction TEXT, ai_status TEXT, created_at TEXT)
        """
        )
        conn.execute(
            "INSERT INTO cls_telegraph (telegraph_id, trade_date, level, score) VALUES ('1', '2026-06-05', 'A', 5)"
        )
        conn.execute(
            "INSERT INTO cls_telegraph (telegraph_id, trade_date, level, score) VALUES ('2', '2026-06-05', 'B', 1)"
        )
        conn.commit()
        conn.close()

        tc = self._make_tc(db_path=db_file)
        stats = tc.get_stats("2026-06-05")
        assert stats["total"] == 2
        assert stats["a_count"] == 1
        assert stats["b_count"] == 1
        assert stats["c_count"] == 0


# =====================================================================
# MacroCollector
# =====================================================================


class TestMacroCollector:
    """MacroCollector: global macro data (US markets, FX, A50, oil, gold).
    Requires akshare/yfinance — skipped by default, run with: pytest -m external
    """

    MACRO_PATH = "data.collect.macro.macro_collector"

    def test_init(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session"):
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mc = MacroCollector(timeout=15)
                assert mc.timeout == 15

    def test_collect_all_structure(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session"):
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mc = MacroCollector()
                # Mock all sub-methods
                mc.get_us_market = MagicMock(
                    return_value={"nasdaq": {"price": 18000, "change": 1.5}}
                )
                mc.get_exchange_rate = MagicMock(
                    return_value={"usd_cny": {"rate": 7.25}}
                )
                mc.get_a50_futures = MagicMock(
                    return_value={"price": 13500, "change": 0.5}
                )
                mc.get_crude_oil = MagicMock(
                    return_value={"price": 75.0, "change": -0.3}
                )
                mc.get_gold = MagicMock(return_value={"price": 2700, "change": 0.8})

                result = mc.collect_all()
                assert "us_market" in result
                assert "exchange_rate" in result
                assert "a50_futures" in result
                assert "crude_oil" in result
                assert "gold" in result
                assert "timestamp" in result

    def test_get_us_market(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session") as mock_session_cls:
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mock_sess = MagicMock()
                mock_session_cls.return_value = mock_sess

                nasdaq_resp = MagicMock()
                nasdaq_resp.json.return_value = {
                    "chart": {
                        "result": [
                            {
                                "meta": {
                                    "chartPreviousClose": 17800,
                                    "regularMarketPrice": 18000,
                                }
                            }
                        ]
                    }
                }

                kweb_resp = MagicMock()
                kweb_resp.json.return_value = {
                    "chart": {
                        "result": [
                            {
                                "meta": {
                                    "chartPreviousClose": 30.0,
                                    "regularMarketPrice": 31.5,
                                    "regularMarketChangePercent": 5.0,
                                }
                            }
                        ]
                    }
                }

                # MacroCollector.__init__ calls self.session.get() once for warmup,
                # then get_us_market makes 2 calls (nasdaq + KWEB).
                # Warmup exception is silently caught.
                mock_sess.get.side_effect = [
                    RuntimeError("warmup fails"),
                    nasdaq_resp,
                    kweb_resp,
                ]

                mc = MacroCollector()
                result = mc.get_us_market()
                assert result is not None
                assert result["nasdaq"]["price"] == 18000
                assert result["nasdaq"]["change"] == pytest.approx(1.12, abs=0.1)
                assert result["china_etf"]["price"] == 31.5
                assert result["china_etf"]["change"] == 5.0

    def test_get_us_market_exception(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session") as mock_session_cls:
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mock_sess = MagicMock()
                mock_sess.get.side_effect = RuntimeError("Yahoo API error")
                mock_session_cls.return_value = mock_sess

                mc = MacroCollector()
                result = mc.get_us_market()
                assert result is None

    def test_get_exchange_rate_akshare(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session"):
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mc = MacroCollector()

                # Mock akshare DataFrame result
                mock_df = MagicMock()
                mock_df.empty = False
                last_row = MagicMock()
                last_row.__getitem__ = MagicMock(return_value=725.0)
                mock_df.iloc.__getitem__ = MagicMock(return_value=last_row)

                mock_ak = MagicMock()
                mock_ak.currency_boc_sina = MagicMock(return_value=mock_df)

                # get_exchange_rate imports get_akshare inside function body,
                # so patch at the SOURCE module
                with patch(
                    "system.config.akshare_config.get_akshare", return_value=mock_ak
                ):
                    result = mc.get_exchange_rate()

                assert result is not None
                assert result["usd_cny"]["rate"] == 7.25

    def test_get_exchange_rate_fallback(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session"):
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mc = MacroCollector()
                with patch(
                    "system.config.akshare_config.get_akshare",
                    side_effect=ImportError("no akshare"),
                ):
                    result = mc.get_exchange_rate()
                assert result["usd_cny"]["rate"] == 7.25

    def test_get_a50_futures_success(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session") as mock_session_cls:
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mock_sess = MagicMock()
                mock_session_cls.return_value = mock_sess

                a50_resp = MagicMock()
                a50_resp.status_code = 200
                a50_resp.json.return_value = {"data": {"f43": 135000, "f170": 50}}
                mock_sess.get.return_value = a50_resp

                mc = MacroCollector()
                result = mc.get_a50_futures()
                assert result["price"] == 13500.0
                assert result["change"] == 0.5

    def test_get_a50_futures_tencent_fallback(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session") as mock_session_cls:
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mock_sess = MagicMock()
                mock_session_cls.return_value = mock_sess

                a50_empty_resp = MagicMock()
                a50_empty_resp.status_code = 200
                a50_empty_resp.json.return_value = {"data": None}
                mock_sess.get.return_value = a50_empty_resp

                mc = MacroCollector()
                result = mc.get_a50_futures()
                # Falls back to hardcoded
                assert result["price"] == 13500.0
                assert result.get("_fallback") is True

    def test_get_crude_oil(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session") as mock_session_cls:
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mock_sess = MagicMock()
                mock_session_cls.return_value = mock_sess

                oil_resp = MagicMock()
                # Tencent format: split by ~, index 3=price, index 32=change
                parts = ["v"] * 100
                parts[3] = "75.50"
                parts[32] = "-1.5"
                oil_resp.text = "~".join(parts)
                oil_resp.status_code = 200
                mock_sess.get.return_value = oil_resp

                mc = MacroCollector()
                result = mc.get_crude_oil()
                assert result["price"] == 75.50
                assert result["change"] == -1.5

    def test_get_crude_oil_fallback(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session") as mock_session_cls:
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mock_sess = MagicMock()
                mock_sess.get.side_effect = RuntimeError("Tencent API error")
                mock_session_cls.return_value = mock_sess

                mc = MacroCollector()
                result = mc.get_crude_oil()
                assert result["price"] == 75.0
                assert result.get("_fallback") is True

    def test_get_gold_yfinance(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session"):
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mc = MacroCollector()

                # Mock yfinance module in sys.modules so the function-body
                # "import yfinance as yf" picks it up
                mock_yfin = MagicMock()

                class _CloseIdx:
                    def __init__(self, vals):
                        self._vals = vals

                    @property
                    def iloc(self):
                        return self

                    def __getitem__(self, i):
                        return self._vals[i]

                mock_hist = MagicMock()
                mock_hist.empty = False
                mock_hist.__len__.return_value = 2
                mock_hist.__getitem__.return_value = _CloseIdx([2680.0, 2700.0])

                mock_ticker = MagicMock()
                mock_ticker.history.return_value = mock_hist
                mock_yfin.Ticker.return_value = mock_ticker

                with patch.dict("sys.modules", {"yfinance": mock_yfin}):
                    result = mc.get_gold()

                assert result is not None
                assert result["price"] == 2700.0
                assert result["change"] == pytest.approx(0.75, abs=0.1)

    def test_get_gold_fallback(self):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session") as mock_session_cls:
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                mock_sess = MagicMock()
                mock_session_cls.return_value = mock_sess

                mc = MacroCollector()

                mock_yfin = MagicMock()
                mock_yfin.Ticker.side_effect = ImportError("no yfinance")

                with patch.dict("sys.modules", {"yfinance": mock_yfin}):
                    with patch.object(mc, "session") as mock_sess:
                        mock_sess.get.side_effect = RuntimeError("tencent also fails")
                        result = mc.get_gold()

                assert result["price"] == 2700.0
                assert result.get("_fallback") is True

    def test_save_to_db(self, tmp_path):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session"):
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                db_file = str(tmp_path / "test_macro.db")

                macro_data = {
                    "us_market": {
                        "nasdaq": {"price": 18000, "change": 1.5},
                        "china_etf": {"price": 31.5, "change": 5.0},
                    },
                    "exchange_rate": {"usd_cny": {"rate": 7.25, "change": 0}},
                    "a50_futures": {"price": 13500, "change": 0.5},
                    "crude_oil": {"price": 75.0, "change": -1.0},
                    "gold": {"price": 2700, "change": 0.8},
                    "timestamp": "2026-06-05T10:00:00",
                }

                # save_to_db imports DATABASE_PATH from system.config.settings
                # inside the function body — patch at source.
                with patch("system.config.settings.DATABASE_PATH", db_file):
                    MacroCollector.save_to_db(macro_data, trade_date="2026-06-05")

                conn = sqlite3.connect(db_file)
                row = conn.execute(
                    "SELECT * FROM macro_daily WHERE trade_date='2026-06-05'"
                ).fetchone()
                conn.close()
                assert row is not None
                assert row[1] == "2026-06-05"
                assert row[2] == 1.5  # nasdaq_change
                assert row[3] == 5.0  # kweb_change
                assert row[4] == 7.25  # usd_cny_rate
                assert row[5] == 13500.0  # a50_price
                assert row[6] == 0.5  # a50_change
                assert row[7] == 75.0  # crude_oil_price
                assert row[8] == -1.0  # crude_oil_change
                assert row[9] == 2700.0  # gold_price
                assert row[10] == 0.8  # gold_change

    def test_fetch_and_save(self, tmp_path):
        with patch(f"{self.MACRO_PATH}.curl_requests.Session"):
            with patch(f"{self.MACRO_PATH}.get_collect_logger"):
                from data.collect.macro.macro_collector import MacroCollector

                db_file = str(tmp_path / "test_fs.db")

                mc = MacroCollector()
                mc.collect_all = MagicMock(
                    return_value={
                        "us_market": {"nasdaq": {"price": 18000, "change": 1.0}},
                        "exchange_rate": {"usd_cny": {"rate": 7.25}},
                        "a50_futures": {"price": 13500, "change": 0.5},
                        "crude_oil": {"price": 75.0, "change": 0.0},
                        "gold": {"price": 2700, "change": 0.5},
                        "timestamp": "2026-06-05T10:00:00",
                    }
                )

                with patch("system.config.settings.DATABASE_PATH", db_file):
                    result = mc.fetch_and_save()

                assert result["us_market"]["nasdaq"]["price"] == 18000

                conn = sqlite3.connect(db_file)
                row = conn.execute("SELECT * FROM macro_daily").fetchone()
                conn.close()
                assert row is not None
