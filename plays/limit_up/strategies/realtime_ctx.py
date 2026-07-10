"""实时行情数据桥接 — 策略层读取实时数据的统一入口。

pipeline_daemon 在每轮评分前调用 set_realtime_quotes() 注入数据，
各策略通过 get_realtime(code) 获取实时行情，替代 Tushare T-1 数据。
"""

from __future__ import annotations

from typing import Any

_REALTIME_CACHE: dict[str, dict] = {}


def set_realtime_quotes(quotes: dict[str, dict]):
    """注入 batch_quotes 实时数据（评分前调用一次）。"""
    global _REALTIME_CACHE
    _REALTIME_CACHE = dict(quotes)


def set_l1_snapshots(snapshots: dict[str, dict]):
    """注入 WS L1 快照数据（盘口/买卖比）。"""
    global _REALTIME_CACHE
    for code, snap in snapshots.items():
        if code not in _REALTIME_CACHE:
            _REALTIME_CACHE[code] = {}
        _REALTIME_CACHE[code]["l1"] = snap


def get_realtime(code: str) -> dict:
    """获取某只股票的实时行情。"""
    return _REALTIME_CACHE.get(code, {})


def get_realtime_pct(code: str) -> float | None:
    """获取实时涨跌幅。"""
    q = _REALTIME_CACHE.get(code, {})
    pct = q.get("pct_chg")
    if pct is not None:
        return float(pct)
    return None


def get_inner_outer_ratio(code: str) -> float | None:
    """获取实时内外盘比 = 内盘/外盘。小于1表示外盘大（买入积极）。"""
    q = _REALTIME_CACHE.get(code, {})
    inner = float(q.get("inner_vol", 0) or 0)
    outer = float(q.get("outer_vol", 0) or 0)
    if outer > 0:
        return inner / outer
    return None


def get_bid_ask_ratio(code: str) -> float | None:
    """获取 L1 买卖比。大于1表示买盘强。"""
    q = _REALTIME_CACHE.get(code, {})
    l1 = q.get("l1", {})
    if not l1:
        return None
    bid_vol = float(l1.get("b1", 0) or 0) + float(l1.get("b2", 0) or 0)
    ask_vol = float(l1.get("s1", 0) or 0) + float(l1.get("s2", 0) or 0)
    if ask_vol > 0:
        return bid_vol / ask_vol
    return None


def get_vol_ratio(code: str) -> float | None:
    """获取实时量比。"""
    q = _REALTIME_CACHE.get(code, {})
    vr = q.get("vol_ratio")
    if vr is not None:
        return float(vr)
    return None


def get_turnover(code: str) -> float | None:
    """获取实时换手率(%)。"""
    q = _REALTIME_CACHE.get(code, {})
    tr = q.get("turnover")
    if tr is not None:
        return float(tr)
    return None
