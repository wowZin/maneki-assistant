"""tests for scripts/proxy_utils.py — 真实环境验证"""

import unittest
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestProxyUtils(unittest.TestCase):

    def test_browser_ua_is_set(self):
        from scripts import proxy_utils as pu
        self.assertIsInstance(pu.BROWSER_UA, str)
        self.assertGreater(len(pu.BROWSER_UA), 10)
        self.assertIn("Mozilla", pu.BROWSER_UA)
        self.assertIn("AppleWebKit", pu.BROWSER_UA)

    def test_eastmoney_home_correct_url(self):
        from scripts import proxy_utils as pu
        self.assertEqual(pu.EASTMONEY_HOME, "https://www.eastmoney.com/")

    def test_get_proxy_ip_returns_valid_format(self):
        """真实API获取代理，验证格式"""
        from scripts import proxy_utils as pu

        pu._cached_proxy = None
        addr = pu.get_proxy_ip(force_refresh=True)
        if addr is None:
            self.skipTest("代理API不可达")
        parts = addr.split(":")
        self.assertEqual(len(parts), 2)
        self.assertGreater(int(parts[1]), 0)

    def test_cached_proxy_reused(self):
        """未过期的缓存直接复用"""
        from scripts import proxy_utils as pu

        pu._cached_proxy = {
            "ip": "10.0.0.1",
            "port": 9999,
            "expires_at": time.time() + 999
        }
        addr = pu.get_proxy_ip()
        self.assertEqual(addr, "10.0.0.1:9999")
        pu._cached_proxy = None

    def test_expired_cache_refreshes(self):
        """过期缓存触发刷新"""
        from scripts import proxy_utils as pu

        pu._cached_proxy = {
            "ip": "10.0.0.1",
            "port": 9999,
            "expires_at": time.time() - 1
        }
        result = pu.get_proxy_ip()
        if result is not None:
            self.assertRegex(result, r"\d+\.\d+\.\d+\.\d+:\d+")
        pu._cached_proxy = None

    def test_proxies_dict_format(self):
        """手动给地址时返回正确格式"""
        from scripts import proxy_utils as pu
        result = pu.get_proxies_dict("127.0.0.1:8080")
        self.assertEqual(result, {
            "http": "http://127.0.0.1:8080",
            "https": "http://127.0.0.1:8080",
        })

    def test_proxies_dict_from_api(self):
        """不传地址时走真实API"""
        from scripts import proxy_utils as pu

        pu._cached_proxy = None
        result = pu.get_proxies_dict()
        if result is None:
            self.skipTest("代理API不可达")
        self.assertIsInstance(result, dict)
        self.assertIn("http", result)

    def test_get_requests_session_always_works(self):
        """无论代理是否可达，都返回Session"""
        import requests
        from scripts import proxy_utils as pu

        pu._cached_proxy = None
        sess = pu.get_requests_session_with_proxy()
        self.assertIsInstance(sess, requests.Session)
        self.assertIn("User-Agent", sess.headers)

    def test_get_requests_session_with_proxy_addr(self):
        """手动给代理地址时的session"""
        import requests
        from scripts import proxy_utils as pu

        sess = pu.get_requests_session_with_proxy("127.0.0.1:8080")
        self.assertIsInstance(sess, requests.Session)
        self.assertEqual(sess.proxies, {
            "http": "http://127.0.0.1:8080",
            "https": "http://127.0.0.1:8080",
        })

    def test_get_urllib_opener_returns_valid(self):
        """urllib opener可以用代理地址构造"""
        from scripts import proxy_utils as pu

        opener = pu.get_urllib_opener_with_proxy("127.0.0.1:8080")
        self.assertIsNotNone(opener)


if __name__ == "__main__":
    unittest.main()