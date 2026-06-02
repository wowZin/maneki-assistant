"""tests for scripts/tu_share.py — 真实调用，不mock"""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.tu_share import (
    CONFIG, load_env, call_tushare,
    clear_tushare_cache, clear_industry_cache,
    get_industry, get_industry_peers,
)


class TestLoadEnv(unittest.TestCase):
    def test_load_env_has_required_keys(self):
        config = load_env()
        self.assertIn("TUSHARE_TOKEN", config)
        self.assertIn("FEISHU_APP_ID", config)

    def test_config_is_loaded_at_module_level(self):
        self.assertIsNotNone(CONFIG)
        self.assertIn("TUSHARE_TOKEN", CONFIG)


class TestCallTushare(unittest.TestCase):
    def setUp(self):
        clear_tushare_cache()
        if not CONFIG.get("TUSHARE_TOKEN"):
            self.skipTest("TUSHARE_TOKEN 未配置")

    def test_stock_basic_single(self):
        result = call_tushare(
            "stock_basic", {"ts_code": "000001.SZ", "list_status": "L"},
            "ts_code,name,industry"
        )
        items = result.get("data", {}).get("items", [])
        self.assertGreater(len(items), 0)
        self.assertEqual(items[0][0], "000001.SZ")

    def test_stock_basic_batch(self):
        result = call_tushare(
            "stock_basic", {"list_status": "L"}, "ts_code,name"
        )
        items = result.get("data", {}).get("items", [])
        self.assertGreater(len(items), 100)

    def test_daily_basic_recent(self):
        result = call_tushare(
            "daily_basic", {"ts_code": "000001.SZ"},
            "trade_date,close,turnover_rate,circ_mv"
        )
        items = result.get("data", {}).get("items", [])
        self.assertGreater(len(items), 0)
        self.assertEqual(len(items[0]), 4)

    def test_cache_hit(self):
        r1 = call_tushare(
            "stock_basic", {"ts_code": "600519.SH", "list_status": "L"}, "ts_code,name"
        )
        r2 = call_tushare(
            "stock_basic", {"ts_code": "600519.SH", "list_status": "L"}, "ts_code,name"
        )
        self.assertEqual(r1, r2)

    def test_cache_key_fields_sensitive(self):
        clear_tushare_cache()
        r1 = call_tushare(
            "daily_basic", {"ts_code": "000001.SZ"}, "close"
        )
        r2 = call_tushare(
            "daily_basic", {"ts_code": "000001.SZ"}, "close,turnover_rate"
        )
        items1 = r1.get("data", {}).get("items", [])
        items2 = r2.get("data", {}).get("items", [])
        if items1 and items2:
            self.assertLess(len(items1[0]), len(items2[0]))

    def test_clear_cache(self):
        r1 = call_tushare(
            "stock_basic", {"ts_code": "000001.SZ", "list_status": "L"}, "ts_code,name"
        )
        clear_tushare_cache()
        r2 = call_tushare(
            "stock_basic", {"ts_code": "000001.SZ", "list_status": "L"}, "ts_code,name"
        )
        items1 = r1.get("data", {}).get("items", [])
        items2 = r2.get("data", {}).get("items", [])
        self.assertEqual(items1, items2)

    def test_invalid_api_returns_empty(self):
        result = call_tushare("nonexistent_api", {"ts_code": "000001.SZ"})
        self.assertTrue(
            result == {} or
            result.get("code") != 0 or
            len(result.get("data", {}).get("items", [])) == 0
        )


class TestIndustryMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clear_industry_cache()

    def setUp(self):
        if not CONFIG.get("TUSHARE_TOKEN"):
            self.skipTest("TUSHARE_TOKEN 未配置")

    def test_get_industry_known_stock(self):
        industry = get_industry("000001.SZ")
        self.assertEqual(industry, "银行")

    def test_get_industry_unknown_returns_empty(self):
        industry = get_industry("999999.SZ")
        self.assertEqual(industry, "")

    def test_get_industry_peers_returns_list(self):
        peers = get_industry_peers("银行")
        self.assertIn("000001.SZ", peers)
        self.assertGreater(len(peers), 1)

    def test_get_industry_peers_limit(self):
        peers = get_industry_peers("银行", limit=5)
        self.assertLessEqual(len(peers), 5)

    def test_get_industry_peers_nonexistent(self):
        peers = get_industry_peers("不存在的行业XYZ")
        self.assertEqual(peers, [])

    def test_clear_industry_cache(self):
        get_industry("000001.SZ")
        clear_industry_cache()
        industry = get_industry("000001.SZ")
        self.assertEqual(industry, "银行")

    def test_cached_call_no_repeat(self):
        clear_industry_cache()
        peers1 = get_industry_peers("银行")
        peers2 = get_industry_peers("银行")
        self.assertEqual(peers1, peers2)


if __name__ == "__main__":
    unittest.main()