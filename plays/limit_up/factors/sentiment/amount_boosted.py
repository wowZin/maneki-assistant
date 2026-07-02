"""Sentiment + Amount 平滑增强 — total_score 组件 A (weight 0.4)."""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_sentiment_amount_boosted(row) -> float:
    sent = safe(row.get("sentiment"), 0.0)
    avg_amount = safe(row.get("avg_amount_5d"), 0.0)

    amount_score = 0.0
    if avg_amount >= 2_000_000:
        amount_score = 10.0
    elif avg_amount >= 1_000_000:
        amount_score = 5.0 + (avg_amount - 1_000_000) / 1_000_000 * 5.0
    elif avg_amount >= 300_000:
        amount_score = (avg_amount - 300_000) / 700_000 * 5.0

    return round(sent + amount_score * 0.2, 2)
