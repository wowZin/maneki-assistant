"""Sentiment + Amount 组合（mine 备用）。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_sentiment_amount_combo(row) -> float:
    sent = safe(row.get("sentiment"), 0.0)
    avg_amount = safe(row.get("avg_amount_5d"), 0.0)
    amount_ratio = safe(row.get("amount_ratio"), 1.0)

    if sent < 30:
        return 0.0

    score = 0.0
    if sent >= 40:
        score += 15.0
    elif sent >= 35:
        score += 10.0
    elif sent >= 30:
        score += 5.0

    if avg_amount >= 2_000_000:
        score += 14.0
    elif avg_amount >= 1_000_000:
        score += 9.0
    elif avg_amount >= 500_000:
        score += 5.0

    if amount_ratio >= 1.5:
        score += 6.0
    elif amount_ratio >= 1.2:
        score += 3.0

    return round(score, 2)
