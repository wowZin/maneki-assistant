"""scanner.py 单元测试。"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from plays.limit_up import scanner
from plays.limit_up.strategies import realtime_ctx


def _make_quote(code: str, pct_chg: float = 3.0, **kwargs) -> dict:
    q = {
        "pct_chg": pct_chg,
        "vol_ratio": 1.5,
        "turnover": 5.0,
        "inner_vol": 1000,
        "outer_vol": 2000,
        "bid1": 10.0,
        "ask1": 10.1,
        "amount": 1_000_000,
    }
    q.update(kwargs)
    return q


@patch("plays.limit_up.scanner.get_ths_client")
def test_scan_batch_preserves_full_code_keys(mock_get_client):
    mock_ths = MagicMock()
    mock_ths.get_batch_quotes.return_value = {
        "000001.SZ": _make_quote("000001.SZ"),
        "600519.SH": _make_quote("600519.SH"),
    }
    mock_get_client.return_value = mock_ths

    realtime_ctx.set_realtime_quotes({})  # reset
    results = scanner.scan_batch(["000001.SZ", "600519.SH"], max_rps=1000)

    assert set(results.keys()) == {"000001.SZ", "600519.SH"}
    assert results["000001.SZ"]["pct_chg"] == 3.0
    mock_ths.get_batch_quotes.assert_called_once()


@patch("plays.limit_up.scanner.get_ths_client")
def test_scan_batch_injects_realtime_ctx(mock_get_client):
    mock_ths = MagicMock()
    mock_ths.get_batch_quotes.return_value = {
        "000001.SZ": _make_quote("000001.SZ", pct_chg=5.0),
    }
    mock_get_client.return_value = mock_ths

    realtime_ctx.set_realtime_quotes({})
    scanner.scan_batch(["000001.SZ"], max_rps=1000)

    assert realtime_ctx.get_realtime_pct("000001.SZ") == 5.0
    assert realtime_ctx.get_vol_ratio("000001.SZ") == 1.5
    assert realtime_ctx.get_turnover("000001.SZ") == 5.0
    assert realtime_ctx.get_inner_outer_ratio("000001.SZ") == 0.5


@patch("plays.limit_up.scanner.get_ths_client")
@patch("plays.limit_up.scanner.time.sleep")
def test_scan_batch_chunks_and_sleeps(mock_sleep, mock_get_client):
    mock_ths = MagicMock()
    # 20 只候选，max_rps=10 则 chunk_size=10，应分 2 批，中间 sleep 一次
    quotes = {f"{i:06d}.SZ": _make_quote(f"{i:06d}.SZ") for i in range(20)}
    mock_ths.get_batch_quotes.return_value = quotes
    mock_get_client.return_value = mock_ths

    scanner.scan_batch(list(quotes.keys()), max_rps=10)

    assert mock_ths.get_batch_quotes.call_count == 2
    assert mock_sleep.call_count == 1
    slept = mock_sleep.call_args[0][0]
    assert slept > 0


def test_build_name_map():
    pool = [
        {"code": "000001.SZ", "name": "平安银行"},
        {"code": "600519.SH", "name": "贵州茅台"},
    ]
    name_map = scanner.build_name_map(pool)
    assert name_map == {"000001.SZ": "平安银行", "600519.SH": "贵州茅台"}
