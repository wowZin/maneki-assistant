"""filter.py 7 规则过滤测试。"""

from __future__ import annotations

import pytest

from plays.limit_up.filter import filter_candidates


def test_filter_excludes_st():
    """真实 ST 股应被过滤掉（filter 内部会查 Tushare stock_basic）。"""
    # 000010.SZ 在 Tushare stock_basic 中 name 为 *ST美丽（真实存在的 ST 股）
    candidates = [
        {"code": "000010.SZ", "name": "*ST美丽", "pct_chg": 3.0},
    ]
    filtered = filter_candidates(candidates)
    assert len(filtered) == 0, f"ST 股应被完全过滤: {filtered}"


def test_filter_excludes_chinext():
    candidates = [
        {"code": "300001.SZ", "name": "创业板股", "pct_chg": 3.0},
    ]
    filtered = filter_candidates(candidates)
    assert not any(c["code"].startswith(("300", "301", "688", "8", "4")) for c in filtered)


def test_filter_returns_list():
    filtered = filter_candidates([])
    assert isinstance(filtered, list)
    assert filtered == []
