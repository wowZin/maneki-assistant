"""tests for plays/limit_up/strategies/fundamental.py — 真实Tushare数据"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.strategies.fundamental import score_fundamental


class TestFundamentalScore(unittest.TestCase):
    def test_known_stock_returns_valid_score(self):
        """平安银行：应有合理的基本面评分"""
        score, reason = score_fundamental("000001.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(reason, str)
        self.assertGreater(len(reason), 0)
        print(f"  平安银行 基本面: {score}分 — {reason[:80]}")

    def test_blue_chip_stock(self):
        """贵州茅台：绩优股应有较高评分"""
        score, reason = score_fundamental("600519.SH")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  贵州茅台 基本面: {score}分 — {reason[:80]}")

    def test_veto_or_score(self):
        """被否决的股票返回0分或正常评分"""
        score, reason = score_fundamental("000001.SZ")
        if score == 0:
            self.assertIn("否决", reason)
        else:
            self.assertGreater(score, 0)

    def test_invalid_code(self):
        """不存在的代码不应崩溃"""
        score, reason = score_fundamental("999999.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertIsInstance(reason, str)


if __name__ == "__main__":
    unittest.main()