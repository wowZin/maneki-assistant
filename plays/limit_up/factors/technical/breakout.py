"""突破质量：接近10日高点、20日维度仍有空间、放量。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_breakout_quality(row) -> float:
    pb10 = safe(row.get("pullback_10d"), 0.0)
    pb20 = safe(row.get("pullback_20d"), 0.0)
    position = safe(row.get("position_20d"), 0.5)
    vol_ratio = safe(row.get("vol_ratio_proxy"), 1.0)
    amount_ratio = safe(row.get("amount_ratio"), 1.0)

    score = 0.0
    if pb10 < 0.05 and 0.03 <= pb20 <= 0.15 and position >= 0.60 and vol_ratio > 1.2:
        score += 18.0
    elif pb10 < 0.08 and 0.02 <= pb20 <= 0.20 and position >= 0.50 and (vol_ratio > 1.0 or amount_ratio > 1.2):
        score += 10.0
    elif pb10 < 0.15 and position >= 0.40:
        score += 4.0

    if pb20 < 0.02 and position > 0.85:
        score -= 8.0

    return score
