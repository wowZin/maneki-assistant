"""换手动量：高换手 + 高量比 + 有波动。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_turnover_momentum(row) -> float:
    turnover = safe(row.get("turnover_rate"), 5.0)
    vol_ratio = safe(row.get("volume_ratio"), 1.0)
    std5 = safe(row.get("pct_chg_std_5d"), 0.0)
    position = safe(row.get("position_20d"), 0.5)

    score = 0.0
    if turnover >= 15 and vol_ratio >= 1.5 and std5 >= 3.0 and 0.30 <= position <= 0.80:
        score += 18.0
    elif turnover >= 10 and vol_ratio >= 1.2 and std5 >= 2.5 and position >= 0.25:
        score += 12.0
    elif turnover >= 5 and vol_ratio >= 1.0 and std5 >= 2.0:
        score += 5.0

    if turnover < 2:
        score -= 5.0

    return score
