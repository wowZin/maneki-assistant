"""PIT 特征构建单测。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.pit_features import build_pit_features


def _make_daily_rows():
    rows = []
    for i in range(25):
        # 第 22 天（20260623）模拟涨停
        pct = 9.9 if i == 22 else 2.0
        rows.append({
            "trade_date": f"202606{i+1:02d}",
            "open": 10.0 + i * 0.1,
            "high": 11.0 + i * 0.12,
            "low": 9.5 + i * 0.08,
            "close": 10.5 + i * 0.1,
            "pct_chg": pct,
            "amount": 10000 + i * 500,
            "vol": 100000 + i * 5000,
        })
    return rows


def test_prev_turnover_and_vol_accel():
    rows = _make_daily_rows()
    basic = {
        "20260623": {"turnover_rate": 8.5, "volume_ratio": 1.5, "circ_mv": 500000},
        "20260622": {"turnover_rate": 7.0, "volume_ratio": 1.2, "circ_mv": 500000},
    }
    feat = build_pit_features("000001.SZ", "20260624", rows, basic, pit_mode=True)
    assert feat["prev_turnover"] == 7.0
    assert feat["turnover_rate"] == 8.5
    assert abs(feat["vol_accel"] - (1.5 / 1.2 - 1.0)) < 1e-6


def test_max_step_and_was_limit():
    rows = _make_daily_rows()
    basic = {"20260623": {"turnover_rate": 5.0, "volume_ratio": 1.0, "circ_mv": 500000}}
    feat = build_pit_features("000001.SZ", "20260624", rows, basic, pit_mode=True)
    # PIT 日是 20260623（涨停），max_step 应至少 1
    assert feat["max_step"] >= 1.0
    assert feat["was_limit"] == 1.0


def test_candle_features():
    rows = [
        {
            "trade_date": "20260623",
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "pct_chg": 10.0,
            "amount": 10000,
            "vol": 100000,
        }
    ]
    basic = {"20260623": {"turnover_rate": 5.0, "volume_ratio": 1.0, "circ_mv": 500000}}
    feat = build_pit_features("000001.SZ", "20260623", rows, basic, pit_mode=True)
    assert feat["close_pos"] == (11.0 - 9.0) / (12.0 - 9.0)
    assert feat["amplitude"] == (12.0 - 9.0) / 11.0
    assert feat["body_ratio"] == abs(11.0 - 10.0) / (12.0 - 9.0)


def test_moneyflow_features():
    rows = _make_daily_rows()
    basic = {"20260623": {"turnover_rate": 5.0, "volume_ratio": 1.0, "circ_mv": 500000}}
    mf = {
        "20260623": {
            "net_mf_amount": 500, "buy_elg_amount": 200, "sell_elg_amount": 100,
            "buy_lg_amount": 150, "sell_lg_amount": 120,
        },
        "20260622": {
            "net_mf_amount": 400, "buy_elg_amount": 180, "sell_elg_amount": 110,
            "buy_lg_amount": 140, "sell_lg_amount": 130,
        },
    }
    feat = build_pit_features("000001.SZ", "20260624", rows, basic, mf, pit_mode=True)
    assert feat["mf_net"] == 500
    assert feat["mf_accel"] == (500 - 400) / 400.0
    # buy_elg_ratio = 200 / (200+100)
    assert abs(feat["buy_elg_ratio"] - 2 / 3) < 1e-6


def test_momentum_features():
    rows = _make_daily_rows()
    basic = {"20260623": {"turnover_rate": 5.0, "volume_ratio": 1.0, "circ_mv": 500000}}
    feat = build_pit_features("000001.SZ", "20260624", rows, basic, pit_mode=True)
    assert feat["positive_5d"] == 5.0
    assert feat["pct_5d"] > 0
    assert feat["prev_pct"] == 2.0


if __name__ == "__main__":
    test_prev_turnover_and_vol_accel()
    test_max_step_and_was_limit()
    test_candle_features()
    test_moneyflow_features()
    test_momentum_features()
    print("test_features OK")
