"""realtime_ctx.py 单元测试。"""

from __future__ import annotations

import threading

import pytest

from plays.limit_up.strategies import realtime_ctx


def setup_function():
    realtime_ctx.set_realtime_quotes({})


def test_set_and_get_realtime():
    realtime_ctx.set_realtime_quotes({
        "000001": {"pct_chg": 5.0, "vol_ratio": 2.0, "turnover": 3.0},
    })
    data = realtime_ctx.get_realtime("000001")
    assert data["pct_chg"] == 5.0
    assert data["vol_ratio"] == 2.0


def test_get_realtime_pct():
    realtime_ctx.set_realtime_quotes({"000001": {"pct_chg": 4.5}})
    assert realtime_ctx.get_realtime_pct("000001") == 4.5
    assert realtime_ctx.get_realtime_pct("999999") is None


def test_get_inner_outer_ratio():
    realtime_ctx.set_realtime_quotes({"000001": {"inner_vol": 100, "outer_vol": 200}})
    assert realtime_ctx.get_inner_outer_ratio("000001") == 0.5


def test_get_inner_outer_ratio_no_outer():
    realtime_ctx.set_realtime_quotes({"000001": {"inner_vol": 100, "outer_vol": 0}})
    assert realtime_ctx.get_inner_outer_ratio("000001") is None


def test_get_vol_ratio_and_turnover():
    realtime_ctx.set_realtime_quotes({"000001": {"vol_ratio": 1.8, "turnover": 6.5}})
    assert realtime_ctx.get_vol_ratio("000001") == 1.8
    assert realtime_ctx.get_turnover("000001") == 6.5


def test_get_bid_ask_ratio_ws_array_format():
    realtime_ctx.set_realtime_quotes({
        "000001": {
            "l1": {
                "bid_qty": [1000, 500],
                "ask_qty": [200, 300],
            }
        }
    })
    ratio = realtime_ctx.get_bid_ask_ratio("000001")
    assert ratio == (1000 + 500) / (200 + 300)


def test_get_bid_ask_ratio_legacy_format():
    realtime_ctx.set_realtime_quotes({
        "000001": {
            "l1": {"b1": 1000, "b2": 500, "s1": 200, "s2": 300}
        }
    })
    ratio = realtime_ctx.get_bid_ask_ratio("000001")
    assert ratio == (1000 + 500) / (200 + 300)


def test_get_bid_ask_ratio_no_l1():
    realtime_ctx.set_realtime_quotes({"000001": {}})
    assert realtime_ctx.get_bid_ask_ratio("000001") is None


def test_set_l1_snapshots_merges():
    realtime_ctx.set_realtime_quotes({"000001": {"pct_chg": 2.0}})
    realtime_ctx.set_l1_snapshots({"000001": {"bid_qty": [100], "ask_qty": [50]}})
    data = realtime_ctx.get_realtime("000001")
    assert data["pct_chg"] == 2.0
    assert data["l1"]["bid_qty"] == [100]


def test_thread_safety():
    errors = []

    def writer():
        try:
            for i in range(100):
                realtime_ctx.set_realtime_quotes({"000001": {"pct_chg": i}})
        except Exception as e:
            errors.append(e)

    def reader():
        try:
            for _ in range(100):
                realtime_ctx.get_realtime_pct("000001")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
