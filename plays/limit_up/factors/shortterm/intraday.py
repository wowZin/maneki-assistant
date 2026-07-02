"""盘中强度：扫描时涨幅 + 开盘缺口 + 放量。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_intraday_strength(row) -> float:
    pct = safe(row.get("pct_chg_score_day"), 0.0)
    gap = safe(row.get("gap_up_pit", row.get("gap_up")), 0.0)
    vol_ratio = safe(row.get("vol_ratio_proxy"), 1.0)
    position = safe(row.get("position_20d"), 0.5)

    score = 0.0
    if 1.5 <= gap <= 5.0 and 2.0 <= pct <= 7.0 and vol_ratio > 1.3 and 0.30 <= position <= 0.75:
        score += 18.0
    elif 0.5 <= gap <= 3.0 and 1.5 <= pct <= 5.0 and vol_ratio > 1.0 and position <= 0.80:
        score += 10.0
    elif pct > 0 and vol_ratio > 1.2 and position > 0.25:
        score += 4.0

    if gap > 7.0 or pct > 8.0:
        score -= 8.0

    return score
