"""涨停基因 + 技术共振 + 反追高。"""
from __future__ import annotations

import pandas as pd

from plays.limit_up.factors._helpers import safe


def factor_limit_gene_momentum(row) -> float:
    gene20 = safe(row.get("limit_up_count_20d"), 0.0)
    gene60 = safe(row.get("limit_up_count_60d"), 0.0)
    tech = safe(row.get("technical"), 0.0)
    t10 = safe(row.get("trailing_10_pit", row.get("trailing_10")), 0.0)
    position = safe(row.get("position_20d"), 0.5)

    score = 0.0
    if gene20 >= 3:
        score += 15.0
    elif gene20 >= 2:
        score += 10.0
    elif gene20 >= 1:
        score += 5.0

    if gene60 >= 4:
        score += 8.0
    elif gene60 >= 2:
        score += 4.0

    if tech >= 40:
        score += 8.0
    elif tech >= 25:
        score += 4.0

    if t10 > 0.35:
        score *= 0.60
    elif t10 > 0.25:
        score *= 0.80
    if position > 0.85:
        score *= 0.70

    return round(score, 2)
