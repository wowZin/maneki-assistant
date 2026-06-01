"""tests for plays/limit_up/strategies/fundflow.py — 真实Tushare数据

注意: l2api Level2实时数据在未连接时自动回退到T+1日频数据，
休盘期间不影响评分逻辑。
"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.strategies.fundflow import score_fundflow


class TestFundflowScore(unittest.TestCase):
    def test_known_stock_returns_valid_score(self):
        """平安银行：应有合理的资金面评分"""
        score, reason = score_fundflow("000001.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(reason, str)
        self.assertGreater(len(reason), 0)
        print(f"  平安银行 资金面: {score}分 — {reason[:80]}")

    def test_blue_chip_stock(self):
        """贵州茅台"""
        score, reason = score_fundflow("600519.SH")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  贵州茅台 资金面: {score}分 — {reason[:80]}")

    def test_small_stock(self):
        """小盘股"""
        score, reason = score_fundflow("002415.SZ")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  海康威视 资金面: {score}分 — {reason[:80]}")

    def test_invalid_code(self):
        """不存在的代码"""
        score, reason = score_fundflow("999999.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertIsInstance(reason, str)


if __name__ == "__main__":
    unittest.main()