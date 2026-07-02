"""趋势动量质量：温和上涨 + 位置有空间。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_trailing_momentum(row) -> float:
    t10 = safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    position = safe(row.get("position_20d"), 0.5)
    pb10 = safe(row.get("pullback_10d"), 0.0)

    score = 0.0
    if 0.05 <= t10 <= 0.25 and position <= 0.75 and pb10 <= 0.10:
        score += 16.0
    elif 0.03 <= t10 <= 0.30 and position <= 0.80 and pb10 <= 0.15:
        score += 10.0
    elif t10 > 0.0 and t5 > 0.0 and position > 0.30:
        score += 4.0

    if t10 > 0.35:
        score -= 8.0
    elif t10 > 0.25 and position > 0.85:
        score -= 5.0

    return score
