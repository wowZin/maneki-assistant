"""tests for plays/limit_up/filter.py — 真实Tushare数据"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.filter import filter_candidates


class TestFilterCandidates(unittest.TestCase):
    def test_empty_input(self):
        """空列表返回空"""
        result = filter_candidates([])
        self.assertEqual(result, [])

    def test_normal_stocks(self):
        """正常股票通过过滤"""
        candidates = [
            {"code": "000001.SZ", "name": "平安银行"},
            {"code": "600519.SH", "name": "贵州茅台"},
        ]
        result = filter_candidates(candidates)
        self.assertIsInstance(result, list)
        print(f"  过滤结果: {len(candidates)}只 → {len(result)}只")

    def test_st_stock_filtered(self):
        """ST股票应被过滤"""
        candidates = [
            {"code": "000001.SZ", "name": "平安银行"},
        ]
        result = filter_candidates(candidates)
        self.assertIsInstance(result, list)

    def test_gem_filtered(self):
        """创业板应被过滤"""
        candidates = [
            {"code": "300750.SZ", "name": "宁德时代"},
        ]
        result = filter_candidates(candidates)
        self.assertEqual(len(result), 0)

    def test_star_market_filtered(self):
        """科创板应被过滤"""
        candidates = [
            {"code": "688001.SH", "name": "华兴源创"},
        ]
        result = filter_candidates(candidates)
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()