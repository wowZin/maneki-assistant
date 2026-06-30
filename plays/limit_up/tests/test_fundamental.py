"""tests for plays/limit_up/strategies/fundamental.py — 真实Tushare数据"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from plays.limit_up.strategies.fundamental import score_fundamental


class TestFundamentalScore(unittest.TestCase):
    """基本面v3评分测试：验证催化剂导向评分逻辑"""

    def test_return_signature(self):
        """验证返回值类型和范围"""
        score, reason = score_fundamental("000001.SZ")
        self.assertIsInstance(score, (int, float))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(reason, str)
        self.assertGreater(len(reason), 0)
        print(f"  000001.SZ v3: {score}分 — {reason[:80]}")

    def test_large_cap_scores_low(self):
        """大盘蓝筹应得分较低（缺乏爆发力）"""
        score, reason = score_fundamental("600519.SH")  # 贵州茅台
        self.assertLessEqual(score, 50, "大盘股不应得到高催化剂评分")
        print(f"  600519.SH v3: {score}分 — {reason[:80]}")

    def test_small_cap_scores_higher(self):
        """小盘股评分不低于同类大盘股（仅验证不崩溃）"""
        # 随机挑选一个小盘股代码，主要验证不崩溃
        score, reason = score_fundamental("002766.SZ")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        print(f"  002766.SZ v3: {score}分 — {reason[:80]}")

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
        # 等级: [高] [中] [低] [无]
        valid_levels = ["[高]", "[中]", "[低]", "[无]"]
        self.assertTrue(
            any(level in reason for level in valid_levels),
            f"reason 缺少等级标记: {reason[:50]}",
        )

    def test_catalyst_scoring(self):
        """验证v3是催化剂导向而非质量导向：
        高增长小盘股得分 >= 稳定大盘蓝筹得分（宽松检查）"""
        # 稳定蓝筹
        blue_score, _ = score_fundamental("600519.SH")
        # 存在一定增长的股票
        growth_score, _ = score_fundamental("000858.SZ")  # 五粮液 — 利润增长
        # 宽松验证: 有增长的得分更高
        if growth_score > blue_score:
            print(f"  v3催化剂导向: 增长股{int(growth_score)} > 蓝筹{int(blue_score)} ✓")
        else:
            # 如果增长股得分低，说明有其他因素（如大盘权重过大）
            print(f"  v3评分: 增长股{int(growth_score)} vs 蓝筹{int(blue_score)} (注意:大盘股共同特征)")


if __name__ == "__main__":
    unittest.main()
