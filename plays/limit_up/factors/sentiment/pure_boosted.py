"""纯 sentiment 增强（仅追高惩罚）。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_sentiment_pure_boosted(row) -> float:
    sent = safe(row.get("sentiment"), 0.0)
    t10 = safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    t5 = safe(row.get("trailing_5_pit", row.get("trailing_5")), 0.0)
    pos = safe(row.get("position_20d"), 0.5)

    score = sent
    penalty = 1.0
    if t10 > 0.35:
        penalty *= 0.75
    elif t10 > 0.25:
        penalty *= 0.85
    elif t10 > 0.15:
        penalty *= 0.93
    if t5 > 0.18:
        penalty *= 0.90
    if pos > 0.90 and t10 > 0.20:
        penalty *= 0.80

    return round(max(0.0, score * penalty), 2)
