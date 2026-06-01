"""tests for plays/limit_up/strategies/sentiment.py — 真实Tushare数据

注意: 东方财富人气排名(_get_popularity_rank)和实时涨幅缓存(_batch_fetch_realtime_pct)
在休盘期间返回空数据，不影响评分逻辑。
"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.strategies.sentiment import score_sentiment


class TestSentimentScore(unittest.TestCase):
    def test_known_stock_returns_valid_score(self):
        """平安银行：应有合理的情绪面评分"""
        score, reason = score_sentiment("000001.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(reason, str)
        self.assertGreater(len(reason), 0)
        print(f"  平安银行 情绪面: {score}分 — {reason[:80]}")

    def test_blue_chip_stock(self):
        """贵州茅台"""
        score, reason = score_sentiment("600519.SH")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  贵州茅台 情绪面: {score}分 — {reason[:80]}")

    def test_hot_stock(self):
        """活跃股"""
        score, reason = score_sentiment("002415.SZ")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  海康威视 情绪面: {score}分 — {reason[:80]}")

    def test_invalid_code(self):
        """不存在的代码"""
        score, reason = score_sentiment("999999.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertIsInstance(reason, str)


if __name__ == "__main__":
    unittest.main()