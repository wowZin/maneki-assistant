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


def test_concept_momentum_features():
    rows = _make_daily_rows()
    basic = {"20260623": {"turnover_rate": 5.0, "volume_ratio": 1.0, "circ_mv": 500000}}
    concept_momentum = {
        "ret1_avg": 2.5,
        "n_concepts": 3,
    }
    feat = build_pit_features(
        "000001.SZ", "20260624", rows, basic,
        concept_momentum=concept_momentum, pit_mode=True,
    )
    assert feat["sector_heat"] == 2.5
    assert feat["n_concepts"] == 3
    import math
    assert abs(feat["sector_rank"] - math.tanh(2.5 / 5.0)) < 1e-6


def test_dragon_tiger_features():
    rows = _make_daily_rows()
    basic = {"20260623": {"turnover_rate": 5.0, "volume_ratio": 1.0, "circ_mv": 500000}}
    top_list = {
        "20260623": {
            "net_amount": 1000000.0,
            "amount": 50000000.0,
            "net_rate": 2.0,
            "l_buy": 600000.0,
            "l_amount": 1000000.0,
        },
    }
    top_inst = {
        "20260623": [
            {"exalter": "机构专用", "net_buy": 800000.0},
            {"exalter": "游资营业部", "net_buy": 200000.0},
            {"exalter": "机构专用", "net_buy": -100000.0},
        ],
    }
    feat = build_pit_features(
        "000001.SZ", "20260624", rows, basic,
        top_list_by_date=top_list, top_inst_by_date=top_inst, pit_mode=True,
    )
    assert feat["dt_is_listed"] == 1.0
    assert feat["dt_net_amount"] == 1000000.0
    assert feat["dt_net_rate"] == 2.0
    assert abs(feat["dt_l_buy_ratio"] - 0.6) < 1e-6
    assert feat["dt_n_exalter"] == 3.0
    assert feat["dt_inst_net_buy"] == 700000.0  # 80万 - 10万
    assert feat["dt_hot_net_buy"] == 200000.0
    assert abs(feat["dt_inst_sell_ratio"] - 100000.0 / 50000000.0) < 1e-6


def test_intraday_features():
    """验证 build_pit_features 正确计算日内分时特征。"""
    rows = _make_daily_rows()
    basic = {"20260623": {"turnover_rate": 5.0, "volume_ratio": 1.0, "circ_mv": 500000}}
    intraday = {
        "20260623": {
            "vwap": 10.5,
            "close": 11.0,
            "high": 11.5,
            "low": 10.0,
            "morning_vol_ratio": 0.6,
            "afternoon_strength": 0.8,
            "tail_vol_ratio": 0.15,
            "amount_est": 150000000.0,
        }
    }
    feat = build_pit_features(
        "000001.SZ", "20260624", rows, basic,
        intraday_by_date=intraday, pit_mode=True,
    )
    assert abs(feat["id_vwap_dev"] - (11.0 / 10.5 - 1.0)) < 1e-6
    assert abs(feat["id_range"] - (11.5 / 10.0 - 1.0)) < 1e-6
    assert feat["id_morning_vol_ratio"] == 0.6
    assert feat["id_afternoon_strength"] == 0.8
    assert feat["id_tail_vol_ratio"] == 0.15
    # avg_amount_5d = 10000*1000 = 10,000,000 元
    assert abs(feat["id_amount_ratio"] - 150000000.0 / 20000000.0) < 1e-6


if __name__ == "__main__":
    test_prev_turnover_and_vol_accel()
    test_max_step_and_was_limit()
    test_candle_features()
    test_moneyflow_features()
    test_momentum_features()
    test_concept_momentum_features()
    test_dragon_tiger_features()
    test_intraday_features()
    print("test_features OK")
