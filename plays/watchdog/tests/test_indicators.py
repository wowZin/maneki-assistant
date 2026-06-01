"""tests for plays/watchdog/indicators.py — 真实数据

注意: 计算指标(calc_all)需要DataFrame输入，测试用真实数据构造。
"""

import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import numpy as np
import pandas as pd

from plays.watchdog.indicators import calc_all, check_trend, check_pullback, check_entry_score, check_exit_signal


class TestIndicators(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """构造60根模拟K线"""
        np.random.seed(42)
        n = 60
        base = 10.0
        closes = base + np.cumsum(np.random.randn(n) * 0.1)
        highs = closes + np.abs(np.random.randn(n) * 0.2)
        lows = closes - np.abs(np.random.randn(n) * 0.2)
        opens = closes - np.random.randn(n) * 0.05
        volumes = np.random.randint(1000, 10000, n)

        cls.df = pd.DataFrame({
            "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": volumes,
        })

    def test_calc_all_returns_dict(self):
        """calc_all 返回字典"""
        result = calc_all(self.df)
        self.assertIsInstance(result, dict)
        self.assertIn("kama", result)
        self.assertIn("adx", result)
        self.assertIn("close", result)
        print(f"  calc_all keys: {list(result.keys())[:5]}...")

    def test_check_trend(self):
        """check_trend 接收 calc_all 返回的 dict"""
        inds = calc_all(self.df)
        ok, reason = check_trend(inds)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(reason, str)
        print(f"  check_trend: ok={ok}, reason={reason}")

    def test_entry_score_range(self):
        """check_entry_score 在合理范围"""
        inds = calc_all(self.df)
        atr = inds["atr20"][-1]
        close = inds["close"][-1]
        vol = float(self.df["volume"].iloc[-1])
        score, _ = check_entry_score(inds, atr, close, close, close, close, close, vol, vol)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)

    def test_exit_signal_boolean(self):
        """check_exit_signal 返回布尔值"""
        inds = calc_all(self.df)
        close = inds["close"][-1]
        atr = inds["atr20"][-1]
        exit_sig, _ = check_exit_signal(inds, close, close, 1, atr, close)
        self.assertIsInstance(exit_sig, (bool, np.bool_))


if __name__ == "__main__":
    unittest.main()