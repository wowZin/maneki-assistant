"""_pre_rank 预排序测试。"""

from __future__ import annotations

from plays.limit_up.pipeline import _pre_rank


def test_pre_rank_returns_top_n():
    candidates = [
        {"code": f"600{i:03d}.SH", "name": f"股{i}",
         "pct_chg": float(i % 10), "surge": float(i % 5)}
        for i in range(20)
    ]
    top5 = _pre_rank(candidates, top_n=5)
    assert len(top5) == 5


def test_pre_rank_orders_by_score():
    """涨速+涨幅越高排名越靠前。"""
    high = {"code": "600001.SH", "name": "高", "pct_chg": 8.0, "surge": 6.0}
    low = {"code": "600002.SH", "name": "低", "pct_chg": 0.5, "surge": 0.0}
    ranked = _pre_rank([low, high], top_n=2)
    assert ranked[0]["code"] == high["code"], f"高分应在前: {ranked}"


def test_pre_rank_empty():
    assert _pre_rank([], top_n=5) == []
