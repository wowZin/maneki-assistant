"""回调质量 / 峰位回调 / 最优位置。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_pullback_quality(row) -> float:
    pb = safe(row.get("pullback_10d"), 1.0)
    vol_ratio = safe(row.get("vol_ratio_proxy"), 1.0)

    if 0.05 <= pb <= 0.15 and vol_ratio < 0.8:
        return 18.0
    if 0.03 <= pb <= 0.20 and vol_ratio < 1.0:
        return 10.0
    if pb > 0.20:
        return -5.0
    return 0.0


def factor_pullback_from_peak(row) -> float:
    pb20 = safe(row.get("pullback_20d"), 0.0)
    if 0.03 <= pb20 <= 0.08:
        return 15.0
    if 0.08 < pb20 <= 0.15:
        return 8.0
    if pb20 < 0.02:
        return -5.0
    return 0.0


def factor_position_optimal(row) -> float:
    pos = safe(row.get("position_20d"), 0.5)
    if 0.30 <= pos <= 0.70:
        return 10.0
    if pos > 0.85:
        return -10.0
    if pos < 0.15:
        return -5.0
    return 0.0
