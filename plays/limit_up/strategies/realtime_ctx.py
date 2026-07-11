"""实时行情数据桥接 — 策略层读取实时数据的统一入口。

pipeline_daemon 在每轮评分前调用 set_realtime_quotes() 注入数据，
各策略通过 get_realtime(code) 获取实时行情，替代 Tushare T-1 数据。

线程安全：所有 set/get 操作均受 _LOCK 保护。
"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.RLock()
_REALTIME_CACHE: dict[str, dict] = {}


def set_realtime_quotes(quotes: dict[str, dict]):
    """注入 batch_quotes 实时数据（评分前调用一次）。

    保存完整 quote dict，不丢弃任何字段，确保策略能读取
    inner_vol/outer_vol/vol_ratio/turnover/bid1/ask1/amount 等。
    """
    global _REALTIME_CACHE
    with _LOCK:
        _REALTIME_CACHE = dict(quotes)


def set_l1_snapshots(snapshots: dict[str, dict]):
    """注入 WS L1 快照数据（盘口/买卖比）。"""
    global _REALTIME_CACHE
    with _LOCK:
        for code, snap in snapshots.items():
            if code not in _REALTIME_CACHE:
                _REALTIME_CACHE[code] = {}
            _REALTIME_CACHE[code]["l1"] = snap


def get_realtime(code: str) -> dict:
    """获取某只股票的实时行情。"""
    with _LOCK:
        return _REALTIME_CACHE.get(code, {}).copy()


def get_realtime_pct(code: str) -> float | None:
    """获取实时涨跌幅。"""
    with _LOCK:
        q = _REALTIME_CACHE.get(code, {})
        pct = q.get("pct_chg")
        if pct is not None:
            return float(pct)
        return None


def get_inner_outer_ratio(code: str) -> float | None:
    """获取实时内外盘比 = 内盘/外盘。小于1表示外盘大（买入积极）。"""
    with _LOCK:
        q = _REALTIME_CACHE.get(code, {})
        inner = float(q.get("inner_vol", 0) or 0)
        outer = float(q.get("outer_vol", 0) or 0)
        if outer > 0:
            return inner / outer
        return None


def _first_non_zero(values: list[Any]) -> float:
    for v in values:
        if v:
            try:
                return float(v)
            except (ValueError, TypeError):
                continue
    return 0.0


def get_bid_ask_ratio(code: str) -> float | None:
    """获取 L1 买卖比。大于1表示买盘强。

    兼容两种 L1 快照格式：
      - WS 返回的 bid_price/bid_qty/ask_price/ask_qty 数组
      - 旧式的 b1/b2/s1/s2 字段
    """
    with _LOCK:
        q = _REALTIME_CACHE.get(code, {})
        l1 = q.get("l1", {})
        if not l1:
            return None

        # WS 数组格式
        bid_qty = l1.get("bid_qty") or l1.get("bid_volume") or []
        ask_qty = l1.get("ask_qty") or l1.get("ask_volume") or []
        if bid_qty and ask_qty:
            bid_vol = sum(float(v) for v in bid_qty[:2] if v is not None)
            ask_vol = sum(float(v) for v in ask_qty[:2] if v is not None)
            if ask_vol > 0:
                return bid_vol / ask_vol
            return None

        # 旧式字段格式
        bid_vol = float(l1.get("b1", 0) or 0) + float(l1.get("b2", 0) or 0)
        ask_vol = float(l1.get("s1", 0) or 0) + float(l1.get("s2", 0) or 0)
        if ask_vol > 0:
            return bid_vol / ask_vol
        return None


def get_vol_ratio(code: str) -> float | None:
    """获取实时量比。"""
    with _LOCK:
        q = _REALTIME_CACHE.get(code, {})
        vr = q.get("vol_ratio")
        if vr is not None:
            return float(vr)
        return None


def get_turnover(code: str) -> float | None:
    """获取实时换手率(%)。"""
    with _LOCK:
        q = _REALTIME_CACHE.get(code, {})
        tr = q.get("turnover")
        if tr is not None:
            return float(tr)
        return None


def get_amount(code: str) -> float | None:
    """获取实时成交额（元）。"""
    with _LOCK:
        q = _REALTIME_CACHE.get(code, {})
        amount = q.get("amount")
        if amount is not None:
            return float(amount)
        return None


def get_bid_ask_spread(code: str) -> tuple[float, float] | None:
    """获取买卖一价。返回 (bid1, ask1)。"""
    with _LOCK:
        q = _REALTIME_CACHE.get(code, {})
        bid1 = q.get("bid1")
        ask1 = q.get("ask1")
        if bid1 is not None and ask1 is not None:
            return float(bid1), float(ask1)
        return None
