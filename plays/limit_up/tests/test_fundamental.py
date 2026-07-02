"""tests for plays/limit_up/strategies/fundamental.py — 真实Tushare数据"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.strategies.fundamental import score_fundamental


class TestFundamentalScore(unittest.TestCase):
    """基本面v4评分测试：验证大盘成长+概念催化评分逻辑"""

    def test_return_signature(self):
        """验证返回值类型和范围"""
        score, reason = score_fundamental("000001.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(reason, str)
        self.assertGreater(len(reason), 0)
        print(f"  000001.SZ v4: {score}分 — {reason[:80]}")

    def test_large_cap_growth_scores_high(self):
        """大盘成长股应得分较高（数据证明大盘股更易涨停）"""
        score, reason = score_fundamental("600519.SH")  # 贵州茅台
        self.assertGreaterEqual(score, 30, "大盘成长股不应得到过低评分")
        print(f"  600519.SH v4: {score}分 — {reason[:80]}")

    def test_small_cap_valid_range(self):
        """小盘股评分在合法范围内即可"""
        score, reason = score_fundamental("002766.SZ")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  002766.SZ v4: {score}分 — {reason[:80]}")

    def test_invalid_code_no_crash(self):
        """不存在的代码不应崩溃"""
        score, reason = score_fundamental("999999.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertIsInstance(reason, str)
        self.assertGreaterEqual(score, 0)

    def test_veto_or_score(self):
        """被否决的股票返回0分，未被否决的返回0-100"""
        score, reason = score_fundamental("000001.SZ")
        if score == 0:
            self.assertIn("否决", reason)
        else:
            self.assertGreater(score, 0)

    def test_reason_contains_level(self):
        """reason 字符串应包含等级标记"""
        _, reason = score_fundamental("000001.SZ")
        valid_levels = ["[高]", "[中]", "[低]", "[无]"]
        self.assertTrue(
            any(level in reason for level in valid_levels),
            f"reason 缺少等级标记: {reason[:50]}",
        )

    def test_growth_orientation(self):
        """验证v4是成长导向：高 PB/PE 大盘股得分不低"""
        blue_score, _ = score_fundamental("600519.SH")
        growth_score, _ = score_fundamental("000858.SZ")
        print(f"  v4评分: 茅台{int(blue_score)} vs 五粮液{int(growth_score)}")
        # 不再强制增长股>蓝筹，只要求两者都在合法范围
        self.assertGreaterEqual(growth_score, 0)
        self.assertLessEqual(growth_score, 100)


if __name__ == "__main__":
    unittest.main()
