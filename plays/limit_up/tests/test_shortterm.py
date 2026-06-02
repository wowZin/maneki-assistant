"""tests for plays/limit_up/strategies/shortterm.py — 真实Tushare数据"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.strategies.shortterm import score_shortterm


class TestShorttermScore(unittest.TestCase):
    def test_known_stock_returns_valid_score(self):
        """平安银行"""
        score, reason = score_shortterm("000001.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(reason, str)
        print(f"  平安银行 短线博弈: {score}分 — {reason[:80]}")

    def test_blue_chip_stock(self):
        """贵州茅台"""
        score, reason = score_shortterm("600519.SH")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  贵州茅台 短线博弈: {score}分 — {reason[:80]}")

    def test_invalid_code(self):
        """不存在的代码"""
        score, reason = score_shortterm("999999.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertIsInstance(reason, str)


if __name__ == "__main__":
    unittest.main()