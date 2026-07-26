"""tests for plays/watchdog/indicators.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import numpy as np

from plays.watchdog.indicators import sma, atr, price_features, realtime_row


def test_sma_basic():
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = sma(arr, 3)
    assert np.isnan(result[0])
    assert np.isnan(result[1])
    assert result[2] == 2.0
    assert result[4] == 4.0


def test_atr_basic():
    high = np.array([10.0, 11.0, 12.0, 11.5, 13.0])
    low = np.array([9.0, 10.0, 10.5, 10.0, 11.0])
    close = np.array([9.5, 10.5, 11.5, 10.8, 12.5])
    result = atr(high, low, close, 2)
    assert np.isnan(result[0])
    assert not np.isnan(result[-1])


def test_price_features_minimal():
    rows = []
    base = 10.0
    for i in range(25):
        rows.append({
            "trade_date": f"202601{i+1:02d}",
            "open": base, "high": base + 0.5, "low": base - 0.5,
            "close": base + i * 0.1, "pre_close": base + (i - 1) * 0.1,
            "vol": 10000, "amount": 1000, "pct_chg": 1.0,
        })
    feats = price_features(rows)
    assert feats["limit_up_count_20d"] == 0.0
    assert feats["position_20d"] >= 0.0


def test_realtime_row():
    market = {
        "last": "11.0", "open": "10.2", "pre_close": "10.0",
        "trade_volume": "100000", "trade_amount": "1100000",
    }
    daily_features = {
        "position_20d": 0.6, "trailing_10": 0.1, "trailing_5": 0.05,
        "pullback_10d": 0.05, "pullback_20d": 0.08,
        "pct_chg_std_10d": 2.5, "pct_chg_std_5d": 2.0,
        "max_pct_chg_5d": 4.0, "avg_amount_5d": 1000000.0,
        "limit_up_count_20d": 2.0, "limit_up_count_60d": 3.0,
    }
    daily_basic = {"circ_mv": 10000000.0, "pe": 20.0, "pb": 2.0}
    dim_scores = {"fundamental": 50, "technical": 60, "fundflow": 70, "sentiment": 40, "shortterm": 55}
    row = realtime_row("000001.SZ", market, 10.5, daily_features, daily_basic, dim_scores)
    assert abs(row["pct_chg_score_day"] - 10.0) < 1e-9
    assert abs(row["gap_up"] - 2.0) < 1e-9
    assert row["turnover_rate"] > 0
    # ── 关键字段手算核对 ──
    # pct_chg_score_day = (11.0/10.0 - 1) * 100 = 10.0（上方已断言）
    # gap_up            = (10.2/10.0 - 1) * 100 = 2.0（上方已断言）
    # vol_ratio_proxy   = trade_amount / avg_amount_5d = 1_100_000 / 1_000_000 = 1.1
    assert abs(row["vol_ratio_proxy"] - 1.1) < 1e-9
    # turnover_rate     = trade_amount / (circ_mv * 10000) * 100
    #                   = 1_100_000 / (10_000_000 * 10000) * 100 = 0.0011
    assert abs(row["turnover_rate"] - 0.0011) < 1e-12
    # amount_ratio      = trade_amount / avg_amount_5d = 1.1
    assert abs(row["amount_ratio"] - 1.1) < 1e-9
    # 透传字段
    assert row["last_price"] == 11.0
    assert row["vwap"] == 10.5
    assert row["code"] == "000001.SZ"


if __name__ == "__main__":
    test_sma_basic()
    test_atr_basic()
    test_price_features_minimal()
    test_realtime_row()
    print("indicators tests passed")
