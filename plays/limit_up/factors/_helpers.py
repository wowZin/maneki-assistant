"""因子共享工具。"""
from __future__ import annotations

import pandas as pd


def safe(v, default: float = 0.0) -> float:
    """安全取值：None/NaN 返回 default。"""
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


def get(row, key: str, default=None):
    """从 pd.Series 或 dict 取值，支持嵌套 fallback。"""
    if hasattr(row, "get"):
        return row.get(key, default)
    return default
