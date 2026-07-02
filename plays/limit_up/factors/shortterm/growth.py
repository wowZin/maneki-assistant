"""成长动量：高估值 + 短线技术强 + 高波动。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_growth_momentum(row) -> float:
    pe = safe(row.get("pe"), 999.0)
    pb = safe(row.get("pb"), 999.0)
    st = safe(row.get("shortterm"), 0.0)
    tech = safe(row.get("technical"), 0.0)
    std10 = safe(row.get("pct_chg_std_10d"), 0.0)

    score = 0.0
    if pe > 50 or pe <= 0:
        score += 6.0
    elif pe > 30:
        score += 3.0

    if pb > 5:
        score += 4.0
    elif pb > 3:
        score += 2.0

    if st >= 45 and tech >= 35 and std10 >= 3.5:
        score += 12.0

    return score
