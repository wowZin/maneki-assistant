"""filter.py 实时过滤测试。"""

from __future__ import annotations

import pytest

from plays.limit_up.filter import filter_realtime


def test_filter_realtime_excludes_yizi_ban():
    quote = {
        "pct_chg": 9.98,
        "turnover": 0.1,
        "limit_up": 11.0,
        "price": 11.0,
    }
    vetoed, reason = filter_realtime(quote)
    assert vetoed is True
    assert "一字板" in reason


def test_filter_realtime_allows_high_turnover_limit_up():
    quote = {
        "pct_chg": 9.98,
        "turnover": 5.0,
        "limit_up": 11.0,
        "price": 11.0,
    }
    vetoed, reason = filter_realtime(quote)
    assert vetoed is False


def test_filter_realtime_excludes_yizi_drop():
    quote = {
        "pct_chg": -9.98,
        "turnover": 0.05,
        "limit_down": 9.0,
        "price": 9.0,
    }
    vetoed, reason = filter_realtime(quote)
    assert vetoed is True
    assert "跌停" in reason


def test_filter_realtime_allows_normal_rising():
    quote = {
        "pct_chg": 3.0,
        "turnover": 2.0,
    }
    vetoed, reason = filter_realtime(quote)
    assert vetoed is False
