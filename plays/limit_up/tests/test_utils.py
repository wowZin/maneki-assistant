"""tests for plays/limit_up/utils.py — 纯函数，不需要外部数据"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.utils import safe_float, safe_float_none, safe_int_none, is_trading_time, list_to_dict


class TestSafeFloat(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(safe_float("123.45"), 123.45)
        self.assertEqual(safe_float(100), 100.0)

    def test_none(self):
        self.assertEqual(safe_float(None), 0.0)

    def test_invalid(self):
        self.assertEqual(safe_float("abc"), 0.0)
        self.assertEqual(safe_float(""), 0.0)

    def test_none_returns_none(self):
        self.assertIsNone(safe_float_none(None))
        self.assertEqual(safe_float_none("123.45"), 123.45)

    def test_int_none(self):
        self.assertIsNone(safe_int_none(None))
        self.assertEqual(safe_int_none("123"), 123)
        self.assertIsNone(safe_int_none("abc"))


class TestIsTradingTime(unittest.TestCase):
    def test_returns_bool(self):
        result = is_trading_time()
        self.assertIsInstance(result, bool)

    def test_weekend_is_false(self):
        from datetime import datetime
        if datetime.now().weekday() >= 5:
            self.assertFalse(is_trading_time())


class TestListToDict(unittest.TestCase):
    def test_normal(self):
        items = [["000001.SZ", "平安银行"], ["600519.SH", "贵州茅台"]]
        fields = ["ts_code", "name"]
        result = list_to_dict(items, fields)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["ts_code"], "000001.SZ")
        self.assertEqual(result[0]["name"], "平安银行")

    def test_empty(self):
        self.assertEqual(list_to_dict([], ["a"]), [])
        self.assertEqual(list_to_dict([["a"]], []), [])

    def test_none(self):
        self.assertEqual(list_to_dict(None, ["a"]), [])


if __name__ == "__main__":
    unittest.main()