"""tests for plays/limit_up/strategies/technical.py — 真实Tushare数据

注意: 盘中实时资金流缓存(_get_realtime_fund_cache)在休盘期间返回空数据，
不影响评分逻辑，但实时量比等因子会使用T-1数据替代。
"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.strategies.technical import score_technical


class TestTechnicalScore(unittest.TestCase):
    def test_known_stock_returns_valid_score(self):
        """平安银行：应有合理的技术面评分"""
        score, reason = score_technical("000001.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(reason, str)
        self.assertGreater(len(reason), 0)
        print(f"  平安银行 技术面: {score}分 — {reason[:80]}")

    def test_blue_chip_stock(self):
        """贵州茅台"""
        score, reason = score_technical("600519.SH")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  贵州茅台 技术面: {score}分 — {reason[:80]}")

    def test_veto_or_score(self):
        """被否决的股票返回0分或正常评分"""
        score, reason = score_technical("000001.SZ")
        if score == 0:
            self.assertIn("否决", reason)

    def test_invalid_code(self):
        """不存在的代码"""
        score, reason = score_technical("999999.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertIsInstance(reason, str)


if __name__ == "__main__":
    unittest.main()