"""Sentiment + Volatility + Limit Gene 组合 — total_score 组件 C (weight 0.7)."""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_sentiment_volatility_combo(row) -> float:
    sent = safe(row.get("sentiment"), 0.0)
    std10 = safe(row.get("pct_chg_std_10d"), 0.0)
    gene20 = safe(row.get("limit_up_count_20d"), 0.0)

    if sent < 30:
        return 0.0

    score = 0.0
    if sent >= 40:
        score += 15.0
    elif sent >= 35:
        score += 10.0
    elif sent >= 30:
        score += 5.0

    if std10 >= 6.0:
        score += 12.0
    elif std10 >= 4.5:
        score += 7.0
    elif std10 >= 3.0:
        score += 3.0

    if gene20 >= 3:
        score += 10.0
    elif gene20 >= 2:
        score += 6.0
    elif gene20 >= 1:
        score += 3.0

    return round(score, 2)
