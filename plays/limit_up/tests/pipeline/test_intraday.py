"""生产 pipeline 日内分时数据接入单测。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up import pipeline as p


def _reset_nv2_cache():
    p._NV2_DATE = "20260702"
    p._NV2_DAILY_CACHE = {
        "000100.SZ": [
            {
                "trade_date": "20260630",
                "open": 6.0,
                "close": 6.05,
                "high": 6.1,
                "low": 5.95,
                "vol": 1000000,
                "amount": 6000,
                "pct_chg": 1.0,
            },
            {
                "trade_date": "20260701",
                "open": 6.05,
                "close": 6.12,
                "high": 6.25,
                "low": 5.91,
                "vol": 2000000,
                "amount": 12000,
                "pct_chg": 1.16,
            },
            {
                "trade_date": "20260702",
                "open": 6.12,
                "close": 6.20,
                "high": 6.30,
                "low": 6.10,
                "vol": 1500000,
                "amount": 9000,
                "pct_chg": 1.31,
            },
        ]
    }
    p._NV2_DAILY_BASIC_CACHE = {
        "000100.SZ": {
            "20260701": {
                "pe": 10,
                "pb": 1.2,
                "circ_mv": 1000000,
                "turnover_rate": 5.0,
                "volume_ratio": 1.5,
            },
        }
    }
    p._NV2_MONEYFLOW_CACHE = {"000100.SZ": {}}
    p._NV2_TOP_LIST_CACHE = {"000100.SZ": {}}
    p._NV2_TOP_INST_CACHE = {"000100.SZ": {}}
    p._NV2_LIMIT_CACHE = {"000100.SZ": 0}
    p._NV2_LIMIT_60D_CACHE = {"000100.SZ": 0}


def test_extract_pit_features_uses_intraday_cache():
    """_extract_pit_features 应使用 _NV2_INTRADAY_CACHE 中的 T-1 分时数据。"""
    _reset_nv2_cache()
    p._NV2_INTRADAY_CACHE = {
        "000100.SZ": {
            "20260701": {
                "ts_code": "000100.SZ",
                "trade_date": "20260701",
                "vwap": 6.06,
                "close": 6.12,
                "high": 6.25,
                "low": 5.91,
                "morning_vol_ratio": 0.75,
                "afternoon_strength": 0.32,
                "tail_vol_ratio": 0.08,
                "amount_est": 1.5e10,
            }
        }
    }

    feats = p._extract_pit_features("000100.SZ", pit_mode=True)
    assert abs(feats["id_vwap_dev"] - (6.12 / 6.06 - 1.0)) < 1e-6
    assert abs(feats["id_range"] - (6.25 / 5.91 - 1.0)) < 1e-6
    assert feats["id_morning_vol_ratio"] == 0.75
    assert feats["id_afternoon_strength"] == 0.32
    assert feats["id_tail_vol_ratio"] == 0.08


def test_extract_pit_features_defaults_without_intraday_cache():
    """未提供 intraday 缓存时，id_* 特征应使用默认值。"""
    _reset_nv2_cache()
    p._NV2_INTRADAY_CACHE = {}

    feats = p._extract_pit_features("000100.SZ", pit_mode=True)
    assert feats["id_vwap_dev"] == 0.0
    assert feats["id_range"] == 0.0
    assert feats["id_morning_vol_ratio"] == 0.5
    assert feats["id_afternoon_strength"] == 1.0
    assert feats["id_tail_vol_ratio"] == 0.1


if __name__ == "__main__":
    test_extract_pit_features_uses_intraday_cache()
    test_extract_pit_features_defaults_without_intraday_cache()
    print("test_pipeline_intraday OK")
