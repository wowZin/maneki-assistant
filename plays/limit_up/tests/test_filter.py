"""tests for plays/limit_up/filter.py — 实时过滤规则"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.filter import filter_realtime, filter_candidates


class TestFilterRealtime(unittest.TestCase):
    def test_yizi_ban_filtered(self):
        """一字板涨停（涨幅高+换手低）应被过滤"""
        quote = {
            "pct_chg": 9.95,
            "turnover": 0.1,
            "limit_up": 11.0,
            "price": 11.0,
        }
        vetoed, reason = filter_realtime(quote)
        self.assertTrue(vetoed)
        self.assertIn("一字板", reason)

    def test_normal_limit_up_not_filtered(self):
        """正常涨停（换手不低）不应被过滤"""
        quote = {
            "pct_chg": 9.95,
            "turnover": 5.0,
            "limit_up": 11.0,
            "price": 11.0,
        }
        vetoed, reason = filter_realtime(quote)
        self.assertFalse(vetoed)

    def test_yizi_drop_filtered(self):
        """一字跌停应被过滤"""
        quote = {
            "pct_chg": -9.95,
            "turnover": 0.1,
            "limit_down": 9.0,
            "price": 9.0,
        }
        vetoed, reason = filter_realtime(quote)
        self.assertTrue(vetoed)
        self.assertIn("跌停", reason)

    def test_low_pct_not_filtered(self):
        """涨幅不足时不被过滤"""
        quote = {
            "pct_chg": 3.0,
            "turnover": 2.0,
        }
        vetoed, reason = filter_realtime(quote)
        self.assertFalse(vetoed)


class TestFilterCandidatesBackwardCompat(unittest.TestCase):
    def test_empty_input(self):
        """空列表返回空"""
        result = filter_candidates([])
        self.assertEqual(result, [])

    def test_identity_return(self):
        """filter_candidates 已废弃，直接返回原列表"""
        candidates = [
            {"code": "000001.SZ", "name": "平安银行"},
        ]
        result = filter_candidates(candidates)
        self.assertEqual(result, candidates)


if __name__ == "__main__":
    unittest.main()
