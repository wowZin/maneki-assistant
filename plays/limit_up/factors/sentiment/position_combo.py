"""Sentiment + Position 非线性组合 — total_score 组件 B (weight 0.5)."""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_sentiment_position_combo(row) -> float:
    sent = safe(row.get("sentiment"), 0.0)
    pos = safe(row.get("position_20d"), 0.5)
    t10 = safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)

    if sent < 30:
        return 0.0

    score = 0.0
    if sent >= 40:
        score += 20.0
    elif sent >= 35:
        score += 14.0
    elif sent >= 30:
        score += 8.0

    if 0.50 <= pos <= 0.85:
        score += 12.0
    elif 0.30 <= pos <= 0.90:
        score += 6.0
    elif pos > 0.95:
        score -= 8.0

    if 0.05 <= t10 <= 0.30:
        score += 8.0
    elif t10 > 0.45:
        score -= 10.0

    return round(max(0.0, score), 2)
